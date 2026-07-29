"""
Shared guard for optional artifact cleanup when deleting tasks.

Mirrors the local-terrain pipeline's convention (see
services/local_terrain_task_manager.py delete_task): removal is best-effort
and only allowed for directories that resolve strictly inside
Config.DOWNLOADS_DIR. Task output_path values are user-supplied, so artifacts
may live elsewhere — those are left alone (the DB row is still deleted).
The shared tile cache (Config.CACHE_DIR) must never be removed.
"""

import logging
import shutil
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)


def remove_task_dir_if_safe(task_dir) -> bool:
    """
    Best-effort removal of a task's on-disk artifact directory.

    Args:
        task_dir: Candidate artifact directory (e.g. output_path/task_<id>).

    Returns:
        True if the directory was eligible for removal (whether or not it
        existed), False if it fell outside the safety boundary.

    Safety boundary:
        - target must resolve strictly inside Config.DOWNLOADS_DIR
          (not equal to it, not one of its ancestors, not a sibling);
        - target must never be the shared tile cache or contain it.
    """
    try:
        target = Path(task_dir).resolve()
        downloads_root = Path(Config.DOWNLOADS_DIR).resolve()
        cache_root = Path(Config.CACHE_DIR).resolve()

        if target == downloads_root or downloads_root not in target.parents:
            logger.warning(
                f"Refusing to delete artifact dir outside DOWNLOADS_DIR: {target}"
            )
            return False
        if target == cache_root or cache_root in target.parents or target in cache_root.parents:
            logger.warning(f"Refusing to delete shared tile cache: {target}")
            return False

        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            logger.info(f"Removed task artifact directory: {target}")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove task artifact dir {task_dir}: {e}")
        return False
