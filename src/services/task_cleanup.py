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
from typing import NamedTuple, Optional

from src.core.config import Config
# 缓存分类名要按界面语种给（没有请求上下文时 get_locale 回落 zh）。
# src.i18n 只依赖自己的 catalog（一堆纯 dict 模块），顶层引进来不成环。
from src.i18n import t
# 缓存分类的人类可读命名要用它们。两个模块都只依赖 src.core.config /
# src.contracts（不碰 numpy / osgeo / DB），顶层导入不成环、不拖慢启动。
# cache_exclusive 与 task_logging 就**不能**这样引：前者在 cache_usage_by_namespace
# 里反过来 import 本模块的 _sum_dir_bytes，顶层引进来就是一个环。
from src.contracts.source import SourceSnapshot
from src.services.source_registry import STYLE_NAMES
# base_terrain 只依赖 src.core.bundle / src.core.config（刻意不碰 numpy / osgeo），
# 顶层导入不会成环，也不会把 GDAL 拖进启动路径。
from src.services.terrain_tiling import base_terrain

logger = logging.getLogger(__name__)

# 启动清扫的匹配前缀/模式 —— 必须与创建点保持一致,宁可漏不可误删:
#   map_dl_stitch_*  src/services/download_engine.py stitch 的 tempfile.mkdtemp
#   contour_warp_*   src/services/contour_engine.py warp 的 tempfile.mkdtemp
#   local_upload_*   src/services/local_terrain_task_manager.py 的上传暂存
#   contour_upload_* src/services/contour_task_manager.py 的上传暂存
#   *.part.*         原子写临时件。共四个生产者,但只有落在 Config.CACHE_DIR
#                    内的那些进本清扫(_sweep_cache_part_files):
#                      1. download_engine 瓦片缓存落盘 `.part.<pid>.<id>`
#                         (CACHE_DIR 内,本清扫覆盖)
#                      2. dem_download_engine 粒度落盘 `.part.<pid>.<id>`
#                         (CACHE_DIR 内;dem_cache_enabled=false 时退回任务目录
#                          —— 那是用户自选的全盘路径,只能靠引擎自己的 finally)
#                      3. download_engine 的拼接产物 `.part.<pid>`(任务输出目录)
#                         与配准中间件 `.part.<pid>.<id>`(stitch work_dir,随
#                         map_dl_stitch_* 整个目录被扫掉)
#                      4. task_manager._stream_copy_tile 的
#                         `.part.<pid>.<thread_ident>`(任务输出目录;线程 id 段
#                         只为让下载回调线程与补拷线程的临时件不互踩)
#                    ⚠️ 名字里必须放 os.getpid():归属判定 _part_owner_pid 拿这个
#                    数去和活进程比对,把线程 id 塞进 pid 槽位会让「另一个活进程
#                    正在写」和「上次进程留下的垃圾」判反。四个生产者现在都带
#                    pid,新增第五个必须照办。
#                    **任务输出目录不纳入 .part 清扫根**(评估后的决定,不是遗漏):
#                    要在那里找 .part 就得遍历每个历史任务的 {style}/{z}/{x} 三层
#                    目录(百万瓦片任务就是几万个目录 × 任务数),而能收回的只是
#                    SIGKILL 瞬间在途的那几个 KB 碎片 —— 启动开销与收益差几个数
#                    量级。该处的清理责任留在生产者自己的 except 里
#                    (task_manager._stream_copy_tile 会 unlink 后重抛)。
#                    (同一个目录确实是【物化栅格】那一类的扫描根,但那是直下一层
#                    scandir + GB 级收益,与这里的取舍不是一回事。)
#   cesium_terrain_<pid>_*  多幅 DEM 物化成单幅的中间栅格
#                    (src/services/terrain_tiling/cesium_terrain.py 的
#                    build_input_raster),与源数据同量级(GB 级任务就是 GB)。
#                    落在切片输出目录的父级 —— 对 DEM 任务那是用户自选的
#                    **全盘路径**,不在 DOWNLOADS_DIR 内,所以扫描根要从 DB 取。
#   .base_unpack_<pid>_*  随包全球底图的解压临时目录
#                    (src/services/terrain_tiling/base_terrain.py 的
#                    ensure_base_unpacked),最多 167 MB / 4.3 万个文件。落在
#                    assets/terrain —— 既不在系统临时目录也不在 DOWNLOADS_DIR
#                    下,前五类一条都扫不到;而那是 Nuitka 的 --include-data-dir
#                    源目录,残留会被打进三个平台的发布产物。
# finally 盖不住 SIGKILL/关窗,这些残留只能在下次启动时清。
# ⚠️ 新增任何 mkdtemp 型临时目录时,必须把前缀同步登记到这里,否则它就是清扫
#    盲区(L5 就是这么来的:5 个创建点只有 3 个被扫到)。
_STITCH_TMP_PREFIX = "map_dl_stitch_"
_CONTOUR_WARP_PREFIX = "contour_warp_"
# 底图解压临时目录的前缀与落点：从创建点导入，不在这里重抄一份字面量 —— 抄一份
# 就有走样的机会，而走样的后果正是上面那条警告说的清扫盲区。
_BASE_UNPACK_PREFIX = base_terrain.UNPACK_TMP_PREFIX
# 上传暂存目录（L5）：两处都是 try/except: rmtree; raise + 函数末尾一条 rmtree，
#   **没有 finally** —— Ctrl-C / SystemExit 会整个绕过，残留几十 GB 上传件。
#   它们不在系统临时目录，而在 DOWNLOADS_DIR 下（与任务目录同盘，便于 replace）。
_LOCAL_UPLOAD_PREFIX = "local_upload_"      # src/services/local_terrain_task_manager.py
_CONTOUR_UPLOAD_PREFIX = "contour_upload_"  # src/services/contour_task_manager.py
_PART_GLOB = "*.part.*"
# pid 的量级上限,用来兜住「线程 id 被塞进了名字的 pid 槽位」这类命名走样
# (登记表第 4 条)。CPython 在 Linux/macOS 上 threading.get_ident() 返回的是
# pthread_t 指针,量级 1e14,与 pid 差七八个数量级(Linux pid_max 默认 2^22);
# Windows 的 get_ident() 与 pid 同量级,名字上根本分不出来 —— 所以真正的防线
# 是生产者必须写 os.getpid(),这里只是最后一道。
_MAX_PLAUSIBLE_PID = 2 ** 31
# 物化中间栅格（cesium_terrain.py:build_input_raster）。它是**文件**不是目录，
# _sweep_tmp_dirs 的 is_dir 过滤盖不住，所以另有 _sweep_orphan_files。
# 前缀里带 pid（`cesium_terrain_<pid>_xxxx.tif`），归属判定走 pid 而不是 mtime：
# 物化产物写完 mtime 就冻住，而切片可以再跑几小时，mtime 判据在这一类上近乎无效。
_MATERIALISED_PREFIX = "cesium_terrain_"
# cache 内 .part 的最深落点:瓦片 cache/{namespace}/{z}/{x}/{y}.png 的 x 目录
# (根=0 往下 4 层);dem cache 是 cache/dem/<granule>,更浅,一并覆盖。
# 限深是为了不随 cache 增长无界遍历 —— 瓦片文件本身在叶子层,扫目录名
# 不需要再往下走。
# ✅ 源命名空间落地（`cache/<style>` → `cache/<style>-<fingerprint>`,见
#    services/source_registry）时**复核过这个 4**:改的只是那一层目录的**名字**,
#    层数没变(仍是 namespace/z/x/y),所以 4 依然正好够到 x 目录。
#    真正会让它失效的是「再插一层」——比如按源 host 再分一级 —— 那时这个常量
#    必须一起加,否则 .part 清扫会在最深一层前停下,残片永远留在盘上而且不报错。
_CACHE_PART_MAX_DEPTH = 4

