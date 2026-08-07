"""代理自动发现（src/services/proxy_autodetect.py）的回归测试。

覆盖三类契约：
1. **优先级链** —— 手动 proxy_url 永远压过自动探测；开关关掉即退化成改造前行为。
2. **候选枚举与淘汰** —— SOCKS/畸形地址在归一化阶段就丢弃（没有 aiohttp-socks
   用不了）；PAC 只取 PROXY/HTTPS；扫描到的开放端口必须过真实验证才采用。
3. **不退化** —— 探测异常/全部失败时返回 ''，即"不显式指定代理"，与本模块
   引入之前完全一致。

所有网络行为（PAC 下载、代理验证、TCP 连通性）都注入假实现，无网可跑。
"""
import os
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.services import proxy_autodetect as pa


# clean_module_state 会把 verify_proxy 打成常量桩，先留一份原函数给它自己的用例。
_REAL_VERIFY_PROXY = pa.verify_proxy
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
             "NO_PROXY", "ALL_PROXY")


class FakeConfig:
    """ConfigManager 的最小替身：只需要 get(key, default)。"""

    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def clean_module_state(monkeypatch):
    """每个用例都从"没探过 + 无代理环境变量"开始，且绝不真的发包。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # 默认把三条外部依赖钉死成"什么都没有"，需要的用例再单独覆盖
    monkeypatch.setattr(pa, "_read_windows_autoconfig_url", lambda: "")
    monkeypatch.setattr(pa, "_port_open", lambda host, port: False)
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda url, probe_url=None, timeout_s=None: False)
    pa.reset_state()
    yield
    # 先 join 掉还在跑的后台探测线程再 reset：测「超时回退」的用例故意让
    # verify 慢，reset_state 之后那个线程仍会把自己的结果写进模块状态，
    # 污染下一个用例（实测 test_resolve_waits_for_running_detection 会拿到
    # 上一个用例的 http://slow:9）。
    for th in threading.enumerate():
        if th.name == 'proxy-autodetect':
            th.join(timeout=10)
    pa.reset_state()


# --- 归一化：候选进入验证环节之前的第一道闸 --------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("http://127.0.0.1:7890/", "http://127.0.0.1:7890"),
    ("https://proxy.corp:8443", "https://proxy.corp:8443"),
    ("http://user:pw@proxy.corp:8080", "http://user:pw@proxy.corp:8080"),
])
def test_normalize_accepts_usable_forms(raw, expected):
    assert pa._normalize_proxy_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "socks5://127.0.0.1:1080",   # aiohttp 不支持 SOCKS，留着只会白等一轮验证
    "socks4://127.0.0.1:1080",
    "http://127.0.0.1",          # 没端口
    "http://:7890",              # 没 host
    "not a url",
])
def test_normalize_rejects_unusable_forms(raw):
    assert pa._normalize_proxy_url(raw) == ""


def test_normalize_keeps_credentials():
    """认证代理离了 user:pass@ 直接连不上，归一化不能顺手洗掉。"""
    assert pa._normalize_proxy_url("user:pw@10.0.0.1:3128") == \
        "http://user:pw@10.0.0.1:3128"


# --- PAC 解析 ---------------------------------------------------------------

def test_pac_parse_takes_proxy_and_https_only():
    """SOCKS 指令要丢掉（用不了），PROXY/HTTPS 保留且按出现顺序。"""
    script = '''
    function FindProxyForURL(url, host) {
        if (isPlainHostName(host)) return "DIRECT";
        return "PROXY 127.0.0.1:7890; HTTPS gw.corp:8443; SOCKS5 127.0.0.1:1080; DIRECT";
    }
    '''
    assert pa.parse_pac_proxies(script) == [
        "http://127.0.0.1:7890", "https://gw.corp:8443",
    ]


def test_pac_parse_dedupes_repeated_addresses():
    script = 'return "PROXY 1.2.3.4:8080"; return "PROXY 1.2.3.4:8080";'
    assert pa.parse_pac_proxies(script) == ["http://1.2.3.4:8080"]


def test_pac_parse_empty_on_direct_only():
    assert pa.parse_pac_proxies('return "DIRECT";') == []
    assert pa.parse_pac_proxies("") == []


def test_pac_candidates_fetch_uses_registry_url(monkeypatch):
    seen = {}

    def _fake_fetch(url):
        seen["url"] = url
        return 'return "PROXY 10.1.1.1:3128";'

    monkeypatch.setattr(pa, "_read_windows_autoconfig_url",
                        lambda: "http://wpad/proxy.pac")
    monkeypatch.setattr(pa, "_fetch_pac_script", _fake_fetch)

    cands = pa._pac_candidates()
    assert seen["url"] == "http://wpad/proxy.pac"
    assert [c.url for c in cands] == ["http://10.1.1.1:3128"]
    assert cands[0].source == "pac"


def test_pac_candidates_empty_without_registry_entry(monkeypatch):
    """没配 PAC（非 Windows 或注册表没这一项）是正常路径，不该炸也不该发请求。"""
    def _boom(url):
        raise AssertionError("must not fetch a PAC when AutoConfigURL is absent")

    monkeypatch.setattr(pa, "_fetch_pac_script", _boom)
    assert pa._pac_candidates() == []


# --- 端口扫描 ---------------------------------------------------------------

def test_scan_reports_only_open_ports(monkeypatch):
    monkeypatch.setattr(pa, "wsl_host_ips", lambda: [])
    monkeypatch.setattr(pa, "_port_open",
                        lambda host, port: port == 7890)
    cands = pa._scan_candidates(ports=(7890, 10809))
    assert [c.url for c in cands] == ["http://127.0.0.1:7890"]
    assert cands[0].source == "scan"


def test_scan_covers_wsl_host_gateway(monkeypatch):
    """WSL 里代理跑在 Windows 宿主上 —— 只扫 127.0.0.1 等于什么都扫不到，
    这正是本模块存在的首要理由，必须覆盖网关地址。"""
    monkeypatch.setattr(pa, "wsl_host_ips", lambda: ["172.28.80.1"])
    monkeypatch.setattr(pa, "_port_open",
                        lambda host, port: host == "172.28.80.1" and port == 7890)
    cands = pa._scan_candidates(ports=(7890,))
    assert [c.url for c in cands] == ["http://172.28.80.1:7890"]


def test_scan_skips_socks_only_ports():
    """纯 SOCKS 端口不该进默认扫描列表：没有 aiohttp-socks，扫中了也用不了。"""
    assert 7891 not in pa.COMMON_PROXY_PORTS   # clash socks
    assert 10808 not in pa.COMMON_PROXY_PORTS  # v2rayN socks


def test_scan_skips_common_dev_server_ports():
    """8080/8888 撞开发服务器的概率远高于撞代理，扫它只是稳定地浪费验证预算。"""
    assert 8080 not in pa.COMMON_PROXY_PORTS
    assert 8888 not in pa.COMMON_PROXY_PORTS


# --- 环境变量候选 -----------------------------------------------------------

def test_env_candidates_prefer_https_then_http(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://1.1.1.1:1111")
    monkeypatch.setenv("HTTPS_PROXY", "http://2.2.2.2:2222")
    assert [c.url for c in pa._env_candidates()] == [
        "http://2.2.2.2:2222", "http://1.1.1.1:1111",
    ]


def test_env_candidates_drop_socks(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    assert pa._env_candidates() == []


# --- 枚举去重与优先级 -------------------------------------------------------

def test_detect_candidates_dedupes_across_sources(monkeypatch):
    """同一个地址同时来自环境变量和端口扫描时只验证一次，且记 env（先到者）。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(pa, "wsl_host_ips", lambda: [])
    monkeypatch.setattr(pa, "_port_open", lambda host, port: port == 7890)

    cands = pa.detect_candidates()
    assert [(c.url, c.source) for c in cands] == [
        ("http://127.0.0.1:7890", "env"),
    ]


