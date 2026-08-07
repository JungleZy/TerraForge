# 删除机制前置改动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 DEM 地形切片能被中途停止，并建立「产物延后删除」的持久化清单——这是「砍掉取消任务」的两个前置条件，各自独立可交付。

**Architecture:** Task 1 只补 `dem_task_manager` 到 `tile_dem_task_dir` 的 stop_flag 接线（字段、透传、检查点全都是现成的，缺的只有调用方）。Task 2 新增 `pending_deletions` 表和启动补删，为后续「删除任务时行立即消失、产物后台收尾」提供落点。两个 Task 都**不改变任何用户可见行为**，可以独立合入。

**Tech Stack:** Python 3.12 / SQLite（`PRAGMA foreign_keys = ON` + WAL）/ threading.Event 协作停止 / pytest

**上游设计：** `docs/superpowers/specs/2026-08-07-task-lifecycle-simplification-design.md`（「前置改动」与 D2 两节）

## Global Constraints

- 注释与文档一律中文，风格是**解释为什么，不解释是什么**——照抄仓库现有注释的密度。
- 测试文件命名 `tests/test_fix_<topic>.py`，模块 docstring 说明这条测试守的是什么契约。
- 不跑格式化工具、不跑 lint、不做无关重构。
- 本计划在隔离 worktree `../map-download-wt/dem-stop-flag`（分支 `dem-stop-flag`，基于 `f72a484`）里执行。
- 解释器用主仓的虚拟环境：**`/home/zhang/workspace/map-download/.venv/bin/python`**。worktree 下没有
  `.venv`，直接敲 `python` 多半不是 3.12.3 那个带 GDAL 的环境。测试靠 `sys.path.insert` 定位
  `src/`，所以会正确加载 worktree 这一份代码。
- 每个 Task 结束时跑一次全量测试，**本 worktree 的干净基线是 1423 passed / 1 skipped**（已实测）。
  注意别拿主仓的数字对照：主仓工作区有一批未提交改动带进了额外 44 个用例，那不是本分支的基线。
- `remove_task_dir_if_safe` 的返回值语义是「**是否符合删除条件**」，不是「是否真的删掉了」——它内部用 `shutil.rmtree(target, ignore_errors=True)`（`task_cleanup.py:194`），Windows 上文件被占用会静默失败却仍返回 `True`。任何依赖它的逻辑都必须自己再查一次 `target.exists()`。
- **不要碰 `src/services/download_engine.py` 里的 `'cancelled'`**——那是瓦片级结果状态（`DownloadCancelled` 异常），与任务状态无关。

---

## File Structure

| 文件 | 责任 | 本计划中的角色 |
|---|---|---|
| `src/services/dem_task_manager.py` | DEM 下载 + 切片作业的线程与状态机 | Task 1 修改：切片线程的 stop_flag 接线（6 处） |
| `src/services/terrain_tiling/dem_task_tiler.py` | `TileParams` 与 `tile_dem_task_dir` | Task 1 **只读**——`stop_flag` 字段（`:55`）与透传（`:136`）已就位，不改 |
| `src/core/database.py` | 建表与连接 | Task 2 修改：新增 `pending_deletions` 表 |
| `src/services/task_cleanup.py` | 启动残留清扫 + 产物删除护栏 | Task 2 修改：新增第 7 类清扫 `_sweep_pending_deletions` |
| `tests/test_fix_dem_tiling_stoppable.py` | Task 1 的契约 | 新建 |
| `tests/test_fix_pending_deletions.py` | Task 2 的契约 | 新建 |

---

### Task 1: DEM 地形切片可中途停止

**Files:**
- Modify: `src/services/dem_task_manager.py:376-394`（起线程）、`:412`（`_run_tiling_job` 签名）、`:504-510`（`TileParams`）、`:519-524`（`rendered==0` 判定）、`:530-540`（收尾）、`_run_tiling_job` 末尾（新增 `finally`）
- Test: `tests/test_fix_dem_tiling_stoppable.py`

