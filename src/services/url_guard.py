"""服务端取用任意 URL 前的准入闸（§8.1-3/4，按 §13-5 降级为「随图源向导附带的
廉价防护」）。

## 为什么仍然需要它

§13-5 把整个项目的威胁模型钉在「只面向可信本机 / LAN」上，所以这里**不做**
公网级加固（没有 allowlist 白名单、没有鉴权、不防 DNS rebinding）。降级之后
剩下的攻击者只有一个：**用户自己粘贴进来的那条 URL**。而图源向导
（`source_wizard`）与地理编码（`geocoding`）的全部价值就在于接受任意 URL —— 它们
把「浏览器里的一次跨域请求」变成「Flask 进程里的一次同机请求」，于是：

    用户粘贴 http://169.254.169.254/latest/meta-data/iam/security-credentials/
      → 服务端替他去读，把云实例的临时凭据当成「瓦片响应」显示在向导页里

这一步不需要任何漏洞，只需要我们老老实实地按用户给的地址发请求。本模块就是
拦这一步的，成本是几十行纯 stdlib 代码。

## 形制来源与它比朴素写法强在哪

GeoLibre `apps/geolibre-desktop/src-tauri/src/lib.rs:730-790`（入口
`ensure_fetchable_url` :821）的覆盖面比常见的「判一下 is_private 就完事」多两类，
两类都是真实绕过手法：

1. **元数据端点的另类写法。** `169.254.169.254` 还可以写成 IPv4-mapped
   （`::ffff:169.254.169.254`）、IPv4-compatible（`::169.254.169.254`）与 6to4
   （`2002:a9fe:a9fe::`）。Python 的 `ipaddress` 对这三种的判定各不相同 ——
   实测（3.12）：`::169.254.169.254` 的 `is_private` 是 **False**（只有
   `is_reserved` 命中），`2002:a9fe:a9fe::` 的 `is_reserved` 是 **False**
   （只有 `is_private` 命中）。任何只看单一属性的实现都会漏掉其中一种，
   而放开 `allow_private` 之后漏的就是致命的那一种。因此本模块一律先
   **拆出内嵌的 IPv4**（见 `_embedded_ipv4s`）再判。
2. **每一跳都重新校验。** `workers/tiles/src/allowlisted-fetch.ts:12,63,84`
   最多跟 5 跳且每跳重校验，防的是「首跳合法、302 跳到同一共享域名下的
   另一个桶」。落到我们这儿更直接：`http://ok.example/r?to=169.254.169.254`
   这种开放重定向能把预检整个绕过去，因为预检只看了首跳的主机。

## 诚实的缺口（照抄 GeoLibre `lib.rs:887` 的自述）

**预检 → 发请求之间存在 TOCTOU：DNS rebinding 可以绕过本模块。** 我们在
`getaddrinfo` 里看到的地址与 urllib 真正连上去的地址是**两次独立解析**，恶意
DNS 可以让第一次返回 8.8.8.8、第二次返回 169.254.169.254。补它需要自己解析、
自己连 IP、再手工设 Host 头与 TLS SNI（并放弃 urllib 的连接复用与代理支持），
在 §13-5 的可信部署前提下不划算。写在这里是为了让下一个人**知道这是已知取舍
而不是遗漏** —— 已有的 `tile_url_probe.is_link_local_host` 同样留了这句话。

与 `tile_url_probe.should_bypass_proxy` 的分工：那个函数答的是「该不该走代理」
（路由问题），本模块答的是「该不该发这个请求」（安全问题）。两者的私网判定
故意不共用一份实现，因为语义相反 —— 自建瓦片镜像住在 192.168.x.x 是**正当
用法**（`allow_private=True` 就是给它开的门），而 169.254.169.254 从来不是一个
瓦片地址。代理**路由**规则本模块直接复用 `should_bypass_proxy`，不重写。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit

from src.core.config import Config
from src.services.proxy_autodetect import resolve_from_config
from src.services.system_proxy import mask_url_userinfo
from src.services.tile_url_probe import should_bypass_proxy

logger = logging.getLogger(__name__)

__all__ = ['UrlNotAllowed', 'ensure_fetchable_url', 'guarded_request',
           'MAX_REDIRECTS', 'DEFAULT_USER_AGENT']

# 只有这两个 scheme 有意义。显式列白名单而不是黑名单掉 file:// gopher:// dict://：
# urllib 的 opener 认得的 scheme 会随 Python 版本增减，黑名单必然滞后，
# 而 file:///etc/passwd 这类恰恰是本模块最该拦的东西。
_ALLOWED_SCHEMES = frozenset(('http', 'https'))

# 最多跟几跳重定向。5 跳与 GeoLibre allowlisted-fetch.ts:84 一致：正常的
# CDN/短链最多两三跳，再多就是重定向环或者在拿跳数当绕过手段。
MAX_REDIRECTS = 5

# 流式读取的步长。**刻意远小于 max_bytes**：`BufferedReader.read(n)` 会一直阻塞
# 到凑满 n 字节，所以按 64 KiB 读的话，一个每秒吐 1 字节的服务器能把一个 worker
# 线程按住 65536 秒，而 max_bytes 的计数器只在「一整块读完」时才前进，
# 永远也跳不到上限。改成小步长 + `read1`（至多一次底层读，不等凑满）之后，
# 每读回一点就能同时检查总时限和字节上限，慢速滴流在一个步长内就会暴露。
_READ_STEP_BYTES = 16 * 1024

# 丢弃 3xx 响应体时最多读多少。3xx 的 body 没有信息量，读它只为让连接可复用；
# 上游硬要在 302 里塞 10 GB 的话，直接放弃复用比陪它读完划算。
_DISCARD_BYTES = 64 * 1024

# 跨主机重定向时必须丢掉的请求头。
#
# 固定名单 + 一条模式：`Authorization` / `Cookie` 这类是死名字，而
# `X-Api-Key` / `X-Auth-Token` / `X-Amz-Security-Token` 这一类是各家自造的，
# 穷举不完，只能按词根认。宁可多丢一个自定义头（后果是这一跳少带一点上下文，
# 上游多半回 401，用户看得见）也不能少丢一个（后果是凭据被送到攻击者挑的主机上，
# 谁都看不见）。
_SENSITIVE_HEADERS = frozenset((
    'authorization', 'proxy-authorization', 'cookie', 'set-cookie'))
_SENSITIVE_HEADER_RE = re.compile(
    r'(?i)(api[-_]?key|auth|token|secret|credential|signature|session|passwd|password)')

# NAT64 的众所周知前缀（RFC 6052 §2.1）。放在模块级是为了只构造一次：
# `_embedded_ipv4s` 在重定向的每一跳、每一条 DNS 解析结果上都要跑。
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network('64:ff9b::/96')

# 默认 UA。这里刻意用**能识别本应用**的写法，而不是 basemap_static._UA 那条
# 伪装成 Chrome 的串：本模块服务的是地理编码与向导校验，Nominatim 一类服务的
# 使用政策明确要求可识别的 UA（伪装会被封），与瓦片 CDN 的风控方向正好相反。
DEFAULT_USER_AGENT = (f'TerraForge/{Config.APP_VERSION} '
                      '(+https://github.com/JungleZy/TerraForge)')

# 代理自动发现最多等多久。0 = 不等：本模块跑在同步 Flask 处理器里，用户正盯着
# 向导页的转圈。proxy_autodetect 默认愿意等 25 秒，那是给「跑几十分钟的下载
# 任务」的预算，套在一次交互请求上就是界面假死（basemap_static 出于同样理由
# 把它压到 3 秒）。等不到就走 urllib 的默认代理行为，见 _proxy_handler。
_PROXY_WAIT_S = 0.0


class UrlNotAllowed(ValueError):
    """URL 未通过准入闸。

    继承 `ValueError` 是刻意的：路由层统一用 `except ValueError` 映射 HTTP 400，
    而「这条 URL 不许取」本质上就是一次用户输入问题，不是服务端故障 ——
    回 500 会让用户以为是我们坏了，然后反复重试同一条地址。
    """


def _embedded_ipv4s(ip):
    """拆出一个 IPv6 地址里内嵌的 IPv4，没有则返回空 tuple。

    这是本模块的核心：`169.254.169.254` 有四种以上写法能穿过「只判一个属性」的
    实现（见模块头注释的实测数据）。把内嵌地址拆出来之后，判定就回到唯一一份
    IPv4 规则上，不必为每种写法各写一条网段。

    覆盖：
      - IPv4-mapped `::ffff:a.b.c.d`（`ipv4_mapped`）
      - 6to4 `2002:aabb:ccdd::`（`sixtofour`）
      - Teredo `2001:0:...`（`teredo` —— Python 已替我们反混淆客户端地址，
        攻击者把元数据地址藏在混淆位里同样会被拆出来）
      - NAT64 众所周知前缀 `64:ff9b::/96`（RFC 6052 §2.1，**Python 没有对应
        属性**）。它不是理论威胁：只要主机所在的网络上有 NAT64（IPv6-only
        与双栈环境的常态），`64:ff9b::a9fe:a9fe` 就是一条通往
        169.254.169.254 的实际可达路径，而这个地址的每一个属性
        （is_private / is_link_local / is_reserved）都是 False。
        `geocoding.search_places` 无条件传 `allow_private=True`，
        只剩「永久拦」那一层挡着 —— 不拆开就一路放行。
      - IPv4-compatible `::a.b.c.d`（同样没有属性，只能手算：
        高 96 位全 0 且不是 `::` / `::1`）
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return ()
    out = []
    for embedded in (ip.ipv4_mapped, ip.sixtofour):
        if embedded is not None:
            out.append(embedded)
    teredo = ip.teredo
    if teredo is not None:
        # (server, client) 两端都要判：把请求引到一个恶意 Teredo 服务器上
        # 同样是出网，不能只看客户端那一半。
        out.extend(teredo)
    packed = int(ip)
    if ip in _NAT64_WELL_KNOWN_PREFIX:
        out.append(ipaddress.IPv4Address(packed & 0xFFFFFFFF))
    if not out and 1 < packed <= 0xFFFFFFFF:
        out.append(ipaddress.IPv4Address(packed))
    return tuple(out)


