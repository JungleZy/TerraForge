"""独占缓存清理与容量治理（§4.6）。

## 核心结论：任务表本身就是清单

来自 [`docs/notes/cache-exclusive-cleanup-plan.md`](../../docs/notes/cache-exclusive-cleanup-plan.md)。
一个任务的瓦片集合是 `(region, zoom 区间, 源指纹)` 的**纯函数**，可以随时由
`contracts.region_tiles` 确定性重建；所以「这个任务独占哪些瓦片」= 它的枚举
减去其余所有存活任务枚举的并集，不需要第二张引用计数表，也不需要把别人的
集合物化出来 —— 逐 zoom 做矩形差集就够了。

`RegionSpec` 落地之后这条结论更强了一点：多边形任务的枚举不再退化成外接
矩形，所以差集算的是真实覆盖，而不是「我框了个大方块所以我引用了整片区域」。

## 为什么只在同一命名空间内做差集

缓存目录现在是 `cache/<style>-<fingerprint>/…`（见 `source_registry`）。
两个不同源的任务在磁盘上根本不共享文件，跨命名空间做差集只会让 A 的独占集
被 B「保护」掉 —— 少删。少删是安全方向，但这里连少删都不必：命名空间不同
就直接不参与。

## 与上游的对照

GeoDownloader 的容量治理有两个结构性缺陷，正好说明这里为什么这么设计：

- `prune()` **只有一个调用者** —— 用户在设置里重新保存缓存上限的那一刻
  （`commands.rs:4144-4146`）。既没有启动清理也没有定时器，缓存可以无限期
  超限。这里的 `enforce_cache_capacity` 同样只有一个调用者，但那个调用者是
  **启动清扫**（`task_cleanup.py:950`）：每次进程起来都收一次，用户不用点
  任何东西。为什么不顺手挂到「删除任务」上 —— 删除路径已经有精确工具
  （`clear_task_exclusive_cache`，只删这个任务独占的瓦片），再叠一次整库级
  的命名空间淘汰，会让「删掉任务 A」顺带清空一个与 A 毫无关系的命名空间。
  容量治理是全局策略，只能在全局时机执行；删除是局部操作，只该有局部后果。
- 它的淘汰粒度是**整个图源的 MBTiles 文件**，不是因为选择而是因为
  `tiles` 表没有访问时间列（`store.rs:40-46`），逐瓦片 LRU 在结构上不可能。
  这里同样按命名空间整体淘汰，但那是权衡的结果（逐瓦片时间戳 = 每次命中都
  要写盘），而且**淘汰前会保护活动任务**，它没有这一步。

GeoLibre 的 `deleteOfflineRegion()`（`offline-regions.ts:267`）有一条值得抄的
顺序保证：**先持久化清单、再淘汰**，写失败就中止，清单与缓存不会分叉。
这里对应的做法是 `enforce_cache_capacity` 先把完整计划算出来并落进日志与
返回值，再逐个删；任何一个删失败就停下并如实报告，不继续往下删 —— 半个
计划执行完却谁都不知道执行到哪，比不执行糟得多。

## 安全边界

- **宁可少删，不可多删。** 任何一步解析不出来（region 坏、快照坏、目录名
  不像命名空间）都按「还被引用」处理。误删的代价是缓存未命中重下；误留的
  代价只是占磁盘。两者不对称。
- **不碰 `.part.*`**：那是别的活进程正在写的原子临时文件。这里只负责「不
  碰」，不判归属 —— 「哪些残件已经没主了、可以删」是启动清扫的职责，判据
  是 `task_cleanup._part_owner_pid`，不在本模块另写一套。
- **不碰非命名空间目录**：`dem/`、`basemap/` 由各自的清理路径负责，判据是
  `SourceSnapshot.is_namespace`（结构判据）而不是黑名单（会漏）。
"""

from __future__ import annotations

