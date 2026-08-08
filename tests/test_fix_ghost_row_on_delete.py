"""删除运行中的任务之后，后台线程不得再把行「推回来」。

砍掉「取消」后删除是唯一的销毁动作，而且任何状态都能删（含 running）。四条
管线的防线各不相同：map 靠内存墓碑、contour 靠 UPDATE 的 rowcount、DEM 靠
emit 前重查行、本地地形靠 get_task 抛 ValueError。本文件钉的是后两条 ——
它们此前**一条测试都没有**，而且 DEM 那道闸门是副作用（重查行的本意是拿最新
数据），一次「改成复用缓存的行快照」的重构就会把它抹掉。

复活的具体形态：payload 是整行、里面 status='running'，前端收到一个既不在
时间流、也不在活动集里的 key（deleteTask 已经摘干净），走 prependStreamRow
把行插回去（static/js/tasks.js）；而停止后收尾直接 return、不再发任何终态
事件，那行就永久卡在「运行中」，只能刷新页面。
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


class _Sock:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload))


def _setup(monkeypatch, tmp_path, module):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    mod = fresh_import(monkeypatch, module)
    return db, mod


def _delete_row(db, table, task_id):
    """把「删除运行中的任务」在库里留下的可观察状态复现出来：行没了。

    不走 task_deletion.delete_task_row —— 它按 manager.active_tasks 判线程是否
    还活着来选同步删还是后台收尾，而这里的执行体是直跑的、没有登记线程，走进去
    只会命中快路径。对后台线程来说删除留下的东西就是「行没了」这一件事。
    """
    conn = db.get_connection()
    try:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DEM
# ---------------------------------------------------------------------------

def _seed_dem_task(db, output_path, granules):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, ?, 0, 0)
            """,
            (str(output_path), len(granules)),
        )
        task_id = cur.lastrowid
        for granule_id in granules:
            cur.execute(
                "INSERT INTO dem_files (task_id, granule_id, status, retry_count) "
                "VALUES (?, ?, 'pending', 0)",
                (task_id, granule_id),
            )
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_deleted_dem_task_emits_no_further_progress(monkeypatch, tmp_path):
    """行删掉之后，颗粒回调不许再推一发 task_progress。

    _maybe_emit 每次都重新 SELECT 整行、`if not row: return`。那个 SELECT 的
    本意是拿最新计数，挡住幽灵行只是顺带 —— 这条用例把顺带变成契约：谁把它
    换成复用 _execute 开头那份 `task` 快照（contour 的 base_payload 就是这么
    翻车的），这里必须红。
    """
    db, dtm = _setup(monkeypatch, tmp_path, "src.services.dem_task_manager")
    # 节流窗口不是本用例的被测对象：留着的话删除之后那一发会被 1s 窗口吞掉，
    # 用例就变成在测节流、探针也照样绿。
    monkeypatch.setattr(dtm, "_PROGRESS_EMIT_MIN_INTERVAL", 0.0)

    sock = _Sock()
    mgr = dtm.DemTaskManager(socketio=sock)
    task_id = _seed_dem_task(db, tmp_path / "out", ["G00.tif", "G01.tif"])

    mark = {}

    async def fake_download_files(dataset, granules, output_dir, progress_callback,
                                  stop_flag, bytes_callback=None):
        await progress_callback(granules[0], "completed", None, 1024)  # 删除前的正常一发
        _delete_row(db, "dem_tasks", task_id)
        mark["at"] = len(sock.events)
        # 下载协程不会因为行没了就当场停下 —— 剩下的颗粒照跑，回调照来，
        # 幽灵行就是从这儿发出去的
        await bytes_callback(granules[1], 4 * 1024 * 1024)
        await progress_callback(granules[1], "completed", None, 1024)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    assert mark["at"] > 0, "删除之前必须先有正常推送，否则这条用例没测到闸门"
    assert sock.events[mark["at"]:] == [], (
        f"行已删除，之后不得再有任何推送: {sock.events[mark['at']:]}")


# ---------------------------------------------------------------------------
# 本地地形
# ---------------------------------------------------------------------------

