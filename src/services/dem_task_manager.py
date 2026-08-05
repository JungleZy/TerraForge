"""
DEM Task Manager

Creates and runs DEM download tasks backed by dem_tasks/dem_files tables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.database import get_connection, utc_now_iso
from src.services.config_manager import ConfigManager
from src.services.dem_download_engine import DemDownloadEngine
from src.services.geo_validation import require_absolute_output_dir, resolve_output_dir, sanitize_filename, validate_bbox
from src.services.dem_granules import (
    tiles_for_bbox, astgtm_v3_granules_for_tile, copernicus_glo30_granules_for_tile,
)
from src.services.task_cleanup import resolve_stored_output_dir
from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir
from src.services.terrain_tiling.layer_json import parent_url_if_base_available

logger = logging.getLogger(__name__)

# task_progress 广播最小间隔（秒）：进度回调每颗粒触发，逐次 emit 会把前端
# 打爆；严格时间窗节流，无「计数变化必发」豁免 —— 颗粒集中完成时每个完成
# 回调都改计数，豁免会让窗口形同虚设（范本：task_manager.PROGRESS_EMIT_MIN_INTERVAL）。
_PROGRESS_EMIT_MIN_INTERVAL = 1.0


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
            cur.execute("SELECT id FROM dem_terrain_jobs WHERE status = 'running'")
            job_ids = [row['id'] for row in cur.fetchall()]
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
        except Exception as e:
            logger.error(f"Failed to recover DEM orphan tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def create_task(self, params: dict) -> int:
        # NOTE: Keep signature compatible-ish with existing API patterns (dict in, id out).
        name = sanitize_filename(params.get("name") or "DEM Task")
        # 四至共用校验(范围/顺序/NaN/类型),见 src/services/geo_validation.py
        north, south, east, west = validate_bbox(
            params.get("north"), params.get("south"),
            params.get("east"), params.get("west"),
        )
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

        # Compute granule list
        tiles = tiles_for_bbox(north=north, south=south, east=east, west=west)
        granules: List[str] = []
        for t in tiles:
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

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO dem_tasks (
                    name, status, north, south, east, west,
                    dataset, output_path, download_num, download_swb,
                    total_files, downloaded_files, failed_files
                )
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (name, north, south, east, west, dataset, output_path, download_num, download_swb, total_files),
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
        try:
            cur = conn.cursor()
            with self._state_lock:
                active_thread = self.active_tasks.get(task_id)
                if active_thread and active_thread.is_alive():
                    raise ValueError(f"DEM task {task_id} is already running")

                cur.execute("SELECT status FROM dem_tasks WHERE id = ?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"DEM task {task_id} not found")
                if row["status"] not in ("pending", "paused"):
                    raise ValueError(f"Cannot start DEM task {task_id} with status '{row['status']}'")

                cur.execute(
                    "UPDATE dem_tasks SET status='running', started_at=? WHERE id=? AND status IN ('pending','paused')",
                    (utc_now_iso(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"DEM task {task_id} could not be started because its status changed")
                conn.commit()

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag), daemon=True, name=f"DemTask-{task_id}")
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
        finally:
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

    def cancel_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE dem_tasks SET status='cancelled' WHERE id=? AND status IN ('pending','running','paused')", (task_id,))
            if cur.rowcount == 0:
                cur.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"DEM task {task_id} not found")
                # 与 pause_task 一致：终态（completed/failed/cancelled）不可取消，
                # 抛错而非静默成功。
                raise ValueError(f"Cannot cancel DEM task {task_id} with status '{row['status']}'")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    @staticmethod
    def _resolve_task_output_dir(output_path: str) -> Path:
        """相对 output_path 相对 Config.DOWNLOADS_DIR 解析（不依赖进程 CWD）。

        创建任务时 output_path 已经过 resolve_output_dir 校验并存为绝对路径；
        这里兼容历史任务入库的相对路径（按 DOWNLOADS_DIR 解析）与绝对路径（原样）。
        """
        p = Path(output_path)
        if p.is_absolute():
            return p
        return Path(resolve_output_dir(output_path))

    def start_tiling(self, task_id: int) -> None:
        task_id = int(task_id)

        # Resolve task output path first; tiling is based on existing DEM outputs.
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, output_path FROM dem_tasks WHERE id = ?", (task_id,))
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
        finally:
            conn.close()

        # M20: layer.json 的 parentUrl 指向全局 base，写死 localhost:5000 会在
        # 非 5000 端口/反代部署下 404 —— 可配置，默认值保持现状兼容。
        #
        # 两道闸门缺一不可（见 layer_json）：目录形式（带 /layer.json 会让 Cesium
        # 请求 .../layer.json/layer.json）+ base 真的存在。任一不满足都是 404，
        # 而 Cesium 对 404 的处理是塞假 heightmap 图层并污染共享 builder ⇒
        # 本任务自己的 quantized-mesh 瓦片也按 heightmap 解析，高程全错且不报错。
        # 全球 base 是可选产物，「没建」是默认装机的常态，所以这里必须放行 None。
        base_dir = resolve_stored_output_dir(
            self.config.get("terrain_global_base_path", "./downloads/terrain/base_z8"))
        parent_url = parent_url_if_base_available(
            self.config.get("terrain_base_parent_url", "")
            or "http://localhost:5000/terrain/base",
            base_dir,
        )

        maxzoom_raw = self.config.get("terrain_local_maxzoom", "14")
        try:
            maxzoom = int(maxzoom_raw) if maxzoom_raw is not None else 14
        except Exception:
            maxzoom = 14

        task_dir = self._resolve_task_output_dir(output_path) / f"dem_task_{task_id}"
        output_dir = task_dir / "terrain_tiles"

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
                        started_at, completed_at, error_message
                    )
                    VALUES (?, 'running', ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status='running',
                        output_dir=excluded.output_dir,
                        maxzoom=excluded.maxzoom,
                        parent_url=excluded.parent_url,
                        started_at=excluded.started_at,
                        completed_at=NULL,
                        error_message=NULL
                    WHERE dem_terrain_jobs.status != 'running'
                    """,
                    (task_id, str(output_dir), maxzoom, parent_url, utc_now_iso()),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"DEM tiling job for task {task_id} is already running")
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        th = threading.Thread(
            target=self._run_tiling_job,
            args=(task_id, task_dir, output_dir, maxzoom, parent_url),
            daemon=True,
            name=f"DemTiling-{task_id}",
        )
        try:
            th.start()
        except Exception as e:
            # L2: 上面已把 job 行 upsert 成 running 并 commit。线程创建失败
            # (RuntimeError: can't start new thread)后不回补的话,job 行永久停在
            # running:再次 start_tiling 被 `WHERE status != 'running'` 判为「已在
            # 运行」而 ValueError,delete_task 也被 DB 状态检查挡住(tiling 线程
            # 不登记进 active_tasks,is_alive() 拦不住),而 src/routes/terrain_api.py
            # 没有任何 cancel/reset job 的端点 —— 只能重启进程让孤儿恢复解开。
            # job 行没有 paused 态,这里置 failed(与下载管线回退 paused 不同)。
            self._mark_tiling_job_failed(
                task_id, f"tiling thread failed to start: {e}")
            raise

    def _mark_tiling_job_failed(self, task_id: int, message: str) -> None:
        """把切片 job 行从 running 回补成 failed（L2 的线程启动失败路径）。"""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE dem_terrain_jobs SET status='failed', error_message=?, "
                "completed_at=? WHERE task_id=? AND status='running'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to mark tiling job {task_id} as failed: {e}")
        finally:
            conn.close()

    def _run_tiling_job(self, task_id: int, task_dir: Path, output_dir: Path, maxzoom: int, parent_url: str) -> None:
        try:
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
                    progress_conn.execute(
                        "UPDATE dem_terrain_jobs SET rendered_tiles=?, total_tiles=? WHERE task_id=?",
                        (tiling_state["done"], tiling_state["total"], task_id),
                    )
                    progress_conn.commit()
                    tiling_state["last_flush"] = time.monotonic()
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
                    params=TileParams(maxzoom=maxzoom, parent_url=parent_url,
                                      progress_cb=tiling_progress,
                                      stage_cb=tiling_stage),
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
            if total > 0 and rendered == 0:
                raise RuntimeError(
                    f"terrain tiling produced no tiles ({failed}/{total} failed)")
            warning = None
            if failed > 0:
                warning = f"部分地形瓦片切片失败({failed}/{total})"
                logger.warning(f"DEM tiling job {task_id}: {warning}")

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_terrain_jobs SET status='completed', completed_at=?, error_message=? WHERE task_id=?",
                    (utc_now_iso(), warning, task_id),
                )
                conn.commit()
            finally:
                conn.close()

        except Exception as e:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_terrain_jobs SET status='failed', completed_at=?, error_message=? WHERE task_id=?",
                    (utc_now_iso(), str(e), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            logger.error(f"DEM tiling job failed for task {task_id}: {e}")

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

    def delete_task(self, task_id: int) -> None:
        """删除任务行。与 start_task/start_tiling 同一把 _state_lock 锁内复查
        下载线程 + dem_tasks 状态 + dem_terrain_jobs 状态:下载中或 tiling 中的
        任务抛 ValueError 拒删 —— tiling 中删除会 rmtree 正在被 GDAL 写入的
        terrain_tiles/,job 行被 ON DELETE CASCADE 删掉,结果静默丢失
        (范本: contour_task_manager.delete_task)。
        磁盘产物清理由路由层负责(delete_files)。"""
        conn = get_connection()
        try:
            cur = conn.cursor()
            with self._state_lock:
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(
                        f"Cannot delete running DEM task {task_id}. Please pause or cancel it first.")
                row = cur.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"DEM task {task_id} not found")
                if row["status"] == "running":
                    raise ValueError(
                        f"Cannot delete running DEM task {task_id}. Please pause or cancel it first.")
                job = cur.execute(
                    "SELECT status FROM dem_terrain_jobs WHERE task_id=?", (task_id,)).fetchone()
                if job and job["status"] == "running":
                    raise ValueError(
                        f"Cannot delete DEM task {task_id} while terrain tiling is running. "
                        "Wait for it to finish first.")
                cur.execute("DELETE FROM dem_tasks WHERE id=?", (task_id,))
                conn.commit()
        finally:
            conn.close()

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        try:
            asyncio.run(self._execute(task_id, stop_flag))
        except Exception as e:
            logger.error(f"DEM task {task_id} thread failed: {e}")
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)

    async def _execute(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dem_tasks WHERE id = ?", (task_id,))
            task = cur.fetchone()
            if not task:
                raise ValueError(f"DEM task {task_id} not found")

            dataset = task["dataset"]
            output_dir = self._resolve_task_output_dir(task["output_path"]) / f"dem_task_{task_id}"

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

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                nonlocal last_emit_at
                # Mirror existing naming: emit task_progress updates.
                downloaded_delta, failed_delta = await asyncio.to_thread(
                    _record_progress, granule_id, status, error, size_bytes)
                progress_counts["downloaded"] += downloaded_delta
                progress_counts["failed"] += failed_delta
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
                self.socketio.emit("task_progress", row)

            # Wire stop flag polling: map threading.Event -> asyncio.Event
            async def stop_watcher():
                while True:
                    if stop_flag and stop_flag.is_set():
                        stop_ev.set()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(stop_watcher())
            try:
                await self.engine.download_files(
                    dataset=dataset,
                    granules=granules,
                    output_dir=output_dir,
                    progress_callback=progress,
                    stop_flag=stop_ev,
                )
            finally:
                watcher.cancel()

            if stop_ev.is_set():
                return

            cur.execute("SELECT status FROM dem_tasks WHERE id=?", (task_id,))
            current = cur.fetchone()
            if not current or current["status"] in ("cancelled", "paused"):
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
            if cur.rowcount and self.socketio:
                # M1: emit 在 completed 落库之后才跑,抛异常会落到兜底 except 把
                # 这条终态记录改写成 failed —— 必须自带 try 只记日志。
                try:
                    self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "dem", "status": "completed"})
                except Exception as emit_error:
                    logger.warning(f"DEM task {task_id}: emit task_completed failed (ignored): {emit_error}")

        except Exception as e:
            try:
                cur = conn.cursor()
                # M1: 'completed' 必须在排除列表里 —— 终态记录绝不可被改写。
                cur.execute(
                    "UPDATE dem_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled', 'paused', 'completed')",
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