# 本进程的启动时刻（近似为本模块导入时刻）。启动清扫只处理【早于】它的临时
# 目录 —— 更新的目录只可能属于另一个活着的进程（H3）。
_PROCESS_START_TIME = time.time()

# 手动清理的分类名白名单:cache 顶层目录名(各 style 代码 / dem),
# 拒绝路径分隔符与 ..,配合 resolve 校验防越界。
_CATEGORY_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


# fail_stranded_running_task 的表名/列名白名单 —— 两者都直接进 SQL，只接受字面量。
#
# 入网判据是**「行先置 running、终态由另一段代码负责写」**，不是「有没有
# `_run_task`」。曾按后者把 local_terrain_tasks 与 dem_terrain_jobs 排除在外
# （当时写的理由是「切片线程 `_run_tiling_job` 自己有兜底 except 把行判
# failed」），这两条管线各自证伪了它：兜底 except 的第一句就是
# `conn = get_connection()`，它在自己的 try【之外】，建连接失败时新异常穿透
# 线程、行留在 running；而 stop 分支是**正常 return**，压根没有异常给那个
# except 接。往后新增这种表一律登记进来。
#
# id_column：定位「这个任务的那一行」的列。四张任务表按主键 id；
# dem_terrain_jobs 是挂在 dem_tasks 下的从表（一个任务一行，唯一键是 task_id，
# 见 DemTaskManager.start_tiling 的 `ON CONFLICT(task_id)`），按 task_id 定位。
# recovery：判 failed 之后用户**真正**做得到的动作。两类表不一样，统一写死
# 「可以重新开始这个任务」对下载类是骗人的：那三条管线的 start/resume 只收
# pending/paused（TaskManager.start_task 里的注释说明这是有意的 —— 失败是
# 终态，重跑会把「它失败过」从历史里擦掉），failed 行按「开始」只会再吃一个
# ValueError，用户拿着一句「可以重新开始」找不到能按的地方。切片类相反，
# start_tiling 的闸门是 `status != 'running'`，failed 行重切本来就是通的。
_RESTART_TILING = '可以重新开始切片。'
_RESTART_NEW_TASK = '失败是终态，「开始 / 继续」不再接受这条记录，请新建一个同样的任务。'


class _StrandedTable(NamedTuple):
    id_column: str
    recovery: str


_STRANDED_TASK_TABLES = {
    'tasks': _StrandedTable('id', _RESTART_NEW_TASK),
    'dem_tasks': _StrandedTable('id', _RESTART_NEW_TASK),
    'contour_tasks': _StrandedTable('id', _RESTART_NEW_TASK),
    'local_terrain_tasks': _StrandedTable('id', _RESTART_TILING),
    'dem_terrain_jobs': _StrandedTable('task_id', _RESTART_TILING),
}

_STRANDED_ERROR = '任务线程已退出但状态仍是「运行中」，已置为失败'


def fail_stranded_running_task(table: str, task_id: int, reason: str = '') -> bool:
    """工作线程退出时行仍停在 'running' → 判 failed。返回是否真的改了行。

    **为什么需要这道网。** 三个 manager 的 `_run_task` 只 `logger.error` 就把异常
    吃掉,而 `_execute*` 自己的失败兜底(把行改成 failed 那段)活在
    `conn = get_connection()` 【之后】的 try 里 —— 建连接失败(库被锁/损坏/磁盘满)
    或 `asyncio.run` 建不出事件循环(EMFILE,几个任务 × concurrent_downloads 就够)
    都绕过它。两条切片线程(`DemTaskManager._run_tiling_job` /
    `LocalTerrainTaskManager._run_tiling_job`)是同一个形状:它们的兜底 except 里
    第一句也是 `conn = get_connection()`,同样在自己的 try 之外。另一条路是删除:
    `task_deletion.delete_task_row` 先置停止标志再 DELETE,commit 失败时事务回滚而
    标志【不】回滚(那是有意的,重试删除仍要它停),worker 于是走某个 stop 分支
    **正常** return —— 没有异常,也没人写终态。

    两条路的结果一样:行永远是 running 且没有线程。下载类三条管线的 start_task 只
    接受 pending/paused,用户点「开始」被拒;两张切片表则被各自 start_tiling 的
    `status != 'running'` 闸门判成「已在运行」而 ValueError —— 都只剩重启进程。

    **放在线程退出处**是因为那里能一次盖住两条路,不用去动 890 行的 `_execute_task`。
    竞态是安全的:行只要还是 running 就不可能有新 worker 被登记(start_task 与
    start_tiling 的门都在这一条上),所以 `WHERE status='running'` 命中的必然是搁死
    的那一行;正常收尾(completed/failed/paused)与已删除的行都命不中,是无害的
    no-op。

    本函数**绝不抛**:调用点在 finally 里,从那儿抛出去会盖掉真正的异常。
    """
    spec = _STRANDED_TASK_TABLES.get(table)
    if spec is None:
        raise ValueError(f'fail_stranded_running_task: 未知任务表 {table!r}')

    message = f'{_STRANDED_ERROR}；{spec.recovery}'
    if reason:
        message += f'（{reason}）'
    try:
        from src.core.database import get_connection_context, utc_now_iso

        with get_connection_context() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET status = 'failed', error_message = ?, "
                f"completed_at = ? WHERE {spec.id_column} = ? AND status = 'running'",
                (message, utc_now_iso(), task_id))
            changed = cur.rowcount > 0
            conn.commit()
    except Exception as e:
        # 连补偿都写不进去(库彻底不可用)只能记账:重启时的孤儿恢复会把它捞成 paused。
        logger.error(f'{table} id={task_id}: 搁死状态补偿失败: {e!r}')
        return False

    if changed:
        logger.warning(
            f'{table} id={task_id}: 线程已退出而行仍是 running,已判 failed'
            + (f'({reason})' if reason else ''))
    return changed


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


