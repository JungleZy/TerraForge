"""代理自动发现：手动没配 proxy_url 时，自己找出一个**实测可用**的代理。

## 为什么需要这个模块

`system_proxy.apply_system_proxy()` 只覆盖了一种情况：OS 层面配了系统代理，
且 `urllib.request.getproxies()` 读得到。现实里这三类用户它一个都救不了：

1. **WSL / Linux** —— `getproxies()` 在 Linux 上只读环境变量，注释里写着
   "this becomes a no-op"。代理跑在 Windows 宿主机上，WSL 里既没有环境变量
   也读不到宿主注册表，于是必须手动填 `http://<网关IP>:7890`。
2. **Windows + PAC** —— 注册表里只有 `AutoConfigURL`，没有 `ProxyServer`。
   `getproxies()` 不解析 PAC，返回空。
3. **Windows + 只开端口不勾"系统代理"** —— Clash/v2rayN 监听着 7890/10809，
   注册表干净，同样读不到。

三种情况的共同表现都是"Google 瓦片全部 30s 超时"，而日志指不到代理这一层。

## 怎么找

候选按优先级枚举，**逐个用一张真实 Google 瓦片验证**，第一个通过的胜出：

  env  —— HTTP(S)_PROXY 环境变量（含 apply_system_proxy 灌进去的系统代理）
  pac  —— Windows 注册表 AutoConfigURL → 下载 PAC → 正则抽 PROXY 地址
  scan —— 本机 127.0.0.1 + WSL 网关上的常见代理端口 TCP 连通性扫描

验证是不可省的一环，不是保险：扫到端口开着不代表它是 HTTP 代理（7891/10808
是纯 SOCKS，本项目没装 aiohttp-socks 用不了），注册表里的系统代理也可能是
Clash 关掉后残留的死值。候选一律按 `http://` 试，能过验证的才算数。

## 与手动配置的关系

`resolve_proxy_url(manual)` 是全项目取代理的唯一入口，优先级：

  手动 proxy_url > 自动探测结果 > ''（直连）

手动值原样返回，不验证、不干预 —— 用户说了算。自动探测只在手动为空时介入，
且只会让情况变好：全部候选都不通过就返回 ''，与改造前完全一致（aiohttp 的
`trust_env=True` 仍会兜底读环境变量）。

## 线程模型

模块级单例状态 + 一把锁。启动时后台线程探一次（不阻塞 Flask 启动）；下载任务
若在探测完成前就要取值，`resolve_proxy_url` 会等一个有界的时长而不是拿空值走。
"""
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from src.services.system_proxy import mask_url_secrets

logger = logging.getLogger(__name__)

# 验证用的样例瓦片：项目实际要下的东西。z2 的一张图，几十 KB。
# 调用方可以传配置里 tile_servers 的第一条覆盖它（见 autodetect 的 probe_url）。
DEFAULT_PROBE_URL = 'http://mts0.googleapis.com/vt?lyrs=m&x=1&y=1&z=2'

# 常见 HTTP 代理端口。刻意**不含** 8080/8888/3000 —— 撞开发服务器的概率远高于
# 撞代理，扫中了也会在验证环节被淘汰，白白多花 6 秒。
# 7891(clash socks)/10808(v2rayN socks) 同样不列：纯 SOCKS 端口，没有
# aiohttp-socks 就用不了，列进来只是稳定地浪费一轮验证。
COMMON_PROXY_PORTS = (
    7890,   # Clash / Clash for Windows —— mixed port，HTTP 与 SOCKS 同端口
    7897,   # Clash Verge rev 默认 mixed port
    # 7892 在 Clash 官方模板里是 `redir-port`（Linux/macOS 的透明代理口），**不是**
    # HTTP 代理口。列它的理由是现实：把 mixed-port 挪到 7892 的配置很常见，本仓
    # 开发机的 Windows 宿主就是这样（WSL 里 `curl -x http://<宿主>:7892` 通，而
    # 7890/7897 都没开），于是自动探测在这台机器上永远是 0 候选，只能手填。
    # 代价是把 7892 真当 redir-port 用的机器上多花一轮 6 秒验证 —— 那种机器的流量
    # 本来就被透明劫持，探不到代理照样能出网，多这一轮不影响结果。
    7892,
    10809,  # v2rayN HTTP
    2080,   # sing-box / Nekoray mixed
    1087,   # Shadowsocks-NG / 老 v2rayX HTTP
    8889,   # Surge / 部分 Shadowsocks 客户端 HTTP
    20172,  # Mellow / Netch
    8118,   # Privoxy
    3128,   # Squid
)

