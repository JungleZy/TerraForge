"""删除任务的两条路径：没在跑就同步删，在跑就置停止标志 + 后台收尾。

砍掉「取消」之后，删除是唯一的销毁动作，必须任何状态都能点。而四条管线都有
一段分钟级的 GDAL 阻塞区（拼接 / warp / 建金字塔），中途打不断 —— 所以「在
HTTP 请求里等线程退出」不可行，只能行立即消失、产物后台收尾。
"""

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

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


class _DeleteSpyConn:
    """包住真连接，只为在 `DELETE FROM tasks` 发出的【那一刻】拍一张快照。

    删除有两条顺序契约 —— 墓碑先于删行、缓存失效后于删行 —— 都只能在 DELETE
    那一刻观察。事后在回调里看只能证明「最终做了」，会把顺序搞反的实现放过去。
    """

    def __init__(self, conn, on_delete):
        self._conn = conn
        self._on_delete = on_delete

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("DELETE FROM TASKS"):
            self._on_delete()
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _spy_on_delete(monkeypatch, td, on_delete):
    """让 td 拿到的连接在发 DELETE 时先回调 on_delete。

    模块导入时就把 get_connection 绑进了自己的命名空间，所以换 td 上的名字即可。
    """
    real_get_connection = td.get_connection
    monkeypatch.setattr(
        td, "get_connection",
        lambda: _DeleteSpyConn(real_get_connection(), on_delete))


def _join_cleanup_thread(task_id=1, timeout=10):
    """等 delete_task_row 起的后台收尾线程真正跑完。

    不等有两个后果：后台那半段（删产物 / 销账 / 摘墓碑）一条也没被断言；而且
    线程会在 monkeypatch teardown 之后才去 get_connection()，打到真实库上 ——
    典型的只在 CI 偶发的污染源。
    """
    for t in threading.enumerate():
        if t.name == f"DeleteCleanup-{task_id}":
            t.join(timeout=timeout)


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


def _pending_paths(db):
    conn = db.get_connection()
    try:
        return [r["path"] for r in
                conn.execute("SELECT path FROM pending_deletions ORDER BY id")]
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


def test_running_task_defers_files_then_background_finishes_the_job(monkeypatch, tmp_path):
    """后台路径两段都要验：行当场消失 + 立即返回，之后后台真的把尾收完。

    只验前半段的话，把 `_background_cleanup` 整个改成 `return` 也照样绿 ——
    后台收尾（删产物 / 销账 / 摘墓碑）正是这个模块的头号卖点。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)
    tomb = set()

    started = time.monotonic()
    out = td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                             artifact_dir=art, tombstone=tomb)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"删除运行中任务不得阻塞等线程，实际 {elapsed:.1f}s"
    assert out.row_deleted is True
    assert out.files_deferred is True, "在跑的任务产物必须延后删"
    assert out.files_removed is None, "延后时不得给出 files_removed（还没删）"
    assert not _row_exists(db), "行必须当场消失"
    assert mgr.stop_flags[1].is_set(), "必须置停止标志，否则线程不会收工"
    # 此刻后台必定还堵在 join 上（假线程要等 release），所以这条是确定性的
    assert art.exists(), "延后期间产物必须还在 —— 不然就不叫延后"
    assert tomb == {1}, "线程没死透之前墓碑不许摘"

    # 产物线索必须落进清单 —— 进程被强杀时靠它补删
    assert _pending_paths(db) == [str(art)]

    release.set()
    th.join(timeout=10)
    _join_cleanup_thread(1)

    assert not art.exists(), "线程收工后后台必须把产物删掉"
    assert _pending_paths(db) == [], "删干净了就要销账，否则每次启动重扫"
    assert tomb == set(), "线程死透了才摘墓碑 —— 此刻必须已摘"


def test_registered_but_not_yet_started_thread_takes_the_background_path(
        monkeypatch, tmp_path):
    """线程「登记了但还没 start()」的那段窗口必须算在跑。

    四条管线的 start_* 都是：锁内提交 status='running' + `active_tasks[id]=th`，
    出锁之后才 `th.start()`。`Thread.is_alive()` 在 start() 之前是 False，只看它
    就会把这段窗口判成「没在跑」而走快路径 —— 不置停止标志、不写墓碑、**同步
    rmtree**、也不记 pending_deletions。

    本地地形是最重的受害者：`_run_tiling_job` 全程不回查行，上传的 DEM 被删掉后
    切片照跑到底、把目录重新建出来写满瓦片，而清单是空的，
    `_sweep_pending_deletions` 只认清单 —— 留下 GB 级、永远无人认领的孤儿目录。

    分支之前 contour / local_terrain 的 delete_task 除 is_alive() 外还查了 DB
    `status=='running'`，那条判据恰好封死这个窗口；共享助手把它丢了。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    # 关键：登记进 active_tasks 但【不】 start()，正是 start_* 出锁前的状态
    th = threading.Thread(target=lambda: None, daemon=True)
    mgr = _FakeManager(thread=th)
    tomb = set()

    out = td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                             artifact_dir=art, tombstone=tomb)

    assert out.files_deferred is True, "还没 start() 的线程马上就要跑，产物必须延后删"
    assert out.files_removed is None, "延后时不得给出 files_removed"
    assert art.exists(), "同步 rmtree 会被随后启动的线程把目录重建出来写满，成为孤儿"
    assert mgr.stop_flags[1].is_set(), "不置停止标志，线程起来后会一路跑到底"
    assert _pending_paths(db) == [str(art)], "清单是孤儿目录唯一的线索"
    assert tomb == {1}, "线程即将启动，进度批次仍要靠墓碑短路"

    _join_cleanup_thread(1)


