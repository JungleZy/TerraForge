"""瓦片专用端口 —— 把瓦片风暴从 API 的浏览器连接池里隔出去。

## 为什么需要它

浏览器对**单源**只开 6 条 HTTP/1.1 连接。底图/地形/历史瓦片全部走同源
转发（routes/basemap_static.py 等），首屏几十张瓦片、每张上游 RTT 秒级，
6 条连接被占满期间页面上一切 API 操作（配置保存、历史查询、SocketIO 握手）
都在浏览器连接池里排队 —— 「加载地图时别的请求都被阻塞」的现场。磁盘缓存
只挡重复访问，首访与平移到新区域时风暴照旧。

解法是把瓦片流量挪到**另一个源**：同一个 Flask app 在独立端口（默认
TILE_PORT）上再听一次，浏览器按 (host, port) 分连接池，瓦片风暴再也挤不到
API 的 6 条连接。

## 形态

- 与主服务**同一个 Flask app 实例**（werkzeug make_server，threaded），不是
  第二个 app：basemap_static 的冷却表、回退记录都挂在 app.extensions 上，
  换实例就等于让 /api/basemap 永远看不到回退状态。
- 外面套一层 wrap_tile_app：只放行瓦片前缀，其余 404；所有响应补
  Access-Control-Allow-Origin: * —— 换端口对浏览器就是跨源，Cesium 取
  瓦片 / layer.json 没有 CORS 头会直接失败。
- 端口被占等任何绑定失败都降级为 None：主服务照常，前端拿 tile_port=None
  退回同源路径（行为与没有本模块时完全一致）。
- 只由 server_runner 在**真正提供服务**的进程里启动；测试 / WSGI import
  路径不启动，描述符里的 tile_port 如实为 None。
"""

import contextlib
import io
import logging
import socket
import threading
from typing import Optional

from flask import current_app
from werkzeug.serving import BaseWSGIServer, make_server

from src.core.runtime_mode import SERVER_HOST, SERVER_PORT
from src.core.tile_paths import TILE_PATH_PREFIXES

logger = logging.getLogger(__name__)

# 默认主端口 + 1；run_server 使用自定义主端口时会显式传入实际的 port + 1。
# 前端始终从描述符读取最终监听端口，不自行猜测。
TILE_PORT = SERVER_PORT + 1

TILE_HEALTH_PATH = '/tile-health'

# 放行的瓦片类前缀来自 src/core/tile_paths.py（**唯一一份**，logging_setup 的
# 访问日志过滤器和 static/js/ui.js 的 tileUrl 用的是同一份名单）。名单外的路径
# （API、页面、静态资源）在瓦片端口上一律 404 —— 这个端口的存在理由就是分流
# 瓦片，不是给 API 开第二个入口。

# app.extensions 的键：server_runner 启动后把实际端口写进来（失败写 None），
# 描述符经 current_tile_port() 读。与其他热路径缓存同一口径（见
# basemap_static 的 _CACHE_KEY_SOURCE 注释）。
_EXTENSIONS_KEY = 'tile_server_port'