def _blocked_reason(ip, allow_private: bool):
    """一个已解析地址是否被拦；返回原因字符串，None 表示放行。

    两层结构，分层的理由是 `allow_private` 只该打开**一半**的门：

    - **永久拦**（链路本地 / 组播 / 未指定）：`allow_private=True` 也不放。
      169.254.0.0/16 与 fe80::/10 上住着云厂商的实例元数据端点，它**从来不是**
      一个瓦片或地理编码服务地址；组播与 0.0.0.0 更不可能是。放开私网的正当
      场景是「局域网里的自建服务」，它与这些段没有任何交集。
    - **私网层**（回环 / 私网 / unique-local / 保留）：默认拦，`allow_private`
      放行。这一层拦的是 SSRF 的主体（探内网端口、读 127.0.0.1 上的管理接口），
      而放行它是 §13-5 明确认可的用法：自建瓦片服务器 / 自建 Nominatim。
    """
    # 内嵌 IPv4 的包装地址本身的属性不可信：Python 3.12 把 `::ffff:0:0/96` 与
    # `2002::/16` 整段列为 private，于是 `::ffff:8.8.8.8` 是「私网」而
    # `::169.254.169.254` 不是 —— 按包装层判会同时误伤和漏放。有内嵌地址时
    # **只**按内嵌的 IPv4 判（Python 3.13 的 is_private 也改成了这个口径）。
    targets = _embedded_ipv4s(ip) or (ip,)
    for target in targets:
        where = f'{ip}' if target is ip else f'{ip}（内嵌 {target}）'
        if target.is_link_local:
            return f'{where} 落在链路本地段，云实例元数据端点就住在这里'
        if target.is_multicast:
            return f'{where} 是组播地址'
        if target.is_unspecified:
            return f'{where} 是未指定地址'
    if allow_private:
        return None
    for target in targets:
        where = f'{ip}' if target is ip else f'{ip}（内嵌 {target}）'
        if target.is_loopback:
            return f'{where} 是回环地址'
        if target.is_private:
            return f'{where} 落在私有网段'
        if target.is_reserved:
            return f'{where} 落在保留网段'
    return None


