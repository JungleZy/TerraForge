# Tile Origin Connection Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Cesium 瓦片请求移到经过客户端可达性验证的专用 HTTP origin，使 `5000` 上的 API 和 Socket.IO 不再被 HTTP/1.1 同源约 6 条连接的瓦片风暴饿死，并在专用端口不可用时保持同源可用。

**Architecture:** 复用当前未提交的 `src/core/tile_server.py` 骨架，在主 Flask app 上增加仅允许瓦片路径和 `/tile-health` 的第二个 threaded Werkzeug listener。前端在创建任何 Cesium Viewer 前用 1 秒 `AbortController` 探测一次专用 origin，并将结果保存为页面级状态；所有应用内瓦片路径只通过 `tileUrl(path)` 解析，HTTPS、探测失败和 WSGI import 自动保持同源。terrain 的旧 `localhost:5000` 父级引用在生成和读取两侧归一为 `/terrain/...`。

**Tech Stack:** Python 3.12、Flask 2.3、Flask-SocketIO 5.3、Werkzeug 3.1、原生 JavaScript、CesiumJS 1.143、pytest、Node.js runtime contract tests、Nuitka、GitHub Actions。

## Global Constraints

- 规格来源：`docs/superpowers/specs/2026-08-10-tile-origin-connection-isolation-design.md`。
- 保留工作区现有所有未提交修改；每次编辑前读取当前文件并做精确增量修改，不覆盖整份已有文件。
- 不执行 `git add`、`git commit`、`git reset`、`git checkout`、`git stash`、`git rebase` 或其他 Git mutation。
- 不增加第三方依赖，不迁移到 ASGI/HTTP/2，不引入 Caddy/Nginx，不实现 Service Worker 或瓦片批量协议。
- 主服务继续承载页面、REST API 和 Socket.IO；专用端口不得开放 `/api/`、`/socket.io/`、页面或静态前端资源。
- 客户端健康探测硬上限为 `1000 ms`，超时请求必须通过 `AbortController.abort()` 取消。
- 只有普通 `http:` 页面使用明文专用端口；`https:` 页面不尝试错误 TLS 或 mixed-content 请求，直接同源。
- `tileUrl(path)` 只改写以单个 `/` 开头的应用内绝对路径；`https://...`、`http://...`、`//cdn/...`、`data:...` 和相对路径原样返回。
- Socket.IO 始终通过无参数 `io()` 连接页面 origin；`tasks.js` 的 API 不经过瓦片 URL helper。
- 新默认 `terrain_base_parent_url` 为 `/terrain/base`；只兼容改写旧应用内 `http://localhost:5000/terrain/...`，部署者配置的其他完整 HTTP(S) terrain URL必须保留。
- 定向测试和完整 `pytest -q` 均属于完成条件；环境阻断必须保留原始命令、输出和未验证范围。

## File Responsibility Map

- `src/core/tile_server.py`：专用 WSGI gate、无状态健康响应、CORS、后台 listener。
- `src/core/server_runner.py`：以实际主端口 `port + 1` 编排专用 listener，并记录成功/失败状态。
- `static/js/ui.js`：页面级专用 origin 状态、1 秒探测、唯一 `tileUrl(path)` 接口。
- `templates/index.html`：首页 splash、任务 API/Socket.IO、健康探测和 Viewer 的启动时序。
- `static/js/history.js`：历史 API 与地图探测并行，并在 Viewer 和数据都就绪后补最终地图渲染。
- `static/js/map.js`：所有底图、任务瓦片、contour、terrain、hillshade URL 的统一消费点。
- `src/services/terrain_tiling/layer_json.py`：新任务和旧配置值的 parent URL 规范化。
- `src/routes/terrain_static.py`：服务存量 `layer.json` 时对旧 `localhost:5000` parent URL 做响应期兼容，不改磁盘文件。
- `src/core/database.py`、`src/services/dem_task_manager.py`、`src/services/local_terrain_task_manager.py`：统一相对 parent URL 默认值。
- `tests/test_tile_server.py`：专用 listener、gate、描述符和前端接线契约。
- `tests/test_tile_origin_runtime.py`：用 Node 执行 `ui.js`，验证探测、取消、单例和 URL 改写的真实行为。
- `tests/test_runtime_mode.py`：主/瓦片端口编排、失败降级和占位进程行为。
- `tests/test_fix_frontend_hardening.py`、`tests/test_map_js_contract.py`、`tests/test_socket_singleton_contract.py`：首页/历史时序、瓦片消费者和 Socket.IO 隔离契约。
- `tests/test_layer_json.py`、`tests/test_terrain_static.py`、现有 parent URL 相关测试：新默认、旧值兼容、外部 URL 保留。
- `tests/test_basemap_tile_cache.py`：主 origin 和专用 origin 共享同一磁盘缓存。
- `.github/workflows/build.yml`、`.github/workflows/test-build.yml`：打包产物同时验证 `5000` 首页和 `5001` 健康端点。
- `docs/guides/DISTRIBUTION.md`、`docs/reference/terrain/*.md`、`CLAUDE.md`：同步端口降级和相对 parent URL 的实际行为。

---

### Task 1: Complete the dedicated tile listener and lifecycle orchestration

**Files:**
- Modify: `src/core/tile_server.py:39-99`
- Modify: `src/core/server_runner.py:68-102`
- Modify: `src/routes/main.py:109-153`
- Modify: `tests/test_tile_server.py:49-172`
- Modify: `tests/test_runtime_mode.py:120-127`
- Modify: `tests/test_fix_infra_e.py:424-501`（子进程启动桩必须替换 `start_tile_server`，禁止测试真实占用固定 `5001`）

