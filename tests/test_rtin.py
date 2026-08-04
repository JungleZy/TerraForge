"""RTIN（Martini）误差表与误差计算的单元测试。

纯 numpy，不需要 GDAL / 采样，因此不做任何 monkey-patch。
"""

import os
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