# --- autodetect 编排 --------------------------------------------------------

def test_autodetect_picks_first_verified_candidate(monkeypatch):
    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://dead:1", "env"),
        pa.ProxyCandidate("http://alive:2", "scan"),
        pa.ProxyCandidate("http://never-tried:3", "scan"),
    ])
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda url, probe_url=None, timeout_s=None:
                        url == "http://alive:2")

    state = pa.autodetect()
    assert state["url"] == "http://alive:2"
    assert state["source"] == "scan"
    assert state["status"] == "done"
    # 命中即停：第三个候选不该被验证（每个候选最坏 6 秒）
    assert [c["url"] for c in state["candidates"]] == \
        ["http://dead:1", "http://alive:2"]


def test_autodetect_no_working_proxy_leaves_url_empty(monkeypatch):
    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://dead:1", "scan"),
    ])
    state = pa.autodetect()
    assert state["url"] == ""
    assert state["source"] == ""
    assert state["status"] == "done"
    assert pa.get_detected_proxy() == ""


def test_autodetect_survives_enumeration_failure(monkeypatch):
    """枚举炸了也不能把调用方带崩 —— 记 error，url 留空，状态照样收敛到 done。"""
    def _boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(pa, "detect_candidates", _boom)
    state = pa.autodetect()
    assert state["status"] == "done"
    assert state["url"] == ""
    assert "registry exploded" in state["error"]