import logging
import os
import shutil
from bisect import bisect_right
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from src.contracts.outcome import ACTIVE_STATE_VALUES
from src.contracts.region import RegionSpec
from src.contracts.region_tiles import iter_region_tile_spans
from src.contracts.source import SourceSnapshot
from src.core.config import Config
from src.core.database import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    'rect_subtract',
    'exclusive_tile_rects',
    'exclusive_dem_granules',
    'clear_task_exclusive_cache',
    'sweep_orphan_cache',
    'cache_usage_by_namespace',
    'enforce_cache_capacity',
    'surviving_task_rows',
]

Rect = Tuple[int, int, int, int]  # (x_min, x_max, y_min, y_max)，闭区间


# --------------------------------------------------------------------------
# 矩形差集
# --------------------------------------------------------------------------

def _intersect(a: Rect, b: Rect) -> Optional[Rect]:
    x0 = max(a[0], b[0])
    x1 = min(a[1], b[1])
    y0 = max(a[2], b[2])
    y1 = min(a[3], b[3])
    if x0 > x1 or y0 > y1:
        return None
    return (x0, x1, y0, y1)


def _subtract_one(rect: Rect, hole: Rect) -> List[Rect]:
    """`rect \\ hole` → 至多四块不相交矩形（上下左右切）。"""
    inter = _intersect(rect, hole)
    if inter is None:
        return [rect]
    x0, x1, y0, y1 = rect
    hx0, hx1, hy0, hy1 = inter
    out: List[Rect] = []
    if hy0 > y0:                      # 上带
        out.append((x0, x1, y0, hy0 - 1))
    if hy1 < y1:                      # 下带
        out.append((x0, x1, hy1 + 1, y1))
    if hx0 > x0:                      # 左块（只在被挖行内）
        out.append((x0, hx0 - 1, hy0, hy1))
    if hx1 < x1:                      # 右块
        out.append((hx1 + 1, x1, hy0, hy1))
    return out


def rect_subtract(rect: Rect, others: Iterable[Rect]) -> List[Rect]:
    """`rect` 减去 `others` 的并集，返回互不相交的矩形列表。

    逐个 hole 顺序切割：每一轮把当前结果集里的每块都切一遍。四块切法保证
    输出永远互不相交（上下带占满整个 x 跨度，左右块只占被挖的那几行），
    所以「面积之和 = 原面积 − 交集面积」这条不变式成立，可以直接拿来测。

    最坏复杂度是 O(块数 × hole 数)，块数随 hole 数增长。实际用途里 hole 是
    「其余存活任务在这一层的矩形」，量级是任务数而不是瓦片数，够用。
    """
    result = [rect]
    for hole in others:
        if not result:
            break
        nxt: List[Rect] = []
        for piece in result:
            nxt.extend(_subtract_one(piece, hole))
        result = nxt
    return result


# --------------------------------------------------------------------------
# 任务行 → 每层矩形
# --------------------------------------------------------------------------

def _row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


class _LazyConfigManager:
    """按需构造、一次删除只建一个的 `ConfigManager` 代理。

    为什么需要它：`_row_namespace` 对**每一个** other_row 调一次，而没有
    `source_snapshot` 的存量行会落到 `snapshot_for_style`，那里在
    `config_manager=None` 时自己 `ConfigManager()` —— 每次都是一条新的 sqlite
    连接。一次删除的 other_rows 是「历史任务全表」，几百行存量行就是几百次
    建连接 + 建表检查，纯浪费。

    又不能无条件先建一个：绝大多数任务行都带快照，`snapshot_for_task_row`
    在第一行就 return 了，根本不读配置。所以这里做成惰性代理 —— 只有真的
    有人来取属性（唯一的取法是 `.get('tile_servers', …)`）时才建。
    """

    __slots__ = ('_real',)

    def __init__(self):
        self._real = None

    def __getattr__(self, name):
        # `_real` 在 __slots__ 里且 __init__ 已赋值，永远不会走到这儿递归。
        if self._real is None:
            from src.services.config_manager import ConfigManager
            self._real = ConfigManager()
        return getattr(self._real, name)