_TCP_PROBE_TIMEOUT_S = 0.35     # 端口是否开着，本机/局域网内一次 RTT 足够
_PAC_FETCH_TIMEOUT_S = 3.0
_VERIFY_TIMEOUT_S = 6.0
# resolve_proxy_url 在后台探测还没跑完时最多等这么久。超时就按"暂无"返回，
# 不让一次探测把整个下载任务的启动挂住。
_WAIT_FOR_DETECTION_S = 25.0

# PAC 里的代理指令：`PROXY host:port` / `HTTPS host:port` / `SOCKS5 host:port`。
# 只取 PROXY 与 HTTPS —— SOCKS 没有 aiohttp 支持，抽出来也用不了。
_PAC_DIRECTIVE_RE = re.compile(
    r'\b(PROXY|HTTPS)\s+([A-Za-z0-9._\-]+:\d{1,5})', re.IGNORECASE)

_WINDOWS_INET_SETTINGS = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'


class ProxyCandidate:
    """一个待验证的代理候选。`source` 只用于展示与日志。"""

    __slots__ = ('url', 'source')

    def __init__(self, url: str, source: str):
        self.url = url
        self.source = source

    def __eq__(self, other):
        return isinstance(other, ProxyCandidate) and self.url == other.url

    def __hash__(self):
        return hash(self.url)

    def __repr__(self):
        return f'ProxyCandidate({mask_url_secrets(self.url)!r}, {self.source!r})'


def _normalize_proxy_url(value: str) -> str:
    """把 `host:port` / `http://host:port/` 归一成 `http://host:port`。

    不合法（缺 host、缺端口、端口越界）一律返回 ''，由调用方丢弃。
    """
    value = (value or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'http://' + value
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return ''
    if not host or not port:
        return ''
    if parts.scheme not in ('http', 'https'):
        return ''      # socks5:// 等：aiohttp 不支持，当场丢弃
    # userinfo（user:pass@）要保留 —— 认证代理离了它连不上
    userinfo = parts.netloc.rsplit('@', 1)[0] + '@' if '@' in parts.netloc else ''
    return f'{parts.scheme}://{userinfo}{host}:{port}'


# --- 候选来源 ---------------------------------------------------------------

def _env_candidates():
    """环境变量里的代理。apply_system_proxy() 已经把 OS 系统代理灌到这里，
    所以这一条同时覆盖了"用户手动 export"和"Windows 注册表系统代理"两种。

    **同一个地址只排一次。** 两个原因，任一单独成立都要求去重：
    Windows 的 os.environ 是**大小写不敏感**的（Python 把 key 统一大写），
    `HTTPS_PROXY` 与 `https_proxy` 读到的是同一个变量，四个 key 会拿到两两
    重复的值；而在 Linux/macOS 上，同时 export 大小写两份本来就是常见做法。
    不去重的话同一个代理会被排进候选两次，验证也就跑两次 —— 每次验证都是一
    次真实的网络往返，而候选列表是有验证预算的。
    """
    out, seen = [], set()
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        url = _normalize_proxy_url(os.environ.get(key, ''))
        if url and url not in seen:
            seen.add(url)
            out.append(ProxyCandidate(url, 'env'))
    return out


def _read_windows_autoconfig_url() -> str:
    """读注册表 HKCU 的 AutoConfigURL。非 Windows / 没配 PAC 都返回 ''。"""
    if not sys.platform.startswith('win'):
        return ''
    try:
        import winreg
    except ImportError:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_INET_SETTINGS) as key:
            value, _ = winreg.QueryValueEx(key, 'AutoConfigURL')
        return str(value or '').strip()
    except OSError:
        # FileNotFoundError = 没配 PAC，是最常见的正常路径，不该刷 warning
        return ''


