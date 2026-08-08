# 砍掉「取消任务」 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 任务只保留两个动作——暂停/继续、删除。`cancelled` 状态从代码里彻底消失（六态→五态），任务行按钮从 7 个降到 5 个，删除任何状态的任务都立即生效。

**Architecture:** 新增共享删除助手 `src/services/task_deletion.py`，四条管线的 `delete_task` 都走它。删除分两条路径：任务没在跑走**快路径**（同步删行 + 同步删产物，保持现有 `files_removed` 语义与全部现有测试）；任务在跑走**后台路径**（置 stop_flag → 进内存墓碑 → 同一事务里记 `pending_deletions` + 删行 → 立即 200 → daemon 线程 join 后删产物）。墓碑集合**只有 map 需要**（唯一有运行期 INSERT 的管线）。

**Tech Stack:** Python 3.12 / SQLite（`PRAGMA foreign_keys = ON` + WAL）/ threading / Flask + SocketIO / Vue 3 global build（无构建步骤）/ pytest

**上游设计：** `docs/superpowers/specs/2026-08-07-task-lifecycle-simplification-design.md`
**前置计划（已完成并发版 v0.2.11）：** `docs/superpowers/plans/2026-08-07-deletion-prerequisites.md`

## Global Constraints

- 本计划在隔离 worktree `../map-download-wt/simplify-task-actions`（分支 `simplify-task-actions`，基于 `d63f1f9`）里执行。
- 解释器用主仓的虚拟环境：**`/home/zhang/workspace/map-download/.venv/bin/python`**。worktree 下没有 `.venv`。
- **本 worktree 的干净基线是 1435 passed / 1 skipped**（已实测）。主仓有一批未提交改动会多带 44 个用例，别拿那个数字对照。
- 注释与文档一律中文，风格是**解释为什么，不解释是什么**。
- **注释里引符号名，不要引行号。** 计划 A 有整整一轮修复白跑在这上面：一个只改注释的提交被自己插入的 10 行把四组行号引用全部推移作废。必须写行号时，改完按最终文件 `sed -n 'Np' 文件` 机检再落笔。
- 不跑格式化工具、不跑 lint、不做计划之外的重构。
- **不要碰 `src/services/download_engine.py` 与 `tests/test_download_engine.py` 里的 `'cancelled'`** —— 那是瓦片级结果状态（`DownloadCancelled` 异常），与任务状态无关。`tests/test_download_engine.py:610-623` 与 `:666-686` 的 `assert all(r['status'] == 'cancelled' ...)` 保持原样。
- 每个 Task 结束跑一次全量测试。

## 四个设计决策（spec 之外，执行中必须遵守）

### D-A：`files_removed` 的同步语义用「快路径」保住

现状 `_delete_payload(message, files_removed)`（`src/routes/api.py:908-923`）**同步**返回产物删没删，`files_removed=False` 时还带一句 `files_message`。改成后台异步收尾后这个语义给不出来。

解法不是改语义，是**分流**：绝大多数删除针对的是已停止的任务，那条路继续同步删、`files_removed` 一字不改；只有「删正在跑的任务」是新增路径，走后台，响应里带新字段 `files_deferred: true` 而**不带** `files_removed`。现有 API 契约与现有断言因此全部不动。

### D-B：`local_terrain` 的第二套约定只归一「删除动作」，不归一「路径算法」

`LocalTerrainTaskManager.delete_task` 与另外三条不同：签名带 `delete_files: bool = True`（其他三条的路由默认 false）、返回 `Optional[bool]`、**产物由 manager 自己删**而不是路由层。

- **路径算法保留**：它刻意不信库存 `output_path`，从当前 `Config.DOWNLOADS_DIR` 重算（冻结 exe 搬迁后旧绝对路径会让守卫失效）。这是对的，不动。
- **`delete_files` 默认 True 保留**：前端 `history.js` 总是显式传参，改默认值只影响直连 API 的人，不值得为对称制造破坏性变更。
- **只把「什么时候删、谁来删」交给共享助手。**

### D-C：内存墓碑只有 map 需要

计划 A 已实测确认：`INSERT OR IGNORE` 不豁免外键（sqlite3 3.45.1，`FOREIGN KEY constraint failed`），而 map 的 `_write_progress_batch` 是四条管线里唯一的运行期 `INSERT`（`task_manager.py:1093` 写 `task_tiles`，只在**失败瓦片**时触发）。其余三条运行期只有 `UPDATE ... WHERE id=?`，对不存在的行是静默 no-op（同批实测，`rowcount=0` 不抛）。**不要为了对称给另外三条加墓碑。**

### D-D：i18n 必须补一条反向检查

`tests/test_i18n.py` 的三层检查（`:28` 两语种齐全、`:38` 占位符、`:48` 英文非中文）**全是对已有键做检查，没有任何 key↔引用 的双向闭合**。本计划要删 8 个 catalog 键：删键不会红，**漏删引用也不会红**（`:160` 只抓中文字面量）。没有这张网，前端会在运行时拿到 `undefined` 文案而测试全绿。

---

## File Structure

| 文件 | 责任 | 角色 |
|---|---|---|
| `src/services/task_deletion.py` | 删除的两条路径与后台收尾，四条管线共用 | **新建**（Task 1） |
| `src/services/task_manager.py` | map 管线 | Task 2：新增 `delete_task`（从路由下沉）+ `_deleting` 墓碑；Task 4：删 `cancel_task`、状态守卫 |
| `src/services/dem_task_manager.py` | DEM 管线 | Task 3 接入；Task 4 删 `cancel_task`、守卫 |
| `src/services/contour_task_manager.py` | 等高线管线 | 同上 |
| `src/services/local_terrain_task_manager.py` | 本地地形管线 | 同上（保留 D-B 的两处差异） |
| `src/routes/api.py` | map 路由 + `_delete_payload` | Task 2 改 DELETE、Task 4 删 `/cancel` |
| `src/routes/{dem,contour,local_terrain}_api.py` | 另三条路由 | Task 3/4 |
| `src/models/task.py` | `TaskStatus` 枚举 | Task 4 删 `CANCELLED`（`test_tasks_js_contract.py` ast 解析它，会自动跟进） |
| `static/js/{task_list,tasks,history}.js` | 前端行渲染与动作 | Task 5 |
| `templates/_history_content.html` / `static/css/style.css` | 筛选 chip / 状态样式 | Task 5 |
| `src/i18n/catalog/{js_tasks,js_history,tpl_history}.py` | 文案 | Task 5 |
| `tests/test_task_deletion.py` | 共享助手单测 | **新建**（Task 1） |
| `tests/test_cancel_terminal_state.py` | 233 行 9 个用例，全部围绕 `cancel_task` 终态守卫 | **整文件删除**（Task 6） |

---

### Task 1: 共享删除助手 `task_deletion.py`

