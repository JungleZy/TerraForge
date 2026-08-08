"""P1#1/#2（2026-08-08 评审）：工作线程退出后不得留下「永久 running 且没有线程」的行。

两条路都通向同一个坏状态：

1. `_run_task` 的 `except` 只 `logger.error`，而 `_execute*` 自己的失败兜底活在
   `conn = get_connection()` **之后**的 try 里 —— 建连接失败（库被锁/损坏/磁盘满）
   或 `asyncio.run` 建不出事件循环（EMFILE）都绕过它；
2. `task_deletion.delete_task_row` 先置停止标志再 DELETE，commit 失败时事务回滚而
   标志**不**回滚（有意的，重试删除仍要它停），worker 于是走某个 stop 分支
   **正常** return —— 没有异常，也没人写终态。

坏状态的代价：三条管线的 `start_task` 都只接受 pending/paused，所以用户点「开始」
被拒，唯一出路是先点「暂停」或重启进程。

补偿网在线程退出处（`task_cleanup.fail_stranded_running_task`），一处盖住两条路。
"""
import asyncio
import os
import sqlite3
import sys
import threading

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from conftest import fresh_import  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一个真库：四张任务表齐全，DATABASE_PATH 指到 tmp_path。"""
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")

    from src.core.database import init_database

    init_database()
    return path


def _insert_running(db_path, table, task_id):
    """往任务表塞一行 status='running'，只填 NOT NULL 列。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cols = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}
    values = {"id": task_id, "status": "running"}
    for name, info in cols.items():
        if name in values or info["dflt_value"] is not None or not info["notnull"]:
            continue
        values[name] = 0 if info["type"].upper() in ("INTEGER", "REAL") else "x"
    conn.execute(
        f"INSERT INTO {table} ({','.join(values)}) "
        f"VALUES ({','.join('?' * len(values))})", tuple(values.values()))
    conn.commit()
    conn.close()


def _status(db_path, table, task_id):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT status, error_message FROM {table} WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return (row["status"], row["error_message"]) if row else (None, None)


# --------------------------------------------------------------------------
# helper 本身
# --------------------------------------------------------------------------

@pytest.mark.parametrize("table", ["tasks", "dem_tasks", "contour_tasks"])
def test_stranded_running_row_is_failed(db, table):
    from src.services.task_cleanup import fail_stranded_running_task

    _insert_running(db, table, 7)
    assert fail_stranded_running_task(table, 7, "线程异常: boom") is True

    status, error = _status(db, table, 7)
    assert status == "failed"
    assert "运行中" in error and "boom" in error, '错误信息要说清发生了什么'


@pytest.mark.parametrize("terminal", ["completed", "paused", "failed", "pending"])
def test_non_running_rows_are_never_rewritten(db, terminal):
    """正常收尾、用户暂停、已失败都不能被这道网改写 —— 它只捞 running。"""
    from src.services.task_cleanup import fail_stranded_running_task

    _insert_running(db, "tasks", 3)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE tasks SET status = ? WHERE id = 3", (terminal,))
    conn.commit()
    conn.close()

    assert fail_stranded_running_task("tasks", 3) is False
    assert _status(db, "tasks", 3)[0] == terminal


def test_deleted_row_is_a_harmless_noop(db):
    from src.services.task_cleanup import fail_stranded_running_task

    assert fail_stranded_running_task("tasks", 999) is False


def test_unknown_table_is_rejected(db):
    """表名直接进 SQL —— 白名单是硬约束，不是文档约定。"""
    from src.services.task_cleanup import fail_stranded_running_task

    with pytest.raises(ValueError):
        fail_stranded_running_task("tasks; DROP TABLE tasks", 1)
    with pytest.raises(ValueError):
        # local_terrain 没有 _run_task，切片线程自己兜底 —— 不在白名单里
        fail_stranded_running_task("local_terrain_tasks", 1)


def test_helper_never_raises_even_when_the_db_is_unusable(db, monkeypatch):
    """调用点在 finally 里 —— 从这里抛出去会盖掉真正的异常。"""
    from src.services import task_cleanup

    def _boom():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("src.core.database.get_connection", _boom)
    assert task_cleanup.fail_stranded_running_task("tasks", 1) is False


# --------------------------------------------------------------------------
# 三条管线的 _run_task 都必须挂上这道网
# --------------------------------------------------------------------------

class _FakeManager:
    """只带 `_run_task` 需要的东西：状态锁、两个登记表、一个假的 `_execute*`。

    `_execute*` 必须挂在**实例**上：`_run_task` 里是 `self._execute_task(...)`，
    而 self 是这个假管理器 —— patch 类属性根本不会被查到，而查不到抛的
    AttributeError 又恰好会让「异常路」用例假绿。
    """

    def __init__(self, exec_name, coro):
        self._state_lock = threading.Lock()
        self.active_tasks = {}
        self.stop_flags = {}
        setattr(self, exec_name, coro)


