"""RTIN（Martini）误差表与误差计算的单元测试。

纯 numpy，不需要 GDAL / 采样，因此不做任何 monkey-patch。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.rtin import rtin_errors, rtin_tables


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
