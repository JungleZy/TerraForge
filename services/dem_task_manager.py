"""
DEM Task Manager

Creates and runs DEM download tasks backed by dem_tasks/dem_files tables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.database import get_connection
from services.config_manager import ConfigManager
from services.dem_download_engine import DemDownloadEngine
from services.geo_validation import resolve_output_dir, sanitize_filename, validate_bbox
from services.dem_granules import (
    tiles_for_bbox, astgtm_v3_granules_for_tile, copernicus_glo30_granules_for_tile,
)
from services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

logger = logging.getLogger(__name__)


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status == "completed") - int(old_status == "completed")
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

            now = datetime.now()
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
        # 四至共用校验(范围/顺序/NaN/类型),见 services/geo_validation.py
        north, south, east, west = validate_bbox(
            params.get("north"), params.get("south"),
            params.get("east"), params.get("west"),
        )
        dataset = params.get("dataset") or "COP-DEM-GLO-30"
        # C5: 创建任务时解析 output_path 并强制落在 Config.DOWNLOADS_DIR 内,
        # 越界抛 ValueError(路由层转 400);相对路径相对 DOWNLOADS_DIR 解析,不依赖 CWD。
        output_path = resolve_output_dir(
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
                    (datetime.now(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"DEM task {task_id} could not be started because its status changed")
                conn.commit()

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag), daemon=True, name=f"DemTask-{task_id}")
                self.active_tasks[task_id] = th
            th.start()
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
            cur.execute("SELECT output_path FROM dem_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"DEM task {task_id} not found")
            output_path = row["output_path"]
        finally:
            conn.close()

        parent_url = "http://localhost:5000/terrain/base/layer.json"

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
                    (task_id, str(output_dir), maxzoom, parent_url, datetime.now()),
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
        th.start()

    def _run_tiling_job(self, task_id: int, task_dir: Path, output_dir: Path, maxzoom: int, parent_url: str) -> None:
        try:
            tile_dem_task_dir(
                task_dir=task_dir,
                out_dir=output_dir,
                params=TileParams(maxzoom=maxzoom, parent_url=parent_url),
            )

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_terrain_jobs SET status='completed', completed_at=?, error_message=NULL WHERE task_id=?",
                    (datetime.now(), task_id),
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
                    (datetime.now(), str(e), task_id),
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

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM dem_tasks ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
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

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                # Mirror existing naming: emit task_progress updates.
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

                    c.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,))
                    trow = c.fetchone()
                    if trow and self.socketio:
                        payload = dict(trow)
                        payload["task_type"] = "dem"
                        self.socketio.emit("task_progress", payload)
                finally:
                    tile_conn.close()

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
                    (error_message, datetime.now(), task_id),
                )
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "dem", "status": "failed", "error_message": error_message})
                return

            cur.execute("UPDATE dem_tasks SET status='completed', completed_at=? WHERE id=? AND status='running'", (datetime.now(), task_id))
            conn.commit()
            if cur.rowcount and self.socketio:
                self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "dem", "status": "completed"})

        except Exception as e:
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE dem_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled', 'paused')",
                    (str(e), datetime.now(), task_id),
                )
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "dem", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark DEM task {task_id} as failed: {e2}")
            raise
        finally:
            conn.close()