**Files:**
- Create: `src/services/task_deletion.py`
- Test: `tests/test_task_deletion.py`

**Interfaces:**
- Produces:
  ```python
  def delete_task_row(
      *,
      manager,                      # 持有 _state_lock / active_tasks / stop_flags 的 manager
      task_id: int,
      table: str,                   # 'tasks' | 'dem_tasks' | 'contour_tasks' | 'local_terrain_tasks'
      artifact_dir: Optional[Path], # None = 调用方没要求删产物
      tombstone: Optional[set] = None,   # 只有 map 传（见 D-C）
      on_row_gone: Optional[Callable[[], None]] = None,  # 静态路由缓存失效，必须同步执行
  ) -> DeleteOutcome
  ```
- Produces: `DeleteOutcome` 具名元组 —— `row_deleted: bool`、`files_removed: Optional[bool]`、`files_deferred: bool`
- 后续 Task 全部依赖这两个名字，不要改。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_task_deletion.py`：

```python
"""删除任务的两条路径：没在跑就同步删，在跑就置停止标志 + 后台收尾。

砍掉「取消」之后，删除是唯一的销毁动作，必须任何状态都能点。而四条管线都有
一段分钟级的 GDAL 阻塞区（拼接 / warp / 建金字塔），中途打不断 —— 所以「在
HTTP 请求里等线程退出」不可行，只能行立即消失、产物后台收尾。
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


class _FakeManager:
    """只提供共享助手真正用到的三样东西。"""

    def __init__(self, thread=None):
        self._state_lock = threading.Lock()
        self.active_tasks = {}
        self.stop_flags = {}
        if thread is not None:
            self.active_tasks[1] = thread
            self.stop_flags[1] = threading.Event()


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    td = fresh_import(monkeypatch, "src.services.task_deletion")
    return db, td


def _seed(db, status="paused"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (id, name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_path, total_tiles) "
            "VALUES (1, 't', ?, 1, 0, 1, 0, 1, 1, 'satellite', ?, 1)",
            (status, "/tmp/x"),
        )
        conn.commit()
    finally:
        conn.close()


def _row_exists(db):
    conn = db.get_connection()
    try:
        return conn.execute("SELECT 1 FROM tasks WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def test_idle_task_deletes_synchronously_and_reports_files(monkeypatch, tmp_path):
    """快路径：没在跑的任务同步删行 + 同步删产物，files_removed 保持真实结果。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    out = td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                             artifact_dir=art)

    assert out.row_deleted is True
    assert out.files_deferred is False
    assert out.files_removed is True
    assert not art.exists(), "快路径必须当场把产物删掉"
    assert not _row_exists(db)


def test_idle_task_without_artifact_request_reports_none(monkeypatch, tmp_path):
    """artifact_dir=None 表示调用方没要求删产物 —— files_removed 必须是 None。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)

    out = td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                             artifact_dir=None)

    assert out.files_removed is None and out.files_deferred is False
    assert not _row_exists(db)


def test_running_task_returns_immediately_and_defers_files(monkeypatch, tmp_path):
    """后台路径：线程还活着时行当场消失、立即返回，产物进清单交后台。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)

    started = time.monotonic()
    out = td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                             artifact_dir=art)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"删除运行中任务不得阻塞等线程，实际 {elapsed:.1f}s"
    assert out.row_deleted is True
    assert out.files_deferred is True, "在跑的任务产物必须延后删"
    assert out.files_removed is None, "延后时不得给出 files_removed（还没删）"
    assert not _row_exists(db), "行必须当场消失"
    assert mgr.stop_flags[1].is_set(), "必须置停止标志，否则线程不会收工"

    # 产物线索必须落进清单 —— 进程被强杀时靠它补删
    conn = db.get_connection()
    try:
        rows = [r["path"] for r in conn.execute("SELECT path FROM pending_deletions")]
    finally:
        conn.close()
    assert rows == [str(art)]

    release.set()
    th.join(timeout=10)


def test_tombstone_receives_task_id_before_row_is_deleted(monkeypatch, tmp_path):
    """墓碑必须在删行【之前】写入，否则 map 的进度批次会撞外键。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)

    seen_when_row_gone = {}
    tomb = set()

    def on_row_gone():
        # 这个回调在删行之后同步执行 —— 此刻墓碑必须已经有它
        seen_when_row_gone["tombstoned"] = 1 in tomb

    td.delete_task_row(manager=mgr, task_id=1, table="tasks", artifact_dir=None,
                       tombstone=tomb, on_row_gone=on_row_gone)

    assert seen_when_row_gone["tombstoned"] is True
    release.set()
    th.join(timeout=10)


def test_on_row_gone_runs_synchronously_even_on_fast_path(monkeypatch, tmp_path):
    """静态路由缓存失效不能丢给后台：丢了的话已删任务的瓦片还能被访问到。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    calls = []

    td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                       artifact_dir=None, on_row_gone=lambda: calls.append(1))

    assert calls == [1]


def test_missing_row_reports_not_deleted(monkeypatch, tmp_path):
    """行本来就不在（并发双删）时如实返回 False，不抛。"""
    db, td = _setup(monkeypatch, tmp_path)

    out = td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                             artifact_dir=None)

    assert out.row_deleted is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/test_task_deletion.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.task_deletion'`

- [ ] **Step 3: 写共享助手**

创建 `src/services/task_deletion.py`：

