"""
Shared guard for optional artifact cleanup when deleting tasks.

Removal is best-effort. 0.2.4 起保存路径全盘可选，边界随之重定为五条
（见 remove_task_dir_if_safe 的 docstring）：路径任一层是符号链接 / 不足两级
目录深度 / 用户家目录 / Config.DOWNLOADS_DIR 本身或其祖先 / 与
Config.CACHE_DIR 相关（是它、在它内部、或包含它）—— 一律拒绝，其余位置
（包括 DOWNLOADS_DIR 之外的用户自定义目录）允许删除。

注意 local-terrain 管线是**另一套**口径：它的 delete_task 不读库存
output_path，而是按当前 Config.DOWNLOADS_DIR 重算路径，并自带
「只删 DOWNLOADS_DIR/terrain 之内」的内联守卫（见
src/services/local_terrain_task_manager.py delete_task）—— 那是为了让冻结 exe
搬迁后旧的绝对路径不误删旧位置的目录。两者现在是并存的两套规则。

The shared tile cache (Config.CACHE_DIR) must never be removed by task
deletion.

下载缓存本身不做任何自动清理:get_cache_stats 分类统计 cache 占用,
clear_cache_category 由用户在前端手动触发(带二次确认),启动清扫
sweep_startup_residue 只清 *.part.* 原子写残留和 stitch/warp 临时目录,
不碰缓存内容。
"""

import fnmatch
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from src.core.config import Config

logger = logging.getLogger(__name__)

# 启动清扫的匹配前缀/模式 —— 必须与创建点保持一致,宁可漏不可误删:
#   map_dl_stitch_*  src/services/download_engine.py stitch 的 tempfile.mkdtemp
#   contour_warp_*   src/services/contour_engine.py warp 的 tempfile.mkdtemp
#   local_upload_*   src/services/local_terrain_task_manager.py 的上传暂存
#   contour_upload_* src/services/contour_task_manager.py 的上传暂存
#   *.part.*         两处引擎落盘的原子写临时件(download_engine /
#                    dem_download_engine,位于 Config.CACHE_DIR 内)
# finally 盖不住 SIGKILL/关窗,这些残留只能在下次启动时清。
# ⚠️ 新增任何 mkdtemp 型临时目录时,必须把前缀同步登记到这里,否则它就是清扫
#    盲区(L5 就是这么来的:5 个创建点只有 3 个被扫到)。
_STITCH_TMP_PREFIX = "map_dl_stitch_"
_CONTOUR_WARP_PREFIX = "contour_warp_"
# 上传暂存目录（L5）：两处都是 try/except: rmtree; raise + 函数末尾一条 rmtree，
#   **没有 finally** —— Ctrl-C / SystemExit 会整个绕过，残留几十 GB 上传件。
#   它们不在系统临时目录，而在 DOWNLOADS_DIR 下（与任务目录同盘，便于 replace）。
_LOCAL_UPLOAD_PREFIX = "local_upload_"      # src/services/local_terrain_task_manager.py
_CONTOUR_UPLOAD_PREFIX = "contour_upload_"  # src/services/contour_task_manager.py
_PART_GLOB = "*.part.*"
# cache 内 .part 的最深落点:瓦片 cache/{style}/{z}/{x}/{y}.png 的 x 目录
# (根=0 往下 4 层);dem cache 是 cache/dem/<granule>,更浅,一并覆盖。
# 限深是为了不随 cache 增长无界遍历 —— 瓦片文件本身在叶子层,扫目录名
# 不需要再往下走。
_CACHE_PART_MAX_DEPTH = 4

# 本进程的启动时刻（近似为本模块导入时刻）。启动清扫只处理【早于】它的临时
# 目录 —— 更新的目录只可能属于另一个活着的进程（H3）。
_PROCESS_START_TIME = time.time()

