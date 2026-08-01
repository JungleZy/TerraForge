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
import time
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

# enforce_cache_size_limit 跳过比这个新的文件:任务下载完瓦片到拼接读完
# 之间有一个窗口,刚落盘的瓦片若被 LRU 清掉,拼接会报 "Tile not found";
# 一小时的宽限对「最久未用先清」的语义没有实质影响。
_EVICTION_MIN_AGE_SECONDS = 3600


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


def _iter_tile_cache_files(cache_root: Path):
    """产出瓦片 cache 里的全部文件（scandir 迭代，不递归进 dem 目录）。

    dem granule 有独立生命周期（dem_cache_enabled），重下要过 Earthdata
    登录，不归瓦片 cache 的 LRU 清理管；其余子目录（各 style 的
    {z}/{x}/{y}.png）都是瓦片 cache。
    """
    try:
        with os.scandir(cache_root) as it:
            top = list(it)
    except OSError:
        return
    stack = [e.path for e in top
             if e.name != 'dem' and e.is_dir(follow_symlinks=False)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield entry
            except OSError:
                continue


def enforce_cache_size_limit(cache_root=None) -> dict:
    """按 cache_max_size_mb 配置对瓦片 cache 做 LRU 清理。

    配置项过去没有任何消费方,cache 可以无限增长(实测 6.1GB vs 配置
    1000MB)。这里把它补上:全量扫一遍瓦片 cache,总大小超过上限时按
    「最久未用先清」(max(atime, mtime) 升序)逐个删,直到回到上限内。

    护栏(宁可漏清不可误删):
      - 0/负值/读配置失败 = 不限制,直接返回(0 绝不能理解成清空 cache);
      - dem 目录不扫(见 _iter_tile_cache_files);
      - *.part.* 原子写临时件跳过 —— 那可能是别的任务正在写的瓦片;
      - 比 _EVICTION_MIN_AGE_SECONDS 新的文件跳过 —— 保护下载完还没
        拼接读完的在途任务;
      - 全程 best-effort:单文件失败跳过,整体异常只记日志。

    调用点:任务下载阶段结束后(task_manager,to_thread 里)。下载是
    cache 唯一增长点,顺势清理即可,不挂启动路径 —— 几十万瓦片的全量
    stat 是秒级活,不能拖慢启动。

    Returns:
        统计 dict:scanned_files/total_bytes(含不可清理项的账面总量)、
        removed_files/removed_bytes。
    """
    root = Path(cache_root) if cache_root is not None else Path(Config.CACHE_DIR)
    stats = {'scanned_files': 0, 'total_bytes': 0,
             'removed_files': 0, 'removed_bytes': 0}
    try:
        from services.config_manager import ConfigManager
        limit_mb = int(ConfigManager().get('cache_max_size_mb', '1000') or '0')
    except Exception as e:
        logger.warning(f"读取 cache_max_size_mb 失败({e!r}),跳过 cache 清理")
        return stats
    if limit_mb <= 0:
        return stats
    limit_bytes = limit_mb * 1024 * 1024

    now = time.time()
    candidates = []  # (atime_key, size, path) —— 仅可清理项
    for entry in _iter_tile_cache_files(root):
        if fnmatch.fnmatch(entry.name, _PART_GLOB):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        stats['scanned_files'] += 1
        stats['total_bytes'] += st.st_size
        key = max(st.st_atime, st.st_mtime)
        if now - key < _EVICTION_MIN_AGE_SECONDS:
            continue
        candidates.append((key, st.st_size, entry.path))

    if stats['total_bytes'] <= limit_bytes:
        return stats

    candidates.sort()  # 最久未用在前
    remaining = stats['total_bytes']
    for _key, size, path in candidates:
        if remaining <= limit_bytes:
            break
        try:
            os.unlink(path)
            remaining -= size
            stats['removed_files'] += 1
            stats['removed_bytes'] += size
        except OSError as e:
            logger.warning(f"cache LRU 清理删除失败 {path}: {e}")

    if stats['removed_files']:
        logger.info(
            f"Tile cache LRU cleanup: removed {stats['removed_files']} file(s), "
            f"{stats['removed_bytes'] / 1024 / 1024:.1f}MB "
            f"(limit {limit_mb}MB, now ~{remaining / 1024 / 1024:.1f}MB)"
        )
    return stats