```python
"""删除任务的共用实现 —— 四条管线（map / DEM / 等高线 / 本地地形）都走这里。

## 为什么删除要分两条路径

砍掉「取消」之后删除是唯一的销毁动作，必须任何状态都能点。但四条管线都有一段
分钟级到数十分钟的 GDAL 同步阻塞区（map 的单 zoom 拼接、等高线的 warp、地形的
多幅 DEM 合并 + 建金字塔），中途完全打不断 —— 让回调抛异常会让 GDAL 把产物删掉
并判整个作业失败，三处独立注释都实测记录过这个坑。

所以「在 HTTP 请求里 join 线程再删」不可行：请求要挂几十秒到几十分钟，Flask
worker 被占死，用户重复点击还会 double-delete。

分流：
  - **快路径**（任务没在跑）—— 同步删行 + 同步删产物。绝大多数删除走这条，
    `files_removed` 的既有语义（删没删成）原样保住。
  - **后台路径**（线程还活着）—— 置停止标志 → 写墓碑 → 同一事务里记
    pending_deletions + 删行 → 立即返回 → daemon 线程 join 完再删产物。
    用户视角是「点了就没了」，后台收尾不冒出来变成第六个状态。

## 为什么产物要先记 pending_deletions 再删行，且同一事务

反过来的话，进程在两者之间被强杀就丢了产物线索。清单的另一端是启动清扫
（task_cleanup._sweep_pending_deletions），它会在下次启动时补删。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from src.core.database import get_connection
from src.services.task_cleanup import remove_task_dir_if_safe

logger = logging.getLogger(__name__)

# 后台收尾线程等下载/切片线程退出的上限。超时不是错误：产物线索已经在
# pending_deletions 里，下次启动的清扫会接着删。这个上限只是防止 daemon 线程
# 无限挂着 —— GDAL 阻塞区没有可靠的时间上界，等不到就交给下次启动。
_JOIN_TIMEOUT_SECONDS = 600


class DeleteOutcome(NamedTuple):
    """删除结果。

    files_removed 三态：True=删掉了 / False=护栏拦下或删除出错 / None=没要求删，
    或者要求了但延后到后台（此时 files_deferred 为 True）。
    """
    row_deleted: bool
    files_removed: Optional[bool]
    files_deferred: bool


def _queue_pending_deletion(conn, artifact_dir: Path) -> None:
    """把产物目录记进待删清单。与删行在同一事务里，调用方负责 commit。

    INSERT OR IGNORE 配合 path 列的 UNIQUE 约束做幂等：重复入队没有意义。
    """
    conn.execute(
        "INSERT OR IGNORE INTO pending_deletions (path) VALUES (?)",
        (str(artifact_dir),),
    )


def _clear_pending_deletion(artifact_dir: Path) -> None:
    """产物删成功后销账。失败只记日志 —— 清单留着，下次启动补删。"""
    try:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_deletions WHERE path = ?",
                         (str(artifact_dir),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to clear pending deletion for {artifact_dir}: {e}")


def _background_cleanup(task_id: int, thread: threading.Thread,
                        artifact_dir: Optional[Path],
                        tombstone: Optional[set]) -> None:
    """等线程收工，然后删产物、销账、摘墓碑。全程 best-effort。"""
    try:
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            # 多半卡在 GDAL 阻塞区里。产物线索还在清单上，下次启动会补删。
            logger.warning(
                f"Task {task_id}: worker still running after "
                f"{_JOIN_TIMEOUT_SECONDS}s; leaving artifact cleanup to the "
                f"startup sweep")
            return
        if artifact_dir is not None:
            eligible = remove_task_dir_if_safe(artifact_dir)
            # 与启动补删同一判据：remove_task_dir_if_safe 返回的是「符合删除
            # 条件」而不是「真的删掉了」（它内部 rmtree(ignore_errors=True)，
            # Windows 上文件被占用会静默失败却仍返回 True）。目录还在就别销账。
            if eligible and not artifact_dir.exists():
                _clear_pending_deletion(artifact_dir)
            elif not eligible:
                # 越界路径永远删不掉，留在清单里只会每次启动重试并刷 warning
                _clear_pending_deletion(artifact_dir)
    except Exception as e:
        logger.warning(f"Background cleanup for task {task_id} failed: {e}")
    finally:
        if tombstone is not None:
            tombstone.discard(task_id)


def delete_task_row(
    *,
    manager,
    task_id: int,
    table: str,
    artifact_dir: Optional[Path],
    tombstone: Optional[set] = None,
    on_row_gone: Optional[Callable[[], None]] = None,
) -> DeleteOutcome:
    """删掉任务行，并按「线程还活着吗」分流产物清理。

    Args:
        manager: 持有 `_state_lock` / `active_tasks` / `stop_flags` 的任务管理器。
        table: 任务表名。调用方传字面量，不接受外部输入 —— 它直接进 SQL。
        artifact_dir: 产物目录；None 表示调用方没要求删产物。
        tombstone: 只有 map 传（见设计文档 D-C）。运行期有 INSERT 的管线才需要，
            用来让进度批次在父行消失后短路，避开外键 IntegrityError。
        on_row_gone: 行删掉后**同步**执行的回调，用于清静态路由的存在性缓存。
            不能丢给后台 —— 否则已删任务的瓦片在缓存失效前仍能被访问到。
    """
    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir)

    conn = get_connection()
    try:
        with manager._state_lock:
            thread = manager.active_tasks.get(task_id)
            running = bool(thread and thread.is_alive())

            if running:
                # 墓碑必须在删行【之前】写入：删行之后、线程发现之前的这段窗口里，
                # map 的进度批次会拿已经不存在的 task_id 去 INSERT task_tiles，
                # 撞上外键约束（实测 INSERT OR IGNORE 不豁免外键）。
                if tombstone is not None:
                    tombstone.add(task_id)
                flag = manager.stop_flags.get(task_id)
                if flag is not None:
                    flag.set()

            if artifact_dir is not None and running:
                # 先记清单再删行，同一事务 —— 中间崩掉就丢了产物线索
                _queue_pending_deletion(conn, artifact_dir)
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (task_id,))
            row_deleted = cur.rowcount > 0
            conn.commit()
    finally:
        conn.close()

    if on_row_gone is not None:
        try:
            on_row_gone()
        except Exception as e:
            logger.warning(f"Task {task_id}: on_row_gone hook failed: {e}")

    if not running:
        files_removed = None
        if artifact_dir is not None:
            files_removed = remove_task_dir_if_safe(artifact_dir)
        return DeleteOutcome(row_deleted, files_removed, False)

    threading.Thread(
        target=_background_cleanup,
        args=(task_id, thread, artifact_dir, tombstone),
        daemon=True,
        name=f"DeleteCleanup-{task_id}",
    ).start()
    return DeleteOutcome(row_deleted, None, artifact_dir is not None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/test_task_deletion.py -v`
Expected: 6 passed

- [ ] **Step 5: 反事实验证（必做）**

逐个植入下面三种坏实现，各跑一次 `tests/test_task_deletion.py`，确认它们**分别**被抓住，跑完立即还原（用 `git diff --stat` 确认还原干净）：

| 坏实现 | 应该红的用例 |
|---|---|
| `delete_task_row` 首行 `return DeleteOutcome(False, None, False)` | 除 `test_missing_row_reports_not_deleted` 外全红 |
| 墓碑写入挪到 `DELETE` 之后 | `test_tombstone_receives_task_id_before_row_is_deleted` |
| 后台路径改成同步 `thread.join()` 再删 | `test_running_task_returns_immediately_and_defers_files`（elapsed 断言） |

把三次的红输出贴进报告。**一个从没红过的测试不知道自己在守什么。**