def test_state_masks_proxy_credentials(monkeypatch):
    """代理凭据不能进状态快照（配置页会原样显示，日志也吃这份数据）。"""
    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://user:secret@gw:8080", "env"),
    ])
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda url, probe_url=None, timeout_s=None: True)
    state = pa.autodetect()
    assert "secret" not in state["url"]
    assert "***:***@gw:8080" in state["url"]
    assert "secret" not in state["candidates"][0]["url"]
    # 但实际使用的值必须是原始凭据，否则连不上
    assert pa.get_detected_proxy() == "http://user:secret@gw:8080"


# --- resolve_proxy_url：优先级链 -------------------------------------------

def test_manual_proxy_always_wins(monkeypatch):
    """手动配置压过一切，且不触发任何探测。"""
    def _boom():
        raise AssertionError("manual proxy must short-circuit detection")

    monkeypatch.setattr(pa, "detect_candidates", _boom)
    assert pa.resolve_proxy_url("http://manual:9") == "http://manual:9"


def test_manual_proxy_is_not_verified(monkeypatch):
    """手动值不做验证：用户说了算，探测不该越俎代庖把它判死。"""
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda *a, **kw: pytest.fail("manual value must not be verified"))
    assert pa.resolve_proxy_url("  http://manual:9  ") == "http://manual:9"


def test_auto_disabled_falls_back_to_direct(monkeypatch):
    """开关关掉 = 退化成本模块引入之前的行为：手动值原样返回，空即直连。"""
    def _boom():
        raise AssertionError("detection must not run when disabled")

    monkeypatch.setattr(pa, "detect_candidates", _boom)
    assert pa.resolve_proxy_url("", auto_enabled=False) == ""


def test_resolve_returns_detected_proxy(monkeypatch):
    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://found:7", "scan"),
    ])
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda url, probe_url=None, timeout_s=None: True)
    pa.autodetect()
    assert pa.resolve_proxy_url("") == "http://found:7"


def test_resolve_triggers_detection_when_never_run(monkeypatch):
    """从没探过时（脚本/测试路径，没走 create_app 钩子）自己触发一轮。"""
    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://lazy:8", "scan"),
    ])
    monkeypatch.setattr(pa, "verify_proxy",
                        lambda url, probe_url=None, timeout_s=None: True)
    assert pa.resolve_proxy_url("", wait_s=10) == "http://lazy:8"


def test_resolve_gives_up_after_wait_budget(monkeypatch):
    """探测慢过预算就回退直连，绝不把下载任务无限期挂在探测上。"""
    release = threading.Event()

    def _slow_verify(url, probe_url=None, timeout_s=None):
        release.wait(timeout=5)
        return True

    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://slow:9", "scan"),
    ])
    monkeypatch.setattr(pa, "verify_proxy", _slow_verify)

    pa.start_background_autodetect()
    started = time.monotonic()
    try:
        assert pa.resolve_proxy_url("", wait_s=0.2) == ""
        assert time.monotonic() - started < 2.0
    finally:
        release.set()


