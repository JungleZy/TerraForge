"""四条管线的共性契约（横向同步回归）。

审查报告里最高频的模式是「修复只做一条管线」：同一份逻辑被复制粘贴到四个
manager 里，每次修 bug 只改被报告的那一条，连回归测试也不横向同步 —— 于是
「已修」的那条永远绿，未修的三条永远没人测。

本文件按 (管线, 表名, 构造方式) 参数化钉住两条共性契约：

1. **终态记录绝不可被改写**（M1）：收尾 emit 抛异常时，已落库的 completed
   不能被兜底 except 改写成 failed。此前只有 contour 修了。
2. **thread.start() 失败必须回补状态**（L2）：commit(running) 与 start() 之间
   失败时，任务不得永久停在 running —— 那会同时挡住重新 start 和 delete。
   此前只有 map/dem 的 start_task 修了，三处 tiling/contour 入口没修。
"""

import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _BoomThread:
    """构造成功、start() 必炸 —— 模拟 OS 线程/内存耗尽。"""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("cannot spawn thread")

    def is_alive(self):
        return False


def _fresh(monkeypatch, tmp_path, *extra_modules):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database") + extra_modules:
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    return db, [importlib.import_module(m) for m in extra_modules]


def _status(db, table, task_id):
    conn = db.get_connection()
    try:
        row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (task_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 契约 1：终态记录绝不可被改写（兜底 except 不得动已经落库的终态）
# ---------------------------------------------------------------------------
#
# 每条：(标签, 模块, 表名, 管理器类, 入口, 注入点, 必须存活的终态)
#
# **注入点**：一个模块级函数名，被替换成「必炸」。它在被守卫的那个 `try` 里、
# 在任何终态写库**之前**被调用，所以一定会把执行流赶进兜底 except —— 也就是
# 本契约要测的那段代码。map/dem/contour 用 `resolve_stored_output_dir`
# （三条都在读完任务行之后立刻解析输出目录），local_terrain 的切片入口不读库，
# 用它真正的重活 `tile_dem_task_dir`。这模拟的是最常见的真实故障：库/盘出问题。
#
# **必须存活的终态**：只列该管线真的会写出来的终态。`completed_with_gaps` 目前
# 只有 map 有（缺块记账是 map 独有的），所以另外三条不列它 —— 测一个不可达的
# 状态只会制造假红。哪天 dem/contour/local_terrain 也长出缺块语义，除了在这里
# 补一项，还必须回去看它们那句 `NOT IN ('paused','completed')`：反向清单**不会**
# 自动排除新增的终态，这正是本文件存在的理由。
_TERMINAL_GUARD_CASES = [
    ("map", "src.services.task_manager", "tasks",
     "TaskManager", "_execute_task", "resolve_stored_output_dir",
     ("completed", "completed_with_gaps")),
    ("dem", "src.services.dem_task_manager", "dem_tasks",
     "DemTaskManager", "_execute", "resolve_stored_output_dir",
     ("completed",)),
    ("contour", "src.services.contour_task_manager", "contour_tasks",
     "ContourTaskManager", "_execute", "resolve_stored_output_dir",
     ("completed",)),
    ("local_terrain", "src.services.local_terrain_task_manager", "local_terrain_tasks",
     "LocalTerrainTaskManager", "_run_tiling_job", "tile_dem_task_dir",
     ("completed",)),
]


def _seed_terminal_row(db, table, task_id, status):
    """往任务表塞一行指定终态，只填 NOT NULL 且无默认值的列。

    刻意塞一行**残缺**的行（数值列 0、文本列 'x'）：本契约测的是兜底 except 的
    WHERE，不是执行体能不能跑通，行越简单越不会把契约埋进一堆桩里。
    """
    conn = db.get_connection()
    try:
        cols = list(conn.execute(f"PRAGMA table_info({table})"))
        values = {"id": task_id, "status": status}
        for col in cols:
            name, ctype, notnull, dflt = col["name"], col["type"], col["notnull"], col["dflt_value"]
            if name in values or dflt is not None or not notnull:
                continue
            values[name] = 0 if ctype.upper() in ("INTEGER", "REAL") else "x"
        conn.execute(
            f"INSERT INTO {table} ({','.join(values)}) "
            f"VALUES ({','.join('?' * len(values))})", tuple(values.values()))
        conn.commit()
    finally:
        conn.close()


def _drive_fallback(mod, mgr, entry, task_id, tmp_path):
    """跑一遍执行体，让注入的故障把它赶进兜底 except。异常照单吞下。

    各管线的兜底 except 收尾方式不同（map/local_terrain 吞、dem/contour 写完
    终态再 `raise`），而本契约只关心库里那一行 —— 谁抛谁不抛不是这条契约的事。
    """
    fn = getattr(mgr, entry)
    try:
        if entry == "_run_tiling_job":
            # 唯一不是协程、也不自己读库的入口：参数得手喂。
            fn(task_id, tmp_path / "src", tmp_path / "out", 12, "/terrain/base")
        else:
            asyncio.run(fn(task_id, None))
    except Exception:
        pass


@pytest.mark.parametrize("label,module,table,cls,entry,hook,survivors",
                         _TERMINAL_GUARD_CASES,
                         ids=[c[0] for c in _TERMINAL_GUARD_CASES])
def test_failure_fallback_never_rewrites_a_completed_row(monkeypatch, tmp_path,
                                                        label, module, table, cls,
                                                        entry, hook, survivors):
    """四条管线的兜底 except 都不得改写一条已经落库的终态记录。

    断言的是**行为**而不是 SQL 的写法。守卫怎么写都行，这三种形态现在同时存在：

    * 反向清单：`WHERE ... status NOT IN ('paused','completed')`（dem / contour）；
    * 精确态：  `WHERE ... status='running'`（local_terrain）；
    * 正向清单：`WHERE ... status IN (?,?,…)` 绑 `FAILABLE_STATE_VALUES`
      （= 活动态减 paused，map 的兜底改成了这一种）。

    早先这条用例直接正则匹配那句 WHERE 里有没有 'completed'。map 换成正向清单
    之后它就假红了：清单里一个终态都没有，守卫其实比以前更严，测的方法却看不懂
    新写法。所以改成真的跑一遍：给行写上终态 → 注入一个必炸的调用 → 断言那一行
    一个字节都没变。这样任何一种（乃至将来第四种）写法只要真的守住了就绿。

    `pending_decision` **不**在必须存活的名单里，而且这是有意的：它是活动态
    （`ACTIVE_TASK_STATES` 里有它），意思是「任务还在系统手里、等你决定」，不是
    终态。一轮跑崩了的任务把自己判成 failed 是对的。
    """
    db, (mod,) = _fresh(monkeypatch, tmp_path, module)
    mgr = getattr(mod, cls)(socketio=None)

    def _boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(mod, hook, _boom)

    for task_id, status in enumerate(survivors, start=101):
        _seed_terminal_row(db, table, task_id, status)
        _drive_fallback(mod, mgr, entry, task_id, tmp_path)
        assert _status(db, table, task_id) == status, (
            f"{module} 的兜底 except 把一条 {status} 的记录改写了 —— "
            f"终态记录被改写过一次就再也说不清任务到底成没成")


# ---------------------------------------------------------------------------
# 契约 2：thread.start() 失败必须回补状态
# ---------------------------------------------------------------------------

def test_dem_start_tiling_thread_failure_reverts_job_status(monkeypatch, tmp_path):
    db, (dtm,) = _fresh(monkeypatch, tmp_path, "src.services.dem_task_manager")
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = mgr.create_task({
        "name": "t", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "output_path": str(tmp_path / "downloads" / "dem"),
    })
    task_dir = tmp_path / "downloads" / "dem" / f"dem_task_{task_id}"
    task_dir.mkdir(parents=True)
    (task_dir / "a_dem.tif").write_bytes(b"x")
    # 只有下载完成的任务才允许切片
    conn = db.get_connection()
    try:
        conn.execute("UPDATE dem_tasks SET status='completed' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(dtm.threading, "Thread", _BoomThread)

    with pytest.raises(RuntimeError):
        mgr.start_tiling(task_id)

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM dem_terrain_jobs WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["status"] == "failed", (
        f"切片线程没起来却把 job 留在 {row['status']} —— 再次 start_tiling 会被"
        f"「已在运行」拒绝，delete 也被拒，只能重启进程")


def test_contour_start_task_thread_failure_reverts_status(monkeypatch, tmp_path):
    db, (ctm,) = _fresh(monkeypatch, tmp_path, "src.services.contour_task_manager")
    mgr = ctm.ContourTaskManager(socketio=None)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west, "
            "dataset, output_path, contour_interval, water, zoom_min, zoom_max, total_files) "
            "VALUES ('c', 'pending', 1, 0, 1, 0, 'upload', ?, 50, 0, 10, 12, 1)",
            (str(tmp_path / "downloads" / "dem"),))
        conn.commit()
        task_id = cur.lastrowid
    finally:
        conn.close()

    monkeypatch.setattr(ctm.threading, "Thread", _BoomThread)

    with pytest.raises(RuntimeError):
        mgr.start_task(task_id)

    status = _status(db, "contour_tasks", task_id)
    assert status != "running", f"start 失败不得卡在 running，实际: {status}"
    assert status == "paused"
    assert task_id not in mgr.active_tasks
    assert task_id not in mgr.stop_flags
