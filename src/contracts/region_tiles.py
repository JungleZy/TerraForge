"""区域 → Web Mercator 瓦片枚举。**全项目唯一的经纬度 → 瓦片实现。**

`src/services/download_engine.py` 的 `lat_lon_to_tile` / `_tile_ranges` 现在
委托到这里。`docs/notes/cache-exclusive-cleanup-plan.md:123-125` 把「第二套
lat/lon → tile 实现」列为本项目最危险的漂移源：缓存独占集算错一格，删除就
会误伤别的任务。所以这一层刻意放在 contracts（最底层），让服务层只能委托、
不能另写。

## 矩形与多边形

矩形走 `bbox_tile_range`，与改造前逐位一致（同一个 `lat_lon_to_tile`、同样
的角点取法、同样的排序纠正）—— 存量任务恢复时枚举出的瓦片集合不会变。

多边形走 `iter_region_tile_spans` 的扫描线栅格化。它解决的是
GeoDownloader 「按 bbox 计费、按 polygon 出图」的错位：那边估算只看外接
矩形（`commands.rs:313-354`），下载的是整个 bbox（`tile.rs:109-120`），只有
最终栅格按多边形掩膜（`commands.rs:1362`）。这里三者是同一份枚举，**计数、
下载、掩膜天然一致**。

## 栅格化为什么这么写

一块瓦片只要与多边形有交集就必须入选（漏一块 = 成品缺一块）。取并集：

1. **边穿过的格子**：把每条边裁进当前行的 [y, y+1] 带内，取裁剪段的
   x 极值 —— 直线段在一条水平带内触及的 x 格子恰好是
   `floor(min_x) .. floor(max_x)`，无近似。
2. **行中线的奇偶扫描**：在 `y + 0.5` 处求所有环与水平线的交点，排序后
   两两配对成区间。

并集是精确的：若一块瓦片与多边形有交集但没有任何边穿过它，那它必然整块
落在多边形内部，其行中线一定被区间覆盖。洞环参与同一套奇偶计数，因此
**洞是真的挖掉的**，不是「后端有能力、前端丢信息」（GeoD `region-selector.tsx:42`
只取外环的那个 bug）。

扫描线用活动边表推进，复杂度 O(边数 + 命中格子数)，不是 O(行数 × 边数)。

## 反经线

跨界区域的顶点经度被 `RegionSpec.from_polygons` 归一到 `> 180`，这里在
**未回绕**的瓦片空间里栅格化，最后统一 `x % n`。跨 n 边界的区间会被切成
两段再输出 —— 消费方拿到的永远是 `0 <= x < n` 的合法瓦片号。
"""

from __future__ import annotations

import math
from typing import Dict, Iterator, List, Sequence, Tuple

__all__ = [
    'MIN_ZOOM',
    'MAX_ZOOM',
    'WEB_MERCATOR_MAX_LAT',
    'bbox_tile_range',
    'count_region_tiles',
    'iter_region_tile_spans',
    'lat_lon_to_tile',
    'tile_lon_lat_bounds',
    'validate_zoom_range',
]

MIN_ZOOM = 0
MAX_ZOOM = 21
WEB_MERCATOR_MAX_LAT = 85.0511