# 手动清理的分类名白名单:cache 顶层目录名(各 style 代码 / dem),
# 拒绝路径分隔符与 ..,配合 resolve 校验防越界。
_CATEGORY_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def resolve_stored_output_dir(stored_path) -> Path:
    """把任务行里存的 output_path 归一化成绝对 Path(兼容存量相对路径)。

    现在的 create_task 入库的是 resolve_output_dir() 解析后的绝对路径;
    更早的行可能是相对路径 —— 旧代码按进程 CWD 解析,exe 换目录启动后
    写盘/删除都会跑偏。这里只做归一化不做越界拒绝:越界防护由调用方
    (remove_task_dir_if_safe / stitch 的白名单检查)各自负责,读路径不能
    因为历史脏数据把任务卡死。

    **相对值的口径（M10 收敛后的唯一一套）**：`./downloads/...` 与
    `downloads/...` 做前缀剥离后落到 `Config.DOWNLOADS_DIR`，其余相对值落到
    `Config.BASE_DIR`。这是 `src/core/database.py` 归一 `default_save_path` 时
    已经认定的历史语义 —— `'./downloads'` 指的就是 DOWNLOADS_DIR 本身，而不是
    它下面的 `downloads/` 子目录。

    在此之前，同一个存量字段有四套解析规则并存：写侧/地图删除侧把相对值一律
    拼到 DOWNLOADS_DIR 下（`'./downloads/map'` → `<BASE>/downloads/downloads/map`），
    读侧做前缀剥离（→ `<BASE>/downloads/map`），DEM/等高线删除侧按进程 CWD 解析，
    local terrain 删除侧干脆不读这个字段。后果：点「删除并删文件」删的是一个
    不存在的目录却回 200 success；恢复任务续下的瓦片写到一处、`/tiles/<id>/`
    去另一处找，产物分裂。

    `Config.DOWNLOADS_DIR` 恒等于 `Config.BASE_DIR / 'downloads'`，所以这套口径
    与 `src/core/database.py` 里 `_root.parent / _p` 的写法在所有相对形态上等价。
    """
    raw = str(stored_path or "").strip()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p

    s = raw.replace("\\", "/")
    for prefix in ("./downloads/", "downloads/"):
        if s.startswith(prefix):
            return Path(Config.DOWNLOADS_DIR) / s[len(prefix):]
    if s in ("./downloads", "downloads"):
        return Path(Config.DOWNLOADS_DIR)
    return Path(Config.BASE_DIR) / p


def _has_symlink_component(path: Path) -> bool:
    """路径任一层是符号链接则为真 —— 逐层 lstat,不靠 resolve 对比
    (macOS /tmp、/var 本身是符号链接,resolve 对比会误伤合法路径)。"""
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True  # 查不了按有风险处理,宁可拒绝
    return False


def remove_task_dir_if_safe(task_dir) -> bool:
    """
    Best-effort removal of a task's on-disk artifact directory.

    Args:
        task_dir: Candidate artifact directory (e.g. output_path/task_<id>).

    Returns:
        True if the directory was eligible for removal (whether or not it
        existed), False if it fell outside the safety boundary.

    Safety boundary（0.2.4 全盘保存路径后重定,不再要求在 DOWNLOADS_DIR 内）:
        - 路径任一层是符号链接 → 拒绝(rmtree 会跟着链接删到别处);
        - 不足两级目录深度(根目录/盘符根/单级目录) → 拒绝;
        - 用户家目录本身 → 拒绝;
        - DOWNLOADS_DIR 本身或其祖先 → 拒绝;
        - 共享瓦片 cache 本身、cache 内部、或包含 cache 的目录 → 拒绝。
    """
    try:
        raw = Path(task_dir).expanduser().absolute()
        target = raw.resolve()
        downloads_root = Path(Config.DOWNLOADS_DIR).resolve()
        cache_root = Path(Config.CACHE_DIR).resolve()

        # symlink 检查必须用未 resolve 的路径 —— resolve 会先把链接塌掉,
        # 塌完再查等于没查(rmtree 会跟着链接删到别处)
        if _has_symlink_component(raw):
            logger.warning(f"Refusing to delete path with symlink component: {raw}")
            return False
        if len(target.parts) < 3:
            logger.warning(f"Refusing to delete shallow path: {target}")
            return False
        if target == Path.home().resolve():
            logger.warning(f"Refusing to delete user home directory: {target}")
            return False
        if target == downloads_root or target in downloads_root.parents:
            logger.warning(f"Refusing to delete downloads root or its ancestor: {target}")
            return False
        if (target == cache_root or cache_root in target.parents
                or target in cache_root.parents):
            logger.warning(f"Refusing to delete shared tile cache or its container: {target}")
            return False

        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            logger.info(f"Removed task artifact directory: {target}")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove task artifact dir {task_dir}: {e}")
        return False