**Interfaces:**
- Consumes: `TileParams(maxzoom, parent_url, progress_cb, stage_cb, stop_flag)` — `stop_flag: Optional[threading.Event]`，定义在 `dem_task_tiler.py:55`，已由 `tile_dem_task_dir` 透传给 `build_terrain`
- Produces: `DemTaskManager.stop_flags[task_id]` 与 `DemTaskManager.active_tasks[task_id]` 在切片期间**有登记**（此前只有下载期间有）。后续计划的 `delete_task` 依赖这两个字典能看见切片线程。`_run_tiling_job` 新签名：
  `_run_tiling_job(self, task_id: int, task_dir: Path, output_dir: Path, maxzoom: int, parent_url: str, stop_flag: Optional[threading.Event] = None) -> None`

**为什么这个 Task 独立有价值：** DEM 切片目前**完全停不下来**——线程既不登记 `active_tasks` 也收不到 stop_flag，进程只能靠杀掉来中断。`dem_task_manager.py:388-390` 的注释早就写明了这个坑但一直没修。即使最后决定不砍「取消」，这一步也该做。

- [ ] **Step 1: 写失败测试——切片线程必须拿到 stop_flag 且登记进 active_tasks**

创建 `tests/test_fix_dem_tiling_stoppable.py`：

```python
"""DEM 地形切片必须能被中途停止。

TileParams.stop_flag 字段（dem_task_tiler.py:55）、tile_dem_task_dir 的透传
（:136）、build_terrain 的逐瓦片检查（cesiumlab_terrain.py:1427/1446）全都是
现成的 —— 缺的一直是 dem_task_manager 这个调用方。后果不止「停不下来」：切片
线程不进 active_tasks，delete_task 的 is_alive() 守卫对它完全无效。
"""

import importlib
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
    return db, dtm


def _seed_completed_dem_task(db, output_path):
    """切片只接受 completed 的下载任务（M16），所以种子行必须是 completed。"""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 1, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_tiling_thread_receives_stop_flag_and_is_registered(monkeypatch, tmp_path):
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        seen["stop_flag"] = params.stop_flag
        entered.set()
        release.wait(timeout=10)
        return {"total": 4, "rendered": 4, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    assert entered.wait(timeout=10), "切片线程没跑起来"

    # 线程还卡在 fake_tiler 里 —— 此刻两个登记表都必须看得见它
    assert isinstance(seen["stop_flag"], threading.Event), (
        f"TileParams.stop_flag 必须是 Event，实际 {seen['stop_flag']!r}")
    assert task_id in mgr.stop_flags, "切片的 stop_flag 必须登记，否则没人能置位"
    th = mgr.active_tasks.get(task_id)
    assert th is not None and th.is_alive(), (
        "切片线程必须登记进 active_tasks，否则 delete_task 的 is_alive() 守卫形同虚设")

    release.set()
    th.join(timeout=10)
    assert not th.is_alive()

    # 收尾后两个登记表都要清干净，别把 key 泄漏给下一轮
    assert task_id not in mgr.active_tasks
    assert task_id not in mgr.stop_flags
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_fix_dem_tiling_stoppable.py -v`
Expected: FAIL — `seen["stop_flag"]` 是 `None`（`TileParams` 没传），且 `task_id not in mgr.stop_flags`

- [ ] **Step 3: 接线（起线程 + 签名 + TileParams + finally）**

`src/services/dem_task_manager.py`，把 `:376-394` 的整段替换为：

