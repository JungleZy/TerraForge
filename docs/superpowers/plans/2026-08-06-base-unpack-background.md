# 底图解压移到启动后台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 随包底图的解压从「第一次切片时同步做」改成「启动后立刻在后台做」，进度实时显示在底部状态栏右侧，所有页面可见。

**Architecture:** 新增 `src/services/base_terrain_warmup.py` 持有状态单例（`{phase, fraction, error}`）与后台执行体，`create_app()` 调一次 `start_warmup(socketio)`；进度经 socketio 广播（节流 0.5 s，终态绕过节流），新客户端在 `connect` 时拿一次快照。前端把 socket 实例从 `tasks.js` 提成全局单例 `window.TerraSocket`，新增一个全局状态栏元素显示进度。

**Tech Stack:** Python 3.12 / Flask-SocketIO 5.3.4（`async_mode=threading`，实测）/ 原生 JS（无框架、无构建）/ Jinja2 / pytest。

**设计稿:** `docs/superpowers/specs/2026-08-06-base-unpack-background-design.md`（先读它，尤其「排除的方案」与「已知代价」两节）

## Global Constraints

- 语言：注释、docstring、提交信息一律中文；代码标识符英文。
- `base_terrain_warmup.py` 可以 import `base_terrain`，但**不得 import numpy / osgeo / GDAL**。它继承 `base_terrain` 的定位 —— 要能在没有 GDAL 的环境里单测。
- 单测**绝不能**往仓库的 `assets/terrain/` 里解压或写入。`tests/conftest.py` 的 autouse fixture `isolate_base_terrain` 已经把 `bundle_dir` 指到空沙箱，新测试**不要**绕过它去 patch 更上层的 `_assets_terrain_dir`，除非该用例确实要测真解压。
- **每新增一处 `url_for('static', ...)` 模板引用，必须同步 `tests/test_css_contract.py` 里 `test_every_static_reference_in_templates_exists_on_disk` 的 `assert len(refs) == N`**，并照该处既有格式追加一行说明「N -> N+1（本次改动）：加了什么、为什么」。当前值是 **20**。漏改就是红。
- 新增 i18n key 必须同时有 `zh` 和 `en`，且两个 locale 的占位符集合一致（`tests/test_i18n.py` 会报错，不是警告）。新的 catalog 模块必须在 `src/i18n/catalog/__init__.py` 的 `_DOMAINS` 里显式登记 —— 没有 pkgutil 自动发现，Nuitka 扫不到。
- 每个 Task 结束时**只跑该 Task 涉及的测试文件**，不跑全量。全量在 Task 5 统一跑。
- 不要跑 formatter / linter。

---

### Task 1: `base_terrain_warmup` 模块 —— 状态机、后台执行体、节流

**Files:**
- Create: `src/services/base_terrain_warmup.py`
- Create: `tests/test_base_terrain_warmup.py`

**Interfaces:**
- Consumes: `src/services/terrain_tiling/base_terrain.py` 的 `base_cache_dir() -> Path`、`is_base_ready(cache_dir) -> bool`、`ensure_base_unpacked(cache_dir=None, stage_cb=None) -> Path | None`（分卷缺失返回 `None`，解压失败抛 `RuntimeError`）
- Produces:
  - `EVENT_NAME = "base_unpack_progress"`
  - `snapshot() -> dict` —— `{"phase": str, "fraction": float, "error": str | None}` 的**副本**
  - `start_warmup(socketio) -> None`
  - `reset_state() -> None` —— 仅测试用

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_base_terrain_warmup.py`：

```python
"""随包底图的启动预热：状态机 + 节流广播。

⚠️ 本文件不碰真实解压。`ensure_base_unpacked` 一律打桩 —— 真解压是 167 MB /
4.3 万个文件，而且会落进仓库的 assets/terrain（CI 里测试跑在 Nuitka 打包之前，
解出来的东西会被打进三个平台的产物）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeSocketIO:
    """记录 emit 载荷；start_background_task 只登记不执行，由用例自己调。

    不真起线程：线程让断言变成「等多久算够」的竞猜，是 flaky 的标准来源。
    执行体是个普通函数，直接调它就能覆盖全部逻辑。
    """

    def __init__(self, emit_raises=False):
        self.events = []
        self.tasks = []
        self._emit_raises = emit_raises

    def emit(self, name, payload=None):
        if self._emit_raises:
            raise RuntimeError("client gone")
        self.events.append((name, payload))

    def start_background_task(self, target, *args, **kwargs):
        self.tasks.append((target, args, kwargs))

    # 用例便利：只看 base_unpack_progress 的载荷序列
    def payloads(self):
        from src.services.base_terrain_warmup import EVENT_NAME
        return [p for n, p in self.events if n == EVENT_NAME]


@pytest.fixture(autouse=True)
def _clean_state():
    """模块级单例在同一个 pytest 进程里跨用例残留，不清就得靠用例顺序。"""
    from src.services import base_terrain_warmup as w
    w.reset_state()
    yield
    w.reset_state()


def test_ready_base_skips_the_thread_entirely(monkeypatch):
    """底图已就位 → 直接 ready，连后台任务都不起。

    这是 99% 的启动路径。起线程再让它立刻退出也能得到同样的状态，但那要多付
    一次线程创建，而启动路径上的每一毫秒都在用户盯着空白页的时间里。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: True)
    sio = FakeSocketIO()

    w.start_warmup(sio)

    assert sio.tasks == [], "已就位时不该起后台任务"
    assert w.snapshot() == {"phase": "ready", "fraction": 1.0, "error": None}
    assert sio.payloads()[-1]["phase"] == "ready"


def test_missing_base_starts_a_background_task_and_reaches_ready(monkeypatch, tmp_path):
    """底图缺失 → 起后台任务；执行体跑完后状态是 ready。"""
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    sio = FakeSocketIO()

    w.start_warmup(sio)

    assert w.snapshot()["phase"] == "running", "起线程之前就该置 running"
    assert len(sio.tasks) == 1
    target, args, _ = sio.tasks[0]

    target(*args)   # 手动执行后台体

    assert w.snapshot() == {"phase": "ready", "fraction": 1.0, "error": None}
    assert sio.payloads()[-1]["phase"] == "ready"


