# 本地高程切片（上传 GeoTIFF 后切片）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在首页上传多个 GeoTIFF（`.tif/.tiff`），后端保存到独立任务目录后自动调用现有 terrain tiler 生成 Cesium quantized-mesh 瓦片。

**Architecture:** 新增一条独立任务线（`local_terrain_tasks` / `local_terrain_files` 表 + `LocalTerrainTaskManager` + `routes/local_terrain_api.py`），不复用、不污染现有 NASA DEM 下载任务。切片复用现有 `services/terrain_tiling/dem_task_tiler.py::tile_dem_task_dir`（它从目录读取 `*_dem.tif` 传给 `build_terrain`），上传文件规范化保存为 `upload_<n>_dem.tif` 即可识别。静态服务从 DB 的 `output_dir` 读路径（不写死），复用现有 `_resolve_safe_file` 安全校验。

**Tech Stack:** Flask + Flask-SocketIO, SQLite (`sqlite3.Row`), threading 后台任务, pytest, 原生 JS + Bootstrap 前端。

**设计依据：** `docs/superpowers/specs/2026-06-12-local-terrain-upload-tiling-design.md`

---

## File Structure

新增：
- `services/local_terrain_task_manager.py` — `LocalTerrainTaskManager`：保存上传文件、维护任务状态、后台切片、orphan recovery。
- `routes/local_terrain_api.py` — blueprint `local_terrain_api_bp`，前缀 `/api/terrain/local`，接收 multipart 上传。
- `tests/test_local_terrain_schema.py` — 两张新表与索引存在性、NOT NULL 校验。
- `tests/test_local_terrain_api.py` — 上传创建任务、校验、文件落盘、注入 fake tiler、查询详情。
- `tests/test_local_terrain_static.py` — `/terrain/local/<id>/...` 从 `output_dir` 服务文件、路径穿越被拒。

修改：
- `database.py` — 在 `init_database()` 内建两张表 + 索引。
- `app.py` — 初始化并注入 `LocalTerrainTaskManager`，注册 blueprint。
- `routes/__init__.py` — 导出 `local_terrain_api_bp`。
- `routes/terrain_static.py` — 新增 `/terrain/local/<task_id>/<subpath>` 路由。
- `tests/test_orphan_recovery.py` — 扩展：残留 `running` 本地切片任务恢复为 `failed`。
- `templates/index.html` — 下载类型新增「本地高程切片」+ 上传字段块。
- `static/js/map.js` — 下载类型切换显隐 + 提交分流（FormData，跳过 bbox 校验）。
- `static/js/tasks.js` — `normalizeTask` 扩展、`apiPrefixForType` 扩展、`loadActiveTasks` 拉取本地切片任务。

---

## Task 1: 数据库 schema

**Files:**
- Modify: `database.py`（在 `dem_terrain_jobs` 索引创建之后、`DEFAULT_CONFIGS` 插入之前）
- Test: `tests/test_local_terrain_schema.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_local_terrain_schema.py`:

```python
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload_db(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    sys.modules.pop("database", None)
    db = importlib.import_module("database")
    db.init_database()
    return db


def test_local_terrain_tables_exist(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r["name"] for r in cur.fetchall()}
        assert "local_terrain_tasks" in tables
        assert "local_terrain_files" in tables

        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {r["name"] for r in cur.fetchall()}
        assert "idx_local_terrain_tasks_status" in indexes
        assert "idx_local_terrain_files_status" in indexes
    finally:
        conn.close()


def test_local_terrain_tasks_notnull_columns(monkeypatch, tmp_path):
    db = _reload_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(local_terrain_tasks)")
        cols = {r["name"]: r for r in cur.fetchall()}
        assert cols["status"]["notnull"] == 1
        assert cols["output_dir"]["notnull"] == 1
        assert cols["maxzoom"]["notnull"] == 1

        cur.execute("PRAGMA table_info(local_terrain_files)")
        fcols = {r["name"]: r for r in cur.fetchall()}
        assert fcols["task_id"]["notnull"] == 1
        assert fcols["stored_filename"]["notnull"] == 1
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_local_terrain_schema.py -v`
Expected: FAIL（断言 `local_terrain_tasks in tables` 失败，表不存在）

- [ ] **Step 3: 加表**

In `database.py`, after the `idx_dem_terrain_jobs_status` index `cursor.execute(...)` block (around line 262) and **before** the `cursor.executemany('INSERT OR IGNORE INTO config ...', DEFAULT_CONFIGS)` block, insert:

```python
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS local_terrain_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT NOT NULL,
                source_dir TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                total_files INTEGER DEFAULT 0,
                uploaded_files INTEGER DEFAULT 0,
                failed_files INTEGER DEFAULT 0,
                maxzoom INTEGER NOT NULL,
                parent_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_local_terrain_tasks_status
            ON local_terrain_tasks(status)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS local_terrain_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                original_filename TEXT,
                stored_filename TEXT NOT NULL,
                local_path TEXT,
                size_bytes INTEGER,
                status TEXT NOT NULL DEFAULT 'uploaded',
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES local_terrain_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, stored_filename)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_local_terrain_files_status
            ON local_terrain_files(status)
        ''')
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_local_terrain_schema.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add database.py tests/test_local_terrain_schema.py
git commit -m "feat(db): add local_terrain_tasks/files tables for upload tiling"
```

---

## Task 2: LocalTerrainTaskManager — 保存上传 + 创建任务

**Files:**
- Create: `services/local_terrain_task_manager.py`
- Test: `tests/test_local_terrain_api.py`（本任务先测 manager 的 `create_task_with_files`，不经过 HTTP）

本任务实现「保存上传文件 + 创建任务行」。切片启动在 Task 3。注意 `create_task_with_files` 接收已读出的字节，不直接依赖 Flask，便于测试。

- [ ] **Step 1: 写失败测试**

Create `tests/test_local_terrain_api.py`:

```python
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("database", "services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("database")
    db.init_database()
    mgr_mod = importlib.import_module("services.local_terrain_task_manager")
    return db, mgr_mod


def test_create_task_saves_files_and_rows(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    # Don't actually tile in this task's tests.
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    files = [
        ("a.tif", b"fake-tif-bytes-a"),
        ("b.tiff", b"fake-tif-bytes-b"),
    ]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=12)

    task = mgr.get_task(task_id)
    assert task["name"] == "local-1"
    assert task["total_files"] == 2
    assert task["uploaded_files"] == 2
    assert task["failed_files"] == 0
    assert task["maxzoom"] == 12

    # Files saved under source/ as *_dem.tif
    from pathlib import Path
    source_dir = Path(task["source_dir"])
    saved = sorted(p.name for p in source_dir.glob("*_dem.tif"))
    assert saved == ["upload_1_dem.tif", "upload_2_dem.tif"]
    assert (source_dir / "upload_1_dem.tif").read_bytes() == b"fake-tif-bytes-a"

    rows = mgr.list_files(task_id)
    assert len(rows) == 2
    assert {r["original_filename"] for r in rows} == {"a.tif", "b.tiff"}
    assert all(r["status"] == "uploaded" for r in rows)


def test_create_task_rejects_non_tif(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="bad", files=[("x.png", b"data")], maxzoom=12)


def test_create_task_rejects_empty_file_list(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="empty", files=[], maxzoom=12)


def test_create_task_rejects_zero_byte_file(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="zero", files=[("a.tif", b"")], maxzoom=12)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_local_terrain_api.py -v`
Expected: FAIL（`ModuleNotFoundError: services.local_terrain_task_manager`）

- [ ] **Step 3: 写 manager（创建 + 保存部分）**

Create `services/local_terrain_task_manager.py`:

```python
"""
Local Terrain Task Manager

Creates terrain tiling tasks from user-uploaded GeoTIFF files, backed by
local_terrain_tasks/local_terrain_files tables. Reuses the existing terrain
tiler (tile_dem_task_dir) by saving uploads as *_dem.tif.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import Config
from database import get_connection
from services.config_manager import ConfigManager
from services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

logger = logging.getLogger(__name__)

_ALLOWED_EXT = (".tif", ".tiff")
UploadFile = Tuple[str, bytes]  # (original_filename, content_bytes)


class LocalTerrainTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        """Demote leftover 'running' rows to 'failed'.

        Tiling is a one-shot build_terrain call with no resume model, so a
        leftover 'running' row from a dead process can only be restarted.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM local_terrain_tasks WHERE status = 'running'")
            ids = [row["id"] for row in cur.fetchall()]
            if ids:
                now = datetime.now()
                cur.executemany(
                    "UPDATE local_terrain_tasks SET status='failed', completed_at=?, "
                    "error_message='Process was interrupted before completion; re-upload to retile' "
                    "WHERE id=? AND status='running'",
                    [(now, i) for i in ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan local terrain tasks (failed): {ids}")
        except Exception as e:
            logger.error(f"Failed to recover local terrain orphans: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _default_maxzoom(self) -> int:
        raw = self.config.get("terrain_local_maxzoom", "14")
        try:
            return int(raw) if raw is not None else 14
        except Exception:
            return 14

    def create_task_with_files(
        self,
        name: str,
        files: Sequence[UploadFile],
        maxzoom: Optional[int] = None,
    ) -> int:
        """Create a task, persist uploaded tifs, then auto-start tiling.

        files: sequence of (original_filename, content_bytes) already read into
        memory by the caller (the route reads werkzeug FileStorage).
        """
        name = (name or "Local Terrain Task").strip() or "Local Terrain Task"

        valid: List[UploadFile] = []
        for original, content in files:
            ext = Path(original or "").suffix.lower()
            if ext not in _ALLOWED_EXT:
                raise ValueError(f"Unsupported file type: {original} (only .tif/.tiff)")
            if not content:
                raise ValueError(f"Empty file: {original}")
            valid.append((original, content))

        if not valid:
            raise ValueError("No valid .tif/.tiff files uploaded")

        if maxzoom is None:
            maxzoom = self._default_maxzoom()
        maxzoom = int(maxzoom)

        base = Path(Config.DOWNLOADS_DIR) / "terrain"
        parent_url = "http://localhost:5000/terrain/base/layer.json"

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO local_terrain_tasks
                  (name, status, output_path, source_dir, output_dir,
                   total_files, uploaded_files, failed_files, maxzoom, parent_url)
                VALUES (?, 'pending', '', '', '', ?, 0, 0, ?, ?)
                """,
                (name, len(valid), maxzoom, parent_url),
            )
            task_id = cur.lastrowid

            task_root = base / f"local_task_{task_id}"
            source_dir = task_root / "source"
            output_dir = task_root / "terrain_tiles"
            source_dir.mkdir(parents=True, exist_ok=True)

            cur.execute(
                "UPDATE local_terrain_tasks SET output_path=?, source_dir=?, output_dir=? WHERE id=?",
                (str(task_root), str(source_dir), str(output_dir), task_id),
            )

            uploaded = 0
            failed = 0
            for idx, (original, content) in enumerate(valid, start=1):
                stored = f"upload_{idx}_dem.tif"
                dest = source_dir / stored
                try:
                    dest.write_bytes(content)
                    cur.execute(
                        """
                        INSERT INTO local_terrain_files
                          (task_id, original_filename, stored_filename, local_path, size_bytes, status)
                        VALUES (?, ?, ?, ?, ?, 'uploaded')
                        """,
                        (task_id, original, stored, str(dest), len(content)),
                    )
                    uploaded += 1
                except Exception as e:
                    failed += 1
                    cur.execute(
                        """
                        INSERT INTO local_terrain_files
                          (task_id, original_filename, stored_filename, local_path, size_bytes, status, error_message)
                        VALUES (?, ?, ?, ?, ?, 'failed', ?)
                        """,
                        (task_id, original, stored, str(dest), len(content), str(e)),
                    )

            cur.execute(
                "UPDATE local_terrain_tasks SET uploaded_files=?, failed_files=? WHERE id=?",
                (uploaded, failed, task_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if uploaded == 0:
            self._mark_failed(task_id, "All uploaded files failed to save")
            raise ValueError("All uploaded files failed to save")

        self.start_tiling(task_id)
        return task_id

    def _mark_failed(self, task_id: int, message: str) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, completed_at=? WHERE id=?",
                (message, datetime.now(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM local_terrain_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Local terrain task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_files(self, task_id: int) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM local_terrain_files WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM local_terrain_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def start_tiling(self, task_id: int) -> None:
        # Implemented in Task 3.
        raise NotImplementedError
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_local_terrain_api.py -v`
Expected: PASS（4 passed；`start_tiling` 被 monkeypatch 成 no-op，未触发 `NotImplementedError`）

- [ ] **Step 5: 提交**

```bash
git add services/local_terrain_task_manager.py tests/test_local_terrain_api.py
git commit -m "feat(local-terrain): save uploads and create task rows"
```

---

## Task 3: LocalTerrainTaskManager — 后台切片 + cancel

**Files:**
- Modify: `services/local_terrain_task_manager.py`（替换 `start_tiling`，新增 `_run_tiling_job`、`cancel_task`）
- Test: `tests/test_local_terrain_api.py`（新增切片相关测试）

- [ ] **Step 1: 写失败测试**

Append to `tests/test_local_terrain_api.py`:

```python
def test_start_tiling_invokes_build_terrain(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    # Capture the tiler call by patching tile_dem_task_dir in the manager module.
    def fake_tile(task_dir, out_dir, params):
        calls["task_dir"] = task_dir
        calls["out_dir"] = out_dir
        calls["maxzoom"] = params.maxzoom
        # Simulate tiler output so completion logic can proceed.
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    files = [("a.tif", b"fake-tif-bytes-a")]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=11)

    # start_tiling spawns a thread; wait for it.
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "completed"
    assert calls["maxzoom"] == 11
    assert str(calls["task_dir"]).endswith(f"local_task_{task_id}/source")
    assert str(calls["out_dir"]).endswith(f"local_task_{task_id}/terrain_tiles")


def test_start_tiling_marks_failed_on_error(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    def boom(task_dir, out_dir, params):
        raise RuntimeError("tiler exploded")

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", boom)

    task_id = mgr.create_task_with_files(name="local-1", files=[("a.tif", b"x")], maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "failed"
    assert "tiler exploded" in (task["error_message"] or "")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_local_terrain_api.py::test_start_tiling_invokes_build_terrain -v`
Expected: FAIL（`start_tiling` 抛 `NotImplementedError`）

