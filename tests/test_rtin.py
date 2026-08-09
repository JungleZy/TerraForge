"""RTIN（Martini）误差表、误差计算，以及它接进切片流程后的行为。

前半部分（rtin_tables / rtin_errors / rtin_extract）是纯 numpy，不需要
GDAL / 采样，因此不做任何 monkey-patch。文件末尾的接线测试要跑
cesiumlab_terrain 的编码与 build_terrain，那部分需要 GDAL。
"""

import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract, rtin_tables


def test_tables_cover_every_triangle_exactly_once():
    grid = 17
    t = grid - 1
    tables = rtin_tables(grid)
    total = sum(len(v[0]) for v in tables.values())
    assert total == t * t * 2 - 2, "RTIN 三角形总数应为 2*t^2-2"


def test_tables_are_cached_per_grid():
    a = rtin_tables(17)
    b = rtin_tables(17)
    assert a is b, "rtin_tables 必须带 lru_cache —— 每瓦片重算是纯浪费"


def _recursive_tables(grid: int) -> dict[int, list[tuple]]:
    """纯递归的 RTIN 参考构造 —— 刻意【不含任何位运算】，也不碰隐式二叉树下标。

    存在的理由：下面的 _naive_errors 里那段坐标还原是从实现里抄来的，
    位运算写错的话两边一起错、测试照样全绿（本项目上个月的 quantized-mesh
    triangleCount 就是这么漏出去的）。所以另起一套只用几何定义的构造来对拍。

    RTIN 的递归定义：三角形 (a, b, c) 中 c 是直角顶点、a-b 是斜边，取斜边
    中点 m 后分裂成 (c, a, m) 与 (b, c, m)。起始是 (t,t)-(0,0)-(0,t) 与
    (0,0)-(t,t)-(t,0) 两个，t = grid-1；层级 = 递归深度（起始两个为 1）。
    lc / rc 分别是两个子三角形的斜边中点，即 mid(c,a) 与 mid(b,c) ——
    实现那边是靠 ccx/ccy 反解直角顶点再取中点的，这里直接用递归携带的 c，
    所以这条对拍也顺带验了那个反解公式。
    """
    t = grid - 1
    max_level, n = 0, t
    while n > 1:          # 边长每减半 = 两个 RTIN 层级
        n //= 2
        max_level += 2
    out: dict[int, list[tuple]] = {}

    def idx(p):
        return p[1] * grid + p[0]

    def mid(p, q):
        return ((p[0] + q[0]) // 2, (p[1] + q[1]) // 2)

    def rec(a, b, c, level):
        m = mid(a, b)
        out.setdefault(level, []).append(
            (idx(a), idx(b), idx(m), idx(mid(c, a)), idx(mid(b, c)))
        )
        if level < max_level:
            rec(c, a, m, level + 1)
            rec(b, c, m, level + 1)

    rec((t, t), (0, 0), (0, t), 1)
    rec((0, 0), (t, t), (t, 0), 1)
    return {lvl: sorted(v) for lvl, v in out.items()}


@pytest.mark.parametrize("grid", [5, 17, 65])
def test_tables_match_pure_recursive_reference(grid):
    """rtin_tables 的位运算转写必须与纯几何递归构造等价。

    同层内的顺序两边本来就不同（实现按隐式二叉树下标 i 遍历，参考是深度
    优先），所以按层比【排序后的五元组集合】，不比数组顺序。
    """
    tables = rtin_tables(grid)
    ref = _recursive_tables(grid)
    assert sorted(tables) == sorted(ref), "层级集合不一致"
    for lvl in sorted(ref):
        got = sorted(zip(*(arr.tolist() for arr in tables[lvl])))
        assert got == ref[lvl], f"level {lvl} 的三角形集合与纯递归参考不一致"


def test_flat_terrain_has_zero_error_inside():
    """完全平坦的地形，内部误差必须全 0（插值与真值无差）。"""
    grid = 17
    h = np.full(grid * grid, 100.0)
    err = rtin_errors(h, grid, pin_border=False)
    assert np.allclose(err, 0.0)


def test_border_pinning_marks_every_border_point_infinite():
    grid = 17
    h = np.full(grid * grid, 100.0)
    err = rtin_errors(h, grid, pin_border=True).reshape(grid, grid)
    assert np.isinf(err[0, :]).all()
    assert np.isinf(err[-1, :]).all()
    assert np.isinf(err[:, 0]).all()
    assert np.isinf(err[:, -1]).all()


def test_border_infinity_propagates_to_ancestors():
    """边界 inf 必须在【传播之前】注入 —— 否则约束到不了祖先三角形。

    这是设计阶段踩过的坑：把 inf 设在传播之后，边界约束完全失效，
    测出 93.4% 的假减面率（真值 84.8%）。

    断言的是【确切数量】而不是 .any()：pin 挪到循环之后时内部 inf 是 0，
    .any() 确实挡得住；但挡不住「只往上传一层」这类部分退化。61 的来历 ——
    grid=17 内部 15x15=225 个点里应有 61 个因子孙含边界点而变 inf。这个数
    只由 RTIN 的树结构决定、与地形数值无关（max(任意, inf) 恒为 inf），
    已实测 5 个随机种子加全平地形均为 61，所以它不是随手锁的回归魔数。
    """
    grid = 17
    rng = np.random.default_rng(0)
    h = rng.random(grid * grid) * 100.0
    err = rtin_errors(h, grid, pin_border=True).reshape(grid, grid)
    interior = err[1:-1, 1:-1]
    assert int(np.isinf(interior).sum()) == 61, "内部祖先节点的 inf 数量不对 —— 传播被截断了"


@pytest.mark.parametrize("grid", [17, 65])
def test_error_matches_naive_reference_on_random_terrain(grid):
    """向量化实现必须与逐三角形的朴素参考实现逐元素相同。

    必须覆盖 65 —— 生产用的就是 65（12 个层级），17 只走 8 个层级。
    """
    rng = np.random.default_rng(7)
    h = rng.random(grid * grid) * 500.0
    fast = rtin_errors(h, grid, pin_border=False)
    slow = _naive_errors(h, grid)
    assert np.array_equal(fast, slow)


def _naive_errors(heights: np.ndarray, grid: int) -> np.ndarray:
    """逐三角形的参考实现（慢但直白），只用于对拍。"""
    t = grid - 1
    num_tri = t * t * 2 - 2
    num_parent = num_tri - t * t
    errors = np.zeros(grid * grid)
    for i in range(num_tri - 1, -1, -1):
        id_ = i + 2
        ax = ay = bx = by = cx = cy = 0
        if id_ & 1:
            bx = by = cx = t
        else:
            ax = ay = cy = t
        id_ >>= 1
        while id_ > 1:
            mx = (ax + bx) >> 1
            my = (ay + by) >> 1
            if id_ & 1:
                bx, by, ax, ay = ax, ay, cx, cy
            else:
                ax, ay, bx, by = bx, by, cx, cy
            cx, cy = mx, my
            id_ >>= 1
        mx = (ax + bx) >> 1
        my = (ay + by) >> 1
        ccx = mx + my - ay
        ccy = my + ax - mx
        mid = my * grid + mx
        interp = (heights[ay * grid + ax] + heights[by * grid + bx]) * 0.5
        errors[mid] = max(errors[mid], abs(interp - heights[mid]))
        if i < num_parent:
            lc = ((ay + ccy) >> 1) * grid + ((ax + ccx) >> 1)
            rc = ((by + ccy) >> 1) * grid + ((bx + ccx) >> 1)
            errors[mid] = max(errors[mid], errors[lc], errors[rc])
    return errors


def test_rejects_non_power_of_two_plus_one_grid():
    with pytest.raises(ValueError):
        rtin_tables(64)   # 64-1=63 不是 2 的幂


def test_rejects_degenerate_grid_of_two():
    """grid=2 形式上是合法的 2^0+1，但退化到 0 个三角形。

    必须在校验里当场拒掉：否则 rtin_tables(2) 返回 {}，rtin_errors 取
    levels[0] 抛 IndexError —— 一个和入参毫无关系、没法排查的报错。
    """
    with pytest.raises(ValueError):
        rtin_tables(2)
    with pytest.raises(ValueError):
        rtin_errors(np.zeros(4), 2)


def test_tables_are_read_only():
    """缓存表必须只读 —— 它被 lru_cache 全进程共享，改一个元素会污染所有后续调用。"""
    tables = rtin_tables(17)
    for arrays in tables.values():
        for arr in arrays:
            assert not arr.flags.writeable
    with pytest.raises(ValueError):
        tables[1][0][0] = 12345


def test_extract_flat_terrain_with_border_pinned_keeps_all_border_points():
    """平坦地形 + 边界满密度：内部塌到最简，但四条边一个点都不能少。"""
    grid = 17
    h = np.full(grid * grid, 50.0)
    err = rtin_errors(h, grid, pin_border=True)
    verts, tris = rtin_extract(err, grid, max_error=1.0)
    rc = np.array([v // grid for v in verts])
    cc = np.array([v % grid for v in verts])
    on_border = (rc == 0) | (rc == grid - 1) | (cc == 0) | (cc == grid - 1)
    assert on_border.sum() == 4 * (grid - 1), (
        f"边界点保留 {on_border.sum()}，应为 {4*(grid-1)}"
    )


def test_adjacent_tiles_share_identical_border_vertex_sets():
    """跨瓦片无缝的核心断言：两块地形各自提取后，公共边的保留点必须一致。

    构造两块地形，让 A 的最后一列 == B 的第一列（真实切片里靠 linspace
    端点共享保证）。边界满密度下两侧都必须全保留，因此逐点相同。

    地形必须【平滑】。原来用的白噪声 ×800 在 max_error=5 下本来就细分到
    508/512 三角形、287/289 顶点，边界点是「顺带全留」的，跟 pin 毫无关系 ——
    把 rtin_errors 里的 inf 全换成 0.0（等于拆掉边界满密度机制），这条测试
    照样通过，那它就没在测它声称测的东西。换成平滑地形后同样 max_error=5：
    pin=True 边界 64/64，pin=False 只剩 17/64，拆 pin 立刻打红。

    注意 b2[:, 0] = a2[:, -1] 这行记录的是真实场景（相邻瓦片公共边采样点
    重合），但它撑不起本断言 —— 边界满密度下两侧全保留，与高程是否相等无关。
    真正的无缝保证来自「全保留」，这行只是让构造贴近现实。
    """
    grid = 17
    yy, xx = np.mgrid[0:grid, 0:grid]
    a2 = 100.0 * np.sin(xx / 6.0) * np.cos(yy / 6.0)
    b2 = 120.0 * np.cos(xx / 5.0) * np.sin(yy / 7.0)
    b2[:, 0] = a2[:, -1]          # 公共边高程一致
    va, _ = rtin_extract(rtin_errors(a2.reshape(-1), grid, True), grid, 5.0)
    vb, _ = rtin_extract(rtin_errors(b2.reshape(-1), grid, True), grid, 5.0)
    east_a = sorted(v // grid for v in va if v % grid == grid - 1)
    west_b = sorted(v // grid for v in vb if v % grid == 0)
    assert east_a == west_b == list(range(grid))


def test_extract_vertex_order_is_first_occurrence():
    """顶点必须按【首次出现顺序】编号 —— Task 3 的向量化 high-water-mark
    编码依赖这个规范形式，顺序错了编码就是错的。

    下半段的面积闭合断言不是锦上添花：只查「三角形索引里新值按 0,1,2… 递增
    出现」的话，该性质对 verts 的【任意置换】都成立 —— 把返回值换成
    np.sort(verts) 或 verts[::-1]，这条测试照样绿。而下一个任务按 verts 写
    顶点缓冲、按 tris 写索引缓冲，两者错位则整个几何全错。面积和恰好等于
    (grid-1)^2 同时钉死了两件事：verts 与 tris 对齐，且网格水密（无洞无重叠、
    无退化三角形）。verts 一经排序，面积和就从 256 变成 2193.5。
    """
    grid = 17
    rng = np.random.default_rng(11)
    err = rtin_errors(rng.random(grid * grid) * 100.0, grid, True)
    verts, tris = rtin_extract(err, grid, 2.0)
    flat = tris.reshape(-1)
    seen = set()
    expected_next = 0
    for v in flat:
        if v not in seen:
            assert v == expected_next, "顶点编号不是按首次出现顺序"
            seen.add(v)
            expected_next += 1
    xs, ys = verts[tris] % grid, verts[tris] // grid
    area = 0.5 * np.abs(
        (xs[:, 1] - xs[:, 0]) * (ys[:, 2] - ys[:, 0])
        - (xs[:, 2] - xs[:, 0]) * (ys[:, 1] - ys[:, 0])
    )
    assert abs(area.sum() - (grid - 1) ** 2) < 1e-9, (
        f"面积和 {area.sum()} != {(grid-1)**2} —— verts 与 tris 错位或网格不水密"
    )


@pytest.mark.parametrize("grid", [17, 65])
@pytest.mark.parametrize("max_error", [1.0, 50.0])
def test_extract_triangles_are_counter_clockwise(grid, max_error):
    """三角形绕向必须是 CCW（有向面积为正），与规则网格分支一致。

    为什么这条不是可有可无的形式检查：下游按三角形绕向算顶点法线。绕向反了
    法线就整体翻转，Cesium 开背面剔除后【地形直接不可见】—— 而失败形态是
    HTTP 全 200、任务标 completed、前端一声不吭，只是什么都不显示。

    基准是 cesiumlab_terrain._mesh_constants 那条规则网格路径（切法为
    [a0,a1,a2] / [a1,a3,a2]），它已经在 Cesium 里验证过能正常渲染，实测
    512/512 有向面积全正。rtin_extract 起初发射顺序是 a->b->c，实测 25 组
    grid x max_error 共 43708 个三角形【全部为负】（CW），方向整体反了，
    改成 a->c->b 后全部转正。

    这个洞当初没有测试钉住，一路漏到接线前才被发现，所以这条断言必须留着。
    """
    rng = np.random.default_rng(4)
    err = rtin_errors(rng.random(grid * grid) * 400.0, grid, True)
    verts, tris = rtin_extract(err, grid, max_error)
    vx = (verts % grid).astype(float)
    vy = (verts // grid).astype(float)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    area = 0.5 * ((vx[b] - vx[a]) * (vy[c] - vy[a]) - (vx[c] - vx[a]) * (vy[b] - vy[a]))
    assert (area > 0).all(), (
        f"grid={grid} max_error={max_error}: {int((area <= 0).sum())}/{len(area)} "
        f"个三角形不是 CCW —— 法线会翻转，Cesium 背面剔除下地形不可见"
    )


def test_larger_max_error_yields_fewer_triangles():
    grid = 65
    rng = np.random.default_rng(5)
    err = rtin_errors(rng.random(grid * grid) * 300.0, grid, True)
    _, fine = rtin_extract(err, grid, 1.0)
    _, coarse = rtin_extract(err, grid, 50.0)
    assert len(coarse) < len(fine)
    assert len(fine) <= 2 * (grid - 1) * (grid - 1)


def test_triangle_count_never_exceeds_full_grid():
    """max_error=0 时细分到满网格，且【不会超过】满网格 —— 上界是 2*(grid-1)^2。

    地形不能用 np.arange：那是关于 (x,y) 的平面（h = y*grid + x），RTIN 误差
    |(h[a]+h[b])/2 - h[mid]| 对平面恒为 0，而分裂判据是严格大于（0.0 > 0.0 为
    假），于是内部一个都不分，只有边界 inf 传上去的祖先分，实测 184 != 512。
    随机地形下除 4 个角点外每个格点都是某个三角形的中点且误差 > 0，因此必然
    分到底，512 是结构决定的，不是挑种子挑出来的。

    判据保持严格 > 而不是 >=，理由有三条（曾经写在这里的「>= 会让平坦地形
    全细分从而打死塌陷测试」是【错的】：那条塌陷测试用 max_error=1.0，平坦
    地形内部误差 0.0，0.0 >= 1.0 仍为假，换成 >= 后 5 条新测试全部照样通过）：
      1. brief 逐字规定了实现，改实现去迁就测试是禁止的；
      2. Martini 原始语义就是严格 >；
      3. 真正的危害在 max_error=0 这一点上 —— 实测 >= 会让完全平坦的瓦片从
         2 个三角形炸到满额 512，对最该减面的地形反而零减面。
    """
    grid = 17
    rng = np.random.default_rng(23)
    h = rng.random(grid * grid) * 100.0
    err = rtin_errors(h, grid, True)
    _, tris = rtin_extract(err, grid, 0.0)
    assert len(tris) == 2 * (grid - 1) * (grid - 1)


# ---------------------------------------------------------------------------
# 接入切片流程（Task 5）：max_error 的层级缩放 + build_terrain 的 triangulator 开关
# ---------------------------------------------------------------------------


def test_max_error_for_level_scales_with_vertex_spacing():
    """max_error 必须按层级缩放 —— 固定绝对值会让高层瓦片压成平面。

    设计阶段实测：max_error=20m 时 z14 瓦片只剩 2 个三角形。
    规则是 K * 顶点间距（米）。注意 DEG_TO_M 是赤道处 1° 经度的度长，只做量级
    换算 —— 「坡度误差容限恒定」这个说法在赤道以外不成立（东西向按 1/cos(纬度)
    偏宽松，详见 _max_error_for_level 的 docstring）。这条测试钉的是**逐级翻倍**
    这一层，与纬度无关。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import _max_error_for_level

    e14 = _max_error_for_level(14, 65, 0.15)
    e13 = _max_error_for_level(13, 65, 0.15)
    assert e13 == pytest.approx(e14 * 2, rel=1e-9), "每降一级，容限应翻倍"
    assert e14 == pytest.approx(0.15 * (180.0 / (1 << 14)) / 64 * 111320.0, rel=1e-9)


def test_build_terrain_martini_produces_fewer_triangles_than_grid():
    """同一份地形，martini 的瓦片必须比 grid 小。"""
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    n = 65
    rng = np.random.default_rng(1)
    heights = (rng.random((n, n)) * 400.0).astype(np.float64)

    grid_bytes = ct.encode_quantized_mesh(86.0, 41.0, 86.01, 41.01, heights)
    err = rtin_errors(heights.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=8.0)
    mart_bytes = ct.encode_quantized_mesh(86.0, 41.0, 86.01, 41.01, heights,
                                          mesh=(verts, tris))
    assert len(mart_bytes) < len(grid_bytes)

    off = 88 + 4 + len(verts) * 2 * 3
    (tri_count,) = struct.unpack_from("<I", mart_bytes, off)
    assert tri_count == len(tris)


def _terrain_tile_stats(out_dir) -> dict:
    """从落盘的 .terrain 里按 quantized-mesh-1.0 spec 读出 (顶点数, 三角形数, 四条边的顶点数)。

    刻意只解析到 EdgeIndices 为止：这条测试要回答的是「martini 有没有真的走到
    编码里、边界有没有真的钉住」，不是重验编码正确性（那是
    test_fix_terrain_mesh_indices 的活）。

    索引宽度按 spec 由**顶点数**决定（>65536 用 uint32）—— 这里的
    tile_size 恒 ≤ 65（4225 顶点）走 uint16，宽度仍照 spec 算，免得将来
    有人加大 tile_size 时这个解析器悄悄错位。

    EdgeIndices 之后还要走一遍扩展段（build_terrain 默认 normals=True，每张
    瓦片末尾都有 extensionId=1 的 oct-encoded 法线段）。推进方式与 Cesium 的
    解析循环一致：`off += extensionLength`。这条路径是**唯一**一处从
    build_terrain 完整跑到磁盘再读回来的检查，所以顺带把「法线段真的落盘了、
    长度恰好是 vertexCount*2」也钉在这里 —— _worker_tile 那一层的守卫在
    test_terrain_normals.py，管不到 build_terrain 的透传。
    """
    import gzip

    stats = {}
    for p in sorted(out_dir.rglob("*.terrain")):
        with gzip.open(p, "rb") as f:
            data = f.read()
        (vcount,) = struct.unpack_from("<I", data, 88)
        off = 88 + 4 + vcount * 6
        (tcount,) = struct.unpack_from("<I", data, off)
        width = 4 if vcount > 65536 else 2
        off += 4 + tcount * 3 * width
        edges = []
        for _ in range(4):
            (ecount,) = struct.unpack_from("<I", data, off)
            edges.append(ecount)
            off += 4 + ecount * width
        ext_ids = []
        while off < len(data):
            ext_id, ext_len = struct.unpack_from("<BI", data, off)
            ext_ids.append(ext_id)
            if ext_id == 1:
                assert ext_len == 2 * vcount, (
                    f"{p}: 法线段长度 {ext_len} != 2*顶点数 {2 * vcount}")
            off += 5 + ext_len
        assert ext_ids == [1], f"{p}: 扩展段应恰好是一个 oct 法线段，实得 {ext_ids}"
        assert off == len(data), f"{p}: 字节流没被精确消费完（off={off} len={len(data)}）"
        stats[str(p.relative_to(out_dir))] = (vcount, tcount, tuple(edges))
    return stats


def _write_smooth_dem(path, px=64, deg=0.05, west=100.0, south=30.0):
    """平滑起伏的 DEM：白噪声会让 RTIN 处处都得细分，减面看不出来。"""
    from osgeo import gdal

    ds = gdal.GetDriverByName("GTiff").Create(str(path), px, px, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg, 0.0, south + px * deg, 0.0, -deg))
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    ds.GetRasterBand(1).WriteArray(500.0 + 300.0 * np.sin(xx / 9.0) * np.cos(yy / 11.0))
    ds.FlushCache()
    ds = None


@pytest.mark.parametrize("tile_size", [17, 65])
def test_martini_reduces_triangles_and_pins_the_border_grid_is_a_real_fallback(tmp_path, tile_size):
    """接线测试：martini 必须真的减面、边界必须满密度、grid 能真退回。

    上面两条测试全都直接调 encode_quantized_mesh，一行都没碰 _worker_tile /
    _iter_tasks / build_terrain 的参数透传 —— 也就是说「参数加了但 worker 根本
    没用上」这种失效它们一条都抓不住。而这正是本项目反复踩到的形态：
    HTTP 全 200、任务 completed、前端不报错、地形却是老样子。

    所以这里跑真实的 build_terrain 落盘，再从字节流里读回来：
      - grid 分支每张瓦片恒为 2*(tile_size-1)^2 个三角形
      - martini 分支必须严格更少
      - **martini 的四条边必须各有 tile_size 个顶点（满密度）**

    满密度那条守的是 _worker_tile 里的 `pin_border=True`：它是跨瓦片无缝的
    唯一保证（设计稿:86），但在此之前**没有任何测试守着生产路径真的传了 True**。
    实测把它改成 False：三角形从 14720 塌到 194（-98.7%）、公共边顶点数从
    65 掉到 2..5、12 对东西相邻瓦片里有 1 对顶点集合对不上（真裂缝），
    而全量 1021 条测试一条不红。边长断言是这个洞最直接的封条 ——
    pin_border 一关，边长立刻从 tile_size 掉到个位数。

    tile_size 参数化到 65 是因为生产值就是 65（TileParams.tile_size），
    mesh= 路径此前只在 17 下跑过。

    「不传 triangulator 时默认值真的生效」这条原本也在这里，现已挪到
    test_auto_never_writes_more_bytes_than_either_backend。

    ⚠️ 挪走的理由曾经写错过，这里记下**实测的**版本，免得后人照着错理由把它挪回来：
    这份 DEM + min_level=0/max_level=2 下，auto 的落点是
    **chose_martini=0 / chose_grid=42**（tile_size=17 与 65 都一样，auto 产物与
    显式 grid 42/42 逐字节相同、与显式 martini 0/42 相同）—— 也就是说旧断言
    「默认产物 == 显式 martini」在默认值改成 'auto' 的那一刻**会当场变红**，
    它完全区分得出 auto 和 martini。所以挪走**不是因为它失效了**，而是因为
    在混合 DEM 上与显式 'auto' 比是更强的写法（那里 auto 与两个单一后端都不同）。
    早先写的「auto 张张都选 martini 所以断言等于失效」把因果说反了。

    为什么这份 DEM 上是 grid 全胜、以及择优为什么必要（实测天山 N42E086
    z0-11 共 907 张瓦片，按瓦片内高差分桶，martini/grid 的 gzip 字节比中位数）：
        高差 <1 m      1 张   比值 1.93   martini 胜 0
        高差 1-30 m    704 张 比值 1.93   martini 胜 14
        高差 30-100 m  17 张  比值 0.37   martini 胜 15
        高差 100-300 m 20 张  比值 0.40   martini 胜 20
        高差 300-800 m 25 张  比值 1.36   martini 胜 11
        高差 >800 m    140 张 比值 1.76   martini 胜 15
    是**两头都输、只赢中间带**：平坦瓦片上 grid 的 u/v 是等差 zigzag-delta、
    高程全相等，整条流几乎零熵，gzip 压到几百字节；而 martini 有 pin_border 撑着
    的地板（实测顶点数中位恒为 589，压不下去），u/v 还被打散成高熵数据 ——
    所以**越平坦 grid 赢得越干脆**。另一头山地则是减面不够（gzip 后 martini 每个
    三角形贵 4.43 倍，减面要 >77.4% 才打平），同样输。
    这份 DEM 在 z0-2 上绝大多数瓦片是平的或越界的（min_level=0 强制出全球图，
    3.2°×3.2° 的 DEM 铺到 z0-z2），正好落在左端那一段。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    dem = tmp_path / "dem.tif"
    _write_smooth_dem(dem)

    kw = dict(min_level=0, max_level=2, tile_size=tile_size, workers=1)
    ct.build_terrain([str(dem)], str(tmp_path / "grid"), triangulator="grid", **kw)
    ct.build_terrain([str(dem)], str(tmp_path / "mart"), triangulator="martini", **kw)

    grid_stats = _terrain_tile_stats(tmp_path / "grid")
    mart_stats = _terrain_tile_stats(tmp_path / "mart")
    assert grid_stats and set(grid_stats) == set(mart_stats)

    full = 2 * (tile_size - 1) ** 2
    assert all(t == full for _, t, _ in grid_stats.values()), (
        f"grid 分支应恒为满网格 {full} 个三角形，实得 {sorted({t for _, t, _ in grid_stats.values()})}"
    )
    assert all(v == tile_size * tile_size for v, _, _ in grid_stats.values())

    assert sum(t for _, t, _ in mart_stats.values()) < sum(t for _, t, _ in grid_stats.values()), (
        "martini 分支没有减面 —— triangulator 参数很可能没透传到 _worker_tile"
    )
    for key, (_, t_m, _) in mart_stats.items():
        assert t_m < full, f"{key}: martini 三角形数 {t_m} 未少于满网格 {full}"

    # pin_border=True 的封条：自适应分支的四条边必须一个顶点都不少。
    for key, (_, _, edges) in mart_stats.items():
        assert edges == (tile_size,) * 4, (
            f"{key}: martini 四条边的顶点数 {edges} 不是满密度 {tile_size} —— "
            f"_worker_tile 的 pin_border 很可能被关掉了，跨瓦片会裂缝"
        )
    # grid 分支同样必须满密度（它本来就是满网格，这条顺带钉住边索引没写漏）。
    for key, (_, _, edges) in grid_stats.items():
        assert edges == (tile_size,) * 4, f"{key}: grid 四条边顶点数 {edges} 异常"


def test_build_terrain_rejects_unknown_triangulator(tmp_path):
    """triangulator 拼错必须在入口就报，不能静默退回规则网格。

    这是「默认开自适应」新引入的失败面：'martni' 会走 _worker_tile 的 else
    分支出满网格瓦片，作业 rendered==total 完美完成，没有任何信号说自适应根本
    没开 —— 又一款「HTTP 200 + completed + 前端不报错」。而且它在 _worker_tile
    的 try/except 里【根本不会报错】（else 是合法路径），所以校验必须在入口。

    输入文件故意给不存在的路径：校验必须在任何 I/O 之前触发。谁把它挪到
    build_input_raster 之后，这里拿到的就是 GDAL 的错误而不是 ValueError，当场变红。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    out = tmp_path / "out"
    with pytest.raises(ValueError, match=r"triangulator.*'martni'"):
        ct.build_terrain([str(tmp_path / "nope.tif")], str(out), triangulator="martni")
    assert not out.exists(), "校验应在建输出目录之前就拦下，不该留下半个产物目录"

    # 'auto' 必须在白名单里 —— 少了它，生产默认值自己会被入口校验拒掉。
    with pytest.raises(Exception) as ei:
        ct.build_terrain([str(tmp_path / "nope.tif")], str(out), triangulator="auto")
    assert "triangulator" not in str(ei.value), (
        f"'auto' 被入口校验拒了（应该越过校验死在读不到输入文件上），实得 {ei.value!r}"
    )


@pytest.mark.parametrize("triangulator", [None, "auto", "martini"])
def test_build_terrain_rejects_tile_size_the_adaptive_path_cannot_handle(tmp_path, triangulator):
    """自适应路径要求 tile_size = 2^k+1；不满足时必须入口即报，且错误信息点名 tile_size。

    实测 tile_size=64 时 rtin_tables 逐瓦片抛 ValueError，被 _worker_tile 的
    容错吞成 warning：rendered=0 / failed=42 外加 42 行刷屏，错误离病因很远。
    生产路径恒为 65（TileParams.tile_size）不受影响，但 CLI 的 --tile-size 收
    任意整数。

    **'auto' 也必须报错，不许静默降级成纯 grid**：静默降级正是这个项目栽过
    三次的失败形态（作业完美完成、什么都不显示）。生产默认 tile_size=65 是
    合法的，能触发这条说明是配置错了，应该暴露。

    triangulator=None 那一组走的是默认值 —— 默认值必须跟显式 'auto' 同样严。

    tile_size=64 在 grid 下是合法的，所以这条校验必须只在自适应路径生效 ——
    末尾那次调用就是钉这一点的（若把校验写成无条件，它会变红）。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    out = tmp_path / "out"
    kw = {} if triangulator is None else {"triangulator": triangulator}
    with pytest.raises(ValueError, match=r"tile_size.*64|64.*tile_size"):
        ct.build_terrain([str(tmp_path / "nope.tif")], str(out), tile_size=64, **kw)
    assert not out.exists(), "校验应在建输出目录之前就拦下，不该留下半个产物目录"

    # grid 分支不受 2^k+1 约束：这里必须越过校验，死在读不到输入文件上。
    with pytest.raises(Exception) as ei:
        ct.build_terrain([str(tmp_path / "nope.tif")], str(out),
                         tile_size=64, triangulator="grid")
    assert "tile_size" not in str(ei.value), (
        f"grid 分支不该被 tile_size 校验拦下，实得 {ei.value!r}"
    )


# ---------------------------------------------------------------------------
# 逐瓦片择优（Task 5b）：auto = 两个后端都编一遍，取 gzip 后更小的那个落盘
# ---------------------------------------------------------------------------


def test_choose_tile_bytes_takes_the_smaller_and_breaks_ties_toward_martini():
    """择优判据：比 **gzip 后**的长度取小；相等时取 martini。

    平局取 martini 不是随手写的分支顺序，是有理由的（顶点更少 => Cesium 侧
    内存占用与 GPU 上传更省），所以要钉住。判据反向（取大）会让整个方案变成
    「每张瓦片都挑更差的那个」，而全局字节仍然只是变大不会崩 —— 没有断言的话
    这又是一款零信号失效。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import _choose_tile_bytes

    assert _choose_tile_bytes(b"a", b"bb") == (b"a", "martini")
    assert _choose_tile_bytes(b"aaa", b"bb") == (b"bb", "grid")
    assert _choose_tile_bytes(b"aa", b"bb") == (b"aa", "martini"), "平局必须取 martini"


def _write_mixed_dem(path, px=256, deg=0.02, west=100.0, north=36.0):
    """左半平滑、右半白噪声的 DEM —— 逼 auto 在两个后端上都真的选到。

    为什么要混合：单一地形的 DEM 上很容易一个后端通吃，那时「逐瓦片」这三个字
    没有被验证过 —— 一个把选择提到瓦片外面（整个任务选一次）的实现照样全绿。
    实测：全白噪声上 grid 通吃（减面不够，gzip 后 martini 每个三角形贵 4.43 倍）；
    而**平坦地形上也是 grid 通吃**（grid 的流几乎零熵，martini 有 pin_border
    撑出的顶点地板还带高熵 u/v）。martini 只在「有起伏但不剧烈」的中间带赢
    —— 分桶实测见 test_martini_reduces_triangles_and_pins_the_border_grid_is_a_real_fallback
    的 docstring。所以这里必须把平滑区的**尺度**调到那个中间带上
    （sin/cos 周期 21/17 像素、振幅 300 m），不能随手写个平面。
    """
    from osgeo import gdal

    ds = gdal.GetDriverByName("GTiff").Create(str(path), px, px, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg, 0.0, north, 0.0, -deg))
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    smooth = 500.0 + 300.0 * np.sin(xx / 21.0) * np.cos(yy / 17.0)
    rough = 1500.0 + np.random.default_rng(7).random((px, px)).astype(np.float32) * 900.0
    ds.GetRasterBand(1).WriteArray(np.where(xx < px // 2, smooth, rough).astype(np.float32))
    ds.FlushCache()
    ds = None


def _tile_files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.terrain"))}


def test_auto_never_writes_more_bytes_than_either_backend(tmp_path):
    """**方案的立身之本**：auto 每张瓦片的落盘字节严格 ≤ min(grid, martini)。

    背景（实测 112584 张配对瓦片）：瓦片是 gzip 落盘、gzip 原样上线，所以
    gzip 后的字节既是磁盘占用也是传输量。gzip 后 grid ≈0.91 字节/三角形、
    martini ≈4.04 字节/三角形，于是「减面 >77.4% 才在字节上打平」—— 山地做到
    74.8% 就翻成净损失（+17.6%）。逐瓦片择优把变大的那部分全部避免。

    这条测试同时钉住四件事：
      1. 字节不可能变差（≤ min）；
      2. auto 的产物是某一个分支的**逐字节副本**，不是某种混合产物；
      3. 选择真的是**逐瓦片**的 —— 同一次任务里两个后端都被选中过；
      4. 不传 triangulator 时默认值真的是 'auto'。第 3 条保证了这份 DEM 上
         auto 与两个单一后端**都不相同**，所以「默认产物 == 显式 auto」在这里
         真的区分得开 'auto'/'martini'/'grid' 三者。

    第 4 条为什么要用这份混合 DEM，而不是留在
    test_martini_reduces_triangles_and_pins_the_border_grid_is_a_real_fallback
    那份平滑 DEM 上：那边实测是 **chose_martini=0 / chose_grid=42**，auto 与显式
    grid 逐字节相同 —— 所以在那边其实只能钉住「默认不是 martini」，钉不住
    「默认不是 grid」。这里三者两两不同，才是三选一都拦得住。
    （早先这里写的是「平滑 DEM 上 auto 张张选 martini 所以区分不出」，方向反了；
     真实机制见那条测试的 docstring 里按高差分桶的实测表。）
    """
    import gzip as _gzip

    from src.services.terrain_tiling import cesiumlab_terrain as ct

    dem = tmp_path / "mixed.tif"
    _write_mixed_dem(dem)

    # min_level=6 跳过 `z<=4` 的强制全球图；z6/z7 的瓦片跨度（2.8125°/1.40625°）
    # 小于 DEM 的半幅（2.56°），平滑区与噪声区各自都有整块落在内部的瓦片。
    kw = dict(min_level=6, max_level=7, tile_size=65, workers=1)
    c_grid = ct.build_terrain([str(dem)], str(tmp_path / "g"), triangulator="grid", **kw)
    c_mart = ct.build_terrain([str(dem)], str(tmp_path / "m"), triangulator="martini", **kw)
    c_auto = ct.build_terrain([str(dem)], str(tmp_path / "a"), triangulator="auto", **kw)
    ct.build_terrain([str(dem)], str(tmp_path / "d"), **kw)  # 不传 = 默认值

    g = _tile_files(tmp_path / "g")
    m = _tile_files(tmp_path / "m")
    a = _tile_files(tmp_path / "a")
    d = _tile_files(tmp_path / "d")
    assert g and set(g) == set(m) == set(a) == set(d), "各后端产出的瓦片集合必须一致"
    assert d == a, "默认产物与 triangulator='auto' 不一致 —— 默认值没生效"

    picked = {"martini": 0, "grid": 0}
    for key in sorted(a):
        assert len(a[key]) <= min(len(g[key]), len(m[key])), (
            f"{key}: auto 落盘 {len(a[key])} 字节 > min(grid={len(g[key])}, "
            f"martini={len(m[key])}) —— 「不可能变差」的保证被打破"
        )
        # 必须是某一个分支的逐字节副本：既排除混合产物，也排除「重新压一遍」
        # 这类看起来无害、实则让产物不可复现的实现。
        if a[key] == m[key]:
            picked["martini"] += 1
        elif a[key] == g[key]:
            picked["grid"] += 1
        else:
            raise AssertionError(
                f"{key}: auto 产物既不等于 grid 也不等于 martini —— "
                f"解压后 {'相同' if _gzip.decompress(a[key]) in (_gzip.decompress(g[key]), _gzip.decompress(m[key])) else '也不同'}"
            )

    assert picked["martini"] > 0 and picked["grid"] > 0, (
        f"同一次任务里两个后端都该被选中过，实得 {picked} —— 择优很可能不是逐瓦片做的"
    )

    # 计数必须与逐张实测的选择完全对得上（不是「算了个大概」）。
    assert (c_auto["chose_martini"], c_auto["chose_grid"]) == (picked["martini"], picked["grid"]), (
        f"build_terrain 报的 {c_auto['chose_martini']}/{c_auto['chose_grid']} 与实际落盘 "
        f"{picked['martini']}/{picked['grid']} 对不上"
    )
    assert c_auto["chose_martini"] + c_auto["chose_grid"] == c_auto["rendered"]

    # 强制单一后端时，计数必须全落在那一侧（否则统计的语义是错的）。
    assert (c_grid["chose_grid"], c_grid["chose_martini"]) == (c_grid["rendered"], 0)
    assert (c_mart["chose_martini"], c_mart["chose_grid"]) == (c_mart["rendered"], 0)


def test_tiles_are_written_with_a_fixed_gzip_timestamp(tmp_path):
    """gzip 头的 4 字节 mtime 必须恒为 0 —— 同输入必须同字节。

    此前用 `gzip.open(tile_file, "wb")` 落盘，头里写的是 time.time()，于是
    同一份数据两次切片的磁盘字节不同。实测后果：一条比较磁盘字节的接线测试
    在干净树上连跑 8 次挂 1 次（差异恰好落在头部第 4 字节）。当时是在测试层
    绕开的（改比解压后的字节流），生产代码的根源留到这里才修 —— 而逐瓦片
    择优必须先在内存里压两次比大小，正好顺手改掉。

    直接断言头部字段而不是「跑两次比字节」：后者只有在两次运行跨秒时才红，
    是随机红灯；这条是确定性的。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    dem = tmp_path / "dem.tif"
    _write_smooth_dem(dem)
    out = tmp_path / "tiles"
    ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1,
                     tile_size=17, workers=1)

    files = sorted(out.rglob("*.terrain"))
    assert files, "没产出瓦片，这条测试等于没跑"
    for p in files:
        raw = p.read_bytes()
        assert raw[:2] == b"\x1f\x8b", f"{p.name}: 不是 gzip 流"
        (mtime,) = struct.unpack_from("<I", raw, 4)
        assert mtime == 0, (
            f"{p.name}: gzip 头 mtime={mtime} 不是 0 —— 同输入会产出不同字节"
        )


def test_triangulation_defaults_agree_across_every_copy(tmp_path, monkeypatch):
    """三角化默认值有多份副本。K 必须处处一致；后端**有意分叉**，只钉合法性。

    历史上三份 triangulator 默认值（TileParams / build_terrain 签名 / CLI
    argparse）被钉成同一个值。三档预设之后这个前提不再成立：
      - 应用侧 TileParams -> 'grid'（用户任务，实测 auto 不在 Pareto 前沿）
      - CLI / 全球底图    -> 'auto'（一次性构建，覆盖海洋与大片平原）
    依据见 docs/reference/terrain/tiling-presets-measured.md 第三节与第八节。

    分叉之后这里还能钉的是：两份默认值都必须是 build_terrain 白名单里的值。
    拼错会静默退回规则网格（build_terrain 入口校验的注释里记着这个坑），
    而白名单本身由 test_build_terrain_rejects_unknown_triangulator 守。
    两个具体字面量各有唯一的家，不在这里抄第三份：
      'grid' -> tests/test_dem_task_tiler.py（TileParams -> build_terrain 透传）
      'auto' -> tests/test_build_scripts_contract.py（全球底图脚本走 CLI 默认）

    K 这里刻意不写死 0.15：唯一的字面量归 test_dem_task_tiler。
    """
    import inspect

    from src.services.terrain_tiling import cesiumlab_terrain as ct
    from src.services.terrain_tiling.dem_task_tiler import TileParams

    sig = inspect.signature(ct.build_terrain)
    params = TileParams(maxzoom=0, parent_url="x")

    assert sig.parameters["max_error_k"].default == ct.DEFAULT_MAX_ERROR_K, (
        f"build_terrain 的 max_error_k 默认值 {sig.parameters['max_error_k'].default} "
        f"与 DEFAULT_MAX_ERROR_K {ct.DEFAULT_MAX_ERROR_K} 不一致"
    )
    assert params.max_error_k == ct.DEFAULT_MAX_ERROR_K, (
        f"TileParams.max_error_k {params.max_error_k}（生产实际用的那份）与 "
        f"DEFAULT_MAX_ERROR_K {ct.DEFAULT_MAX_ERROR_K} 不一致"
    )
    accepted = ("auto", "martini", "grid")
    assert params.triangulator in accepted, (
        f"TileParams.triangulator {params.triangulator!r} 不是 build_terrain "
        f"接受的值，会被入口校验拒掉")

    # CLI 的 K 必须与 DEFAULT_MAX_ERROR_K 同源，后端只钉合法性（有意分叉，
    # 'auto' 这个字面量的家在 tests/test_build_scripts_contract.py）。走真实的
    # main()（只替掉 build_terrain）而不是去翻 parser 的内部结构 —— 顺带钉住了
    # CLI 到 build_terrain 的透传。
    captured = {}

    def fake_build_terrain(inputs, output, **kw):
        captured.update(kw)

    src = tmp_path / "x.tif"
    src.write_bytes(b"")
    monkeypatch.setattr(ct, "build_terrain", fake_build_terrain)
    assert ct.main(["-i", str(src), "-o", str(tmp_path / "out")]) == 0

    assert captured["triangulator"] in accepted, (
        f"CLI --triangulator 默认 {captured['triangulator']!r} 不是 build_terrain "
        f"接受的值")
    assert captured["max_error_k"] == ct.DEFAULT_MAX_ERROR_K, (
        f"CLI --max-error-k 默认 {captured['max_error_k']} 与 DEFAULT_MAX_ERROR_K 不一致"
    )