def test_repeated_start_does_not_spawn_a_second_task(monkeypatch):
    """已经在跑时再调 start_warmup → 不起第二个。

    生产上 create_app 只调一次，但这条挡的是「有人为了『保险』在别处又调了一次」，
    那会让两个线程同时抢跨进程锁，后到的那个白等几分钟。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: None)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    w.start_warmup(sio)

    assert len(sio.tasks) == 1


def test_failed_state_allows_a_retry(monkeypatch, tmp_path):
    """失败之后再调 start_warmup 会重新尝试。

    语义是「确保底图在解压或已就位」，不是「一辈子只跑一次」。生产上不会发生
    （create_app 只调一次），但「失败 → 修好前提 → 重试成功」是要能走通的。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise RuntimeError("Read-only file system")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()
    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])
    assert w.snapshot()["phase"] == "failed"

    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    w.start_warmup(sio)
    assert len(sio.tasks) == 2, "failed 之后必须能重试"
    sio.tasks[1][0](*sio.tasks[1][1])
    assert w.snapshot()["phase"] == "ready"


def test_unpack_failure_lands_in_failed_with_the_reason(monkeypatch):
    """解压抛 RuntimeError → phase=failed 且 error 带得上原因。

    最常见的原因是 assets/ 不可写（装在 Program Files、只读介质）。状态栏要
    hover 出这句话，error 是空的话用户只能看到「底图不可用」而无从下手。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise RuntimeError("随包底图解压失败：[Errno 30] Read-only file system")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    state = w.snapshot()
    assert state["phase"] == "failed"
    assert "Read-only" in state["error"]
    assert sio.payloads()[-1]["phase"] == "failed"


def test_missing_parts_is_a_failure_not_a_silent_ready(monkeypatch):
    """ensure_base_unpacked 返回 None（分卷被删）→ 同样是 failed。

    返回 None 在 base_terrain 的契约里不是异常，但在用户视角与解压失败是同一件
    事：底图不可用。当成成功处理的话状态栏会安静地什么都不显示，而地形产出从此
    不自包含 —— 又一款静默降级。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: None)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    state = w.snapshot()
    assert state["phase"] == "failed"
    assert state["error"], "分卷缺失必须给出原因，不能是空 error 的 failed"


def test_progress_is_throttled_but_the_terminal_event_always_fires(monkeypatch, tmp_path):
    """上万次回调只发少数几条，但终态一定发得出去。

    解压是流式读，readinto 每次调用都触发 stage_cb —— 167 MB 下上万次，不节流
    会把前端打爆。而终态被节流窗口吃掉就永远不发了，前端进度条卡在 97% 转到
    天荒地老 —— 所以终态必须绕过节流。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def fake_unpack(stage_cb=None):
        for i in range(500):
            stage_cb("base_unpack", i / 500.0)
        return tmp_path

    monkeypatch.setattr(w, "ensure_base_unpacked", fake_unpack)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    payloads = sio.payloads()
    running = [p for p in payloads if p["phase"] == "running"]
    assert len(running) < 20, f"节流没生效：500 次回调发出了 {len(running)} 条"
    assert payloads[-1] == {"phase": "ready", "fraction": 1.0, "error": None}


def test_emit_failure_does_not_break_the_state_machine(monkeypatch, tmp_path):
    """emit 抛异常（客户端断开）不能影响解压与状态。

    与 base_terrain._emit、cesium_terrain._gdal_stage_callback 同一条既有约定：
    一次广播故障不该让整件事失败。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    sio = FakeSocketIO(emit_raises=True)

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    assert w.snapshot()["phase"] == "ready"


def test_unexpected_exception_still_lands_in_failed(monkeypatch):
    """契约是 RuntimeError，但漏出别的异常时状态不能停在 running。

    后台线程里的异常会静默吞掉线程，状态永远是 running —— 前端进度条一直转，
    没有任何东西告诉用户已经没救了。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    assert w.snapshot()["phase"] == "failed"


def test_snapshot_returns_a_copy(monkeypatch):
    """调用方改不动内部状态。

    返回内部 dict 的话，connect handler 把它 emit 出去的同时后台线程还在改它 ——
    序列化到一半状态变了，客户端收到 phase=running / fraction=1.0 这种撕裂组合。
    """
    from src.services import base_terrain_warmup as w

    first = w.snapshot()
    first["phase"] = "tampered"
    assert w.snapshot()["phase"] == "idle"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_terrain_warmup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.base_terrain_warmup'`

- [ ] **Step 3: 写实现**

创建 `src/services/base_terrain_warmup.py`：

```python
"""随包底图的启动预热：后台解压 + 进度广播。

**为什么不放进 base_terrain.py**：那个模块刻意不 import numpy / osgeo / flask，
才能在没有 GDAL 的环境里单测（它自己的 docstring 写着这条）。让它认识 socketio
会毁掉这个性质。而且「怎么解压」与「什么时候跑、进度报给谁」本来就是两件事。

**为什么不放进 app_factory.py**：那是组装根，只做接线不放业务逻辑。