def _row_namespace(row, config_manager=None) -> Optional[str]:
    """任务行 → 缓存命名空间。算不出来返回 None（调用方按「保守跳过」处理）。

    `config_manager` 由调用方传一个共享实例进来（见 `_LazyConfigManager`）：
    这个函数在一次删除里会被调几百次，每次现建连接是纯开销。
    """
    try:
        from src.services.source_registry import snapshot_for_task_row
        return snapshot_for_task_row(row, config_manager).cache_namespace
    except Exception as e:
        logger.warning(f'任务 {_row_get(row, "id")} 的缓存命名空间算不出来（{e!r}）')
        return None


def _row_zoom_range(row) -> Optional[Tuple[int, int]]:
    try:
        zmin = int(_row_get(row, 'zoom_min'))
        zmax = int(_row_get(row, 'zoom_max'))
    except (TypeError, ValueError):
        return None
    if zmin > zmax:
        return None
    return zmin, zmax


def _row_rects_by_zoom(row) -> Optional[Dict[int, List[Rect]]]:
    """任务行 → `{zoom: [矩形…]}`。任何一步失败返回 None。

    多边形任务在每一行上可能有多段 x 区间，所以这里存的是**行级 span 合成的
    矩形列表**而不是一个大 bbox：把多边形还原成外接矩形会让它「保护」自己
    根本没下载过的瓦片，导致别的任务永远删不掉那片缓存。
    """
    region = RegionSpec.from_row(row)
    zooms = _row_zoom_range(row)
    if region is None or zooms is None:
        return None
    out: Dict[int, List[Rect]] = {}
    try:
        for zoom in range(zooms[0], zooms[1] + 1):
            rects = [(x0, x1, y, y) for y, x0, x1 in iter_region_tile_spans(region, zoom)]
            if rects:
                out[zoom] = rects
    except ValueError as e:
        logger.warning(f'任务 {_row_get(row, "id")} 的瓦片枚举失败（{e!r}）')
        return None
    return out


def exclusive_tile_rects(task_row, other_rows) -> Iterator[Tuple[int, int, int, int, int]]:
    """`(zoom, x_min, x_max, y_min, y_max)`：只被本任务引用的瓦片矩形。

    保守规则（写在模块 docstring 里的那条）在这里体现为两个 return：
    本任务自己解析不了 → 什么都不产出（一块都不删）；某个**其他**任务解析
    不了 → 把它整片外接矩形当作被引用（多留，不多删）。
    """
    mine = _row_rects_by_zoom(task_row)
    if not mine:
        return
    # 整个差集过程共享一个（惰性的）ConfigManager，别让每行存量任务各开一条
    # sqlite 连接。
    cm = _LazyConfigManager()
    namespace = _row_namespace(task_row, cm)
    if namespace is None:
        return

    holes_by_zoom: Dict[int, List[Rect]] = {}
    for other in other_rows:
        if _row_get(other, 'id') == _row_get(task_row, 'id'):
            continue
        if _row_namespace(other, cm) != namespace:
            # 不同源 = 不同目录，磁盘上根本不重叠。
            continue
        rects = _row_rects_by_zoom(other)
        if rects is None:
            # 解析不了的存活任务：按「它引用了自己四至内的一切」处理。
            fallback = _conservative_rects(other)
            if fallback:
                for zoom, rect in fallback.items():
                    holes_by_zoom.setdefault(zoom, []).append(rect)
            continue
        for zoom, rect_list in rects.items():
            holes_by_zoom.setdefault(zoom, []).extend(rect_list)

    for zoom in sorted(mine):
        holes = holes_by_zoom.get(zoom, ())
        for rect in mine[zoom]:
            for piece in rect_subtract(rect, holes):
                yield (zoom, piece[0], piece[1], piece[2], piece[3])