- [ ] **Step 3: 实现 start_tiling / _run_tiling_job / cancel_task**

In `services/local_terrain_task_manager.py`, replace the `start_tiling` stub (the `raise NotImplementedError` method) with:

```python
    def start_tiling(self, task_id: int) -> None:
        task_id = int(task_id)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, source_dir, output_dir, maxzoom, parent_url "
                "FROM local_terrain_tasks WHERE id=?",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Local terrain task {task_id} not found")
            if row["status"] == "running":
                raise ValueError(f"Local terrain task {task_id} is already running")

            cur.execute(
                "UPDATE local_terrain_tasks SET status='running', started_at=?, "
                "completed_at=NULL, error_message=NULL WHERE id=?",
                (datetime.now(), task_id),
            )
            conn.commit()
            source_dir = Path(row["source_dir"])
            output_dir = Path(row["output_dir"])
            maxzoom = int(row["maxzoom"])
            parent_url = row["parent_url"] or "http://localhost:5000/terrain/base/layer.json"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self._emit_progress(task_id)

        with self._state_lock:
            th = threading.Thread(
                target=self._run_tiling_job,
                args=(task_id, source_dir, output_dir, maxzoom, parent_url),
                daemon=True,
                name=f"LocalTerrainTiling-{task_id}",
            )
            self.active_tasks[task_id] = th
        th.start()

    def _run_tiling_job(
        self, task_id: int, source_dir: Path, output_dir: Path, maxzoom: int, parent_url: str
    ) -> None:
        try:
            tile_dem_task_dir(
                task_dir=source_dir,
                out_dir=output_dir,
                params=TileParams(maxzoom=maxzoom, parent_url=parent_url),
            )
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='completed', completed_at=?, "
                    "error_message=NULL WHERE id=? AND status='running'",
                    (datetime.now(), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            if self.socketio:
                self.socketio.emit(
                    "task_completed",
                    {"task_id": task_id, "task_type": "local_terrain", "status": "completed"},
                )
        except Exception as e:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='failed', completed_at=?, "
                    "error_message=? WHERE id=?",
                    (datetime.now(), str(e), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            logger.error(f"Local terrain tiling failed for task {task_id}: {e}")
            if self.socketio:
                self.socketio.emit(
                    "task_failed",
                    {"task_id": task_id, "task_type": "local_terrain",
                     "status": "failed", "error_message": str(e)},
                )
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)

    def cancel_task(self, task_id: int) -> None:
        """Cancel if not yet tiling. If build_terrain is in-flight it cannot be
        hard-interrupted; we only flip non-running states to cancelled."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='cancelled' "
                "WHERE id=? AND status IN ('pending','uploading')",
                (task_id,),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT status FROM local_terrain_tasks WHERE id=?", (task_id,))
                r = cur.fetchone()
                if not r:
                    raise ValueError(f"Local terrain task {task_id} not found")
                if r["status"] == "running":
                    raise ValueError(
                        "Tiling is in progress and cannot be interrupted; "
                        "wait for it to finish"
                    )
            conn.commit()
        finally:
            conn.close()

    def _emit_progress(self, task_id: int) -> None:
        if not self.socketio:
            return
        try:
            task = self.get_task(task_id)
            task["task_type"] = "local_terrain"
            self.socketio.emit("task_progress", task)
        except Exception as e:
            logger.warning(f"Failed to emit local terrain progress for {task_id}: {e}")
```

Also confirm the module-level import `from services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir` exists (added in Task 2). The tests patch `mgr_mod.tile_dem_task_dir`, which requires it to be imported by name at module level — it is.

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_local_terrain_api.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add services/local_terrain_task_manager.py tests/test_local_terrain_api.py
git commit -m "feat(local-terrain): background tiling job + cancel semantics"
```

---

## Task 4: API blueprint + app wiring

**Files:**
- Create: `routes/local_terrain_api.py`
- Modify: `routes/__init__.py`, `app.py`
- Test: `tests/test_local_terrain_api.py`（新增 HTTP 层测试）

- [ ] **Step 1: 写失败测试**

Append to `tests/test_local_terrain_api.py`:

```python
import io