状态是进程级单例。解压一次启动最多跑一次，没有第二个消费者需要独立状态。
"""
from __future__ import annotations

import logging
import threading
import time

from src.services.terrain_tiling.base_terrain import (
    base_cache_dir,
    ensure_base_unpacked,
    is_base_ready,
)

logger = logging.getLogger(__name__)

EVENT_NAME = "base_unpack_progress"

# 广播最小间隔（秒）。解压是流式读，_extract_stream 的 readinto 每次调用都触发
# 一次 stage_cb —— 167 MB 下是上万次，不节流会把前端打爆（范本：
# dem_task_manager._PROGRESS_EMIT_MIN_INTERVAL，那里的注释写明「严格时间窗，
# 无『变化必发』豁免」，同样适用）。
# 取 0.5 而不是照抄那边的 1.0：解压是分钟级的一次性过程，切片是小时级长跑，
# 同样的绝对间隔在短过程上显得迟钝。0.5 秒下整个解压最多几百次 emit。
_EMIT_MIN_INTERVAL = 0.5

# 后台解压线程写、socketio 的 connect handler 在请求线程里读 —— 是真实的跨线程
# 访问，锁不是形式主义。
_LOCK = threading.Lock()
_STATE = {"phase": "idle", "fraction": 0.0, "error": None}


def snapshot() -> dict:
    """线程安全地读当前状态。

    返回**副本**：后台线程随时在写，调用方拿着内部 dict 的引用会读到撕裂的状态
    （fraction 已更新而 phase 还没）。connect handler 正是拿它去序列化。
    """
    with _LOCK:
        return dict(_STATE)


def reset_state() -> None:
    """只给测试用：模块级单例在同一个 pytest 进程里跨用例残留。"""
    with _LOCK:
        _STATE.update({"phase": "idle", "fraction": 0.0, "error": None})


def _set(phase: str, fraction: float, error: str | None = None) -> dict:
    """写状态并返回快照（供紧接着的 emit 用，避免二次加锁）。"""
    with _LOCK:
        _STATE["phase"] = phase
        _STATE["fraction"] = fraction
        _STATE["error"] = error
        return dict(_STATE)


def _emit(socketio, payload: dict) -> None:
    """广播状态，吞掉异常。

    一次广播故障（客户端断开等）不该影响解压 —— 与 base_terrain._emit、
    cesium_terrain._gdal_stage_callback 同一条既有约定。
    """
    try:
        socketio.emit(EVENT_NAME, payload)
    except Exception:
        logger.debug("base_unpack 进度广播失败（已忽略）", exc_info=True)


def start_warmup(socketio) -> None:
    """确保底图在解压或已就位。幂等；已就位时连线程都不起。

    已就位是 99% 的启动路径，开销就是 is_base_ready 的三次 iterdir 计数。
    phase == 'running' 时直接返回，不起第二个线程 —— 两个线程会同时抢跨进程锁，
    后到的白等几分钟。phase == 'failed' 时会**重新尝试**：本函数的语义是「确保
    底图在解压或已就位」，不是「一辈子只跑一次」。
    """
    # is_base_ready 是磁盘 IO，放在锁外 —— 持锁做 IO 会把 connect handler 的
    # snapshot() 一起堵住。
    try:
        ready = is_base_ready(base_cache_dir())
    except Exception:
        # 路径解析失败（打包目录异常等）当作没就位，让后台去试并给出真正的原因。
        ready = False

    with _LOCK:
        if _STATE["phase"] == "running":
            return
        # 检查与置位必须在同一个锁里：分成两段的话，两个并发调用都能通过检查，
        # 然后各起一个线程。
        _STATE["phase"] = "ready" if ready else "running"
        _STATE["fraction"] = 1.0 if ready else 0.0
        _STATE["error"] = None
        payload = dict(_STATE)

    _emit(socketio, payload)
    if ready:
        logger.debug("Terrain: 随包底图已就位，跳过预热")
        return

    logger.info("Terrain: 随包底图未就位，启动后台预热")
    socketio.start_background_task(_run, socketio)


def _run(socketio) -> None:
    """后台执行体：解压 + 节流上报。任何异常都转成 failed，绝不冒到线程外。"""
    last_emit = [0.0]

    def stage_cb(_phase, fraction):
        # last_emit 初值 0.0 而 time.monotonic() 是个大数 —— 第一次回调必发，
        # 前端立刻看到进度条而不是等满一个窗口。
        now = time.monotonic()
        if now - last_emit[0] < _EMIT_MIN_INTERVAL:
            return
        last_emit[0] = now
        _emit(socketio, _set("running", float(fraction)))

    try:
        result = ensure_base_unpacked(stage_cb=stage_cb)
    except RuntimeError as e:
        logger.warning(f"Terrain: 随包底图预热失败（{e}）；"
                       f"切片时会重试一次，仍失败则退回 parentUrl 级联")
        _emit(socketio, _set("failed", 0.0, str(e)))
        return
    except Exception as e:
        # 契约是 RuntimeError，但线程里漏出的任何异常都会静默吞掉线程，状态永远
        # 停在 running —— 前端进度条一直转，没有任何东西告诉用户已经没救了。
        logger.exception("Terrain: 底图预热遇到未预期的异常")
        _emit(socketio, _set("failed", 0.0, str(e)))
        return

    if result is None:
        # base_terrain 用 None 表示「找不到分卷」，那不是异常但在用户视角与解压
        # 失败是同一件事：底图不可用。当成成功会让状态栏安静地什么都不显示。
        msg = "找不到随包底图分卷（assets/terrain/base_z8.tar.gz.part*）"
        logger.warning(f"Terrain: {msg}")
        _emit(socketio, _set("failed", 0.0, msg))
        return

    logger.info(f"Terrain: 底图预热完成 {result}")
    _emit(socketio, _set("ready", 1.0))
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_terrain_warmup.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: 提交**

```bash
git add src/services/base_terrain_warmup.py tests/test_base_terrain_warmup.py
git commit -m "feat(terrain): 底图启动预热模块 —— 状态单例 + 后台解压 + 节流广播"
```

---

### Task 2: 接线 —— `create_app` 触发 + `connect` 推快照

**Files:**
- Modify: `src/app_factory.py`（顶部预热 import 清单 + `create_app` 里一次调用）
- Modify: `src/routes/socketio_events.py`（`connect` handler 末尾推快照）
- Modify: `tests/test_fix_socketio_events.py`
- Create: `tests/test_base_unpack_wiring.py`

**Interfaces:**
- Consumes: Task 1 的 `start_warmup(socketio)`、`snapshot()`、`EVENT_NAME`
- Produces: 无新公开符号

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_base_unpack_wiring.py`：

```python
"""底图预热的接线：create_app 必须触发它。