def _conservative_rects(row) -> Dict[int, Rect]:
    """解析失败时的兜底：按四至列取整片外接矩形，尽量多保护。"""
    from src.contracts.region_tiles import bbox_tile_range
    zooms = _row_zoom_range(row)
    if zooms is None:
        return {}
    try:
        north = float(_row_get(row, 'north'))
        south = float(_row_get(row, 'south'))
        east = float(_row_get(row, 'east'))
        west = float(_row_get(row, 'west'))
    except (TypeError, ValueError):
        return {}
    out: Dict[int, Rect] = {}
    for zoom in range(zooms[0], zooms[1] + 1):
        try:
            out[zoom] = bbox_tile_range(north, south, east, west, zoom)
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# DEM 颗粒
# --------------------------------------------------------------------------

def exclusive_dem_granules(dataset: str, bbox, other_rows) -> Set[str]:
    """只被本任务引用的 DEM 颗粒文件名。

    `bbox` 是 `(north, south, east, west)`。枚举一律走
    `src.services.dem_granules.tiles_for_bbox` —— 那是 DEM 侧唯一的
    「经纬度 → 颗粒」实现，在这里另写一份就是设计稿点名的那个漂移风险。

    ASTER 的 `_num.tif` 伴生文件永远算「仍被引用」：它与主文件同名不同后缀，
    判定它是否独占要额外一层映射，而漏删一个伴生文件的代价（占几 MB）远小于
    误删（下次用到 ASTER 覆盖判定时静默缺数据）。
    """
    from src.services.dem_granules import tiles_for_bbox

    def _granules(ds, bb) -> Set[str]:
        north, south, east, west = bb
        names: Set[str] = set()
        try:
            tiles = tiles_for_bbox(north, south, east, west)
        except ValueError:
            return names
        from src.services import dem_granules as dg
        for tile in tiles:
            try:
                if ds == 'COP-DEM-GLO-30':
                    entries = dg.copernicus_glo30_granules_for_tile(tile)
                else:
                    entries = dg.astgtm_v3_granules_for_tile(tile, False)
            except Exception:
                continue
            for entry in entries:
                names.add(Path(str(entry)).name)
        return names

    mine = _granules(dataset, bbox)
    if not mine:
        return set()

    for other in other_rows:
        other_ds = _row_get(other, 'dataset') or dataset
        try:
            other_bbox = (float(_row_get(other, 'north')), float(_row_get(other, 'south')),
                          float(_row_get(other, 'east')), float(_row_get(other, 'west')))
        except (TypeError, ValueError):
            # 解析不了 → 无法证明它不引用任何颗粒 → 整个独占集作废（保守）。
            return set()
        mine -= _granules(other_ds, other_bbox)
        if not mine:
            break

    # 伴生文件一律视为仍被引用。
    return {n for n in mine if not n.endswith('_num.tif')}


# --------------------------------------------------------------------------
# 实际删除
# --------------------------------------------------------------------------

def _is_part_file(name: str) -> bool:
    return '.part.' in name


def _merge_x_intervals(intervals: Iterable[Tuple[int, int]]
                       ) -> Tuple[List[int], List[int]]:
    """一堆可能重叠的 `(x0, x1)` → 两条平行数组（起点升序、互不相交）。

    合并相邻（`x1 + 1 == 下一个 x0`）也一起做：矩形差集切出来的碎片本来就多，
    不合并的话每层的区间数会随任务数线性膨胀，二分的常数白涨。
    """
    starts: List[int] = []
    ends: List[int] = []
    for x0, x1 in sorted(intervals):
        if starts and x0 <= ends[-1] + 1:
            if x1 > ends[-1]:
                ends[-1] = x1
            continue
        starts.append(x0)
        ends.append(x1)
    return starts, ends