```python
        # 切片线程与下载线程共用 stop_flags / active_tasks 两张表：切片只在
        # 下载已 completed 之后才启动，同一 task_id 不会有两个线程并存。
        # 登记进 active_tasks 是 delete_task 的 is_alive() 守卫能看见它的前提。
        stop_flag = threading.Event()
        with self._state_lock:
            self.stop_flags[task_id] = stop_flag
            th = threading.Thread(
                target=self._run_tiling_job,
                args=(task_id, task_dir, output_dir, maxzoom, parent_url, stop_flag),
                daemon=True,
                name=f"DemTiling-{task_id}",
            )
            self.active_tasks[task_id] = th
        try:
            th.start()
        except Exception as e:
            # L2: 上面已把 job 行 upsert 成 running 并 commit。线程创建失败
            # (RuntimeError: can't start new thread)后不回补的话,job 行永久停在
            # running:再次 start_tiling 被 `WHERE status != 'running'` 判为「已在
            # 运行」而 ValueError,delete_task 也被 DB 状态检查挡住,而
            # src/routes/terrain_api.py 没有任何 cancel/reset job 的端点 ——
            # 只能重启进程让孤儿恢复解开。
            # job 行没有 paused 态,这里置 failed(与下载管线回退 paused 不同)。
            with self._state_lock:
                if self.active_tasks.get(task_id) is th:
                    self.active_tasks.pop(task_id, None)
                if self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            self._mark_tiling_job_failed(
                task_id, f"tiling thread failed to start: {e}")
            raise
```

把 `:412` 的签名改为：

```python
    def _run_tiling_job(self, task_id: int, task_dir: Path, output_dir: Path,
                        maxzoom: int, parent_url: str,
                        stop_flag: Optional[threading.Event] = None) -> None:
```

把 `:504-510` 的 `tile_dem_task_dir` 调用改为：

```python
                counts = tile_dem_task_dir(
                    task_dir=task_dir,
                    out_dir=output_dir,
                    params=TileParams(maxzoom=maxzoom, parent_url=parent_url,
                                      progress_cb=tiling_progress,
                                      stage_cb=tiling_stage,
                                      stop_flag=stop_flag),
                ) or {}
```

在 `_run_tiling_job` 现有 `except Exception as e:` 块（`:542-554`）之后追加 `finally`（范本 `_run_task:665-670`）：

```python
        finally:
            # 与 _run_task 同一约定：只在自己就是登记的那个线程/flag 时才摘，
            # 否则会把下一轮 start_tiling 刚放进去的登记误删。
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_fix_dem_tiling_stoppable.py -v`
Expected: PASS

- [ ] **Step 5: 写第二个失败测试——切片刚开始就被停时不得判 failed**

追加到 `tests/test_fix_dem_tiling_stoppable.py`：

```python
def test_stopped_tiling_with_zero_rendered_is_not_a_failure(monkeypatch, tmp_path):
    """刚开始切就被停掉时 rendered 本来就是 0 —— 不能误判成「一张瓦片都没切出来」。

    dem_task_manager 的 `if total > 0 and rendered == 0: raise` 是给「切片器真的
    什么都没产出」准备的失败判据。中途停止会让它误命中：作业被记 failed、
    error_message 写成 "produced no tiles"，而真实原因是用户主动停的。
    local_terrain_task_manager.py:482 早就有逐字对应的豁免。
    """
    db, dtm = _setup(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        # 模拟 build_terrain 看到 stop_flag 立刻收工：一张都没渲染
        params.stop_flag.set()
        return {"total": 100, "rendered": 0, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th is not None:
        th.join(timeout=10)

    job = mgr.get_tiling_job(task_id)
    assert job["status"] != "failed", (
        f"中途停止不是失败，实际 status={job['status']} error={job['error_message']}")
    # 中途停止也不能报 completed —— 产物是残缺的
    assert job["status"] != "completed", (
        "中途停止的切片不能记 completed（产物残缺）")


def test_stopped_tiling_writes_no_terminal_state(monkeypatch, tmp_path):
    """中途停止时不写状态、不广播 —— 置位的唯一入口是删除，行已经不在了。

    DEM 切片没有暂停语义。改造后能置这个 flag 的只有「删除任务」，那时
    dem_tasks 行连同 CASCADE 的 dem_terrain_jobs 行都已经没了：UPDATE 是静默
    no-op，emit 也没有行可更新。写死这条契约，免得后来者「补上遗漏的状态迁移」。
    """
    db, dtm = _setup(monkeypatch, tmp_path)

    emitted = []

    class _Sock:
        def emit(self, event, payload=None):
            emitted.append((event, payload))

    mgr = dtm.DemTaskManager(socketio=_Sock())
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        params.stop_flag.set()
        return {"total": 10, "rendered": 3, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th is not None:
        th.join(timeout=10)

    job = mgr.get_tiling_job(task_id)
    assert job["status"] == "running", (
        f"中途停止不该改写 job 状态，实际 {job['status']}")
    assert job["completed_at"] is None
    finished = [p for e, p in emitted
                if e == "terrain_job_progress" and p.get("status") in ("completed", "failed")]
    assert finished == [], f"中途停止不该广播终态，实际 {finished}"
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m pytest tests/test_fix_dem_tiling_stoppable.py -v`
Expected: 前一个用例 PASS；两个新用例 FAIL —— 第一个因为 `rendered==0` 触发 `RuntimeError` 判 failed，第二个因为收尾无条件写 `completed`