def _sweep_tmp_dirs(root: Path, prefix: str, older_than: Optional[float] = None) -> int:
    """删除 root 直下所有 `prefix*` 目录（不递归匹配、不碰文件），返回删除数。

    older_than（H3 第二层防护）：只删 mtime 早于该时刻的目录。mkdtemp 目录名里
    不带任何归属信息（pid 只在 .part 【文件名】里），纯前缀匹配分不清「上次进程
    的残留」和「另一个活着的进程正在写的工作目录」；调用方传入本进程启动时刻，
    即可放过启动之后才出现的目录。主防护是 src/core/single_instance.py 的实例锁，
    这一层用于兜住 TERRAFORGE_ALLOW_MULTI_INSTANCE 等逃生场景。
    """
    removed = 0
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return 0
    for entry in entries:
        try:
            if not (entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False)):
                continue
            if older_than is not None:
                try:
                    if entry.stat(follow_symlinks=False).st_mtime >= older_than:
                        logger.debug(
                            f"Skipping temp dir newer than this process start: {entry.path}")
                        continue
                except OSError:
                    continue
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _part_owner_pid(name: str) -> Optional[int]:
    """从 `<name>.part.<pid>.<id>` 里解析出写它的进程 pid，取不到返回 None。

    两个引擎的原子写临时件名里本来就带 `os.getpid()`（download_engine 与
    dem_download_engine），这是三类清扫对象里唯一带归属信息的一类 —— 可以用它
    精确跳过另一个活进程正在写的文件，而不必退到 mtime 这种近似判据（H3）。
    """
    marker = ".part."
    idx = name.rfind(marker)
    if idx < 0:
        return None
    head = name[idx + len(marker):].split(".")
    if not head or not head[0].isdigit():
        return None
    try:
        return int(head[0])
    except ValueError:
        return None


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
                    owner = _part_owner_pid(entry.name)
                    if owner is not None and owner != os.getpid():
                        try:
                            from src.core.process_watchdog import pid_alive
                            if pid_alive(owner):
                                # 另一个活着的进程正在写它 —— 删掉会让那次
                                # 原子写的 replace 抛异常。pid 复用只会导致漏
                                # 删（下次启动再清），方向是安全的。
                                continue
                        except Exception:
                            pass
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
    return removed


def sweep_startup_residue() -> None:
    """启动一次性清扫三类 finally 盖不住（SIGKILL/关窗）的临时残留：

    1. stitch work_dir（map_dl_stitch_*，系统临时目录 + 配置的 stitch_tmpdir）；
    2. contour warp tmpdir（contour_warp_*，系统临时目录 + 配置的
       contour_warp_tmpdir 两处）；
    3. 两处上传暂存目录（local_upload_* / contour_upload_*，位于
       DOWNLOADS_DIR 下 —— 它们的清理只有 except 分支和函数末尾各一条 rmtree、
       **没有 finally**，Ctrl-C / SystemExit 会整个绕过）；
    4. 共享瓦片/DEM cache 里的原子写临时件（*.part.*）。

    只处理 mtime 早于本进程启动时刻的目录（见 _sweep_tmp_dirs 的 older_than）；
    .part 文件按名字里的 pid 跳过仍存活的写者。

    全程 best-effort：单个删除失败跳过，整体异常只记日志，绝不影响启动。
    匹配规则按前缀/通配精确限定（见模块顶部常量），同步执行、毫秒级。
    """
    removed = {"stitch": 0, "warp": 0, "upload": 0, "part": 0}
    started_at = _PROCESS_START_TIME
    try:
        sys_tmp = Path(tempfile.gettempdir())
        removed["stitch"] += _sweep_tmp_dirs(sys_tmp, _STITCH_TMP_PREFIX, started_at)
        removed["warp"] += _sweep_tmp_dirs(sys_tmp, _CONTOUR_WARP_PREFIX, started_at)

        # contour_warp_tmpdir 配置键可把 warp 产物指到别的盘（大区域数十 GB）;
        # 配置库不可用(fresh clone、cwd 不同等)时跳过该处,系统临时目录已扫。
        try:
            from src.services.config_manager import ConfigManager
            warp_base = (ConfigManager().get("contour_warp_tmpdir", "") or "").strip()
        except Exception:
            warp_base = ""
        if warp_base:
            warp_root = Path(warp_base)
            if warp_root.resolve() != sys_tmp.resolve():
                removed["warp"] += _sweep_tmp_dirs(
                    warp_root, _CONTOUR_WARP_PREFIX, started_at)

        # L5: stitch_tmpdir 与 contour_warp_tmpdir 此前处理不对称 —— 这个键存在
        # 的意义恰恰是把 GB 级中间产物挪到空间充足的盘,配了它反而进清扫盲区,
        # 而配置页的「缓存管理」只覆盖 Config.CACHE_DIR,没有任何回收入口。
        try:
            from src.services.config_manager import ConfigManager
            stitch_base = (ConfigManager().get("stitch_tmpdir", "") or "").strip()
        except Exception:
            stitch_base = ""
        if stitch_base:
            stitch_root = Path(stitch_base)
            try:
                _differs = stitch_root.resolve() != sys_tmp.resolve()
            except Exception:
                _differs = True
            if _differs:
                removed["stitch"] += _sweep_tmp_dirs(
                    stitch_root, _STITCH_TMP_PREFIX, started_at)

        # L5: 两处上传暂存目录（DOWNLOADS_DIR 下，非系统临时目录）。
        removed["upload"] += _sweep_tmp_dirs(
            Path(Config.DOWNLOADS_DIR) / "terrain", _LOCAL_UPLOAD_PREFIX, started_at)
        removed["upload"] += _sweep_tmp_dirs(
            Path(Config.DOWNLOADS_DIR) / "dem", _CONTOUR_UPLOAD_PREFIX, started_at)

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