def test_resolve_waits_for_running_detection(monkeypatch):
    """后台那一轮马上就出结果时要等到它 —— 拿空值直连的代价是每张瓦片超时 30s。"""
    def _slow_verify(url, probe_url=None, timeout_s=None):
        time.sleep(0.15)
        return True

    monkeypatch.setattr(pa, "detect_candidates", lambda: [
        pa.ProxyCandidate("http://slowish:9", "scan"),
    ])
    monkeypatch.setattr(pa, "verify_proxy", _slow_verify)

    pa.start_background_autodetect()
    assert pa.resolve_proxy_url("", wait_s=10) == "http://slowish:9"


def test_concurrent_autodetect_runs_once(monkeypatch):
    """两条下载管线同时启动时不该叠加两轮探测（各自最坏二十几秒）。"""
    calls = []
    gate = threading.Event()

    def _counting_detect():
        calls.append(1)
        gate.wait(timeout=5)
        return []

    monkeypatch.setattr(pa, "detect_candidates", _counting_detect)

    pa.start_background_autodetect()
    # 让后台线程先进到 detect_candidates 里
    for _ in range(100):
        if calls:
            break
        time.sleep(0.01)
    assert pa.start_background_autodetect() is False
    assert pa.autodetect()["status"] == "detecting"
    gate.set()
    assert len(calls) == 1


# --- resolve_from_config：配置口径 ------------------------------------------

def test_resolve_from_config_reads_manual_value():
    cfg = FakeConfig(proxy_url="http://cfg:1", proxy_auto_detect="true")
    assert pa.resolve_from_config(cfg) == "http://cfg:1"


def test_resolve_from_config_honours_disable_switch(monkeypatch):
    def _boom():
        raise AssertionError("detection must not run when disabled")

    monkeypatch.setattr(pa, "detect_candidates", _boom)
    cfg = FakeConfig(proxy_url="", proxy_auto_detect="false")
    assert pa.resolve_from_config(cfg) == ""


@pytest.mark.parametrize("raw", ["true", "TRUE", "", None, "yes", "1"])
def test_auto_detect_enabled_defaults_to_on(raw):
    """只有明确的 'false' 才算关 —— 缺键/脏值按开启，探不到就回退直连。"""
    assert pa.auto_detect_enabled(FakeConfig(proxy_auto_detect=raw)) is True


@pytest.mark.parametrize("raw", ["false", "False", "  FALSE  "])
def test_auto_detect_enabled_off(raw):
    assert pa.auto_detect_enabled(FakeConfig(proxy_auto_detect=raw)) is False


# --- verify_proxy：真实验证的语义 -------------------------------------------

class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_opener(monkeypatch, response=None, error=None):
    class _Opener:
        def open(self, url, timeout=None):
            if error is not None:
                raise error
            return response

    monkeypatch.setattr(pa.urllib.request, "build_opener",
                        lambda *handlers: _Opener())


@pytest.fixture
def real_verify_proxy(monkeypatch):
    """clean_module_state 把 verify_proxy 打成了常量桩（其它用例不该真发包）。
    验证逻辑自己的用例要测真函数，这里换回来，只替掉底下的 opener。"""
    monkeypatch.setattr(pa, "verify_proxy", _REAL_VERIFY_PROXY)
    return _REAL_VERIFY_PROXY


def test_verify_proxy_accepts_200_with_body(monkeypatch, real_verify_proxy):
    _stub_opener(monkeypatch, response=_FakeResponse(200, b"\x89PNG"))
    assert real_verify_proxy("http://p:1") is True


def test_verify_proxy_rejects_non_200(monkeypatch, real_verify_proxy):
    """代理常以 407/502 回应 —— 端口开着不等于能用，这一步就是淘汰它们的。"""
    _stub_opener(monkeypatch, response=_FakeResponse(407, b"denied"))
    assert real_verify_proxy("http://p:1") is False


def test_verify_proxy_rejects_empty_body(monkeypatch, real_verify_proxy):
    """200 但空响应体：透明劫持/错误页常见形态，不能算通过。"""
    _stub_opener(monkeypatch, response=_FakeResponse(200, b""))
    assert real_verify_proxy("http://p:1") is False


def test_verify_proxy_swallows_network_errors(monkeypatch, real_verify_proxy):
    _stub_opener(monkeypatch, error=OSError("connection refused"))
    assert real_verify_proxy("http://p:1") is False


def test_verify_proxy_rejects_empty_url(real_verify_proxy):
    assert real_verify_proxy("") is False
