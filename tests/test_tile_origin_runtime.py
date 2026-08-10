import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = ROOT / 'static' / 'js' / 'ui.js'


def _run_ui_case(tmp_path, body, *, protocol='http:', href='http://example.test:5000/'):
    if shutil.which('node') is None:
        pytest.skip('node is required for JavaScript runtime contract tests')
    script = tmp_path / 'tile-origin-case.js'
    script.write_text(
        "const assert = require('node:assert/strict');\n"
        "global.window = global;\n"
        f"global.location = {json.dumps({'protocol': protocol, 'href': href})};\n"
        "global.document = {};\n"
        "global.requestAnimationFrame = function () {};\n"
        "global.t = function (key) { return key; };\n"
        + UI_JS.read_text(encoding='utf-8')
        + "\n(async function () {\n"
        + body
        + "\n})().catch(function (error) { console.error(error); process.exit(1); });\n",
        encoding='utf-8',
    )
    # encoding 必须显式给：Windows 上 text=True 默认按 locale（cp1252）解码。
    # 这里的杀伤在失败路径：断言炸了以后，node 的 assert 消息连同上面内联进来
    # 的 ui.js（带中文）一起走 stderr，check=True 抛 CalledProcessError 的过程
    # 中解码就炸 —— 真正的断言消息被一个 UnicodeDecodeError 顶掉，恰恰在你最
    # 需要看清报错的时刻把报错吃了。
    return subprocess.run(['node', str(script)], cwd=ROOT, check=True,
                          text=True, encoding='utf-8', errors='replace',
                          capture_output=True)


def test_probe_success_selects_dedicated_origin_once(tmp_path):
    _run_ui_case(tmp_path, """
let calls = 0;
global.fetch = async function (url, opts) {
    calls += 1;
    assert.equal(url, 'http://example.test:5001/tile-health');
    assert.equal(opts.cache, 'no-store');
    assert.ok(opts.signal);
    return { ok: true };
};
assert.equal(await initTileOrigin(5001), true);
assert.equal(await initTileOrigin(5999), true);
assert.equal(calls, 1);
assert.equal(tileUrl('/tiles/7/0/0/0.png'),
             'http://example.test:5001/tiles/7/0/0/0.png');
""")


def test_probe_timeout_aborts_and_falls_back(tmp_path):
    _run_ui_case(tmp_path, """
let delay = null;
let timeoutCallback = null;
let signal = null;
let clearedTimer = null;
const timerId = { id: 'tile-health-timeout' };
global.setTimeout = function (fn, ms) {
    timeoutCallback = fn;
    delay = ms;
    return timerId;
};
global.clearTimeout = function (id) { clearedTimer = id; };
global.fetch = function (url, opts) {
    signal = opts.signal;
    return new Promise(function (resolve, reject) {
        opts.signal.addEventListener('abort', function () {
            reject(new Error('aborted'));
        }, { once: true });
    });
};
const probe = initTileOrigin(5001);
assert.equal(typeof timeoutCallback, 'function');
timeoutCallback();
assert.equal(await probe, false);
assert.equal(delay, 1000);
assert.equal(signal.aborted, true);
assert.strictEqual(clearedTimer, timerId);
assert.equal(tileUrl('/terrain/base/layer.json'), '/terrain/base/layer.json');
""")


def test_concurrent_first_calls_share_pending_promise(tmp_path):
    _run_ui_case(tmp_path, """
let calls = 0;
let finishFetch = null;
global.fetch = function () {
    calls += 1;
    return new Promise(function (resolve) { finishFetch = resolve; });
};
const first = initTileOrigin(5001);
const second = initTileOrigin(5999);
assert.strictEqual(second, first);
assert.equal(calls, 1);
finishFetch({ ok: true });
assert.equal(await first, true);
""")


def test_failed_probe_is_cached_without_retry(tmp_path):
    _run_ui_case(tmp_path, """
let calls = 0;
global.fetch = async function () {
    calls += 1;
    throw new Error('network down');
};
const first = initTileOrigin(5001);
assert.equal(await first, false);
const second = initTileOrigin(5999);
assert.strictEqual(second, first);
assert.equal(await second, false);
assert.equal(calls, 1);
""")


@pytest.mark.parametrize('response_body', [
    "return { ok: false };",
    "throw new Error('network down');",
])
def test_probe_failure_never_rejects(tmp_path, response_body):
    _run_ui_case(tmp_path, f"""
global.fetch = async function () {{ {response_body} }};
assert.equal(await initTileOrigin(5001), false);
assert.equal(tileUrl('/basemap/0/0/0'), '/basemap/0/0/0');
""")


def test_missing_port_skips_fetch(tmp_path):
    _run_ui_case(tmp_path, """
global.fetch = function () { throw new Error('fetch must not run'); };
assert.equal(await initTileOrigin(null), false);
assert.equal(tileUrl('/tiles/1'), '/tiles/1');
""")


def test_https_skips_plain_tile_origin_even_with_port(tmp_path):
    _run_ui_case(tmp_path, """
global.fetch = function () { throw new Error('fetch must not run'); };
assert.equal(await initTileOrigin(5001), false);
assert.equal(tileUrl('/tiles/1'), '/tiles/1');
""", protocol='https:', href='https://example.test/')


def test_tile_url_rewrites_only_whitelisted_tile_prefixes(tmp_path):
    """瓦片端口只放行四个前缀，其余一律 404 —— 前端就不能把「任意内部绝对
    路径」都改写过去。

    改写一个不在名单里的路径（新加的 /overlay/、误传进来的 /api/、/static/）
    换来的是一个**硬 404**：瓦片端口自己应答，主端口上那份能用的资源根本没被
    请求，而探测结果整页缓存，本次会话内不会回退。所以名单外的路径 fail-open
    —— 原样返回，走主端口，最坏只是慢，不是坏。
    """
    _run_ui_case(tmp_path, """
global.fetch = async function () { return { ok: true }; };
await initTileOrigin(5001);
assert.equal(tileUrl('/basemap/0/0/0'), 'http://example.test:5001/basemap/0/0/0');
assert.equal(tileUrl('/tiles/7/1/2/3.png'), 'http://example.test:5001/tiles/7/1/2/3.png');
assert.equal(tileUrl('/terrain/base/layer.json'),
             'http://example.test:5001/terrain/base/layer.json');
assert.equal(tileUrl('/contour/9/1/2.png'), 'http://example.test:5001/contour/9/1/2.png');
// 名单外的内部绝对路径：保持主端口，别换来一个硬 404
assert.equal(tileUrl('/api/basemap'), '/api/basemap');
assert.equal(tileUrl('/static/js/map.js'), '/static/js/map.js');
assert.equal(tileUrl('/'), '/');
// 撞名前缀（服务端同样只认带尾斜杠的那四条）
assert.equal(tileUrl('/basemapx/0/0/0'), '/basemapx/0/0/0');
assert.equal(tileUrl('/terrain'), '/terrain');
""")


def test_tile_url_only_rewrites_internal_absolute_paths(tmp_path):
    _run_ui_case(tmp_path, """
global.fetch = async function () { return { ok: true }; };
await initTileOrigin(5001);
assert.equal(tileUrl('https://cdn.example/x'), 'https://cdn.example/x');
assert.equal(tileUrl('http://cdn.example/x'), 'http://cdn.example/x');
assert.equal(tileUrl('//cdn.example/x'), '//cdn.example/x');
assert.equal(tileUrl('relative/x'), 'relative/x');
assert.equal(tileUrl('data:image/png;base64,x'), 'data:image/png;base64,x');
assert.equal(tileUrl(null), null);
""")
