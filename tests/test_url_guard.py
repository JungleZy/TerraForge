"""url_guard —— 服务端代取任意 URL 前的准入闸。

威胁模型（§13-5）里真实存在的攻击者是「用户粘贴进向导页的一条 URL」和
「用户自己配置的那台服务器返回的一个 302」。这道闸拦的就是这两件事：

- 云实例元数据端点 `169.254.169.254` 有四种以上写法能穿过「只判一个属性」的
  实现（`::ffff:` 映射、`::` 兼容、`2002:` 6to4、Teredo）。Python 3.12 里
  `::ffff:8.8.8.8` 反而是「私网」而 `::169.254.169.254` 不是 —— 按包装层判
  会同时误伤和漏放。
- **首跳合法 + 302 到元数据端点**是最省事的绕过手法。只校验首跳等于没校验。

本文件一次网络都不发：`ensure_fetchable_url` 全部喂 IP 字面量（不走 DNS），
`guarded_request` 的 opener 被换成手写替身。
"""
import io
import os
import sys
import urllib.error
import urllib.request
from email.message import Message

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.url_guard import (  # noqa: E402
    MAX_REDIRECTS,
    UrlNotAllowed,
    ensure_fetchable_url,
    guarded_request,
)

#: 元数据端点的各种写法。都必须被同一条 IPv4 规则拦下 —— 而不是为每种写法
#: 各写一条网段（写漏一条就是一个可用的 SSRF 出口）。
METADATA_SPELLINGS = [
    'http://169.254.169.254/latest/meta-data/',            # 明文
    'http://[::ffff:169.254.169.254]/latest/',             # IPv4-mapped
    'http://[::169.254.169.254]/latest/',                  # IPv4-compatible
    'http://[2002:a9fe:a9fe::]/latest/',                   # 6to4
    'http://[2001:0:a9fe:a9fe::]/latest/',                 # Teredo（服务器侧）
]


# ---------------------------------------------------------------------------
# ensure_fetchable_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url', METADATA_SPELLINGS)
def test_every_metadata_spelling_is_blocked(url):
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url)


@pytest.mark.parametrize('url', METADATA_SPELLINGS)
def test_metadata_stays_blocked_even_with_allow_private(url):
    """`allow_private` 只该打开**一半**的门。

    放开私网的正当场景是「局域网里的自建瓦片服务 / 自建 Nominatim」，它与
    链路本地段没有任何交集 —— 而云厂商的实例元数据端点就住在那里。
    """
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url, allow_private=True)


@pytest.mark.parametrize('url', [
    'file:///etc/passwd',
    'gopher://127.0.0.1:70/',
    'dict://127.0.0.1:2628/',
    'ftp://192.168.1.1/x',
    'javascript:alert(1)',
    '/relative/path',
    '',
    '   ',
])
def test_non_http_schemes_are_rejected(url):
    """白名单而不是黑名单：urllib 认得的 scheme 会随 Python 版本增减。"""
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url)


@pytest.mark.parametrize('url', [
    'http://mts0.google.com@169.254.169.254/',
    'http://user:pass@93.184.216.34/tiles/1/2/3.png',
    'https://:secret@93.184.216.34/',
])
def test_embedded_credentials_are_rejected(url):
    """两条理由缺一不可：它是主机伪装的标准手法，而且凭据会随重定向外泄。

    `http://mts0.google.com@169.254.169.254/` 肉眼读起来像 Google，实际主机是
    元数据端点 —— 我们解析得对，但用户在向导页上看到的回显会骗到他。
    """
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8080/tiles/{z}/{x}/{y}.png',
    'http://[::1]:8080/tiles/',
    'http://192.168.1.10/tiles/',
    'http://10.0.0.5/tiles/',
    'http://[fd00::1]/tiles/',
])
def test_private_addresses_are_blocked_by_default(url):
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8080/tiles/',
    'http://192.168.1.10/tiles/',
    'http://[fd00::1]/tiles/',
])
def test_allow_private_opens_the_lan_escape_hatch(url):
    """自建瓦片镜像 / 自建 Nominatim 是 §13-5 明确认可的用法。"""
    assert ensure_fetchable_url(url, allow_private=True).startswith('http://')


@pytest.mark.parametrize('url', [
    'http://0.0.0.0/x',            # 未指定
    'http://224.0.0.1/x',          # 组播
    'http://[ff02::1]/x',          # 组播（v6）
    'http://169.254.1.1/x',        # 链路本地
])
def test_permanently_blocked_ranges_ignore_allow_private(url):
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url(url, allow_private=True)


def test_public_url_passes_and_loses_its_fragment():
    """`#` 之后的内容从不上网；留着只会让日志与缓存键出现两条等价 URL。"""
    got = ensure_fetchable_url('https://93.184.216.34/a/b?z=1#frag')
    assert got == 'https://93.184.216.34/a/b?z=1'


def test_an_illegal_port_is_a_user_input_error_not_a_crash():
    """`urlsplit.port` 在取值时才抛，不是解析时 —— 漏掉就是 HTTP 500。"""
    with pytest.raises(UrlNotAllowed):
        ensure_fetchable_url('http://93.184.216.34:99999/x')


