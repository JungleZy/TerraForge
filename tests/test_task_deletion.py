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
            "zoom_min, zoom_max, style, output_format, output_path, total_tiles) "
            "VALUES (1, 't', ?, 1, 0, 1, 0, 1, 1, 'satellite', 'png', ?, 1)",
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
    """墓碑必须在删行【之前】写入，否则 map 的进度批次会撞外键。

    观察点必须是 DELETE 语句发出的那一刻，不能是删完之后的某个回调：外键窗口
    的起点就是 DELETE 本身，在回调里看只能证明「最终写了」，把「先删行、后写
    墓碑」这种实现放过去（实测过：墓碑挪到 DELETE 之后、commit 之前，用回调
    观察时六个用例全绿）。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)

    tomb = set()
    seen = {}
    real_get_connection = td.get_connection

    class _SpyConn:
        """只拦一件事：DELETE 发出时把墓碑当时的状态记下来，其余原样转发。"""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("DELETE FROM TASKS"):
                seen["tombstoned_at_delete"] = 1 in tomb
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(td, "get_connection",
                        lambda: _SpyConn(real_get_connection()))

    td.delete_task_row(manager=mgr, task_id=1, table="tasks", artifact_dir=None,
                       tombstone=tomb)

    assert seen["tombstoned_at_delete"] is True
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