**Interfaces:**
- Consumes: existing `wrap_tile_app(app)`, `start_tile_server(app, host, port)`, `record_tile_port(app, port)`, `current_tile_port()`.
- Produces: `TILE_HEALTH_PATH = '/tile-health'`; exact `204 No Content` health response with CORS and `Cache-Control: no-store`; `run_server()` starts tile listener on `port + 1` and always continues to `socketio.run()`.

- [ ] **Step 1: Extend the WSGI gate tests with exact health, CORS, cache and isolation assertions**

Add to `TestTileGate` and `TestStartTileServer` in `tests/test_tile_server.py`:

```python
def test_health_is_served_without_calling_wrapped_app(self):
    captured, body, calls = self._call('/tile-health')
    assert calls == []
    assert captured['status'].startswith('204')
    assert body == b''
    assert captured['headers']['Access-Control-Allow-Origin'] == '*'
    assert captured['headers']['Cache-Control'] == 'no-store'

@pytest.mark.parametrize('path', ['/tile-health/', '/tile-healthx',
                                  '/socket.io/', '/api/basemap'])
def test_non_tile_and_health_prefix_collisions_are_rejected(self, path):
    captured, _, calls = self._call(path)
    assert calls == []
    assert captured['status'].startswith('404')


def test_tile_gate_preserves_wrapped_cache_control(self):
    def cached_app(environ, start_response):
        start_response('200 OK', [('Cache-Control', 'public, max-age=86400')])
        return [b'ok']
    from src.core.tile_server import wrap_tile_app
    captured = {}
    body = b''.join(wrap_tile_app(cached_app)(
        {'PATH_INFO': '/basemap/0/0/0'},
        lambda status, headers, exc_info=None: captured.update(
            status=status, headers=dict(headers))))
    assert body == b'ok'
    assert captured['headers']['Cache-Control'] == 'public, max-age=86400'
    assert captured['headers']['Access-Control-Allow-Origin'] == '*'


def test_health_endpoint_is_reachable_with_cors_and_no_store(self):
    from src.core.tile_server import start_tile_server
    srv = start_tile_server(_sentinel_app([]), host='127.0.0.1', port=0)
    assert srv is not None
    try:
        status, headers, body = _get(
            f'http://127.0.0.1:{srv.server_port}/tile-health')
        assert status == 204
        assert body == b''
        assert headers['Access-Control-Allow-Origin'] == '*'
        assert headers['Cache-Control'] == 'no-store'
    finally:
        srv.shutdown()
        srv.server_close()
```

Also add `srv.server_close()` to the existing real-server test teardown.

- [ ] **Step 2: Run the listener tests and confirm the new health tests fail**

Run:

```bash
pytest -q tests/test_tile_server.py::TestTileGate tests/test_tile_server.py::TestStartTileServer
```

Expected before implementation: `/tile-health` returns `404`, so the health tests fail while existing tile-prefix tests remain green.

- [ ] **Step 3: Implement the exact lightweight health response in `wrap_tile_app()`**

Add the constant and exact-path branch before the prefix gate:

```python
TILE_HEALTH_PATH = '/tile-health'


def wrap_tile_app(app):
    """把任意 WSGI app 包成「健康检查 + 只放行瓦片前缀 + CORS」的 app。"""
    def tile_app(environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == TILE_HEALTH_PATH:
            start_response('204 No Content', [
                ('Access-Control-Allow-Origin', '*'),
                ('Cache-Control', 'no-store'),
                ('Content-Length', '0'),
            ])
            return [b'']
        if not path.startswith(TILE_PATH_PREFIXES):
            start_response('404 Not Found', [
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Access-Control-Allow-Origin', '*'),
            ])
            return [b'not found']
        ...
```

Keep the existing allowed prefixes, threaded server, `OSError/SystemExit` degradation and extension key unchanged.

- [ ] **Step 4: Add `run_server()` orchestration tests using fakes instead of real fixed ports**

Append to `tests/test_runtime_mode.py`:

```python
class _FakeSocketIO:
    def __init__(self):
        self.calls = []

    def run(self, app, **kwargs):
        self.calls.append((app, kwargs))


def _quiet_runner(monkeypatch):
    from src.core import server_runner
    monkeypatch.setattr(server_runner, '_silence_duplicate_startup_lines', lambda: None)
    monkeypatch.setattr(server_runner, '_install_reloader_watchdog', lambda: None)
    return server_runner


def test_run_server_uses_main_port_plus_one_and_records_actual_port(monkeypatch):
    from flask import Flask
    from src.core import tile_server
    runner = _quiet_runner(monkeypatch)
    app = Flask(__name__)
    socketio = _FakeSocketIO()
    calls = []
    fake_server = type('Server', (), {'server_port': 7001})()
    monkeypatch.setattr(tile_server, 'start_tile_server',
                        lambda app_arg, host, port: calls.append((app_arg, host, port)) or fake_server)

    runner.run_server(app, socketio, debug=False, show_startup_output=False,
                      host='127.0.0.1', port=7000)

    assert calls == [(app, '127.0.0.1', 7001)]
    assert app.extensions['tile_server_port'] == 7001
    assert socketio.calls[0][1]['port'] == 7000


def test_run_server_tile_failure_records_none_and_still_runs_main(monkeypatch):
    from flask import Flask
    from src.core import tile_server
    runner = _quiet_runner(monkeypatch)
    app = Flask(__name__)
    socketio = _FakeSocketIO()
    monkeypatch.setattr(tile_server, 'start_tile_server', lambda *a, **k: None)

    runner.run_server(app, socketio, debug=False, show_startup_output=False)

    assert app.extensions['tile_server_port'] is None
    assert len(socketio.calls) == 1


def test_reloader_placeholder_does_not_start_tile_listener(monkeypatch):
    from src.core import tile_server
    runner = _quiet_runner(monkeypatch)
    socketio = _FakeSocketIO()
    monkeypatch.setattr(tile_server, 'start_tile_server',
                        lambda *a, **k: pytest.fail('placeholder must not start tile listener'))

    runner.run_server(None, socketio, debug=True, show_startup_output=False)

    assert len(socketio.calls) == 1
```

