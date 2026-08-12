"""磁盘预算 —— 五种独立估算 + **全局感知**的空间判决（§4.2）。

## 定位：只估算与展示，不拦截

2026-08 起本模块**不再拦截任何任务**：估算超过可用空间既不拒绝启动，也不
中途叫停。判决的去处只剩两个 —— 前端选区时的「需要约 X、可用 Y」展示
（routes/api.py 的估算接口），与任务日志里的一行数字。用户在一块快满的盘
上硬跑是他的选择；模块的职责是让这个选择**知情**，并在 ENOSPC 真的发生时
留下「还差多少」的事后诊断数字。

## 为什么必须有这个模块

改造前 TerraForge **完全没有磁盘预检**：`shutil.disk_usage` / `statvfs` 在全部
`*.py` / `*.js` 里零命中，代码里所有「磁盘满」都是事后处理注释
（`dem_task_manager.py:504`、`download_engine.py:1226`、
`local_terrain_task_manager.py:790`）。已有的只是**数量估算**（前端瓦片计数、
`raster_probe` 的最大层级），不是字节预算。后果不是「少一个提示」：百万瓦片的
任务在盘写满的那一刻，GDAL 边写边落盘的 GTiff 会留下一个**非空**半成品，而
断点判定是「output_path 存在且非空就跳过重拼」—— 于是恢复后那一层被记成功、
任务 completed 无 warning，用户拿到一个静默截断的产物。

## 三条上游事故，本模块逐条对应

- GeoD [#30](https://github.com/gaopengbin/geo-downloader/issues/30)：预估
  23.91 GB、实际 408.11 GB，**偏差 17 倍**（Google 卫星 z16、1,253,724 瓦片）。
  根因是硬编码 `avg_tile_size_kb = 20`，且估算不看压缩、金字塔与图源。它的修复
  （`58cfa570`，`commands.rs:203`）把这个常量变成「按图源类型与 zoom 分档的
  函数」——**那个修复形态就是本模块 `avg_tile_bytes` 的最低标准**：平均值至少
  要随图源与层级变化，绝不能是一个常数。这里还多做一步：出错的另一半是它只算
  了瓦片、没算产物，所以 `estimate_map_task` 把「松散瓦片镜像 + 逐层拼接产物 +
  拼接工作区」分开算，各自出数。
- GeoD [#32](https://github.com/gaopengbin/geo-downloader/issues/32)：瓦片先缓冲
  到系统 TEMP 再整体搬到目标盘，海量瓦片时 IO 翻倍（跨盘搬运是逐字节复制，不是
  rename）。对应 `work_dir_for`：工作目录跟随输出盘。
- GeoD 的运行中复查是**固定 512 MiB 地板**（`fs_util.rs:57-67`），拦不住
  「这一层要 40 GB、只剩 3 GB」。对应 `recheck_remaining`：按**剩余工作量**重估，
  没有地板。

## 两处判决，各自的节奏

`check_budget` 在**按下开始**那一刻调用（四条管线的 `start_*` 各一次）与
前端估算接口里调用。它只是一张快照：任务要排队等名额，跑起来又是几十分钟
到几小时，这期间另一个任务、另一个进程、用户自己拷东西都能把盘吃掉。
判决不通过也照常放行 —— 调用方把 `verdict.reason`（自带四个数字）记一笔
warning 就继续。

`recheck_remaining` 是**运行中**的复查，由 `RunningRecheck` 接到每条管线的循环
上（它负责节流与绝不抛）。四条管线的接线点与节奏：

- 地图下载：`download_engine.download_tiles_batch` 的**批**边界（每批
  `DOWNLOAD_BATCH_SIZE` 张）。剩余量走 `remaining_map_estimate`。
- DEM 下载：`dem_download_engine.download_files` 的**颗粒**边界（一颗 30-50 MB，
  正好是复查要盯的量级）。
- 等高线渲染：`contour_engine.build_contour_tiles` 的批 / 逐瓦片检查点，与
  `stop_flag` 同一处。剩余量只算没渲的瓦片（warp 产物在渲染开始前就落盘了）。
- 地形切片：`cesium_terrain.build_input_raster` 开工之前一次（单幅与多幅**都**
  查，只是单幅不算物化那一份）。这条管线是一次性的 `build_terrain` 调用，中间
  没有可插的批边界，所以就这一次。

这些复查的全部产物是日志：真的 ENOSPC 时，任务日志里最后一行 `disk_recheck`
事件就是「剩下的活要多少、盘上还剩多少」，而不是一句没头没尾的 I/O error。

## 全局性：数字为什么仍然要扣掉别人的预留

单机单任务时「查剩余空间」谁都会写。问题在并发：四个任务各自查一次同一块盘，
各自看到同一份剩余空间 —— 拦截时代里这会一起写满盘；只展示的时代里这会让
每个任务的日志与前端都报一个偏乐观的「可用」。所以 `check_budget` 仍向
`resource_scheduler` 要「其他任务已经预留但尚未落盘的字节数」并从可用空间里
扣掉（scheduler 对 `DISK_BYTES` 只记账不设限）。判决之后调用方仍应去
scheduler 上真的 `reserve(DISK_BYTES)`，否则这次判决对下一个任务不可见。

## 估算值的诚实边界

本模块所有系数都是**保守假设**，不是本仓库的实测统计（唯一例外是地形物化的
0.78 / 1.9，那两个数来自 `cesium_terrain.py:600-606` 的实测记录）。真正的长期
解法是按图源做**运行统计**（每个 namespace 记录已下载瓦片的字节数直方图，用
实测均值替换本表），本模块的分档表是那之前的替代品。所有系数都放在模块级常量
里、带出处注释，就是为了将来替换时能一眼看全影响面。

估算永远会错，问题只是错多少：`disk_safety_factor`（出厂 1.15）吸收「错一点」，
「错 17 倍」必须靠分档估算解决，不能靠拧系数。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from src.contracts.region_tiles import (MAX_ZOOM, MIN_ZOOM, count_region_tiles,
                                        validate_zoom_range)
from src.contracts.reservation import ResourceKind
from src.core.database import DEFAULT_CONFIGS

logger = logging.getLogger(__name__)

__all__ = [
    'DiskEstimate', 'BudgetVerdict',
    'free_bytes', 'same_volume', 'avg_tile_bytes',
    'estimate_map_task', 'estimate_dem_task', 'estimate_terrain_tiling',
    'estimate_contour_task', 'remaining_map_estimate',
    'check_budget', 'recheck_remaining', 'RunningRecheck', 'work_dir_for',
]

KIB = 1024
MIB = 1024 * 1024

# 兜底配置值从 DEFAULT_CONFIGS 取，不在这里抄第二份数字 —— 抄一份就意味着
# 「改了出厂默认但忘了改兜底」时，配置库坏掉的降级路径用的是一个谁也没审过的
# 值。口径与 resource_scheduler._DEFAULT_CONFIG_VALUES 一致。
_DEFAULT_CONFIG_VALUES: Dict[str, str] = dict(DEFAULT_CONFIGS)

_BUDGET_CONFIG_KEYS = ('disk_budget_enabled', 'disk_reserve_mb', 'disk_safety_factor')

# 「同一个坏值只警告一次」的去重集合。判决在任务运行期间会被反复调用
# （`RunningRecheck` 把 `recheck_remaining` 接在下载/渲染循环的批边界上），
# 不去重的话一个拼错的配置值会把日志刷爆。
# 用 raw 值入键，所以用户把坏值改成另一个坏值时仍会再警告一次。
_WARNED_VALUES: set = set()
_WARN_LOCK = threading.Lock()


def _warn_once(dedupe_key, message: str) -> None:
    with _WARN_LOCK:
        if dedupe_key in _WARNED_VALUES:
            return
        _WARNED_VALUES.add(dedupe_key)
    logger.warning(message)


# --------------------------------------------------------------------------
# 每瓦片平均字节数：按【图源类别 × 层级档】分档
#
# ⚠️ 下面这张表是**保守假设，不是本仓库的实测统计**。它的形制（而不是具体
#    数字）才是要点：GeoD #30 的 17 倍偏差来自一个常数 20 KB，它的修复把常数
#    变成了「随图源与 zoom 变化的函数」，本表是同一形状。
#    长期正解是按 SourceSnapshot namespace 做运行统计（已下载瓦片的实际字节数
#    均值），届时本表退化为「没有统计数据时的冷启动值」。
#
# 数字的依据（量级判断，非测量）：
#   - 影像类（卫星 / 混合）是 JPEG 照片，信息量随层级上升而增加：低层级一张瓦片
#     覆盖整个海面或大片森林（同色，压得动），高层级是城市纹理（压不动）。
#     这正是 #30 踩的坑 —— 用低层级的经验值去乘高层级的瓦片数。
#   - 路网 / 矢量渲染类是有限调色板的 PNG，随层级几乎不变（多出来的只是更多
#     线与标注），所以这一档基本平坦。
#   - 地形晕渲介于两者之间：连续色但低频。
# 每档写成 (zoom_upper_inclusive, bytes)，按 zoom 从小到大匹配第一个命中的档。
# --------------------------------------------------------------------------
_TILE_BYTES_BANDS: Dict[str, tuple] = {
    'imagery': ((6, 20 * KIB), (10, 35 * KIB), (13, 55 * KIB),
                (16, 75 * KIB), (MAX_ZOOM, 95 * KIB)),
    'vector': ((6, 8 * KIB), (10, 12 * KIB), (13, 15 * KIB),
               (16, 18 * KIB), (MAX_ZOOM, 20 * KIB)),
    'terrain': ((6, 15 * KIB), (10, 25 * KIB), (13, 35 * KIB),
                (16, 45 * KIB), (MAX_ZOOM, 55 * KIB)),
}

# style_code（1 字符）→ 体积类别。**故意只认代码、不认人类可读的样式名**：
# 样式名 → 代码的映射表只有一份（wave B 起住在 source_registry.STYLE_CODES），
# 在这里再列一遍人类名就是第二处事实来源。未登记的代码（用户自定义图源）按最贵
# 的一类算 —— 估少了会让任务在半路写满盘，估多了只是拦得早一点。
_TILE_CLASS_BY_STYLE_CODE: Dict[str, str] = {
    'm': 'vector',    # roadmap
    's': 'imagery',   # satellite
    'y': 'imagery',   # hybrid（卫星 + 标注，体积与纯卫星同量级）
    't': 'terrain',   # terrain
}
_DEFAULT_TILE_CLASS = 'imagery'

# 拼接产物的像素成本：一张瓦片在马赛克里占 256×256 像素 × 3 波段（RGB）。
# download_engine 的逐块配准中间件也是这个尺寸（`:1594-1605` 的注释里写着
# 「每瓦片固定 ~196KB(256x256x3)」），两处是同一个事实。
_MOSAIC_BYTES_PER_TILE = 256 * 256 * 3

# GTiff 压缩系数（出厂 gdal_compression=LZW，见 DEFAULT_CONFIGS）。
# ⚠️ 同样是假设。分类别给是因为无损压缩在两类数据上的表现差一个量级：
# LZW/DEFLATE 对照片级 RGB 几乎无效（熵已经很高），对有限调色板的路网渲染极其
# 有效。用一个平均系数会同时高估路网、低估影像 —— 而低估影像正是 #30 的方向。
# 用户把 gdal_compression 设成 NONE 时真值是 1.0；本函数签名里没有
# config_manager（估算要在建任务表单里、库里还没有这一行的时候就能算），
# 这部分偏差由 disk_safety_factor 吸收。
_MOSAIC_COMPRESSION_BY_CLASS: Dict[str, float] = {
    'imagery': 0.90,
    'vector': 0.35,
    'terrain': 0.60,
}

# 输出格式 → 产物形态。
# ⚠️ 这三个集合的行为事实源是 task_manager 的两个内联判断
# （`:1486` 决定是否拼接、`:1637` 决定是否镜像瓦片），那里目前是字面量列表、
# 没有具名常量可 import。等那边抽出常量，这里必须改成 import 而不是各留一份。
# 'png' / 'jpg' 是历史同义值（实际产物仍是 GTiff，见 task_manager:1255 的注释）。
_STITCHED_FORMATS = frozenset({'both', 'image_only', 'png', 'jpg'})
_LOOSE_TILE_FORMATS = frozenset({'both', 'image_only', 'png', 'jpg', 'tiles_only'})
# MBTiles 容器（§5.3 的通用产物容器）的额外开销：5% 是 SQLite 页头、索引与
# metadata 表。**没有 _CONTAINER_FORMATS 这张表** —— MBTiles 与 output_format
# 正交（见 estimate_map_task 的 export_mbtiles 参数），拿 output_format 去查表
# 的那个版本是恒假的死代码。
_CONTAINER_OVERHEAD = 1.05

# DEM 每颗粒字节数。⚠️ 保守假设，同上。
#   COP-DEM-GLO-30：1°×1° Float32 COG。3601×3601×4 B ≈ 49.5 MiB 未压缩，
#     DEFLATE 后按最坏情况留到 45 MiB（地形起伏大的颗粒压不动）。
#   ASTGTM.003：Int16 的 _dem.tif（3601×3601×2 B ≈ 24.7 MiB 未压缩）。_num 伴随
#     文件更小，但按同样大小计 —— 颗粒数是调用方数出来的，这里不区分类型。
_DEM_GRANULE_BYTES: Dict[str, int] = {
    'COP-DEM-GLO-30': 45 * MIB,
    'ASTGTM.003': 25 * MIB,
}
_DEM_GRANULE_BYTES_FALLBACK = 45 * MIB

# 地形切片的物化中间栅格系数。**这两个数是实测**，出处
# `src/services/terrain_tiling/cesium_terrain.py:600-606`：6 幅 ASTER（Int16，
# 源 118 MB）物化 + 8 层 overview 共 91.8 MB = 源的 78%；默认数据集
# Copernicus GLO-30 是 Float32，实测约为源的 1.9 倍。
_TERRAIN_MATERIALISE_INT16 = 0.78
_TERRAIN_MATERIALISE_FLOAT32 = 1.9
# quantized-mesh 金字塔产物。⚠️ 假设：网格是按高程起伏自适应简化的，产物远小于
# 源栅格，但 z 从 0 到 maxzoom 的全金字塔又会把它加回来一部分。0.45 是保守取值。
_TERRAIN_MESH_FACTOR = 0.45

# 等高线：warp 到 3857 的像素膨胀。EPSG:4326 → 3857 在中高纬会拉伸，输出栅格的
# 像素数多于源。⚠️ 1.2 是保守假设（赤道附近接近 1.0，60° 附近更高）。
_CONTOUR_WARP_FACTOR = 1.2
# warp 产物之上再建内部金字塔（`contour_engine._build_raster_overviews`）：
# 2 的幂逐层减半，几何级数 1 + 1/4 + 1/16 + ... → 4/3。
_CONTOUR_OVERVIEW_FACTOR = 4.0 / 3.0
# 等高线瓦片。⚠️ 假设：纯线画 PNG 只有 10~20 KB，但开了 hillshade 混合后接近
# 照片级 PNG（`contour_hillshade_blend`），按后者留量。
_CONTOUR_TILE_BYTES = 48 * KIB


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskEstimate:
    """一次作业的五种独立估算（§4.2 的落地边界逐字对应）。

    network_bytes
        要从网络拉下来的字节数。它**不是**磁盘占用（缓存命中的部分不走网络），
        单独出数是为了让 UI 能回答「这个任务要下多少」。
    cache_bytes
        共享缓存目录的新增占用。
    temp_bytes
        工作区峰值（拼接 / warp / 物化的中间产物）。这一项在作业结束时归零，
        但作业期间与产物**同时存在**，所以它必须进 peak。
    output_bytes
        最终产物。
    peak_bytes
        同时存在的最大值。默认口径是 cache + output + temp —— 三者确实会共存
        （缓存不会在拼接前删、产物边写边留、工作区最后才清）。判决只看它。
    tile_count
        估算依据的瓦片数，给 UI 展示与日志复核用（估算错时第一件事是看它）。
    detail
        推导过程：逐层瓦片数、用到的每瓦片字节数、所有假设。UI 与每任务日志
        直接展示它 —— 一个不能被复核的估算数字，出错时没人查得动（#30 的
        issue 里正是靠「1,253,724 × 20 KB」这个算式才定位到根因）。
    """

    network_bytes: int
    cache_bytes: int
    temp_bytes: int
    output_bytes: int
    peak_bytes: int
    tile_count: int = 0
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetVerdict:
    """空间判决。**永远带数字**，因为「空间不足」这四个字对用户毫无操作性。
    判决只用于展示与日志（ok=False 不拦任何东西，见模块 docstring 头部）。

    free_bytes
        **扣掉其他任务已预留字节之后**的可用空间，不是 `shutil.disk_usage` 的
        原始值。这是本模块与「每个任务各查一次剩余空间」的唯一区别所在，也是
        它必须是这个口径的理由：判决要回答的是「轮到我还剩多少」。
    required_bytes
        `peak_bytes × disk_safety_factor`。
    reserve_bytes
        `disk_reserve_mb`，永远不给任务用的系统地板（盘写到 0 会让 SQLite 与
        日志一起失败，那时连「为什么失败」都记不下来）。
    shortfall_bytes
        差多少。`ok` 为真时是 0；为假时**必然为正** —— UI 直接拿它说
        「再腾出 X」。
    reason
        一句话英文，把上面四个数说清楚。
    """

    ok: bool
    free_bytes: int
    required_bytes: int
    reserve_bytes: int
    shortfall_bytes: int
    reason: str


# --------------------------------------------------------------------------
# 文件系统探测
# --------------------------------------------------------------------------


def _nearest_existing(path) -> Optional[Path]:
    """向上找到第一个真实存在的祖先目录。

    存在的意义：预检发生在**建目录之前**，目标目录几乎必然还不存在，而
    `shutil.disk_usage` 对不存在的路径直接抛 FileNotFoundError。要问的其实是
    「这条路径将来会落在哪块盘上」，最近的已存在祖先就是答案。
    """
    try:
        p = Path(path).expanduser()
    except (TypeError, ValueError):
        return None
    try:
        # strict=False：不存在的路径也能拿到绝对形式（还顺带解开符号链接，
        # 而符号链接可能指向另一块盘 —— 那正是我们要问的东西）。
        p = p.resolve()
    except OSError:
        try:
            p = p.absolute()
        except OSError:
            return None
    for candidate in (p, *p.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            # 权限不足 / 网络盘掉线：这一层问不出来，继续往上问。
            continue
    return None


def free_bytes(path) -> int:
    """`path` 所在卷的剩余字节数。**绝不抛** —— 探测失败返回 0。

    返回 0 而不是抛异常，是因为调用点全在「任务能不能起」这条主路径上：一个
    路径探测失败不该让建任务的 HTTP 请求 500。0 会让 `check_budget` 判 not ok，
    用户看到的是「空间不足」而不是崩溃 —— 保守但可操作。真实原因留在 warning
    里（去重，见 `_warn_once`）。
    """
    target = _nearest_existing(path)
    if target is None:
        _warn_once(('free_bytes', str(path)),
                   f'disk_budget: no existing ancestor for {path!r}; '
                   f'reporting 0 bytes free')
        return 0
    try:
        return int(shutil.disk_usage(target).free)
    except OSError as e:
        _warn_once(('free_bytes_oserror', str(target)),
                   f'disk_budget: cannot read free space of {target} ({e}); '
                   f'reporting 0 bytes free')
        return 0


def same_volume(a, b) -> bool:
    """两条路径是否在同一块盘上（按最近的已存在祖先判断）。

    用途是 `work_dir_for`：跨盘的「工作目录」意味着产物要被**逐字节复制**过去
    而不是 rename，海量瓦片时 IO 直接翻倍（GeoD #32）。

    判据是 `st_dev`。Windows 上 `st_dev` 对本地盘可用，但网络路径 / 挂载点的
    行为不保证；探测失败时退回比较 `Path.anchor`（`C:\\` 或 `/`）—— 那个判据在
    Windows 上恰好等价于盘符相同，在 POSIX 上则永远相等（退化成「假定同盘」，
    对本函数是安全方向：最坏情况是把工作目录放到系统临时目录，行为与改造前一致）。
    """
    pa, pb = _nearest_existing(a), _nearest_existing(b)
    if pa is None or pb is None:
        return False
    try:
        return os.stat(pa).st_dev == os.stat(pb).st_dev
    except OSError as e:
        _warn_once(('same_volume', f'{pa}|{pb}'),
                   f'disk_budget: st_dev comparison failed for {pa} / {pb} ({e}); '
                   f'falling back to drive-anchor comparison')
        return pa.anchor.lower() == pb.anchor.lower()


# --------------------------------------------------------------------------
# 估算
# --------------------------------------------------------------------------


def _tile_class(style_code: str) -> str:
    code = str(style_code or '').strip()
    cls = _TILE_CLASS_BY_STYLE_CODE.get(code)
    if cls is None:
        _warn_once(('tile_class', code),
                   f'disk_budget: unknown style code {code!r}; sizing tiles as '
                   f'{_DEFAULT_TILE_CLASS} (the most expensive class)')
        return _DEFAULT_TILE_CLASS
    return cls


def avg_tile_bytes(style_code: str, zoom: int) -> int:
    """单张瓦片的平均字节数，**同时是图源与层级的函数**。

    这个签名本身就是 #30 的教训：它的旧实现是常量 `avg_tile_size_kb = 20`，
    用低层级路网的经验值去乘高层级卫星影像的瓦片数，于是 23.91 GB 对上了
    408.11 GB。返回值永远 > 0 —— 0 会让「预估 0 字节」这种明显错误的判决静默
    通过，比估错更糟。
    """
    try:
        z = int(zoom)
    except (TypeError, ValueError):
        z = MAX_ZOOM
    # 层级越界按边界档算：这里是估算，不是校验，越界值不该抛（真正的校验在
    # region_tiles.validate_zoom_range，建任务时已经过了那道门）。
    z = max(MIN_ZOOM, min(MAX_ZOOM, z))
    for upper, size in _TILE_BYTES_BANDS[_tile_class(style_code)]:
        if z <= upper:
            return size
    # 表的最后一档 upper 就是 MAX_ZOOM，走不到这里；留着是为了「改表时漏掉
    # 最高档」不会返回 None。
    return _TILE_BYTES_BANDS[_DEFAULT_TILE_CLASS][-1][1]


def estimate_map_task(region, zoom_min, zoom_max, output_format, style_code,
                      *, cached_tiles=0, export_mbtiles=False) -> DiskEstimate:
    """地图下载任务的五种估算。

    瓦片数一律走 `region_tiles.count_region_tiles` —— 全项目唯一的经纬度→瓦片
    实现。在这里另写一遍「按 bbox 算个大概」会让**预估的数**与**实际下的数**
    分叉，那是比估错单价更难查的偏差（多边形任务尤其：按 bbox 估会高出好几倍，
    用户会以为估算器坏了）。

    cached_tiles
        已在缓存里的瓦片数（标量，非逐层）。它只影响 network / cache，不影响
        产物 —— 缓存命中的瓦片照样要拷进输出目录。
    export_mbtiles
        任务是否勾了「同时导出 MBTiles」（`tasks.export_mbtiles`）。
        **必须由调用方显式传**：MBTiles 是与 `output_format` **正交**的追加产物
        （§5.3 的「同一任务的第 N 种产物」），不是它的第四个取值，所以从
        `output_format` 里推不出来。容器的体积约等于整份松散镜像，而导出发生在
        任务的最后一步 —— 不算进预算，就等于把「盘写满」推迟到跑了几小时之后
        的那一刻。
    """
    zoom_min, zoom_max = validate_zoom_range(zoom_min, zoom_max)
    fmt = str(output_format or '').strip().lower()
    code = str(style_code or '').strip()
    cls = _tile_class(code)

    tiles_by_zoom: Dict[int, int] = {}
    avg_by_zoom: Dict[int, int] = {}
    raw_tile_bytes = 0
    for z in range(zoom_min, zoom_max + 1):
        n = count_region_tiles(region, z, z)
        avg = avg_tile_bytes(code, z)
        tiles_by_zoom[z] = n
        avg_by_zoom[z] = avg
        raw_tile_bytes += n * avg

    total_tiles = sum(tiles_by_zoom.values())
    cached = max(0, min(int(cached_tiles or 0), total_tiles))
    # 命中率按**张数**折算到字节：cached_tiles 是标量，不知道命中的是哪一层。
    # 均匀假设会在「只有低层级命中」时低估剩余下载量，但那个方向由安全系数吸收，
    # 且真实的补漏场景（断点续传）本来就是各层均匀缺块。
    uncached_ratio = (total_tiles - cached) / total_tiles if total_tiles else 0.0
    network = int(raw_tile_bytes * uncached_ratio)
    # 下载下来的瓦片就落在缓存里，所以缓存新增量等于网络下载量。缓存关闭时
    # （cache_enabled=false）这一项会高估 —— 高估的方向是安全的，且调用方可以
    # 用 cached_tiles 表达命中，无法表达「不写缓存」，那属于 wave B 的接线。
    cache = network

    writes_loose = fmt in _LOOSE_TILE_FORMATS
    writes_stitched = fmt in _STITCHED_FORMATS
    # MBTiles **不是** output_format 的取值，所以这里不查表、只看那个正交开关。
    # 早先这里写的是 `fmt in _CONTAINER_FORMATS`，而合法的 output_format 只有
    # png/jpg/both/image_only/tiles_only（models/task.py 的 OutputFormat），
    # 'mbtiles' 永远不在其中 —— 那个分支是恒假的死代码，容器体积从来没有被
    # 算进过预算。
    writes_container = bool(export_mbtiles)
    if not (writes_loose or writes_stitched):
        # 未知格式按最费盘的组合算。「没登记 = 估 0」等于对新格式**默认关掉
        # 预算**，正是 #30 那类事故的温床（它的估算同样不看输出形态）。
        _warn_once(('output_format', fmt),
                   f'disk_budget: unknown output_format {fmt!r}; assuming the '
                   f'most expensive layout (loose tiles + stitched GeoTIFF)')
        writes_loose = writes_stitched = True

    mirror_bytes = raw_tile_bytes if writes_loose else 0
    # 容器与松散镜像**同时存在**：artifact_export 是从已经落盘的 XYZ 目录打包的
    # （artifact_export.py 的 add_dir），打包完两者都留着。所以这是加法不是替代。
    container_bytes = int(raw_tile_bytes * _CONTAINER_OVERHEAD) if writes_container else 0

    compression = _MOSAIC_COMPRESSION_BY_CLASS[cls]
    stitched_bytes = 0
    temp_bytes = 0
    if writes_stitched:
        # 逐层各出一个 GeoTIFF（task_manager 把产物命名成 `*_zoom_<z>.tif`），
        # 所以是逐层求和而不是「按最大层算一个」。
        for z, n in tiles_by_zoom.items():
            stitched_bytes += int(n * _MOSAIC_BYTES_PER_TILE * compression)
        # 工作区只装**一层**：stitch 是逐层跑的，每层建一个 map_dl_stitch_* 目录、
        # 跑完就删（download_engine.stitch_tiles_with_gdal）。所以峰值是最大的
        # 那一层，不是所有层之和。
        # 中间件本身带 COMPRESS=DEFLATE（download_engine:1594-1605），这里**故意
        # 不打折**：那是照片级 RGB，DEFLATE 的收益接近 0，而工作区估小了会在
        # 拼接阶段（任务已经跑了几小时之后）才写满盘 —— 最贵的失败时机。
        temp_bytes = max(tiles_by_zoom.values(), default=0) * _MOSAIC_BYTES_PER_TILE

    output = mirror_bytes + stitched_bytes + container_bytes
    # 三者共存：缓存不会在拼接前清、产物边写边留、工作区最后才删。
    peak = cache + output + temp_bytes

    detail = {
        'style_code': code,
        'tile_class': cls,
        'output_format': fmt,
        'zoom_min': zoom_min,
        'zoom_max': zoom_max,
        'tiles_by_zoom': tiles_by_zoom,
        'avg_tile_bytes_by_zoom': avg_by_zoom,
        'cached_tiles': cached,
        'raw_tile_bytes': raw_tile_bytes,
        'mirror_bytes': mirror_bytes,
        'stitched_bytes': stitched_bytes,
        'container_bytes': container_bytes,
        'mosaic_bytes_per_tile': _MOSAIC_BYTES_PER_TILE,
        'mosaic_compression_factor': compression,
        'assumptions': [
            f'per-tile size is a banded assumption for {cls} sources, not a '
            f'measurement from this deployment',
            f'stitched GeoTIFF sized as tiles x {_MOSAIC_BYTES_PER_TILE} B x '
            f'{compression} compression (LZW on {cls} data)',
            'stitch work dir holds one zoom level at a time, uncompressed',
            'cache hits assumed evenly spread across zoom levels',
        ],
    }
    return DiskEstimate(network_bytes=network, cache_bytes=cache,
                        temp_bytes=temp_bytes, output_bytes=output,
                        peak_bytes=peak, tile_count=total_tiles, detail=detail)


def remaining_map_estimate(full: DiskEstimate, done_tiles: int) -> DiskEstimate:
    """把一份**整任务**的地图估算折成「还没干的活」的估算。

    ## 为什么不是再调一次 `estimate_map_task(cached_tiles=已下数)`

    那个函数的 `cached_tiles` 只折 network / cache，产物一项**按整份算**（它的
    docstring 说明了理由：缓存命中的瓦片照样要拷进输出目录）。那是**启动时**的
    正确口径，却是运行中复查的错口径 —— 跑到 90% 时它仍然按整份松散镜像的
    空间报数，于是一个马上就要跑完的任务报出来的「需要」会是整份。
    `recheck_remaining` 的 docstring 把这条列为「数字必然虚高」。

    ## 折算口径

    - 随瓦片走的几项（network / cache / 松散镜像 / MBTiles 容器）按**未下载
      比例**折：已下好的瓦片既在缓存里也在产物目录里了，那些字节已经占用，
      不该再要求一遍。
    - 逐层拼接产物与拼接工作区**不折**：拼接跑在整轮下载之后，此刻一个字节
      都还没写。
    - 不重算瓦片数。`count_region_tiles` 对大多边形不便宜，而复查是每十几秒
      一次的循环调用；所有需要的分项都在 `full.detail` 里（那份 detail 本来
      就是为了「估算要能被复核」而存在的）。
    """
    total = max(0, int(full.tile_count or 0))
    done = max(0, min(int(done_tiles or 0), total))
    ratio = ((total - done) / total) if total else 0.0
    d = full.detail or {}
    raw = max(0, int(d.get('raw_tile_bytes') or 0))
    mirror = max(0, int(d.get('mirror_bytes') or 0))
    container = max(0, int(d.get('container_bytes') or 0))
    stitched = max(0, int(d.get('stitched_bytes') or 0))

    network = int(raw * ratio)
    cache = network
    output = int((mirror + container) * ratio) + stitched
    temp = max(0, int(full.temp_bytes or 0))
    detail = dict(d)
    detail.update({
        'done_tiles': done,
        'remaining_tiles': total - done,
        'remaining_ratio': round(ratio, 6),
        'folded_from': 'estimate_map_task',
    })
    return DiskEstimate(network_bytes=network, cache_bytes=cache, temp_bytes=temp,
                        output_bytes=output, peak_bytes=cache + output + temp,
                        tile_count=total - done, detail=detail)


def estimate_dem_task(granule_count: int, dataset: str) -> DiskEstimate:
    """DEM 颗粒下载的估算。单价按数据集分档 —— GLO-30 是 Float32，ASTER 是
    Int16，同样是 1°×1° 的一块，体积差近一倍。

    颗粒先落共享缓存（dem_cache_enabled 出厂为 true）再拷进任务目录，所以缓存
    与产物**同时**各占一份，peak 是两倍下载量。
    """
    n = max(0, int(granule_count or 0))
    key = str(dataset or '').strip()
    per = _DEM_GRANULE_BYTES.get(key)
    if per is None:
        _warn_once(('dem_dataset', key),
                   f'disk_budget: unknown DEM dataset {key!r}; sizing granules at '
                   f'{_DEM_GRANULE_BYTES_FALLBACK // MIB} MiB each')
        per = _DEM_GRANULE_BYTES_FALLBACK

    total = n * per
    detail = {
        'dataset': key,
        'granule_count': n,
        'bytes_per_granule': per,
        'assumptions': [
            'per-granule size is a conservative assumption from the product '
            'specification (pixel count x data type), not a measurement',
            'granules occupy the shared DEM cache and the task output dir at '
            'the same time',
        ],
    }
    return DiskEstimate(network_bytes=total, cache_bytes=total, temp_bytes=0,
                        output_bytes=total, peak_bytes=total * 2,
                        tile_count=0, detail=detail)


def estimate_terrain_tiling(source_bytes: int, *, float_source: bool = False,
                            materialise: bool = True) -> DiskEstimate:
    """地形切片（quantized-mesh）的估算。纯本地作业，不走网络也不写缓存。

    中间栅格的两个系数是**实测**（`cesium_terrain.py:600-606`）：多幅输入必须
    先物化成单幅再补 overview，Int16 实测为源的 78%，Float32（默认数据集
    Copernicus GLO-30）实测约 1.9 倍。这一份与切片产物共存，切完才由
    `build_terrain` 的 finally 删掉 —— 所以它进 peak。

    `materialise=False` 是**单幅输入**那一档：`build_input_raster` 对单幅直接
    直通，一个字节都不物化，所以中间栅格那一项必须归零。默认 True（启动时不
    知道用户后面会喂几幅，按最坏情况报数）；只有运行中复查知道确切幅数，而在
    那里多算一整份中间栅格就是拿不存在的开销虚报。
    """
    src = max(0, int(source_bytes or 0))
    factor = _TERRAIN_MATERIALISE_FLOAT32 if float_source else _TERRAIN_MATERIALISE_INT16
    temp = int(src * factor) if materialise else 0
    output = int(src * _TERRAIN_MESH_FACTOR)
    detail = {
        'source_bytes': src,
        'float_source': bool(float_source),
        'materialise': bool(materialise),
        'materialise_factor': factor if materialise else 0,
        'mesh_factor': _TERRAIN_MESH_FACTOR,
        'assumptions': [
            'materialise factor measured on this codebase '
            '(cesium_terrain.py:600-606): 0.78x for Int16, 1.9x for Float32',
            f'quantized-mesh pyramid assumed at {_TERRAIN_MESH_FACTOR}x the '
            f'source raster (assumption, not a measurement)',
            ('single-input jobs skip materialisation entirely, so the '
             'intermediate raster is not counted here'
             if not materialise else
             'assumes the inputs must be materialised into one raster '
             '(true for every multi-input job)'),
        ],
    }
    return DiskEstimate(network_bytes=0, cache_bytes=0, temp_bytes=temp,
                        output_bytes=output, peak_bytes=temp + output,
                        tile_count=0, detail=detail)


def estimate_contour_task(source_bytes: int, tile_count: int) -> DiskEstimate:
    """等高线切片的估算。同样是纯本地作业。

    工作区 = warp 到 EPSG:3857 的 `dem_3857.tif`（重投影会拉伸像素）加上它的
    内部金字塔。金字塔的 4/3 是几何级数（每层边长减半 → 面积 1/4）的和，不是
    拍脑袋的数；warp 的膨胀系数才是假设。

    水体图层开启时还有一份同样大小的 att 栅格 —— 调用方把 att 源的字节数一起
    算进 `source_bytes` 即可，本函数不猜。
    """
    src = max(0, int(source_bytes or 0))
    tiles = max(0, int(tile_count or 0))
    temp = int(src * _CONTOUR_WARP_FACTOR * _CONTOUR_OVERVIEW_FACTOR)
    output = tiles * _CONTOUR_TILE_BYTES
    detail = {
        'source_bytes': src,
        'tile_count': tiles,
        'warp_factor': _CONTOUR_WARP_FACTOR,
        'overview_factor': _CONTOUR_OVERVIEW_FACTOR,
        'bytes_per_tile': _CONTOUR_TILE_BYTES,
        'assumptions': [
            f'reprojection to EPSG:3857 assumed to inflate the raster by '
            f'{_CONTOUR_WARP_FACTOR}x (latitude dependent, assumption)',
            'internal overviews add 1/3 of the warped raster (geometric series)',
            f'{_CONTOUR_TILE_BYTES // KIB} KiB per PNG tile assumes hillshade '
            f'blending is on; plain line tiles are far smaller',
        ],
    }
    return DiskEstimate(network_bytes=0, cache_bytes=0, temp_bytes=temp,
                        output_bytes=output, peak_bytes=temp + output,
                        tile_count=tiles, detail=detail)


# --------------------------------------------------------------------------
# 判决
# --------------------------------------------------------------------------


def _config(config_manager):
    if config_manager is not None:
        return config_manager
    # 惰性构造：本模块会被 routes / manager 在 import 期拉起，那时 data/ 目录与
    # config 表可能都还不存在。构造 ConfigManager 本身不碰库（它的方法才碰）。
    from src.services.config_manager import ConfigManager
    return ConfigManager()


def _budget_settings(config_manager) -> tuple:
    """读三个预算配置。**绝不抛**：脏值 / 缺行 / 库读不出来一律退回出厂默认。

    `ConfigManager.get_many` 对 sqlite 错误是有意重抛的（不把锁/IO 错误静默吞成
    默认值）。但在预算这一层「读不到配置」不该等于「任务全部起不来」——退回
    DEFAULT_CONFIGS 让服务继续跑，同时留一条 warning。
    """
    default_enabled = _DEFAULT_CONFIG_VALUES['disk_budget_enabled'] != 'false'
    default_reserve = int(_DEFAULT_CONFIG_VALUES['disk_reserve_mb'])
    default_factor = float(_DEFAULT_CONFIG_VALUES['disk_safety_factor'])
    try:
        raw = _config(config_manager).get_many(_BUDGET_CONFIG_KEYS)
    except Exception as e:
        _warn_once(('budget_read', str(e)),
                   f'disk_budget: cannot read budget config ({e}); falling back '
                   f'to DEFAULT_CONFIGS')
        return default_enabled, default_reserve * MIB, default_factor

    # 布尔开关的口径与全项目一致：只有字面量 'false' 关掉，脏值等价于默认（开）。
    raw_enabled = raw.get('disk_budget_enabled')
    enabled = default_enabled if raw_enabled is None else str(raw_enabled).strip().lower() != 'false'

    reserve_mb = default_reserve
    raw_reserve = raw.get('disk_reserve_mb')
    if raw_reserve is not None:
        try:
            reserve_mb = max(0, int(str(raw_reserve).strip()))
        except (TypeError, ValueError):
            _warn_once(('disk_reserve_mb', raw_reserve),
                       f'disk_budget: disk_reserve_mb={raw_reserve!r} is not an '
                       f'integer, using default {default_reserve}')

    factor = default_factor
    raw_factor = raw.get('disk_safety_factor')
    if raw_factor is not None:
        try:
            parsed = float(str(raw_factor).strip())
        except (TypeError, ValueError):
            _warn_once(('disk_safety_factor', raw_factor),
                       f'disk_budget: disk_safety_factor={raw_factor!r} is not a '
                       f'number, using default {default_factor}')
        else:
            # < 1 的系数意味着「按比估算值更少的空间放行」，那是把安全余量用成
            # 了折扣；_VALUE_RULES 的下界也是 1.0。
            if parsed < 1.0:
                _warn_once(('disk_safety_factor_low', raw_factor),
                           f'disk_budget: disk_safety_factor={raw_factor!r} is below '
                           f'1.0, using default {default_factor}')
            else:
                factor = parsed

    return enabled, reserve_mb * MIB, factor


def _reserved_by_others(exclude_owner=None) -> int:
    """其他任务已在 scheduler 上预留、但尚未落盘的字节数。

    **这一行是本模块与「每个任务各查一次 disk_usage」的全部区别。** 没有它，
    四个并发任务会各自看到同一份剩余空间、各报各的乐观数字
    （§4.2 的「预算必须是全局的」）。

    `exclude_owner` 是**运行中复查**必须传的：那时调用方自己已经持有一张
    DISK_BYTES 凭据（启动时预留的），把它算进「别人占的」会让报出的可用空间
    比真实值小一份自身预算 —— 实测：物理 70 MiB 空闲、任务需要 40 MiB，任务
    按规矩预留了自己那 40 MiB，跑到中途复查看到的就是「可用 30 MiB、还缺
    10 MiB」的虚警。而 reason 里还会写「40 MiB 已被**其他任务**预留」，把用户
    支去找一个并不存在的并发任务。

    惰性 import：本模块要能在不拉起 scheduler 的情况下单独导入（估算函数是纯
    计算，测试与 UI 预览都会只用它们）。scheduler 不可用时按 0 处理并留 warning
    —— 退化成「只看物理剩余空间」，比让建任务直接失败好。
    """
    try:
        from src.services.resource_scheduler import get_scheduler
        snap = get_scheduler().snapshot()
        total = max(0, int((snap.get('in_use') or {}).get(
            ResourceKind.DISK_BYTES.value, 0) or 0))
        if exclude_owner is None:
            return total
        mine = 0
        for entry in snap.get('owners') or ():
            if tuple(entry.get('owner') or ()) == tuple(exclude_owner):
                mine = int((entry.get('granted') or {}).get(
                    ResourceKind.DISK_BYTES.value, 0) or 0)
                break
        return max(0, total - mine)
    except Exception as e:
        _warn_once(('scheduler', str(e)),
                   f'disk_budget: cannot read reserved disk bytes from the '
                   f'scheduler ({e}); checking physical free space only — '
                   f'concurrent tasks may each pass the same check')
        return 0


def _human(n: int) -> str:
    """给 reason 用的人类可读字节数。判决里出现的每个数都要能被用户直接对照
    资源管理器/df 看，所以用 GiB/MiB 而不是原始字节。"""
    value = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024.0 or unit == 'TiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024.0
    return f'{value:.1f} TiB'


def _verdict(path, estimate: DiskEstimate, config_manager, *, what: str,
             exclude_owner=None) -> BudgetVerdict:
    """check_budget 与 recheck_remaining 的共同算式。差别只在 reason 的措辞
    （「这个任务要多少」vs「剩下的活还要多少」）与 `exclude_owner`—— 算式必须是
    同一个，否则启动时日志与运行中日志会是两套口径互相打架的数字。"""
    enabled, reserve, factor = _budget_settings(config_manager)
    physical_free = free_bytes(path)
    reserved = _reserved_by_others(exclude_owner)
    available = max(0, physical_free - reserved)
    required = int(estimate.peak_bytes * factor)
    shortfall = max(0, required + reserve - available)

    held = (f' ({_human(reserved)} is already reserved by other tasks)'
            if reserved else '')
    if not enabled:
        # 开关现在的全部影响就是这一支：verdict 恒 ok（UI 不弹「磁盘不足」、
        # 日志措辞变成「检查已关」），估算数字照常给。拦截语义移除之后它不再
        # 放行任何东西 —— 本来就没有东西可放了。
        would_block = (f'; shortfall is {_human(shortfall)}'
                       if shortfall > 0 else '')
        return BudgetVerdict(
            ok=True, free_bytes=available, required_bytes=required,
            reserve_bytes=reserve, shortfall_bytes=shortfall,
            reason=(f'disk budget check is disabled; {what} needs about '
                    f'{_human(required)} plus a {_human(reserve)} reserve and '
                    f'{_human(available)} is free{held}{would_block}'))

    if shortfall > 0:
        return BudgetVerdict(
            ok=False, free_bytes=available, required_bytes=required,
            reserve_bytes=reserve, shortfall_bytes=shortfall,
            reason=(f'not enough disk space: {what} needs {_human(required)} '
                    f'plus a {_human(reserve)} safety reserve, but only '
                    f'{_human(available)} is free{held} — free up '
                    f'{_human(shortfall)} to continue'))

    return BudgetVerdict(
        ok=True, free_bytes=available, required_bytes=required,
        reserve_bytes=reserve, shortfall_bytes=0,
        reason=(f'{_human(available)} free{held} covers the {_human(required)} '
                f'{what} needs plus the {_human(reserve)} safety reserve'))


def check_budget(path, estimate: DiskEstimate, config_manager=None) -> BudgetVerdict:
    """启动时与前端估算接口的判决：`peak × disk_safety_factor + disk_reserve_mb`
    对上「扣掉别人预留之后的剩余空间」。

    判决**只用于展示与日志**，不通过也不拦任何东西（调用方记一笔 warning 继续）。

    ⚠️ 判决之后调用方仍应紧接着去
    `resource_scheduler.reserve(ResourceKind.DISK_BYTES, required)` 真的预留，
    否则这次判决对下一个任务不可见，并发任务看到的「可用」会偏乐观。
    """
    return _verdict(path, estimate, config_manager, what='this task')


def recheck_remaining(path, remaining: DiskEstimate, config_manager=None, *,
                      owner=None) -> BudgetVerdict:
    """运行中的复查：按**剩余工作量**重估，不是固定地板。纯观测，判决只进日志。

    对照 GeoD：它的每 zoom / 每 50 瓦片复查是写死的 512 MiB 地板
    （`fs_util.rs:57-67`、`downloader.rs:540-541`）。那个形状报得出「盘只剩
    200 MB」，但报不出真正会出事的情况 ——「下一层要 40 GB，盘上还有 3 GB」。

    调用方传的 `remaining` 必须是**还没干的活**的估算（已下载的瓦片、已写完的
    产物都要扣掉），否则复查会把已经占用的空间又报一遍，跑到后半程数字必然
    虚高。

    `owner` 是本任务在 scheduler 上的凭据 owner（例如 `('dem', 12, 'tiling')`）。
    **必须传**：启动时本任务已经预留了自己那份 DISK_BYTES，不排除掉就会把它
    当成「别人占的」再扣一遍，于是报出的可用空间比真实值小一份自身预算
    （详见 `_reserved_by_others` 的 docstring 与那里记的实测数字）。
    不传时退化成「把自己也算进别人」，只在没有凭据的调用点（纯预览）才正确。
    """
    return _verdict(path, remaining, config_manager, what='the remaining work',
                    exclude_owner=owner)


#: `RunningRecheck` 两次真正复查之间的最小间隔（秒）。
#
# 为什么需要一个下限：批边界可以是亚秒级的（等高线 512 瓦片一批、地图 1000
# 瓦片一批、DEM 一颗颗粒），而一次复查是一次 statvfs 加一次调度器快照。每批
# 都查纯属浪费 —— 复查要抓的是「盘在这几十秒里被别人（另一个任务、另一个
# 进程、用户自己拷东西）吃掉了」这个尺度的变化，不是毫秒级抖动。
#
# 为什么是 10 秒而不是更长：复查的全部价值是日志里的现场数字 —— ENOSPC 发生
# 时，最后一条 disk_recheck 事件离写失败越近，数字越能说明问题。地图管线一批
# 1000 瓦片在快线路上就是几秒，10 秒保证写死之前日志里一定有一行最近的判决。
_RECHECK_MIN_INTERVAL_SECONDS = 10.0


class RunningRecheck:
    """运行中的周期性磁盘复查：把 `recheck_remaining` 接到管线的循环上。

    **纯观测，不拦截。** 它曾是「判死就收手」的闸门；2026-08 起拦截语义整体
    移除（见模块 docstring 头部），现在每次复查的全部产物是经 `on_verdict`
    写进任务日志的一行数字 —— ENOSPC 真的发生时，那行数字就是事后诊断的
    第一手现场。

    ## 为什么是一个对象，而不是在循环里直接调 `recheck_remaining`

    - **节奏要节流。** 见 `_RECHECK_MIN_INTERVAL_SECONDS`。
    - **绝不抛。** 调用点全在下载/渲染的热循环里。盘掉线、调度器读不出来、
      估算函数收到脏参数 —— 这些都不该有把任务打死的权力，否则「磁盘预算」
      这个次要检查本身就成了任务失败的新来源。出错时退回不查、照写，只留
      一条去重后的 warning。

    ## 用法

    循环的**批边界**上每批调一次 `poll()`，返回值忽略即可。

    Args:
        path: 要查哪块盘。必须是**真正写产物的那个目录**（工作区与产物不同盘
            时查错盘的复查比没有复查更糟 —— 它给出一个自信的「够用」）。
        remaining: `DiskEstimate`，或一个返回它的可调用对象。可调用形态是主
            路径：剩余工作量随循环推进而缩小，每次复查都要现算（传死值等于
            跑到后半程还在按整个任务的空间报数）。返回 None = 这一轮
            算不出来，跳过。
        owner: 本任务在 scheduler 上的凭据 owner 元组。**必须传**，理由见
            `recheck_remaining` 的 docstring（不传 = 自己那份预留被当成别人
            占的再扣一遍，报出的可用空间偏小一倍）。
        on_verdict: 每次真的复查之后都会收到判决（通过与否都给）。各管线
            用它把判决写进每任务日志。
    """

    def __init__(self, path, remaining, *, owner=None, config_manager=None,
                 min_interval: float = _RECHECK_MIN_INTERVAL_SECONDS,
                 on_verdict=None):
        self._path = path
        self._remaining = remaining
        self._owner = owner
        self._config_manager = config_manager
        self._min_interval = max(0.0, float(min_interval))
        self._on_verdict = on_verdict
        # -inf 而不是 monotonic()：第一个批边界就要查一次。任务是在启动判决
        # 之后才排队起来的，排队期间盘上发生了什么谁也不知道。
        self._checked_at = float('-inf')
        #: 最后一次判决（通过的也留着，日志/诊断要看数字）。
        self.verdict: Optional[BudgetVerdict] = None

    def poll(self, *, force: bool = False) -> Optional[BudgetVerdict]:
        """到点就复查一次。返回本次判决；未到点 / 算不出 / 出错都返回 None。

        返回值是给日志与测试看的，**不该**用来叫停循环 —— 拦截语义已移除，
        复查不通过也只是数字，不是命令。

        `force=True` 忽略节流间隔（测试用）。
        """
        now = time.monotonic()
        if not force and now - self._checked_at < self._min_interval:
            return None
        self._checked_at = now
        try:
            remaining = self._remaining() if callable(self._remaining) else self._remaining
            if remaining is None:
                return None
            verdict = recheck_remaining(self._path, remaining, self._config_manager,
                                       owner=self._owner)
        except Exception as e:
            # 见类 docstring 的「绝不抛」：退回不查、照写。
            _warn_once(('recheck', str(e)),
                       f'disk_budget: the in-flight recheck failed ({e}); this run '
                       f'continues without disk rechecks')
            return None
        self.verdict = verdict
        if self._on_verdict is not None:
            try:
                self._on_verdict(verdict)
            except Exception:
                # 日志回调炸了不该影响判决本身 —— 判决是主路径，记录是次要 sink。
                logger.debug('disk_budget: on_verdict callback failed (ignored)',
                             exc_info=True)
        return verdict


def work_dir_for(output_dir, prefix: str) -> Path:
    """选一个工作目录路径：系统临时目录与输出**不同盘**时，改到输出旁边。

    这就是「工作目录跟随输出盘」（GeoD #32）。跨盘时中间产物写在系统盘、收尾
    再整体搬到目标盘，而跨设备的搬运是**逐字节复制**（`os.replace` 直接
    EXDEV），等于每个字节写两遍加读一遍 —— 海量瓦片时这是任务耗时的主要成分，
    而且要求系统盘临时装得下整份产物（C 盘小、数据盘大是最常见的配置）。

    「旁边」= 输出目录的父级（同盘时），不是输出目录**内部**：任务输出目录会被
    /tiles/<id>/ 预览遍历、被产物拷贝阶段扫描，把几十 GB 中间件放进去会被当成
    产物。父级不同盘（输出目录本身就是挂载点）时退回输出目录内部。

    **只算路径，不建目录。** 创建与清理都留给调用方 —— 因为
    `src/services/task_cleanup.py:84-96` 明确要求：每一个新的 mkdtemp 型前缀都
    必须登记到那里的清扫表，否则它就是启动清扫的盲区（那条 ⚠️ 注释下面记着
    「5 个创建点只有 3 个被扫到」这笔账）。本函数返回的目录名以 `prefix` 开头、
    紧跟 `os.getpid()`，正是为了让那边的「按前缀匹配 + 按 pid 判归属」两条判据
    都能用上；接线时（wave B）必须同步登记新前缀。
    """
    out = Path(output_dir)
    tmp = Path(tempfile.gettempdir())
    if same_volume(tmp, out):
        base = tmp
    else:
        parent = out.parent
        base = parent if parent != out and same_volume(parent, out) else out
    return base / f'{prefix}{os.getpid()}_{uuid.uuid4().hex[:8]}'