# 删除进度回调的口径 —— 只有 remove_task_dir_if_safe / remove_task_dir_and_confirm
# 的 progress_cb 用它，消费者是 services/task_deletion 的 socket 广播。
#
#   progress_cb(phase, done, total)
#     phase='scan'    统计阶段：done = 已扫到的条目数，total = None
#     phase='delete'  删除阶段：done = 已删条目数，total = 扫描阶段数出来的总数
#
# 「条目」= 文件 + 目录，两个阶段同一套口径。用文件数当分母、却把 rmdir 也算进
# 分子的话，百分比会在末尾冲过 100%（瓦片金字塔的目录数是文件数的百分之几）。
#
# 为什么要先扫一遍才删：没有分母就只有一个不断变大的数字，用户看不出还要等多久
# ——「删除进度」这个需求要的正是那个百分比。扫描一遍只做 scandir、不改元数据，
# 比删除那一遍便宜得多（删除要写目录项、Windows 上还要过一遍杀软）。
_PROGRESS_REPORT_EVERY = 256


def _scandir_entries(path: str) -> list:
    """列出目录直下的条目。任何 OSError 当作空目录 —— 与 rmtree(ignore_errors=True)
    同一条约定：删不动的东西留在盘上，但绝不让删除流程因此抛出。"""
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _iter_tree_bottom_up(root: str):
    """产出 `(路径, 是否目录)`，子项恒排在父目录之前。

    符号链接一律按「文件」产出、**不跟进**（`is_dir(follow_symlinks=False)`）——
    与 shutil.rmtree 的判据逐字相同。跟进的后果是顺着链接把别处的目录删空，
    而 remove_task_dir_if_safe 的护栏只查 task_dir 自身的各层，管不到树内部。

    迭代而非递归：瓦片金字塔是 {z}/{x}/{y}.png 三层，递归本来也够，但物化中间
    件与用户自定义目录深度不可控，递归深度不该由磁盘内容决定。
    """
    # (路径, 是否目录, 子项是否已入栈)
    stack = [(root, True, False)]
    while stack:
        path, is_dir, expanded = stack.pop()
        if is_dir and not expanded:
            stack.append((path, True, True))
            for entry in _scandir_entries(path):
                try:
                    child_is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    child_is_dir = False
                stack.append((entry.path, child_is_dir, False))
        else:
            yield path, is_dir


def _rmtree_reporting(target: Path, progress_cb) -> None:
    """删整棵树并逐段回报进度。语义等价 `shutil.rmtree(target, ignore_errors=True)`。

    只在调用方真的要进度时才走这条路：它比 rmtree 多遍历一遍（数分母）。
    没有 progress_cb 的调用方（启动补删、手动清理）继续走 rmtree，不付这份钱。

    单个条目删不掉（Windows 文件占用、权限）只是跳过，不中断也不抛 —— 与
    ignore_errors=True 一致。「到底删干净没有」由 remove_task_dir_and_confirm
    事后 exists() 复查，不由这里的返回值表达。
    """
    total = 0
    for _ in _iter_tree_bottom_up(str(target)):
        total += 1
        if total % _PROGRESS_REPORT_EVERY == 0:
            progress_cb('scan', total, None)
    progress_cb('scan', total, total)

    done = 0
    for path, is_dir in _iter_tree_bottom_up(str(target)):
        try:
            if is_dir:
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError:
            pass
        done += 1
        if done % _PROGRESS_REPORT_EVERY == 0:
            progress_cb('delete', done, total)
    # 终态必发：条目数不是 _PROGRESS_REPORT_EVERY 的整数倍时，上面那发漏掉的
    # 尾巴正是进度条停在 97% 不动的那一段。
    progress_cb('delete', done, total)