def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """经纬度 → 瓦片 (x, y)。Web Mercator，纬度钳到 ±85.0511。

    这是改造前 `DownloadEngine.lat_lon_to_tile` 的原样搬迁 —— 逐字保持
    `int()` 截断、钳位顺序与 zoom 校验，因为存量缓存与存量任务的瓦片集合
    都由它决定，改一个字就是静默的缓存全失效。
    """
    if not MIN_ZOOM <= zoom <= MAX_ZOOM:
        raise ValueError(f"Zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom}")

    lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tile_lon_lat_bounds(zoom: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """瓦片 → `(west, south, east, north)`。MBTiles metadata 的 bounds 用它。"""
    n = 2.0 ** zoom
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return west, south, east, north


def _frac_x(lon: float, n: int) -> float:
    return (lon + 180.0) / 360.0 * n


def _frac_y(lat: float, n: int) -> float:
    lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    lat_rad = math.radians(lat)
    return (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n


def validate_zoom_range(zoom_min: int, zoom_max: int) -> Tuple[int, int]:
    """层级区间校验。消息与 `download_engine._tile_ranges` 原文一致 ——
    路由层把 ValueError 原文当 400 body 返回，改文案就是改 API。"""
    if not MIN_ZOOM <= zoom_min <= MAX_ZOOM:
        raise ValueError(
            f"Minimum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_min}")
    if not MIN_ZOOM <= zoom_max <= MAX_ZOOM:
        raise ValueError(
            f"Maximum zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom_max}")
    if zoom_min > zoom_max:
        raise ValueError(
            f"Minimum zoom ({zoom_min}) must be less than or equal to maximum zoom ({zoom_max})")
    return int(zoom_min), int(zoom_max)


def bbox_tile_range(north: float, south: float, east: float, west: float,
                    zoom: int) -> Tuple[int, int, int, int]:
    """矩形 → `(x_min, x_max, y_min, y_max)`（闭区间）。

    与改造前 `_tile_ranges` 的单层逻辑逐位一致，含「角点算完再纠正次序」
    那一步 —— 它看着多余，但纬度在南半球时 y 会反过来。
    """
    x_min, y_max = lat_lon_to_tile(south, west, zoom)
    x_max, y_min = lat_lon_to_tile(north, east, zoom)
    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min
    return x_min, x_max, y_min, y_max


def _merge_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + 1:
            if hi > last_hi:
                merged[-1] = (last_lo, hi)
        else:
            merged.append((lo, hi))
    return merged


def _wrap_spans(spans: Sequence[Tuple[int, int]], n: int) -> List[Tuple[int, int]]:
    """未回绕的 x 区间 → `0 <= x < n` 的区间列表（跨 n 边界的切成两段）。"""
    out: List[Tuple[int, int]] = []
    for lo, hi in spans:
        if hi - lo + 1 >= n:
            out.append((0, n - 1))
            continue
        wlo = lo % n
        whi = wlo + (hi - lo)
        if whi < n:
            out.append((wlo, whi))
        else:
            out.append((wlo, n - 1))
            out.append((0, whi - n))
    return _merge_spans(out)


class _Edge:
    """活动边表的一条边（瓦片空间，未回绕）。"""

    __slots__ = ('y0', 'y1', 'x0', 'x1', 'row_lo', 'row_hi')

    def __init__(self, xa: float, ya: float, xb: float, yb: float):
        if ya <= yb:
            self.x0, self.y0, self.x1, self.y1 = xa, ya, xb, yb
        else:
            self.x0, self.y0, self.x1, self.y1 = xb, yb, xa, ya
        self.row_lo = math.floor(self.y0)
        # 端点正好落在整行边界时不要多算一行：y1=3.0 只触及第 2 行。
        self.row_hi = math.ceil(self.y1) - 1 if float(self.y1).is_integer() else math.floor(self.y1)
        if self.row_hi < self.row_lo:
            self.row_hi = self.row_lo

    def x_at(self, y: float) -> float:
        dy = self.y1 - self.y0
        if abs(dy) < 1e-15:
            return self.x0
        return self.x0 + (y - self.y0) * (self.x1 - self.x0) / dy

    def x_extent_in_row(self, row: int) -> Tuple[float, float]:
        """边裁进 [row, row+1] 带内之后的 x 极值。

        ⚠️ **水平边必须单独处理。** `x_at` 在 dy≈0 时只能返回 `x0`（斜率无定义），
        于是一条从 x=1.2 横到 x=5.8 的水平边被算成 `(1.2, 1.2)` —— 规则 1
        「边穿过的格子」只标了一格，而它实际横跨五格。

        为什么这会漏瓦片而不只是少标几格：规则 2 的奇偶扫描只在 `y+0.5` 这条
        行中线上取交点，用的是半开区间 `y0 <= yc < y1`；当多边形在这一行里整个
        落在中线的**同一侧**（矩形底边贴着行中线、薄横条、L 形的水平段……），
        这一行一个交点都没有，覆盖完全依赖规则 1。两条规则同时失灵，那一行就
        只剩两端的竖边被标到，中间的格子静默消失。

        实测代价：轴对齐矩形（多一个中点顶点、绕开 is_rectangle 快路径）120 例
        里 77 例漏块，梳状多边形 160 例里 112 例漏块。漏掉的瓦片不进计数、不进
        下载、不进拼接，也不进独占缓存的保护集 —— 成品上一个洞，却没有缺块行、
        没有日志，而且那块缓存还会被别的任务当成孤儿删掉。

        修法就是这里的 dy≈0 分支：水平边在它所在的那一行里，横跨的 x 区间就是
        两个端点之间，与 row 无关。
        """
        if abs(self.y1 - self.y0) < 1e-15:
            return (self.x0, self.x1) if self.x0 <= self.x1 else (self.x1, self.x0)
        lo_y = max(self.y0, float(row))
        hi_y = min(self.y1, float(row) + 1.0)
        if hi_y < lo_y:
            lo_y = hi_y = max(self.y0, min(self.y1, float(row)))
        xa = self.x_at(lo_y)
        xb = self.x_at(hi_y)
        return (xa, xb) if xa <= xb else (xb, xa)


def _region_edges(region, n: int) -> List[_Edge]:
    edges: List[_Edge] = []
    for poly in region.geometry:
        for ring in poly:
            pts = [(_frac_x(lon, n), _frac_y(lat, n)) for lon, lat in ring]
            for i in range(len(pts) - 1):
                xa, ya = pts[i]
                xb, yb = pts[i + 1]
                if abs(ya - yb) < 1e-15 and abs(xa - xb) < 1e-15:
                    continue
                edges.append(_Edge(xa, ya, xb, yb))
    return edges


def iter_region_tile_spans(region, zoom: int) -> Iterator[Tuple[int, int, int]]:
    """区域 + 层级 → `(y, x_start, x_end)`（x 闭区间），y 升序、区间升序不重叠。

    矩形走快路径，与 `bbox_tile_range` 完全一致。
    """
    if not MIN_ZOOM <= zoom <= MAX_ZOOM:
        raise ValueError(f"Zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom}")
    n = 2 ** zoom

    if region.is_rectangle:
        # 跨反经线时 antimeridian_parts 给两段，两段各自算 x 范围后**按行合并**
        # 再产出。不能简单地「一段一段 yield」，那样有两个毛病：
        #   · 违反本函数声明的 `y 升序` 契约（也是 download_engine.iter_region_tiles
        #     的 `(zoom, y, x) 升序` 契约）—— 行号会先升一遍再从头来一遍；
        #   · z=0 时两段都钳到 x=0（整个世界只有一块瓦片），同一块瓦片被产出两次，
        #     于是它被下载两遍、total_tiles 也多算一块。
        # _merge_spans 顺带把两段在低层级重叠/相邻的情况合并掉。
        rows: Dict[int, List[Tuple[int, int]]] = {}
        for (north, south, east, west) in region.antimeridian_parts:
            x_min, x_max, y_min, y_max = bbox_tile_range(north, south, east, west, zoom)
            for y in range(y_min, y_max + 1):
                rows.setdefault(y, []).append((x_min, x_max))
        for y in sorted(rows):
            for lo, hi in _merge_spans(rows[y]):
                yield y, lo, hi
        return

    edges = _region_edges(region, n)
    if not edges:
        return

    row_lo = max(0, min(e.row_lo for e in edges))
    row_hi = min(n - 1, max(e.row_hi for e in edges))
    if row_hi < row_lo:
        return

    # 活动边表：按 row_lo 升序取边，行推进时淘汰 row_hi 已过的边。
    edges.sort(key=lambda e: e.row_lo)
    cursor = 0
    active: List[_Edge] = []

    for row in range(row_lo, row_hi + 1):
        while cursor < len(edges) and edges[cursor].row_lo <= row:
            active.append(edges[cursor])
            cursor += 1
        if active:
            active = [e for e in active if e.row_hi >= row]
        if not active:
            continue

        spans: List[Tuple[int, int]] = []

        # (1) 边穿过的格子
        for e in active:
            xa, xb = e.x_extent_in_row(row)
            spans.append((math.floor(xa), math.floor(xb)))

        # (2) 行中线奇偶扫描
        yc = row + 0.5
        crossings: List[float] = []
        for e in active:
            # 半开区间 [y0, y1) 避免顶点被数两次
            if e.y0 <= yc < e.y1:
                crossings.append(e.x_at(yc))
        if len(crossings) >= 2:
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                xa, xb = crossings[i], crossings[i + 1]
                if xb < xa:
                    continue
                lo = math.floor(xa)
                hi = math.floor(xb)
                if float(xb).is_integer() and hi > lo:
                    hi -= 1
                spans.append((lo, hi))

        for lo, hi in _wrap_spans(_merge_spans(spans), n):
            yield row, lo, hi


def count_region_tiles(region, zoom_min: int, zoom_max: int) -> int:
    """区域在 [zoom_min, zoom_max] 上的瓦片总数。

    与 `iter_region_tile_spans` 同源，所以「预估的数」和「实际要下的数」
    永远相等 —— GeoD #30 那种 17 倍偏差在这一层不可能发生（它的偏差来自
    每块瓦片的**字节数**估算，见 disk_budget；这里只管块数）。
    """
    zoom_min, zoom_max = validate_zoom_range(zoom_min, zoom_max)
    total = 0
    for zoom in range(zoom_min, zoom_max + 1):
        for _y, x0, x1 in iter_region_tile_spans(region, zoom):
            total += x1 - x0 + 1
    return total