def test_late_started_thread_still_gets_its_artifacts_cleaned(monkeypatch, tmp_path):
    """「登记了但还没 start()」这一支的后台收尾必须真的收完尾。

    分流判据（is_alive() or ident is None）把这段窗口正确地送进后台路径，但
    `Thread.join()` 对一个从未 start() 的线程直接抛
    `RuntimeError: cannot join thread before it is started` —— 整个
    `_background_cleanup` 被 except 接住提前退出，产物一个没删、清单没销账、
    墓碑没摘。实测日志：`Background cleanup for task 1 failed: cannot join
    thread before it is started`。

    这里把等 start 的上限调得远大于测试自己 start() 的延迟，让「先等到 ident
    再 join」成为确定性路径，不靠调度运气。
    """
    db, td = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(td, "_START_WAIT_SECONDS", 30.0)
    _seed(db, status="running")
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    th = threading.Thread(target=lambda: None, daemon=True)
    mgr = _FakeManager(thread=th)
    tomb = set()

    out = td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                             artifact_dir=art, tombstone=tomb)
    assert out.files_deferred is True

    # 真实管线里这就是 start_* 出锁后的下一条语句
    th.start()
    th.join(timeout=10)
    _join_cleanup_thread(1)

    assert not art.exists(), "线程收工后产物必须被删掉，不能等下次启动补删"
    assert _pending_paths(db) == [], "删干净了就要销账"
    assert tomb == set(), "线程死透了，墓碑必须摘"


def test_never_started_thread_still_gets_its_artifacts_cleaned(monkeypatch, tmp_path):
    """线程永远不会启动时也要走到「删产物 + 销账 + 摘墓碑」，不能静默跳过。

    这不是假想：四条管线的 `th.start()` 都带 L2 回补块，
    `RuntimeError: can't start new thread` 时会把状态回退并清掉 active_tasks
    —— 那个线程对象再也不会启动。等不到就按「没启动 = 不会再写盘」直接删，
    否则 GB 级的本地地形目录要挂到下次进程启动才释放。
    """
    db, td = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(td, "_START_WAIT_SECONDS", 0.02)
    _seed(db, status="running")
    art = tmp_path / "downloads" / "task_1"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    th = threading.Thread(target=lambda: None, daemon=True)
    mgr = _FakeManager(thread=th)
    tomb = set()

    td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                       artifact_dir=art, tombstone=tomb)
    _join_cleanup_thread(1)

    assert not art.exists(), "线程不会启动了，产物没有任何理由留着"
    assert _pending_paths(db) == [], "删干净了就要销账"
    assert tomb == set(), "线程永不启动 = 永不写进度，墓碑必须摘，否则永久泄漏一个 int"


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
    _spy_on_delete(monkeypatch, td,
                   lambda: seen.__setitem__("tombstoned_at_delete", 1 in tomb))

    td.delete_task_row(manager=mgr, task_id=1, table="tasks", artifact_dir=None,
                       tombstone=tomb)

    assert seen["tombstoned_at_delete"] is True
    release.set()
    th.join(timeout=10)
    _join_cleanup_thread(1)