class _ZoomRectIndex:
    """某一层的独占矩形集合 → `(x, y)` 命中判定，单次查询 O(log n)。

    结构：按 y 做一遍扫描线，把矩形切成互不重叠的**水平条带**，每条带内的
    x 区间归并成有序不相交列表。查询先在条带起点上二分定位到唯一可能命中的
    条带，再在该条带的 x 起点上二分。

    为什么不能对矩形列表做线性扫描：`exclusive_tile_rects` 产出的矩形数量与
    区域的瓦片**行数**同量级（每行一段，多边形任务每行还可能被 hole 切成多
    段），一个 z18 的省级任务轻松上万。磁盘侧遍历是「每个文件查一次」，线性
    扫描会把总代价变成 文件数 × 矩形数 —— 那就等于把刚从坐标枚举里省下来的
    时间原样还回去。
    """

    __slots__ = ('_y_starts', '_y_ends', '_x_starts', '_x_ends', 'x_min', 'x_max')

    def __init__(self, rects: Iterable[Rect]):
        opens: Dict[int, List[Tuple[int, int]]] = {}
        closes: Dict[int, List[Tuple[int, int]]] = {}
        x_min: Optional[int] = None
        x_max: Optional[int] = None
        for x0, x1, y0, y1 in rects:
            if x0 > x1 or y0 > y1:
                continue
            opens.setdefault(y0, []).append((x0, x1))
            closes.setdefault(y1 + 1, []).append((x0, x1))
            if x_min is None or x0 < x_min:
                x_min = x0
            if x_max is None or x1 > x_max:
                x_max = x1
        # 空索引留成 [0, -1]：任何 x 都落在区间外，目录级粗筛直接全跳过。
        self.x_min = 0 if x_min is None else x_min
        self.x_max = -1 if x_max is None else x_max

        self._y_starts: List[int] = []
        self._y_ends: List[int] = []
        self._x_starts: List[List[int]] = []
        self._x_ends: List[List[int]] = []
        bounds = sorted(set(opens) | set(closes))
        # 同一个 (x0, x1) 可能来自多个矩形，所以用计数而不是集合：一个副本
        # 关掉不代表这段 x 在这条带上就没人覆盖了。
        active: Dict[Tuple[int, int], int] = {}
        for i in range(len(bounds) - 1):
            edge = bounds[i]
            for iv in opens.get(edge, ()):
                active[iv] = active.get(iv, 0) + 1
            for iv in closes.get(edge, ()):
                left = active.get(iv, 0) - 1
                if left > 0:
                    active[iv] = left
                else:
                    active.pop(iv, None)
            if not active:
                continue
            starts, ends = _merge_x_intervals(active)
            self._y_starts.append(edge)
            self._y_ends.append(bounds[i + 1] - 1)
            self._x_starts.append(starts)
            self._x_ends.append(ends)

    def contains(self, x: int, y: int) -> bool:
        i = bisect_right(self._y_starts, y) - 1
        if i < 0 or y > self._y_ends[i]:
            return False
        starts = self._x_starts[i]
        j = bisect_right(starts, x) - 1
        return j >= 0 and x <= self._x_ends[i][j]