def test_url_not_allowed_is_a_value_error():
    """路由层统一 `except ValueError` → HTTP 400。回 500 会让用户以为是我们坏了。"""
    assert issubclass(UrlNotAllowed, ValueError)


# ---------------------------------------------------------------------------
# guarded_request（opener 全部替身，绝不出网）
# ---------------------------------------------------------------------------

def _headers(pairs):
    message = Message()
    for key, value in pairs.items():
        message[key] = value
    return message


class FakeResponse(io.BytesIO):
    """最小的 urllib 响应替身：status / headers / read + 上下文管理。"""

    def __init__(self, status, headers, body):
        super().__init__(body)
        self.status = status
        self.headers = _headers(headers)

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    """按顺序吐出预置结果的 opener。记录每一跳请求的 URL 供断言。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.seen = []

    def open(self, request, timeout=None):
        self.seen.append(request.full_url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_opener(monkeypatch):
    """把 `build_opener` 换掉。装了什么 handler 不重要，本文件只测闸门逻辑。"""
    holder = {}

    def _install(*outcomes):
        opener = FakeOpener(outcomes)
        holder['opener'] = opener
        monkeypatch.setattr(urllib.request, 'build_opener',
                            lambda *handlers: opener)
        return opener

    return _install


def test_guarded_request_returns_lowercased_headers_and_the_body(fake_opener):
    """HTTP 头名大小写不敏感而 dict 敏感 —— 原样落盘必然在某一家上游身上踩到。"""
    fake_opener(FakeResponse(200, {'Content-Type': 'image/png',
                                   'X-Cache': 'HIT'}, b'tile-bytes'))
    status, headers, body = guarded_request('http://93.184.216.34/t.png')
    assert status == 200
    assert headers['content-type'] == 'image/png'
    assert headers['x-cache'] == 'HIT'
    assert body == b'tile-bytes'


def test_guarded_request_reports_a_non_2xx_instead_of_raising(fake_opener):
    """向导要能告诉用户「上游回了 403」—— 那是有效信息，不是我们的故障。"""
    error = urllib.error.HTTPError(
        'http://93.184.216.34/t.png', 403, 'Forbidden',
        _headers({'Content-Type': 'text/plain'}), io.BytesIO(b'denied'))
    fake_opener(error)
    status, headers, body = guarded_request('http://93.184.216.34/t.png')
    assert status == 403
    assert headers['content-type'] == 'text/plain'
    assert body == b'denied'


def test_guarded_request_caps_the_response_body(fake_opener):
    """边读边掐，不是读完再判 —— 上游返回 10 GB 时判定语句永远来不及执行。"""
    fake_opener(FakeResponse(200, {}, b'x' * 5000))
    with pytest.raises(UrlNotAllowed):
        guarded_request('http://93.184.216.34/big', max_bytes=100)


def test_a_redirect_to_the_metadata_endpoint_is_refused(fake_opener):
    """**这是本模块存在的核心理由。** 首跳合法 + 302 到 169.254.169.254。"""
    redirect = urllib.error.HTTPError(
        'http://93.184.216.34/start', 302, 'Found',
        _headers({'Location': 'http://169.254.169.254/latest/meta-data/'}),
        io.BytesIO(b''))
    opener = fake_opener(redirect)
    with pytest.raises(UrlNotAllowed):
        guarded_request('http://93.184.216.34/start')
    assert opener.seen == ['http://93.184.216.34/start']   # 第二跳根本没发出去


def test_a_relative_redirect_is_resolved_and_re_checked(fake_opener):
    """相对 Location 完全合法，按当前 URL 解析后仍然要重新过闸。"""
    redirect = urllib.error.HTTPError(
        'http://93.184.216.34/a/b', 302, 'Found',
        _headers({'Location': '/c/d.png'}), io.BytesIO(b''))
    opener = fake_opener(redirect, FakeResponse(200, {}, b'final'))
    status, _headers_out, body = guarded_request('http://93.184.216.34/a/b')
    assert (status, body) == (200, b'final')
    assert opener.seen == ['http://93.184.216.34/a/b', 'http://93.184.216.34/c/d.png']


def test_a_redirect_loop_stops_at_the_hop_limit(fake_opener):
    """重定向环 / 拿跳数当绕过手段，都在 MAX_REDIRECTS 处止损。"""
    hops = [urllib.error.HTTPError(
        'http://93.184.216.34/loop', 302, 'Found',
        _headers({'Location': 'http://93.184.216.34/loop'}), io.BytesIO(b''))
        for _ in range(MAX_REDIRECTS + 1)]
    fake_opener(*hops)
    with pytest.raises(UrlNotAllowed, match=str(MAX_REDIRECTS)):
        guarded_request('http://93.184.216.34/loop')


def test_a_blocked_url_never_reaches_the_opener(fake_opener):
    """预检在**发请求之前**：闸门失效时这条会变成一次真的元数据访问。"""
    opener = fake_opener(FakeResponse(200, {}, b'should-never-be-read'))
    with pytest.raises(UrlNotAllowed):
        guarded_request('http://169.254.169.254/latest/')
    assert opener.seen == []