def _resolve_addresses(host: str, port):
    """主机 → 所有解析结果的 IP 对象列表。

    **必须逐个检查全部结果，不能只看第一个。** 攻击者可以给同一个名字挂
    A=8.8.8.8 与 AAAA=::169.254.169.254 两条记录，或者按顺序轮换；urllib 连的是
    它自己选的那一条，我们放行了任何一条就等于放行了那一条。

    IP 字面量不做解析：`getaddrinfo` 对字面量虽然也能返回，但那是一次没必要的
    系统调用，而本模块的调用点在请求路径上（重定向每跳都要走一遍）。
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port or None,
                                   type=socket.SOCK_STREAM,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        # 解析不了就不放行：拿不到地址意味着我们无法判断它指向哪里，
        # 「判不了就放过」在安全闸里是错误的默认值。
        raise UrlNotAllowed(f'主机 {host} 无法解析：{e}') from e
    out = []
    for info in infos:
        sockaddr = info[4]
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            # AF_UNIX 之类不该出现在 TCP 解析结果里；出现了当作可疑，跳过。
            continue
    if not out:
        raise UrlNotAllowed(f'主机 {host} 没有解析出任何 IP 地址')
    return out


def ensure_fetchable_url(url: str, *, allow_private: bool = False) -> str:
    """校验一条 URL 可否由服务端代取；通过则返回规范化后的 URL，否则抛
    `UrlNotAllowed`。

    Args:
        allow_private: 放行回环 / 私网 / unique-local / 保留段。这是给
            **局域网自建服务**（自建瓦片镜像、自建 Nominatim）的显式逃生口，
            §13-5 认可的正当用法。即便打开，链路本地 / 组播 / 未指定段仍然拦死。

    返回的是**去掉 fragment 的** URL：`#` 之后的内容从不上网（HTTP 请求里没有
    这一段），留着它只会让日志与缓存键出现两条实际等价的 URL。
    """
    raw = (url or '').strip()
    if not raw:
        raise UrlNotAllowed('URL 为空')
    try:
        parts = urlsplit(raw)
    except ValueError as e:
        raise UrlNotAllowed(f'URL 无法解析：{e}') from e
    scheme = (parts.scheme or '').lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlNotAllowed(
            f'只支持 http/https，收到 {scheme or "（缺少 scheme）"}')
    try:
        host = (parts.hostname or '').lower()
        port = parts.port
    except ValueError as e:
        # 端口不是数字 / 越界时 urlsplit.port 才抛，不是解析时抛。
        raise UrlNotAllowed(f'URL 端口非法：{e}') from e
    if not host:
        raise UrlNotAllowed('URL 缺少主机名')
    # 内嵌凭据一律拒绝，两条理由缺一不可：
    # 1. **它是主机伪装的标准手法。** `http://mts0.google.com@169.254.169.254/`
    #    肉眼读起来像 Google，实际主机是元数据端点。我们自己解析得对，但用户
    #    在向导页上看到的回显会骗到他，把「服务端拒绝」变成「用户主动放行」。
    # 2. **凭据会外泄。** 一旦跟了重定向，urllib 会把 Authorization 连同
    #    userinfo 带到新主机上；而这条 URL 会被写进日志、任务记录与配置库。
    # 真要带认证就走 `headers={'Authorization': ...}`：那条路径**不会被持久化**，
    # 而且 `guarded_request` 在跨主机的那一跳上会主动把敏感头摘掉
    # （见 `_headers_for_hop`）。注意这不是天然属性 —— urllib 的
    # `Request.headers` 本身是跟着跳的，摘头是我们显式做的一步。
    if parts.username is not None or parts.password is not None:
        raise UrlNotAllowed(
            'URL 不允许内嵌用户名/密码（user:pass@host）；'
            '需要认证请用请求头传递')
    for ip in _resolve_addresses(host, port):
        reason = _blocked_reason(ip, allow_private)
        if reason:
            # 日志里带上掩码后的 URL：userinfo 形态（http://u:p@host/）常被用来
            # 伪装主机，排查时要看得见它，但不能把凭据写进日志。
            logger.warning(f'拒绝服务端代取 {mask_url_userinfo(raw)}：{reason}')
            raise UrlNotAllowed(f'不允许访问该地址：{reason}')
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ''))


def _proxy_handler(url: str, config_manager=None):
    """按项目统一口径决定这次请求走哪条出网路径。

    三分支与 `routes/basemap_static._fetch_upstream` 逐字同构，那边的注释解释了
    为什么第三种情况**不装** ProxyHandler：download_engine 的 aiohttp 开着
    `trust_env=True`，proxy_url 为空时照样吃 HTTP(S)_PROXY 环境变量（那是
    `apply_system_proxy()` 从 Windows 注册表灌进来的系统代理）。这里无条件传
    `ProxyHandler({})` 就会造成「下载正常、向导校验全失败」的分叉。

    内网/回环目标强制直连的判据直接复用 `tile_url_probe.should_bypass_proxy`：
    这条规则在项目里已经因为「验证走一套、下载走另一套」踩过坑（M4），
    第二份实现就是第二次踩坑的入口。
    """
    if should_bypass_proxy(url):
        return [urllib.request.ProxyHandler({})]
    proxy = ''
    if config_manager is not None:
        try:
            proxy = resolve_from_config(config_manager, wait_s=_PROXY_WAIT_S) or ''
        except Exception as e:
            # 配置库不可用（fresh clone 尚无 data/、cwd 不同）不该让一次校验请求
            # 失败：退回 urllib 默认行为，与「没配代理」等价。
            logger.warning(f'读取代理配置失败，按默认出网路径处理：{e}')
            proxy = ''
    if proxy:
        return [urllib.request.ProxyHandler({'http': proxy, 'https': proxy})]
    return []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 urllib 的自动重定向关掉。

    `redirect_request` 返回 None 时 urllib 不跟跳，3xx 会一路落到
    `HTTPDefaultErrorHandler` 变成 `HTTPError` —— 于是每一跳都回到我们手里，
    可以在跳之前重新过一遍 `ensure_fetchable_url`。这正是
    `allowlisted-fetch.ts:63,84` 的做法：**不重校验的重定向等于没有预检**。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_step(response, want: int) -> bytes:
    """读**至多** want 字节，不等凑满。

    优先用 `read1`：`HTTPResponse.read(n)` 底下是 `BufferedReader.read(n)`，
    语义是「阻塞到凑满 n 字节或 EOF」，所以慢速滴流的对端能把调用方按住任意久，
    期间我们既检查不了时限也检查不了字节数。`read1` 至多触发一次底层读，
    对端吐多少就返回多少，控制权立刻回到循环里。
    `HTTPError` 是 `addinfourl`，属性访问会转发给底下的 `HTTPResponse`，
    所以这条路径同样拿得到 `read1`；拿不到（测试替身、老式 file-like）时
    退回 `read`，行为不比改动前差。
    """
    read1 = getattr(response, 'read1', None)
    if read1 is not None:
        return read1(want)
    return response.read(want)


def _read_capped(response, max_bytes: int, deadline: float) -> bytes:
    """边读边掐 `max_bytes` 与总时限；超了分别抛 `UrlNotAllowed` / `TimeoutError`。

    三个决定值得写下来：

    1. **边读边掐，不是读完再判。** 读完再判时上游返回 10 GB 我们就先把 10 GB
       吃进内存 —— 那是一次 OOM，判定语句永远来不及执行。
    2. **超限抛错，不是静默截断。** 截断的后果是调用方拿到一段**语法上损坏**的
       JSON / 瓦片，报错点离真正的原因（响应太大）十万八千里。宁可明确失败。
    3. **每一次部分读之后都要判时限。** socket timeout 管的是「两次收包之间
       最多等多久」，一个每秒吐一个字节的服务器永远不触发它，却能把这个同步
       Flask worker 占用到天荒地老。总时限是唯一能终结这种对端的东西。
    """
    chunks = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f'读取响应体超过总时限（已读 {total} 字节）—— '
                f'对端可能在逐字节滴流')
        # 只多要一个字节就够判「超限」了，不必真把超出的部分读进来。
        chunk = _read_step(response, min(_READ_STEP_BYTES, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UrlNotAllowed(f'响应体超过 {max_bytes} 字节上限')
        chunks.append(chunk)
    return b''.join(chunks)


def _discard_body(response, deadline: float) -> None:
    """把 3xx 的响应体丢掉，让连接可复用。读不动就算了，绝不抛。

    同样走小步长 + 时限：一个「302 + 10 GB body」的上游不该在这里把整个请求
    的时间预算烧光 —— 那个 body 我们本来就不看。
    """
    dropped = 0
    try:
        while dropped < _DISCARD_BYTES and time.monotonic() < deadline:
            chunk = _read_step(response, _READ_STEP_BYTES)
            if not chunk:
                break
            dropped += len(chunk)
    except OSError:
        pass


def _is_sensitive_header(name: str) -> bool:
    lowered = (name or '').lower()
    return lowered in _SENSITIVE_HEADERS or bool(_SENSITIVE_HEADER_RE.search(lowered))


def _headers_for_hop(request_headers, origin_host: str, host: str):
    """本跳真正要发的请求头：跨主机时摘掉一切凭据性的头。

    调用方给的 `headers` 是**冲着第一个主机**去的。上游一个 302 就能把它送到
    任意别的主机上，而 `Authorization: Bearer ...` 跟过去就是一次凭据泄漏 ——
    浏览器与 curl 都会在跨源重定向时丢掉这些头，我们没有理由更宽松。
    比的是主机名而不是整个 origin：同主机换端口/换 scheme 属于上游自己的
    部署细节（http→https 升级是最常见的一跳），摘头只会让正常场景平白失败。
    """
    if host == origin_host:
        return request_headers
    kept = {k: v for k, v in request_headers.items() if not _is_sensitive_header(k)}
    dropped = [k for k in request_headers if k not in kept]
    if dropped:
        logger.warning('重定向跨主机（%s -> %s），丢弃请求头 %s',
                       origin_host, host, ', '.join(sorted(dropped)))
    return kept


def guarded_request(url, *, timeout=10, max_bytes=2_000_000,
                    allow_private=False, headers=None, config_manager=None):
    """过闸取一条 URL，返回 `(status, headers, body)`。

    - `timeout` 是**整个请求的墙钟总预算**，不是单次 socket 操作的上限：连接、
      每一跳重定向、读响应体全部算在这一个预算里，超了抛 `TimeoutError`。
      按 socket 计时是不够的 —— 「每次收包间隔 9 秒、每次吐 1 字节」的对端在
      per-socket 口径下可以合法地跑上几个小时，而本函数跑在同步 Flask 处理器
      里，那就是一个 worker 线程被一条 URL 永久占用。
    - `headers` 返回的是**键名全小写**的普通 dict。HTTP 头名大小写不敏感而
      `dict` 敏感，原样落盘会让 `Content-Type` / `content-type` 变成两个键，
      调用方必然在某一家上游身上踩到。
    - 非 2xx **不抛异常**，照样返回 `(status, headers, body)`：向导要能告诉用户
      「上游回了 403」，这是有效信息，不是我们的故障。
    - 传输层失败（连不上、超时、TLS 失败）原样抛 `urllib.error.URLError` /
      `OSError` 子类，不包装、不返回哨兵值：那是「这条 URL 取不到」而不是
      「这条 URL 不许取」，映射到 HTTP 400 会误导用户去改地址。
      `TimeoutError` 是 `OSError` 的子类，落在同一类里。
    - 最多跟 `MAX_REDIRECTS` 跳，**每跳都重新过 `ensure_fetchable_url`**，
      并且**跨主机的那一跳会摘掉凭据性请求头**（见 `_headers_for_hop`）。
      相对 Location 按当前 URL 解析（上游给相对路径完全合法）。

    用的是 stdlib `urllib` 而不是 aiohttp：调用点是同步 Flask 处理器，为了发一个
    请求现起一个事件循环不划算（`proxy_autodetect.verify_proxy` 出于同样理由
    也用 urllib）。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    current = ensure_fetchable_url(url, allow_private=allow_private)
    origin_host = (urlsplit(current).hostname or '').lower()
    request_headers = {'User-Agent': DEFAULT_USER_AGENT}
    request_headers.update(headers or {})
    for hop in range(MAX_REDIRECTS + 1):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(
                f'取回 {mask_url_userinfo(current)} 超过 {timeout} 秒总时限'
                f'（第 {hop + 1} 跳开始前）')
        opener = urllib.request.build_opener(
            _NoRedirect(), *_proxy_handler(current, config_manager))
        host = (urlsplit(current).hostname or '').lower()
        req = urllib.request.Request(
            current, headers=_headers_for_hop(request_headers, origin_host, host))
        try:
            # 单次 socket 操作也不许超过剩余预算：否则最后一跳仍能独享一个
            # 完整的 timeout，总预算就成了 (跳数+1) x timeout。
            with opener.open(req, timeout=left) as response:
                return (response.status,
                        {k.lower(): v for k, v in response.headers.items()},
                        _read_capped(response, max_bytes, deadline))
        except urllib.error.HTTPError as e:
            location = e.headers.get('Location') if e.headers else None
            if e.code not in (301, 302, 303, 307, 308) or not location:
                # 普通的 4xx/5xx：HTTPError 本身就是一个可读的文件对象，
                # 响应体照样过 max_bytes（错误页也能是 10 GB）。
                with e:
                    return (e.code,
                            {k.lower(): v for k, v in e.headers.items()}
                            if e.headers else {},
                            _read_capped(e, max_bytes, deadline))
            with e:
                _discard_body(e, deadline)
            if hop >= MAX_REDIRECTS:
                raise UrlNotAllowed(
                    f'重定向超过 {MAX_REDIRECTS} 跳，最后一跳指向 '
                    f'{mask_url_userinfo(location)}')
            # 关键的一步：新目标要从头再过一次闸。首跳合法 + 302 到
            # 169.254.169.254 是最省事的绕过手法，只校验首跳等于没校验。
            nxt = urljoin(current, location)
            logger.debug(f'重定向 {mask_url_userinfo(current)} -> '
                         f'{mask_url_userinfo(nxt)}（第 {hop + 1} 跳）')
            current = ensure_fetchable_url(nxt, allow_private=allow_private)
    # for 循环必然从 return 或 raise 出去（hop >= MAX_REDIRECTS 那条分支兜底），
    # 走到这里说明上面的跳数计算被改坏了 —— 宁可明确炸掉也不要返回 None。
    raise AssertionError('guarded_request 重定向循环未正常收敛')