- [ ] **Step 7: 加 stop 豁免与收尾短路**

`src/services/dem_task_manager.py`，把 `:519-528` 的计数判定段改为：

```python
            rendered = int(counts.get("rendered", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            total = int(counts.get("total", 0) or 0)
            stopped = stop_flag is not None and stop_flag.is_set()
            # 中途停止时 rendered 可以合法地是 0（刚进瓦片循环就被叫停），
            # 不豁免的话会被下面这条「切片器什么都没产出」的失败判据误命中。
            # 范本逐字对照 local_terrain_task_manager.py:482。
            if total > 0 and rendered == 0 and not stopped:
                raise RuntimeError(
                    f"terrain tiling produced no tiles ({failed}/{total} failed)")
            warning = None
            if failed > 0:
                warning = f"部分地形瓦片切片失败({failed}/{total})"
                logger.warning(f"DEM tiling job {task_id}: {warning}")

            if stopped:
                # 中途停止的唯一入口是删除任务（DEM 切片没有暂停/恢复语义）——
                # dem_tasks 行连同 CASCADE 的 job 行此刻都已经不在了。写状态是
                # 静默 no-op，_emit_tiling_finished 也没有行可更新。直接收工，
                # 不是漏了状态迁移。
                return
```

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m pytest tests/test_fix_dem_tiling_stoppable.py -v`
Expected: 3 passed

- [ ] **Step 9: 修一处必定打红的测试替身**

`tests/test_fix_dem_tiling_requires_completed.py:87-90` 现在是：

```python
    monkeypatch.setattr(
        dtm.DemTaskManager, "_run_tiling_job",
        lambda self, task_id, task_dir, output_dir, maxzoom, parent_url: None,
    )
```

`_run_tiling_job` 多了 `stop_flag` 位置参数，这个替身**一定**会抛
`TypeError: <lambda>() takes 6 positional arguments but 7 were given`。改成：

```python
    monkeypatch.setattr(
        dtm.DemTaskManager, "_run_tiling_job",
        lambda self, *args, **kwargs: None,
    )
```

用 `*args` 而不是补一个具名参数：这个替身只是让线程立刻返回，它不关心任何参数，
钉死形参个数只会让下一次签名变更再打红一遍。

- [ ] **Step 10: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全绿。若 `tests/test_fix_dem_start_tiling_race.py`（并发 start_tiling 只有一个赢）打红，
说明锁内登记的顺序写错了——`stop_flags` / `active_tasks` 的写入必须和 `th` 的创建在**同一个**
`with self._state_lock` 块里，改产品代码不改测试。

- [ ] **Step 11: 提交**

```bash
git add src/services/dem_task_manager.py \
        tests/test_fix_dem_tiling_stoppable.py \
        tests/test_fix_dem_tiling_requires_completed.py