- [ ] **Step 6: 跑全量并提交**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`
Expected: 1441 passed / 1 skipped（基线 1435 + 6）

```bash
git add src/services/task_deletion.py tests/test_task_deletion.py
git commit -m "feat: 共享删除助手 —— 没在跑就同步删，在跑就后台收尾"
```

---

### Task 2: map 的 `delete_task` 下沉 + 内存墓碑

**Files:**
- Modify: `src/services/task_manager.py`（新增 `delete_task`、`__init__` 加 `_deleting`、`_write_progress_batch` 加短路）
- Modify: `src/routes/api.py`（DELETE 端点改为调 manager；`_delete_payload` 加 `files_deferred`）
- Test: `tests/test_fix_map_delete_running.py`（新建）

**Interfaces:**
- Consumes: `task_deletion.delete_task_row(...)`、`DeleteOutcome`（Task 1）
- Produces: `TaskManager.delete_task(task_id, artifact_dir=None) -> DeleteOutcome`
- Produces: `_delete_payload(message, files_removed, files_deferred=False)` —— 第三参有默认值，另外三条路由不用改签名

**为什么 map 要下沉：** 四条管线里只有它把删除逻辑整段写在路由里（`api.py:340-420`），`TaskManager` 根本没有 `delete_task`。而墓碑集合必须住在 manager 里（`_write_progress_batch` 要查它），不下沉就得在路由里写第二套约定。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_fix_map_delete_running.py`：

```python
"""删除正在跑的 map 任务：行当场消失，进度批次不得撞外键。

砍掉「取消」后删除是唯一的销毁动作。map 是四条管线里唯一在运行期有 INSERT 的
（失败瓦片写 task_tiles），父行删掉后那条 INSERT 会抛
IntegrityError: FOREIGN KEY constraint failed —— 实测 INSERT OR IGNORE 不豁免
外键。异常被 _restore_progress_batch 退回队列再 re-raise，被 progress_callback
的 except 吞掉只记日志，于是 pending_tile_inserts 单调增长直到下载结束：大任务
上是几十万个 tuple 的内存泄漏，而且只在网络不好（有失败瓦片）时才发生。
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    tm_mod = fresh_import(monkeypatch, "src.services.task_manager")
    return db, tm_mod


def _seed(db, status="running"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (id, name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_path, total_tiles) "
            "VALUES (1, 't', ?, 1, 0, 1, 0, 1, 1, 'satellite', ?, 1)",
            (status, "/tmp/x"),
        )
        conn.commit()
    finally:
        conn.close()


def test_delete_running_map_task_succeeds_and_sets_stop_flag(monkeypatch, tmp_path):
    db, tm_mod = _setup(monkeypatch, tmp_path)
    mgr = tm_mod.TaskManager(socketio=None)
    _seed(db)

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    flag = threading.Event()
    with mgr._state_lock:
        mgr.active_tasks[1] = th
        mgr.stop_flags[1] = flag

    out = mgr.delete_task(1)

    assert out.row_deleted is True
    assert flag.is_set(), "删除必须自己把任务停下来 —— 用户不该先取消一次"
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT 1 FROM tasks WHERE id=1").fetchone() is None
    finally:
        conn.close()

    release.set()
    th.join(timeout=10)


def test_tombstoned_task_skips_tile_inserts(monkeypatch, tmp_path):
    """墓碑必须挡住 task_tiles 的 INSERT —— 这是墓碑存在的唯一理由。"""
    db, tm_mod = _setup(monkeypatch, tmp_path)
    mgr = tm_mod.TaskManager(socketio=None)
    _seed(db)

    # 父行删掉、task_id 进墓碑；此时再写失败瓦片必须被短路，而不是撞外键
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM tasks WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    mgr._deleting.add(1)

    # 不加短路的话这里抛 IntegrityError
    written = mgr._write_progress_batch_for_test(
        task_id=1, inserts=[(1, 5, 1, 1, "boom")])
    assert written is False, "墓碑命中时必须整批丢弃"
```

> **实现提示：** 上面用到的 `_write_progress_batch_for_test` 是本 Task 要新增的一个薄封装 —— `_write_progress_batch` 是 `_execute` 里的闭包，测试够不着。加一个模块级或方法级的可测入口，内部与闭包共用同一个短路判断；**不要**把闭包整个搬出来重构。

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/test_fix_map_delete_running.py -v`
Expected: FAIL — `AttributeError: 'TaskManager' object has no attribute 'delete_task'`

- [ ] **Step 3: 加墓碑集合与 `delete_task`**

`src/services/task_manager.py` 的 `__init__` 里，在 `stop_flags` 声明之后加：

```python
        # 已经被删除、但工作线程还没收工的 task_id。唯一用途见 _write_progress_batch：
        # map 是四条管线里唯一在运行期 INSERT 的（失败瓦片写 task_tiles），父行删掉
        # 后那条 INSERT 会撞外键 —— 实测 INSERT OR IGNORE 不豁免外键约束。
        # 另外三条管线运行期只有 UPDATE ... WHERE id=?，对不存在的行是静默 no-op，
        # 所以它们不需要墓碑，别为了对称加。
        self._deleting: set[int] = set()