def clear_task_exclusive_cache(task_row, other_rows) -> Dict[str, int]:
    """删掉只被 `task_row` 引用的缓存瓦片。返回 `{'removed_bytes','removed_files'}`。

    调用顺序（设计稿 :39-47 的安全表）：**先快照枚举、再删任务行、最后清文件**。
    本函数负责最后一步。`other_rows` 是删行**之前**拍的全表快照，里面**包含**
    被删任务自己那一行 —— 排除它是 `exclusive_tile_rects` 按 id 做的
    （见 `task_deletion._snapshot_cache_scope` 的同款说明）。不要在这里再过滤
    一次：多一处 id 口径就多一处静默失效的机会。

    ## 遍历方向：走磁盘上真实存在的文件，不走矩形里的坐标

    早先的写法是「枚举独占矩形里的每个坐标 → 拼路径 → stat」。它的代价是
    **O(区域瓦片数)**，与盘上有没有东西无关：实测 15.25 μs/坐标，一个 z0-18、
    2°×1.5° 的区域是 2,783,278 块 = 约 42 秒，省级或 z19-20 的选区要跑几十
    分钟。而这段代码跑在 Flask 的 DELETE 处理器里（快路径，
    `task_deletion.py` 的 `_clear_exclusive_cache`），用户对着转圈等几分钟，
    然后再点一次删除。

    现在反过来：遍历 `cache/<namespace>/{z}/{x}/` 下**确实存在**的文件，逐个
    拿去问矩形索引。代价变成 **O(盘上文件数)**，而盘上文件数的上界是「真的
    下过多少块」—— 一个下了 0 块就被删掉的任务从几百万次 stat 变成零工作量，
    一个下了一半的任务也只按那一半算。两者删掉的文件集合完全一致：老写法只
    可能碰 `{y}.png`，所以这里也只认「整数 + `.png`」这一种文件名。

    副作用（而且是必需的）：`_is_part_file` 从此是**活**判据。老写法拼出来的
    路径永远是 `{y}.png`，名字里不可能出现 `.part.`，那行 if 是死代码，模块
    docstring 里「不碰 `.part.*`」那条承诺在这条路径上从来没被执行过。反转
    之后我们直接读目录，`123.png.part.<pid>.<id>`（`download_engine` 正在写
    的原子临时件）会真的出现在结果里 —— 那是**另一个活着的下载线程**的文件，
    删掉它等于让人家的 `os.replace` 炸在半路。
    """
    rects_by_zoom: Dict[int, List[Rect]] = {}
    for zoom, x0, x1, y0, y1 in exclusive_tile_rects(task_row, other_rows):
        rects_by_zoom.setdefault(zoom, []).append((x0, x1, y0, y1))
    if not rects_by_zoom:
        return {'removed_bytes': 0, 'removed_files': 0}

    try:
        from src.services.source_registry import snapshot_for_task_row
        namespace = snapshot_for_task_row(task_row).cache_namespace
    except Exception as e:
        logger.warning(f'独占缓存清理跳过：快照不可用（{e!r}）')
        return {'removed_bytes': 0, 'removed_files': 0}

    # 路径规则与 Tile.cache_path 同源（`cache/<ns>/{z}/{x}/{y}.png`）；这里
    # 不 import Tile 只是因为反转之后根本不需要构造瓦片对象。
    base = Path(Config.CACHE_DIR) / str(namespace)
    removed_bytes = 0
    removed_files = 0
    touched_dirs: Set[Path] = set()
    for zoom, rects in rects_by_zoom.items():
        index = _ZoomRectIndex(rects)
        try:
            x_entries = list(os.scandir(base / str(zoom)))
        except OSError:
            continue  # 这一层压根没下过 —— 零工作量
        for x_entry in x_entries:
            if not x_entry.is_dir():
                continue
            try:
                x = int(x_entry.name)
            except ValueError:
                continue
            # 目录级粗筛：整个 x 列不在独占范围内就连 scandir 都不做。
            if x < index.x_min or x > index.x_max:
                continue
            try:
                tile_entries = list(os.scandir(x_entry.path))
            except OSError:
                continue
            hit = False
            for entry in tile_entries:
                name = entry.name
                if _is_part_file(name):
                    continue
                if not name.endswith('.png'):
                    continue
                try:
                    y = int(name[:-4])
                except ValueError:
                    continue
                if not index.contains(x, y):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                try:
                    os.unlink(entry.path)
                except OSError:
                    continue
                removed_bytes += size
                removed_files += 1
                hit = True
            if hit:
                # 只登记「真的删掉过东西」的目录：本来就空的 x/ 目录不是我们
                # 造成的，收掉它会让这次删除的副作用超出「删我自己的瓦片」。
                touched_dirs.add(Path(x_entry.path))

    _prune_empty_dirs(touched_dirs)
    if removed_files:
        logger.info(f'任务 {_row_get(task_row, "id")} 独占缓存已清理：'
                    f'{removed_files} 块 / {removed_bytes / 1024 / 1024:.1f} MB')
    return {'removed_bytes': removed_bytes, 'removed_files': removed_files}