不接的话 Task 1 整个模块就是死代码，而现象是「启动后什么都没发生」—— 没有报错、
没有日志差异，只有第一次切片时又回到那段几分钟的无提示等待。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_create_app_kicks_off_the_warmup(monkeypatch, tmp_path):
    """create_app 走完之后 start_warmup 必须被调用过一次，且拿到的是真 socketio。"""
    from src.core import config as config_mod

    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)

    from src.services import base_terrain_warmup as w

    calls = []
    monkeypatch.setattr(w, "start_warmup", lambda sio: calls.append(sio))

    import src.app_factory as factory
    app, socketio = factory.create_app()[:2]

    assert len(calls) == 1, f"start_warmup 被调用 {len(calls)} 次，应当恰好 1 次"
    assert calls[0] is socketio, "传进去的必须是 create_app 构造的那个 socketio"


def test_app_factory_lists_the_module_for_nuitka():
    """预热 import 清单里要有新模块。

    那份清单同时是**打包的可达性清单**（模块 docstring 写明）：凡是只在函数体内
    import 的模块都要列出来，让 Nuitka 的静态分析看得见。漏了的话源码运行一切正常，
    打包产物启动即 ModuleNotFoundError —— 而这只有真去跑 exe 才会发现。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "src", "app_factory.py"), encoding="utf-8") as f:
        src = f.read()
    assert "import src.services.base_terrain_warmup" in src
```

追加到 `tests/test_fix_socketio_events.py`（复用该文件已有的 `FakeSocketIO` / `registered` / `flask_app` fixture）：

```python
def test_connect_pushes_the_base_unpack_snapshot(registered, flask_app, monkeypatch):
    """新客户端连上时必须收到一次底图状态快照。

    没有这一步，两个真实场景失效：用户在解压跑到一半才打开浏览器（要等下一个
    节流窗口）；以及用户在**解压失败几小时后**才打开浏览器 —— 终态事件早发完了，
    他永远看不到那条失败标记，而「失败要一直显示」是既定要求。
    """
    from src.services import base_terrain_warmup as w

    sent = []
    monkeypatch.setattr(events, "emit", lambda name, payload=None: sent.append((name, payload)))
    monkeypatch.setattr(w, "snapshot", lambda: {"phase": "running", "fraction": 0.4, "error": None})

    with flask_app.test_request_context():
        from flask import request
        request.sid = "sid-1"
        registered.handlers["connect"]()

    names = [n for n, _ in sent]
    assert w.EVENT_NAME in names, f"connect 没推底图快照，只发了 {names}"
    payload = dict(sent)[w.EVENT_NAME]
    assert payload["phase"] == "running" and payload["fraction"] == 0.4


def test_connect_snapshot_failure_does_not_break_the_connection(registered, flask_app, monkeypatch):
    """快照取不到时连接照常建立。

    这是个附加信息，不是连接的前提条件。让它把 connect 打挂的话，一个底图相关的
    小毛病会变成「整个实时推送用不了」。
    """
    from src.services import base_terrain_warmup as w

    sent = []
    monkeypatch.setattr(events, "emit", lambda name, payload=None: sent.append((name, payload)))

    def boom():
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(w, "snapshot", boom)

    with flask_app.test_request_context():
        from flask import request
        request.sid = "sid-2"
        registered.handlers["connect"]()

    assert "connected" in [n for n, _ in sent], "欢迎消息仍必须发出去"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_unpack_wiring.py tests/test_fix_socketio_events.py -q`
Expected: FAIL — `test_create_app_kicks_off_the_warmup` 断言 `0 != 1`；`test_app_factory_lists_the_module_for_nuitka` 断言失败；两条 connect 用例断言 `EVENT_NAME not in names`

- [ ] **Step 3: 改 `src/app_factory.py`**

顶部预热 import 清单里，在 `import src.services.contour_task_manager` 之前加一行（按字母序）：

```python
import src.services.base_terrain_warmup  # noqa: F401
```

`create_app()` 里，把 `register_socketio_events(socketio)` 那两行改成：

```python
    register_socketio_events(socketio)

    # 随包底图的后台预热。放在 socketio 与蓝图都就绪之后：它会立刻 emit 一次
    # 初始状态，而 connect handler 要能拿到同一个模块的快照。
    # 已就位时这里连线程都不起（三次 iterdir 计数就返回），是 99% 的启动路径；
    # 缺失时起一个后台线程，绝不阻塞启动 —— 解压 4.3 万个小文件在 Windows 上
    # 要几分钟，同步做的话用户看到的就是一个卡死的启动。
    # create_app 只在 StartupRole.should_create_app 时被调用，所以 dev reloader
    # 的观察者父进程、multiprocessing worker 都不会重复解压。
    start_warmup(socketio)

    logger.debug("Application initialization complete")
    return (app, socketio) + managers
```

并在 `create_app()` 顶部的函数内 import 区加一行（与既有的 `from src.services.task_cleanup import sweep_startup_residue` 并列）：

```python
    from src.services.base_terrain_warmup import start_warmup
```

⚠️ 必须是**函数体内 import**，与该文件的既有约定一致（模块 docstring 解释了原因：测试会 pop 掉 `src.services.*` 再重新 import，模块级 import 会把旧模块对象钉死，导致「测试 patch 新模块、请求却打到旧模块」的静默假绿）。顶部那行 `import src.services.base_terrain_warmup` 是给 Nuitka 看的预热清单，不留对象引用，两者不冲突。

- [ ] **Step 4: 改 `src/routes/socketio_events.py`**

在 `handle_connect` 里，`emit('connected', {...})` 之后追加：

```python
            # 底图预热状态快照。必须单独套一层 try：这是附加信息不是连接的前提，
            # 让它把 connect 打挂的话，一个底图相关的小毛病会变成「整个实时推送
            # 用不了」。
            #
            # 为什么 connect 时要推：进度是广播的，中途连上的客户端只会收到之后
            # 的增量。而解压**失败**是终态，事件早就发完了 —— 几小时后才打开
            # 浏览器的用户永远看不到那条失败标记，可「失败要一直显示」是既定要求。
            try:
                from src.services.base_terrain_warmup import EVENT_NAME, snapshot
                emit(EVENT_NAME, snapshot())
            except Exception:
                logger.debug("底图状态快照推送失败（已忽略）", exc_info=True)
```

- [ ] **Step 5: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_unpack_wiring.py tests/test_fix_socketio_events.py tests/test_base_terrain_warmup.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/app_factory.py src/routes/socketio_events.py \
        tests/test_base_unpack_wiring.py tests/test_fix_socketio_events.py
git commit -m "feat(terrain): 启动时触发底图预热，connect 时推状态快照"
```

---

### Task 3: socket 实例单例化（含 vendor block 拆分）

**Files:**
- Create: `static/js/socket.js`
- Modify: `templates/base.html`（拆 `vendor_js` block + 引入 `socket.js`）
- Modify: `templates/config.html:7`（只清 Cesium，保留 socket.io）
- Modify: `static/js/tasks.js:18`
- Modify: `tests/test_css_contract.py`（引用计数 20 → 21）
- Create: `tests/test_socket_singleton_contract.py`

**Interfaces:**
- Produces: `window.TerraSocket.get()` —— 返回全局唯一的 socket.io 实例（惰性创建）

**背景（实施前必读）：** 现状只有首页创建 socket（`tasks.js:18` 的 `socket = io()`），`/history`、`/config` 没有任何 WebSocket 连接。更关键的是 **`/config` 页覆盖了 `{% block vendor_js %}{% endblock %}` 为空**，连 socket.io 库都不加载 —— 那个 block 把 Cesium（5.7 MB）和 socket.io（44 KB）绑在了一起。设计要求「所有页面都能看到解压进度」，所以必须把两者拆开：Cesium 继续只在首页/历史页，socket.io 变成全局。44 KB 与 5.7 MB 不是一个量级，那行注释里说的「白付的解析/下载全省」针对的是前者。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_socket_singleton_contract.py`：

```python
"""socket 实例必须是全局单例，且每个页面都拿得到。