def test_on_row_gone_runs_synchronously_after_delete_on_fast_path(monkeypatch, tmp_path):
    """静态路由缓存失效：必须同步执行，且必须在行删掉【之后】。

    丢给后台的话，已删任务的瓦片在缓存失效前仍能被访问到；反过来抢在 DELETE
    之前清缓存也不行 —— 那段窗口里并发请求会把「存在」重新灌回缓存，等于没清。
    所以观察点和墓碑用例一样落在 DELETE 那一刻，只断言「调过了」会把顺序反过来
    的实现放过去。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    calls = []
    seen = {}
    _spy_on_delete(monkeypatch, td,
                   lambda: seen.__setitem__("calls_at_delete", len(calls)))

    td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                       artifact_dir=None, on_row_gone=lambda: calls.append(1))

    assert seen["calls_at_delete"] == 0, "缓存失效不能抢在 DELETE 之前"
    assert calls == [1], "必须同步执行，不能丢给后台"


def test_missing_row_reports_not_deleted(monkeypatch, tmp_path):
    """行本来就不在（并发双删）时如实返回 False，不抛。"""
    db, td = _setup(monkeypatch, tmp_path)

    out = td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                             artifact_dir=None)

    assert out.row_deleted is False


def test_join_timeout_keeps_the_tombstone(monkeypatch, tmp_path):
    """join 超时说明线程还活着 —— 这一刻【不能】摘墓碑。

    这一支刚刚证明了 worker 还在 GDAL 阻塞区里（模块自己的注释写着阻塞区是
    「分钟级到数十分钟」，600s 就在这个区间里面）。它出来后 flush 进度仍要靠
    墓碑短路，否则拿已删的 task_id 撞外键，每次 flush 再炸一次直到进程重启。
    产物有 pending_deletions 兜底，墓碑没有第二道。
    """
    db, td = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(td, "_JOIN_TIMEOUT_SECONDS", 0.05)

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    tomb = {1}
    try:
        td._background_cleanup(1, th, None, tomb)

        assert th.is_alive(), "前提没成立：超时时线程必须还活着"
        assert tomb == {1}, "线程还活着时摘墓碑 = 进度批次撞外键，且永不自愈"
    finally:
        release.set()
        th.join(timeout=10)


def test_tombstone_is_released_when_the_delete_transaction_blows_up(monkeypatch,
                                                                    tmp_path):
    """墓碑写了但后台线程没接手成功时必须摘掉，否则永久泄漏。

    墓碑写入到 Thread.start() 之间有四个可抛点：INSERT / DELETE / commit（都可能
    database is locked）以及 start() 的「can't start new thread」。抛出去而不摘，
    留下的是行还在（事务回滚）+ 停止标志已置 + 进度写入被永久短路，用户看到任务
    卡在 running 且进度不动，只能重启进程。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)
    tomb = set()

    def blow_up():
        raise sqlite3.OperationalError("database is locked")

    _spy_on_delete(monkeypatch, td, blow_up)

    with pytest.raises(sqlite3.OperationalError):
        td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                           artifact_dir=None, tombstone=tomb)

    assert tomb == set(), "抛出前必须摘墓碑，否则进度写入被永久短路"
    assert _row_exists(db), "前提没成立：事务应当回滚，行还在"

    release.set()
    th.join(timeout=10)