def remove_task_dir_if_safe(task_dir, *, progress_cb=None) -> bool:
    """
    Best-effort removal of a task's on-disk artifact directory.

    Args:
        task_dir: Candidate artifact directory (e.g. output_path/task_<id>).

    Returns:
        True if the directory was eligible for removal (whether or not it
        existed), False if it fell outside the safety boundary.

        progress_cb: 可选的删除进度回调，口径见 _PROGRESS_REPORT_EVERY 上方。
            给了它就改走 _rmtree_reporting（多遍历一遍数分母），不给则仍是
            shutil.rmtree —— 没人看进度的调用方不该为此多扫一遍盘。

    ⚠️ 返回值是「护栏放行了」,**不是**「目录真的没了」:下面用的是
    `rmtree(..., ignore_errors=True)`,Windows 上任意一个文件被占(资源管理器
    预览、看图软件、杀软扫描)就会静默失败,而这里照样返回 True。凡是要把结果
    报给用户、或者拿它去销 pending_deletions 账的调用方,一律改用
    remove_task_dir_and_confirm —— 那个函数把「放行」和「真没了」分成两个字段,
    照抄本函数的返回值就是 2026-08-08 评审 P1#6 那条 bug。

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
            if progress_cb is None:
                shutil.rmtree(target, ignore_errors=True)
            else:
                _rmtree_reporting(target, progress_cb)
            logger.info(f"Removed task artifact directory: {target}")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove task artifact dir {task_dir}: {e}")
        return False


def purge_registered_artifacts(pipeline: str, task_id: int,
                               removed_dir=None) -> dict:
    """删产物文件时的收尾：清掉**落在任务目录之外**的登记产物，再销掉登记行。

    为什么需要它：MBTiles 不在任务目录里。`artifact_export` 把库写在
    `<output_path>/<任务名>.mbtiles` —— 与 `task_<id>/` **同级**，因为它是那个
    目录的打包结果，装进去会在下一次导出时被自己打进自己。于是
    「删任务并删文件」只 rmtree 了 `task_<id>/`，几百 MB 的库留在盘上，而登记
    行刚被销掉 —— 从此没有任何东西知道它存在。这正是 artifacts 表要防的事，
    却由删除路径亲手制造出来。

    顺序：先删文件、后销行。反过来的话中途失败就把线索也丢了（与
    `pending_deletions` 那条「先入队再删」是同一个道理）。

    Args:
        removed_dir: 调用方已经整个删掉的任务目录。落在它**里面**的产物跳过
            —— 已经没了，再 stat 一次只是浪费；而且那条路径此刻必然不存在，
            走下面的 unlink 分支只会白记一条 debug。

    Returns:
        {'files_removed': N, 'rows_removed': M}

    **绝不抛**：它跑在一次已经成功的删除之后，让它把 200 翻成 500 只会让用户
    以为任务没删掉而再点一次。
    """
    from src.services.artifact_store import delete_artifacts_for, list_artifacts

    files_removed = 0
    try:
        inside = None
        if removed_dir is not None:
            try:
                inside = Path(removed_dir).expanduser().absolute().resolve()
            except OSError:
                inside = None

        for artifact in list_artifacts(pipeline, task_id):
            try:
                raw = Path(artifact.path).expanduser().absolute()
                target = raw.resolve()
            except OSError:
                continue
            if inside is not None and (target == inside or inside in target.parents):
                continue  # 已随任务目录一起没了
            if _has_symlink_component(raw):
                # 与 remove_task_dir_if_safe 同一条判据：跟着链接删会删到别处。
                logger.warning(f"Refusing to delete artifact via symlink: {raw}")
                continue
            if target.is_dir():
                # 登记在任务目录之外的**目录**型产物。这里不 rmtree ——
                # remove_task_dir_if_safe 那套边界是按「任务目录」设计的，套在一个
                # 任意登记路径上是在赌。留下文件、留一条日志，比赌一次 rmtree 好。
                logger.warning(
                    f"Artifact directory outside the task dir kept: {target}")
                continue
            try:
                target.unlink()
                files_removed += 1
                logger.info(f"Removed registered artifact file: {target}")
            except FileNotFoundError:
                pass  # 已经不在了，等同删掉
            except OSError as e:
                logger.warning(f"Cannot remove artifact file {target}: {e}")
    except Exception as e:
        logger.warning(
            f"Artifact purge failed for {pipeline}/{task_id} (ignored): {e}")

    rows_removed = delete_artifacts_for(pipeline, task_id)
    return {'files_removed': files_removed, 'rows_removed': rows_removed}


class DirRemoval(NamedTuple):
    """remove_task_dir_and_confirm 的结果。

    eligible: 护栏放行了(路径在安全边界内)。False 表示这个目录**永远**删不掉,
        再排队重试只会每次启动刷一条 warning。
    removed: 走完之后目录确实不在了(含「本来就不存在」)。这才是能报给用户、
        能拿去销 pending_deletions 账的那一位。
    """
    eligible: bool
    removed: bool


def remove_task_dir_and_confirm(task_dir, *, progress_cb=None) -> DirRemoval:
    """删产物目录,并复查它是不是真的没了。

    为什么单独有这个函数:remove_task_dir_if_safe 内部是
    `rmtree(..., ignore_errors=True)`,只要护栏放行就返回 True —— 它报的是
    「可删」不是「已删」。三个需要真相的消费者(删除快路径、后台收尾、启动补删)
    此前各自在调用点后面补一句 `and not dir.exists()`,漏抄一处的后果是
    Windows 上文件被占时接口回 `files_removed: true` 而整个瓦片金字塔留在盘上
    (2026-08-08 评审 P1#6,快路径就是漏抄的那一处)。判据收到这里一份,
    第四个消费者不必再抄一遍。

    exists() 用与护栏同一套展开(expanduser):拿没展开的 `~/...` 去 exists()
    恒为 False,会把整类 ~ 路径误报成「删掉了」。
    """
    eligible = remove_task_dir_if_safe(task_dir, progress_cb=progress_cb)
    if not eligible:
        return DirRemoval(False, False)
    try:
        removed = not Path(task_dir).expanduser().exists()
    except OSError:
        # 连 stat 都做不了(权限/断开的网络盘)——不敢说删掉了
        removed = False
    return DirRemoval(True, removed)



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
    """从 `<name>.part.<pid>[.<id>]` 里解析出写它的进程 pid，取不到返回 None。

    落在 CACHE_DIR 内的三处原子写临时件名里本来就带 `os.getpid()`（download_engine
    与 dem_download_engine），这是三类清扫对象里唯一带归属信息的一类 —— 可以用它
    精确跳过另一个活进程正在写的文件，而不必退到 mtime 这种近似判据（H3）。

    量级兜底（2026-08-08 评审）：四个生产者【现在】都把 `os.getpid()` 写在
    pid 槽位上，但 task_manager._stream_copy_tile 是这次评审才改的 —— 改名之前
    它写的是 `.part.<thread_ident>`，形态与 `.part.<pid>` 一模一样，靠形状分不
    出来。用户盘上那批存量文件不会自己消失，所以对超出 pid 量级的值一律返回
    None（当作「归属未知」），免得拿一个线程 id 去和活进程表比对得出反向结论。
    这不是完备判据（Windows 的线程 id 与 pid 同量级），完备性靠的是生产者都写
    os.getpid() —— 见模块顶部登记表的 ⚠️。
    """
    marker = ".part."
    idx = name.rfind(marker)
    if idx < 0:
        return None
    head = name[idx + len(marker):].split(".")
    if not head or not head[0].isdigit():
        return None
    try:
        pid = int(head[0])
    except ValueError:
        return None
    return pid if 0 < pid < _MAX_PLAUSIBLE_PID else None


def _materialised_owner_pid(name: str) -> Optional[int]:
    """从 `cesium_terrain_<pid>_xxxx.tif` 里解析出写它的进程 pid。

    与 _part_owner_pid 同一个用途、不同的命名形态（那边是后缀 `.part.<pid>.<id>`，
    这边是前缀）。取不到返回 None —— 老版本（2026-08-06 之前）的产物名里没有
    pid，那些只能退到 mtime 判据。
    """
    if not name.startswith(_MATERIALISED_PREFIX):
        return None
    rest = name[len(_MATERIALISED_PREFIX):]
    head = rest.split("_", 1)[0]
    if not head.isdigit():
        return None
    try:
        return int(head)
    except ValueError:
        return None


def _sweep_orphan_files(root: Path, prefix: str, older_than: Optional[float] = None) -> int:
    """删除 root **直下**以 prefix 开头的文件（不递归），返回删除数。

    为什么不复用 _sweep_tmp_dirs：那个函数第一件事就是 `entry.is_dir()`，
    只处理目录型残留。

    为什么不递归：任务目录下面是 terrain_tiles/{z}/{x}/{y}.terrain，可达百万级
    条目，rglob 会把启动拖到分钟级。物化产物的落点是确定的（恒在 work_dir 直下），
    不需要走下去。

    归属判定优先用文件名里的 pid（另一个活进程正在用就跳过）；解析不出 pid 的
    老产物退回 mtime 判据。符号链接一律不碰。
    """
    removed = 0
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.name.startswith(prefix):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            owner = _materialised_owner_pid(entry.name)
            if owner is not None:
                if owner == os.getpid():
                    continue
                try:
                    from src.core.process_watchdog import pid_alive
                    if pid_alive(owner):
                        # 另一个活着的进程正在拿它切片。pid 复用只会导致漏删
                        # （下次启动再清），方向是安全的。
                        continue
                except Exception:
                    pass
            elif older_than is not None:
                # 没有 pid 的老产物：只能退到 mtime。这条判据对本类残留偏弱
                # （写完就冻结），保留只是为了不漏掉升级前留下的文件。
                try:
                    if entry.stat(follow_symlinks=False).st_mtime >= older_than:
                        continue
                except OSError:
                    continue
            os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


def record_retained_output(artifact_dir) -> bool:
    """登记一个「任务行已删、文件按用户要求保留」的产物目录。返回是否登记成功。

    为什么必须登记：删除任务时不勾「同时删除磁盘产物」是四条管线的**默认**，
    而任务行一走（DEM 还会连带级联掉 dem_terrain_jobs），
    `<output_path>/<pipeline>_task_<id>/` 就成了零引用目录 —— 启动清扫只认
    pending_deletions 与几张任务表，从此谁都找不回它。半成品切片目录（用户点
    删除时任务多半还没跑完）就这么永久留在用户盘上，而目录直下还可能压着一个
    与源数据同量级的物化中间栅格（cesium_terrain_<pid>_*.tif）——
    _materialised_sweep_roots 把本表当扫描根，正是为了把那条回收路径接回来。

    **这张表不授权删任何东西**：用户说了留文件，就一个字节都不许动。它只保证
    「app 建的目录始终有一条 DB 引用」，与 pending_deletions（读到一行=删掉它）
    是两个动词，不要混。

    non-absolute 一律拒收：相对路径的基准是进程 cwd，冻结 exe 的 cwd 是用户
    双击时所在的任意目录，登记一条按 cwd 解释的路径等于给清扫根埋一个随机目录。
    全程 best-effort —— 登记失败不该让删除接口报错（行已经删了）。
    """
    try:
        target = Path(artifact_dir).expanduser()
        if not target.is_absolute():
            logger.warning(
                f"Refusing to record non-absolute retained output: {str(target)!r}")
            return False
        from src.core.database import get_connection_context

        with get_connection_context() as conn:
            # path UNIQUE + INSERT OR IGNORE：同一目录重复登记没有意义
            conn.execute(
                "INSERT OR IGNORE INTO retained_outputs (path) VALUES (?)",
                (str(target),))
            conn.commit()
        logger.info(f"Recorded retained output directory: {target}")
        return True
    except Exception as e:
        logger.warning(f"Failed to record retained output {artifact_dir}: {e}")
        return False


def _retained_output_roots() -> list[Path]:
    """读出 retained_outputs 里仍然存在的目录，顺手把已经消失的行销掉。

    销账放在这里而不是单开一类清扫：这张表唯一的消费者就是扫描根，用户在资源
    管理器里手工删掉目录之后行留着只会让每次启动多 scandir 一个不存在的路径，
    而「目录不在了」正好就是这条引用可以退休的判据 —— 表因此不会无界增长。

    与其它 DB 相关的清扫一样全程 best-effort：表不存在（老库、迁移中）返回空。
    """
    roots: list[Path] = []
    try:
        from src.core.database import get_connection_context

        with get_connection_context() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, path FROM retained_outputs").fetchall()
            except Exception:
                return []
            for row in rows:
                target = Path(row["path"]).expanduser()
                try:
                    alive = target.is_absolute() and target.exists()
                except OSError:
                    # 断开的网络盘：查不了就当它还在，别把引用丢了
                    alive = True
                if alive:
                    roots.append(target)
                else:
                    conn.execute(
                        "DELETE FROM retained_outputs WHERE id = ?", (row["id"],))
            conn.commit()
    except Exception as e:
        logger.warning(f"Retained-output roots lookup failed (ignored): {e}")
    return roots


def _materialised_sweep_roots() -> list[Path]:
    """物化产物可能落在哪些目录 —— work_dir 恒等于「切片输出目录的父级」。

    必须查 DB 而不是只扫 DOWNLOADS_DIR：DEM 任务的 output_path 自 0.2.4 起是
    用户每次创建任务时必传的**任意绝对路径**（geo_validation 明确放开了全盘），
    而那正是 GB 级残留最容易发生的一条线。这与 sweep_startup_residue 已经为
    contour_warp_tmpdir / stitch_tmpdir 读配置键扩根的做法同构。

    retained_outputs 也是一个根：任务被「删记录、留文件」删掉之后，
    dem_terrain_jobs 行随外键级联一起没了，上面那条查询再也推不出这个 work_dir，
    而物化中间栅格恰恰就躺在里面（见 record_retained_output）。
    """
    roots: list[Path] = []
    # 本地地形任务（固定路径）与全球 base 构建脚本的落点
    try:
        roots.append(Path(Config.DOWNLOADS_DIR) / "terrain")
    except Exception:
        pass
    # DEM 切片作业与本地地形任务：从 DB 取真实的输出目录
    try:
        from src.core.database import get_connection_context

        with get_connection_context() as conn:
            for table in ("dem_terrain_jobs", "local_terrain_tasks"):
                try:
                    rows = conn.execute(
                        f"SELECT DISTINCT output_dir FROM {table} "
                        "WHERE output_dir IS NOT NULL AND output_dir != ''").fetchall()
                except Exception:
                    continue
                for row in rows:
                    try:
                        roots.append(Path(row[0]).parent)
                    except Exception:
                        continue
    except Exception:
        # 数据库不可用（fresh clone、迁移中等）不应让启动清扫失败
        pass
    # 已经没有任务行、但用户选择保留文件的产物目录（它自己就是 work_dir）
    roots.extend(_retained_output_roots())

    seen: set = set()
    unique: list[Path] = []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


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


def _sweep_pending_deletions() -> int:
    """补删 pending_deletions 里残留的任务产物目录。

    返回本次从清单里销账的目录数 —— 含「早就不在了」的那些（用户手工删过），
    所以不等于本次真正 rmtree 掉的个数。

    与前六类不同，这一类的线索来自 DB 而不是文件名模式：删除任务时先记清单再
    删任务行（同一事务），后台线程删成功后清行。进程被强杀时行会留下来。

    三种结局，**不能只看护栏的返回值** —— 判据统一在 remove_task_dir_and_confirm
    里（它把「可删」和「真没了」拆成两个字段，理由见那边的 docstring）：
      - eligible=False（越界）→ 清行。它永远不会被删掉，留着只会每次启动重试
        一遍并刷一条 warning。
      - removed=True → 清行，计入删除数（含「早就不在了」）。
      - eligible=True 但 removed=False → **保留行**。Windows 上文件被占用时
        rmtree(ignore_errors=True) 会静默失败，只看「可删」就会把没删干净的
        目录从清单里抹掉，那正是这张表要防的事。

    非绝对路径（空串 / '.' / 相对路径）不交给护栏，直接清行丢弃：护栏按【进程
    当前工作目录】解释它们，那是一个能删掉无关目录的口子，详见循环里的注释。

    表不存在（老库、迁移中）时返回 0，不抛 —— 启动清扫全程 best-effort。
    """
    removed = 0
    try:
        # 延迟 import 必须在 try 【里面】（与 _materialised_sweep_roots 里那句
        # `from src.core.database import get_connection_context`
        # 同构）：它存在的理由就是防未来成环，一旦抛，外面没有任何一层接得住
        # —— 调用点 sweep_startup_residue 没套 try，会一路穿到 create_app()。
        from src.core.database import get_connection_context

        with get_connection_context() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, path FROM pending_deletions").fetchall()
            except Exception as e:
                logger.warning(
                    f"Pending-deletion sweep: table unavailable (ignored): {e}")
                return 0
            for row in rows:
                # expanduser 在这里做一次，并且【同一个 target】既喂护栏又拿去
                # exists()：护栏内部自己会 expanduser（remove_task_dir_if_safe 里
                # 的 `Path(task_dir).expanduser().absolute()`），拿没展开的
                # `~/...` 去 exists() 恒为 False（实测），「删不干净就保留行」
                # 那一支会对整类 ~ 路径失效 —— 正是这张表要防的事。
                target = Path(row["path"]).expanduser()
                if not target.is_absolute():
                    # 空串 / '.' / 相对路径会被护栏按【进程 cwd】解释（它用的是
                    # absolute()，基准就是 cwd）。冻结 exe 的 cwd 是用户双击时
                    # 所在的任意目录（桌面、下载夹、数据盘），实测 '' 和 '.'
                    # 会把 cwd 整个 rmtree 掉。清行丢弃，不交给护栏。
                    logger.warning(
                        "Pending-deletion sweep: dropping non-absolute path: "
                        f"{row['path']!r}")
                    conn.execute(
                        "DELETE FROM pending_deletions WHERE id = ?", (row["id"],))
                    continue
                outcome = remove_task_dir_and_confirm(target)
                if outcome.eligible and not outcome.removed:
                    # 没删干净（占用中）—— 留着行，下次启动再试
                    continue
                conn.execute(
                    "DELETE FROM pending_deletions WHERE id = ?", (row["id"],))
                if outcome.removed:
                    removed += 1
            conn.commit()
    except Exception as e:
        logger.warning(f"Pending-deletion sweep failed (ignored): {e}")
        return removed
    return removed


def sweep_startup_residue() -> None:
    """启动一次性清扫九类残留 —— 前七类是 finally 盖不住（SIGKILL/关窗）的临时件，
    后两类是**有保留期/容量上限**的长期资产，到期治理没有别的触发点：

    1. stitch work_dir（map_dl_stitch_*，系统临时目录 + 配置的 stitch_tmpdir）；
    2. contour warp tmpdir（contour_warp_*，系统临时目录 + 配置的
       contour_warp_tmpdir 两处）；
    3. 两处上传暂存目录（local_upload_* / contour_upload_*，位于
       DOWNLOADS_DIR 下 —— 它们的清理只有 except 分支和函数末尾各一条 rmtree、
       **没有 finally**，Ctrl-C / SystemExit 会整个绕过）；
    4. 共享瓦片/DEM cache 里的原子写临时件（*.part.*）；
    5. 多幅 DEM 物化的中间栅格（cesium_terrain_<pid>_*，与源数据同量级，
       GB 级任务就是 GB）—— 落在切片输出目录的父级，而 DEM 任务的 output_path
       是用户自选的全盘路径，所以扫描根要从 DB 取（见 _materialised_sweep_roots）；
    6. 随包底图的解压临时目录（.base_unpack_<pid>_*，位于 assets/terrain）——
       最多 167 MB / 4.3 万个文件，且那是 Nuitka 的 --include-data-dir 源目录，
       残留会被打进发布产物；前五类的扫描根一条都覆盖不到那里。
    7. 上次进程没删完的任务产物目录（pending_deletions 表）—— 唯一一类线索来自
       DB 而不是文件名模式的残留，见 _sweep_pending_deletions。
    8. 过期的每任务日志（`logs/tasks/<pipeline>_<id>.log`，超过
       task_log_retain_days）—— 见 services/task_logging.prune_task_logs。
    9. 超出 `cache_max_mb` 的瓦片缓存 —— 见
       services/cache_exclusive.enforce_cache_capacity。

    第 8、9 类为什么落在启动而不是定时器：这个进程里没有调度器，而缓存与日志
    的增长是**跨会话**的（一次几百 GB 的下载跑完就退出了）。启动是唯一一个
    「一定会到、且此刻没有任务在跑」的时刻 —— 后者尤其重要，第 9 类会真的删
    瓦片，放在运行期做就要和 clear_cache_category 一样考虑活动任务。

    只处理 mtime 早于本进程启动时刻的目录（见 _sweep_tmp_dirs 的 older_than）；
    .part 文件与物化栅格按名字里的 pid 跳过仍存活的写者。

    全程 best-effort：单个删除失败跳过，整体异常只记日志，绝不影响启动。
    匹配规则按前缀/通配精确限定（见模块顶部常量），同步执行、毫秒级。
    """
    removed = {"stitch": 0, "warp": 0, "upload": 0, "part": 0, "materialised": 0,
               "base_unpack": 0, "pending": 0, "task_log": 0, "cache_bytes": 0}
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

        # 第 6 类：随包底图的解压临时目录。落点 = 缓存目录的父级 = 分卷所在的
        # assets/terrain。只读安装（Program Files / 只读介质）下 scandir 直接
        # 失败返回 0，不需要额外分支。
        removed["base_unpack"] += _sweep_tmp_dirs(
            base_terrain.base_cache_dir().parent, _BASE_UNPACK_PREFIX, started_at)
    except Exception as e:
        logger.warning(f"Startup residue sweep failed (ignored): {e}")
        return

    # 第 5 类：多幅 DEM 物化的中间栅格。单独套一层 try —— 它要查 DB，比前四类
    # 多一个失败面，不该因为数据库暂时不可用就把已经统计好的清扫结果丢掉。
    try:
        for root in _materialised_sweep_roots():
            removed["materialised"] += _sweep_orphan_files(
                root, _MATERIALISED_PREFIX, started_at)
    except Exception as e:
        logger.warning(f"Materialised-raster sweep failed (ignored): {e}")

    # 第 7 类：上次进程没删完的任务产物目录。与第 5 类同理排在前六类的 except
    # 之后 —— 它要查 DB，多一个失败面，不该因为数据库暂时不可用就把已统计的
    # 清扫结果丢掉。异常在 _sweep_pending_deletions 内部就已吞掉并记日志，
    # 这里不必再套一层 try。
    removed["pending"] += _sweep_pending_deletions()

    # 第 8 类：过期的每任务日志。每一层都必须挡住异常 —— prune_task_logs 自己
    # 承诺不抛（它的 docstring 写着），但「承诺」不是护栏：它要读配置库、要
    # 列目录、要 unlink，任何一处冒出 OSError 都会把启动打断在一个用户完全
    # 无从下手的地方（表现是双击 exe 什么都没发生）。日志清不掉的代价只是
    # 磁盘上多几个文件；启动起不来的代价是整个程序不能用。
    try:
        from src.services.task_logging import prune_task_logs
        removed["task_log"] += prune_task_logs()
    except Exception as e:
        logger.warning(f"Task-log pruning failed (ignored): {e}")

    # 第 9 类：把缓存总量压回 cache_max_mb 以内（0 = 不限）。同样单独套 try,
    # 理由同上,而且它比日志清理重得多:要遍历全部命名空间 + 查任务表 + rmtree。
    # 只在这里调,不在运行期调 —— 它会真的删瓦片,运行期删就要处理活动任务
    # （clear_cache_category 的那道护栏）,而启动时没有任务在跑。
    # cache_exclusive 在函数体里 import：它反过来要用本模块的 _sum_dir_bytes。
    try:
        from src.services import cache_exclusive
        capacity = cache_exclusive.enforce_cache_capacity()
        removed["cache_bytes"] += int(capacity.get('removed_bytes') or 0)
    except Exception as e:
        logger.warning(f"Cache capacity enforcement failed (ignored): {e}")

    # cache_bytes 是**字节数**不是件数,混进 total 会让「清理了 3 件残留」变成
    # 「清理了 5033164 件」。单独摘出来。
    freed_bytes = removed.pop("cache_bytes")
    total = sum(removed.values())
    if total or freed_bytes:
        logger.info(
            f"Startup residue sweep removed {total} leftover(s): "
            f"stitch tmp={removed['stitch']}, contour warp tmp={removed['warp']}, "
            f"upload tmp={removed['upload']}, cache .part={removed['part']}, "
            f"materialised raster={removed['materialised']}, "
            f"base unpack tmp={removed['base_unpack']}, "
            f"pending deletions={removed['pending']}, "
            f"expired task logs={removed['task_log']}; "
            f"cache trimmed by {freed_bytes / 1024 / 1024:.1f}MB"
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


def _category_label(dir_name: str) -> str:
    """缓存顶层目录名 → 给人看的分类名。

    源命名空间落地之后目录名变成了 `s-1a2b3c4d`（样式码 + 配置指纹，见
    contracts/source.SourceSnapshot.cache_namespace）。直接把它印在缓存管理页上，
    用户看到的是一串没有任何意义的十六进制 —— 而这一页唯一的操作是「删掉哪一类」，
    认不出是哪个图源就等于让人凭运气删几十 GB。

    所以这里把它翻回人话：`s` → satellite，指纹保留在括号里当消歧标识（换过源
    之后同一个样式会有两个命名空间，它们的区别**只有**指纹）。

    映射表从 source_registry.STYLE_NAMES 取,不在这里抄第二份 —— 抄一份就等于
    「加了新样式，缓存页仍显示单字符码」这类静默走样。认不出的码原样显示：
    用户自定义图源的码不在表里，显示 `瓦片缓存（q-1a2b3c4d）` 也比显示一个猜
    出来的名字诚实。

    文案走 i18n（`api.cache.category.*`）：这几个字符串是直接印在配置页上的
    界面文案，写死中文就是英文界面上突然冒出来的一句中文。括号也在译文里 ——
    中文全角（）、英文半角 ()，不是同一个字符。没有请求上下文时（启动清扫、
    CLI、测试）get_locale() 回落 zh，输出与改造前逐字一致。
    """
    if dir_name == 'dem':
        return t('api.cache.category.dem')
    code = SourceSnapshot.style_of_namespace(dir_name)
    style = STYLE_NAMES.get(code, '')
    if not style:
        return t('api.cache.category.tiles', name=dir_name)
    if SourceSnapshot.is_namespace(dir_name):
        # `s-1a2b3c4d` → `瓦片缓存（satellite · 1a2b3c4d）`
        fingerprint = dir_name[len(code) + 1:]
        return t('api.cache.category.tiles_fingerprint',
                 style=style, fingerprint=fingerprint)
    # 迁移前的裸样式码目录（`cache/s`）。user_version 6 会把它们改名，但迁移
    # 失败（目录被占用）时它们还在，得有个说法而不是掉进上面那条兜底。
    return t('api.cache.category.tiles', name=style)


def get_cache_stats(cache_root=None) -> dict:
    """分类统计下载缓存占用:cache 顶层每个子目录一个分类。

    分类规则:dem → DEM 缓存(重下需 Earthdata 登录);其余子目录是各源命名空间
    (`<样式码>-<配置指纹>`)的瓦片缓存,标签由 _category_label 翻成人话;顶层
    散落文件(正常不会有)归入 _root/其他。
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
                label = _category_label(entry.name)
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
        categories.append({'key': '_root',
                           'label': t('api.cache.category.other'),
                           'size_bytes': root_bytes, 'file_count': root_files})
        total_bytes += root_bytes
    categories.sort(key=lambda c: c['size_bytes'], reverse=True)
    return {'categories': categories, 'total_bytes': total_bytes}


def _namespace_in_use(namespace: str) -> bool:
    """这个缓存命名空间**此刻**被某个活动任务引用着吗。

    「活动」的判据是 contracts/outcome.ACTIVE_STATE_VALUES（pending / running /
    paused / retrying / pending_decision …），口径与调度、历史列表完全一致 ——
    这里绝不手写状态字面量，正列表少写一个状态就是少拦一类正在跑的任务。

    判定结果直接复用 `cache_exclusive.cache_usage_by_namespace()` 里的 `active`
    标志，不在这里重新查一遍任务表：路由层已经有一道「整库清理时列出全部未完成
    任务」的 409 闸（api.clear_cache_api 的 _unfinished_task_labels），这道闸是它
    的**每分类**版本，两处判据一旦分头实现就会漂移成「整库拦、单类不拦」。

    cache_exclusive 只能在函数体里 import：它在 cache_usage_by_namespace 内部反过来
    import 本模块的 _sum_dir_bytes，顶层引进来就是一个 import 环。

    读不出来时返回 False（放行）。这是有意的：库坏了/表不存在时把清理**永久**锁死，
    等于把用户唯一的磁盘回收入口也一起毁掉，而误删的最坏后果只是 cache miss 后重下。
    """
    from src.services import cache_exclusive

    try:
        usage = cache_exclusive.cache_usage_by_namespace()
    except Exception as e:
        logger.warning(
            f"Cannot check cache namespace liveness for {namespace!r}: {e}")
        return False
    info = usage.get(namespace)
    return bool(info and info.get('active'))


def clear_cache_category(category: str, cache_root=None, *, force: bool = False) -> dict:
    """手动清理一个缓存分类(删除 cache 顶层对应子目录的全部内容)。

    安全护栏(与 remove_task_dir_if_safe 同一思路,宁可拒绝不可误删):
    category 必须是简单目录名(_CATEGORY_NAME_RE,拒绝 .. / 分隔符 /
    绝对路径),且 resolve 后严格位于 CACHE_DIR 内、不等于 CACHE_DIR。
    目录不存在时抛 ValueError(前端分类清单来自 get_cache_stats,
    不存在即视为非法输入)。

    **活动任务护栏**（force=False 时生效）:这个命名空间正被某个活动任务引用
    就直接拒绝。此前这里是**零**存活性检查的一次 rmtree —— 用户在下载途中点
    「清理 satellite 缓存」,正在跑的任务立刻踩空:

      - 枚举阶段命中 cache 的瓦片已经被移出待下载列表并计进 downloaded_tiles,
        它们**不会**重下;
      - 产物目录靠补拷线程从 cache 复制,源没了只吞成一条 warning;
      - 完成判定只看 task_tiles 的失败行,而 cache 命中瓦片从不在那张表里。

    合起来就是:任务照样 completed、计数满值、产物目录静默缺瓦片。tiles_only
    任务全程无声。这与 api.clear_cache_api 里那道「整库清理」的 409 闸是同一个
    危险的两种粒度,判据也复用同一个(见 _namespace_in_use),不各写一份。

    Args:
        force: 跳过活动任务护栏。给「用户看到警告后仍坚持」的那条路留的,
            与路由层 force 参数同一语义。

    Returns:
        {'removed_bytes': N, 'removed_files': M}

    Raises:
        ValueError: category 非法、越界、不存在,或被活动任务引用且未传 force。
    """
    root = Path(cache_root) if cache_root is not None else Path(Config.CACHE_DIR)
    if not _CATEGORY_NAME_RE.match(category or ''):
        raise ValueError(f"非法的缓存分类名: {category!r}")

    # 护栏只对源命名空间有意义:`dem` 与 `_root` 不在 cache_usage_by_namespace 的
    # 视野里(它只枚举 SourceSnapshot.is_namespace 认的目录),对它们查也是白查。
    # DEM 缓存的活动引用留给路由层那道整库 409 闸 —— 它按四张任务表判,覆盖 DEM。
    if not force and _namespace_in_use(category):
        raise ValueError(
            f"缓存分类 {category!r} 正被运行中的任务使用,清理会让它静默缺瓦片;"
            f"请先暂停或删除相关任务,或确认后强制清理")

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