本项目没有 JS 测试框架（无 package.json，且不打算引入 —— 会破坏离线打包形态），
所以这些断言守的是源码**形态**，与 tests/test_tasks_js_contract.py 同一路数。
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_socket_js_exposes_a_lazy_singleton():
    """window.TerraSocket.get() 是唯一的创建点。"""
    src = _read("static", "js", "socket.js")
    assert "window.TerraSocket" in src
    assert re.search(r"\bio\s*\(", src), "socket.js 里应当有唯一的 io() 调用"


def test_tasks_js_no_longer_creates_its_own_socket():
    """tasks.js 必须复用单例，不能自己 io()。

    自己建一个的话首页会开出两个 WebSocket 连接：服务端 connected_clients 计数
    翻倍，每条广播被处理两遍，而现象只是「偶尔重复刷新」，极难归因。
    """
    src = _read("static", "js", "tasks.js")
    assert "window.TerraSocket.get()" in src, "tasks.js 没有复用全局单例"
    assert not re.search(r"socket\s*=\s*io\s*\(", src), (
        "tasks.js 仍在直接 io() —— 会开出第二个连接")


def test_socket_io_vendor_is_loaded_on_every_page():
    """socket.io 库不能再跟 Cesium 绑在同一个 block 里。

    /config 页刻意把 vendor block 覆盖成空以省掉 Cesium 的 5.7 MB，socket.io
    （44 KB）被顺带省掉了 —— 那样它就收不到底图解压进度，而「所有页面都要看得到」
    是既定要求。
    """
    base = _read("templates", "base.html")
    config = _read("templates", "config.html")

    assert "vendor_map_js" in base, "base.html 应当把 Cesium 单独放进 vendor_map_js"
    # socket.io 的 <script> 必须在 vendor_map_js 之外
    map_block = re.search(r"{%\s*block vendor_map_js\s*%}(.*?){%\s*endblock\s*%}",
                          base, re.S)
    assert map_block, "找不到 vendor_map_js block"
    assert "socket.io" not in map_block.group(1), (
        "socket.io 还在 vendor_map_js 里 —— /config 覆盖该 block 时会连它一起省掉")

    assert "vendor_map_js" in config, "config.html 应当只覆盖 vendor_map_js"
    assert "{% block vendor_js %}{% endblock %}" not in config, (
        "config.html 还在覆盖旧的 vendor_js block，socket.io 仍会被省掉")


def test_base_html_loads_socket_js_after_ui_js():
    """socket.js 要排在 ui.js 之后 —— 它调 initConnectionStatus（ui.js 定义的）。"""
    base = _read("templates", "base.html")
    ui = base.index("js/ui.js")
    sock = base.index("js/socket.js")
    assert ui < sock, "socket.js 必须排在 ui.js 之后，否则 initConnectionStatus 还没定义"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_socket_singleton_contract.py -q`
Expected: FAIL — 四条全红（`static/js/socket.js` 不存在，`FileNotFoundError`）

- [ ] **Step 3: 创建 `static/js/socket.js`**

```javascript
/**
 * 全局 Socket.IO 单例。
 *
 * 为什么要有它：socket 实例原本由 tasks.js 创建（`socket = io()`），只有首页有。
 * 底图解压进度要在**所有页面**显示，而 /history、/config 根本没有连接。把创建点
 * 提到这里，各页按需 get()，全站只有一个连接。
 *
 * ⚠️ 不能写成 `window.socket = io()`：tasks.js 顶部的 `let socket` 在全局作用域会
 * **遮蔽** window.socket，两边看到的是不同的东西（`let` 声明不挂到 window 上，
 * 但同作用域内的引用会解析到它）。用带命名空间的 window.TerraSocket 避开。
 */