```

新增方法（放在 `cancel_task` 原来的位置附近）：

```python
    def delete_task(self, task_id: int, artifact_dir=None):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        砍掉「取消」之后这是唯一的销毁动作，任何状态都能调 —— 不再有
        「Cannot delete running task. Please pause or cancel it first.」。
        """
        from src.services.task_deletion import delete_task_row
        from src.routes import tiles_static

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="tasks",
            artifact_dir=artifact_dir,
            tombstone=self._deleting,
            # 行已删：清掉 /tiles 静态路由的 output_path 缓存，否则
            # delete_files=false（磁盘瓦片保留）时已删任务的瓦片仍能被访问到
            on_row_gone=lambda: tiles_static.invalidate_output_path_cache(task_id),
        )
```

- [ ] **Step 4: 给 `_write_progress_batch` 加墓碑短路**

在 `_write_progress_batch` 的函数体最前面加：

```python
                if task_id in self._deleting:
                    # 任务已被删除，父行不在了。这批进度写不进去也不该写进去 ——
                    # 直接丢弃，不要走 _restore_progress_batch 退回队列（那会让
                    # pending_tile_inserts 单调增长到下载结束）。
                    return False
```

并让函数在正常路径末尾 `return True`。同时新增可测入口（`_write_progress_batch` 是闭包，测试够不着）：

```python
    def _write_progress_batch_for_test(self, task_id, inserts):
        """契约测试用的薄入口：只覆盖墓碑短路这一段判断。

        真正的 _write_progress_batch 是 _execute 里的闭包（它捕获 progress_conn
        等一堆局部状态），测试拿不到。这里复刻的只有短路判断本身 —— 短路逻辑
        必须与闭包里那份保持一致，改一处要改两处。
        """
        if task_id in self._deleting:
            return False
        return True
```

- [ ] **Step 5: 路由改为调 manager**

`src/routes/api.py` 的 DELETE 端点整段替换（去掉两处 `Cannot delete running task` 拒绝、去掉手写的锁与 SQL）：

```python
@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id: int):
    """
    Delete a task and its associated tiles

    Query Parameters:
        delete_files: Optional (1/true/yes). Also remove the task's on-disk
            artifact directory (output_path/task_<id>). Defaults to false.
            删除边界见 services/task_cleanup.remove_task_dir_if_safe。

    正在运行的任务**可以**直接删：删除会自己置停止标志、当场删行、把产物清理
    交给后台线程（见 services/task_deletion）。响应里的 files_deferred=true
    表示产物还没删完 —— 后台在等工作线程收工。
    """
    try:
        if not task_manager:
            return jsonify({'error': 'Task manager not initialized'}), 500

        conn = get_connection()
        try:
            row = conn.execute(
                'SELECT id, status, output_path FROM tasks WHERE id = ?',
                (task_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({'error': f'Task {task_id} not found'}), 404

        artifact_dir = None
        if request.args.get('delete_files', '').lower() in ('1', 'true', 'yes'):
            # 存量行的 output_path 可能是相对路径(旧版本只校验不改写)——先归一化
            # 成绝对路径;否则 Path.resolve() 按进程 CWD 解析,CWD≠BASE_DIR 时会
            # 误判成"越界"而拒删,接口却已经返回 success。
            artifact_dir = resolve_stored_output_dir(row['output_path']) / f"task_{task_id}"

        outcome = task_manager.delete_task(task_id, artifact_dir=artifact_dir)
        if not outcome.row_deleted:
            return jsonify({'error': f'Task {task_id} not found'}), 404

        logger.info(f"Task {task_id} deleted via API")
        return jsonify(_delete_payload(
            f'Task {task_id} deleted', outcome.files_removed,
            files_deferred=outcome.files_deferred))

    except Exception as e:
        logger.error(f"Error deleting task {task_id}: {e}")
        return jsonify({'error': 'Failed to delete task'}), 500
```

`_delete_payload` 加第三参：

```python
def _delete_payload(message: str, files_removed, files_deferred: bool = False):
    """DELETE 端点的统一响应体（M10）。

    `remove_task_dir_if_safe` 用返回值区分「已删」与「越界拒删」，但四个调用点
    此前全部丢弃它 —— 任何护栏命中都只写一条 warning，HTTP 仍是
    200 {"success": true}。用户点了「删除并删文件」，看到成功提示，几十 GB
    产物却纹丝不动（存量相对 output_path 尤其容易命中，见 M10）。

    files_removed 为 None 表示调用方没要求删文件，响应里就不带这两个字段。

    files_deferred=True 是删除**正在运行**的任务时的新形态：行已经没了，但产物
    要等工作线程收工才能删（四条管线都有分钟级的 GDAL 阻塞区，见 task_deletion）。
    此时 files_removed 必然是 None —— 还没删，给不出真假。
    """
    payload = {'success': True, 'message': message}
    if files_deferred:
        payload['files_deferred'] = True
        return payload
    if files_removed is not None:
        payload['files_removed'] = bool(files_removed)
        if not files_removed:
            payload['files_message'] = t('api.tasks.files_kept_unsafe_dir')
    return payload
```

- [ ] **Step 6: 跑测试**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/test_fix_map_delete_running.py -v`
Expected: 2 passed

- [ ] **Step 7: 跑全量，处理拒删断言翻面**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`

预期打红（**这些是语义翻面，改测试不改产品代码**）：
- `tests/test_fix_api_hardening.py:168-170` 与 `:185-187` —— 400→200、`is not None`→`is None`
  （对照组 `:193-199` 不动）

改完重跑。Expected: 1443 passed / 1 skipped

- [ ] **Step 8: 提交**

```bash
git add src/services/task_manager.py src/routes/api.py \
        tests/test_fix_map_delete_running.py tests/test_fix_api_hardening.py
git commit -m "feat(map): delete_task 下沉进 manager + 内存墓碑，运行中也能删"
```

---

### Task 3: 另外三条管线接入共享删除

**Files:**
- Modify: `src/services/dem_task_manager.py`、`src/services/contour_task_manager.py`、`src/services/local_terrain_task_manager.py`
- Modify: `src/routes/dem_api.py`、`src/routes/contour_api.py`、`src/routes/local_terrain_api.py`
- Test: `tests/test_fix_dem_delete_tiling_guard.py`、`tests/test_fix_contour_review.py`、`tests/test_local_terrain_api.py`（改现有断言）

**Interfaces:**
- Consumes: `delete_task_row(...)` / `DeleteOutcome`（Task 1）、`_delete_payload(..., files_deferred=)`（Task 2）
- Produces: 三个 manager 的 `delete_task` 统一返回 `DeleteOutcome`

**三条管线各自的差异（务必保留，见设计文档 D-B）：**

| 管线 | `artifact_dir` 谁算 | 算法 | `on_row_gone` |
|---|---|---|---|
| DEM | 路由层 | `resolve_stored_output_dir(output_path) / f"dem_task_{id}"` | `terrain_static.invalidate_dem_task(id)` |
| 等高线 | 路由层 | `resolve_stored_output_dir(output_path) / f"contour_task_{id}"` | `contour_static.invalidate_known_task(id)` |
| 本地地形 | **manager 内部** | `Config.DOWNLOADS_DIR / "terrain" / f"local_task_{id}"` —— **刻意不信库存 output_path**（冻结 exe 搬迁后旧绝对路径会让守卫失效） | `terrain_static.invalidate_known_task(id)` |

- [ ] **Step 1: 三个 manager 的 `delete_task` 换实现**

三处都改成调 `delete_task_row`，`tombstone` **一律不传**（见 D-C）。以 DEM 为例：

```python
    def delete_task(self, task_id: int, artifact_dir=None):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        切片线程自 v0.2.11 起也登记进 active_tasks / stop_flags，所以「切片中
        删除」走的是同一条后台路径，不再需要 dem_terrain_jobs 那道单独的守卫来
        拒绝 —— 但**那道守卫本身要留着**：进程重启后的孤儿 job 恢复靠它
        （见 _recover_orphan_running_tasks）。
        """
        from src.services.task_deletion import delete_task_row
        from src.routes import terrain_static

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="dem_tasks",
            artifact_dir=artifact_dir,
            on_row_gone=lambda: terrain_static.invalidate_dem_task(task_id),
        )
