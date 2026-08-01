"""
Shared guard for optional artifact cleanup when deleting tasks.

Mirrors the local-terrain pipeline's convention (see
services/local_terrain_task_manager.py delete_task): removal is best-effort
and only allowed for directories that resolve strictly inside
Config.DOWNLOADS_DIR. Task output_path values are user-supplied, so artifacts
may live elsewhere — those are left alone (the DB row is still deleted).
The shared tile cache (Config.CACHE_DIR) must never be removed.
"""

import fnmatch
import logging
import os
import shutil
import tempfile
from pathlib import Path

from core.config import Config

logger = logging.getLogger(__name__)

# 启动清扫的匹配前缀/模式 —— 必须与创建点保持一致,宁可漏不可误删:
#   map_dl_stitch_*  services/download_engine.py stitch 的 tempfile.mkdtemp
#   contour_warp_*   services/contour_engine.py warp 的 tempfile.mkdtemp
#   *.part.*         两处引擎落盘的原子写临时件(download_engine /
#                    dem_download_engine,位于 Config.CACHE_DIR 内)
# finally 盖不住 SIGKILL/关窗,这些残留只能在下次启动时清。
_STITCH_TMP_PREFIX = "map_dl_stitch_"
_CONTOUR_WARP_PREFIX = "contour_warp_"
_PART_GLOB = "*.part.*"
# cache 内 .part 的最深落点:瓦片 cache/{style}/{z}/{x}/{y}.png 的 x 目录
# (根=0 往下 4 层);dem cache 是 cache/dem/<granule>,更浅,一并覆盖。
# 限深是为了不随 cache 增长无界遍历 —— 瓦片文件本身在叶子层,扫目录名
# 不需要再往下走。
_CACHE_PART_MAX_DEPTH = 4


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


def _sweep_tmp_dirs(root: Path, prefix: str) -> int:
    """删除 root 直下所有 `prefix*` 目录（不递归匹配、不碰文件），返回删除数。"""
    removed = 0
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _sweep_cache_part_files(cache_root: Path) -> int:
    """删除 cache 内限深（_CACHE_PART_MAX_DEPTH）的 *.part.* 文件，返回删除数。

    手动 scandir 限深遍历而不是 rglob：cache 可能已有几十万瓦片，
    无界遍历会把启动拖慢；.part 只会出现在已知的几层目录里（见常量注释）。
    只删文件，目录一律不碰。
    """
    removed = 0
    stack: list = [(cache_root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < _CACHE_PART_MAX_DEPTH:
                        stack.append((entry.path, depth + 1))
                elif fnmatch.fnmatch(entry.name, _PART_GLOB):
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
    return removed


def sweep_startup_residue() -> None:
    """启动一次性清扫三类 finally 盖不住（SIGKILL/关窗）的临时残留：

    1. 系统临时目录里的 stitch work_dir（map_dl_stitch_*）；
    2. contour warp tmpdir（contour_warp_*，系统临时目录 + 配置的
       contour_warp_tmpdir 两处）；
    3. 共享瓦片/DEM cache 里的原子写临时件（*.part.*）。

    全程 best-effort：单个删除失败跳过，整体异常只记日志，绝不影响启动。
    匹配规则按前缀/通配精确限定（见模块顶部常量），同步执行、毫秒级。
    """
    removed = {"stitch": 0, "warp": 0, "part": 0}
    try:
        sys_tmp = Path(tempfile.gettempdir())
        removed["stitch"] += _sweep_tmp_dirs(sys_tmp, _STITCH_TMP_PREFIX)
        removed["warp"] += _sweep_tmp_dirs(sys_tmp, _CONTOUR_WARP_PREFIX)

        # contour_warp_tmpdir 配置键可把 warp 产物指到别的盘（大区域数十 GB）;
        # 配置库不可用(fresh clone、cwd 不同等)时跳过该处,系统临时目录已扫。
        try:
            from services.config_manager import ConfigManager
            warp_base = (ConfigManager().get("contour_warp_tmpdir", "") or "").strip()
        except Exception:
            warp_base = ""
        if warp_base:
            warp_root = Path(warp_base)
            if warp_root.resolve() != sys_tmp.resolve():
                removed["warp"] += _sweep_tmp_dirs(warp_root, _CONTOUR_WARP_PREFIX)

        removed["part"] += _sweep_cache_part_files(Path(Config.CACHE_DIR))
    except Exception as e:
        logger.warning(f"Startup residue sweep failed (ignored): {e}")
        return
    total = sum(removed.values())
    if total:
        logger.info(
            f"Startup residue sweep removed {total} leftover(s): "
            f"stitch tmp={removed['stitch']}, contour warp tmp={removed['warp']}, "
            f"cache .part={removed['part']}"
        )