def wrap_tile_app(app):
    """把任意 WSGI app 包成「健康检查 + 只放行瓦片前缀 + CORS」的 app。"""
    def tile_app(environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == TILE_HEALTH_PATH:
            # 不带 Content-Length：RFC 7230 §3.3.2 禁止 204 携带它，而它在这里
            # 也没有任何用处（204 的语义本身就是没有正文）。
            start_response('204 No Content', [
                ('Access-Control-Allow-Origin', '*'),
                ('Cache-Control', 'no-store'),
            ])
            return [b'']
        if not path.startswith(TILE_PATH_PREFIXES):
            # 404 也带 CORS 头：跨源瓦片的 404 若不带，浏览器只报一句
            # CORS 错误，真实状态码被埋掉（basemap_static docstring 记过的坑）。
            start_response('404 Not Found', [
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Access-Control-Allow-Origin', '*'),
            ])
            return [b'not found']

        def cors_start_response(status, headers, exc_info=None):
            return start_response(
                status, headers + [('Access-Control-Allow-Origin', '*')],
                exc_info)

        return app(environ, cors_start_response)

    return tile_app


def _bind_conflict(host: str, port: int) -> Optional[OSError]:
    """先自己试着绑一下：绑不上返回那个 OSError，绑得上（或探不出来）返回 None。

    存在的理由是**用户看得见的那句话**：werkzeug 绑定失败会往 stderr 打印
    「Port 5001 is in use by another program. Either identify and stop that
    program, or start the server with a different port.」——而瓦片端口不可配置
    （固定主端口 +1，见 docs/guides/DISTRIBUTION.md），照这句话去找「换一个
    端口」的开关是找不到的；更糟的是它把一次只影响性能的自动降级说成了必须
    动手处理的故障。自己先探一次，绑不上就直接降级，make_server 根本不被调用。

    探测 socket 与 werkzeug 的监听 socket 用同一套选项（SO_REUSEADDR，见
    BaseWSGIServer.allow_reuse_address），判定口径才对得上。getaddrinfo 解不出
    来时返回 None 放行 —— 探不动就交给 make_server 定夺，那条路径外面还包了
    stderr 兜底。

    「同口径」只对**字面 IP** 严格成立（本仓实际传的是 0.0.0.0 / 127.0.0.1）。
    传主机名时两边挑的地址族可能不同：werkzeug 的 select_address_family 只看
    host 里有没有 ':'，没有就一律 AF_INET；这里的 getaddrinfo 不限族、取第一条，
    在 IPv6 优先的解析结果下会去探 ::1 而不是 127.0.0.1。偏差的两个方向都是安全
    的 —— 探得通而 make_server 绑不上，落到下面 try 里照样降级（stderr 已兜住）；
    探不通而 make_server 本可绑上，只是多降一次级，瓦片退回主端口同源出图。
    两种都不会出现「探测放行了一个起不来的服务却当成功」。
    """
    try:
        family, socktype, proto, _, sockaddr = socket.getaddrinfo(
            host or '0.0.0.0', port, type=socket.SOCK_STREAM)[0]
    except OSError:
        return None
    probe = socket.socket(family, socktype, proto)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(sockaddr)
        return None
    except OSError as e:
        return e
    finally:
        probe.close()


def start_tile_server(app, host: str = SERVER_HOST,
                      port: int = TILE_PORT) -> Optional[BaseWSGIServer]:
    """在后台线程起瓦片端口。成功返回 server，绑定失败返回 None（降级）。"""
    conflict = _bind_conflict(host, port)
    if conflict is not None:
        logger.warning(f'瓦片端口 {port} 启动失败，瓦片走主端口同源路径（{conflict}）')
        return None
    try:
        # 探测与真正绑定之间还有一个窗口（别的进程正好抢在中间）。这一层
        # redirect_stderr 只兜那个窗口：werkzeug 用 print(file=sys.stderr) 输出，
        # 而日志的 StreamHandler 早在建立时就拿住了真正的 stderr 对象，
        # 并发的日志不受影响。
        with contextlib.redirect_stderr(io.StringIO()):
            server = make_server(host, port, wrap_tile_app(app), threaded=True)
    except (OSError, SystemExit) as e:
        # werkzeug 绑定失败不是抛 OSError，而是打印一句后 sys.exit(1) ——
        # 不拦的话端口被占会把整个主服务一起带走。
        logger.warning(f'瓦片端口 {port} 启动失败，瓦片走主端口同源路径（{e}）')
        return None
    threading.Thread(target=server.serve_forever, daemon=True,
                     name='TileServer').start()
    logger.info(f'瓦片服务已监听 {host}:{server.server_port}')
    return server


def current_tile_port() -> Optional[int]:
    """当前 app 的瓦片端口；没启动过（测试/WSGI 路径）或启动失败时为 None。"""
    return current_app.extensions.get(_EXTENSIONS_KEY)


def record_tile_port(app, port: Optional[int]) -> None:
    """server_runner 启动后登记实际端口（含失败的 None），供描述符读取。"""
    app.extensions[_EXTENSIONS_KEY] = port
