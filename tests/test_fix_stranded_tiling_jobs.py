"""P1（2026-08-09 评审）：两条切片线程退出后也不得留下「永久 running 且没有线程」的行。

`dem_terrain_jobs` 与 `local_terrain_tasks` 曾被排除在
`task_cleanup._STRANDED_TASK_TABLES` 之外，白名单注释给的理由是「它没有
`_run_task`，切片线程 `_run_tiling_job` 自己有兜底 except 把行判 failed」。
两条路各自证伪了这句话：

1. 兜底 `except` 的第一句就是 `conn = get_connection()`，它在自己的 try **之外** ——
   建连接失败（库被锁 / 磁盘满）时新异常直接穿透线程，行留在 running；
2. stop 分支是**正常** return。`task_deletion.delete_task_row` 先置停止标志再
   DELETE，commit 失败时事务回滚而标志**不**回滚（有意的，重试删除仍要它停），
   于是行还在、标志已置、没有异常给那个 except 接。

坏状态的代价比下载类三条管线更硬：两个 `start_tiling` 的闸门都是
`status != 'running'`，搁死的行会被判成「已在运行」而 ValueError，而
`src/routes/terrain_api.py` 没有任何重置作业的端点 —— 只有重启进程能解开。

补偿网与另外三条管线同一处、同一形状：`_run_tiling_job` 的 finally 调
`task_cleanup.fail_stranded_running_task`，一处盖住两条路。
"""
import os
import sqlite3
import sys
import threading

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：全部任务表齐全，DATABASE_PATH 指到 tmp_path。"""
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core.database import init_database

    init_database()
    return path


def _insert(conn, table, values):
    """塞一行，NOT NULL 且无默认值的列自动补占位值（同 test_fix_stranded_running_task）。"""
    cols = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}
    row = dict(values)
    for name, info in cols.items():
        if name in row or info["dflt_value"] is not None or not info["notnull"]:
            continue
        row[name] = 0 if info["type"].upper() in ("INTEGER", "REAL") else "x"
    conn.execute(
        f"INSERT INTO {table} ({','.join(row)}) "
        f"VALUES ({','.join('?' * len(row))})", tuple(row.values()))


def _seed_running(db_path, table, task_id, tmp_path):
    """种一条「切片作业正在跑」的行。

    DEM 侧连父行一起种：切片作业挂在 dem_tasks 下（唯一键 task_id），而
    start_tiling 只接受 completed 的下载任务。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if table == "dem_terrain_jobs":
        _insert(conn, "dem_tasks", {
            "id": task_id, "status": "completed", "name": "t",
            "output_path": str(tmp_path / "downloads")})
        _insert(conn, "dem_terrain_jobs", {
            "task_id": task_id, "status": "running",
            "output_dir": str(tmp_path / "out"), "maxzoom": 12})
    else:
        _insert(conn, "local_terrain_tasks", {
            "id": task_id, "status": "running", "name": "t", "maxzoom": 12,
            "output_path": str(tmp_path / "downloads"),
            "source_dir": str(tmp_path / "src"),
            "output_dir": str(tmp_path / "out")})
    conn.commit()
    conn.close()


def _status(db_path, table, id_col, task_id):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        f"SELECT status, error_message FROM {table} WHERE {id_col} = ?",
        (task_id,)).fetchone()
    conn.close()
    return (row["status"], row["error_message"]) if row else (None, None)


def _row_count(db_path, table, id_col, task_id):
    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ?", (task_id,)).fetchone()[0]
    conn.close()
    return n


# 两条切片管线的形状是逐字对应的：同名方法、同一组位置参数、同一个被 patch 的
# 切片器符号。差别只有表名与定位列（作业行挂在 dem_tasks 下，按 task_id 定位）。
PIPELINES = [
    ("src.services.dem_task_manager", "DemTaskManager", "dem_terrain_jobs", "task_id"),
    ("src.services.local_terrain_task_manager", "LocalTerrainTaskManager",
     "local_terrain_tasks", "id"),
]
PIPELINE_IDS = ["dem_terrain_jobs", "local_terrain_tasks"]