def _fetch_pac_script(pac_url: str) -> str:
    """下载 PAC 脚本。**必须直连** —— 走代理去取"怎么找代理"是鸡生蛋。"""
    if not pac_url:
        return ''
    if pac_url.startswith('file://'):
        try:
            with urllib.request.urlopen(pac_url, timeout=_PAC_FETCH_TIMEOUT_S) as resp:
                return resp.read(256 * 1024).decode('utf-8', errors='replace')
        except (OSError, urllib.error.URLError) as e:
            logger.debug(f'PAC file read failed: {e}')
            return ''
    if not pac_url.startswith(('http://', 'https://')):
        return ''
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(pac_url, timeout=_PAC_FETCH_TIMEOUT_S) as resp:
            return resp.read(256 * 1024).decode('utf-8', errors='replace')
    except (OSError, urllib.error.URLError, ValueError) as e:
        logger.debug(f'PAC fetch failed for {pac_url}: {e}')
        return ''


def parse_pac_proxies(script: str):
    """从 PAC 脚本里正则抽出 PROXY/HTTPS 指令的地址，按出现顺序去重。

    刻意**不跑 JS**：PAC 是一段 FindProxyForURL(url, host) 函数，真求值要带
    dnsDomainIs/isInNet 等一整套宿主 API 和一个 JS 引擎。而实际部署里 99% 的
    PAC（Clash/公司网关生成的那种）代理地址都是字面量常量，正则抽得到。
    抽错了也没关系 —— 后面还有一道真实瓦片验证。

    `PROXY` 与 `HTTPS` 是**到代理自身**的连接方式（明文 / TLS），不是目标
    协议，所以指令关键字决定 scheme：丢掉 HTTPS 会让 TLS 代理被当明文连，
    握手直接失败。SOCKS/SOCKS4/SOCKS5 不在正则里 —— 没有 aiohttp-socks 用不了。
    """
    seen, out = set(), []
    for kind, addr in _PAC_DIRECTIVE_RE.findall(script or ''):
        scheme = 'https' if kind.upper() == 'HTTPS' else 'http'
        url = _normalize_proxy_url(f'{scheme}://{addr}')
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _pac_candidates():
    pac_url = _read_windows_autoconfig_url()
    if not pac_url:
        return []
    urls = parse_pac_proxies(_fetch_pac_script(pac_url))
    if urls:
        logger.info(f'PAC {pac_url} yielded {len(urls)} proxy candidate(s)')
    return [ProxyCandidate(u, 'pac') for u in urls]


def is_wsl() -> bool:
    """WSL1/WSL2 判定。/proc/version 里带 microsoft 标记。"""
    if not sys.platform.startswith('linux'):
        return False
    try:
        with open('/proc/version', 'r', encoding='utf-8', errors='replace') as f:
            return 'microsoft' in f.read().lower()
    except OSError:
        return False


