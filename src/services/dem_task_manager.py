"""
DEM Task Manager

Creates and runs DEM download tasks backed by dem_tasks/dem_files tables.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.contracts.artifact import PIPELINES
from src.contracts.region import RegionSpec
from src.contracts.reservation import ResourceKind, ResourceRequest
from src.core.database import get_connection, utc_now_iso
from src.services import disk_budget
from src.services.config_manager import ConfigManager
from src.services.dem_download_engine import DemDownloadEngine
from src.services.download_speed import SpeedMeter
from src.services.geo_validation import (AUTO_MAXZOOM, DEFAULT_TILING_QUALITY,
                                         TILING_QUALITY_OFFSETS, coerce_maxzoom,
                                         maxzoom_from_db, maxzoom_to_db,
                                         require_absolute_output_dir, sanitize_filename,
                                         validate_bbox, validate_tiling_quality)
from src.services.resource_scheduler import (get_scheduler,
                                             plan_download_reservation,
                                             plan_tiling_reservation)
from src.services.task_logging import open_task_log
from src.services.dem_granules import (
    tiles_for_bbox, astgtm_v3_granules_for_tile, copernicus_glo30_granules_for_tile,
)
# resolve_stored_output_dir 是【读存量 output_path 的唯一一套口径】——
# 不要换回 geo_validation.resolve_output_dir:那是给**请求里**新传进来的路径做
# 校验的（强制落在 DOWNLOADS_DIR 内），两者对相对值的解释不同
# （'./downloads/dem' → <DL>/dem vs <DL>/downloads/dem），而 M10 的存量归一
# （`database.normalize_stored_output_paths`）认的是前者。混用会让升级后的旧
# DEM 任务指针指到空目录：/terrain/dem/<id> 404、恢复即全量重下、删除报成功而产物滞留。
# 见 docs/reviews/2026-08-08-full-project-review.md 的 P1#5。
from src.services.task_cleanup import (fail_stranded_running_task,
                                       resolve_stored_output_dir)
from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir
from src.services.terrain_tiling.vrt_builder import list_dem_tifs
from src.services.terrain_tiling.layer_json import parent_url_if_base_available

logger = logging.getLogger(__name__)

# task_progress 广播最小间隔（秒）：进度回调每颗粒触发，逐次 emit 会把前端
# 打爆；严格时间窗节流，无「计数变化必发」豁免 —— 颗粒集中完成时每个完成
# 回调都改计数，豁免会让窗口形同虚设（范本：task_manager.PROGRESS_EMIT_MIN_INTERVAL）。
_PROGRESS_EMIT_MIN_INTERVAL = 1.0

# 管线名从合同表取，不手写字面量：task_logging 的文件名正则、artifacts 表的
# 取值域与这里必须是同一个词。`.index()` 不是绕弯 —— 它让「合同里把 'dem' 改名
# 了」在 import 期就炸，而不是等到某天发现任务日志目录里一个 dem_*.log 都没有。
_PIPELINE = PIPELINES[PIPELINES.index('dem')]


# M7: 'skipped' 也算「已终结的下载项」。404 的颗粒（海洋 / 覆盖范围外 ——
# Copernicus GLO-30 对海面本来就没瓦片）由引擎有意上报 skipped，是部分成功
# 语义；但计数增量此前只认 completed/failed，收尾判定又把 skipped 算作已终结、
# 任务照常 completed。结果终态下 downloaded_files + failed_files < total_files
# 这个不变量被破坏：记录面板渲染「已完成 · 4 / 10 文件」，详情弹窗给一个
# **已完成任务** 40% 的进度条，下载过程中进度条同样封顶、「预计剩余」偏大。
# 磁盘产物与后续切片都是对的 —— 纯计数/展示口径问题。
_DONE_STATUSES = ("completed", "skipped")


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status in _DONE_STATUSES) - int(old_status in _DONE_STATUSES)
    failed_delta = int(new_status == "failed") - int(old_status == "failed")
    return downloaded_delta, failed_delta


class DemTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.engine = DemDownloadEngine()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()

        # Same orphan-recovery rationale as TaskManager: nothing in active_tasks
        # at __init__ time, so any DB row still 'running' is from a dead process.
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        """Demote leftover 'running' rows in dem_tasks and dem_terrain_jobs.

        - dem_tasks: flipped to 'paused' (supports resume_task)
        - dem_terrain_jobs: flipped to 'failed' (no pause/resume model — terrain
          tiling is a one-shot build_terrain call, must restart from scratch)

        两类降级都必须在**任务自己的**日志里留下解释（§4.5）。切片作业那一条
        尤其不能省：它写的是 `failed` —— 一个硬终态，而用户点开任务详情看到的
        是「失败」两个字加一片空白，日志文件在崩溃那一瞬间戛然而止，最后一行
        是某个瓦片的进度。进程崩溃 / 断电 / 关窗口是这条管线上**最常发生的**
        真实终态转移，不是边角情况。
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM dem_tasks WHERE status = 'running'")
            task_ids = [row['id'] for row in cur.fetchall()]
            if task_ids:
                cur.executemany(
                    "UPDATE dem_tasks SET status = 'paused' WHERE id = ? AND status = 'running'",
                    [(tid,) for tid in task_ids],
                )

            now = utc_now_iso()
            # 连 task_id 一起取：每任务日志按**任务** id 命名（下载与切片是同一
            # 个任务的两个阶段，共用一份日志文件，见 _run_tiling_job 的注释），
            # 而这张表的主键是**作业** id —— 只拿 id 就会把解释写进一个 id 恰好
            # 相同的、毫不相干的任务的日志里。
            cur.execute("SELECT id, task_id FROM dem_terrain_jobs WHERE status = 'running'")
            jobs = [(row['id'], row['task_id']) for row in cur.fetchall()]
            job_ids = [jid for jid, _ in jobs]
            if job_ids:
                cur.executemany(
                    "UPDATE dem_terrain_jobs SET status = 'failed', completed_at = ?, "
                    "error_message = 'Process was interrupted before completion; restart terrain tiling' "
                    "WHERE id = ? AND status = 'running'",
                    [(now, jid) for jid in job_ids],
                )

            if task_ids or job_ids:
                conn.commit()
                logger.warning(
                    f"Recovered orphans — dem_tasks paused: {task_ids}, dem_terrain_jobs failed: {job_ids}"
                )
            # 上面那条 warning 只进**全局**日志。落库已经提交，所以下面这些写
            # 日志的动作即使全部失败也不影响状态机 —— _log_recovery 自己兜住。
            for tid in task_ids:
                self._log_recovery(
                    tid, 'paused',
                    '进程在本任务下载期间退出（崩溃 / 断电 / 关窗口）：重启时'
                    '发现库里还写着 running 而没有任何线程，已降级为 paused。'
                    '已下载的颗粒都在，点「恢复」从断点继续。')
            for jid, tid in jobs:
                self._log_recovery(
                    tid, 'failed',
                    f'进程在地形切片期间退出（崩溃 / 断电 / 关窗口）：重启时发现'
                    f'切片作业 #{jid} 在库里还写着 running 而没有任何线程，已判为'
                    f' failed。切片是一次性的 build_terrain 调用，没有断点续跑，'
                    f'需要重新起切片（下载好的颗粒还在，不用重下）。')
        except Exception as e:
            logger.error(f"Failed to recover DEM orphan tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _log_recovery(self, task_id: int, status: str, note: str) -> None:
        """把一次「启动时孤儿恢复」写进**这个任务自己的**日志。绝不抛。

        绝不抛是硬要求：调用点在 `__init__` 里，一个次要 sink 的环境问题没有
        资格让整个 DemTaskManager 构造不出来 —— 那等于一条日志写不动就让服务
        起不来（同 `open_task_log` 类 docstring 的论证）。

        句柄短命（开 → 写 → 关）：`open_task_log` 会摘掉同 (pipeline, task_id)
        的遗留 handler，留着不关会让后续真正跑起来的那一轮写到已轮转走的
        inode。恢复跑在 `__init__`，此刻没有任何任务线程持有句柄，不存在互抢。
        """
        try:
            tlog = open_task_log(_PIPELINE, task_id)
            try:
                tlog.event('terminal', status=status, reason='process_restart')
                tlog.warning('%s', note)
            finally:
                tlog.close()
        except Exception as e:
            logger.warning(f"DEM task {task_id}: 孤儿恢复日志写入失败（忽略）: {e!r}")

    def create_task(self, params: dict) -> int:
        # NOTE: Keep signature compatible-ish with existing API patterns (dict in, id out).
        name = sanitize_filename(params.get("name") or "DEM Task")
        # 区域合同（§D）。请求带 region 就以它为准 —— 多边形、洞环、跨反经线都
        # 只在这条路上表达得出来；没带就从四至现造一个矩形。两条路产出同一个
        # RegionSpec，从这里往下不再有第二套坐标口径：颗粒枚举、落库的四至列、
        # region_spec 列全部出自它。
        # 落库的四至列取 spec.bbox（序就是 (n, s, e, w)，与表列同序）。跨界时
        # east 会是 >180 的规范化写法 —— 那是 RegionSpec 的写法，**不经过**
        # validate_bbox（那个函数守的是「旧的四列输入」，见它上面那段注释）。
        raw_region = params.get("region")
        if raw_region:
            # RegionValidationError 是 ValueError 的子类，路由照旧转 400。
            region = RegionSpec.from_json(raw_region)
        else:
            # 四至共用校验(范围/顺序/NaN/类型),见 src/services/geo_validation.py
            region = RegionSpec.from_bbox(
                *validate_bbox(params.get("north"), params.get("south"),
                               params.get("east"), params.get("west")),
                source='manual')
        north, south, east, west = region.bbox
        dataset = params.get("dataset") or "COP-DEM-GLO-30"
        # C5: 创建任务时校验 output_path —— 必须是绝对路径且至少两级深度,
        # 非法抛 ValueError(路由层转 400)。0.2.4 起不再强制落在
        # Config.DOWNLOADS_DIR 内(全盘可选,见 require_absolute_output_dir);
        # 绝对路径的要求(0.2.3 起)保留,避免依赖进程 CWD。
        output_path = require_absolute_output_dir(
            params.get("output_path") or self.config.get("default_save_path", "./downloads")
        )
        download_num = 1 if str(params.get("download_num", "false")).lower() in ("1", "true", "yes") else 0
        download_swb = 1 if str(params.get("download_swb", "false")).lower() in ("1", "true", "yes") else 0

        if dataset not in ("ASTGTM.003", "COP-DEM-GLO-30"):
            raise ValueError(f"Unsupported dataset: {dataset}")
        # Copernicus GLO-30 has no NUM/SWB companion files.
        if dataset == "COP-DEM-GLO-30":
            download_num = 0
            download_swb = 0
        # ASTGTM.003 does not ship _swb granules (water bodies live in ASTWBD.001);
        # creating a task with swb would queue nothing but guaranteed 404s.
        if download_swb:
            raise ValueError(
                "ASTGTM.003 has no _swb granules; water body data comes from the "
                "separate ASTWBD.001 product"
            )

        # 颗粒枚举逐段来（§D）。dem_granules.tiles_for_bbox 的 docstring 写着
        # 「跨界要由调用方拆」，而在此之前**没有任何调用方拆过**：east < west 被
        # validate_bbox 直接拒了，east > 180 这种规范化写法则会算出 N45E181 这类
        # 根本不存在的颗粒名，整批 404、任务全失败。现在这个调用方就是拆的那个。
        # 不跨界时 antimeridian_parts 只有一段，与改造前逐字等价。
        #
        # 按 tile_id 归并：两段一段贴 +180、一段贴 -180，1°×1° 网格上不相交，
        # 正常情况下不会重复。归并是为了让「拆分口径以后变了」不会静默变成重复的
        # dem_files 行 —— 那张表的插入是无冲突约束的 executemany，重复的
        # granule_id 会让 total_files 虚高，进度条永远到不了 100%（M7 修过的
        # 同一类不变量：downloaded + failed 必须能追平 total）。
        tiles_by_id = {}
        for part_n, part_s, part_e, part_w in region.antimeridian_parts:
            for t in tiles_for_bbox(north=part_n, south=part_s,
                                    east=part_e, west=part_w):
                tiles_by_id.setdefault(t.tile_id, t)

        # 再按**几何**筛一遍（§9 门槛「RegionSpec 被地图与 DEM 两条管线共同消费」）。
        # tiles_for_bbox 只认外接矩形，所以在这一行之前，一个 L 形或带洞的区域
        # 拿到的颗粒清单与它的外接矩形**逐个相同** —— 实测 4 颗对 4 颗，用户画的
        # 多边形对 DEM 完全没有意义。那正是 GeoDownloader「按 bbox 计费、按
        # polygon 出图」的同一个错位，而且在 DEM 侧更贵：一颗 COP-DEM 颗粒
        # 45 MiB，多下一圈是实打实的流量与磁盘。
        #
        # 矩形区域一颗都筛不掉（intersects_bbox 对外接矩形恒真），所以这一步对
        # 改造前的行为逐字无损；只有多边形任务会变少。
        #
        # 逐段问：跨反经线时区域顶点经度被归一到 >180，而颗粒的经度是回绕过的
        # （E179 / W180），两边坐标系不一致。用 part 的偏移把颗粒挪回同一套未回绕
        # 坐标再比。
        if not region.is_rectangle:
            kept = {}
            for part_n, part_s, part_e, part_w in region.antimeridian_parts:
                # 这一段在未回绕坐标里的起点：第二段（-180 打头）要 +360。
                shift = 360.0 if part_w < region.bbox_west else 0.0
                for t in tiles_by_id.values():
                    lon_w = t.lon + shift
                    if not (part_w - 1.0 <= t.lon < part_e):
                        continue
                    if region.intersects_bbox(north=t.lat + 1, south=t.lat,
                                              east=lon_w + 1, west=lon_w):
                        kept[t.tile_id] = t
            dropped = len(tiles_by_id) - len(kept)
            if dropped:
                logger.info(f"DEM 颗粒按区域几何筛除 {dropped} 颗"
                            f"（外接矩形 {len(tiles_by_id)} → 实际 {len(kept)}）")
            tiles_by_id = kept
        granules: List[str] = []
        # 排序按 (lat, lon) 而不是 tile_id 字符串：'N05...' 与 'S05...' 混排时
        # 字符串序会把南北半球交错开，下载顺序与用户在地图上看到的从南到北对不上。
        for t in sorted(tiles_by_id.values(), key=lambda x: (x.lat, x.lon)):
            if dataset == "COP-DEM-GLO-30":
                granules.extend(copernicus_glo30_granules_for_tile(t))
            else:
                granules.extend(astgtm_v3_granules_for_tile(t, include_num=bool(download_num), include_swb=bool(download_swb)))

        total_files = len(granules)
        # 选区完全落在数据覆盖范围外（如 ASTGTM |lat|>83）时颗粒列表为空：
        # 拒绝创建，否则会产生一个 total_files=0、无事可做却"成功完成"的空任务。
        if total_files == 0:
            raise ValueError(
                f"Selected area yields no {dataset} granules (outside dataset coverage); "
                "nothing to download"
            )

        # 建任务时的体积估算。这里**只报不拦** —— 拦在 start_task（那时才知道
        # 还剩几颗要下、输出目录在哪块盘上）。报是必须的：#30 那次 17 倍偏差
        # 之所以能定位，靠的就是「颗粒数 × 单价」这个算式被写了下来。
        estimate = disk_budget.estimate_dem_task(total_files, dataset)
        logger.info(
            f"DEM 任务预估：{total_files} 颗 {dataset} 颗粒，峰值约 "
            f"{estimate.peak_bytes / (1024 * 1024):.0f} MiB"
            f"（缓存 + 任务目录各一份）；区域 {region.summary()}")

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dem_tasks (
                    name, status, north, south, east, west,
                    dataset, output_path, download_num, download_swb,
                    total_files, downloaded_files, failed_files,
                    region_spec
                )
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (name, north, south, east, west, dataset, output_path, download_num,
                 download_swb, total_files, region.to_json()),
            )
            task_id = cur.lastrowid

            cur.executemany(
                """
                INSERT INTO dem_files (task_id, granule_id, status, retry_count)
                VALUES (?, ?, 'pending', 0)
                """,
                [(task_id, g) for g in granules],
            )
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_task(self, task_id: int) -> None:
        conn = get_connection()
        reservation = None
        owner = (_PIPELINE, task_id, 'download')
        try:
            cur = conn.cursor()
            with self._state_lock:
                active_thread = self.active_tasks.get(task_id)
                if active_thread and active_thread.is_alive():
                    raise ValueError(f"DEM task {task_id} is already running")

                cur.execute(
                    "SELECT status, dataset, output_path FROM dem_tasks WHERE id = ?",
                    (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"DEM task {task_id} not found")
                if row["status"] not in ("pending", "paused"):
                    raise ValueError(f"Cannot start DEM task {task_id} with status '{row['status']}'")

                # ---- 准入：磁盘预算 + 全局配额。夹在状态闸门与 UPDATE 之间 ----
                # 位置是刻意的：这两道都可能拒，而在 UPDATE 之前被拒**不需要任何
                # 回补** —— 行还停在 pending/paused，用户腾出空间或等一个任务跑完
                # 再点一次就行。本文件下面那一串 L2 补偿块的存在，正是因为「先落库
                # 再做可能失败的事」这个顺序。
                output_dir = (resolve_stored_output_dir(row["output_path"])
                              / f"dem_task_{task_id}")
                # 估算按**还没下的**颗粒算，不是 total_files：恢复一个下到 90% 的
                # 任务时按总量算会凭空要求十倍空间，把本来能跑完的任务拦死。
                cur.execute(
                    "SELECT COUNT(*) AS n FROM dem_files "
                    "WHERE task_id=? AND status IN ('pending','failed','downloading')",
                    (task_id,))
                pending_granules = int((cur.fetchone() or {"n": 0})["n"] or 0)
                estimate = disk_budget.estimate_dem_task(pending_granules, row["dataset"])
                verdict = disk_budget.check_budget(output_dir, estimate, self.config)
                logger.info(
                    f"DEM task {task_id} 磁盘预检（{pending_granules} 颗待下）：{verdict.reason}")
                if not verdict.ok:
                    raise ValueError(verdict.reason)

                # concurrent_downloads 从这里起是**请求量**而不是生效值：真正开的
                # 连接数是调度器授予的那个（最低 1 条，一条也能跑完）。脏值不该让
                # 任务起不来，退回出厂默认。
                try:
                    requested_conns = int(self.config.get("concurrent_downloads", "5"))
                except (TypeError, ValueError):
                    requested_conns = 5
                scheduler = get_scheduler()
                # 这里**没有**「先按 owner 键回收一张同名凭据」那一步，是刻意的：
                # 那种写法能把一张还在服役的凭据摘掉（完整的缺陷形态记在
                # release_owner 的 docstring 里），而它声称要防的泄漏在这条路径
                # 上并不存在 —— 上面的 is_alive() 闸门证明没有活线程，而线程侧的
                # 归还写在 _run_task 的 finally 里、**先于**它把自己从 active_tasks
                # 摘掉，所以「线程已不在册」蕴含「凭据已归还」。真出现重复 owner
                # 只可能是进程内另有 bug，那时 reserve 会当场 ValueError 把话说
                # 清楚，而不是让我们悄悄吊销一份别人正在用的配额。
                # 磁盘一并预留：check_budget 的判决对**别的**任务不可见，不预留的话
                # 三个任务能一起通过同一份剩余空间（disk_budget 模块 docstring 的
                # 「预算必须是全局的」）。DISK_BYTES 是只记账不设限的种类，
                # 一定授予，不会成为拒绝的原因。
                reservation = scheduler.reserve(owner, plan_download_reservation(
                    requested_conns) + [
                        # DISK_BYTES 是全额或不给的种类（半个磁盘预算没有意义），
                        # 所以 minimum 必须等于 requested，否则 ResourceRequest
                        # 的 __post_init__ 直接拒收。
                        ResourceRequest(kind=ResourceKind.DISK_BYTES,
                                        requested=verdict.required_bytes,
                                        minimum=verdict.required_bytes)])
                if reservation is None:
                    free = scheduler.snapshot()['available']
                    raise ValueError(
                        f"DEM task {task_id} cannot start now: the global resource "
                        f"budget is saturated (free task_slot="
                        f"{free.get('task_slot')}, network={free.get('network')}). "
                        f"Wait for a running task to finish, or raise "
                        f"max_concurrent_tasks / max_network_connections in Settings.")

                cur.execute(
                    "UPDATE dem_tasks SET status='running', started_at=? WHERE id=? AND status IN ('pending','paused')",
                    (utc_now_iso(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"DEM task {task_id} could not be started because its status changed")
                conn.commit()

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task_entry,
                                      args=(task_id, stop_flag, reservation),
                                      daemon=True, name=f"DemTask-{task_id}")
                self.active_tasks[task_id] = th
            try:
                th.start()
            except Exception:
                # commit 与 thread.start() 之间的异常会留下"DB 是 running、
                # 线程从未启动"的任务：状态回退为 paused（可重新 start/resume），
                # 并清理登记，避免卡死在 running。
                with self._state_lock:
                    if self.active_tasks.get(task_id) is th:
                        self.active_tasks.pop(task_id, None)
                    if self.stop_flags.get(task_id) is stop_flag:
                        self.stop_flags.pop(task_id, None)
                cur.execute(
                    "UPDATE dem_tasks SET status='paused' WHERE id=? AND status='running'",
                    (task_id,),
                )
                conn.commit()
                raise
            # 交接完成：配额的所有权已经在线程手里，本方法的 finally 不该再碰它。
            reservation = None
        finally:
            # 线程没接手（任何一条抛出路径）就在这里还回去。凭据的 release 是
            # 幂等的，所以这一句与线程侧的归还不会互相踩。
            if reservation is not None:
                reservation.release()
            conn.close()

    def pause_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE dem_tasks SET status='paused' WHERE id=? AND status='running'", (task_id,))
            if cur.rowcount == 0:
                cur.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"DEM task {task_id} not found")
                raise ValueError(f"Cannot pause DEM task {task_id} with status '{row['status']}'")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def resume_task(self, task_id: int) -> None:
        self.start_task(task_id)

    def start_tiling(self, task_id: int,
                     maxzoom: Optional[Union[int, str]] = None,
                     quality: Optional[str] = None,
                     vertex_normals: Optional[bool] = None) -> None:
        task_id = int(task_id)

        # Resolve task output path first; tiling is based on existing DEM outputs.
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, output_path, dataset FROM dem_tasks WHERE id = ?",
                        (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"DEM task {task_id} not found")
            # 只有下载完成（completed）的任务才能切片：pending/running 的任务
            # 数据残缺，tiling 会在不完整输入上"成功"产出错误的 terrain。
            if row["status"] != "completed":
                raise ValueError(
                    f"Cannot start terrain tiling for DEM task {task_id} with status "
                    f"'{row['status']}'; wait for the download to complete"
                )
            output_path = row["output_path"]
            dataset = row["dataset"]
        finally:
            conn.close()

        # M20: layer.json 的 parentUrl 指向全局 base，写死 localhost:5000 会在
        # 非 5000 端口/反代/远程访问下 404 —— 可配置，默认值是应用内相对路径
        # `/terrain/base`（由浏览器继承提供 layer.json 的 origin，瓦片走 5001
        # 专用 origin 时也对）。
        #
        # 两道闸门缺一不可（见 layer_json）：目录形式（带 /layer.json 会让 Cesium
        # 请求 .../layer.json/layer.json）+ base 真的存在。任一不满足都是 404，
        # 而 Cesium 对 404 的处理是塞假 heightmap 图层并污染共享 builder ⇒
        # 本任务自己的 quantized-mesh 瓦片也按 heightmap 解析，高程全错且不报错。
        # 全球 base 是可选产物，「没建」是默认装机的常态，所以这里必须放行 None。
        base_dir = resolve_stored_output_dir(
            # 兜底值与 DEFAULT_CONFIGS 逐字一致（旧的 ./downloads/... 会把底图判成
            # 不可用，然后写一个 404 的 parentUrl —— 上面说的那条链）。
            self.config.get("terrain_global_base_path", "./assets/terrain/base_z8"))
        parent_url = parent_url_if_base_available(
            self.config.get("terrain_base_parent_url", "") or "/terrain/base",
            base_dir,
        )

        # 处理弹窗「对已下载的高程任务做地形切片」允许调用方覆盖最大层级；
        # 缺省（None / 空串）仍读配置，保持原有装机默认不变。
        if maxzoom is not None:
            maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
        if maxzoom is None:
            # 配置值同样过 coerce_maxzoom（范本 local_terrain_task_manager.
            # _default_maxzoom，那边两条路径都校验）。此前这里是裸 int()：
            # terrain_local_maxzoom 在 config_manager._UNCONSTRAINED_KEYS 里没有
            # 取值规则，写进去的 99 能一路传到 build_terrain。
            # 尤其不能把原始值直接交给 maxzoom_to_db —— 它对越界值静默放行
            # （`int('-1')` = -1，而 -1 正是自动挡的哨兵），配置里一个 '-1' 就会
            # 在库里变成一条与「用户真的选了自动」无从分辨的作业。
            # 校验失败不抛：配置是装机默认，一个坏值不该让所有切片都启动不了；
            # 但必须留痕。local 侧 _default_maxzoom 是同一条规矩。
            # （显式传参那条相反：调用方给了非法值必须当场报错，不能静默改写。）
            maxzoom_raw = self.config.get("terrain_local_maxzoom", AUTO_MAXZOOM)
            try:
                maxzoom = coerce_maxzoom(maxzoom_raw, "terrain_local_maxzoom")
            except Exception as e:
                maxzoom = AUTO_MAXZOOM
                logger.warning(
                    f"配置 terrain_local_maxzoom={maxzoom_raw!r} 不可用({e})，"
                    f"本次切片改用出厂默认 {AUTO_MAXZOOM!r}")
            if maxzoom is None:
                # 配置被清空 = 没配过 → 出厂默认
                maxzoom = AUTO_MAXZOOM
        # 从这里往下 maxzoom 一律是**库形态的 int**（自动挡即哨兵）：下面的
        # UPSERT 绑定与切片线程参数都直接用它，还原成 TileParams 要的形态是
        # _run_tiling_job 里那次 maxzoom_from_db。
        maxzoom = maxzoom_to_db(maxzoom)

        # 档位与法线：请求未给就取配置默认，与 maxzoom 完全同形。兜底值与
        # DEFAULT_CONFIGS 逐字一致（同上面 terrain_global_base_path 兜底那条注释
        # 的规矩：兜底和出厂默认不一致会造出「改了没反应」的假旋钮）。
        # 校验落在管理器而不是路由层：DEM 这条路径的 maxzoom 校验就在这里
        # （local 那条在路由层），两个新参数跟着各自路径的既有位置走。
        if quality is None:
            # 报错里点名配置键而不是请求字段：这条分支上用户根本没传过 quality，
            # 说 "quality (...) must be one of" 会把他指到一个不存在的输入上。
            quality = validate_tiling_quality(
                self.config.get("terrain_quality_preset", DEFAULT_TILING_QUALITY)
                or DEFAULT_TILING_QUALITY,
                "terrain_quality_preset")
        else:
            # 拼错的档位当场 ValueError（路由转 400），不静默退回 balanced：
            # 「改了档位重切、结果一模一样且零报错」是这条路径最难查的一类假象。
            quality = validate_tiling_quality(quality)
        if vertex_normals is None:
            vertex_normals = (
                self.config.get("terrain_vertex_normals", "false") or "false") == "true"
        vertex_normals = bool(vertex_normals)

        task_dir = resolve_stored_output_dir(output_path) / f"dem_task_{task_id}"
        output_dir = task_dir / "terrain_tiles"

        # ---- 准入：磁盘预算 + 全局配额。在 job 行 UPSERT 成 running **之前** ----
        # 顺序与 start_task 同一条理由：这两道都可能拒，拒在落库之前不需要回补。
        # 落库之后再拒就得走下面那个 L2 回补块，而那条路每多一个分支就多一次
        # 「job 行永久停在 running、只能重启进程解开」的机会。
        source_bytes = 0
        for tif in list_dem_tifs(task_dir):
            try:
                source_bytes += tif.stat().st_size
            except OSError:
                pass
        # Copernicus GLO-30 是 Float32（物化实测约源的 1.9 倍），ASTER 是 Int16
        # （0.78 倍）—— 差一倍多，不能用同一个系数。
        estimate = disk_budget.estimate_terrain_tiling(
            source_bytes, float_source=(dataset == "COP-DEM-GLO-30"))
        verdict = disk_budget.check_budget(output_dir, estimate, self.config)
        logger.info(f"DEM task {task_id} 切片磁盘预检"
                    f"（源 {source_bytes / (1024 * 1024):.0f} MiB）：{verdict.reason}")
        if not verdict.ok:
            raise ValueError(verdict.reason)

        # ---- 切片作业的准入闸门必须跑在 reserve **之前** ----
        # 这里曾经是一句无条件的 `scheduler.release_owner(owner)`，注释声称
        # 「状态闸门已证明这个 owner 没有活着的线程」。那句话在 start_task 成立，
        # 在这里是假的：本方法在它之前跑过的唯一闸门是 dem_tasks.status ==
        # 'completed'（上面那处 SELECT），而整个切片期间 dem_tasks.status 一直
        # 就是 completed —— 真正判定「已经在切片了」的是下面那条 dem_terrain_jobs
        # 的 UPSERT，隔着二十多行。于是重复点一次「开始切片」的后果是：先把正在
        # 跑的那份凭据（若干 CPU worker + 一个 GDAL 槽 + 一个任务槽）吊销，再让
        # UPSERT 拒绝本次请求 —— 活着的作业继续跑，调度器账上却一格不占。
        #
        # active_tasks 不能拿来当这里的闸门：切片线程与下载线程共用那张表，
        # 下载刚收尾的那一小段里它挂的还是下载线程（下面登记线程处的长注释讲的
        # 就是这个窗口），拿它当闸门会把「下完立刻切片」这条正常路径拒掉。
        # 所以按 job 行自己的状态预检。
        conn = get_connection()
        try:
            job_row = conn.execute(
                "SELECT status FROM dem_terrain_jobs WHERE task_id=?",
                (task_id,)).fetchone()
        finally:
            conn.close()
        if job_row and job_row["status"] == "running":
            raise ValueError(f"DEM tiling job for task {task_id} is already running")
        # 这道预检只负责「被拒时一分钱都不动」，它**不是**权威闸门：它与下面的
        # UPSERT 之间仍有窗口，两个请求可以同时读到非 running。权威的仍然是
        # UPSERT 的 `WHERE status != 'running'` + rowcount ——读与写在同一条
        # 语句里原子完成，并发请求里只有一个能把行改成 running，竞态是它关掉的。

        # 请求的 worker 数：这条管线没有自己的配置键，直接按调度器的 CPU 上限要，
        # 让全局配额去分（要 0 会被 plan_tiling_reservation 顶成 1）。
        scheduler = get_scheduler()
        owner = (_PIPELINE, task_id, 'tiling')
        requested_workers = scheduler.limits().get(ResourceKind.CPU_WORKER, 1)
        reservation = scheduler.reserve(
            owner,
            plan_tiling_reservation(requested_workers) + [
                # 全额或不给，minimum 必须等于 requested（见 ResourceRequest）。
                ResourceRequest(kind=ResourceKind.DISK_BYTES,
                                requested=verdict.required_bytes,
                                minimum=verdict.required_bytes)],
        )
        if reservation is None:
            free = scheduler.snapshot()['available']
            raise ValueError(
                f"DEM tiling for task {task_id} cannot start now: the global resource "
                f"budget is saturated (free task_slot={free.get('task_slot')}, "
                f"cpu_worker={free.get('cpu_worker')}, "
                f"gdal_slot={free.get('gdal_slot')}). Wait for a running task to "
                f"finish, or raise max_concurrent_tasks / max_cpu_workers / "
                f"max_gdal_slots in Settings.")

        conn = get_connection()
        try:
            cur = conn.cursor()
            # I2: 锁内条件 upsert + rowcount（范本同 start_task）——并发
            # start_tiling 只有一个能把 job 置为 running，其余 ValueError。
            with self._state_lock:
                cur.execute(
                    """
                    INSERT INTO dem_terrain_jobs (
                        task_id, status, output_dir, maxzoom, parent_url,
                        quality, vertex_normals,
                        -- vertex_normals 必须显式给值：这一列的 DEFAULT 是 NULL
                        -- （= 未知，见 database.py 建表处），从列表里漏掉它，
                        -- 新作业的详情面板会显示「法线未知」——明明本次切片用的
                        -- 就是下面绑的这个 0/1。
                        started_at, completed_at, error_message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status='running',
                        output_dir=excluded.output_dir,
                        maxzoom=excluded.maxzoom,
                        parent_url=excluded.parent_url,
                        -- 这两列必须跟着一起更新：重切走的正是 DO UPDATE 分支，
                        -- 漏掉的话「改了档位重切」会沉默沿用上一轮的旧档位 ——
                        -- 产物没变、全程零报错，用户只会以为旋钮是假的。
                        quality=excluded.quality,
                        vertex_normals=excluded.vertex_normals,
                        -- 重切先把上一轮的产物事实清空：新档位切出来的实际层级
                        -- 要到收尾才知道，中间这段时间显示旧值等于撒谎。NULL 期间
                        -- 详情面板回落到 maxzoom（基准值）并标明。
                        effective_maxzoom=NULL,
                        started_at=excluded.started_at,
                        completed_at=NULL,
                        error_message=NULL
                    WHERE dem_terrain_jobs.status != 'running'
                    """,
                    (task_id, str(output_dir), maxzoom, parent_url,
                     quality, 1 if vertex_normals else 0, utc_now_iso()),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"DEM tiling job for task {task_id} is already running")
                conn.commit()
        except Exception:
            conn.rollback()
            # UPSERT 这一段失败（并发已在跑、库锁死）时线程还没接手凭据 ——
            # 不还的话一个任务槽就此蒸发，而调用方只看到一句 ValueError。
            # 只还**本次调用自己申请到的**那一张：release() 内部走 _on_release，
            # 它按身份把登记摘掉，所以不需要、也不该再按 owner 键补一刀 ——
            # 那一刀在并发下摘掉的可能正是别人刚拿到的凭据。
            reservation.release()
            raise
        finally:
            conn.close()

        # 切片线程与下载线程共用 stop_flags / active_tasks 两张表，而且两者
        # 确实会短暂并存：_execute 是先 commit dem_tasks 的 status='completed'、
        # 再 emit task_completed,下载线程要一路退回 _run_task 的
        # finally 才把自己从两张表里摘掉。任何调用方（详情弹窗点
        # 「开始切片」,或别的客户端直接打这个端点）都可能落在这段窗口里:
        # 状态闸门看到 'completed' 放行,下面两行会盖掉下载线程还在的登记。
        # 盖掉是安全的 —— 下载线程摘登记时做的是身份比较(_run_task 的 finally 里
        # 那两个 `is` 判断),盖掉之后它一条都不命中,什么都不摸。
        # 别把身份比较简化成无条件 pop。
        # 登记进 active_tasks 是 delete_task 的 is_alive() 守卫能看见它的前提。
        stop_flag = threading.Event()
        # 线程构造 + 登记必须与 th.start() 同处一个 try：job 行在上面那条
        # dem_terrain_jobs UPSERT 里已经 commit 成 running,
        # 而 threading.Thread(...) 构造本身、以及登记那两行
        # 都可能抛。抛在 try 外面的话下面的回补块够不着,job 行永久停在 running
        # （再次 start_tiling 被 `WHERE status != 'running'` 判为已在运行,
        # delete_task 也被挡,只能重启进程靠孤儿恢复解开）。
        # th 预置 None：构造就抛时 except 里的身份比较不能撞 NameError。
        th = None
        try:
            with self._state_lock:
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(
                    target=self._run_tiling_entry,
                    args=(task_id, task_dir, output_dir, maxzoom, parent_url, stop_flag,
                          quality, vertex_normals, reservation),
                    daemon=True,
                    name=f"DemTiling-{task_id}",
                )
                self.active_tasks[task_id] = th
            th.start()
        except Exception as e:
            # L2: 上面已把 job 行 upsert 成 running 并 commit。线程创建失败
            # (RuntimeError: can't start new thread)后不回补的话,job 行永久停在
            # running:再次 start_tiling 被 `WHERE status != 'running'` 判为「已在
            # 运行」而 ValueError,delete_task 也被 DB 状态检查挡住,而
            # src/routes/terrain_api.py 没有任何重置 job 的端点 ——
            # 只能重启进程让孤儿恢复解开。
            # job 行没有 paused 态,这里置 failed(与下载管线回退 paused 不同)。
            with self._state_lock:
                if self.active_tasks.get(task_id) is th:
                    self.active_tasks.pop(task_id, None)
                if self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            # 与 job 行的回补同一组：线程没起来，凭据也就没人还。
            # 同上：release() 自己就按身份摘登记，不再按 owner 键补第二刀。
            reservation.release()
            self._mark_tiling_job_failed(
                task_id, f"tiling thread failed to start: {e}")
            raise

    def _mark_tiling_job_failed(self, task_id: int, message: str) -> None:
        """把切片 job 行从 running 回补成 failed（L2 的线程启动失败路径）。

        终态的原因必须落进**任务自己的**日志（§4.5）。这条路径上没有现成的
        句柄：唯一调用点在 `start_tiling` 的「线程创建失败」分支，而开每任务
        日志是 `_run_tiling_job` 的第一件事 —— 线程压根没起来，那一行从来没
        被执行。只写全局 terraforge.log 的话，用户点开任务详情看到的是「失败」
        两个字加一片空白。
        """
        conn = get_connection()
        changed = False
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE dem_terrain_jobs SET status='failed', error_message=?, "
                "completed_at=? WHERE task_id=? AND status='running'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
            changed = bool(cur.rowcount)
        except Exception as e:
            logger.error(f"Failed to mark tiling job {task_id} as failed: {e}")
        finally:
            conn.close()
        if not changed:
            # 一行都没改（作业已经是终态，或行已被删）就没有终态需要解释。
            # 无条件写会让「已判 failed」这句话出现在一个其实跑完了的任务的
            # 日志里 —— 比不写更糟（同 fail_stranded_running_task 那几处的口径）。
            return
        try:
            tlog = open_task_log(_PIPELINE, task_id)
            try:
                tlog.event('terminal', status='failed',
                           reason='tiling_thread_start_failed', detail=message)
                tlog.error('切片线程未能启动，切片作业行已回补为 failed：%s', message)
            finally:
                tlog.close()
        except Exception as e:
            logger.warning(f"DEM task {task_id}: 切片启动失败日志写入失败（忽略）: {e!r}")

    def _run_tiling_entry(self, task_id: int, task_dir: Path, output_dir: Path,
                          maxzoom: int, parent_url: str,
                          stop_flag: Optional[threading.Event] = None,
                          quality: str = DEFAULT_TILING_QUALITY,
                          vertex_normals: bool = False, reservation=None) -> None:
        """切片线程的真正入口。多这一层的理由与 _run_task_entry 逐字相同：
        配额挂在线程上，_run_tiling_job 被替换或炸出 BaseException 时也还得回来。
        """
        try:
            self._run_tiling_job(task_id, task_dir, output_dir, maxzoom, parent_url,
                                 stop_flag, quality, vertex_normals,
                                 reservation=reservation)
        finally:
            # 只归还**交接给这个线程的那一张**。按 owner 键归还会在「本线程迟到
            # 收尾、用户已经把下一轮起起来了」的窗口里吊销下一轮的凭据，而那个
            # 窗口可以长达数十秒（fail_stranded_running_task 会新开一条
            # busy_timeout=30s 的 sqlite 连接）。身份比较之下，迟到的这一次
            # 直接返回 0，什么都不动。
            if reservation is not None:
                get_scheduler().release_owner(
                    (_PIPELINE, task_id, 'tiling'), reservation)

    def _run_tiling_job(self, task_id: int, task_dir: Path, output_dir: Path,
                        maxzoom: int, parent_url: str,
                        stop_flag: Optional[threading.Event] = None,
                        quality: str = DEFAULT_TILING_QUALITY,
                        vertex_normals: bool = False, reservation=None) -> None:
        failure = None
        # 每任务日志与下载线程共用同一个 pipeline/task_id —— 下载与切片是同一个
        # 任务的两个阶段，日志本来就该在一份文件里按时间顺序接上。
        tlog = open_task_log(_PIPELINE, task_id)
        # 授予的 CPU 名额必须真的传到进程池：不传的话 build_terrain 走它自己的
        # min(4, cpu_count)，两个地形任务并行就是 8 个重型 worker，全局上限白设。
        granted_workers = reservation.cpu_workers if reservation is not None else 0
        try:
            tlog.event('stage', name='tiling', maxzoom=maxzoom, quality=quality,
                       normals=bool(vertex_normals), workers=granted_workers or 'auto',
                       output_dir=str(output_dir))
            # 切片进度节流落库/emit（范本：contour_task_manager 渲染阶段的
            # render_progress）：build_terrain 逐瓦片回调，不节流时每次回调
            # 都是 UPDATE + commit + 广播，百万级瓦片会把切片拖垮、把前端打爆。
            # 距上次落库不足 _PROGRESS_EMIT_MIN_INTERVAL 且未处理完时只记
            # 内存；结束后强制 flush 保住节流窗口内最后一段计数。
            progress_conn = get_connection()
            try:
                # last_flush 初始 -inf:首次回调（0/total）必落库 —— 重启切片时
                # 也顺势清掉上一轮残留的进度计数。
                tiling_state = {"done": 0, "total": 0, "last_flush": float("-inf")}

                def _flush_tiling_progress() -> None:
                    # U1（落库侧）：本函数经 progress_cb 被 build_terrain 在瓦片
                    # 循环里**同步**调用，抛出会一路穿透 ex.map / ProcessPoolExecutor
                    # 到 _run_tiling_job 的 catch-all，把一个瓦片已 99% 落盘的作业
                    # 记成 failed；切片没有恢复模型（_worker_tile 不跳过已存在的
                    # 瓦片），重跑要从 z8 全量重算。rendered/total 只是展示字段，
                    # 一次 database is locked 或写满磁盘不该有作废产物的权力 ——
                    # 与下面的 emit 同一约定：只记日志。
                    # last_flush 无论成败都推进：失败后立刻重试只会在每张瓦片上
                    # 再撞一次同样的锁，把切片拖成串行等锁。
                    tiling_state["last_flush"] = time.monotonic()
                    try:
                        progress_conn.execute(
                            "UPDATE dem_terrain_jobs SET rendered_tiles=?, total_tiles=? WHERE task_id=?",
                            (tiling_state["done"], tiling_state["total"], task_id),
                        )
                        progress_conn.commit()
                    except Exception as db_error:
                        logger.warning(
                            f"DEM tiling job {task_id}: persist progress failed "
                            f"(ignored): {db_error}")
                    # getattr 而非 self.socketio:契约测试用 __new__ 构造的管理器
                    # 直调本方法（无 __init__、无 socketio 属性）验证失败落库路径。
                    socketio = getattr(self, "socketio", None)
                    if socketio:
                        # 专用事件而非 task_progress：job 行没有 dem 任务的计数
                        # 字段，混进 task_progress 会被前端按 task_type:task_id
                        # 当成 dem 任务行把计数冲掉（见 static/js/tasks.js）。
                        # 前端详情弹窗轮询 GET /api/terrain/dem/<id> 拿全行，
                        # 这发只是实时 nudge。
                        # U1：这发 emit 经 progress_cb 被 build_terrain 在瓦片
                        # 循环里同步调用，抛出会一路穿透把整个切片作业记成
                        # failed。与 task_manager 的收尾 emit 同一约定：只记日志。
                        try:
                            socketio.emit("terrain_job_progress", {
                                "task_id": task_id,
                                "task_type": "dem_terrain",
                                "status": "running",
                                "rendered_tiles": tiling_state["done"],
                                "total_tiles": tiling_state["total"],
                            })
                        except Exception as emit_error:
                            logger.warning(
                                f"DEM tiling job {task_id}: emit progress failed "
                                f"(ignored): {emit_error}")

                def tiling_progress(done: int, total: int) -> None:
                    tiling_state["done"] = done
                    tiling_state["total"] = total
                    if done < total and \
                            time.monotonic() - tiling_state["last_flush"] < _PROGRESS_EMIT_MIN_INTERVAL:
                        return
                    _flush_tiling_progress()

                # 瓦片循环之前的耗时阶段（多幅 DEM 物化 + 建金字塔）。它发生在
                # total 算出来之前，没有分母，所以走 stage 而不是 rendered/total。
                # 不落库：这是纯瞬时状态，作业记录里没有对应字段，而且落库会在
                # GDAL 的高频回调下变成每秒几十次写。
                stage_state = {"last_emit": float("-inf")}
                _STAGE_LABELS = {"merge": "合并 DEM", "overview": "建金字塔"}

                def tiling_stage(phase: str, fraction: float) -> None:
                    # 节流：GDAL 的原生回调频率不受我们控制（BuildOverviews 实测
                    # 每层多次）。首帧与末帧必发，中间按 _PROGRESS_EMIT_MIN_INTERVAL。
                    now = time.monotonic()
                    edge = fraction <= 0.0 or fraction >= 1.0
                    if not edge and now - stage_state["last_emit"] < _PROGRESS_EMIT_MIN_INTERVAL:
                        return
                    stage_state["last_emit"] = now
                    socketio = getattr(self, "socketio", None)
                    if not socketio:
                        return
                    # 与 _flush_tiling_progress 同一约定（U1）：这发 emit 经
                    # stage_cb 被 GDAL 的进度回调同步调用，抛出会一路穿透 ——
                    # 而 GDAL 把回调抛异常当成「用户请求中止」，实测会让
                    # gdal.Translate 返回 None、产物被删、整个作业失败。
                    try:
                        socketio.emit("terrain_job_progress", {
                            "task_id": task_id,
                            "task_type": "dem_terrain",
                            "status": "running",
                            "stage": phase,
                            "stage_label": _STAGE_LABELS.get(phase, phase),
                            "stage_fraction": max(0.0, min(1.0, float(fraction))),
                        })
                    except Exception as emit_error:
                        logger.warning(
                            f"DEM tiling job {task_id}: emit stage failed "
                            f"(ignored): {emit_error}")

                tiling_progress(0, 0)
                # `or {}`：多个契约测试直接 monkeypatch 掉 tile_dem_task_dir
                # 并返回 None，归一成空计数后行为与改动前一致（不判 failed）。
                counts = tile_dem_task_dir(
                    task_dir=task_dir,
                    out_dir=output_dir,
                    # maxzoom 一路传下来的是库形态：哨兵在这里还原成 None，
                    # 也就是 build_terrain 按源数据像素尺寸现算基准层级那一态。
                    # 传 -1 会被它当成显式层级钳成 0 —— 一张 z0 瓦片，作业照报
                    # completed。
                    params=TileParams(maxzoom=maxzoom_from_db(maxzoom),
                                      parent_url=parent_url,
                                      normals=vertex_normals,
                                      workers=granted_workers,
                                      level_offset=TILING_QUALITY_OFFSETS[quality],
                                      progress_cb=tiling_progress,
                                      stage_cb=tiling_stage,
                                      stop_flag=stop_flag,
                                      # 运行中复查（cesium_terrain 里那次
                                      # recheck_remaining）必须知道「本任务自己
                                      # 预留的那份 DISK_BYTES 不算别人占的」——
                                      # 不传 owner 的话它把本任务的预留读成他人
                                      # 预留，于是一台空机器上单个任务需要两倍
                                      # 空间，切到一半被自己的预留判死。
                                      # 必须用 reservation.owner 而不是手写
                                      # ('dem', task_id, 'tiling')：那是第二份
                                      # 事实来源，对不上时 _reserved_by_others
                                      # 按元组相等匹配，找不到就静默退回旧的
                                      # 双重计数，一声不吭。
                                      owner=(reservation.owner
                                             if reservation is not None
                                             else None)),
                ) or {}
                _flush_tiling_progress()
            finally:
                progress_conn.close()

            # M11: 消费 build_terrain 的失败计数（此前整个返回值被丢弃，逐瓦片
            # 容错因此变成纯静默：缺瓦片的作业照报 completed，layer.json 还按
            # 完整矩形声明 available）。对齐 contour 的收尾：rendered==0 判
            # failed，failed>0 记 warning 并写进 error_message。
            rendered = int(counts.get("rendered", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            total = int(counts.get("total", 0) or 0)
            # 实际切到的最深层级：档位偏移 + 钳位之后的**产物事实**，与
            # layer.json 的 maxzoom 同源。None = 切片器没回报（老测试替身），
            # 那时保持库里原值不动，由界面回落到基准值。
            effective_maxzoom = counts.get("max_level")
            stopped = stop_flag is not None and stop_flag.is_set()
            # 中途停止时 rendered 可以合法地是 0（刚进瓦片循环就被叫停），
            # 不豁免的话会被下面这条「切片器什么都没产出」的失败判据误命中。
            # 范本逐字对照 local_terrain_task_manager._run_tiling_job 的同一条判据。
            if total > 0 and rendered == 0 and not stopped:
                raise RuntimeError(
                    f"terrain tiling produced no tiles ({failed}/{total} failed)")
            warning = None
            if failed > 0:
                warning = f"部分地形瓦片切片失败({failed}/{total})"
                logger.warning(f"DEM tiling job {task_id}: {warning}")
                tlog.warning('%s', warning)
            tlog.event('tiles', rendered=rendered, failed=failed, total=total,
                       effective_maxzoom=effective_maxzoom
                       if effective_maxzoom is not None else 'unreported')

            if stopped:
                # 中途停止的唯一入口是删除任务（DEM 切片没有暂停/恢复语义）——
                # 正常情况下 dem_tasks 行连同 CASCADE 的 job 行此刻都已经不在了，
                # 写状态是静默 no-op，_emit_tiling_finished 也没有行可更新。
                # 但「行一定已经没了」这个假设不成立：task_deletion.delete_task_row
                # 的 commit 失败分支回滚 DELETE 却【不】回滚停止标志（有意的），
                # 于是行还在、标志已置、这里正常 return —— 没有异常给下面的兜底
                # except 接。那种行由 finally 的搁死补偿判 failed，不是靠这里。
                tlog.event('terminal', status='stopped',
                           reason='stop flag set (task deleted)',
                           rendered=rendered, total=total)
                return

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_terrain_jobs SET status='completed', completed_at=?, "
                    # COALESCE 而不是直接赋值：切片器没回报层级时（注入的替身）
                    # 不该把已有值抹成 NULL。
                    "error_message=?, effective_maxzoom=COALESCE(?, effective_maxzoom) "
                    # AND status='running'：终态 UPDATE 不能改写一条已经是终态
                    # 的记录（同 _mark_failed 的约定，local terrain 侧亦然）。
                    "WHERE task_id=? AND status='running'",
                    (utc_now_iso(), warning, effective_maxzoom, task_id),
                )
                conn.commit()
            finally:
                conn.close()
            tlog.event('terminal', status='completed', rendered=rendered,
                       failed=failed, total=total, warning=warning or '')
            self._emit_tiling_finished(task_id, "completed")

        except Exception as e:
            # failure 必须在开连接之前先记下：下面这句 get_connection() 自己就会抛
            # （库被锁/磁盘满），抛了就没人再写终态，只剩 finally 的搁死补偿，而它
            # 要拿这个原因写进 error_message。
            failure = e
            # GDAL 的失败在这里已经是一个 Python 异常了 —— 回溯必须进任务日志，
            # 「为什么切片失败」这句话的答案九成在那段回溯里（BuildVRT 丢源、
            # ENOSPC、BrokenProcessPool 各有各的形状）。
            tlog.exception('DEM 切片终态 failed：%s', e)
            tlog.event('terminal', status='failed', reason=str(e))
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_terrain_jobs SET status='failed', completed_at=?, "
                    # 同上：已落终态的 job 行不该被这条兜底再改一次。
                    "error_message=? WHERE task_id=? AND status='running'",
                    (utc_now_iso(), str(e), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            self._emit_tiling_finished(task_id, "failed")
            logger.error(f"DEM tiling job failed for task {task_id}: {e}")
        finally:
            # 配额与线程登记同生共死，所以两者在同一个 finally 里还。
            if reservation is not None:
                reservation.release()
            # 与 _run_task 同一约定：只在自己就是登记的那个线程/flag 时才摘。
            # 首先防的是并发重叠 —— start_tiling 里登记线程前那段注释说明了
            # 下载收尾与 start_tiling 有真实的窗口，谁被谁盖掉取决于抢锁顺序，被盖掉的
            # 一方靠这里的身份比较认出「表里的已经不是我」而收手；其次才是
            # 串行的下一轮 start_tiling 刚放进去的登记不能被上一轮误删。
            # 无条件 pop 会同时踩掉这两条。
            # getattr 而非 self._state_lock：与上面的 socketio 同一原因 ——
            # 契约测试用 __new__ 构造的管理器直调本方法，压根没有登记表，
            # 也就没有什么可摘的。
            state_lock = getattr(self, "_state_lock", None)
            # 没有登记表 = 契约测试用 __new__ 直调本方法，那种调用不可能有第二个
            # worker 来抢这一行，按「登记的就是自己」处理，补偿照常做。
            stranded_owner = True
            if state_lock is not None:
                with state_lock:
                    stranded_owner = (
                        self.active_tasks.get(task_id) is threading.current_thread())
                    if stranded_owner:
                        self.active_tasks.pop(task_id, None)
                    if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                        self.stop_flags.pop(task_id, None)
            if stranded_owner:
                # 行还停在 running 就是搁死了（理由与竞态分析见 helper 的
                # docstring）。盖住两条路：上面那个兜底 except 自己抛出去了，
                # 以及 stopped 分支正常 return 而行没被删掉。
                stranded_reason = (f'切片线程异常: {failure}' if failure is not None
                                   else '')
                if fail_stranded_running_task('dem_terrain_jobs', task_id,
                                              stranded_reason):
                    # 只在**真的改了行**（running → failed）时补这一笔。那个
                    # helper 只写全局日志，而 §4.5 的门槛是「任何终态都能从
                    # **任务自己的**日志解释原因」—— 没有下面两行，任务日志的
                    # 最后一句是某个瓦片的进度，库里却写着 failed，两份记录当面
                    # 打架。看返回值而不是猜：正常收尾时那条带
                    # `WHERE status='running'` 的 UPDATE 是无害的 no-op，那种
                    # 情况下多写一句「已判 failed」比不写更糟。
                    tlog.event('terminal', status='failed', reason='thread_stranded',
                               detail=stranded_reason or 'worker exited without settling the row')
                    tlog.error(
                        '切片线程退出时作业行仍停在 running，已由兜底判为 failed：%s',
                        stranded_reason or 'worker 没有走到任何终态写入')
            tlog.close()

    def _emit_tiling_finished(self, task_id: int, status: str) -> None:
        """切片作业收尾时补一发 terrain_job_progress。

        没有它，前端 updateTerrainJobProgress 里那条 `status !== 'running'
        → 清空 tiling_text` 的分支永远不会被触发：作业期间逐瓦片事件把
        「切片中 N / N」写进任务行，作业结束后这行字一直挂着，要刷新页面才
        消失。DEM 的切片跑在下载任务已 completed 之后，行本来就停在终态，
        没有任何别的事件会重建它。
        """
        socketio = getattr(self, "socketio", None)
        if not socketio:
            return
        try:
            socketio.emit("terrain_job_progress", {
                "task_id": task_id,
                "task_type": "dem_terrain",
                "status": status,
            })
        except Exception as emit_error:
            logger.warning(
                f"DEM tiling job {task_id}: emit finish failed "
                f"(ignored): {emit_error}")

    def get_tiling_job(self, task_id: int) -> Optional[Dict[str, Any]]:
        task_id = int(task_id)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dem_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"DEM task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = int(limit or 100)
        # 钳到 [1, 100] —— SQLite LIMIT -1 表示无上限、0 返回空，两者都是
        # 调用方 bug，回退到默认窗口（同 src/routes/api.py get_tasks 的约定）。
        if limit > 100:
            limit = 100
        if limit < 1:
            limit = 100
        conn = get_connection()
        try:
            cur = conn.cursor()
            # status='active' 是路由层契约的特殊值（同 /api/history_all）：
            # 展开成活动三态；其余取值（含 None）维持原行为。
            if status == 'active':
                cur.execute(
                    "SELECT * FROM dem_tasks "
                    "WHERE status IN ('pending','running','paused') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur.execute("SELECT * FROM dem_tasks ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def delete_task(self, task_id: int, artifact_dir=None, on_row_gone=None):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        切片线程自 v0.2.11 起也登记进 active_tasks / stop_flags（见 start_tiling），
        「切片中删除」因此走的是同一条后台路径 —— 等线程收工再删产物，不再需要
        dem_terrain_jobs 那道单独的守卫来拒绝。job 行本身仍由
        _recover_orphan_running_tasks 在进程重启后收拾孤儿。

        on_row_gone 由调用方给：清 /terrain/dem 静态路由缓存的那个 hook 依赖
        Flask 请求上下文（走 current_app.extensions），放在这里等于让服务层持有
        一个只对路由调用方有效的回调，非路由调用方那里它会静默失效。
        """
        from src.services.task_deletion import delete_task_row

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="dem_tasks",
            artifact_dir=artifact_dir,
            on_row_gone=on_row_gone,
        )

    def _run_task_entry(self, task_id: int, stop_flag: Optional[threading.Event] = None,
                        reservation=None) -> None:
        """下载线程的**真正**入口：配额的归还挂在线程上，而不是挂在 _run_task 里面。

        为什么多这一层：TASK_SLOT 泄漏一份就少一个全局任务名额，而且是永久的
        （进程重启才清），表现是「所有管线突然都起不了任务，日志里一句话都没有」。
        _run_task 自己的 finally 已经归还一次；这一层兜的是它够不着的形态 ——
        它被子类/替身换掉、或者在 finally 之外炸出 BaseException。release 幂等，
        两处都跑不会把配额还两次。
        """
        try:
            self._run_task(task_id, stop_flag, reservation=reservation)
        finally:
            # 同 _run_tiling_entry：只还交接给本线程的那一张。按 owner 键还会在
            # 「暂停后立刻恢复」这种再普通不过的操作里吊销新一轮的凭据。
            if reservation is not None:
                get_scheduler().release_owner(
                    (_PIPELINE, task_id, 'download'), reservation)

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None,
                  reservation=None) -> None:
        failure = None
        # 每任务日志（§4.5）。open_task_log 永不返回 None、所有方法都不抛，
        # 所以下面不需要任何 if tlog。
        tlog = open_task_log(_PIPELINE, task_id)
        try:
            granted = reservation.granted if reservation is not None else {}
            tlog.event('task_start', pipeline=_PIPELINE, task_id=task_id,
                       **{kind.value: n for kind, n in granted.items()})
            asyncio.run(self._execute(
                task_id, stop_flag, tlog=tlog,
                max_connections=(reservation.network if reservation is not None else None)))
        except Exception as e:
            logger.error(f"DEM task {task_id} thread failed: {e}")
            # 终态的原因必须落在**这个任务自己的**日志里：用户点开的是任务详情，
            # 不是全局 terraforge.log。
            tlog.exception(f"DEM 下载线程异常退出: {e}")
            failure = e
        finally:
            # 配额与线程登记一起还：两者的生命周期完全相同（这个线程活着的时段）。
            if reservation is not None:
                reservation.release()
            with self._state_lock:
                deregistered = (
                    self.active_tasks.get(task_id) is threading.current_thread())
                if deregistered:
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            if deregistered:
                # 行还停在 running 就是搁死了（理由与竞态分析见 helper 的 docstring）。
                stranded_reason = f'线程异常: {failure}' if failure is not None else ''
                if fail_stranded_running_task('dem_tasks', task_id, stranded_reason):
                    # 只在真的改了行时补终态记录，理由同 _run_tiling_job 里那处。
                    tlog.event('terminal', status='failed', reason='thread_stranded',
                               detail=stranded_reason or 'worker exited without settling the row')
                    tlog.error(
                        '线程退出时任务行仍停在 running，已由兜底判为 failed：%s',
                        stranded_reason or 'worker 没有走到任何终态写入')
            tlog.event('task_thread_exit', failure=str(failure) if failure else '')
            tlog.close()

    async def _execute(self, task_id: int, stop_flag: Optional[threading.Event] = None,
                       *, tlog=None, max_connections: Optional[int] = None) -> None:
        """执行一个 DEM 下载任务。

        tlog / max_connections 都是 keyword-only 且可缺省：大量契约测试直接
        `asyncio.run(mgr._execute(task_id))` 或 `(task_id, None)` 调这个方法，
        它们测的是记账与状态机，不该被这两个新参数逼着改签名。
        tlog 缺省时退化成一个只写全局日志的句柄（open_task_log 自己就是这么
        降级的），max_connections 缺省时引擎回落到读 concurrent_downloads 配置。
        """
        if tlog is None:
            tlog = open_task_log(_PIPELINE, task_id)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dem_tasks WHERE id = ?", (task_id,))
            task = cur.fetchone()
            if not task:
                raise ValueError(f"DEM task {task_id} not found")

            dataset = task["dataset"]
            output_dir = resolve_stored_output_dir(task["output_path"]) / f"dem_task_{task_id}"
            tlog.event('stage', name='download', dataset=dataset,
                       output_dir=str(output_dir), connections=max_connections or 'config')

            # C4: 暂停/崩溃时下载中的文件停留在 downloading —— 恢复时重新入队，
            # 否则下面的查询会跳过它们、终态统计也漏掉（任务被误报 completed）。
            cur.execute(
                "UPDATE dem_files SET status='pending' WHERE task_id=? AND status='downloading'",
                (task_id,),
            )
            conn.commit()

            cur.execute(
                """
                SELECT granule_id FROM dem_files
                WHERE task_id=? AND status IN ('pending','failed')
                ORDER BY granule_id
                """,
                (task_id,),
            )
            granules = [r["granule_id"] for r in cur.fetchall()]
            tlog.event('granules', pending=len(granules),
                       total=int(task["total_files"] or 0))

            stop_ev = asyncio.Event()
            if stop_flag and stop_flag.is_set():
                stop_ev.set()

            # 进度记账（同步 sqlite I/O：新开连接 + SELECT 状态 + UPDATE + commit）
            # 整体放 worker 线程 —— 回调在下载事件循环里被 await，直接在循环
            # 上跑会堵住所有并发颗粒的下载协程。只回传计数增量（deltas）：
            # SELECT * 全行挪到真正要 emit 的分支（见 progress），不广播的
            # 回调不做这次全行查询。
            def _record_progress(granule_id: str, status: str, error: Optional[str],
                                 size_bytes: Optional[int]) -> tuple[int, int]:
                tile_conn = get_connection()
                try:
                    c = tile_conn.cursor()
                    c.execute(
                        "SELECT status FROM dem_files WHERE task_id=? AND granule_id=?",
                        (task_id, granule_id),
                    )
                    existing = c.fetchone()
                    old_status = existing["status"] if existing else None

                    c.execute(
                        """
                        UPDATE dem_files SET status=?, error_message=?, size_bytes=?, local_path=?
                        WHERE task_id=? AND granule_id=?
                        """,
                        # I14: COP-DEM 的 granule_id 是嵌套路径，实际落盘是
                        # basename（见引擎 local_name），local_path 与之保持一致。
                        (status, error, size_bytes, str(output_dir / Path(granule_id).name), task_id, granule_id),
                    )
                    downloaded_delta, failed_delta = _status_count_deltas(old_status, status)
                    if downloaded_delta or failed_delta:
                        c.execute(
                            """
                            UPDATE dem_tasks
                            SET downloaded_files = MAX(downloaded_files + ?, 0),
                                failed_files = MAX(failed_files + ?, 0)
                            WHERE id=?
                            """,
                            (downloaded_delta, failed_delta, task_id),
                        )
                    tile_conn.commit()
                    return downloaded_delta, failed_delta
                finally:
                    tile_conn.close()

            def _fetch_task_row() -> Optional[Dict[str, Any]]:
                row_conn = get_connection()
                try:
                    c = row_conn.cursor()
                    c.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,))
                    trow = c.fetchone()
                    return dict(trow) if trow else None
                finally:
                    row_conn.close()

            # emit 节流（与 map/contour 对齐的严格时间窗）：距上次广播不足
            # _PROGRESS_EMIT_MIN_INTERVAL 且未到最后一颗时只落库不广播；
            # 计数取内存累计值（每回调已逐次落库，实时进度不必再查 DB），
            # done 达 total_files 的末发必发。不再有「计数变化必发」豁免 ——
            # 颗粒集中完成时豁免会让时间窗形同虚设；任务级状态变更由收尾的
            # task_completed/task_failed 事件覆盖，payload 结构不变（task 整行
            # + task_type）。
            progress_counts = {
                "downloaded": int(task["downloaded_files"] or 0),
                "failed": int(task["failed_files"] or 0),
            }
            total_files = int(task["total_files"] or 0)
            last_emit_at = float("-inf")
            # 下载吞吐计。字节**只**来自引擎的在途回调（bytes_callback）：单颗
            # DEM 是 30-50MB 的 COG，走完要几分钟，而颗粒级状态回调
            # （downloading → completed）在这几分钟里一次都不发 —— 只按收尾的
            # size_bytes 记账的话速率是脉冲式的，前端 5s 就判过期、把行上的速度
            # 显示成 0 B/s（static/js/task_list.js 的 SPEED_STALE_MS）。
            #
            # 顺带解决了旧口径的坑：size_bytes 是双重用途的（还要写进
            # dem_files.size_bytes 列），缓存命中 / 文件已存在时引擎照样上报真实
            # 大小，直接当网络字节会让速度虚高一个数量级。在途回调只在真的读到
            # 网络字节时才触发，缓存命中天然不进这条路，判别逻辑可以整个删掉。
            speed_meter = SpeedMeter()

            async def _maybe_emit() -> None:
                nonlocal last_emit_at
                if not self.socketio:
                    return
                done = progress_counts["downloaded"] + progress_counts["failed"]
                now = time.monotonic()
                if done < total_files and now - last_emit_at < _PROGRESS_EMIT_MIN_INTERVAL:
                    return
                last_emit_at = now
                row = await asyncio.to_thread(_fetch_task_row)
                if not row:
                    return
                row["task_type"] = "dem"
                # 瞬时网络吞吐(字节/秒)。dem_tasks 表没有这一列,只活在推送里。
                row["download_speed_bps"] = round(speed_meter.bps())
                self.socketio.emit("task_progress", row)

            async def on_bytes(granule_id: str, n_bytes: int) -> None:
                speed_meter.record(n_bytes)
                await _maybe_emit()

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                # record(0) 是在推**时间窗**，不是记字节：只在有字节时 record，
                # 下载停滞/失败时速率会一直冻在最后那个高值上（见 download_speed）。
                speed_meter.record(0)
                downloaded_delta, failed_delta = await asyncio.to_thread(
                    _record_progress, granule_id, status, error, size_bytes)
                progress_counts["downloaded"] += downloaded_delta
                progress_counts["failed"] += failed_delta
                # 只数**终态**：'downloading' 会在同一颗上先来一发，'pending'
                # （暂停回写）压根不是尘埃落定。数错的后果是剩余量偏小、复查偏松。
                if status in ("completed", "skipped", "failed"):
                    settled["n"] += 1
                await _maybe_emit()

            # ---- 运行中磁盘复查（§4.2）--------------------------------------
            # 准入时 start_task 已经做过一次 check_budget，但那是**任务排队之前**
            # 的一张快照：排队等名额、下载跑几十分钟，这期间另一个任务、另一个
            # 进程、用户自己拷东西都能把盘吃掉。不复查的话终点是 ENOSPC —— 边写
            # 边落盘的 COG 留下一份非空半成品，而断点判定是「存在且非空就跳过」，
            # 于是下一轮把截断文件当成下好的（disk_budget 模块 docstring 的头一段）。
            #
            # 剩余工作量按**还没尘埃落定的颗粒数**现算：传死值等于跑到后半程还在
            # 要求整个任务的空间，必然误判（recheck_remaining 的 docstring）。
            settled = {"n": 0}
            owner = (_PIPELINE, task_id, 'download')

            def _remaining_estimate():
                pending = max(0, len(granules) - settled["n"])
                if pending == 0:
                    return None
                return disk_budget.estimate_dem_task(pending, dataset)

            recheck = disk_budget.RunningRecheck(
                output_dir, _remaining_estimate,
                owner=owner, config_manager=self.config,
                # 通过与否都记一行：估算错的时候第一件事就是回头看这行的数字。
                on_verdict=lambda v: tlog.event(
                    'disk_recheck', ok=v.ok, free=v.free_bytes,
                    required=v.required_bytes, shortfall=v.shortfall_bytes,
                    reason=v.reason))

            # Wire stop flag polling: map threading.Event -> asyncio.Event
            async def stop_watcher():
                while True:
                    if stop_flag and stop_flag.is_set():
                        stop_ev.set()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(stop_watcher())
            try:
                download_kwargs = dict(
                    dataset=dataset,
                    granules=granules,
                    output_dir=output_dir,
                    progress_callback=progress,
                    bytes_callback=on_bytes,
                    stop_flag=stop_ev,
                )
                # 授予的连接数与磁盘复查只在引擎**认得**这两个参数时才传。
                # self.engine 是注入点，十来个契约测试塞的是位置签名的替身
                # （dataset, granules, output_dir, progress_callback, stop_flag,
                # bytes_callback=None）—— 它们测的是记账与状态机，无条件多塞
                # 关键字会让它们全部 TypeError。真引擎恒有这两个参数，所以生产
                # 路径一定按授予量开连接、一定复查磁盘。
                try:
                    engine_params = inspect.signature(
                        self.engine.download_files).parameters
                except (TypeError, ValueError):
                    # 取不出签名的可调用对象（内建 / C 实现的替身）：按「都不认得」
                    # 处理，退回改造前的调用形态，而不是让任务在这里炸。
                    engine_params = {}
                if max_connections and 'max_concurrent' in engine_params:
                    download_kwargs['max_concurrent'] = max_connections
                if 'disk_recheck' in engine_params:
                    download_kwargs['disk_recheck'] = recheck
                await self.engine.download_files(**download_kwargs)
            finally:
                watcher.cancel()

            if recheck.blocked is not None:
                # 盘在跑到一半时不够了。引擎已经按「暂停」那条路径干净收手
                # （在途颗粒回写 pending），这里补上状态与**原因** —— 只收手不
                # 写原因的话，用户看到的是一个没有任何解释的 paused。
                #
                # 落 paused 而不是 failed：failed 不在 start_task 的准入白名单
                # （'pending','paused'）里，判成 failed 等于「腾出空间也点不动
                # 恢复」，把一个可恢复的处境变成死局。
                reason = recheck.blocked.reason
                tlog.event('terminal', status='paused', reason='disk_budget',
                           shortfall=recheck.blocked.shortfall_bytes, detail=reason)
                tlog.error('磁盘空间在下载途中不够了，任务已暂停：%s', reason)
                logger.warning(f"DEM task {task_id} paused by the disk recheck: {reason}")
                cur.execute(
                    "UPDATE dem_tasks SET status='paused', error_message=? "
                    "WHERE id=? AND status='running'",
                    (reason, task_id),
                )
                conn.commit()
                if cur.rowcount and self.socketio:
                    try:
                        self.socketio.emit("task_paused", {
                            "task_id": task_id, "task_type": "dem",
                            "status": "paused", "error_message": reason})
                    except Exception as emit_error:
                        logger.warning(
                            f"DEM task {task_id}: emit task_paused failed (ignored): {emit_error}")
                return

            if stop_ev.is_set():
                tlog.event('terminal', status='stopped',
                           reason='stop flag set (pause or delete)')
                return

            cur.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,))
            current = cur.fetchone()
            # 只剩 "paused" 要挡：用户明确按了暂停，收尾不得改写它。行不在了
            # （被删）同样直接退出。
            if not current or current["status"] == "paused":
                tlog.event('terminal', status=(current["status"] if current else 'deleted'),
                           reason='row is paused or gone; finaliser stood down')
                return

            cur.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN status NOT IN ('completed','skipped','failed') THEN 1 ELSE 0 END) AS pending_count
                FROM dem_files
                WHERE task_id = ?
                """,
                (task_id,),
            )
            counts = cur.fetchone()
            failed_count = counts["failed_count"] or 0
            pending_count = counts["pending_count"] or 0

            if failed_count > 0 or pending_count > 0:
                error_message = f"{failed_count} DEM file(s) failed, {pending_count} DEM file(s) pending"
                tlog.error('DEM 任务终态 failed：%s', error_message)
                tlog.event('terminal', status='failed', failed=failed_count,
                           pending=pending_count)
                cur.execute(
                    "UPDATE dem_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                    (error_message, utc_now_iso(), task_id),
                )
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "dem", "status": "failed", "error_message": error_message})
                return

            cur.execute("UPDATE dem_tasks SET status='completed', completed_at=? WHERE id=? AND status='running'", (utc_now_iso(), task_id))
            conn.commit()
            tlog.event('terminal', status='completed',
                       total=int(task["total_files"] or 0))
            if cur.rowcount and self.socketio:
                # M1: emit 在 completed 落库之后才跑,抛异常会落到兜底 except 把
                # 这条终态记录改写成 failed —— 必须自带 try 只记日志。
                try:
                    self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "dem", "status": "completed"})
                except Exception as emit_error:
                    logger.warning(f"DEM task {task_id}: emit task_completed failed (ignored): {emit_error}")

        except Exception as e:
            tlog.exception('DEM 任务终态 failed（执行异常）：%s', e)
            tlog.event('terminal', status='failed', reason=str(e))
            try:
                cur = conn.cursor()
                # M1: 'completed' 必须在排除列表里 —— 终态记录绝不可被改写；
                # 'paused' 是用户的明确意图，失败兜底也不该把它抢走。
                cur.execute(
                    "UPDATE dem_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('paused', 'completed')",
                    (str(e), utc_now_iso(), task_id),
                )
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "dem", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark DEM task {task_id} as failed: {e2}")
            raise
        finally:
            conn.close()