def _load_app(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "database", "services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_http_wiring_list_does_not_500(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/terrain/local/tasks")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_http_upload_creates_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)

    # Don't run real tiler.
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    data = {
        "name": "http-local",
        "maxzoom": "10",
        "files": [
            (io.BytesIO(b"fake-a"), "a.tif"),
            (io.BytesIO(b"fake-b"), "b.tiff"),
        ],
    }
    resp = client.post(
        "/api/terrain/local/tasks",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    task_id = body["task_id"]

    detail = client.get(f"/api/terrain/local/tasks/{task_id}")
    assert detail.status_code == 200
    dbody = detail.get_json()
    assert dbody["task"]["total_files"] == 2
    assert dbody["layer_url"].endswith(f"/terrain/local/{task_id}/layer.json")
    assert len(dbody["files"]) == 2


def test_http_upload_no_valid_files_returns_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "bad", "files": [(io.BytesIO(b"x"), "x.png")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_local_terrain_api.py::test_http_wiring_list_does_not_500 -v`
Expected: FAIL（404，路由不存在 / 或 app 无 `local_terrain_task_manager` 属性）

- [ ] **Step 3: 写 blueprint**

Create `routes/local_terrain_api.py`:

```python
"""
Local Terrain API routes

Endpoints for uploading GeoTIFF files and tiling them into Cesium terrain.
"""

import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

local_terrain_api_bp = Blueprint("local_terrain_api", __name__, url_prefix="/api/terrain/local")

local_terrain_task_manager = None


def init_local_terrain_task_manager(tm):
    global local_terrain_task_manager
    local_terrain_task_manager = tm
    logger.info("Local terrain task manager initialized in local terrain API routes")


@local_terrain_api_bp.route("/tasks", methods=["POST"])
def create_local_terrain_task():
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        name = request.form.get("name") or "Local Terrain Task"
        maxzoom_raw = request.form.get("maxzoom")
        maxzoom = int(maxzoom_raw) if maxzoom_raw not in (None, "") else None

        uploads = request.files.getlist("files")
        files = [(f.filename, f.read()) for f in uploads]

        task_id = local_terrain_task_manager.create_task_with_files(
            name=name, files=files, maxzoom=maxzoom
        )
        return jsonify({"success": True, "task_id": task_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating local terrain task: {e}")
        return jsonify({"error": "Failed to create local terrain task"}), 500


@local_terrain_api_bp.route("/tasks", methods=["GET"])
def list_local_terrain_tasks():
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        limit = request.args.get("limit", 100, type=int)
        tasks = local_terrain_task_manager.list_tasks(limit=limit)
        return jsonify({"success": True, "tasks": tasks, "count": len(tasks)})
    except Exception as e:
        logger.error(f"Error listing local terrain tasks: {e}")
        return jsonify({"error": "Failed to list local terrain tasks"}), 500


@local_terrain_api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_local_terrain_task(task_id: int):
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        task = local_terrain_task_manager.get_task(task_id)
        files = local_terrain_task_manager.list_files(task_id)
        layer_url = f"{request.host_url.rstrip('/')}/terrain/local/{task_id}/layer.json"
        return jsonify({"success": True, "task": task, "files": files, "layer_url": layer_url})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Error getting local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to get local terrain task"}), 500


@local_terrain_api_bp.route("/tasks/<int:task_id>/cancel", methods=["POST"])
def cancel_local_terrain_task(task_id: int):
    if not local_terrain_task_manager:
        return jsonify({"error": "Local terrain task manager not initialized"}), 500
    try:
        local_terrain_task_manager.cancel_task(task_id)
        return jsonify({"success": True, "message": f"Local terrain task {task_id} cancelled"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error cancelling local terrain task {task_id}: {e}")
        return jsonify({"error": "Failed to cancel local terrain task"}), 500
```

- [ ] **Step 4: 导出 blueprint**

In `routes/__init__.py`, add the import and `__all__` entry:

```python
from routes.local_terrain_api import local_terrain_api_bp
```

And extend `__all__` to include `'local_terrain_api_bp'`:

```python
__all__ = ['main_bp', 'api_bp', 'dem_api_bp', 'terrain_api_bp', 'terrain_static_bp', 'local_terrain_api_bp']
```

- [ ] **Step 5: app wiring**

In `app.py`:

Add to the routes import line (extend the existing `from routes import ...`):

```python
from routes import main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp, local_terrain_api_bp
```

Add a new manager import after `from services.dem_task_manager import DemTaskManager`:

```python
from services.local_terrain_task_manager import LocalTerrainTaskManager
from routes.local_terrain_api import init_local_terrain_task_manager
```

After the `init_terrain_dem_task_manager(dem_task_manager)` block (around line 86), add:

```python
# Create LocalTerrainTaskManager and inject into local terrain API routes
local_terrain_task_manager = LocalTerrainTaskManager(socketio=socketio)
init_local_terrain_task_manager(local_terrain_task_manager)
logger.info("LocalTerrainTaskManager created and injected")
```

After `app.register_blueprint(terrain_static_bp)` (around line 101), add:

```python
app.register_blueprint(local_terrain_api_bp)
logger.info("Local terrain API blueprint registered")
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `uv run pytest tests/test_local_terrain_api.py -v`
Expected: PASS（9 passed）

- [ ] **Step 7: 提交**

```bash
git add routes/local_terrain_api.py routes/__init__.py app.py tests/test_local_terrain_api.py
git commit -m "feat(local-terrain): upload API blueprint + app wiring"
```

---

## Task 5: 静态服务 /terrain/local/<id>/...

**Files:**
- Modify: `routes/terrain_static.py`
- Test: `tests/test_local_terrain_static.py`

路径从 `local_terrain_tasks.output_dir` 读取（不写死），复用现有 `_resolve_safe_file`。

- [ ] **Step 1: 写失败测试**

Create `tests/test_local_terrain_static.py`:

```python
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "database", "services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _insert_task(db, output_dir):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt', 'completed', ?, ?, ?, 14)
            """,
            (str(output_dir.parent), str(output_dir.parent / "source"), str(output_dir)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_serves_layer_json_from_output_dir(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    output_dir = tmp_path / "downloads" / "terrain" / "local_task_X" / "terrain_tiles"
    output_dir.mkdir(parents=True)
    (output_dir / "layer.json").write_text('{"ok":true}', encoding="utf-8")

    task_id = _insert_task(db, output_dir)

    resp = client.get(f"/terrain/local/{task_id}/layer.json")
    assert resp.status_code == 200
    assert b'"ok"' in resp.data


def test_blocks_path_traversal(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    output_dir = tmp_path / "downloads" / "terrain" / "local_task_Y" / "terrain_tiles"
    output_dir.mkdir(parents=True)
    task_id = _insert_task(db, output_dir)

    resp = client.get(f"/terrain/local/{task_id}/..%2f..%2f..%2fsecret")
    assert resp.status_code in (400, 404)


def test_missing_task_returns_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/terrain/local/99999/layer.json")
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/test_local_terrain_static.py -v`
Expected: FAIL（404 路由不存在，`test_serves_layer_json_from_output_dir` 失败）

- [ ] **Step 3: 加路由**

In `routes/terrain_static.py`, add at the end of the file (after `terrain_dem_static`):

```python
@terrain_static_bp.route("/local/<int:task_id>/<path:subpath>", methods=["GET"])
def terrain_local_static(task_id: int, subpath: str):
    from database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT output_dir FROM local_terrain_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row["output_dir"]:
        abort(404)

    base_dir = Path(row["output_dir"])
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return send_file(str(target))
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/test_local_terrain_static.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add routes/terrain_static.py tests/test_local_terrain_static.py
git commit -m "feat(local-terrain): serve tiles from output_dir with traversal guard"
```

---

## Task 6: Orphan recovery 测试扩展

**Files:**
- Modify: `tests/test_orphan_recovery.py`

manager 的 `_recover_orphan_running_tasks` 已在 Task 2 实现；本任务补测试锁定行为。

- [ ] **Step 1: 写失败测试**

Append to `tests/test_orphan_recovery.py`:

```python
def test_local_terrain_manager_recovers_orphans(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt-orphan', 'running', '/tmp/x', '/tmp/x/source', '/tmp/x/terrain_tiles', 14)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    ltm_mod = importlib.import_module("services.local_terrain_task_manager")
    ltm_mod.LocalTerrainTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, error_message FROM local_terrain_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert "interrupted" in (row["error_message"] or "").lower()
    finally:
        conn.close()
```

Also add `"services.local_terrain_task_manager"` to the module-pop tuple inside `_reload_with_isolated_db` (the list currently popping `"database"`, `"services.task_manager"`, `"services.dem_task_manager"`, `"app"`).

- [ ] **Step 2: 运行测试，确认失败前先确认它能跑**

Run: `uv run pytest tests/test_orphan_recovery.py::test_local_terrain_manager_recovers_orphans -v`
Expected: PASS（manager recovery 已在 Task 2 实现，本测试应直接通过；若 FAIL 说明 Task 2 的 recovery 逻辑或表名有问题，修复后再继续）

- [ ] **Step 3: 提交**

```bash
git add tests/test_orphan_recovery.py
git commit -m "test(local-terrain): cover orphan running task recovery"
```

---

## Task 7: 前端 — 首页表单上传入口

**Files:**
- Modify: `templates/index.html`, `static/js/map.js`

- [ ] **Step 1: index.html 加下载类型选项**

In `templates/index.html`, inside `<select id="downloadType">` (lines 54-57), add a third option:

```html
                            <option value="local_terrain">本地高程切片（上传 GeoTIFF）</option>
```

- [ ] **Step 2: index.html 加上传字段块**

In `templates/index.html`, after the `demOptions` block closing `</div>` (line 100) and before the `outputPath` `.mb-3` block (line 102), add:

```html
                    <div class="mb-3" id="localTerrainOptions" style="display:none;">
                        <label for="localTerrainFiles" class="form-label">上传高程文件（可多选 .tif/.tiff）</label>
                        <input type="file" class="form-control" id="localTerrainFiles" accept=".tif,.tiff" multiple>
                        <label for="localTerrainMaxzoom" class="form-label mt-2">最大切片层级</label>
                        <input type="number" class="form-control" id="localTerrainMaxzoom" min="0" max="21" value="14">
                    </div>
```

- [ ] **Step 3: map.js 切换显隐 + 跳过 bbox 要求**

In `static/js/map.js`, replace the `apply()` function inside `initDownloadTypeToggle` (lines 91-101) with one that handles three types and toggles the create button independent of bbox for local terrain:

```javascript
    const localOptions = document.getElementById('localTerrainOptions');

    function apply() {
        const t = typeEl.value;
        const isDem = t === 'dem';
        const isLocal = t === 'local_terrain';
        mapFields.forEach(el => el.style.display = (isDem || isLocal) ? 'none' : '');
        if (demOptions) demOptions.style.display = isDem ? '' : 'none';
        if (localOptions) localOptions.style.display = isLocal ? '' : 'none';

        const boundsInfo = document.getElementById('boundsInfo');
        if (boundsInfo) boundsInfo.style.display = isLocal ? 'none' : '';

        const outputPath = document.getElementById('outputPath');
        if (outputPath) {
            outputPath.closest('.mb-3').style.display = isLocal ? 'none' : '';
            if (!outputPath.dataset.userEdited) {
                outputPath.value = isDem ? './downloads/dem' : './downloads/map';
            }
        }

        const btn = document.getElementById('createTaskBtn');
        if (btn && isLocal) btn.disabled = false;
    }
```

- [ ] **Step 4: map.js 提交分流（FormData，跳过 bbox 校验）**

In `static/js/map.js`, the submit handler (line 148) begins with a `currentBounds` guard that aborts for all types. Replace the top of the handler — from `e.preventDefault();` through the `const downloadType = ...` line (lines 149-156) — with:

```javascript
    e.preventDefault();

    const downloadType = document.getElementById('downloadType')?.value || 'map';

    // Local terrain uploads have no bbox; handle separately and return early.
    if (downloadType === 'local_terrain') {
        await submitLocalTerrain();
        return;
    }

    if (!currentBounds) {
        showNotification('请先在地图上框选下载区域', 'warning');
        return;
    }
```

Then add a new function `submitLocalTerrain` at the end of `static/js/map.js`:

```javascript
async function submitLocalTerrain() {
    const fileInput = document.getElementById('localTerrainFiles');
    const files = fileInput?.files;
    if (!files || files.length === 0) {
        showNotification('请先选择至少一个 .tif/.tiff 文件', 'warning');
        return;
    }

    const fd = new FormData();
    fd.append('name', document.getElementById('taskName').value || '本地高程切片');
    fd.append('maxzoom', document.getElementById('localTerrainMaxzoom')?.value || '14');
    for (const f of files) {
        fd.append('files', f);
    }

    const btn = document.getElementById('createTaskBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '上传中...';
    try {
        const resp = await fetch('/api/terrain/local/tasks', { method: 'POST', body: fd });
        const result = await resp.json();
        if (resp.ok) {
            showNotification('上传成功，已开始切片！ID: ' + result.task_id, 'success');
            document.getElementById('downloadForm').reset();
            loadActiveTasks();
        } else {
            showNotification('上传失败: ' + (result.error || resp.status), 'danger');
        }
    } catch (err) {
        showNotification('上传失败: ' + err.message, 'danger');
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}
```

- [ ] **Step 5: 手动验证（无自动化前端测试）**

Run: `DEBUG=0 uv run python app.py`（另一终端）
- 浏览器打开 `http://localhost:5000`
- 下载类型选「本地高程切片」→ 地图字段隐藏、文件选择框出现、创建按钮可点。
- 选一个 `.tif` 文件 → 点「创建下载任务」→ 应提示上传成功并出现活动任务卡片。
- Ctrl-C 停止服务。

Expected: 上传成功、活动任务出现。若 GDAL 未装，切片会失败但任务会出现且状态变 `failed`（属预期，本步只验证上传与任务创建链路）。

- [ ] **Step 6: 提交**

```bash
git add templates/index.html static/js/map.js
git commit -m "feat(local-terrain): homepage upload entry and submit flow"
```

---

## Task 8: 前端 — 活动任务归一化与控制

**Files:**
- Modify: `static/js/tasks.js`

- [ ] **Step 1: loadActiveTasks 拉取本地切片任务**

In `static/js/tasks.js`, update `loadActiveTasks` (lines 45-69). Change the `Promise.all` to fetch three endpoints and merge:

```javascript
async function loadActiveTasks() {
    try {
        const [mapResp, demResp, localResp] = await Promise.all([
            fetch('/api/tasks'),
            fetch('/api/dem/tasks'),
            fetch('/api/terrain/local/tasks')
        ]);
        const mapData = await mapResp.json();
        const demData = await demResp.json();
        const localData = await localResp.json();

        const mapTasks = (mapData.tasks || []).map(t => normalizeTask(t, 'map'));
        const demTasks = (demData.tasks || []).map(t => normalizeTask(t, 'dem'));
        const localTasks = (localData.tasks || []).map(t => normalizeTask(t, 'local_terrain'));
        const all = [...mapTasks, ...demTasks, ...localTasks].filter(t =>
            ['pending', 'uploading', 'running', 'paused'].includes(t.status)
        );

        activeTasks.clear();
        all.forEach(task => {
            activeTasks.set(task._key, task);
        });

        renderActiveTasks(all);
    } catch (error) {
        console.error('Failed to load tasks:', error);
    }
}
```

- [ ] **Step 2: normalizeTask 支持 local_terrain**

In `static/js/tasks.js`, in `normalizeTask` (lines 71-94), add a branch before the final `map` return:

```javascript
    if (type === 'local_terrain') {
        const total = task.total_files || 0;
        const done = task.status === 'completed' ? total : (task.uploaded_files || 0);
        return {
            ...task,
            task_type: 'local_terrain',
            id: task.id,
            _key: `local_terrain:${task.id}`,
            total_items: total,
            downloaded_items: done,
            failed_items: task.failed_files || 0,
            items_label: '文件'
        };
    }
```

- [ ] **Step 3: apiPrefixForType 支持 local_terrain**

In `static/js/tasks.js`, replace `apiPrefixForType` (lines 480-482):

```javascript
function apiPrefixForType(taskType) {
    if (taskType === 'dem') return '/api/dem/tasks';
    if (taskType === 'local_terrain') return '/api/terrain/local/tasks';
    return '/api/tasks';
}
```

This makes the existing `cancelTask(taskId, 'local_terrain')` button POST to `/api/terrain/local/tasks/<id>/cancel` automatically. The card already renders a cancel button for `running` tasks; local terrain tasks have no pause/resume backend, and `startTask`/`pauseTask`/`resumeTask` are only rendered for `pending`/`running`/`paused` map/dem states — for local terrain the task arrives already `running` (auto-start) so only cancel shows, which matches the design.

- [ ] **Step 4: 手动验证**

Run: `DEBUG=0 uv run python app.py`
- 上传一个 tif → 活动任务卡片出现，类型显示为本地切片，进度按文件数显示。
- 切片完成（或失败）后卡片按状态更新。
- Ctrl-C 停止。

Expected: 本地切片任务出现在活动任务区并正确归一化。

- [ ] **Step 5: 提交**

```bash
git add static/js/tasks.js
git commit -m "feat(local-terrain): active task normalization and api routing"
```

---

## Task 9: 全量测试 + 收尾

**Files:** 无新增

- [ ] **Step 1: 跑全套后端测试**

Run: `uv run pytest tests/ -v`
Expected: 全绿。重点确认新增的 `test_local_terrain_schema.py`、`test_local_terrain_api.py`、`test_local_terrain_static.py`、扩展后的 `test_orphan_recovery.py` 全部 PASS，且未破坏既有 `test_terrain_api.py` 等。

- [ ] **Step 2: 若有失败，按 systematic-debugging 修复**

不要跳过失败。修复后重跑 `uv run pytest tests/ -v` 直到全绿。

- [ ] **Step 3: 最终提交（如有修复）**

```bash
git add -A
git commit -m "test(local-terrain): full suite green"
```

---

## Self-Review 结果

**Spec coverage：**
- 独立任务线（不污染 dem_tasks）→ Task 1（表）+ Task 2（manager）✓
- 多文件上传 → Task 2 `create_task_with_files` + Task 4 `request.files.getlist` ✓
- 上传后自动切片 → Task 2 末尾 `self.start_tiling(task_id)` ✓
- 首页入口 → Task 7 ✓
- 目录约定 `local_task_<id>/source` + `/terrain_tiles` → Task 2 ✓
- 静态服务从 output_dir 读路径 + 安全校验 → Task 5 ✓
- 错误处理（非 tif / 空文件 / 全部失败 / 切片失败）→ Task 2 + Task 3 ✓
- 取消语义（running 不可硬中断）→ Task 3 `cancel_task` ✓
- 重启恢复 running→failed → Task 2 recovery + Task 6 测试 ✓
- Socket.IO 统一事件 → Task 3 emit ✓
- 不做暂停/恢复 → 前端只渲染 cancel（Task 8 Step 3 说明）✓
- 测试范围四类 → Task 1/4/5/6 覆盖 ✓

**Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整代码。

**Type/名称一致性：**
- `create_task_with_files(name, files, maxzoom)` — Task 2 定义，Task 4 调用一致 ✓
- `start_tiling(task_id)` — Task 2 stub、Task 3 实现、Task 4 monkeypatch 一致 ✓
- `tile_dem_task_dir(task_dir, out_dir, params)` — 与现有签名一致（已读源码确认）✓
- `_resolve_safe_file(base_dir, subpath)` — 与现有签名一致 ✓
- 表名 `local_terrain_tasks` / `local_terrain_files`、列名在 Task 1/2/3/4/5 全一致 ✓
- 前端 `task_type: 'local_terrain'`、`_key`、`apiPrefixForType` 一致 ✓

无未决问题。