git commit -m "fix(dem): 地形切片接上 stop_flag，切片线程登记进 active_tasks"
```

---

### Task 2: 待删产物清单与启动补删

**Files:**
- Modify: `src/core/database.py`（`dem_terrain_jobs` 建表段之后，`:546` 附近插入新表）
- Modify: `src/services/task_cleanup.py`（新增 `_sweep_pending_deletions`；`sweep_startup_residue` 的 docstring、`removed` 字典、调用、日志各加一处）
- Test: `tests/test_fix_pending_deletions.py`

**Interfaces:**
- Produces: 表 `pending_deletions(id INTEGER PK AUTOINCREMENT, path TEXT NOT NULL UNIQUE, created_at TIMESTAMP)`。后续计划的 `delete_task` 在删任务行的**同一事务**里 `INSERT OR IGNORE INTO pending_deletions (path) VALUES (?)`，后台清理线程删成功后 `DELETE FROM pending_deletions WHERE path=?`。
- Produces: `task_cleanup._sweep_pending_deletions() -> int`，由 `sweep_startup_residue()` 调用，返回本次真正删掉的目录数。

**为什么没有外键：** 任务行先删、清单行后删，反过来关联就悬空了。这张表的存在意义恰恰是「任务已经不在了，但产物还在」。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_fix_pending_deletions.py`：

```python
"""待删产物清单：进程被强杀后，下次启动必须把没删完的任务产物补删掉。

删除任务时行立即消失、产物在后台收尾（见任务生命周期简化设计 D2）。后台线程
可能卡在一段分钟级的 GDAL 阻塞区上，这期间用户关掉程序，那个目录就没人管了。
pending_deletions 是这条承诺的兜底：删任务行的同一事务里先记一行，后台删成功
后清行，进程被强杀时残留的行由启动清扫补删。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    for mod in ("app", "src.core.database", "src.services.task_cleanup"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    cleanup = importlib.import_module("src.services.task_cleanup")
    return db, cleanup


def _queue(db, path):
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO pending_deletions (path) VALUES (?)", (str(path),))
        conn.commit()
    finally:
        conn.close()


def _rows(db):
    conn = db.get_connection()
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM pending_deletions")]
    finally:
        conn.close()


def test_pending_deletion_is_swept_and_row_cleared(monkeypatch, tmp_path):
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_9"
    target.mkdir(parents=True)
    (target / "a.tif").write_bytes(b"x")
    _queue(db, target)

    removed = cleanup._sweep_pending_deletions()

    assert removed == 1
    assert not target.exists(), "清单里的目录必须被删掉"
    assert _rows(db) == [], "删成功后清单行必须清掉"


def test_out_of_bounds_path_is_dropped_not_retried_forever(monkeypatch, tmp_path):
    """越界路径永远删不掉，留在清单里只会每次启动重试一遍并刷 warning。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    # DOWNLOADS_DIR 本身 —— remove_task_dir_if_safe 明确拒绝（:185-187）
    downloads_root = tmp_path / "downloads"
    _queue(db, downloads_root)

    removed = cleanup._sweep_pending_deletions()

    assert removed == 0, "越界拒删不算删除数"
    assert downloads_root.exists(), "护栏必须挡住 DOWNLOADS_DIR 本身"
    assert _rows(db) == [], "越界路径要从清单里丢弃，不能无限重试"


def test_row_survives_when_directory_could_not_be_removed(monkeypatch, tmp_path):
    """rmtree 用的是 ignore_errors=True —— 删不掉也返回 True。

    Windows 上文件被占用就是这种情况：只看返回值会把没删干净的目录从清单里
    抹掉，那正是这张表要防的事。目录还在就必须留着行，下次启动再试。
    """
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_8"
    target.mkdir(parents=True)
    _queue(db, target)

    # 模拟「符合删除条件、但实际没删掉」
    monkeypatch.setattr(cleanup, "remove_task_dir_if_safe", lambda p: True)

    removed = cleanup._sweep_pending_deletions()

    assert removed == 0
    assert _rows(db) == [str(target)], "目录还在时清单行必须保留"


def test_missing_directory_clears_the_row(monkeypatch, tmp_path):
    """目录早就没了（用户手工删过）也要清行，别让清单无限增长。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_7"
    _queue(db, target)

    removed = cleanup._sweep_pending_deletions()

    assert _rows(db) == []
    assert removed == 1


def test_sweep_startup_residue_runs_pending_deletions(monkeypatch, tmp_path):
    """补删必须真的挂在启动清扫上，不是一个没人调的函数。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_6"
    target.mkdir(parents=True)
    _queue(db, target)

    cleanup.sweep_startup_residue()

    assert not target.exists()
    assert _rows(db) == []


def test_sweep_never_raises_when_table_is_missing(monkeypatch, tmp_path):
    """老库没有这张表 —— 清扫是 best-effort，绝不能因此让启动失败。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        conn.execute("DROP TABLE pending_deletions")
        conn.commit()
    finally:
        conn.close()

    assert cleanup._sweep_pending_deletions() == 0
    cleanup.sweep_startup_residue()   # 不得抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_fix_pending_deletions.py -v`
