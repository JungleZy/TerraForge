"""
Contour Task Manager

One-stop pipeline: download ASTER DEM granules (reuses DemDownloadEngine), then
render brown contour PNG tiles (contour_task_tiler). Lifecycle/threading mirror
DemTaskManager (active_tasks + stop_flags + orphan recovery).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from database import get_connection
from services.config_manager import ConfigManager
from services.dem_download_engine import DemDownloadEngine
from services.dem_granules import (
    tiles_for_bbox, astgtm_v3_granules_for_tile, astwbd_v1_att_granules_for_tile,
    copernicus_glo30_granules_for_tile, coverage_bbox,
)

logger = logging.getLogger(__name__)


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status == "completed") - int(old_status == "completed")
    failed_delta = int(new_status == "failed") - int(old_status == "failed")
    return downloaded_delta, failed_delta


class ContourTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.engine = DemDownloadEngine()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM contour_tasks WHERE status = 'running'")
            task_ids = [row["id"] for row in cur.fetchall()]
            if task_ids:
                cur.executemany(
                    "UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'",
                    [(tid,) for tid in task_ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan contour tasks (paused): {task_ids}")
        except Exception as e:
            logger.error(f"Failed to recover contour orphan tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def create_task(self, params: dict) -> int:
        name = params.get("name") or "Contour Task"
        north = float(params["north"]); south = float(params["south"])
        east = float(params["east"]); west = float(params["west"])
        dataset = params.get("dataset") or "COP-DEM-GLO-30"
        if dataset not in ("ASTGTM.003", "COP-DEM-GLO-30"):
            raise ValueError(f"Unsupported dataset: {dataset}")

        interval_raw = params.get("contour_interval")
        if interval_raw in (None, ""):
            interval_raw = self.config.get("contour_default_interval", "50")
        interval = float(interval_raw)
        if interval <= 0:
            raise ValueError(f"contour_interval must be > 0, got {interval}")

        zoom_min = int(params.get("zoom_min", 12))
        zoom_max = int(params.get("zoom_max", 14))
        if zoom_min > zoom_max:
            raise ValueError(f"zoom_min ({zoom_min}) must be <= zoom_max ({zoom_max})")

        background = params.get("background") or "#FAF6EC"
        if background != "transparent" and not str(background).startswith("#"):
            background = "#FAF6EC"

        def _flag(key: str, default: int = 1) -> int:
            return 1 if str(params.get(key, default)).strip().lower() in ("1", "true", "yes", "on") else 0
        terrain_shade = _flag("terrain_shade")
        water = _flag("water")

        output_path = params.get("output_path") or str(Path(Config.DOWNLOADS_DIR) / "dem")

        tiles = tiles_for_bbox(north=north, south=south, east=east, west=west)
        dem_granules: List[str] = []
        for t in tiles:
            if dataset == "COP-DEM-GLO-30":
                dem_granules.extend(copernicus_glo30_granules_for_tile(t))
            else:
                dem_granules.extend(astgtm_v3_granules_for_tile(t, include_num=False, include_swb=False))
        att_granules: List[str] = []
        if water:
            for t in tiles:
                att_granules.extend(astwbd_v1_att_granules_for_tile(t))
        total_files = len(dem_granules) + len(att_granules)

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO contour_tasks (
                    name, status, north, south, east, west, dataset,
                    contour_interval, background, terrain_shade, water,
                    zoom_min, zoom_max, output_path,
                    total_files, downloaded_files, failed_files,
                    total_tiles, rendered_tiles, failed_tiles
                )
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
                """,
                (name, north, south, east, west, dataset,
                 interval, background, terrain_shade, water,
                 zoom_min, zoom_max, output_path, total_files),
            )
            task_id = cur.lastrowid
            file_rows = [(task_id, g, "dem") for g in dem_granules] + \
                        [(task_id, g, "water") for g in att_granules]
            cur.executemany(
                "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count) VALUES (?, ?, ?, 'pending', 0)",
                file_rows,
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
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(f"Contour task {task_id} is already running")
                cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                if row["status"] not in ("pending", "paused"):
                    raise ValueError(f"Cannot start contour task {task_id} with status '{row['status']}'")
                cur.execute(
                    "UPDATE contour_tasks SET status='running', started_at=? WHERE id=? AND status IN ('pending','paused')",
                    (datetime.now(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"Contour task {task_id} could not be started (status changed)")
                conn.commit()
                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag),
                                      daemon=True, name=f"ContourTask-{task_id}")
                self.active_tasks[task_id] = th
            th.start()
        finally:
            conn.close()

    def pause_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                raise ValueError(f"Cannot pause contour task {task_id} with status '{row['status']}'")
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
            cur.execute("UPDATE contour_tasks SET status='cancelled' WHERE id=? AND status!='cancelled'", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Contour task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM contour_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _update_render_counts(self, task_id: int, rendered: int, total: int) -> None:
        conn = get_connection()
        try:
            conn.execute("UPDATE contour_tasks SET rendered_tiles=?, total_tiles=? WHERE id=?",
                         (rendered, total, task_id))
            conn.commit()
        finally:
            conn.close()

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        try:
            asyncio.run(self._execute(task_id, stop_flag))
        except Exception as e:
            logger.error(f"Contour task {task_id} thread failed: {e}")
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
            task = cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError(f"Contour task {task_id} not found")

            dataset = task["dataset"]
            output_dir = Path(task["output_path"]) / f"contour_task_{task_id}"
            want_water = bool(task["water"])

            dem_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='dem' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()]
            att_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='water' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()] if want_water else []

            stop_ev = asyncio.Event()
            if stop_flag and stop_flag.is_set():
                stop_ev.set()

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                tile_conn = get_connection()
                try:
                    c = tile_conn.cursor()
                    existing = c.execute("SELECT status FROM contour_files WHERE task_id=? AND granule_id=?",
                                         (task_id, granule_id)).fetchone()
                    old_status = existing["status"] if existing else None
                    c.execute(
                        "UPDATE contour_files SET status=?, error_message=?, size_bytes=?, local_path=? WHERE task_id=? AND granule_id=?",
                        (status, error, size_bytes, str(output_dir / granule_id), task_id, granule_id),
                    )
                    d_delta, f_delta = _status_count_deltas(old_status, status)
                    if d_delta or f_delta:
                        c.execute(
                            "UPDATE contour_tasks SET downloaded_files=MAX(downloaded_files+?,0), failed_files=MAX(failed_files+?,0) WHERE id=?",
                            (d_delta, f_delta, task_id),
                        )
                    tile_conn.commit()
                    trow = c.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                    if trow and self.socketio:
                        payload = dict(trow)
                        payload["task_type"] = "contour"
                        payload["phase"] = "download"
                        self.socketio.emit("task_progress", payload)
                finally:
                    tile_conn.close()

            async def stop_watcher():
                while True:
                    if stop_flag and stop_flag.is_set():
                        stop_ev.set()
                        return
                    await asyncio.sleep(0.2)

            watcher = asyncio.create_task(stop_watcher())
            try:
                await self.engine.download_files(
                    dataset=dataset, granules=dem_granules, output_dir=output_dir,
                    progress_callback=progress, stop_flag=stop_ev,
                )
                # Water (ASTWBD) is best-effort: tiles with no water bodies may have
                # no att granule (404), which must not fail the task.
                if att_granules and not stop_ev.is_set():
                    await self.engine.download_files(
                        dataset="ASTWBD.001", granules=att_granules, output_dir=output_dir,
                        progress_callback=progress, stop_flag=stop_ev,
                    )
            finally:
                watcher.cancel()

            if stop_ev.is_set():
                return

            current = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not current or current["status"] in ("cancelled", "paused"):
                return

            counts = cur.execute(
                """
                SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count
                FROM contour_files WHERE task_id=? AND kind='dem'
                """,
                (task_id,),
            ).fetchone()
            failed_count = counts["failed_count"] or 0
            pending_count = counts["pending_count"] or 0
            if failed_count > 0 or pending_count > 0:
                msg = f"{failed_count} DEM file(s) failed, {pending_count} pending"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            # ---- One-stop render phase: DEM downloaded -> contour tiles ----
            from services.contour_task_tiler import ContourParams, tile_contour_task_dir
            from services.contour_engine import ContourStyle, count_tiles

            from dataclasses import replace
            style = ContourStyle.from_config(self.config)
            style = replace(style, background=task["background"] or "#FAF6EC")
            interval = float(task["contour_interval"])
            zoom_min = int(task["zoom_min"]); zoom_max = int(task["zoom_max"])
            # Contours render over the whole downloaded DEM (union of 1° granule
            # tiles), not just the framed bbox, so count tiles over that coverage.
            cov_n, cov_s, cov_e, cov_w = coverage_bbox(task["north"], task["south"], task["east"], task["west"])
            total_tiles = count_tiles(cov_n, cov_s, cov_e, cov_w, zoom_min, zoom_max)

            def render_progress(done: int, total: int):
                self._update_render_counts(task_id, rendered=done, total=total)
                if self.socketio:
                    trow = self.get_task(task_id)
                    payload = dict(trow)
                    payload["task_type"] = "contour"
                    payload["phase"] = "render"
                    self.socketio.emit("task_progress", payload)

            # 立即推一次 render 阶段事件:DEM 下载完进入切片时,前端要马上从"下载 DEM"
            # 切到"渲染瓦片 0/total",不必手动刷新。warp 大区域可能耗时数十秒、期间无
            # 瓦片产出,这一发确保用户看到已进入渲染阶段而非卡在下载 100%。
            logger.info(f"Contour task {task_id}: 进入渲染阶段, 预计 {total_tiles} 瓦片")
            render_progress(0, total_tiles)

            try:
                workers = int(self.config.get("contour_workers", "0") or 0)
            except (TypeError, ValueError):
                workers = 0
            params = ContourParams(interval=interval, zoom_min=zoom_min, zoom_max=zoom_max,
                                   style=style, shade=bool(task["terrain_shade"]), water=want_water,
                                   workers=workers)
            render_counts = tile_contour_task_dir(
                task_dir=output_dir, out_dir=output_dir / "contour_tiles",
                params=params, progress_cb=render_progress, stop_flag=stop_flag,
            )

            if stop_flag and stop_flag.is_set():
                return
            if render_counts.get("rendered", 0) == 0:
                msg = "No contour tiles rendered (check DEM coverage / interval / zoom range)"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            # 诊断:部分瓦片渲染失败(被 _render_contour_tile_core 的 except 吞成 failed)
            # 仍会标 completed,瓦片会缺。记 warning 便于排查"切片不完整"——failed 大说明
            # 是渲染异常,failed=0 但缺层多半是低 zoom 无等高线穿过的设计性 skip。
            failed_tiles = render_counts.get("failed", 0)
            if failed_tiles > 0:
                logger.warning(
                    f"Contour task {task_id}: {failed_tiles} 个瓦片渲染失败 "
                    f"(rendered={render_counts.get('rendered', 0)}, total={render_counts.get('total', 0)}),切片可能不完整"
                )

            cur.execute("UPDATE contour_tasks SET status='completed', completed_at=? WHERE id=? AND status='running'",
                        (datetime.now(), task_id))
            conn.commit()
            if cur.rowcount and self.socketio:
                self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "contour", "status": "completed"})

        except Exception as e:
            try:
                cur = conn.cursor()
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled','paused')",
                            (str(e), datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark contour task {task_id} failed: {e2}")
            raise
        finally:
            conn.close()