Add this WSGI-import degradation assertion to `TestTilePortDescriptor` in `tests/test_tile_server.py`:

```python
def test_wsgi_import_descriptor_has_no_tile_port(self, isolated_app):
    response = isolated_app.app.test_client().get('/api/basemap')
    payload = response.get_json()
    assert response.status_code == 200
    assert payload['basemap']['tile_port'] is None
```

In the `tests/test_fix_infra_e.py:424-501` subprocess source, assign a fake `src.core.tile_server.start_tile_server` before executing `app.py`; the fake returns `None`. This keeps the runtime-role tests from binding the developer's real `5001` while still exercising the main `socketio.run()` path.

- [ ] **Step 5: Run the lifecycle tests and confirm the custom-port test fails**

Run:

```bash
pytest -q tests/test_runtime_mode.py tests/test_fix_infra_e.py
```

Expected before the fix: custom main port `7000` still calls the tile server with its default `5001`, failing the `7001` assertion.

- [ ] **Step 6: Pass the actual tile port and keep the main server unconditional**

Change only the call in `src/core/server_runner.py`:

```python
server = start_tile_server(app, host=host, port=port + 1)
record_tile_port(app, server.server_port if server is not None else None)
```

Do not place `socketio.run()` inside the success branch. In `src/routes/main.py`, make the exception fallback descriptor pass `tile_port=current_tile_port()` just like the normal descriptor path.

- [ ] **Step 7: Verify Task 1**

Run:

```bash
pytest -q tests/test_tile_server.py tests/test_runtime_mode.py tests/test_fix_infra_e.py
```

Expected: all selected tests pass; no test leaves a listening socket behind.

- [ ] **Step 8: Review checkpoint without committing**

Inspect `git diff -- src/core/tile_server.py src/core/server_runner.py src/routes/main.py tests/test_tile_server.py tests/test_runtime_mode.py tests/test_fix_infra_e.py` and confirm no unrelated startup behavior or existing worktree changes were removed. Do not stage or commit.

---

### Task 2: Add a page-scoped, abortable tile-origin resolver

**Files:**
- Create: `tests/test_tile_origin_runtime.py`
- Modify: `static/js/ui.js:1-11,302-320`
- Modify: `tests/test_tile_server.py:147-172`
- Modify: `tests/test_tif_info_frontend.py:251-266`

**Interfaces:**
- Consumes: browser globals `location.href`, `location.protocol`, `fetch`, `AbortController`, `setTimeout`, `clearTimeout`.
- Produces: `window.initTileOrigin(tilePort) -> Promise<boolean>` and `window.tileUrl(path) -> string`; each page probes at most once and never rejects.

- [ ] **Step 1: Create a Node-backed runtime harness that executes the real `ui.js`**

Create `tests/test_tile_origin_runtime.py`:

```python
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
    return subprocess.run(['node', str(script)], cwd=ROOT, check=True,
                          text=True, capture_output=True)
```

- [ ] **Step 2: Add runtime tests for success, singleton, timeout cancellation and URL safety**

Add these cases to the same file:

```python
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
let signal = null;
global.setTimeout = function (fn, ms) { delay = ms; fn(); return 1; };
global.clearTimeout = function () {};
global.fetch = function (url, opts) {
    signal = opts.signal;
    return Promise.reject(new Error('aborted'));
};
assert.equal(await initTileOrigin(5001), false);
assert.equal(delay, 1000);
assert.equal(signal.aborted, true);
assert.equal(tileUrl('/terrain/base/layer.json'), '/terrain/base/layer.json');
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
```

- [ ] **Step 3: Run the new runtime tests and confirm the missing API failure**

Run:

```bash
pytest -q tests/test_tile_origin_runtime.py
```

Expected before implementation: `initTileOrigin is not defined`, and the old two-argument `tileUrl` contract cannot satisfy the session-state assertions.

- [ ] **Step 4: Replace the stateless helper with the exact page-scoped implementation**

Replace the tile-origin block in `static/js/ui.js` with:

```javascript
const TILE_HEALTH_TIMEOUT_MS = 1000;
let tileOrigin = '';
let tileOriginReady = null;

function initTileOrigin(tilePort) {
    if (tileOriginReady) return tileOriginReady;
    tileOriginReady = (async function () {
        if (!tilePort || location.protocol !== 'http:') return false;
        let timer = null;
        try {
            const health = new URL(location.href);
            health.port = String(tilePort);
            health.pathname = '/tile-health';
            health.search = '';
            health.hash = '';
            const controller = new AbortController();
            timer = setTimeout(function () {
                controller.abort();
            }, TILE_HEALTH_TIMEOUT_MS);
            const response = await fetch(health.href, {
                signal: controller.signal,
                cache: 'no-store',
            });
            if (!response.ok) return false;
            tileOrigin = health.origin;
            return true;
        } catch (error) {
            return false;
        } finally {
            if (timer !== null) clearTimeout(timer);
        }
    })();
    return tileOriginReady;
}

function tileUrl(path) {
    if (typeof path !== 'string' || !/^\/(?!\/)/.test(path)) return path;
    return tileOrigin ? tileOrigin + path : path;
}
```

Expose both functions:

```javascript
window.initTileOrigin = initTileOrigin;
window.tileUrl = tileUrl;
```

Also update the file-level public-interface comment. Do not log probe failures: fallback is expected in HTTPS, WSGI and blocked-port environments.