```

本地地形保留 `delete_files` 参数与默认值 True，产物路径在 manager 内算：

```python
    def delete_task(self, task_id: int, delete_files: bool = True):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        delete_files 默认 True 是本管线的历史约定（另外三条的路由默认 false）。
        前端总是显式传参，改默认值只影响直连 API 的人，不值得为对称制造破坏性变更。

        产物路径**不信库存 output_path**，从当前 Config.DOWNLOADS_DIR 重算（同
        terrain_static 的约定）：冻结 exe 搬迁后库存的旧绝对路径既不会让下面的
        护栏失效、也不会误删旧位置的目录。
        """
        from src.services.task_deletion import delete_task_row
        from src.routes import terrain_static

        artifact_dir = None
        if delete_files:
            artifact_dir = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="local_terrain_tasks",
            artifact_dir=artifact_dir,
            on_row_gone=lambda: terrain_static.invalidate_known_task(task_id),
        )
```

> **注意本地地形丢掉了一道护栏**：原实现有 `if task_root.resolve().parent != terrain_root: return False`（只允许删 `DOWNLOADS_DIR/terrain` 下的目录）。改用共享助手后走的是 `remove_task_dir_if_safe` 的通用护栏。**两道都要**：在算出 `artifact_dir` 之后、传给助手之前，保留那道 parent 检查，不通过就把 `artifact_dir` 置 None 并记 warning。

- [ ] **Step 2: 三条路由改为消费 `DeleteOutcome`**

去掉各自的 400 拒删分支，`_delete_payload` 带上 `files_deferred=outcome.files_deferred`。DEM 路由的 `except ValueError` 按文案分流 404/400 那段（`dem_api.py:112-115`）只保留 404 分支。

- [ ] **Step 3: 跑全量，翻面拒删断言**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`

预期打红（**全部是语义翻面，改测试不改产品代码**）：

| 文件:行 | 现在断言 | 改成 |
|---|---|---|
| `test_fix_dem_delete_tiling_guard.py:82-86, :100-102, :113-115` | `pytest.raises(ValueError)` + 行还在 | 删除成功、行消失 |
| `test_fix_dem_delete_tiling_guard.py:168-171, :180-183` | HTTP 400 + 行保留 | HTTP 200 + 行消失 |
| `test_fix_contour_review.py:279-283` | 400、GET 200 | 200、GET 404 |
| `test_local_terrain_api.py:370-372, :392-394` | `pytest.raises(ValueError)` | 删除成功、行消失 |

对照组 `test_fix_dem_delete_tiling_guard.py:118-128/:131-136/:186-191/:194-203` 与 `test_fix_contour_review.py:286-291` **保持不变**。

- [ ] **Step 4: 提交**

```bash
git add src/services/dem_task_manager.py src/services/contour_task_manager.py \
        src/services/local_terrain_task_manager.py \
        src/routes/dem_api.py src/routes/contour_api.py src/routes/local_terrain_api.py \
        tests/test_fix_dem_delete_tiling_guard.py tests/test_fix_contour_review.py \
        tests/test_local_terrain_api.py
git commit -m "feat: 另外三条管线接入共享删除，运行中也能删"
```

---

### Task 4: 删掉 `cancel_task` 与四条 `/cancel` 路由

**Files:**
- Modify: 四个 manager（删 `cancel_task`）、四个路由文件（删 `/cancel` 端点）、`src/models/task.py`
- Delete: `tests/test_cancel_terminal_state.py`（233 行 9 个用例，全部围绕 `cancel_task` 终态守卫）
- Modify: `tests/test_pipeline_endpoints.py`、`tests/test_task_lifecycle_state.py`、`tests/test_contour_api.py`、`tests/test_local_terrain_api.py`、`tests/test_fix_api_hardening.py`、`tests/test_orphan_recovery.py`、`tests/test_history_all_stream.py`、`tests/test_fix_dem_tiling_requires_completed.py`、`tests/test_fix_cache_chain.py`

- [ ] **Step 1: 删后端 cancel**

删除：
- `task_manager.py:639-694`、`dem_task_manager.py:256-274`、`contour_task_manager.py:643-661`、`local_terrain_task_manager.py:571-600` 四个 `cancel_task`
- `api.py:307-337`、`dem_api.py:163-174`、`contour_api.py:257-270`、`local_terrain_api.py:102-113` 四条路由
- `src/models/task.py:20` 的 `CANCELLED = "cancelled"`，以及 `:152` 六态注释里的 cancelled

- [ ] **Step 2: 三处状态守卫语义重推导**

这三处不是简单删字符串，要想清楚剩下的语义：

| 位置 | 现在 | 改成 | 为什么 |
|---|---|---|---|
| `task_manager.py:1576` | `if status in ('cancelled','paused'): return` | `if status == 'paused': return` | 收尾时不覆盖已暂停的任务 |
| `dem_task_manager.py:840` / `contour_task_manager.py:922` | 同上 | 同上 | 同上 |
| `task_manager.py:1689` | `WHERE ... status NOT IN ('cancelled','paused','completed')` | `NOT IN ('paused','completed')` | 失败兜底不得改写终态与已暂停 |
| `dem_task_manager.py:883` / `contour_task_manager.py:1109` | 同上 | 同上 | 同上 |
| `local_terrain_task_manager.py:492-502` | 切片中途停落 `status='cancelled'` + `_emit_tiling_finished(..., "cancelled")` | 整段收敛成裸 `return` | 置位的唯一入口变成删除，行已经不在，写什么都是静默 no-op |

`local_terrain_task_manager.py:584` 那条 `UPDATE ... SET status='cancelled' WHERE id=? AND status='pending'` 随 `cancel_task` 一起删。

- [ ] **Step 3: D3 —— map 的 `failed` 重启白名单删掉（两处，不是一处）**

`task_manager.py:420` 的状态门与紧跟其后的 `:432` `UPDATE ... WHERE id=? AND status IN ('pending','paused','failed')` **都要**去掉 `'failed'`。只改前者会让状态门放行、UPDATE 却匹配不到行，抛出「could not be started because its status changed」这种驴唇不对马嘴的错。

同时修正 `static/js/tasks.js:540-542` 那句过期注释（它说「三个 manager 都要求 pending/paused」，对 map 是错的 —— 改完之后才变成对的）。

- [ ] **Step 4: 删测试**

```bash
git rm tests/test_cancel_terminal_state.py
```

按 scout 清单改其余测试：

| 文件 | 改动 |
|---|---|
| `test_pipeline_endpoints.py` | `_PIPELINES:27-31` 四个元组各删 `"cancel"`（local_terrain 那行只剩空元组 → 整行删）；`:106-116`、`:119-136` 两个用例整删 |
| `test_task_lifecycle_state.py` | `:196-211`、`:214-232` 整删；`:549-592` 保留契约但把 `tm.cancel_task` 换成 `stop_flags[task_id].set()`、删末行 `== "cancelled"` |
| `test_contour_api.py` | `:178-183`、`:187-192` 整删 |
| `test_local_terrain_api.py` | `:229-250`、`:253-263` 整删 |
| `test_fix_api_hardening.py` | `:204-209`、`:224-225` 删 `POST .../cancel` 两行、函数改名 |
| `test_orphan_recovery.py` | `:96` 种子循环与 `:122-128` 期望列表同改（只改一处会红） |
| `test_history_all_stream.py` | `:126` 的 `_insert_map(cur,'m_cancelled','cancelled',...)` 删掉 |
| `test_fix_dem_tiling_requires_completed.py` | `:68` parametrize 删 `"cancelled"`（5→4 参） |
| `test_fix_cache_chain.py` | `:263` parametrize 删 `"cancelled"`（3→2 参），`:255` 活动态对照组不动 |
| `test_fix_cancel_progress_revive.py` | 四个用例无一调 `cancel_task`，**全绿不用改逻辑**；只改 `:10/:115/:200/:220` 的注释文案，并把文件改名为 `test_fix_stop_flag_progress_revive.py` |

- [ ] **Step 5: 改本 Task 触发的契约断言（4 处）**

删掉 `TaskStatus.CANCELLED` 之后，`test_tasks_js_contract.py` 里 **ast 解析枚举**的那部分
（`:805-820`）会自动跟进拿到 5 个值，但同文件的**手写表不会**。这 4 处由本 Task 触发，
就在本 Task 改掉 —— 每个 Task 都是一道 review 门禁，不能留着红给下一个 Task：

| 文件:行 | 现在 | 改成 |
|---|---|---|
| `test_tasks_js_contract.py:960` | `assert len(enum_values) == 6` | `== 5` |
| `test_tasks_js_contract.py:1124` | `_STATUS_LABEL_KEYWORD` 含 `'cancelled': '取消'` | 删该行 |
| `test_tasks_js_contract.py:1136` | `_STATUS_STROKE_TOKEN` 含 `'cancelled': '--color-neutral'` | 删该行 |
| `test_tasks_js_contract.py:1166` | `assert checked == 12` | `== 10` |

`_ACTION_GUARDS:450-451`（两颗按钮）与三条 dismiss 用例**不在本 Task** —— 它们由 Task 5
删按钮触发，留给 Task 5。本 Task 结束时它们仍是绿的（按钮还在）。

- [ ] **Step 6: 跑全量并提交**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`
Expected: 全绿。总数低于基线（删了 `test_cancel_terminal_state.py` 的 9 个用例与其余若干），
在报告里给出「基线 1435 − 删除数 + 新增数 = 实际」的算式。

```bash
git add -A
git commit -m "refactor: 删掉 cancel_task 与四条 /cancel 路由，cancelled 退出状态机"
```

---

### Task 5: 前端 —— 两个叉号下岗

**Files:**
- Modify: `static/js/task_list.js`、`static/js/tasks.js`、`static/js/history.js`
- Modify: `templates/_history_content.html`、`static/css/style.css`
- Modify: `src/i18n/catalog/js_tasks.py`、`src/i18n/catalog/js_history.py`、`src/i18n/catalog/tpl_history.py`

- [ ] **Step 1: 删两颗按钮**

`static/js/task_list.js` 的 `ROW_TEMPLATE`：
- 删 `:92-99`（cancelTask 按钮，`v-if` 含 `task.status !== 'failed'`）
- 删 `:115-122`（dismissTask 按钮，`v-if="task.status === 'failed'"`）
- `:108-114` 的删除按钮**保持无 `v-if`** —— 改造后它终于永远有效（此前运行中点它必然报错）
- 同步删 `methods` 里对应的转发

- [ ] **Step 2: 删动作函数与状态词表**

| 文件 | 删什么 |
|---|---|
| `tasks.js` | `cancelTask:726-751`、`dismissTask:543-549`、`getStatusColor:561` 的 `'cancelled': 'dark'`、`getStatusText:573` |
| `history.js` | `getStatusColor:306`、`getStatusStroke:347`、`getStatusText:368` 各一行 |

`history.js:615-674` 的 `deleteTask` **不动** —— 它已经在调 `closeFailureToast:646` 和 `TaskStore.remove:656`，正好覆盖了原「移除」按钮的两件事。三条 dismiss 契约测试迁移到它身上（Task 6）。

- [ ] **Step 3: 删 chip、CSS、i18n**

- `templates/_history_content.html:69` 的 `data-status="cancelled"` chip（五枚→四枚）
- `static/css/style.css:1051` 的 `.task-row.status-cancelled .task-dot` 规则与 `:1043` 注释里提到 cancelled 的那句
- i18n 八个键：`js_tasks.py` 的 `js.tasks.confirm.cancel_title`/`cancel_message`/`js.tasks.status.cancelled`/`js.tasks.toast.cancel_failed`；`js_history.py` 的 `js.history.action.cancel:93`/`dismiss_title:105`/`dismiss_label:109`；`tpl_history.py` 的 `tpl.history.filter.cancelled:68`。**中英两份都要删干净。**

- [ ] **Step 4: 改本 Task 触发的契约断言（9 处 + 3 条用例迁移）**

删两颗按钮、删状态词表条目、删 chip 会打红三个契约测试。这些**全部由本 Task 触发**，
就在本 Task 改掉。注意三个契约测试拿「所有状态」的路径完全不同：
`test_tasks_js_contract.py` ast 解析枚举（Task 4 已让它自动跟进），
`test_css_contract.py` **完全不解析枚举**（手写表 + grep `getStatusColor`），
`test_records_panel_structure.py` **纯硬编码**。

| 文件:行 | 改动 | 被什么触发 |
|---|---|---|
| `test_tasks_js_contract.py:450-451` | `_ACTION_GUARDS` 删 `'cancelTask'` / `'dismissTask'` 两个表项 | 删按钮 |
| `test_css_contract.py:4704` | `ICON_ONLY_BUTTON_COUNT = 20` → `18` | 删按钮（不改直接打红 `:4765`） |
| `test_css_contract.py:5576` | `_STATUS_SEMANTIC_TOKEN` 删 `'cancelled': None` | 删状态 |
| `test_css_contract.py:5636` | `assert len(checked) == 12` → `10` | 删状态 |
| `test_css_contract.py:5694` | `assert ... == 6` → `5` | 删状态 |
| `test_css_contract.py:5441` | 六色名集合去 `'dark'` | 删 `getStatusColor` 的 `'cancelled': 'dark'` |
| `test_css_contract.py:1139` | 自检去 `'dark'` | 同上 |
| `test_records_panel_structure.py:41` | `EXPECTED_CHIP_STATUSES` 删 `'cancelled'` | 删 chip |
| `test_records_panel_structure.py:109` + 文件头 `:6/:22` | 「五枚」→「四枚」 | 删 chip |

**三条 dismiss 用例要迁移而不是删**（`test_tasks_js_contract.py:495-509`、`:541-544`、
`:1630-1644`）：它们因 `_fn('dismissTask')` 找不到而红，但它们守的契约
——「失败行能被清掉 + 那条常驻失败 toast 一起关掉」——**没有消失，只是搬到了
`deleteTask` 身上**（`history.js:615-674` 已经在调 `closeFailureToast:646` 与
`TaskStore.remove:656`）。改成断言 `deleteTask` 里同时有这两个调用。直接删掉这三条
等于把一个仍然成立的契约丢了。

- [ ] **Step 5: 跑全量并提交**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`
Expected: 全绿。

```bash
git add -A
git commit -m "feat(ui): 删掉「取消」与「移除」两颗按钮，状态词表降到五态"
```

---

### Task 6: 补 i18n 反向检查（D-D）

**Files:**
- Modify: `tests/test_i18n.py`

**为什么单独一个 Task：** 契约测试的修改已经按「谁触发谁修」分给了 Task 4（状态相关 4 处）
和 Task 5（按钮/词表/chip 相关 9 处 + 3 条用例迁移）。这里只剩一件事，而它是**新增一张网**
而不是修补已有断言 —— 值得单独一道 review 门禁。

**这张网防什么：** `tests/test_i18n.py` 现有三层检查（`:28` 两语种齐全、`:38` 占位符、
`:48` 英文非中文）**全是对已有键做检查，没有任何 key↔引用 的双向闭合**。Task 5 删了 8 个
catalog 键：删键不会红，**漏删引用也不会红**（`:160` 只抓中文字面量）。没有这张网，前端会
在运行时把 `undefined` 显示给用户，而全套测试一片绿。

- [ ] **Step 1: 先读现有收集器**

读 `tests/test_i18n.py:1-60`，找到它现在是怎么把 `src/i18n/catalog/*.py` 的键收集起来的
（函数名/fixture 名）。下一步要复用它，**不要新造一份收集逻辑** —— 两份收集器迟早会漂。

- [ ] **Step 2: 写新检查（红）**

按上一步找到的真实收集器名替换下面的 `_all_keys()`：

```python
def test_every_referenced_key_exists_in_catalog():
    """前端引用的每个 t('...') 键都必须在 catalog 里。

    现有三层检查（两语种齐全 / 占位符 / 英文非中文）全是「对已有键做检查」，
    没有反向闭合。砍掉「取消任务」时删了 8 个 catalog 键 —— 漏删任何一处引用，
    用户看到的就是 undefined，而测试全绿。这条网就是为那次改动补的。
    """
    import re
    from pathlib import Path

    catalog_keys = set(_all_keys())          # ← 换成上一步找到的真实收集器
    pattern = re.compile(r"""t\(\s*['"]([a-z0-9_]+(?:\.[a-z0-9_]+)+)['"]""", re.I)
    missing = []
    for root in (Path("static/js"), Path("templates")):
        for path in root.rglob("*"):
            if path.suffix not in (".js", ".html") or "vendor" in path.parts:
                continue
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                if key not in catalog_keys:
                    missing.append(f"{path}: {key}")
    assert missing == [], "引用了不存在的 i18n 键:\n" + "\n".join(missing)
```

跑一次。**如果它直接绿**，说明 Task 5 把引用删干净了 —— 那也要继续做 Step 3，否则这条
测试从没红过，你不知道它在守什么。**如果它红**，红的就是 Task 5 漏删的引用，按提示删掉。

- [ ] **Step 3: 反事实验证这张网真的有网**

临时在 `static/js/tasks.js` 里塞一句 `t('js.nonexistent.key')`，跑新测试确认**红**、且错误
信息里点名了这个键和文件，然后删掉。把红输出原文贴进报告。

- [ ] **Step 4: 跑全量并提交**

Run: `/home/zhang/workspace/map-download/.venv/bin/python -m pytest tests/ -q`
Expected: 全绿。

```bash
git add tests/test_i18n.py
git commit -m "test: 补 i18n 反向检查 —— 引用了不存在的键必须红"
```

---

### Task 7: 文档

**Files:** `README.md`、`CLAUDE.md`、`RELEASE_NOTES.md`、`docs/superpowers/specs/2026-08-07-task-lifecycle-simplification-design.md`

- [ ] **Step 1: README**

| 行 | 改动 |
|---|---|
| `:39` | 功能列表「暂停 / 恢复 / 取消」→「暂停 / 恢复」 |
| `:224`、`:235`、`:245`、`:257` | 四条 `POST .../cancel` API 整行删除 |
| `:236`、`:258` | 「running 任务需先暂停或取消」→ 说明运行中可直接删、产物后台清理 |
| `:310` | 「任务取消约定」整条重写为「任务删除约定」 |

- [ ] **Step 2: CLAUDE.md**

`:89`（cancel/pause 机制描述）、`:95`（"Cancel never rewrites terminal states" 整条约定）、`:126`（提到 cancelled 任务保留镜像目录那句）三处重写。

- [ ] **Step 3: RELEASE_NOTES.md**

这是**破坏性变更**（四条公开 API 端点消失），单开一节写清楚：谁会受影响（直连 API 的脚本）、迁移方式（改调 `DELETE`）、以及用户能看到的变化（按钮从 7 个变 5 个、删除不用先取消）。

- [ ] **Step 4: 设计文档标记完成**

在「计划 A 执行完毕 —— 留给计划 B 的输入」之后补一节，记录计划 B 的执行结论，特别是 D-A 到 D-D 四个 spec 之外的决策。

- [ ] **Step 5: 跑全量并提交**

```bash
git add -A
git commit -m "docs: 任务生命周期简化的文档跟进"
```

---

## Self-Review

**1. Spec 覆盖：** 覆盖 spec 实现顺序的第 3-6 步全部内容，外加 spec 之外发现的四条（D-A 到 D-D）。spec 的第 1-2 步已由计划 A 完成并发版 v0.2.11。

**2. 占位符扫描：** 有两处**有意的**实现提示而非占位符——Task 2 的 `_write_progress_batch_for_test`（闭包不可测，给了明确的薄封装写法与「两处要同步改」的警告）、Task 6 的 `_all_keys()`（要求先读现有收集器名字，不要新造）。两处都写明了做法和理由，不是「TBD」。

**3. 类型一致性：** `DeleteOutcome(row_deleted, files_removed, files_deferred)` 三个字段名在 Task 1 定义、Task 2/3 消费、Task 2 的 `_delete_payload(message, files_removed, files_deferred=False)` 对齐；`delete_task_row` 的关键字参数名在四处调用点一致。

**4. 每个 Task 自成绿态（结构上已保证）：** 契约测试的 13 处硬编码按「谁触发谁修」分配 ——
Task 4 删枚举，就在 Task 4 改状态相关的 4 处；Task 5 删按钮/词表/chip，就在 Task 5 改
对应的 9 处 + 迁移 3 条 dismiss 用例。没有任何 Task 会把红留给下一个 Task。
（`test_tasks_js_contract.py` 用 ast 解析枚举那部分会自动跟进，另外两个契约测试**完全不
解析枚举**，全靠手写表 —— 这是必须逐处手改的原因。）

**5. 已知风险（实施时留意）：**
- 本地地形那道 `parent != terrain_root` 的额外护栏在 Task 3 里容易被顺手删掉 —— 它和
  `remove_task_dir_if_safe` 是两道，**都要**。
- 合并回 master 时与主仓未提交改动的重叠只有 `static/css/style.css`（改 1486 行 vs 本计划改 1043/1051）与 `README.md`（改 210/345 vs 本计划改 39/224-310），实测无冲突，但合并前要复查。
