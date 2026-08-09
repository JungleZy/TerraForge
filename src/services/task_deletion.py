"""删除任务的共用实现 —— 四条管线（map / DEM / 等高线 / 本地地形）都走这里。

## 为什么删除要分两条路径

砍掉「取消」之后删除是唯一的销毁动作，必须任何状态都能点。但四条管线都有一段
分钟级到数十分钟的 GDAL 同步阻塞区（map 的单 zoom 拼接、等高线的 warp、地形的
多幅 DEM 合并 + 建金字塔），中途完全打不断 —— 让回调抛异常会让 GDAL 把产物删掉
并判整个作业失败，三处独立注释都实测记录过这个坑。

所以「在 HTTP 请求里 join 线程再删」不可行：请求要挂几十秒到几十分钟，Flask
worker 被占死，用户重复点击还会 double-delete。

分流：
  - **快路径**（任务没在跑）—— 同步删行 + 同步删产物。绝大多数删除走这条，
    `files_removed` 的既有语义（删没删成）原样保住。
  - **后台路径**（线程还活着）—— 置停止标志 → 写墓碑 → 同一事务里删行 + 记
    pending_deletions → 立即返回 → daemon 线程 join 完再删产物。
    用户视角是「点了就没了」，后台收尾不冒出来变成又一个状态。

## 为什么记 pending_deletions 与删行必须在同一个事务里

约束是**同一事务**，不是两条语句谁先谁后：一次 commit，进程在中间被强杀两边一起
回滚，不会留下「行没了、产物线索也没了」。清单的另一端是启动清扫
（task_cleanup._sweep_pending_deletions），它会在下次启动时补删。

当前顺序是先 DELETE、后记清单，而且**必须**是这个顺序：闸门要拿 DELETE 的
rowcount —— 行本来就不存在时一片磁盘都不能碰，否则删一个不存在的 task_id 会
「返回 404 的同时把同名残留目录 rmtree 掉」（见 delete_task_row 里那道闸）。
把 DELETE 挪回记清单之后就会把那个静默真删放回来。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from src.core.database import get_connection
from src.services.task_cleanup import remove_task_dir_and_confirm

logger = logging.getLogger(__name__)

# 后台收尾线程等下载/切片线程退出的上限。超时不是错误：产物线索已经在
# pending_deletions 里，下次启动的清扫会接着删。这个上限只是防止 daemon 线程
# 无限挂着 —— GDAL 阻塞区没有可靠的时间上界，等不到就交给下次启动。
_JOIN_TIMEOUT_SECONDS = 600

# 后台收尾等「已登记但还没 start()」的线程真正启动的上限，以及自旋间隔。
# 为什么需要它：分流判据把 `active_tasks[id]=th` 与 `th.start()` 之间那段窗口
# 判成「在跑」（走后台路径），但 `Thread.join()` 对从未 start() 的线程直接抛
# RuntimeError —— 后台收尾会整个提前退出，产物一个都不删。
# 上限取得很小：那段窗口在真实管线里只隔一条语句（微秒级），100ms 是三个数量级
# 的余量；等不到就说明线程再也不会启动（`th.start()` 抛异常时四条管线的 L2 块
# 会把登记清掉），继续等只是白占一个 daemon 线程。
_START_WAIT_SECONDS = 0.1
_START_POLL_SECONDS = 0.005

# 表名直接拼进 SQL,只接受这四张任务表的字面量。四条管线全都传字面量,今天注不
# 进来 —— 但「调用方传字面量」此前只是一句 docstring,而 docstring 不会在有人
# 把某个 ?table= 透传下来的那天报错。与 task_cleanup._STRANDED_TASK_TABLES 同
# 一套做法;这里多一张 local_terrain_tasks:本函数服务四条管线,那个助手只服务
# 有 `_run_task` 的三条。
_DELETABLE_TASK_TABLES = frozenset(
    {'tasks', 'dem_tasks', 'contour_tasks', 'local_terrain_tasks'})


class DeleteOutcome(NamedTuple):
    """删除结果。

    files_removed 三态：True=目录确实不在了 / False=没删掉（护栏拦下、删除出错，
    或 Windows 上文件被占导致 rmtree(ignore_errors=True) 静默失败）/ None=没要求
    删，或者要求了但延后到后台（此时 files_deferred 为 True）。

    False 那一档【不是】「护栏拦下」的同义词：判据统一走
    task_cleanup.remove_task_dir_and_confirm 的 removed 字段，理由见 P1#6。
    """
    row_deleted: bool
    files_removed: Optional[bool]
    files_deferred: bool


def _queue_pending_deletion(conn, artifact_dir: Path) -> None:
    """把产物目录记进待删清单。与删行在同一事务里，调用方负责 commit。

    INSERT OR IGNORE 配合 path 列的 UNIQUE 约束做幂等：重复入队没有意义。
    """
    conn.execute(
        "INSERT OR IGNORE INTO pending_deletions (path) VALUES (?)",
        (str(artifact_dir),),
    )


def _clear_pending_deletion(artifact_dir: Path) -> None:
    """产物删成功后销账。失败只记日志 —— 清单留着，下次启动补删。"""
    try:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pending_deletions WHERE path = ?",
                         (str(artifact_dir),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to clear pending deletion for {artifact_dir}: {e}")


def _wait_until_started(thread: threading.Thread) -> bool:
    """有界自旋等 `thread.ident` 出现，返回「这个线程 start() 过」。

    ident 是唯一能区分「还没 start()」和「跑完死了」的属性（两种情况
    `is_alive()` 都是 False）：start() 前为 None，start() 之后恒非 None。
    """
    if thread.ident is not None:
        return True
    deadline = time.monotonic() + _START_WAIT_SECONDS
    while thread.ident is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(_START_POLL_SECONDS)
    return True


def _remove_artifacts_and_settle(artifact_dir: Path) -> bool:
    """删产物目录，按结果销 pending_deletions 的账，返回「目录确实不在了」。

    两条删除路径（快路径、后台收尾）共用这一份判据。以前只有后台那条在调用点
    后面补了 `and not dir.exists()`，快路径直接把护栏的返回值当结果 —— 而护栏
    内部是 `rmtree(ignore_errors=True)`，Windows 上任意一个文件被占就静默失败
    却照样返回 True。后果是接口回 `files_removed: true`、整个瓦片金字塔留在盘
    上、且清单里没有行，启动清扫永远收不回（2026-08-08 评审 P1#6）。

    销账的两种情形：
      - removed：真删干净了，清单上的线索没用了。
      - not eligible：越界路径永远删不掉，留着只会每次启动重试并刷一条 warning。
    剩下那一种（可删但没删掉）**保留清单行**，下次启动补删 —— 那正是这张表
    存在的理由。
    """
    outcome = remove_task_dir_and_confirm(artifact_dir)
    if outcome.removed or not outcome.eligible:
        _clear_pending_deletion(artifact_dir)
    return outcome.removed


def _background_cleanup(task_id: int, thread: threading.Thread,
                        artifact_dir: Optional[Path],
                        tombstone: Optional[set]) -> None:
    """等线程收工，然后删产物、销账、摘墓碑。全程 best-effort。

    「收工」有两种：线程跑完死了，或者线程压根没 start()（分流判据故意把那段
    窗口算作在跑，见 delete_task_row）。两种都要走完整收尾 —— 唯一不收尾的是
    join 超时那一支，因为那时线程还活着、还在写盘。

    摘墓碑的唯一前提是「线程不会再写进度」—— 见 worker_done。
    """
    worker_done = False
    try:
        if not _wait_until_started(thread):
            # 等不到 start() —— 按「没启动 = 不会再写盘」处理，和线程跑完收工
            # 走同一条收尾（删产物 + 销账 + 摘墓碑）。这一支必须真的收尾：
            # 直接 return 会把 GB 级的本地地形目录挂到下次进程启动才释放。
            # 墓碑在这一支摘是安全的：从未启动的线程不会有进度批次去 INSERT
            # task_tiles，没有需要短路的写入。
            logger.info(
                f"Task {task_id}: worker never started within "
                f"{_START_WAIT_SECONDS}s; cleaning artifacts now")
        else:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                # 多半卡在 GDAL 阻塞区里。产物线索还在清单上，下次启动会补删。
                #
                # 墓碑【不能】在这一支摘：这里刚刚证明了线程还活着，它走出阻塞
                # 区后的进度批次仍要靠墓碑短路，否则会拿已删的 task_id 去 INSERT
                # task_tiles 撞外键，然后每次 flush 再炸一次直到进程重启。产物有
                # pending_deletions 兜底，墓碑没有第二道 —— 摘早了没人能补。
                # 代价对比：泄漏一个 int，对上一条永不自愈的外键失败链。
                logger.warning(
                    f"Task {task_id}: worker still running after "
                    f"{_JOIN_TIMEOUT_SECONDS}s; leaving artifact cleanup to the "
                    f"startup sweep")
                return
        worker_done = True
        if artifact_dir is not None:
            _remove_artifacts_and_settle(artifact_dir)
    except Exception as e:
        # join 本身抛出时 worker_done 还是 False —— 线程状态未知就按「还活着」
        # 保守处理，同样不摘墓碑。
        logger.warning(f"Background cleanup for task {task_id} failed: {e}")
    finally:
        if worker_done and tombstone is not None:
            tombstone.discard(task_id)


def delete_task_row(
    *,
    manager,
    task_id: int,
    table: str,
    artifact_dir: Optional[Path],
    tombstone: Optional[set] = None,
    on_row_gone: Optional[Callable[[], None]] = None,
) -> DeleteOutcome:
    """删掉任务行，并按「线程还活着吗」分流产物清理。

    Args:
        manager: 持有 `_state_lock` / `active_tasks` / `stop_flags` 的任务管理器。
        table: 任务表名。它直接拼进 SQL，只接受 _DELETABLE_TASK_TABLES 里的字面量，
            传别的抛 ValueError。
        artifact_dir: 产物目录；None 表示调用方没要求删产物。非绝对路径会被拒收
            并当作 None 处理（理由见函数体内的卫兵注释）。删行没命中任何行时同样
            当作 None —— 产物删除以「行真的被删掉了」为前提，同上。
        tombstone: 只有 map 传（见设计文档 D-C）。运行期有 INSERT 的管线才需要，
            用来让进度批次在父行消失后短路，避开外键 IntegrityError。
        on_row_gone: 行删掉后**同步**执行的回调，用于清静态路由的存在性缓存。
            不能丢给后台 —— 否则已删任务的瓦片在缓存失效前仍能被访问到。
    """
    if table not in _DELETABLE_TASK_TABLES:
        raise ValueError(f'delete_task_row: 未知任务表 {table!r}')

    if artifact_dir is not None:
        # expanduser 在这里做一次，并且【同一个 target】既喂护栏又拿去
        # exists()：护栏内部自己会 expanduser，拿没展开的 `~/...` 去 exists()
        # 恒为 False，`_background_cleanup` 里「删不干净就别销账」那一支会对整类
        # ~ 路径静默失效 —— 正是 pending_deletions 这张表要防的事。
        artifact_dir = Path(artifact_dir).expanduser()
        if not artifact_dir.is_absolute():
            # 护栏用 absolute() 按【进程 cwd】解释相对路径，实测 '' 和 '.' 会把
            # cwd 整个 rmtree 掉。同一张表的另一个消费者
            # task_cleanup._sweep_pending_deletions 有同样一道卫兵；两个消费者
            # 的判据必须对称，否则迟早从没卫兵的那一侧漏进去。
            # 当作「调用方没要求删产物」丢弃，不交给护栏。
            logger.warning(
                f"Task {task_id}: refusing non-absolute artifact dir: "
                f"{str(artifact_dir)!r}")
            artifact_dir = None

    tombstoned = False
    conn = get_connection()
    try:
        try:
            # 为什么 DELETE + commit 留在锁里（2026-08-08 评审复核后维持原状）。
            # 代价是真的：get_connection 设了 busy_timeout=30000，写竞争下这把锁
            # 最长能被按住 30 秒，start_task / pause_task / 并发删除 / worker 退出
            # 时的登记回收全排在后面。但挪出去会开一个更坏的窗口 ——
            #   1. 本函数在锁内看到 active_tasks 里没有它 → running=False，出锁；
            #   2. start_task 拿到锁，读到 status='paused' → UPDATE 成 'running'
            #      → commit → 登记 active_tasks → 出锁 → thread.start()；
            #   3. 本函数才发出 DELETE，rowcount=1，而 running 是上一步的旧结论：
            #      既不写 pending_deletions 也不置停止标志、不写墓碑。
            # 结果就是快路径 rmtree 完，刚起来的线程把目录重建出来写满瓦片 ——
            # 零引用的孤儿目录（本地地形一路跑到底，最重），外加 map 的进度批次
            # 拿已删的 task_id 撞外键直到进程重启。start_task 的「读状态 → UPDATE
            # → 登记线程」整段就在同一把锁内（task_manager.TaskManager.start_task
            # 里那个 `with self._state_lock` 块），所以
            # 「判在跑」与「删行」必须同为一个临界区，只挪 commit 也不行：DELETE
            # 没提交就等于没发生。
            with manager._state_lock:
                thread = manager.active_tasks.get(task_id)
                # 为什么 is_alive() 之外还要看 ident：四条管线的 start_* 都是锁内
                # 提交 status='running' + 登记 active_tasks，出锁之后才 start()。
                # 这段窗口里 is_alive() 仍是 False，只看它会判成「没在跑」而走快
                # 路径同步 rmtree —— 线程随后启动、把目录重新建出来写满瓦片，而
                # pending_deletions 是空的，启动清扫只认清单，扫不到这个孤儿目录
                # （本地地形最重：_run_tiling_job 全程不回查行，一路跑到底）。
                # ident 在 start() 前为 None、start() 之后（含线程已死）恒非 None，
                # 正好只补上这段窗口，不会把跑完的线程重新算成在跑。
                running = bool(thread) and (thread.is_alive() or thread.ident is None)

                if running:
                    # 墓碑必须在删行【之前】写入：删行之后、线程发现之前的这段
                    # 窗口里，map 的进度批次会拿已经不存在的 task_id 去 INSERT
                    # task_tiles，撞上外键约束（实测 INSERT OR IGNORE 不豁免外键）。
                    # 只有【自己写进去的】墓碑才置 tombstoned：墓碑是没有引用计数
                    # 的 set，同一个运行中任务的第二发 DELETE 撞上 add() 的幂等，
                    # 无条件置位的话它在后面任何一点抛出（commit / Thread.start()
                    # 都可能 database is locked）都会 discard 掉第一发还在用的墓碑，
                    # 而第一发的 worker 还活着 —— 之后每次 flush 都撞外键，直到进程
                    # 重启（同下面 _background_cleanup join 超时那一支的理由）。
                    if tombstone is not None and task_id not in tombstone:
                        tombstone.add(task_id)
                        tombstoned = True
                    flag = manager.stop_flags.get(task_id)
                    if flag is not None:
                        flag.set()

                cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (task_id,))
                row_deleted = cur.rowcount > 0
                if not row_deleted:
                    # 行本来就不存在 —— 一片磁盘都不能碰。产物目录名只由 task_id
                    # 推出来，删一个不存在的 id 时那个同名目录多半是别的生命周期
                    # 留下的残留（典型：先 delete_files=false 删了行、目录留着，
                    # 客户端重试再带 delete_files=true）。调用方把 row_deleted=False
                    # 翻成 404，这时还去 rmtree 就是「404 + 静默真删」。
                    #
                    # 这道闸对本地地形是必需的：只有它的 artifact_dir 在 manager
                    # 内部按 task_id 硬算，另外三条在路由层算、算之前先查过行。
                    artifact_dir = None
                if artifact_dir is not None:
                    # 记清单与删行同一事务 —— 中间崩掉就丢了产物线索。放在 DELETE
                    # 之后只是为了先拿到 rowcount 喂上面那道闸；一次 commit，语句
                    # 先后不影响原子性。
                    #
                    # 快路径（没在跑）也要入队，且必须在删产物【之前】：护栏用的
                    # 是 rmtree(ignore_errors=True)，Windows 上文件被占会静默删不
                    # 掉，先删后判就来不及补一条清单行了 —— 事务已经提交，行也没
                    # 了，那个瓦片金字塔从此没有任何线索（P1#6）。删成功的那条路
                    # 上这行会被 _remove_artifacts_and_settle 当场销掉，正常删除
                    # 不会在表里留下痕迹。
                    _queue_pending_deletion(conn, artifact_dir)
                conn.commit()
        finally:
            conn.close()

        if on_row_gone is not None:
            try:
                on_row_gone()
            except Exception as e:
                logger.warning(f"Task {task_id}: on_row_gone hook failed: {e}")

        if not running:
            files_removed = None
            if artifact_dir is not None:
                # 与后台收尾同一个助手：护栏返回的是「可删」，报给用户的必须是
                # 「真删了」，而清单行只有在真删了（或永远删不掉）时才销账。
                files_removed = _remove_artifacts_and_settle(artifact_dir)
            return DeleteOutcome(row_deleted, files_removed, False)

        threading.Thread(
            target=_background_cleanup,
            args=(task_id, thread, artifact_dir, tombstone),
            daemon=True,
            name=f"DeleteCleanup-{task_id}",
        ).start()
    except Exception:
        # 墓碑写了但后台收尾线程还没接手 —— 从这里抛出去就再没人摘它了。
        # 可抛点：_queue_pending_deletion 的 INSERT、DELETE、commit（三者都可能
        # database is locked），以及 Thread.start() 的「can't start new thread」。
        # 不摘的话留下的状态是:行还在(事务回滚)+ 停止标志已置 + 进度写入被
        # 永久短路。stop flag 不回滚是有意的:线程停下来是幂等的,重试删除仍然要
        # 它停。worker 随后走某个 stop 分支【正常】return —— 没有异常,所以
        # `_run_task` 的 except 盖不住它;把行从 running 捞出来的是线程退出处的
        # `task_cleanup.fail_stranded_running_task`(2026-08-08 评审 P1#2)。
        # 在那之前这里的状态是「永久 running 且没有线程」,而 start_task 只接受
        # pending/paused —— 用户点不动,只能先点暂停或重启进程。
        if tombstoned:
            tombstone.discard(task_id)
        raise
    return DeleteOutcome(row_deleted, None, artifact_dir is not None)
