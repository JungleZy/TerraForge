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

from core.config import Config

logger = logging.getLogger(__name__)


def resolve_stored_output_dir(stored_path) -> Path:
    """把任务行里存的 output_path 归一化成绝对 Path(兼容存量相对路径)。

    现在的 create_task 入库的是 resolve_output_dir() 解析后的绝对路径;
    更早的行可能是相对路径 —— 旧代码按进程 CWD 解析,exe 换目录启动后
    写盘/删除都会跑偏。相对路径一律相对 Config.DOWNLOADS_DIR 解析
    (与创建时的校验口径一致);绝对路径原样返回。这里只做归一化不做
    越界拒绝:越界防护由调用方(remove_task_dir_if_safe / stitch 的
    白名单检查)各自负责,读路径不能因为历史脏数据把任务卡死。
    """
    p = Path(str(stored_path)).expanduser()
    if not p.is_absolute():
        p = Path(Config.DOWNLOADS_DIR) / p
    return p


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
        if target == cache_root or cache_root in target.parents:
            logger.warning(f"Refusing to delete shared tile cache: {target}")
            return False

        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            logger.info(f"Removed task artifact directory: {target}")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove task artifact dir {task_dir}: {e}")
        return False