def _pipeline(module_name, class_name):
    mod = __import__(module_name, fromlist=[class_name])
    return mod, getattr(mod, class_name)(socketio=None)


def _run(mgr, task_id, tmp_path, stop_flag=None):
    mgr._run_tiling_job(task_id, tmp_path / "src", tmp_path / "out", 12, "", stop_flag)


def _tiler(**counts):
    def _fake(**kwargs):
        return dict({"total": 10, "rendered": 3, "failed": 0}, **counts)
    return _fake


# --------------------------------------------------------------------------
# 路 1：线程带着异常死掉，行还是 running
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_a_dying_tiling_thread_does_not_leave_the_row_running(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """兜底 except 里的 `conn = get_connection()` 自己抛 —— 它盖不住自己。"""
    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 21, tmp_path)
    mgr.active_tasks[21] = threading.current_thread()

    # 库在切片器炸掉的那一刻被锁住 —— 兜底 except 的第一句 `get_connection()`
    # 于是自己抛，把原来的异常连同「谁来写终态」一起带出线程。
    state = {"tiling_started": False}
    real_connect = mod.get_connection

    def _boom(**kwargs):
        state["tiling_started"] = True
        raise RuntimeError("GDAL 挂了")

    def _locked():
        if state["tiling_started"]:
            raise sqlite3.OperationalError("database is locked")
        return real_connect()

    monkeypatch.setattr(mod, "tile_dem_task_dir", _boom)
    monkeypatch.setattr(mod, "get_connection", _locked)

    with pytest.raises(sqlite3.OperationalError):
        _run(mgr, 21, tmp_path)

    status, error = _status(db, table, id_col, 21)
    assert status == "failed", (
        f'{table}: 异常穿透切片线程后行还停在 running —— start_tiling 会一直判'
        '「已在运行」，只有重启进程解得开')
    assert "运行中" in error and "GDAL 挂了" in error, (
        f'错误信息要说清发生了什么，实际 {error!r}')
    assert 21 not in mgr.active_tasks


# --------------------------------------------------------------------------
# 路 2：停止标志置位后**正常** return，而行没被删掉
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_the_stop_branch_compensates_a_surviving_row(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """delete_task_row 的 commit 失败分支回滚 DELETE 却不回滚停止标志 —— 行活了下来。"""
    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 22, tmp_path)
    mgr.active_tasks[22] = threading.current_thread()
    flag = threading.Event()
    flag.set()
    mgr.stop_flags[22] = flag

    monkeypatch.setattr(mod, "tile_dem_task_dir", _tiler())

    _run(mgr, 22, tmp_path, flag)  # 没有异常可捕：这正是那条路的形状

    assert _status(db, table, id_col, 22)[0] == "failed", (
        f'{table}: 停止标志置位后正常返回的切片线程没留下终态 —— '
        '行永久停在 running（删除 commit 失败那条路）')


# --------------------------------------------------------------------------
# 补偿必须闭嘴的两种局面
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_a_normally_completed_row_is_not_rewritten(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """正常收尾自己写好了 completed，这道网不能改写它，也不能塞 error_message。"""
    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 23, tmp_path)
    mgr.active_tasks[23] = threading.current_thread()

    monkeypatch.setattr(mod, "tile_dem_task_dir",
                        _tiler(total=4, rendered=4, failed=0, max_level=12))

    _run(mgr, 23, tmp_path)

    assert _status(db, table, id_col, 23) == ("completed", None)


