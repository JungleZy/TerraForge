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
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database") + extra_modules:
        sys.modules.pop(mod, None)
    db = importlib.import_module("core.database")
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
# 契约 1：终态记录绝不可被改写（兜底 except 的 WHERE 必须排除 'completed'）
# ---------------------------------------------------------------------------

_TERMINAL_GUARD_CASES = [
    ("map", "services.task_manager", "tasks"),
    ("dem", "services.dem_task_manager", "dem_tasks"),
    ("contour", "services.contour_task_manager", "contour_tasks"),
    ("local_terrain", "services.local_terrain_task_manager", "local_terrain_tasks"),
]


@pytest.mark.parametrize("label,module,table", _TERMINAL_GUARD_CASES,
                         ids=[c[0] for c in _TERMINAL_GUARD_CASES])
def test_failure_fallback_never_rewrites_a_completed_row(monkeypatch, tmp_path,
                                                         label, module, table):
    """四条管线的兜底 except 都必须把 'completed' 排除在 UPDATE 之外。

    直接对 SQL 断言而不是走完整执行路径：这条契约的实质就是那句 WHERE，
    走执行路径需要为每条管线各造一套下载/渲染桩，反而把契约埋掉。
    """
    import re
    src_path = module.replace(".", os.sep) + ".py"
    src = open(os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), src_path),
        encoding="utf-8").read()

    # 收集所有「把状态改成 failed」的 UPDATE 语句。结束符只认字符串定界符
    # （三引号或双引号）—— SQL 内部的单引号是 'failed'/'running' 这类字面量，
    # 拿它当结束符会把语句截断在 WHERE 中间。
    updates = re.findall(
        r"UPDATE\s+%s\s+SET\s+status\s*=\s*'failed'.*?(?=\"\"\"|'''|\")" % table,
        src, re.S | re.I)
    assert updates, f"{module} 里没找到置 failed 的 UPDATE"

    def _flat(sql):
        return re.sub(r"\s+", " ", sql).lower()

    unguarded = []
    for u in updates:
        flat = _flat(u)
        if " where " not in flat:
            # 无 WHERE 的（如 create 阶段的「全部上传失败」回滚）不在本契约内：
            # 那条路径上任务还没跑起来，不可能是终态。
            continue
        # 只看 WHERE 子句 —— SET 里的 `completed_at = ?` 也含 "completed"，
        # 拿整条语句判会把守卫缺失误判成已守卫（本断言最初就栽在这里）。
        # 去掉全部空白再比较：四条管线里 `status='running'` 和
        # `status = 'running'` 两种写法都有。
        where = flat.split(" where ", 1)[1].replace(" ", "")
        if ("completed" in where
                or "status='running'" in where
                or "status='pending'" in where):
            continue
        unguarded.append(u)
    assert not unguarded, (
        f"{module} 存在可能改写终态记录的 UPDATE（WHERE 既没排除 completed、"
        f"也没限定 status='running'）:\n" + "\n".join(unguarded)
    )


# ---------------------------------------------------------------------------
# 契约 2：thread.start() 失败必须回补状态
# ---------------------------------------------------------------------------

def test_dem_start_tiling_thread_failure_reverts_job_status(monkeypatch, tmp_path):
    db, (dtm,) = _fresh(monkeypatch, tmp_path, "services.dem_task_manager")
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
    db, (ctm,) = _fresh(monkeypatch, tmp_path, "services.contour_task_manager")
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
