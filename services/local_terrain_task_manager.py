"""
Local Terrain Task Manager

Creates terrain tiling tasks from user-uploaded GeoTIFF files, backed by
local_terrain_tasks/local_terrain_files tables. Reuses the existing terrain
tiler (tile_dem_task_dir) by saving uploads as *_dem.tif.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import Config
from database import get_connection
from services.config_manager import ConfigManager
from services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

logger = logging.getLogger(__name__)

_ALLOWED_EXT = (".tif", ".tiff")
UploadFile = Tuple[str, bytes]  # (original_filename, content_bytes)


class LocalTerrainTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        """Demote leftover 'running' rows to 'failed'.

        Tiling is a one-shot build_terrain call with no resume model, so a
        leftover 'running' row from a dead process can only be restarted.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM local_terrain_tasks WHERE status = 'running'")
            ids = [row["id"] for row in cur.fetchall()]
            if ids:
                now = datetime.now()
                cur.executemany(
                    "UPDATE local_terrain_tasks SET status='failed', completed_at=?, "
                    "error_message='Process was interrupted before completion; re-upload to retile' "
                    "WHERE id=? AND status='running'",
                    [(now, i) for i in ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan local terrain tasks (failed): {ids}")
        except Exception as e:
            logger.error(f"Failed to recover local terrain orphans: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _default_maxzoom(self) -> int:
        raw = self.config.get("terrain_local_maxzoom", "14")
        try:
            return int(raw) if raw is not None else 14
        except Exception:
            return 14

    def create_task_with_files(
        self,
        name: str,
        files: Sequence[UploadFile],
        maxzoom: Optional[int] = None,
    ) -> int:
        """Create a task, persist uploaded tifs, then auto-start tiling.

        files: sequence of (original_filename, content_bytes) already read into
        memory by the caller (the route reads werkzeug FileStorage).
        """
        name = (name or "Local Terrain Task").strip() or "Local Terrain Task"

        valid: List[UploadFile] = []
        for original, content in files:
            ext = Path(original or "").suffix.lower()
            if ext not in _ALLOWED_EXT:
                raise ValueError(f"Unsupported file type: {original} (only .tif/.tiff)")
            if not content:
                raise ValueError(f"Empty file: {original}")
            valid.append((original, content))

        if not valid:
            raise ValueError("No valid .tif/.tiff files uploaded")

        if maxzoom is None:
            maxzoom = self._default_maxzoom()
        maxzoom = int(maxzoom)

        base = Path(Config.DOWNLOADS_DIR) / "terrain"
        parent_url = "http://localhost:5000/terrain/base/layer.json"

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO local_terrain_tasks
                  (name, status, output_path, source_dir, output_dir,
                   total_files, uploaded_files, failed_files, maxzoom, parent_url)
                VALUES (?, 'pending', '', '', '', ?, 0, 0, ?, ?)
                """,
                (name, len(valid), maxzoom, parent_url),
            )
            task_id = cur.lastrowid

            task_root = base / f"local_task_{task_id}"
            source_dir = task_root / "source"
            output_dir = task_root / "terrain_tiles"
            source_dir.mkdir(parents=True, exist_ok=True)

            cur.execute(
                "UPDATE local_terrain_tasks SET output_path=?, source_dir=?, output_dir=? WHERE id=?",
                (str(task_root), str(source_dir), str(output_dir), task_id),
            )

            uploaded = 0
            failed = 0
            for idx, (original, content) in enumerate(valid, start=1):
                stored = f"upload_{idx}_dem.tif"
                dest = source_dir / stored
                try:
                    dest.write_bytes(content)
                    cur.execute(
                        """
                        INSERT INTO local_terrain_files
                          (task_id, original_filename, stored_filename, local_path, size_bytes, status)
                        VALUES (?, ?, ?, ?, ?, 'uploaded')
                        """,
                        (task_id, original, stored, str(dest), len(content)),
                    )
                    uploaded += 1
                except Exception as e:
                    failed += 1
                    cur.execute(
                        """
                        INSERT INTO local_terrain_files
                          (task_id, original_filename, stored_filename, local_path, size_bytes, status, error_message)
                        VALUES (?, ?, ?, ?, ?, 'failed', ?)
                        """,
                        (task_id, original, stored, str(dest), len(content), str(e)),
                    )

            cur.execute(
                "UPDATE local_terrain_tasks SET uploaded_files=?, failed_files=? WHERE id=?",
                (uploaded, failed, task_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if uploaded == 0:
            self._mark_failed(task_id, "All uploaded files failed to save")
            raise ValueError("All uploaded files failed to save")

        self.start_tiling(task_id)
        return task_id

    def _mark_failed(self, task_id: int, message: str) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, completed_at=? WHERE id=?",
                (message, datetime.now(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM local_terrain_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Local terrain task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_files(self, task_id: int) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM local_terrain_files WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM local_terrain_tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def start_tiling(self, task_id: int) -> None:
        # Implemented in Task 3.
        raise NotImplementedError