def _prune_empty_dirs(dirs: Iterable[Path]) -> None:
    """删空的 `x/` 目录，再删空的 `z/` 目录。命名空间目录本身留着。

    只往上走两级：再往上就是命名空间目录，它的存在与否是缓存管理页的分类
    来源，删掉会让「卫星影像 0 B」这一行凭空消失，用户以为缓存被整个清了。
    """
    cache_root = Path(Config.CACHE_DIR)
    for d in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        current = d
        for _ in range(2):
            try:
                if current.parent == cache_root or current == cache_root:
                    break
                current.rmdir()
            except OSError:
                break
            current = current.parent


# --------------------------------------------------------------------------
# 命名空间级治理
# --------------------------------------------------------------------------

def surviving_task_rows(conn=None) -> List:
    """全部存活的地图任务行（含 region_spec / style / 四至 / zoom 区间）。

    只查 `tasks` 表：命名空间缓存里放的就是地图瓦片，另外三条管线的产物不在
    这里（DEM 走 `cache/dem`，等高线与地形不进缓存）。
    """
    owns = conn is None
    conn = conn or get_connection()
    try:
        return conn.execute('SELECT * FROM tasks').fetchall()
    except Exception as e:
        logger.warning(f'存活任务查询失败（{e!r}）')
        return []
    finally:
        if owns:
            conn.close()


def _namespace_dirs() -> List[Path]:
    root = Path(Config.CACHE_DIR)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return [e for e in entries if e.is_dir() and SourceSnapshot.is_namespace(e.name)]


def cache_usage_by_namespace() -> Dict[str, dict]:
    """`{namespace: {'bytes','files','style','tasks':[task_id…],'active':bool}}`。"""
    from src.services.task_cleanup import _sum_dir_bytes

    rows = surviving_task_rows()
    by_ns: Dict[str, List] = {}
    active_ns: Set[str] = set()
    cm = _LazyConfigManager()  # 全表逐行算命名空间，别每行开一条 sqlite 连接
    for row in rows:
        ns = _row_namespace(row, cm)
        if ns is None:
            continue
        by_ns.setdefault(ns, []).append(_row_get(row, 'id'))
        if (_row_get(row, 'status') or '') in ACTIVE_STATE_VALUES:
            active_ns.add(ns)

    out: Dict[str, dict] = {}
    for d in _namespace_dirs():
        size, count = _sum_dir_bytes(d)
        out[d.name] = {
            'bytes': size,
            'files': count,
            'style': SourceSnapshot.style_of_namespace(d.name),
            'tasks': sorted(i for i in by_ns.get(d.name, []) if i is not None),
            'active': d.name in active_ns,
        }
    return out


def sweep_orphan_cache(*, protected: Sequence[str] = ()) -> Dict[str, object]:
    """删掉没有任何存活任务引用的命名空间目录。

    「没有任何任务引用」而不是「没有活动任务引用」：已完成任务的缓存仍然
    有价值（同区域再下一次就是全命中），只有连任务行都不存在了的命名空间
    才是真的孤儿 —— 那通常来自「换过源、旧任务已被删干净」。
    """
    protected_set = set(protected)
    usage = cache_usage_by_namespace()
    removed: List[str] = []
    removed_bytes = 0
    for ns, info in usage.items():
        if info['tasks'] or ns in protected_set:
            continue
        target = Path(Config.CACHE_DIR) / ns
        try:
            shutil.rmtree(target)
        except OSError as e:
            logger.warning(f'孤儿缓存命名空间 {ns} 删除失败（{e!r}）')
            continue
        removed.append(ns)
        removed_bytes += info['bytes']
    if removed:
        logger.info(f'孤儿缓存命名空间已清理：{removed}（{removed_bytes / 1024 / 1024:.1f} MB）')
    return {'removed': removed, 'removed_bytes': removed_bytes}


