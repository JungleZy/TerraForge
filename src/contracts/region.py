"""RegionSpec —— 四条管线共用的区域合同。

## 为什么需要它

改造前每条管线各自表达区域，而且互相不兼容（2026-08-12 实测）：

- 地图任务：`Task.north/south/east/west` 四个 float，`Task.__post_init__` 校验；
- DEM 任务：请求 dict → `validate_bbox` → 四个 float，`dem_granules.tiles_for_bbox`
  里还有第二套独立的同名校验；
- 等高线任务：**不收区域**，从上传栅格的角点反推 `(north, south, east, west)`，
  读不出来时静默退化成 `(0,0,0,0)`；
- 本地地形任务：**表里根本没有 bbox 列**。

再加上坐标序有三种写法（管理器层 `(n,s,e,w)`、切片层 `(w,s,e,n)`、
`raster_probe` 输出 `[w,s,e,n]`），任何跨管线的区域能力都要先做一次翻译。

## 合同

`RegionSpec` 是**不可变**的、已规范化的区域：几何一律是 MultiPolygon（矩形
也是），坐标一律 (lon, lat) 且 CRS 一律 EPSG:4326，环一律闭合。构造即校验，
构造成功的实例可以直接进估算、进枚举、进掩膜，不需要下游再猜。

## 反经线

`bbox` 的 east 允许 **超过 180**，用来无歧义地表示跨反经线：
`west=170, east=190` 表示「从 170°E 往东 20°」，而不是「从 190°W 往东 350°」。
`antimeridian_parts` 把它拆成两段合法 bbox；`crosses_antimeridian` 是判据。

改造前的行为是「拒绝」：`geo_validation.validate_bbox` 对 `east <= west` 直接
抛错，而 `dem_granules.py` 的注释写着「caller should split」却没有任何调用方
在拆。现在拆分是合同的一部分，计数与枚举都走 `antimeridian_parts`。

## 坐标序

对外只有两种，且都显式命名：

- `bbox` 属性 → `(north, south, east, west)`，与现有四张任务表的列序一致；
- `bounds` 属性 → `(west, south, east, north)`，与 GDAL / 切片层一致。

不提供第三种，也不提供无名 4-tuple。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    'RegionSpec',
    'RegionValidationError',
    'split_antimeridian',
    'MAX_RING_POINTS',
]

# 单个环的顶点上限。导入的行政区划边界动辄数万点，逐点参与自相交检测是
# O(n²)；超过这个数就只做「首尾闭合 + 值域」检查并按合法处理，不做自相交
# 判定 —— 拒绝一个合法的复杂边界比放过一个自相交更糟。
MAX_RING_POINTS = 20000

# 自相交检测的边数上限。超过就跳过检测（理由同上）。
SELF_INTERSECTION_MAX_EDGES = 2000

_EPS = 1e-12

# `intersects_bbox` 判「有正面积的交集」时把矩形向内缩的量（度）。
# 1e-9° ≈ 0.1 mm：任何真实的地理重叠都远大于它，而相邻 1°×1° 颗粒之间纯粹的
# 贴边被挡住。不能用 _EPS（1e-12）：那个量级下浮点比较本身就不稳。
_TOUCH_EPS = 1e-9

Point = Tuple[float, float]
Ring = Tuple[Point, ...]
Polygon = Tuple[Ring, ...]
MultiPolygon = Tuple[Polygon, ...]

VALID_SOURCES = ('drawn', 'imported', 'administrative', 'manual', 'derived')

CRS_WGS84 = 'EPSG:4326'


class RegionValidationError(ValueError):
    """区域几何非法。

    继承 ValueError 是为了让现有路由层的 `except ValueError -> 400` 原样生效
    （`src/routes/api.py` 等四处都是这个形状），不需要为新合同再加一个分支。
    """


def _finite(value, name: str) -> float:
    if isinstance(value, bool):
        raise RegionValidationError(f"{name} must be a number, got bool")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise RegionValidationError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(f):
        raise RegionValidationError(f"{name} must be finite, got {value!r}")
    return f


def split_antimeridian(west: float, south: float, east: float, north: float
                       ) -> List[Tuple[float, float, float, float]]:
    """把可能跨 180° 的 bbox 拆成 1~2 段都落在 [-180, 180] 内的 bbox。

    入参与返回都是 `(west, south, east, north)`（GDAL 序）。

    形制照搬 GeoLibre `apps/geolibre-desktop/src/lib/offline-tiles.ts:82-89`
    的 `splitAntimeridian()`：它在 `west > east` 时返回
    `[[west,south,180,north], [-180,south,east,north]]`，并在 `countTiles()`
    与 `enumerateTiles()` 两处消费 —— 拆分点与消费点是同一份，不会出现
    「预览按不拆算、实际按拆切」的错位。本项目此前正是那个错位状态。

    本函数额外接受 `east > 180` 的规范化写法（RegionSpec 用它表示跨界），
    等价于 `west > east` 的绕回写法。
    """
    if east > 180.0:
        # 规范化写法：west=170, east=190
        return [(west, south, 180.0, north), (-180.0, south, east - 360.0, north)]
    if west > east:
        # 绕回写法：west=170, east=-170
        return [(west, south, 180.0, north), (-180.0, south, east, north)]
    return [(west, south, east, north)]


def _close_ring(points: Sequence[Point]) -> Ring:
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        raise RegionValidationError(
            f"ring needs at least 3 distinct points, got {len(pts)}")
    if abs(pts[0][0] - pts[-1][0]) > _EPS or abs(pts[0][1] - pts[-1][1]) > _EPS:
        pts.append(pts[0])
    # 去掉相邻重复点：导入的 KML 常见首尾之外还有重复顶点，重复点会让
    # 自相交检测把「零长度边」判成相交。
    deduped: List[Point] = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - deduped[-1][0]) > _EPS or abs(p[1] - deduped[-1][1]) > _EPS:
            deduped.append(p)
    if len(deduped) < 4:
        raise RegionValidationError(
            "ring degenerates to fewer than 3 distinct points")
    if abs(deduped[0][0] - deduped[-1][0]) > _EPS or abs(deduped[0][1] - deduped[-1][1]) > _EPS:
        deduped.append(deduped[0])
    return tuple(deduped)


def ring_area(ring: Ring) -> float:
    """带符号面积（shoelace，度²）。正 = 逆时针（CCW）。

    只用来判定环的绕向与「是否退化成一条线」，不是地理面积。
    """
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _segments_properly_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """两条线段是否**真交叉**（共享端点、共线重叠都不算）。"""
    def orient(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and \
       ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)):
        return True
    return False


def _ring_is_self_intersecting(ring: Ring) -> bool:
    n = len(ring) - 1
    if n > SELF_INTERSECTION_MAX_EDGES:
        return False
    for i in range(n):
        a1, a2 = ring[i], ring[i + 1]
        # j 从 i+2 开始跳过相邻边（它们共享端点，不算交叉）；
        # 最后一条边与第一条边同样相邻，用 last 排除。
        last = n - 1 if i == 0 else n
        for j in range(i + 2, last):
            if _segments_properly_intersect(a1, a2, ring[j], ring[j + 1]):
                return True
    return False


def point_in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """奇偶规则的点在环内判定（边界点的归属不保证，本用途无所谓）。"""
    inside = False
    n = len(ring) - 1
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if x_at > lon:
                inside = not inside
    return inside


@dataclass(frozen=True)
class RegionSpec:
    """规范化后的区域。构造即校验，构造成功即可直接消费。

    geometry
        MultiPolygon：`((outer_ring, hole_ring, ...), ...)`。矩形也是
        MultiPolygon（一个多边形、一个四点外环），下游不需要分支。
        环坐标是 (lon, lat)，闭合（首点 == 末点）。
    bbox_north / bbox_south / bbox_east / bbox_west
        外接矩形。`bbox_east` 允许 > 180 表示跨反经线（见模块 docstring）。
    crs
        永远是 `EPSG:4326`。导入路径负责重投影后再构造本对象 ——
        合同里留一个「可能不是 4326」的口子，等于把重投影责任推给每个消费方。
    source
        `drawn | imported | administrative | manual | derived`。
        `derived` 是等高线 / 本地地形那种「从源栅格反推」的来源。
    display_name
        给 UI 看的名字（导入文件名、行政区名）。可空。
    """

    geometry: MultiPolygon
    bbox_north: float
    bbox_south: float
    bbox_east: float
    bbox_west: float
    crs: str = CRS_WGS84
    source: str = 'manual'
    display_name: str = ''

    # ---- 构造校验 ---------------------------------------------------

    def __post_init__(self):
        if self.crs != CRS_WGS84:
            raise RegionValidationError(
                f"RegionSpec only stores {CRS_WGS84}; reproject before constructing "
                f"(got {self.crs!r})")
        if self.source not in VALID_SOURCES:
            raise RegionValidationError(
                f"source must be one of {VALID_SOURCES}, got {self.source!r}")
        if not self.geometry:
            raise RegionValidationError("geometry must contain at least one polygon")

        n = _finite(self.bbox_north, 'north')
        s = _finite(self.bbox_south, 'south')
        e = _finite(self.bbox_east, 'east')
        w = _finite(self.bbox_west, 'west')
        if not (-90.0 <= s <= 90.0):
            raise RegionValidationError(f"south must be within [-90, 90], got {s}")
        if not (-90.0 <= n <= 90.0):
            raise RegionValidationError(f"north must be within [-90, 90], got {n}")
        if n <= s:
            raise RegionValidationError(f"north ({n}) must be greater than south ({s})")
        if not (-180.0 <= w <= 180.0):
            raise RegionValidationError(f"west must be within [-180, 180], got {w}")
        # east 的上界是 360：west 最小 -180，跨界最多再走 360°。
        if not (-180.0 <= e <= 360.0):
            raise RegionValidationError(f"east must be within [-180, 360], got {e}")
        if e <= w:
            raise RegionValidationError(
                f"east ({e}) must be greater than west ({w}); an antimeridian-crossing "
                f"region is expressed as east > 180, not as east < west")
        if e - w > 360.0:
            raise RegionValidationError(f"region spans more than 360 degrees of longitude")

        for poly in self.geometry:
            if not poly:
                raise RegionValidationError("polygon must have an outer ring")
            for ring in poly:
                if len(ring) < 4:
                    raise RegionValidationError("ring must be closed with >= 3 points")
                if abs(ring[0][0] - ring[-1][0]) > _EPS or abs(ring[0][1] - ring[-1][1]) > _EPS:
                    raise RegionValidationError("ring is not closed")
                if len(ring) > MAX_RING_POINTS:
                    continue
                # 自相交先查：一个「领结」形环的 shoelace 面积恰好是 0，
                # 先查面积会给出「所有点共线」这种彻头彻尾的假原因。
                if _ring_is_self_intersecting(ring):
                    raise RegionValidationError(
                        "ring is self-intersecting; fix the geometry before importing")
                if abs(ring_area(ring)) <= _EPS:
                    raise RegionValidationError(
                        "ring has zero area (all points collinear or coincident)")

    # ---- 构造入口 ---------------------------------------------------

    @classmethod
    def from_bbox(cls, north, south, east, west, *, source='manual',
                  display_name='') -> 'RegionSpec':
        """矩形 → RegionSpec。

        接受两种跨反经线写法并归一成 `east > 180`：
        - `east < west`（绕回写法，例如 west=170, east=-170）；
        - `east > 180`（规范化写法）。
        """
        n = _finite(north, 'north')
        s = _finite(south, 'south')
        e = _finite(east, 'east')
        w = _finite(west, 'west')
        if e < w and -180.0 <= e <= 180.0:
            e += 360.0
        ring = _close_ring([(w, s), (e, s), (e, n), (w, n)])
        return cls(geometry=((ring,),), bbox_north=n, bbox_south=s,
                   bbox_east=e, bbox_west=w, source=source,
                   display_name=display_name)

    @classmethod
    def from_polygons(cls, polygons: Iterable[Sequence[Sequence[Point]]], *,
                      source='imported', display_name='') -> 'RegionSpec':
        """MultiPolygon（外环 + 洞环）→ RegionSpec。

        洞环**必须**在这里传进来。GeoDownloader 的洞环 bug 根因就是前端
        只取了外环（`region-selector.tsx:42,45`），而它的后端掩膜本来是
        奇偶扫描线、传进内环就能正确挖洞。这里不给「只取外环」留接口。
        """
        norm: List[Polygon] = []
        for poly in polygons:
            rings = [_close_ring(r) for r in poly]
            if not rings:
                continue
            # 绕向归一：外环 CCW、洞环 CW。奇偶规则本身不看绕向，但归一之后
            # 产出的 GeoJSON 符合 RFC 7946，外部工具（QGIS / OGR）读得对。
            outer = rings[0]
            if ring_area(outer) < 0:
                outer = tuple(reversed(outer))
            holes = []
            for h in rings[1:]:
                holes.append(tuple(reversed(h)) if ring_area(h) > 0 else h)
            norm.append((outer, *holes))
        if not norm:
            raise RegionValidationError("no usable polygon in input")

        lons = [p[0] for poly in norm for ring in poly for p in ring]
        lats = [p[1] for poly in norm for ring in poly for p in ring]
        west, east = min(lons), max(lons)
        south, north = min(lats), max(lats)
        # 跨反经线的多边形通常写成 -179 与 +179 两簇点，直接取 min/max 会得到
        # 一个「几乎整个地球」的外接矩形。要把它读成 2° 宽的跨界区域，就得把
        # 负经度 +360。问题在于什么时候该这么读。
        #
        # ⚠️ 原判据是「直接跨度 > 180 且平移后 ≤ 180 就平移」，它会把一个**合法的
        # 宽区域**换成它的补集。实测反例：经度 -170..30 的北大西洋+欧洲矩形
        # （200° 宽，完全没碰反经线）被读成 30..190，`contains_point(0°, 40°N)`
        # 从 True 变 False、`contains_point(120°E, 40°N)` 从 False 变 True ——
        # 用户导入一个文件，程序去下地球另一半，而瓦片数看着还挺合理（z4 是
        # 32 对 40），界面上没有任何异常。它还会随 to_json 一起持久化进
        # tasks.region_spec。
        #
        # 光看顶点集是**无法**区分这两种情况的：{179, -179} 与 {-170, 30} 结构
        # 完全一样，都是「两个值、两侧各一条弧」，信息在边的走向里，而
        # RFC 7946 §3.1.9 恰恰规定跨反经线的几何**应当**先切成两块，所以一条
        # 200° 的边本身就是不合规写法，无从判定作者意图。
        #
        # 所以改成一个可解释、且永不产出补集的判据：**只有当没有任何顶点落在
        # 本初子午线那一侧的半球（-90, 90）内时，才读成跨界。**
        #   · {179, -179}   两点都在 (-90,90) 之外 → 跨界，2° 宽。正确。
        #   · {-170, 30}    30 在带内            → 不跨界，200° 宽。正确。
        # 判据失效的方向是「把一个真跨界区域读成宽区域」（例如从 80°E 跨到
        # -170°），那是保守方向：多下一些瓦片，不会把区域挪到地球另一边。
        # 真遇上这种区域，按 RFC 7946 的建议在反经线处切成两个多边形再导入。
        if east - west > 180.0 and all(abs(x) >= 90.0 for x in lons):
            shifted = [x + 360.0 if x < 0 else x for x in lons]
            if max(shifted) - min(shifted) <= 180.0:
                west, east = min(shifted), max(shifted)
                norm = tuple(
                    tuple(
                        tuple(((x + 360.0 if x < 0 else x), y) for x, y in ring)
                        for ring in poly
                    )
                    for poly in norm
                )
        return cls(geometry=tuple(tuple(p) for p in norm), bbox_north=north,
                   bbox_south=south, bbox_east=east, bbox_west=west,
                   source=source, display_name=display_name)

    @classmethod
    def from_geojson(cls, obj, *, source='imported', display_name='') -> 'RegionSpec':
        """GeoJSON（Feature / FeatureCollection / Geometry）→ RegionSpec。

        多个 Feature 会合并成一个 MultiPolygon（用户选了一个文件就是想下
        整个文件覆盖的范围）。非面要素（Point / LineString）被忽略；
        一个面都没有就抛错，不静默产出空区域。
        """
        if isinstance(obj, (bytes, bytearray)):
            obj = obj.decode('utf-8')
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except json.JSONDecodeError as exc:
                raise RegionValidationError(f"invalid GeoJSON: {exc}") from None
        if not isinstance(obj, dict):
            raise RegionValidationError("GeoJSON root must be an object")

        polys: List[Sequence[Sequence[Point]]] = []
        _collect_geojson_polygons(obj, polys, depth=0)
        if not polys:
            raise RegionValidationError(
                "GeoJSON contains no Polygon or MultiPolygon geometry")
        return cls.from_polygons(polys, source=source, display_name=display_name)

    # ---- 派生属性 ---------------------------------------------------

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        """`(north, south, east, west)` —— 与四张任务表的列序一致。"""
        return (self.bbox_north, self.bbox_south, self.bbox_east, self.bbox_west)

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        """`(west, south, east, north)` —— 与 GDAL / 切片层一致。"""
        return (self.bbox_west, self.bbox_south, self.bbox_east, self.bbox_north)

    @property
    def crosses_antimeridian(self) -> bool:
        return self.bbox_east > 180.0

    @property
    def antimeridian_parts(self) -> Tuple[Tuple[float, float, float, float], ...]:
        """拆分后的 bbox 段，序为 `(north, south, east, west)`，均落在 ±180 内。

        不跨界时返回一个元素 —— 消费方永远走同一条路径，不需要 if。
        """
        parts = split_antimeridian(self.bbox_west, self.bbox_south,
                                   self.bbox_east, self.bbox_north)
        return tuple((n, s, e, w) for (w, s, e, n) in parts)

    @property
    def has_holes(self) -> bool:
        return any(len(poly) > 1 for poly in self.geometry)

    @property
    def hole_count(self) -> int:
        return sum(len(poly) - 1 for poly in self.geometry)

    @property
    def polygon_count(self) -> int:
        return len(self.geometry)

    @property
    def vertex_count(self) -> int:
        return sum(len(ring) - 1 for poly in self.geometry for ring in poly)

    @property
    def is_rectangle(self) -> bool:
        """几何是否就是外接矩形本身。

        为真时下游可以走「按 bbox 枚举」的快路径，跳过多边形栅格化。
        """
        if len(self.geometry) != 1 or len(self.geometry[0]) != 1:
            return False
        ring = self.geometry[0][0]
        if len(ring) != 5:
            return False
        xs = {round(p[0], 9) for p in ring[:4]}
        ys = {round(p[1], 9) for p in ring[:4]}
        return (xs == {round(self.bbox_west, 9), round(self.bbox_east, 9)}
                and ys == {round(self.bbox_south, 9), round(self.bbox_north, 9)})

    def contains_point(self, lon: float, lat: float) -> bool:
        """奇偶规则：外环内、洞环外才算命中。"""
        hit = False
        for poly in self.geometry:
            for ring in poly:
                if point_in_ring(lon, lat, ring):
                    hit = not hit
        return hit

    def intersects_bbox(self, north: float, south: float, east: float,
                        west: float) -> bool:
        """本区域与一块经纬度矩形是否有交集。**精确判定，不是外接矩形近似。**

        为 DEM 颗粒筛选而生：颗粒是 1°×1° 的经纬度块，`tiles_for_bbox` 只按
        外接矩形枚举，所以一个 L 形或带洞的区域会拿到和它外接矩形**一模一样**
        的颗粒清单 —— 实测 4 个对 4 个，多边形白画了。那正是 GeoDownloader
        「按 bbox 计费、按 polygon 出图」的同一个错位，只不过发生在 DEM 侧，
        而且更糟：DEM 颗粒是几十 MB 一个，多下一圈是实打实的流量与磁盘。

        判据是三条的并集，缺一不可：
          1. 矩形的任一角点落在区域内（区域完全包住矩形时只有这条成立）；
          2. 区域的任一顶点落在矩形内（矩形完全包住区域时只有这条成立）；
          3. 任一条区域边与矩形的四条边相交（十字交叉时前两条都不成立）。
        洞环参与第 1 条的奇偶判定，所以整块落在洞里的矩形会被正确排除。

        坐标序与 `bbox` 属性一致：`(north, south, east, west)`。跨反经线的区域
        顶点经度可能 > 180，调用方传的矩形也要用同一套未回绕坐标，否则两边
        对不上 —— 调用方应当按 `antimeridian_parts` 逐段问。
        判定的是**有正面积的交集**，不是「碰到了」。相邻的两块 1°×1° 颗粒天然
        共享一条边，若把贴边算成相交，每个被排除的格子都会被它的邻居救回来 ——
        实测过：L 形区域筛完还是 4 颗，一颗没少。做法是把矩形向内缩
        `_TOUCH_EPS`（1e-9 度，约 0.1 mm）再判：真实的重叠远大于它，纯贴边的
        则被排除干净。
        """
        n = float(north); s = float(south); e = float(east); w = float(west)
        if n < s:
            n, s = s, n
        if e < w:
            e, w = w, e
        # 外接矩形不相交就一定不相交 —— 便宜的早退，绝大多数颗粒走这条。
        if (e < self.bbox_west or w > self.bbox_east
                or n < self.bbox_south or s > self.bbox_north):
            return False
        # 向内缩一点，把「只是贴着边」挡在外面（见 docstring）。缩到反了就说明
        # 这个矩形本身退化成一条线/一个点，那种输入没有正面积可言。
        iw, ie = w + _TOUCH_EPS, e - _TOUCH_EPS
        isth, inth = s + _TOUCH_EPS, n - _TOUCH_EPS
        if iw >= ie or isth >= inth:
            return False
        # 1. 缩进后的角点或中心落在区域内。中心那一条覆盖「区域整块包住矩形」，
        #    角点覆盖「区域盖住矩形的一角」。
        probes = ((iw, isth), (ie, isth), (ie, inth), (iw, inth),
                  ((iw + ie) / 2.0, (isth + inth) / 2.0))
        for lon, lat in probes:
            if self.contains_point(lon, lat):
                return True
        rect_edges = (((iw, isth), (ie, isth)), ((ie, isth), (ie, inth)),
                      ((ie, inth), (iw, inth)), ((iw, inth), (iw, isth)))
        for poly in self.geometry:
            for ring in poly:
                for i in range(len(ring) - 1):
                    a, b = ring[i], ring[i + 1]
                    # 2. 区域顶点**严格**落在缩进后的矩形内（矩形包住区域一角）
                    if iw < a[0] < ie and isth < a[1] < inth:
                        return True
                    # 3. 区域的边真交叉矩形的边（十字交叉时前两条都不成立）
                    for r1, r2 in rect_edges:
                        if _segments_properly_intersect(a, b, r1, r2):
                            return True
        return False

    def summary(self) -> str:
        """一行摘要，进任务日志与诊断包。"""
        shape = 'rect' if self.is_rectangle else f'poly x{self.polygon_count}'
        holes = f', holes={self.hole_count}' if self.has_holes else ''
        am = ', antimeridian' if self.crosses_antimeridian else ''
        name = f' "{self.display_name}"' if self.display_name else ''
        return (f'{shape}{holes}{am} [{self.bbox_west:.5f},{self.bbox_south:.5f},'
                f'{self.bbox_east:.5f},{self.bbox_north:.5f}] src={self.source}{name}')

    # ---- 序列化 -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': 'MultiPolygon',
            'coordinates': [[[list(p) for p in ring] for ring in poly]
                            for poly in self.geometry],
            'bbox': [self.bbox_west, self.bbox_south, self.bbox_east, self.bbox_north],
            'crs': self.crs,
            'source': self.source,
            'display_name': self.display_name,
        }

    def to_json(self) -> str:
        """落库形态。列是 TEXT，空串表示「这一行没有 RegionSpec」。"""
        return json.dumps(self.to_dict(), separators=(',', ':'), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RegionSpec':
        """落库/请求体的 dict → RegionSpec。

        **结构性损坏一律收敛成 `RegionValidationError`。** 这不是防御性编程：
        `tasks.region_spec` 是一个普通 TEXT 列，可能来自手改的库、写了一半的行、
        或者更早的格式。裸 `float(p[0])` 对 `{"coordinates": "not-a-polygon"}`
        抛的是 `ValueError: could not convert string to float: 'n'`，而
        `from_row` 的 docstring 承诺「任何一步失败都返回 None」、只 catch
        `RegionValidationError` —— 于是一行脏数据会穿过兜底，一路传到
        `cache_exclusive._row_rects_by_zoom` 和历史列表渲染里。那正是
        `Task.from_row` 刻意绕过 `__post_init__` 想避免的同一类事故。

        收敛点放在这里而不是在 `from_row` 加宽 except：这样**每一个**调用方
        （路由收到的客户端 region、from_json、from_row）都拿到同一种异常，
        而不是各自去猜要 catch 哪几种。
        """
        if not isinstance(data, dict):
            raise RegionValidationError(
                f'region payload must be an object, got {type(data).__name__}')
        coords = data.get('coordinates')
        if not coords:
            raise RegionValidationError("region dict has no coordinates")
        try:
            polys = [[[(float(p[0]), float(p[1])) for p in ring] for ring in poly]
                     for poly in coords]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise RegionValidationError(
                f'malformed region coordinates: {exc}') from None
        spec = cls.from_polygons(polys, source=data.get('source', 'imported'),
                                 display_name=data.get('display_name', ''))
        bbox = data.get('bbox')
        if bbox and not isinstance(bbox, (list, tuple)):
            raise RegionValidationError(
                f'region bbox must be an array, got {type(bbox).__name__}')
        if bbox and len(bbox) == 4:
            # 落库的 bbox 是权威值：from_polygons 会从顶点重算，跨反经线的
            # 归一有可能与写入时不同（例如导入后用户又编辑过 bbox）。
            try:
                w, s, e, n = (float(v) for v in bbox)
            except (TypeError, ValueError) as exc:
                raise RegionValidationError(
                    f'malformed region bbox: {exc}') from None
            spec = cls(geometry=spec.geometry, bbox_north=n, bbox_south=s,
                       bbox_east=e, bbox_west=w, source=spec.source,
                       display_name=spec.display_name)
        return spec

    @classmethod
    def from_json(cls, text) -> 'RegionSpec':
        if not text:
            raise RegionValidationError("empty region payload")
        if isinstance(text, (bytes, bytearray)):
            text = text.decode('utf-8')
        if isinstance(text, dict):
            return cls.from_dict(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RegionValidationError(f"invalid region JSON: {exc}") from None
        return cls.from_dict(data)

    @classmethod
    def from_row(cls, row, *, source='derived'):
        """任务行 → RegionSpec，`region_spec` 列优先、四至列兜底。

        存量任务行没有 `region_spec`（该列是本次改造新加的，默认空串），
        必须能从 north/south/east/west 还原，否则历史任务在新 UI 上会
        整片消失 —— 这正是 `Task.from_row` 刻意绕过 `__post_init__` 想避免的
        那类回归。任何一步失败都返回 None，由调用方决定降级形态。
        """
        def _get(key):
            try:
                return row[key]
            except (IndexError, KeyError, TypeError):
                return None

        raw = _get('region_spec')
        if raw:
            try:
                return cls.from_json(raw)
            except RegionValidationError:
                pass
        n, s, e, w = _get('north'), _get('south'), _get('east'), _get('west')
        if None in (n, s, e, w):
            return None
        try:
            return cls.from_bbox(n, s, e, w, source=source)
        except RegionValidationError:
            return None


def _collect_geojson_polygons(node, out: List, depth: int) -> None:
    """递归收集 GeoJSON 里的 Polygon / MultiPolygon 坐标。

    depth 上限防御恶意嵌套的 GeometryCollection（本机应用威胁模型下概率极低，
    但一个 while-true 的 JSON 能挂死服务进程，代价不对称）。
    """
    if depth > 8 or not isinstance(node, dict):
        return
    gtype = node.get('type')
    if gtype == 'FeatureCollection':
        for feat in node.get('features') or ():
            _collect_geojson_polygons(feat, out, depth + 1)
    elif gtype == 'Feature':
        _collect_geojson_polygons(node.get('geometry'), out, depth + 1)
    elif gtype == 'GeometryCollection':
        for geom in node.get('geometries') or ():
            _collect_geojson_polygons(geom, out, depth + 1)
    elif gtype == 'Polygon':
        coords = node.get('coordinates') or []
        if coords:
            out.append([[(float(p[0]), float(p[1])) for p in ring] for ring in coords])
    elif gtype == 'MultiPolygon':
        for poly in node.get('coordinates') or ():
            if poly:
                out.append([[(float(p[0]), float(p[1])) for p in ring] for ring in poly])