Expected: FAIL — `no such table: pending_deletions` / `AttributeError: module has no attribute '_sweep_pending_deletions'`

- [ ] **Step 3: 建表**

`src/core/database.py`，在 `dem_terrain_jobs` 的索引创建之后（`:546` 那行空行处）插入：

```python
        # 待删产物清单。删除任务时若用户选了「同时删除磁盘产物」，先在这里记
        # 一行、再删任务行（同一事务）；后台清理线程删成功后清掉该行。进程被
        # 强杀（SIGKILL / 关窗）时行会留下来，由启动清扫补删
        # （task_cleanup._sweep_pending_deletions）。
        #
        # 刻意没有外键：任务行先删、这行后删，反过来关联就悬空了 —— 这张表存在
        # 的意义恰恰是「任务已经不在了，但产物还在」。
        # path UNIQUE：同一目录重复入队没有意义，用 INSERT OR IGNORE 幂等。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
```

- [ ] **Step 4: 写补删函数**

`src/services/task_cleanup.py`，在 `_sweep_cache_part_files` 之后（`:413` 的空行处）插入：

```python
def _sweep_pending_deletions() -> int:
    """补删 pending_deletions 里残留的任务产物目录，返回真正删掉的个数。

    与前六类不同，这一类的线索来自 DB 而不是文件名模式：删除任务时先记清单再
    删任务行（同一事务），后台线程删成功后清行。进程被强杀时行会留下来。

    三种结局，**不能只看 remove_task_dir_if_safe 的返回值**：
      - 返回 False（越界）→ 清行。它永远不会被删掉，留着只会每次启动重试一遍
        并刷一条 warning。
      - 返回 True 且目录确实没了 → 清行，计入删除数。
      - 返回 True 但目录还在 → **保留行**。那个函数用的是
        `rmtree(..., ignore_errors=True)`（见 :194），Windows 上文件被占用会
        静默失败却仍然返回 True —— 只看返回值就会把没删干净的目录从清单里
        抹掉，那正是这张表要防的事。

    表不存在（老库、迁移中）时返回 0，不抛 —— 启动清扫全程 best-effort。
    """
    from src.core.database import get_connection_context

    removed = 0
    try:
        with get_connection_context() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, path FROM pending_deletions").fetchall()
            except Exception as e:
                logger.warning(
                    f"Pending-deletion sweep: table unavailable (ignored): {e}")
                return 0
            for row in rows:
                target = Path(row["path"])
                eligible = remove_task_dir_if_safe(target)
                if eligible and target.exists():
                    # 没删干净（占用中）—— 留着行，下次启动再试
                    continue
                conn.execute(
                    "DELETE FROM pending_deletions WHERE id = ?", (row["id"],))
                if eligible:
                    removed += 1
            conn.commit()
    except Exception as e:
        logger.warning(f"Pending-deletion sweep failed (ignored): {e}")
        return removed
    return removed
```

- [ ] **Step 5: 挂到启动清扫上**

`src/services/task_cleanup.py` 的 `sweep_startup_residue`，四处改动：