def test_second_delete_failing_must_not_strip_the_first_deletes_tombstone(
        monkeypatch, tmp_path):
    """墓碑是无引用计数的 set —— 只有【真的是自己写进去的】才有资格回滚它。

    同一个运行中任务收到第二发 DELETE 时 `tombstone.add()` 是幂等的，但
    `tombstoned=True` 照置。第二发若在之后任何一点抛出（commit / Thread.start()，
    典型是另一连接持写事务撞上 database is locked），except 就会 discard 掉
    **第一发还在用的**墓碑。

    后果不可自愈：第一发的 worker 还活着，墓碑没了之后它的进度批次拿已删的
    task_id 去 INSERT task_tiles 撞外键，每次 flush 再炸一次直到进程重启
    （见 _background_cleanup 里 join 超时那一支的注释）。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)
    tomb = set()

    td.delete_task_row(manager=mgr, task_id=1, table="tasks", artifact_dir=None,
                       tombstone=tomb)
    assert tomb == {1}, "前提没成立：第一发应当写下墓碑"

    # 第二发落在同一个仍在跑的任务上，并在 DELETE 处撞 database is locked
    def blow_up():
        raise sqlite3.OperationalError("database is locked")

    _spy_on_delete(monkeypatch, td, blow_up)

    with pytest.raises(sqlite3.OperationalError):
        td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                           artifact_dir=None, tombstone=tomb)

    assert tomb == {1}, "第二发没写过这块墓碑，无权摘 —— 第一发的 worker 还活着"

    release.set()
    th.join(timeout=10)
    _join_cleanup_thread(1)


def test_queue_and_delete_commit_or_roll_back_together(monkeypatch, tmp_path):
    """记清单与删行必须在【同一个事务】里 —— 中间炸掉两边一起回滚。

    这条不变式管的是「进程在两者之间挂掉会不会留下行没了、产物线索也没了」，
    与两条语句谁先谁后无关（DELETE 现在排在前面，是因为闸门要拿它的 rowcount，
    防「行不存在还去 rmtree」）。

    探针：让 _queue_pending_deletion 先真的把清单写进去、再抛 —— 此刻 DELETE
    已经执行过、INSERT 也执行过，两条都还没 commit。异常传出后三件事都要成立。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db, status="running")

    release = threading.Event()
    th = threading.Thread(target=release.wait, kwargs={"timeout": 10}, daemon=True)
    th.start()
    mgr = _FakeManager(thread=th)
    tomb = set()

    real_queue = td._queue_pending_deletion

    def queue_then_blow_up(conn, artifact_dir):
        real_queue(conn, artifact_dir)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(td, "_queue_pending_deletion", queue_then_blow_up)

    with pytest.raises(sqlite3.OperationalError):
        td.delete_task_row(manager=mgr, task_id=1, table="tasks",
                           artifact_dir=str(tmp_path / "downloads" / "task_1"),
                           tombstone=tomb)

    assert _row_exists(db), "DELETE 没提交就炸 —— 行必须随事务一起回滚"
    assert _pending_paths(db) == [], "清单写入必须和删行一起回滚，不能单独留下"
    assert tomb == set(), "抛出前必须摘墓碑，否则进度写入被永久短路"

    release.set()
    th.join(timeout=10)


def test_non_absolute_artifact_dir_is_refused(monkeypatch, tmp_path):
    """相对路径不许进护栏 —— 它会按【进程 cwd】解释，'' 和 '.' 能删掉 cwd。

    同一张 pending_deletions 表的另一个消费者
    (task_cleanup._sweep_pending_deletions) 有同样一道卫兵；两边判据必须对称，
    否则迟早从没卫兵的这一侧漏进去。拒收后当作「没要求删产物」。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    removed = []
    monkeypatch.setattr(td, "remove_task_dir_if_safe",
                        lambda p: removed.append(p) or True)

    out = td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                             artifact_dir=".")

    assert removed == [], "相对路径绝不能喂给护栏"
    assert out.row_deleted is True, "拒收产物路径不影响删行"
    assert out.files_removed is None and out.files_deferred is False


def test_tilde_artifact_dir_is_expanded_at_the_entry(monkeypatch, tmp_path):
    """喂护栏的和拿去 exists() 销账的必须是【同一个】展开后的 target。

    护栏内部自己会 expanduser，但助手若留着没展开的 `~/...` 去 exists()，那个
    判断恒为 False，`_background_cleanup` 里「删不干净就别销账」那一支会对整类
    ~ 路径静默失效 —— 正是 pending_deletions 这张表要防的事。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    seen = []
    monkeypatch.setattr(td, "remove_task_dir_if_safe",
                        lambda p: seen.append(p) or True)

    td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                       artifact_dir="~/map-download-probe/task_1")

    assert seen == [Path.home() / "map-download-probe" / "task_1"]