(function () {
    'use strict';

    let instance = null;

    function get() {
        if (instance) return instance;
        if (typeof io !== 'function') {
            // socket.io 库没加载（理论上不该发生 —— 它现在是全局 vendor）。
            // 返回 null 而不是抛：调用方都有 null 守卫，实时推送降级即可，
            // 不该把整个页面脚本打挂。
            console.warn('socket.io 库未加载，实时推送不可用');
            return null;
        }
        instance = io();
        if (window.initConnectionStatus) window.initConnectionStatus(instance);
        return instance;
    }

    window.TerraSocket = { get: get };

    // 立即建立连接：底图解压进度是**启动就开始**的全局事件，等某个页面脚本
    // 想起来调 get() 就晚了。
    get();
})();
```

- [ ] **Step 4: 改 `templates/base.html`**

把现有的 vendor block 整段（从 `{# 地图/实时推送的重型 vendor...` 注释到 `{% endblock %}`）替换成：

```jinja
    {# 地图重型 vendor(Cesium 5.7MB):只有首页和历史页用得到,/config 独立页
       覆盖本 block 为空,白付的解析/下载全省。
       ⚠️ socket.io **不在**这里 —— 它原本与 Cesium 同 block,于是 /config 连它
       一起省掉了,那一页收不到任何实时推送(包括底图解压进度,而那是全站可见的
       要求)。44 KB 与 5.7 MB 不是一个量级,拆开各管各的。
       url_for 必须保持字符串字面量 —— tests/test_css_contract.py 按正则抠模板
       源码对账 vendor 清单与引用数。 #}
    {% block vendor_map_js %}
    <!-- CesiumJS 1.143.0（本地 vendor，断网可用；CESIUM_BASE_URL 必须在
         Cesium.js 之前设好，workers/assets 都按它解析） -->
    <script>window.CESIUM_BASE_URL = "{{ url_for('static', filename='vendor/cesium/1.143.0') }}/";</script>
    <script src="{{ url_for('static', filename='vendor/cesium/1.143.0/Cesium.js') }}"></script>
    {% endblock %}

    <!-- Socket.IO 4.5.4 —— 版本必须锁在 4.x：服务端 Flask-SocketIO 5.3.4 /
         python-socketio 5.9.0 / python-engineio 4.7.1 对应 Socket.IO 协议 v5 +
         Engine.IO v4，换大版本会直接握不上手。
         全局加载（不在任何 block 里）：底图解压进度要在所有页面显示。 -->
    <script src="{{ url_for('static', filename='vendor/socket.io/4.5.4/socket.io.min.js') }}"></script>
```

然后在 `js/ui.js` 那行 `<script>` 之后、`js/theme.js` 之前插入：

```jinja
    <!-- 全局 Socket.IO 单例（window.TerraSocket）。必须排在 ui.js 之后 ——
         它建立连接后立刻调 initConnectionStatus 点亮底部状态栏的连接指示。 -->
    <script src="{{ url_for('static', filename='js/socket.js') }}"></script>
```

- [ ] **Step 5: 改 `templates/config.html:7`**

```jinja
{% block vendor_map_js %}{% endblock %}
```

（只清 Cesium。socket.io 现在在 block 之外，自动保留。）

- [ ] **Step 6: 改 `static/js/tasks.js:18`**

```javascript
    socket = window.TerraSocket.get();
```

删掉紧随其后的 `if (window.initConnectionStatus) window.initConnectionStatus(socket);` —— 连接指示器现在由 `socket.js` 在创建时点亮，留着就是调两遍。

- [ ] **Step 7: 更新 `tests/test_css_contract.py` 的引用计数**

在 `test_every_static_reference_in_templates_exists_on_disk` 的注释末尾追加一行，并把断言从 20 改成 21：

```python
    # 20 -> 21（底图解压进度）：base.html 新增 static/js/socket.js
    # （window.TerraSocket 全局单例；socket 实例原本由 tasks.js 独占创建，
    # 只有首页有，而解压进度要求全站可见）。
    assert len(refs) == 21, (
```

- [ ] **Step 8: 跑测试确认它绿**

Run: `uv run pytest tests/test_socket_singleton_contract.py tests/test_css_contract.py tests/test_tasks_js_contract.py -q`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add static/js/socket.js static/js/tasks.js templates/base.html templates/config.html \
        tests/test_socket_singleton_contract.py tests/test_css_contract.py
git commit -m "refactor(js): socket 实例提成全局单例，socket.io 与 Cesium 拆开加载"
```

---

### Task 4: 底部状态栏的解压进度

**Files:**
- Create: `static/js/base_terrain_status.js`
- Create: `src/i18n/catalog/js_base_terrain.py`
- Modify: `src/i18n/catalog/__init__.py`（登记新域）
- Modify: `templates/base.html`（状态栏元素 + 脚本引用）
- Modify: `static/css/style.css`（新元素样式 + 窄屏规则改写）
- Modify: `tests/test_css_contract.py`（引用计数 21 → 22）
- Create: `tests/test_base_unpack_statusbar.py`

**Interfaces:**
- Consumes: Task 1 的 `EVENT_NAME = "base_unpack_progress"` 与载荷 `{phase, fraction, error}`；Task 3 的 `window.TerraSocket.get()`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_base_unpack_statusbar.py`：

```python
"""底部状态栏的底图解压进度：markup / JS / CSS / i18n 的源码契约。"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_statusbar_element_sits_after_the_page_block():
    """元素要在 {% block statusbar %} 之后 —— 那才是「右侧」。

    statusbar 是 flex 且 gap: 0，位置完全由 DOM 顺序决定。放在 block 之前就跑到
    页面自己的读数左边去了。
    """
    base = _read("templates", "base.html")
    block = base.index("{% block statusbar %}")
    elem = base.index('id="statusBaseUnpack"')
    assert block < elem, "解压进度元素必须排在页面 statusbar block 之后（最右）"


def test_statusbar_element_is_hidden_by_default():
    """默认 hidden：99% 的启动底图已就位，状态栏不该多出一块常驻的空位。"""
    base = _read("templates", "base.html")
    m = re.search(r'<span[^>]*id="statusBaseUnpack"[^>]*>', base)
    assert m, "找不到 statusBaseUnpack 元素"
    assert "hidden" in m.group(0)


def test_js_handles_running_failed_and_collapses_otherwise():
    """running / failed 各有自己的分支，其余（idle / ready）收起来。

    漏掉 failed 分支就等于失败静默 —— 那正是这个功能要消灭的东西。
    只钉这两个字面量：idle 与 ready 共用「收起」的 else 分支，没有各自的字面量
    可查，所以改钉那条分支的存在（box.hidden = true）。
    """
    src = _read("static", "js", "base_terrain_status.js")
    assert "base_unpack_progress" in src, "没有监听事件"
    assert "window.TerraSocket" in src, "应当复用全局 socket 单例"
    for phase in ("running", "failed"):
        assert f"'{phase}'" in src or f'"{phase}"' in src, f"没处理 phase={phase}"
    assert re.search(r"hidden\s*=\s*true", src), "没有「收起」分支，就绪后元素会一直占着状态栏"


def test_failure_reason_goes_into_the_title_attribute():
    """失败原因必须挂到 title 上。

    状态栏只有「底图不可用」四个字，用户无从下手；原因（多半是 assets/ 不可写）
    是他唯一能据以行动的信息。
    """
    src = _read("static", "js", "base_terrain_status.js")
    assert ".title" in src, "失败原因没有写进 title 属性"


def test_narrow_screen_rule_no_longer_depends_on_last_child():
    """窄屏隐藏规则不能再靠 :last-child。

    原规则 `.statusbar-item:last-child { display: none }` 的正确性依赖「时钟恰好
    排最后」这个巧合。新元素插到末尾之后它一个都选不中，时钟在窄屏不再隐藏 ——
    既有行为被静默改掉。改成按语义选中时钟本身。
    """
    css = _read("static", "css", "style.css")
    assert ".statusbar-item:last-child" not in css, (
        "窄屏规则仍依赖 :last-child —— 往 statusbar 末尾加任何东西都会踩到")
    assert ".statusbar-clock" in css, "窄屏规则应当按语义选中时钟"


def test_i18n_keys_exist_in_both_locales():
    """三个新 key 的中英文都要在 —— 漏翻会在界面上显示成 key 本身。"""
    from src.i18n.catalog import MESSAGES

    for key, zh in (
        ("js.base_unpack.running", "底图解压"),
        ("js.base_unpack.failed", "底图不可用"),
        ("js.base_unpack.failed_title", "全球底图解压失败"),
    ):
        assert key in MESSAGES, f"缺 i18n key: {key}"
        assert zh in MESSAGES[key]["zh"], f"{key} 的中文不是预期文案"
        assert MESSAGES[key]["en"], f"{key} 缺英文"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_unpack_statusbar.py -q`
Expected: FAIL — 全红（`static/js/base_terrain_status.js` 不存在，`base.html` 里没有该元素）

- [ ] **Step 3: 加 i18n 文案**

创建 `src/i18n/catalog/js_base_terrain.py`：

```python
"""static/js/base_terrain_status.js（底部状态栏的底图解压进度）的界面文案。

key 命名：`js.<模块>.<短名>`。只有 js.* 前缀会被内联进页面（window.__I18N__），
所以 JS 里要用的文案必须挂在这个前缀下。
"""

MESSAGES = {
    'js.base_unpack.running': {
        'zh': '底图解压 {percent}%',
        'en': 'Unpacking base terrain {percent}%',
    },
    'js.base_unpack.failed': {
        'zh': '底图不可用',
        'en': 'Base terrain unavailable',
    },
    # hover 出来的完整说明。用户看到「底图不可用」时唯一能据以行动的信息就是
    # {error}（多半是 assets/ 不可写），后半句告诉他这不影响切片本身。
    'js.base_unpack.failed_title': {
        'zh': '全球底图解压失败：{error}。地形切片仍可进行，但产出目录不会自包含。',
        'en': 'Base terrain unpack failed: {error}. Terrain tiling still works, '
              'but the output directory will not be self-contained.',
    },
}
```

在 `src/i18n/catalog/__init__.py` 的 import 与 `_DOMAINS` 里各加一处（按既有顺序，放在 `js_config` 之后）：

```python
from src.i18n.catalog import (api, app, js_base_terrain, js_config, js_history,
                              js_map, js_tasks, js_ui, tpl_base, tpl_config,
                              tpl_history, tpl_index, tpl_path_browser,
                              validation)
```

```python
    js_config,
    js_base_terrain,
    js_ui,
```

- [ ] **Step 4: 加 markup（`templates/base.html`）**

在 `{% block statusbar %}{% endblock %}` **之后**插入：

```jinja
            <!-- 随包底图的解压进度（全局事件，所有页面可见）。默认 hidden ——
                 99% 的启动底图已就位，状态栏不该多出一块常驻空位。
                 位置在页面 statusbar block 之后 = 最右；statusbar 是 flex 且
                 gap: 0，位置完全由 DOM 顺序决定。 -->
            <span class="statusbar-basemap" id="statusBaseUnpack" hidden>
                <span id="statusBaseUnpackText"></span>
                <span class="statusbar-progress" id="statusBaseUnpackProgress" hidden>
                    <span class="statusbar-progress__bar" id="statusBaseUnpackBar"></span>
                </span>
            </span>
```

在 `js/socket.js` 那行 `<script>` 之后插入：

```jinja
    <!-- 底部状态栏的底图解压进度。要排在 socket.js 之后（用它的单例）。 -->
    <script src="{{ url_for('static', filename='js/base_terrain_status.js') }}"></script>
```

- [ ] **Step 5: 写 `static/js/base_terrain_status.js`**

```javascript
/**
 * 底部状态栏的随包底图解压进度。
 *
 * 事件 base_unpack_progress 的载荷是 {phase, fraction, error}，
 * phase ∈ idle | running | ready | failed（见 src/services/base_terrain_warmup.py）。
 * 服务端在 connect 时也推一次快照 —— 解压失败是终态，中途/事后连上的客户端
 * 只能靠那一次快照拿到。
 */
(function () {
    'use strict';

    function render(state) {
        const box = document.getElementById('statusBaseUnpack');
        const text = document.getElementById('statusBaseUnpackText');
        const prog = document.getElementById('statusBaseUnpackProgress');
        const bar = document.getElementById('statusBaseUnpackBar');
        if (!box || !text || !prog || !bar) return;

        const phase = state && state.phase;

        if (phase === 'running') {
            const percent = Math.round(Math.max(0, Math.min(1, state.fraction || 0)) * 100);
            box.hidden = false;
            box.classList.remove('statusbar-basemap--failed');
            box.title = '';
            text.textContent = t('js.base_unpack.running', { percent: percent });
            prog.hidden = false;
            bar.style.width = percent + '%';
            return;
        }

        if (phase === 'failed') {
            box.hidden = false;
            box.classList.add('statusbar-basemap--failed');
            text.textContent = t('js.base_unpack.failed');
            // 原因是用户唯一能据以行动的信息（多半是 assets/ 不可写）。
            box.title = t('js.base_unpack.failed_title', { error: state.error || '' });
            prog.hidden = true;
            return;
        }

        // idle / ready：整个元素收起来。ready 之后不留「已完成」的残迹 ——
        // 那是一次性的启动事项，长期占着状态栏没有信息量。
        box.hidden = true;
        box.classList.remove('statusbar-basemap--failed');
        box.title = '';
        prog.hidden = true;
    }

    const socket = window.TerraSocket && window.TerraSocket.get();
    if (!socket) return;   // 没有 socket 的环境（库没加载）静默降级
    socket.on('base_unpack_progress', render);
})();
```

- [ ] **Step 6: 加 CSS 并改窄屏规则（`static/css/style.css`）**

在 `.statusbar-progress__bar` 那一组之后追加：

```css
/* 随包底图的解压进度（底部状态栏最右，全局元素）。
   刻意**不带** .statusbar-item：那个类会画一条左分隔线伪元素，而本元素在
   99% 的时间里是 hidden 的，带上分隔线会在它出现/消失时让整条状态栏抖一下。 */
.statusbar-basemap {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-left: 12px;
    padding: 0 12px;
    border-left: 1px solid var(--color-border);
}

.statusbar-basemap--failed {
    color: var(--color-danger);
    cursor: help;              /* 提示「这里有 title 可看」 */
}
```

窄屏那条规则（`@media` 块内）改成：

```css
    /* 窄屏隐藏时钟。**不要写回 .statusbar-item:last-child** —— 那条规则的正确性
       依赖「时钟恰好排在最后」这个巧合，往 statusbar 末尾加任何元素都会让它
       选中别的东西，或者一个都选不中（新元素不带 .statusbar-item 类）。
       底图解压进度在窄屏保留显示：它是要紧状态，窄屏更需要。 */
    .workbench-statusbar .statusbar-clock {
        display: none;
    }
```

- [ ] **Step 7: 更新 `tests/test_css_contract.py` 的引用计数**

注释末尾追加一行，断言从 21 改成 22：

```python
    # 21 -> 22（底图解压进度）：base.html 新增 static/js/base_terrain_status.js
    # （底部状态栏最右的解压进度，监听 base_unpack_progress）。
    assert len(refs) == 22, (
```

- [ ] **Step 8: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_unpack_statusbar.py tests/test_css_contract.py tests/test_i18n.py tests/test_socket_singleton_contract.py -q`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add static/js/base_terrain_status.js static/css/style.css templates/base.html \
        src/i18n/catalog/js_base_terrain.py src/i18n/catalog/__init__.py \
        tests/test_base_unpack_statusbar.py tests/test_css_contract.py
git commit -m "feat(ui): 底部状态栏显示底图解压进度，窄屏规则改按语义选中时钟"
```

---

### Task 5: 全量回归 + 真实启动验证

**Files:**
- Test: 全量

- [ ] **Step 1: 跑全量**

Run: `uv run pytest tests/ -q --durations=10`
Expected: 全绿。基线是 v0.2.9 的 1245 项；新增用例后应为 1245 + 本次新增数。

**若有既有用例变红**，逐个核对而不是改断言迁就：
- `tests/test_css_contract.py` 的引用计数 —— Task 3 与 Task 4 各加了一处，最终必须是 22。红了先数一遍 `grep -c "url_for('static'" templates/*.html`，别直接把数字改成报错里那个。
- `tests/test_tasks_js_contract.py` —— 它按函数体切 `initTasks`，Task 3 动了那个函数的第一行。若红，看是不是切函数体的正则被带偏了。
- `tests/test_theme_switch.py` / 其它 base.html 相关的模板契约 —— Task 3、4 都动了 `base.html` 的脚本顺序。

- [ ] **Step 2: 真实启动验证（本机源码运行）**

这一步不能省：前面全是源码形态断言，没有任何一条证明「浏览器里真的显示出来了」。

```bash
# 先把底图挪走，制造「未就位」的场景（同盘 mv 是瞬时的，验证完再挪回来）
mv assets/terrain/base_z8 /tmp/base_z8_backup 2>/dev/null || true

DEBUG=0 uv run python app.py
```

在浏览器打开 http://localhost:5000 ，确认四件事：

1. **启动没有卡住** —— 控制台出现 "Terrain: 随包底图未就位，启动后台预热" 之后立刻就能访问页面，不是等几分钟。
2. 底部状态栏右侧出现「底图解压 N%」，百分比在涨。
3. 切到 `/config` 页（那一页原本没有 socket），进度**照样显示**。这是 Task 3 拆 vendor block 的验收点。
4. 解压完成后该元素消失，`assets/terrain/base_z8/layer.json` 存在。

验证完停掉服务，把备份挪回去或删掉（解压出来的那份是等价的）：

```bash
rm -rf /tmp/base_z8_backup
```

- [ ] **Step 3: 确认仓库没被污染**

Run: `uv run pytest tests/test_no_repo_pollution.py -q && git status --short`
Expected: PASS，且 `git status` 里不出现 `assets/terrain/base_z8`（`.gitignore` 已挡）

- [ ] **Step 4: 提交（若有收尾改动）**

```bash
git add -A
git commit -m "test: 底图启动预热的全量回归"
```

---

## 完成后

改动是用户可见的行为变化（启动后多一段后台解压、状态栏多一个读数、`/config` 页多一个 WebSocket 连接），需要写进 `RELEASE_NOTES.md` 并 bump 版本。当前版本 `0.2.9` 尚未发布（v0.2.9 的说明已写好），所以**优先把本次改动并进 v0.2.9 的发版说明**，而不是另开一个版本号 —— 两者是同一批地形改动的两半。这一步不在本计划内，实施完成后单独确认。