def wsl_host_ips():
    """WSL 里 Windows 宿主机的可能 IP。

    - NAT 模式（默认）：宿主是默认网关，`ip route` 的 `default via <IP>`。
      /etc/resolv.conf 的 nameserver 通常是同一个地址，但开了
      `generateResolvConf=false` 或用了 mirrored 网络模式时两者会分叉，
      所以两个都收。
    - 镜像模式（Win11 mirrored）：宿主就是 127.0.0.1，已在扫描主机里覆盖。
    """
    if not is_wsl():
        return []
    ips, seen = [], set()

    def _add(raw):
        raw = (raw or '').strip()
        if not raw or raw in seen:
            return
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return
        if ip.is_loopback:
            return          # 127.0.0.1 已经在固定扫描列表里
        seen.add(raw)
        ips.append(raw)

    try:
        with open('/proc/net/route', 'r', encoding='utf-8') as f:
            next(f, None)   # 表头
            for line in f:
                cols = line.split()
                # Destination 全 0 即默认路由；Gateway 是小端序的十六进制
                if len(cols) > 2 and cols[1] == '00000000':
                    packed = int(cols[2], 16).to_bytes(4, 'little')
                    _add(socket.inet_ntoa(packed))
    except (OSError, ValueError, IndexError) as e:
        logger.debug(f'Failed to read default gateway from /proc/net/route: {e}')

    try:
        with open('/etc/resolv.conf', 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('nameserver'):
                    parts = line.split()
                    if len(parts) > 1:
                        _add(parts[1])
    except OSError as e:
        logger.debug(f'Failed to read /etc/resolv.conf: {e}')

    return ips


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_TCP_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _scan_candidates(ports=COMMON_PROXY_PORTS):
    """扫 127.0.0.1（+ WSL 宿主网关）上的常见代理端口。

    只做 TCP connect，不发数据 —— 判"端口开着"，是否真是 HTTP 代理留给验证。
    全部并发，总耗时约等于单次超时（0.35s）。
    """
    hosts = ['127.0.0.1'] + wsl_host_ips()
    targets = [(h, p) for h in hosts for p in ports]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
        opened = list(pool.map(lambda t: _port_open(*t), targets))
    out = [ProxyCandidate(f'http://{h}:{p}', 'scan')
           for (h, p), ok in zip(targets, opened) if ok]
    if out:
        logger.info(f'Port scan found {len(out)} open proxy port(s): '
                    f'{[c.url for c in out]}')
    return out


def detect_candidates():
    """枚举全部候选，按优先级排序并去重。纯枚举，不发 HTTP 验证请求。"""
    seen, out = set(), []
    for group in (_env_candidates(), _pac_candidates(), _scan_candidates()):
        for cand in group:
            if cand.url not in seen:
                seen.add(cand.url)
                out.append(cand)
    return out


# --- 验证 -------------------------------------------------------------------

def verify_proxy(proxy_url: str, probe_url: str = DEFAULT_PROBE_URL,
                 timeout_s: float = _VERIFY_TIMEOUT_S) -> bool:
    """经 proxy_url 真实取一张瓦片，200 且有响应体才算通过。

    用 urllib 而不是 aiohttp：这里跑在普通后台线程里，没有事件循环，起一个
    只为发一个请求不划算；且 urllib 的 ProxyHandler 与 aiohttp 的显式
    `proxy=` 走的是同一套 HTTP CONNECT / 绝对 URI 语义，验证结论可迁移。
    """
    if not proxy_url:
        return False
    handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(probe_url, timeout=timeout_s) as resp:
            if resp.status != 200:
                return False
            return bool(resp.read(1024))
    except (OSError, urllib.error.URLError, ValueError) as e:
        logger.debug(f'Proxy verification failed for '
                     f'{mask_url_secrets(proxy_url)}: {e}')
        return False


# --- 状态与编排 -------------------------------------------------------------

_lock = threading.RLock()
_done = threading.Event()       # 至少完成过一轮探测
_running = False
_state = {
    'status': 'idle',           # idle | detecting | done
    'url': '',                  # 已验证可用的代理；'' = 没找到
    'source': '',               # env | pac | scan
    'candidates': [],           # [{url(掩码), source, verified}]
    'checked_at': None,         # epoch 秒
    'error': None,
}


def get_state() -> dict:
    """当前探测状态的快照（可 JSON 化，代理凭据已掩码）。"""
    with _lock:
        return {
            'status': _state['status'],
            'url': mask_url_secrets(_state['url']) if _state['url'] else '',
            'source': _state['source'],
            'candidates': list(_state['candidates']),
            'checked_at': _state['checked_at'],
            'error': _state['error'],
        }


def get_detected_proxy() -> str:
    """已验证可用的代理原值（未掩码）。没有则 ''。"""
    with _lock:
        return _state['url']


def autodetect(probe_url: str = DEFAULT_PROBE_URL) -> dict:
    """枚举 → 逐个验证 → 记录状态。同步执行，返回 get_state() 快照。

    第一个通过验证的候选胜出，后面的不再验证（每个候选最坏 6 秒，串行验证
    完 5 个候选就是半分钟，用户等不起）。全部失败时 url 留空 —— 调用方据此
    回退直连，与本模块存在之前的行为一致。
    """
    global _running
    with _lock:
        if _running:
            # 已有一轮在跑：不叠加第二轮，直接返回当前快照
            return get_state()
        _running = True
        _state.update(status='detecting', error=None, candidates=[])
    started = time.monotonic()
    winner, tried, error = None, [], None
    try:
        candidates = detect_candidates()
        for cand in candidates:
            ok = verify_proxy(cand.url, probe_url=probe_url)
            tried.append({'url': mask_url_secrets(cand.url),
                          'source': cand.source, 'verified': ok})
            if ok:
                winner = cand
                break
    except Exception as e:                       # 探测绝不能把调用方带崩
        logger.warning(f'Proxy autodetect failed: {e!r}')
        error = str(e)
    finally:
        with _lock:
            _state.update(
                status='done',
                url=winner.url if winner else '',
                source=winner.source if winner else '',
                candidates=tried,
                checked_at=time.time(),
                error=error,
            )
            _running = False
        _done.set()

    elapsed = round((time.monotonic() - started) * 1000)
    if winner:
        logger.info(f'Proxy autodetect: using {mask_url_secrets(winner.url)} '
                    f'(source={winner.source}, {len(tried)} candidate(s) tried, '
                    f'{elapsed}ms)')
    else:
        logger.info(f'Proxy autodetect: no working proxy found '
                    f'({len(tried)} candidate(s) tried, {elapsed}ms) — direct connection')
    return get_state()


def start_background_autodetect(probe_url: str = DEFAULT_PROBE_URL) -> bool:
    """后台线程跑一轮探测。启动路径用，绝不阻塞。

    返回 False 表示已有一轮在跑（或已完成）而没有新起线程。
    """
    with _lock:
        if _running:
            return False
    _done.clear()
    threading.Thread(
        target=autodetect, kwargs={'probe_url': probe_url},
        name='proxy-autodetect', daemon=True,
    ).start()
    return True


def reset_state():
    """把状态清回未探测。测试与「重新检测」按钮用。"""
    global _running
    with _lock:
        _running = False
        _state.update(status='idle', url='', source='', candidates=[],
                      checked_at=None, error=None)
    _done.clear()


def resolve_proxy_url(manual_proxy_url: str = '', auto_enabled: bool = True,
                      wait_s: float = _WAIT_FOR_DETECTION_S) -> str:
    """全项目取代理的唯一入口。优先级：手动 > 自动探测 > 直连。

    Args:
        manual_proxy_url: 配置页 proxy_url 的值。非空即直接采用，不验证、
            不干预 —— 用户显式配置永远压过自动探测。
        auto_enabled: 配置项 proxy_auto_detect。关掉后本函数退化成
            "原样返回手动值"，即本模块引入之前的行为。
        wait_s: 后台探测尚未完成时最多等多久。等不到就返回 ''（回退直连 +
            aiohttp trust_env 兜底），不把下载任务挂在探测上。

    Returns:
        代理 URL，'' 表示不显式指定代理。
    """
    manual = (manual_proxy_url or '').strip()
    if manual:
        return manual
    if not auto_enabled:
        return ''

    detected = get_detected_proxy()
    if detected:
        return detected

    with _lock:
        running, finished = _running, _state['status'] == 'done'
    if not running and not finished:
        # 从没探过（create_app 没跑启动钩子的测试/脚本路径）。仍然走后台线程
        # 而不是就地同步探：本函数会被 download_engine 从事件循环里
        # （经 asyncio.to_thread）调用，两条路径共用一份超时预算更好推理。
        start_background_autodetect()
        running = True
    if not running:
        return ''
    # 后台正在探 —— 等一个有界的时长，比拿空值直连然后每张瓦片超时 30s 强
    if _done.wait(timeout=wait_s):
        return get_detected_proxy()
    logger.debug(f'Proxy autodetect still running after {wait_s}s — '
                 f'falling back to direct connection')
    return ''


def auto_detect_enabled(config_manager) -> bool:
    """读 proxy_auto_detect 开关。缺键/脏值一律按开启 —— 自动探测只在手动值
    为空时介入，且探不到就回退直连，默认开的下行风险是几秒探测耗时。"""
    raw = (config_manager.get('proxy_auto_detect', 'true') or 'true')
    return str(raw).strip().lower() != 'false'


def resolve_from_config(config_manager, wait_s: float = _WAIT_FOR_DETECTION_S) -> str:
    """从 ConfigManager 取生效代理。下载引擎与配置页探测路由共用这一个入口，
    避免"验证走一套、下载走另一套"的经典分叉（M4 就是这么来的）。

    注意本函数可能阻塞到 wait_s：在 async 上下文里必须
    `await asyncio.to_thread(resolve_from_config, cm)`，不要直接调用。
    """
    return resolve_proxy_url(
        config_manager.get('proxy_url', '') or '',
        auto_enabled=auto_detect_enabled(config_manager),
        wait_s=wait_s,
    )