docstring 第一行「启动一次性清扫六类」改为「七类」，并在第 6 条之后追加：

```
    7. 上次进程没删完的任务产物目录（pending_deletions 表）—— 唯一一类线索来自
       DB 而不是文件名模式的残留，见 _sweep_pending_deletions。
```

`removed` 字典初始化加一个键：

```python
    removed = {"stitch": 0, "warp": 0, "upload": 0, "part": 0, "materialised": 0,
               "base_unpack": 0, "pending": 0}
```

在第 5 类「物化栅格」的 `try` 块之后（`:502` 的 `logger.warning(f"Materialised-raster sweep failed...` 那个 except 之后）追加：

```python
    # 第 7 类：上次进程没删完的任务产物目录。与第 5 类同理单独套一层 try ——
    # 它要查 DB，不该因为数据库暂时不可用就把已统计的清扫结果丢掉。
    removed["pending"] += _sweep_pending_deletions()
```

日志行补上新分类：

```python
            f"upload tmp={removed['upload']}, cache .part={removed['part']}, "
            f"materialised raster={removed['materialised']}, "
            f"base unpack tmp={removed['base_unpack']}, "
            f"pending deletions={removed['pending']}"
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_fix_pending_deletions.py -v`
Expected: 6 passed

- [ ] **Step 7: 跑全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全绿。重点关注 `tests/test_startup_residue_sweep.py` —— 它断言了 `sweep_startup_residue` 的行为与日志，新增分类可能需要同步更新其中的期望。

- [ ] **Step 8: 提交**

```bash
git add src/core/database.py src/services/task_cleanup.py tests/test_fix_pending_deletions.py
git commit -m "feat: 待删产物清单 + 启动补删，为异步删除兜底"
```

---

## Self-Review

**1. Spec 覆盖：** 本计划对应 spec 的「前置改动：DEM 地形切片必须先能停」全部六处（Task 1）与 D2「产物删不掉时——记进待删清单，下次启动补删」（Task 2）。spec 的其余部分（内存墓碑、`delete_task` 下沉、删 `cancel_task`、前端、文档）属于计划 B，本计划**有意不覆盖**。

**2. 占位符扫描：** 无 TBD / TODO / 「类似 Task N」。每个代码步骤都给了可直接粘贴的完整代码块。

**3. 类型一致性：** `stop_flag` 在 Task 1 的三处（起线程、`_run_tiling_job` 签名、`TileParams`）都是 `threading.Event`；`_sweep_pending_deletions() -> int` 与 `removed["pending"] += ...` 的用法一致；`remove_task_dir_if_safe(path) -> bool` 的语义在 Task 2 的注释与测试里保持同一口径（「是否符合删除条件」而非「是否真的删掉」）。

**4. 已知的连带风险（实施时留意，不是缺口）：**
- `dem_task_manager` 需要确认已 import `threading` 与 `Optional`——两者在文件头部（`:11` / `:14`）都在。
- `tests/test_fix_dem_tiling_requires_completed.py:87-90` 的 `_run_tiling_job` 替身**必定打红**（已核实是六参 lambda），Step 9 给了确切改法。
- `tests/test_startup_residue_sweep.py` 断言了清扫的返回统计，Task 2 新增分类后可能需要同步。

---

## 计划 B（后续，本计划验证后再写）

砍掉「取消」本体，对应 spec 的实现顺序 3-6：`TaskManager.delete_task` 下沉 + 内存墓碑、四条管线的墓碑式删除、删 `cancel_task` 与四条 `/cancel` 路由 + 三处状态守卫语义重推导、前端按钮与状态词表、文档与契约测试。

**为什么分开：** 本计划的两个 Task 不改变任何用户可见行为，可以独立合入并在真实使用中验证；计划 B 强依赖 Task 1 的 stop_flag 接线（否则「删除自己会停任务」在 DEM 切片上是谎言），且写 B 时应该拿到 A 的实测结果——尤其是「置位到切片线程真正退出要多久」这个只能实测得到的数字。