def _sum_dir_bytes(root: Path) -> tuple:
    """递归统计目录大小与文件数(scandir 迭代,best-effort,OSError 跳过)。"""
    total_bytes = 0
    file_count = 0
    stack = [root]
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
                    st = entry.stat(follow_symlinks=False)
                    total_bytes += st.st_size
                    file_count += 1
            except OSError:
                continue
    return total_bytes, file_count


def get_cache_stats(cache_root=None) -> dict:
    """分类统计下载缓存占用:cache 顶层每个子目录一个分类。

    分类规则:dem → DEM 缓存(重下需 Earthdata 登录);其余子目录是各
    style 的瓦片缓存;顶层散落文件(正常不会有)归入 _root/其他。
    只统计不删除 —— 缓存不做任何自动清理,清理由用户在前端手动触发
    (clear_cache_category)。

    Returns:
        {'categories': [{'key', 'label', 'size_bytes', 'file_count'}...],
         'total_bytes': N}
    """
    root = Path(cache_root) if cache_root is not None else Path(Config.CACHE_DIR)
    categories = []
    total_bytes = 0
    root_bytes = 0
    root_files = 0
    try:
        with os.scandir(root) as it:
            top = list(it)
    except OSError:
        top = []
    for entry in top:
        try:
            if entry.is_dir(follow_symlinks=False):
                size, count = _sum_dir_bytes(Path(entry.path))
                label = 'DEM 缓存' if entry.name == 'dem' else f'瓦片缓存（{entry.name}）'
                categories.append({
                    'key': entry.name, 'label': label,
                    'size_bytes': size, 'file_count': count,
                })
                total_bytes += size
            elif entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                root_bytes += st.st_size
                root_files += 1
        except OSError:
            continue
    if root_files:
        categories.append({'key': '_root', 'label': '其他',
                           'size_bytes': root_bytes, 'file_count': root_files})
        total_bytes += root_bytes
    categories.sort(key=lambda c: c['size_bytes'], reverse=True)
    return {'categories': categories, 'total_bytes': total_bytes}


def clear_cache_category(category: str, cache_root=None) -> dict:
    """手动清理一个缓存分类(删除 cache 顶层对应子目录的全部内容)。

    安全护栏(与 remove_task_dir_if_safe 同一思路,宁可拒绝不可误删):
    category 必须是简单目录名(_CATEGORY_NAME_RE,拒绝 .. / 分隔符 /
    绝对路径),且 resolve 后严格位于 CACHE_DIR 内、不等于 CACHE_DIR。
    目录不存在时抛 ValueError(前端分类清单来自 get_cache_stats,
    不存在即视为非法输入)。

    Returns:
        {'removed_bytes': N, 'removed_files': M}

    Raises:
        ValueError: category 非法、越界或不存在。
    """
    root = Path(cache_root) if cache_root is not None else Path(Config.CACHE_DIR)
    if not _CATEGORY_NAME_RE.match(category or ''):
        raise ValueError(f"非法的缓存分类名: {category!r}")

    # _root 是 get_cache_stats 里「顶层散落文件」的分类,只删文件不碰子目录。
    if category == '_root':
        removed_bytes = 0
        removed_files = 0
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            os.unlink(entry.path)
                            removed_bytes += size
                            removed_files += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return {'removed_bytes': removed_bytes, 'removed_files': removed_files}

    target = (root / category).resolve()
    root_resolved = root.resolve()
    if target == root_resolved or root_resolved not in target.parents:
        raise ValueError(f"缓存分类越界: {category!r}")
    if not target.is_dir():
        raise ValueError(f"缓存分类不存在: {category!r}")
    removed_bytes, removed_files = _sum_dir_bytes(target)
    shutil.rmtree(target, ignore_errors=True)
    logger.info(
        f"Cache category cleared: {category} "
        f"({removed_files} file(s), {removed_bytes / 1024 / 1024:.1f}MB)"
    )
    return {'removed_bytes': removed_bytes, 'removed_files': removed_files}
