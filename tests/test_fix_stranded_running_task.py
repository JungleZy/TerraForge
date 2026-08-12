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
    """表名与定位列都直接进 SQL —— 白名单是硬约束，不是文档约定。"""
    from src.services.task_cleanup import fail_stranded_running_task

    with pytest.raises(ValueError):
        fail_stranded_running_task("tasks; DROP TABLE tasks", 1)
    with pytest.raises(ValueError):
        fail_stranded_running_task("dem_files", 1)


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

    其余几样都是「每任务日志 + 准入」改造带进来的、`_run_task` 会无条件摸的字段，
    一个都不能少 —— 缺一个就把真正的 assert 变成 AttributeError：

    * `config_manager`：map 的 `open_task_log('map', task_id, self.config_manager)`
      第三个参数（None 合法，句柄照样开得出来）；
    * `_admission`：`start_task` 里算出的配额 / 磁盘判决，本用例没走准入所以是空的；
    * `_run_status`：本轮的运行态，缺省即 running —— 正是搁死补偿要收拾的那一种；
    * `_refill_targets` / `_gap_accepted`：map 的 finally 会清掉的两份本轮状态。
    """

    def __init__(self, exec_name, coro):
        self._state_lock = threading.Lock()
        self.active_tasks = {}
        self.stop_flags = {}
        self.config_manager = None
        self._admission = {}
        self._run_status = {}
        self._refill_targets = {}
        self._gap_accepted = set()
        self.released = []
        setattr(self, exec_name, coro)

    def _release_reservation(self, task_id, reservation=None):
        """真管理器在这里把配额还给调度器；这里没申请过，只记一笔给用例看。

        第二个参数是 map 侧 B1 修复带进来的:归还必须携带**本轮自己拿到的那张**
        凭据(身份比较),没有凭据对象就没有归还权。本用例走的是「线程搁死」那条
        路,`_admission` 是空的,所以传进来的恒为 None —— 给它一个缺省值是为了
        让 dem / contour 那两条仍按单参数调用的路径也能共用这个假管理器。
        """
        self.released.append(task_id)


MANAGERS = [
    ("src.services.task_manager", "TaskManager", "tasks", "_execute_task"),
    ("src.services.dem_task_manager", "DemTaskManager", "dem_tasks", "_execute"),
    ("src.services.contour_task_manager", "ContourTaskManager", "contour_tasks", "_execute"),
]


def _run_task_of(module_name, class_name):
    mod = __import__(module_name, fromlist=[class_name])
    return getattr(mod, class_name)._run_task


# 假 `_execute*` 的签名是**契约的一部分**，不是随便写的：
#
# * `tlog` 写成 keyword-only 且**没有缺省值** —— 三条管线的 `_run_task` 都必须把
#   自己开出来的每任务日志句柄传进协程（§4.5：任何终态都要能从任务日志解释）。
#   哪天有人把这个传参删了，这里立刻 TypeError，而不是悄悄退化成「只写全局日志」。
# * 各管线自己的那一个额外 kwarg（map 没有、dem 是 `max_connections`、contour 是
#   `workers`）用 `**_granted` 兜住：这几个用例测的是搁死补偿网，不关心配额怎么
#   传，为它们逐个开参数只会让签名跟着调度器一起漂。


@pytest.mark.parametrize("module_name,class_name,table,attr", MANAGERS)
def test_thread_exit_on_exception_fails_the_row(db, module_name, class_name, table, attr):
    """路 1：`_execute*` 在建连接之前就炸（它自己的兜底盖不住这一段）。"""
    _insert_running(db, table, 11)

    async def _boom(task_id, stop_flag=None, *, tlog, **_granted):
        assert tlog is not None, '_run_task 必须把每任务日志句柄传下来'
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

    async def _quiet_stop(task_id, stop_flag=None, *, tlog, **_granted):
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

    async def _finish(task_id, stop_flag=None, *, tlog, **_granted):
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

    async def _quiet_stop(task_id, stop_flag=None, *, tlog, **_granted):
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

    async def _boom(task_id, stop_flag=None, *, tlog, **_granted):
        raise RuntimeError("old thread dying")

    mgr = _FakeManager("_execute_task", _boom)
    newcomer = threading.Thread(target=lambda: None)
    mgr.active_tasks[14] = newcomer  # 登记的不是当前线程

    _run_task_of("src.services.task_manager", "TaskManager")(mgr, 14)

    assert _status(db, "tasks", 14)[0] == "running", (
        '晚退的旧线程把新一轮运行的行判失败了')
    assert mgr.active_tasks[14] is newcomer, '不该摘掉别人的登记'



# --------------------------------------------------------------------------
# 补偿判 failed 时，**任务自己的日志**里必须留下原因（§4.5）
#
# `fail_stranded_running_task` 只写全局 terraforge.log。用户点开的是任务详情，
# 那里读的是 logs/tasks/<pipeline>_<id>.log —— 没有这一笔，日志的最后一句是
# 线程退出事件，库里却写着 failed，两份记录当面打架，排查无从下手。
# 反过来，正常收尾时那条带 `WHERE status='running'` 的 UPDATE 是无害的 no-op，
# 那种情况下多写一句「已判 failed」比不写更糟 —— 所以判据是 helper 的**返回值**。
# --------------------------------------------------------------------------

_LOGGED_MANAGERS = [
    ("src.services.task_manager", "TaskManager", "tasks", "_execute_task", "map"),
    ("src.services.dem_task_manager", "DemTaskManager", "dem_tasks", "_execute", "dem"),
    ("src.services.contour_task_manager", "ContourTaskManager", "contour_tasks",
     "_execute", "contour"),
]


@pytest.mark.parametrize("module_name,class_name,table,attr,pipeline", _LOGGED_MANAGERS)
def test_the_stranded_verdict_is_explained_in_the_task_log(
        db, module_name, class_name, table, attr, pipeline):
    from src.services.task_logging import read_task_log

    _insert_running(db, table, 21)

    async def _boom(task_id, stop_flag=None, *, tlog, **_granted):
        raise RuntimeError("worker died mid-flight")

    mgr = _FakeManager(attr, _boom)
    mgr.active_tasks[21] = threading.current_thread()

    _run_task_of(module_name, class_name)(mgr, 21)

    assert _status(db, table, 21)[0] == "failed"
    messages = "\n".join(e["message"] for e in read_task_log(pipeline, 21))
    assert "EVENT terminal status=failed" in messages, (
        f"{pipeline}: 补偿判了 failed，任务自己的日志里却没有终态记录")
    assert "reason=thread_stranded" in messages
    assert "worker died mid-flight" in messages, "终态记录要带上真正的原因"


@pytest.mark.parametrize("module_name,class_name,table,attr,pipeline", _LOGGED_MANAGERS)
def test_a_normally_finished_task_is_not_told_it_failed(
        db, module_name, class_name, table, attr, pipeline):
    """no-op 的补偿不许写终态记录 —— 那会在一个跑完的任务里凭空多出一句失败。"""
    from src.services.task_logging import read_task_log

    _insert_running(db, table, 22)

    async def _settles(task_id, stop_flag=None, *, tlog, **_granted):
        conn = sqlite3.connect(str(db))
        conn.execute(f"UPDATE {table} SET status='completed' WHERE id=22")
        conn.commit()
        conn.close()

    mgr = _FakeManager(attr, _settles)
    mgr.active_tasks[22] = threading.current_thread()

    _run_task_of(module_name, class_name)(mgr, 22)

    assert _status(db, table, 22)[0] == "completed"
    messages = "\n".join(e["message"] for e in read_task_log(pipeline, 22))
    assert "status=failed" not in messages, (
        f"{pipeline}: 正常收尾的任务日志里被补了一句 failed")