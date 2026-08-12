"""§4.5 —— 任何终态都要能从**任务自己的**日志解释原因。

用户排查一个失败任务时点开的是任务详情，那里读的是
`logs/tasks/<pipeline>_<id>.log`。终态只写进库的 `error_message` 加全局
`terraforge.log` 是不够的：全局日志按时间混着四条管线所有任务的行，而任务日志
是用户唯一会看的那一份。

本文件盯的是**没有 tlog 在场**的那几个终态写入点 —— 它们的共同处境是工作线程
从未开始（线程创建失败、上传阶段就全挂了），而开每任务日志是线程体的第一条
语句。这类点最容易被漏：它们不在任何 happy path 上，测试也很少走到。

另外两类终态写入点在别处立着：
- 启动孤儿恢复 → `tests/test_orphan_recovery.py`
- 线程搁死补偿 → `tests/test_fix_stranded_running_task.py`
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    from src.core import database
    from src.core.config import Config

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


def _insert(sql, params=()):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _status(table, row_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            f'SELECT status, error_message FROM {table} WHERE id = ?', (row_id,)).fetchone()
        return (row['status'], row['error_message'])
    finally:
        conn.close()


def test_a_tiling_thread_that_never_started_explains_itself(isolated_db):
    """DEM 切片线程创建失败：作业行回补成 failed，理由必须进任务日志。

    这条路径上没有句柄可用 —— 开每任务日志是 `_run_tiling_job` 的第一件事，而
    线程压根没起来，那一行从来没被执行。
    """
    from src.services.dem_task_manager import DemTaskManager
    from src.services.task_logging import read_task_log

    mgr = DemTaskManager(socketio=None)   # 先构造：__init__ 会做孤儿恢复
    task_id = _insert(
        """
        INSERT INTO dem_tasks
          (name, status, north, south, east, west, dataset, output_path)
        VALUES ('dem', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', '/tmp')
        """)
    job_id = _insert(
        "INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom) "
        "VALUES (?, 'running', '/tmp', 12)", (task_id,))

    mgr._mark_tiling_job_failed(task_id, 'tiling thread failed to start: boom')

    assert _status('dem_terrain_jobs', job_id)[0] == 'failed'
    messages = '\n'.join(e['message'] for e in read_task_log('dem', task_id))
    assert 'EVENT terminal status=failed' in messages
    assert 'tiling_thread_start_failed' in messages
    assert 'boom' in messages, '终态记录要带上真正的原因'


def test_a_noop_repair_does_not_claim_the_job_failed(isolated_db):
    """一行都没改（作业已是终态）时不许写终态记录 —— 那会在日志里凭空多一句失败。"""
    from src.services.dem_task_manager import DemTaskManager
    from src.services.task_logging import read_task_log

    mgr = DemTaskManager(socketio=None)
    task_id = _insert(
        """
        INSERT INTO dem_tasks
          (name, status, north, south, east, west, dataset, output_path)
        VALUES ('dem', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', '/tmp')
        """)
    _insert("INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom) "
            "VALUES (?, 'completed', '/tmp', 12)", (task_id,))

    mgr._mark_tiling_job_failed(task_id, 'late repair')

    messages = '\n'.join(e['message'] for e in read_task_log('dem', task_id))
    assert 'status=failed' not in messages
    assert 'late repair' not in messages


def test_a_local_terrain_upload_failure_explains_itself(isolated_db):
    """建任务阶段「全部上传失败」：行标 failed，理由必须进任务日志。"""
    from src.services.local_terrain_task_manager import LocalTerrainTaskManager
    from src.services.task_logging import read_task_log

    mgr = LocalTerrainTaskManager(socketio=None)
    task_id = _insert(
        """
        INSERT INTO local_terrain_tasks
          (name, status, output_path, source_dir, output_dir, maxzoom)
        VALUES ('lt', 'pending', '/tmp/x', '/tmp/x/source', '/tmp/x/tiles', 14)
        """)

    mgr._mark_failed(task_id, 'all uploads failed: disk is read-only')

    status, error = _status('local_terrain_tasks', task_id)
    assert status == 'failed'
    assert 'read-only' in (error or '')
    messages = '\n'.join(e['message'] for e in read_task_log('local_terrain', task_id))
    assert 'EVENT terminal status=failed' in messages
    assert 'upload_failed' in messages
    assert 'read-only' in messages


def test_a_local_terrain_tiling_thread_that_never_started_explains_itself(isolated_db):
    """切片线程创建失败：running 回补成 failed，理由必须进任务日志。"""
    from src.services.local_terrain_task_manager import LocalTerrainTaskManager
    from src.services.task_logging import read_task_log

    mgr = LocalTerrainTaskManager(socketio=None)
    task_id = _insert(
        """
        INSERT INTO local_terrain_tasks
          (name, status, output_path, source_dir, output_dir, maxzoom)
        VALUES ('lt', 'running', '/tmp/x', '/tmp/x/source', '/tmp/x/tiles', 14)
        """)

    mgr._mark_running_task_failed(task_id, 'tiling thread failed to start: no threads left')

    assert _status('local_terrain_tasks', task_id)[0] == 'failed'
    messages = '\n'.join(e['message'] for e in read_task_log('local_terrain', task_id))
    assert 'EVENT terminal status=failed' in messages
    assert 'tiling_thread_start_failed' in messages
    assert 'no threads left' in messages


def test_the_pending_guard_still_holds_and_stays_quiet(isolated_db):
    """`_mark_failed` 的 `status='pending'` 守卫不许被改写终态记录，也不许写日志。"""
    from src.services.local_terrain_task_manager import LocalTerrainTaskManager
    from src.services.task_logging import read_task_log

    mgr = LocalTerrainTaskManager(socketio=None)
    task_id = _insert(
        """
        INSERT INTO local_terrain_tasks
          (name, status, output_path, source_dir, output_dir, maxzoom)
        VALUES ('lt', 'completed', '/tmp/x', '/tmp/x/source', '/tmp/x/tiles', 14)
        """)

    mgr._mark_failed(task_id, 'should never land')

    assert _status('local_terrain_tasks', task_id)[0] == 'completed'
    messages = '\n'.join(e['message'] for e in read_task_log('local_terrain', task_id))
    assert 'should never land' not in messages