@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_a_deleted_row_is_a_harmless_noop(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """删除成功那条常规路：行已经不在了，补偿是静默 no-op，不得把行写回来。

    顺带钉住「no-op 对别人也是 no-op」：另一个任务真的在跑，补偿不能顺手把它
    一起判失败（`WHERE` 少一个 id 谓词就是这个后果）。
    """
    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 24, tmp_path)
    _seed_running(db, table, 99, tmp_path)
    mgr.active_tasks[24] = threading.current_thread()
    flag = threading.Event()
    mgr.stop_flags[24] = flag

    def _delete_then_stop(**kwargs):
        # 复刻 delete_task 的成功路径：置停止标志 + 行真的没了。
        kwargs["params"].stop_flag.set()
        conn = sqlite3.connect(str(db))
        conn.execute(f"DELETE FROM {table} WHERE {id_col} = 24")
        conn.commit()
        conn.close()
        return {"total": 10, "rendered": 2, "failed": 0}

    monkeypatch.setattr(mod, "tile_dem_task_dir", _delete_then_stop)

    _run(mgr, 24, tmp_path, flag)

    assert _row_count(db, table, id_col, 24) == 0, '补偿把一条已删除的行写了回来'
    assert _status(db, table, id_col, 99)[0] == "running", '补偿碰了别人那一行'


@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_a_relaunched_job_is_not_stolen_by_a_late_exiting_thread(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """晚退的旧线程不能把新一轮 start_tiling 登记的行判失败。

    补偿只在「登记在册的就是自己」时做 —— 与 `_run_task` 的 finally 同一判据。
    """
    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 25, tmp_path)
    newcomer = threading.Thread(target=lambda: None)
    mgr.active_tasks[25] = newcomer  # 登记的不是当前线程
    flag = threading.Event()
    flag.set()

    monkeypatch.setattr(mod, "tile_dem_task_dir", _tiler())

    _run(mgr, 25, tmp_path, flag)

    assert _status(db, table, id_col, 25)[0] == "running", (
        f'{table}: 晚退的旧线程把新一轮运行的行判失败了')
    assert mgr.active_tasks[25] is newcomer, '不该摘掉别人的登记'


# --------------------------------------------------------------------------
# P2：搁死信息承诺的动作，入口必须真的接受
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module_name,class_name,table,id_col", PIPELINES, ids=PIPELINE_IDS)
def test_the_stranded_message_points_at_an_action_that_works(
        db, tmp_path, monkeypatch, module_name, class_name, table, id_col):
    """切片类说「可以重新开始切片」，而 start_tiling 的闸门确实收 failed 行。"""
    from src.services.task_cleanup import fail_stranded_running_task

    mod, mgr = _pipeline(module_name, class_name)
    _seed_running(db, table, 26, tmp_path)

    assert fail_stranded_running_task(table, 26) is True
    status, error = _status(db, table, id_col, 26)
    assert status == "failed"
    assert "重新开始切片" in error, f'切片类的出路写错了，实际 {error!r}'

    monkeypatch.setattr(type(mgr), "_run_tiling_job", lambda self, *a, **k: None)
    mgr.start_tiling(26)  # 承诺兑现：failed 行重切是通的

    assert _status(db, table, id_col, 26)[0] == "running"


def test_download_pipelines_do_not_promise_a_restart_that_is_refused(db):
    """P2：三条下载管线的 start/resume 只收 pending/paused —— 不能叫用户「重新开始」。

    失败是终态是有意的（见 TaskManager.start_task 的注释：重跑会把「它失败过」
    从历史里擦掉）。所以这条信息要说的是真正做得到的事：新建一个同样的任务。
    """
    from src.services.task_cleanup import fail_stranded_running_task

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _insert(conn, "tasks", {"id": 27, "status": "running"})
    conn.commit()
    conn.close()

    assert fail_stranded_running_task("tasks", 27) is True
    error = _status(db, "tasks", "id", 27)[1]
    assert "新建" in error, f'把用户指向一个 start_task 会拒绝的动作，实际 {error!r}'

    from src.services.task_manager import TaskManager

    with pytest.raises(ValueError):
        TaskManager(socketio=None).start_task(27)
