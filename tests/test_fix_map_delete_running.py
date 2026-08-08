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
            "zoom_min, zoom_max, style, output_format, output_path, total_tiles) "
            "VALUES (1, 't', ?, 1, 0, 1, 0, 1, 1, 'satellite', 'png', ?, 1)",
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
    """墓碑必须挡住 task_tiles 的 INSERT —— 这是墓碑存在的唯一理由。

    打的是 _execute_task 真正用的那个 _write_progress_batch（真方法 + 真连接 +
    已删父行）。拿薄封装代打不行：那样短路被误删时这条用例照样绿。
    """
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

    progress_conn = db.get_connection()
    try:
        # 不加短路的话这里抛 sqlite3.IntegrityError: FOREIGN KEY constraint failed
        mgr._write_progress_batch(
            progress_conn, 1,
            ([(1, 5, 1, 1, "boom")], [], [], 3, 1),
        )
        left = progress_conn.execute(
            "SELECT COUNT(*) FROM task_tiles WHERE task_id = 1").fetchone()[0]
        assert left == 0, "墓碑命中时必须整批丢弃，一行都不该落库"
    finally:
        progress_conn.close()