- [ ] **Step 5: Update old textual contracts to the new one-argument resolver**

In `tests/test_tile_server.py`, replace `location.hostname` and two-argument assertions with exact assertions for both exports and `tileUrl(bm.url)`. In `tests/test_tif_info_frontend.py`, change the old `tileUrl(bm.url, bm.tile_port)` expectation to `tileUrl(bm.url)`.

- [ ] **Step 6: Verify Task 2**

Run:

```bash
pytest -q tests/test_tile_origin_runtime.py tests/test_tile_server.py tests/test_tif_info_frontend.py
node --check static/js/ui.js
```

Expected: all tests pass and Node reports no syntax errors.

- [ ] **Step 7: Review checkpoint without committing**

Inspect the diff for `static/js/ui.js` and the new runtime tests. Confirm the old `tileUrl(path, tilePort)` implementation is gone, probe failures resolve `false`, and external URLs are untouched. Do not stage or commit.

---

### Task 3: Gate Cesium Viewer creation while starting APIs immediately

**Files:**
- Modify: `templates/index.html:481-518`
- Modify: `static/js/history.js:7-55,72-129,159-185`
- Modify: `tests/test_fix_frontend_hardening.py:62-96`
- Modify: `tests/test_tile_server.py:147-172`
- Verify only: `tests/test_splash_ready_signal.py`（现有 `initMap()` 内 tilesLoaded/splashReady 契约不得改动）

**Interfaces:**
- Consumes: `initTileOrigin(tilePort) -> Promise<boolean>`, `tileUrl(path)`, existing `initTasks()`, `initMap(config, basemap)`, `initHistoryMap()`, `loadHistory(page, renderMap)`.
- Produces: homepage startup where task API/Socket.IO begins while probe is pending; history startup where stats, timeline and map/probe run concurrently and final map rendering happens once after both map and timeline settle.

- [ ] **Step 1: Replace the obsolete history sequencing tests with parallel-start contracts**

In `tests/test_fix_frontend_hardening.py`, replace the tests requiring `await initHistoryMap()` before `loadHistory(1)` with:

```python
def test_init_history_starts_map_and_timeline_before_joint_barrier():
    body = _js_function_body(_clean('history.js'), 'initHistory')
    map_at = body.index('initHistoryMap()')
    history_at = body.index('loadHistory(1, false)')
    barrier_at = body.index('Promise.all')
    assert map_at < barrier_at
    assert history_at < barrier_at
    assert 'renderHistoryMap()' in body[barrier_at:]


def test_init_history_map_awaits_tile_origin_before_viewer():
    body = _js_function_body(_clean('history.js'), 'initHistoryMap')
    descriptor_at = body.index('await _resolveHistoryBasemap()')
    probe_at = body.index('await initTileOrigin(bm.tile_port)')
    viewer_at = body.index("new Cesium.Viewer('historyMap'")
    assert descriptor_at < probe_at < viewer_at


def test_map_failure_does_not_block_initial_timeline_load():
    body = _js_function_body(_clean('history.js'), 'initHistory')
    assert '.catch' in body
    assert 'loadHistory(1, false)' in body
    assert 'Promise.all' in body
```

- [ ] **Step 2: Add homepage source-order contracts**

In `tests/test_tile_server.py`, add `from pathlib import Path`, then read `templates/index.html` and assert:

```python
def test_index_starts_tasks_while_tile_probe_is_pending():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    probe_at = html.index('initTileOrigin(basemapDescriptor.tile_port)')
    tasks_at = html.index("_boot('tasks', initTasks)")
    map_at = html.index("_boot('map'")
    assert probe_at < tasks_at < map_at
    assert 'tileOriginReady.then(function ()' in html


def test_index_keeps_double_animation_frame_before_viewer():
    html = Path('templates/index.html').read_text(encoding='utf-8')
    outer = html.index('requestAnimationFrame(function ()')
    inner = html.index('requestAnimationFrame(function ()', outer + 1)
    map_at = html.index("_boot('map'", inner)
    assert outer < inner < map_at
```

- [ ] **Step 3: Run the startup contract tests and confirm they fail against the old order**

Run:

```bash
pytest -q tests/test_fix_frontend_hardening.py tests/test_tile_server.py tests/test_splash_ready_signal.py
```

Expected before implementation: no `initTileOrigin`, homepage creates Viewer before tasks, and history waits for map before starting timeline.

- [ ] **Step 4: Reorder homepage startup without moving Viewer-dependent work ahead of `initMap()`**

Change the inline script to create one descriptor and one probe promise:

```javascript
const config = {{ map_config|tojson }};
const basemapDescriptor = {{ basemap|tojson }};

...

document.addEventListener('DOMContentLoaded', function () {
    _boot('splash', initSplash);
    const tileOriginReady = initTileOrigin(basemapDescriptor.tile_port);
    _boot('tasks', initTasks);
    requestAnimationFrame(function () {
        requestAnimationFrame(function () {
            tileOriginReady.then(function () {
                _boot('map', function () { initMap(config, basemapDescriptor); });
                _boot('workbench', initMapWorkbench);
                _boot('downloadTypeToggle', initDownloadTypeToggle);
                _boot('processTypeToggle', initProcessTypeToggle);
                _boot('mapStylePreview', initMapStylePreview);
                _boot('contourPreview', initContourPreview);
            });
        });
    });
});
```

Keep `initMapWorkbench` and all Viewer-dependent initializers after `initMap`. `initTasks` remains outside the promise so Socket.IO and four API calls start immediately.

- [ ] **Step 5: Make history APIs and map initialization concurrent**

Change `initHistory()` to:

```javascript
loadStats();
const mapReady = initHistoryMap().catch(function (error) {
    console.error('Failed to init history map:', error);
});
const historyReady = loadHistory(1, false);
await Promise.all([mapReady, historyReady]);
renderHistoryMap();
```

Change `initHistoryMap()` immediately after descriptor resolution:

```javascript
const bm = await _resolveHistoryBasemap();
await initTileOrigin(bm.tile_port);
historyViewer = new Cesium.Viewer('historyMap', {
    baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
        url: tileUrl(bm.url),
        ...
    })),
    ...
});
```

Change the load signature and render guard:

```javascript
async function loadHistory(page = 1, renderMap = true) {
    ...
    renderPagination(p.page || 1, p.total_pages || 1);
    if (renderMap) renderHistoryMap();
    ...
}
```

All existing calls such as chip filtering and pagination keep the default `renderMap=true`; only the initial load passes `false` to avoid duplicate rendering.

- [ ] **Step 6: Update comments that still describe serial map-before-history behavior**

Rewrite the comments at `static/js/history.js:13-54` and `templates/index.html:492-503` to describe the new barrier and immediate API start. Remove claims that `loadHistory` must be after `await initHistoryMap()`.

- [ ] **Step 7: Verify Task 3**

Run:

```bash
pytest -q tests/test_fix_frontend_hardening.py tests/test_tile_server.py tests/test_splash_ready_signal.py tests/test_tasks_js_contract.py
node --check static/js/history.js
```

Expected: all selected tests pass; history failures remain isolated from table loading.

- [ ] **Step 8: Review checkpoint without committing**

Confirm from the diff that `initTasks()` is not inside the probe continuation, both animation frames remain, and history initial rendering has exactly one explicit post-barrier `renderHistoryMap()` plus guarded normal reload rendering. Do not stage or commit.

---

### Task 4: Route every application tile consumer through the session resolver

**Files:**
- Modify: `static/js/map.js:212-224,263-320,2082-2165`
- Modify: `static/js/history.js:94-106`
- Modify: `tests/test_map_js_contract.py`
- Modify: `tests/test_socket_singleton_contract.py:265-408`
- Modify: `tests/test_tile_server.py:147-172`
- Verify only: `static/js/socket.js`, `static/js/tasks.js`（本任务不得修改这两个文件）

**Interfaces:**
- Consumes: `tileUrl(path) -> string` after Task 2 has resolved page state.
- Produces: one URL resolver for initial/runtime basemap, map task, contour, local terrain, DEM terrain, hillshade metadata and hillshade PNG; Socket.IO/API remain on main origin.

- [ ] **Step 1: Add function-scoped map contracts instead of occurrence counts**

Use the existing `_map_js()`, `_strip_comments(src)` and `_fn_body(src, name)` helpers in `tests/test_map_js_contract.py`:

```python
def test_initial_and_runtime_basemaps_use_session_tile_url():
    src = _strip_comments(_map_js())
    init_body = _fn_body(src, 'initMap')
    rebuild_body = _fn_body(src, '_rebuildBaseImagery')
    assert 'url: tileUrl(bm.url)' in init_body
    assert 'url: tileUrl(bm.url)' in rebuild_body
    assert 'bm.tile_port' not in init_body
    assert 'bm.tile_port' not in rebuild_body


def test_preview_routes_every_application_tile_path_through_tile_url():
    body = _fn_body(_strip_comments(_map_js()), 'previewTask')
    for anchor in (
        'tileUrl(`/tiles/${task.id}`)',
        'tileUrl(`/contour/${task.id}`)',
        'tileUrl(`/terrain/local/${task.id}`)',
        'tileUrl(`/terrain/dem/${task.id}`)',
        'fetch(`${base}/layer.json`)',
        'Cesium.CesiumTerrainProvider.fromUrl(base',
        'fetch(`${base}/hillshade`)',
        'url: tileUrl(hs.url)',
    ):
        assert anchor in body
```

- [ ] **Step 2: Add Socket.IO and API separation contracts**

Append to `tests/test_socket_singleton_contract.py`:

```python
def test_socket_io_stays_on_page_origin_and_ignores_tile_helpers():
    socket_code = _strip_js_comments(_read('static', 'js', 'socket.js'))
    assert re.search(r'instance\s*=\s*io\s*\(\s*\)', socket_code)
    assert 'tileUrl' not in socket_code
    assert 'initTileOrigin' not in socket_code
    assert 'tile_port' not in socket_code


def test_task_api_requests_never_use_tile_origin():
    tasks_code = _strip_js_comments(_read('static', 'js', 'tasks.js'))
    assert 'tileUrl(' not in tasks_code
    assert 'initTileOrigin(' not in tasks_code
```

- [ ] **Step 3: Run the contracts and confirm hillshade and old helper signatures fail**

Run:

```bash
pytest -q tests/test_map_js_contract.py tests/test_socket_singleton_contract.py tests/test_tile_server.py
```

Expected before implementation: initial/runtime basemaps still pass `bm.tile_port`, preview uses `_tileUrl`, and hillshade PNG uses raw `hs.url`.

- [ ] **Step 4: Remove the secondary `_tileUrl()` wrapper and update exact consumers**

Delete `static/js/map.js:219-224`. Make these replacements:

```javascript
url: tileUrl(bm.url)
```

in `_rebuildBaseImagery()` and `initMap()`; and:

```javascript
const base = taskType === 'map'
    ? tileUrl(`/tiles/${task.id}`)
    : tileUrl(`/contour/${task.id}`);

const base = taskType === 'local_terrain'
    ? tileUrl(`/terrain/local/${task.id}`)
    : tileUrl(`/terrain/dem/${task.id}`);
```

Keep metadata/provider requests based on `base`. Change the final hillshade provider to:

```javascript
new Cesium.SingleTileImageryProvider({
    url: tileUrl(hs.url),
    rectangle: Cesium.Rectangle.fromDegrees(
        hs.bounds[0], hs.bounds[1], hs.bounds[2], hs.bounds[3]),
})
```

In `history.js`, use `tileUrl(bm.url)` after its probe await. Do not pass `tile_port` to any `tileUrl` call.

- [ ] **Step 5: Update comments and textual tests to the session-state contract**

Comments must say that `initTileOrigin()` chose the page origin once; they must no longer claim each URL call reads `bm.tile_port`. Replace `tests/test_tile_server.py`'s old count-based assertions with exact path anchors and the one-argument helper signature.

- [ ] **Step 6: Verify Task 4**

Run:

```bash
pytest -q \
  tests/test_map_js_contract.py \
  tests/test_socket_singleton_contract.py \
  tests/test_tile_server.py \
  tests/test_terrain_lighting_frontend.py \
  tests/test_fix_terrain_preview_transition.py \
  tests/test_tif_info_frontend.py
node --check static/js/map.js
node --check static/js/history.js
node --check static/js/socket.js
node --check static/js/tasks.js
```

Expected: selected tests and syntax checks pass; no `tileUrl(..., tilePort)` or `_tileUrl(...)` call remains.

- [ ] **Step 7: Review checkpoint without committing**

Search with the dedicated Grep tool for `tileUrl\([^\n]*,` and `_tileUrl\(` under `static/js`; expected no production matches. Confirm `socket.js` still contains exactly one no-argument `io()` creation. Do not stage or commit.

---

### Task 5: Normalize terrain parent URLs for new, configured and already-generated layers

**Files:**
- Modify: `src/services/terrain_tiling/layer_json.py:1-35`
- Modify: `src/routes/terrain_static.py:7-55,205-235`
- Modify: `src/core/database.py:100-105`
- Modify: `src/services/dem_task_manager.py:290-305`
- Modify: `src/services/local_terrain_task_manager.py:43-62`
- Modify: `tests/test_layer_json.py:51-133`
- Modify: `tests/test_terrain_static.py`
- Modify as required by existing assertions: `tests/test_fix_dem_parent_url_config.py`, `tests/test_local_terrain_api.py`, `tests/test_fix_config_path_validation.py`, `tests/test_docs_claims.py`
- Modify: `docs/reference/terrain/README.md`
- Modify: `docs/reference/terrain/cesiumjs-loading.md`
- Modify: `docs/reference/terrain/global-base-build.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `normalize_parent_url(parent_url) -> str | None`, existing terrain static file routes.
- Produces: old `http://localhost:5000/terrain/...` becomes `/terrain/...`; external HTTP(S) URLs remain complete; new defaults are `/terrain/base`; stored legacy `layer.json` is rewritten only in the HTTP response, not on disk.

- [ ] **Step 1: Change parent URL tests to the new exact compatibility rules**

Update the parameter table in `tests/test_layer_json.py` to include:

```python
@pytest.mark.parametrize('given,expected', [
    ('http://localhost:5000/terrain/base/layer.json', '/terrain/base'),
    ('http://localhost:5000/terrain/base/', '/terrain/base'),
    ('http://localhost:5000/terrain/dem/7/layer.json', '/terrain/dem/7'),
    ('https://x.example:8443/terrain/base/layer.json',
     'https://x.example:8443/terrain/base'),
    ('http://localhost:5001/terrain/base',
     'http://localhost:5001/terrain/base'),
    ('/terrain/base/layer.json', '/terrain/base'),
    ('', None),
    (None, None),
])
```

Rewrite `test_default_parent_url_is_a_directory_everywhere_it_is_written()` so it expects exactly `/terrain/base` in `database.py`, `dem_task_manager.py`, and `local_terrain_task_manager.py`, rather than matching only `https?://` URLs.

- [ ] **Step 2: Add a response-time compatibility test for a stored legacy `layer.json`**

In `tests/test_terrain_static.py`, use the existing isolated task fixture and write:

```json
{
  "tilejson": "2.1.0",
  "format": "quantized-mesh-1.0",
  "parentUrl": "http://localhost:5000/terrain/base"
}
```

Then assert the task's `/terrain/.../layer.json` response contains `"parentUrl": "/terrain/base"`, while rereading the file from disk still shows the original `http://localhost:5000/...`. Add a second case whose stored parent is `https://terrain.example.com/base` and assert it is unchanged.

- [ ] **Step 3: Run parent URL tests and confirm old absolute defaults fail the new assertions**

Run:

```bash
pytest -q \
  tests/test_layer_json.py \
  tests/test_terrain_static.py \
  tests/test_fix_dem_parent_url_config.py \
  tests/test_local_terrain_api.py \
  tests/test_fix_config_path_validation.py
```

Expected before implementation: old localhost values remain absolute, stored layer responses are unchanged, and default-value assertions fail.

- [ ] **Step 4: Implement narrow legacy-origin normalization**

Add `urlsplit` and update `normalize_parent_url()`:

```python
from urllib.parse import urlsplit


def normalize_parent_url(parent_url: str | None) -> str | None:
    if not parent_url:
        return None
    url = parent_url.strip().rstrip('/')
    if url.lower().endswith('/layer.json'):
        url = url[:-len('/layer.json')].rstrip('/')
    if not url:
        return None

    parsed = urlsplit(url)
    if (parsed.scheme.lower() == 'http'
            and parsed.hostname == 'localhost'
            and parsed.port == 5000
            and parsed.path.startswith('/terrain/')
            and not parsed.query
            and not parsed.fragment):
        return parsed.path.rstrip('/')
    return url
```

This intentionally does not rewrite `localhost:5001`, external domains, HTTPS or URLs with query/fragment semantics.