def _seed_local_terrain_task(db, tmp_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('t', 'running', ?, ?, ?, 12)
            """,
            (str(tmp_path / "out"), str(tmp_path / "src"), str(tmp_path / "tiles")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_deleted_local_terrain_task_emits_nothing_and_logs_nothing(
        monkeypatch, tmp_path, caplog):
    """行没了要**静默**返回：既不推送，也不记一条假警报。

    「不推送」这一半是过度确定的（就算把显式检查拿掉，get_task 抛的 ValueError
    也会被宽 except 吞掉），单独断言它是重言式。真正钉得住的是**机制**：行没了
    是正常的并发结果，不是异常，不该走异常路径、更不该在日志里留一条
    "Failed to emit ..." —— 那是会把人引去查根本不存在的故障的假线索。

    但下面那句 `assert sock.events == []` **不要删**：它只对「拿掉显式
    except」这一种改法是重言式，对另外两种是唯一的防线 —— get_task 改成返回
    None（TypeError 那条路就没了）、以及 payload 换成缓存的行快照。两句合起来
    才是「静默返回」的完整契约。
    """
    db, ltm = _setup(monkeypatch, tmp_path, "src.services.local_terrain_task_manager")

    sock = _Sock()
    mgr = ltm.LocalTerrainTaskManager(socketio=sock)
    task_id = _seed_local_terrain_task(db, tmp_path)
    _delete_row(db, "local_terrain_tasks", task_id)

    with caplog.at_level(logging.WARNING, logger=ltm.__name__):
        mgr._emit_progress(task_id)

    assert sock.events == [], f"行已删除，不得推送整行（里面还写着 running）: {sock.events}"
    assert caplog.records == [], (
        "并发删除是正常结果，不该记警告: "
        f"{[r.getMessage() for r in caplog.records]}")


def test_local_terrain_emit_failure_is_still_logged(monkeypatch, tmp_path, caplog):
    """反过来的一半：真故障不能被上面那道静默口子一起吞掉。

    没有这条，把整个 try/except 删成裸调用也能让上面那条绿 —— 那样广播层出问题
    时就彻底没声了。
    """
    db, ltm = _setup(monkeypatch, tmp_path, "src.services.local_terrain_task_manager")

    class _BoomSock:
        def emit(self, event, payload=None):
            raise RuntimeError("socket is down")

    mgr = ltm.LocalTerrainTaskManager(socketio=_BoomSock())
    task_id = _seed_local_terrain_task(db, tmp_path)

    with caplog.at_level(logging.WARNING, logger=ltm.__name__):
        mgr._emit_progress(task_id)

    assert any("socket is down" in r.getMessage() for r in caplog.records), (
        f"广播失败必须留下日志: {[r.getMessage() for r in caplog.records]}")


def test_local_terrain_row_lookup_failure_never_escapes(monkeypatch, tmp_path, caplog):
    """取行本身失败（database is locked 等）只许记日志，不许往外抛。

    唯一调用点 start_tiling:389 站在一个没有补偿的位置上：状态已 commit 成
    running、线程已登记进 active_tasks/stop_flags，而 L2 的回补块（清登记 +
    置 failed）要到下一行 th.start() 才开始。异常从 :389 逃出去谁也接不住，
    留下的是一个行停在 running、登记里挂着永不启动的线程、路由却返 500 的
    任务 —— 可恢复，但不该发生。

    这条挡的是把「行没了」收窄成 ValueError 时顺手丢掉的那部分健壮性。
    """
    import sqlite3

    db, ltm = _setup(monkeypatch, tmp_path, "src.services.local_terrain_task_manager")

    sock = _Sock()
    mgr = ltm.LocalTerrainTaskManager(socketio=sock)
    task_id = _seed_local_terrain_task(db, tmp_path)

    def boom(_task_id):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(mgr, "get_task", boom)

    with caplog.at_level(logging.WARNING, logger=ltm.__name__):
        mgr._emit_progress(task_id)   # 不抛就是通过

    assert sock.events == []
    assert any("database is locked" in r.getMessage() for r in caplog.records), (
        f"取行失败必须留下日志，不能静默丢: {[r.getMessage() for r in caplog.records]}")