def enforce_cache_capacity(config_manager=None, *, protected: Sequence[str] = ()
                           ) -> Dict[str, object]:
    """把缓存总量压到 `cache_max_mb` 以内。`0` = 不限（与 GeoD 的 0 语义一致）。

    执行顺序刻意分成「先算完整计划、再逐个删」（GeoLibre `offline-regions.ts:267`
    的「先持久化清单再淘汰」在本项目里的对应形态）：计划先落日志与返回值，
    删到一半失败就**停下并如实报告**，不继续往下删。半个计划执行完却谁都不
    知道执行到哪，比不执行糟得多。

    淘汰顺序：先淘汰没有任务引用的（等同 `sweep_orphan_cache` 的对象），
    再按体积从大到小淘汰只被终态任务引用的。**永不淘汰**被活动任务引用的
    或调用方点名 `protected` 的 —— 那会让一个正在跑的任务当场缺文件，而它的
    完成态判据正是「缓存文件存在」。
    """
    if config_manager is None:
        from src.services.config_manager import ConfigManager
        config_manager = ConfigManager()
    try:
        limit_mb = int(config_manager.get('cache_max_mb', '0') or 0)
    except (TypeError, ValueError):
        logger.warning('cache_max_mb 取值非法，按不限处理')
        limit_mb = 0

    usage = cache_usage_by_namespace()
    total = sum(info['bytes'] for info in usage.values())

    if limit_mb <= 0:
        return {'evicted': [], 'removed_bytes': 0, 'skipped': [],
                'total_bytes': total,
                'reason': 'cache_max_mb=0（不限），未做任何淘汰'}

    limit = limit_mb * 1024 * 1024
    if total <= limit:
        return {'evicted': [], 'removed_bytes': 0, 'skipped': [],
                'total_bytes': total,
                'reason': f'缓存 {total / 1024 / 1024:.1f} MB 未超上限 {limit_mb} MB'}

    protected_set = set(protected)
    evictable = []
    skipped = []
    for ns, info in usage.items():
        if info['active'] or ns in protected_set:
            skipped.append(ns)
            continue
        # 排序键：无任务引用的排最前（0），其余按体积降序。
        evictable.append((0 if not info['tasks'] else 1, -info['bytes'], ns, info))
    evictable.sort()

    plan = []
    freed = 0
    for _prio, _neg, ns, info in evictable:
        if total - freed <= limit:
            break
        plan.append(ns)
        freed += info['bytes']

    if not plan:
        return {'evicted': [], 'removed_bytes': 0, 'skipped': skipped,
                'total_bytes': total,
                'reason': (f'缓存 {total / 1024 / 1024:.1f} MB 超过上限 {limit_mb} MB，'
                           f'但所有命名空间都被活动任务引用，未淘汰')}

    logger.info(f'缓存容量治理计划：淘汰 {plan}，预计释放 '
                f'{freed / 1024 / 1024:.1f} MB（当前 {total / 1024 / 1024:.1f} MB / '
                f'上限 {limit_mb} MB）')

    evicted: List[str] = []
    removed_bytes = 0
    for ns in plan:
        target = Path(Config.CACHE_DIR) / ns
        try:
            shutil.rmtree(target)
        except OSError as e:
            logger.error(f'缓存淘汰在 {ns} 处失败（{e!r}），计划中止；'
                         f'已淘汰 {evicted}')
            return {'evicted': evicted, 'removed_bytes': removed_bytes,
                    'skipped': skipped, 'total_bytes': total,
                    'reason': f'淘汰 {ns} 失败并中止：{e!r}'}
        evicted.append(ns)
        removed_bytes += usage[ns]['bytes']

    return {'evicted': evicted, 'removed_bytes': removed_bytes, 'skipped': skipped,
            'total_bytes': total - removed_bytes,
            'reason': f'淘汰 {len(evicted)} 个命名空间，释放 '
                      f'{removed_bytes / 1024 / 1024:.1f} MB'}