- [ ] **Step 5: Change all three defaults to an application-relative path**

Use this exact value and update adjacent comments/docstrings:

```python
'/terrain/base'
```

Change it in:

```text
src/core/database.py DEFAULT_CONFIGS
src/services/dem_task_manager.py fallback
src/services/local_terrain_task_manager.py fallback
```

Do not add a database migration: existing config/task rows are handled by `normalize_parent_url()` when new output is generated, and existing files are handled by the next response-time step.

- [ ] **Step 6: Rewrite only legacy parent URLs while serving `layer.json`**

In `src/routes/terrain_static.py`, import `json` and `normalize_parent_url`, and add:

```python
def _send_layer_json(target: Path):
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return send_file(target)

    original = data.get('parentUrl')
    normalized = normalize_parent_url(original)
    if normalized == original:
        return send_file(target)
    if normalized is None:
        data.pop('parentUrl', None)
    else:
        data['parentUrl'] = normalized
    return jsonify(data)
```

Call it at the top of `_send_terrain_file()` after the target has been resolved and before opening the file:

```python
if target.name == 'layer.json':
    return _send_layer_json(target)
```

The helper must not write back to disk and must preserve current behavior for malformed/non-JSON files by serving the original response.

- [ ] **Step 7: Update old assertions and documentation claims**

Update tests that hard-code the old default to expect `/terrain/base`. In docs and `CLAUDE.md`, replace claims that users must change the default when the service is not on `localhost:5000`; explain that the default now inherits whichever tile origin served the task `layer.json`, while a configured external terrain service remains supported. Preserve the existing warning that parent URL must be a directory and base terrain must exist.

- [ ] **Step 8: Verify Task 5**

Run:

```bash
pytest -q \
  tests/test_layer_json.py \
  tests/test_terrain_static.py \
  tests/test_local_terrain_static.py \
  tests/test_fix_dem_parent_url_config.py \
  tests/test_local_terrain_api.py \
  tests/test_fix_config_path_validation.py \
  tests/test_docs_claims.py
```

Expected: old application-internal URLs are relative in generated/served JSON, external URLs remain unchanged, disk fixtures are not mutated, and documentation contracts pass.

- [ ] **Step 9: Review checkpoint without committing**

Inspect all parent URL diffs. Search for `http://localhost:5000/terrain/base` under active source/docs/tests; remaining matches are allowed only where a legacy input is intentionally tested or historical archive/review text is preserved. Do not stage or commit.

---

### Task 6: Prove cache sharing and package-level health

**Files:**
- Modify: `tests/test_basemap_tile_cache.py:104-119`
- Modify: `.github/workflows/build.yml:210-232`
- Modify: `.github/workflows/test-build.yml:143-161`
- Modify: `tests/test_fix_build_ci_hardening.py`
- Modify: `docs/guides/DISTRIBUTION.md:62-84`

**Interfaces:**
- Consumes: `start_tile_server(app, host='127.0.0.1', port=0)`, current `cache/basemap` implementation, `/tile-health`.
- Produces: evidence that both origins share one server-side cache; build smoke requires main page `200` and tile health `204`; distribution docs describe client-side blocked-port and HTTPS fallback accurately.

- [ ] **Step 1: Add a cross-origin cache integration test**

Append to `tests/test_basemap_tile_cache.py`:

```python
def test_main_and_tile_origins_share_disk_cache(app_ctx, monkeypatch):
    import urllib.request
    from src.core.tile_server import start_tile_server

    client, route_mod = app_ctx
    up = _FakeUpstream()
    monkeypatch.setattr(route_mod.urllib.request, 'build_opener',
                        lambda *a, **k: up)
    monkeypatch.setattr(route_mod, 'resolve_from_config', lambda cm, wait_s=None: '')

    from src.services.basemap_source import BASEMAP_PRESETS, source_version
    version = source_version(BASEMAP_PRESETS['esri']['url'])
    path = f'/basemap/3/6/2?v={version}'
    first = client.get(path)
    assert first.status_code == 200

    server = start_tile_server(client.application, host='127.0.0.1', port=0)
    assert server is not None
    try:
        with urllib.request.urlopen(
                f'http://127.0.0.1:{server.server_port}{path}',
                timeout=5) as response:
            assert response.status == 200
            assert response.headers['Access-Control-Allow-Origin'] == '*'
            assert response.headers['Cache-Control'] == 'public, max-age=86400'
            assert response.read() == _PNG
    finally:
        server.shutdown()
        server.server_close()

    assert _tile_count(up) == 1
```

- [ ] **Step 2: Run cache tests as a characterization check**

Run:

```bash
pytest -q tests/test_basemap_proxy_route.py tests/test_basemap_tile_cache.py
```

Expected on the current one-app skeleton: the new cross-origin test passes. A failure means `start_tile_server()` is not serving `client.application` or the route is using a cache root other than `Config.CACHE_DIR / 'basemap'`; correct that exact divergence without changing cache policy.

- [ ] **Step 3: Add CI contract tests for both smoke endpoints**

In `tests/test_fix_build_ci_hardening.py`, read both workflow files and assert each smoke block contains:

```text
http://127.0.0.1:5000/
http://127.0.0.1:5001/tile-health
```

and checks status `200` for main plus `204` for health before killing the process.

- [ ] **Step 4: Run the workflow contract and confirm the missing health probe fails**

Run:

```bash
pytest -q tests/test_fix_build_ci_hardening.py
```

Expected before implementation: both workflows contain only the `5000` probe.

- [ ] **Step 5: Extend both smoke scripts with a separate health status**

Use two variables in each workflow:

```bash
main_code=000
tile_code=000
for i in $(seq 1 30); do
  main_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/ || true)
  tile_code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5001/tile-health || true)
  [ "$main_code" = "200" ] && [ "$tile_code" = "204" ] && break
  sleep 2
done
...
if [ "$main_code" != "200" ] || [ "$tile_code" != "204" ]; then
  echo "Smoke test FAILED (main HTTP $main_code, tile health HTTP $tile_code)"
  exit 1
fi
echo "Smoke test OK (main HTTP 200, tile health HTTP 204)"
```

Preserve existing process cleanup and runtime-data stripping.

- [ ] **Step 6: Update distribution guidance**

In `docs/guides/DISTRIBUTION.md`, state:

- `5001` bind failure is recorded server-side and falls back immediately;
- firewall/NAT/client reachability failure is detected by the browser within 1 second and falls back after refresh/startup;
- HTTPS pages do not probe the plaintext listener and stay same-origin;
- blocking `5001` reduces performance but does not remove functionality;
- both ports remain unauthenticated on `0.0.0.0`, and only trusted LAN exposure is appropriate.

- [ ] **Step 7: Verify Task 6**

Run:

```bash
pytest -q \
  tests/test_basemap_proxy_route.py \
  tests/test_basemap_tile_cache.py \
  tests/test_fix_build_ci_hardening.py \
  tests/test_docs_claims.py
```

Expected: all selected tests pass and workflow contracts prove both endpoints are checked.

- [ ] **Step 8: Review checkpoint without committing**

Inspect workflow YAML indentation and shell quoting. Confirm smoke teardown still runs before failure reporting and no release packaging behavior changed. Do not stage or commit.

---

### Task 7: Run integrated verification and inspect the final contract

**Files:**
- Verify all files changed in Tasks 1-6
- Save this implementation plan after leaving plan mode to: `docs/superpowers/plans/2026-08-10-tile-origin-connection-isolation.md`

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: verified end-to-end source contract, passing test suite, and a reviewable unstaged diff with no Git mutation.

- [ ] **Step 1: Run JavaScript syntax checks**

Run:

```bash
node --check static/js/ui.js
node --check static/js/map.js
node --check static/js/history.js
node --check static/js/tasks.js
node --check static/js/socket.js
```

Expected: every command exits `0` with no output.

- [ ] **Step 2: Run the complete focused regression set**

Run:

```bash
pytest -q \
  tests/test_tile_server.py \
  tests/test_tile_origin_runtime.py \
  tests/test_runtime_mode.py \
  tests/test_fix_infra_e.py \
  tests/test_basemap_source.py \
  tests/test_basemap_proxy_route.py \
  tests/test_basemap_tile_cache.py \
  tests/test_map_js_contract.py \
  tests/test_fix_frontend_hardening.py \
  tests/test_socket_singleton_contract.py \
  tests/test_tasks_js_contract.py \
  tests/test_tif_info_frontend.py \
  tests/test_splash_ready_signal.py \
  tests/test_fix_terrain_preview_transition.py \
  tests/test_terrain_lighting_frontend.py \
  tests/test_terrain_hillshade.py \
  tests/test_terrain_static.py \
  tests/test_local_terrain_static.py \
  tests/test_layer_json.py \
  tests/test_fix_dem_parent_url_config.py \
  tests/test_local_terrain_api.py \
  tests/test_fix_config_path_validation.py \
  tests/test_fix_build_ci_hardening.py \
  tests/test_docs_claims.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
pytest -q
```

Expected: full suite exits `0`. If an external dependency or host-specific condition blocks it, preserve the exact failing command/output and do not claim full verification.

- [ ] **Step 4: Perform a local HTTP smoke only when the normal ports are safe**

First inspect the existing instance lock and listening ports without deleting or stopping anything. If no running TerraForge instance owns `5000/5001`, start this checkout's application, remember only the PID created by this step, and verify:

```text
GET http://127.0.0.1:5000/                  -> 200
GET http://127.0.0.1:5001/tile-health       -> 204
GET http://127.0.0.1:5001/api/basemap       -> 404
GET http://127.0.0.1:5001/socket.io/         -> 404
```

Stop only the PID created by this step. Never delete `data/.terraforge-instance.lock` and never stop a pre-existing process. When the normal ports are already owned, skip this step and record that temporary-port integration tests supplied the HTTP evidence.

- [ ] **Step 5: Perform the browser connection-pool acceptance check when a safe running instance is available**

Open `http://127.0.0.1:5000/`, disable browser cache, and reload with the Network panel recording. Verify:

```text
HTML, REST API and /socket.io requests use :5000
/basemap, /tiles, /contour, /terrain and hillshade PNG requests use :5001
initTasks API requests are issued while cold :5001 tiles are still pending
blocking :5001 and reloading delays Viewer creation by no more than about 1 second,
then application tile requests fall back to :5000
```

Do not add Playwright or another browser dependency for this check. If no browser or safe running instance is available, report this exact browser acceptance check as not run rather than inferring success from source tests.

- [ ] **Step 6: Inspect the final source contract**

Use Grep/Read to confirm:

```text
all production tileUrl calls have one argument
no production _tileUrl helper remains
initTileOrigin is called before both Cesium Viewer constructors
Socket.IO still uses exactly one no-argument io()
active source defaults contain /terrain/base, not localhost:5000
both workflows probe /tile-health
```

Also inspect `git diff --check` and `git status --short`. Do not alter unrelated worktree files.

- [ ] **Step 7: Save the plan and report verification evidence**

After plan mode exits, copy this approved plan content to `docs/superpowers/plans/2026-08-10-tile-origin-connection-isolation.md` using the file tools. Report:

- files changed;
- focused and full test commands with pass/fail counts;
- whether real HTTP and browser smoke checks ran;
- any environment-limited verification;
- that no commit was created and all changes remain unstaged unless the user separately requests Git actions.