MANAGERS = [
    ("src.services.task_manager", "TaskManager", "tasks", "_execute_task"),
    ("src.services.dem_task_manager", "DemTaskManager", "dem_tasks", "_execute"),
    ("src.services.contour_task_manager", "ContourTaskManager", "contour_tasks", "_execute"),
]


def _run_task_of(module_name, class_name):
    mod = __import__(module_name, fromlist=[class_name])
    return getattr(mod, class_name)._run_task


@pytest.mark.parametrize("module_name,class_name,table,attr", MANAGERS)
def test_thread_exit_on_exception_fails_the_row(db, module_name, class_name, table, attr):
    """路 1：`_execute*` 在建连接之前就炸（它自己的兜底盖不住这一段）。"""
    _insert_running(db, table, 11)

    async def _boom(task_id, stop_flag=None):
        raise sqlite3.OperationalError("unable to open database file")

    mgr = _FakeManager(attr, _boom)
    mgr.active_tasks[11] = threading.current_thread()

    _run_task_of(module_name, class_name)(mgr, 11)

    status, error = _status(db, table, 11)
    assert status == "failed", (
        f'{table} 的 _run_task 没挂补偿网 —— 行永久停在 running 且没有线程')
    assert "unable to open database file" in error, '错误信息要带上真正的原因'
    assert 11 not in mgr.active_tasks


@pytest.mark.parametrize("module_name,class_name,table,attr", MANAGERS)
def test_thread_exit_after_a_silent_stop_return_fails_the_row(db, module_name, class_name,
                                                             table, attr):
    """路 2：删除失败后 worker 看到停止标志【正常】return —— 没有异常可捕。"""
    _insert_running(db, table, 12)

    async def _quiet_stop(task_id, stop_flag=None):
        return  # 正是 _execute* 里那几个裸 return

    mgr = _FakeManager(attr, _quiet_stop)
    mgr.active_tasks[12] = threading.current_thread()
    flag = threading.Event()
    flag.set()
    mgr.stop_flags[12] = flag

    _run_task_of(module_name, class_name)(mgr, 12, flag)

    assert _status(db, table, 12)[0] == "failed", (
        f'{table}: 停止标志置位后正常返回的 worker 没留下终态 —— '
        '行永久停在 running（删除 commit 失败那条路）')


@pytest.mark.parametrize("module_name,class_name,table,attr", MANAGERS)
def test_normal_completion_is_not_rewritten(db, module_name, class_name, table, attr):
    """回归保护：`_execute*` 自己写好终态时，这道网必须闭嘴。"""
    _insert_running(db, table, 13)

    async def _finish(task_id, stop_flag=None):
        conn = sqlite3.connect(str(db))
        conn.execute(f"UPDATE {table} SET status='completed' WHERE id=13")
        conn.commit()
        conn.close()

    mgr = _FakeManager(attr, _finish)
    mgr.active_tasks[13] = threading.current_thread()

    _run_task_of(module_name, class_name)(mgr, 13)

    assert _status(db, table, 13) == ("completed", None)


@pytest.mark.parametrize("module_name,class_name,table,attr", MANAGERS)
def test_paused_row_survives_the_stop_return(db, module_name, class_name, table, attr):
    """`pause_task` 是先 commit 'paused' 再置标志 —— 那条正常路不能被改写成 failed。"""
    _insert_running(db, table, 15)
    conn = sqlite3.connect(str(db))
    conn.execute(f"UPDATE {table} SET status='paused' WHERE id=15")
    conn.commit()
    conn.close()

    async def _quiet_stop(task_id, stop_flag=None):
        return

    mgr = _FakeManager(attr, _quiet_stop)
    mgr.active_tasks[15] = threading.current_thread()
    flag = threading.Event()
    flag.set()
    mgr.stop_flags[15] = flag

    _run_task_of(module_name, class_name)(mgr, 15, flag)

    assert _status(db, table, 15)[0] == "paused", '用户主动暂停被判成失败了'


def test_a_relaunched_task_is_not_stolen_by_a_late_exiting_thread(db):
    """晚退的旧线程不能把新登记线程的行判失败。

    `_run_task` 的 finally 只在「自己就是登记在册的那个线程」时才补偿。
    """
    _insert_running(db, "tasks", 14)

    async def _boom(task_id, stop_flag=None):
        raise RuntimeError("old thread dying")

    mgr = _FakeManager("_execute_task", _boom)
    newcomer = threading.Thread(target=lambda: None)
    mgr.active_tasks[14] = newcomer  # 登记的不是当前线程

    _run_task_of("src.services.task_manager", "TaskManager")(mgr, 14)

    assert _status(db, "tasks", 14)[0] == "running", (
        '晚退的旧线程把新一轮运行的行判失败了')
    assert mgr.active_tasks[14] is newcomer, '不该摘掉别人的登记'
