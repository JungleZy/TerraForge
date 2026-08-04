"""RTIN（Right-Triangulated Irregular Network）自适应三角化。

Mapbox Martini 算法的 numpy 实现。相比固定规则网格，按高程误差驱动细分，
标准档（K=0.15）减面 73.7%（山地）~82.7%（平缓）。

两个关键设计（改动前务必读 docs/superpowers/specs/2026-08-04-terrain-triangulation-design.md）：

1. **误差按 RTIN 层级批量向量化**。同层三角形彼此独立，12 个层级用
   np.maximum.at 批处理即可，实测比逐三角形循环快 379x（37.47ms -> 0.099ms）。

2. **边界满密度靠 pin_border**，且 inf 必须在自底向上传播【之前】注入 ——
   RTIN 的误差是从最细层往粗层传的，设在传播之后约束到不了祖先，边界约束
   会完全失效（设计阶段因此测出 93.4% 的假减面率，真值 84.8%）。
   边界全保留后，相邻瓦片公共边顶点集合逐点一致，实测 2880/2880 无一例外。
"""

from __future__ import annotations

import functools

import numpy as np


@functools.lru_cache(maxsize=None)
def rtin_tables(grid: int):
    """按 RTIN 层级分组的索引表。只依赖 grid，因此可以缓存。

    返回 {level: [a, b, mid, left_child, right_child]}，五个都是格点线性索引
    的 int64 数组。a/b 是三角形斜边两端，mid 是斜边中点（即该三角形的分裂点）。

    数组一律置为只读：结果被 lru_cache 全进程共享，任何一个消费者就地改一个
    元素都会永久污染后续所有调用者拿到的表。
    """
    t = grid - 1
    # t >= 2：grid=2 形式上是合法的 2^0+1，但退化到 0 个三角形，放过去的话
    # rtin_errors 会在 levels[0] 抛 IndexError —— 报错点离病因十万八千里。
    if t < 2 or (t & (t - 1)) != 0:
        raise ValueError(f"grid must be 2^k+1 and >= 3, got {grid}")
    num_tri = t * t * 2 - 2
    buckets: dict[int, list[list[int]]] = {}
    for i in range(num_tri):
        lvl = int(np.log2(i + 2))
        # 从隐式二叉树下标还原三角形的三个顶点坐标
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
        b = buckets.setdefault(lvl, [[], [], [], [], []])
        b[0].append(ay * grid + ax)
        b[1].append(by * grid + bx)
        b[2].append(my * grid + mx)
        b[3].append(((ay + ccy) >> 1) * grid + ((ax + ccx) >> 1))
        b[4].append(((by + ccy) >> 1) * grid + ((bx + ccx) >> 1))
    tables = {}
    for lvl, v in buckets.items():
        arrays = []
        for x in v:
            arr = np.array(x, np.int64)
            arr.flags.writeable = False
            arrays.append(arr)
        tables[lvl] = arrays
    return tables


def rtin_errors(heights_flat: np.ndarray, grid: int, pin_border: bool = True) -> np.ndarray:
    """逐格点的 RTIN 误差。heights_flat 是行优先展平的 grid*grid 高程。

    pin_border=True 时把四条边的误差置 inf，强制这些顶点全部保留 —— 这是
    跨瓦片无缝的唯一保证，不要改成传播之后再设。
    """
    tables = rtin_tables(grid)
    errors = np.zeros(grid * grid, dtype=np.float64)
    if pin_border:
        e2 = errors.reshape(grid, grid)
        e2[0, :] = np.inf
        e2[-1, :] = np.inf
        e2[:, 0] = np.inf
        e2[:, -1] = np.inf
    h = np.asarray(heights_flat, dtype=np.float64)
    levels = sorted(tables, reverse=True)
    deepest = levels[0]
    for lvl in levels:
        a, b, mid, lc, rc = tables[lvl]
        np.maximum.at(errors, mid, np.abs((h[a] + h[b]) * 0.5 - h[mid]))
        if lvl < deepest:
            np.maximum.at(errors, mid, np.maximum(errors[lc], errors[rc]))
    return errors


def rtin_extract(errors: np.ndarray, grid: int, max_error: float):
    """按 max_error 从误差表提取三角网。

    返回 (vertex_grid_indices, triangles)：
      - vertex_grid_indices: 保留顶点的格点线性索引，**按首次出现顺序**排列。
        这个顺序不是随意的 —— quantized-mesh 的 high-water-mark 索引编码要求
        顶点按首次出现顺序编号才能向量化（见 cesiumlab_terrain._hwm_encode）。
      - triangles: (M,3) 的局部索引，值域 [0, len(vertex_grid_indices))。

    递归深度只有 2*log2(grid-1)（grid=65 时 12 层），不必改写成迭代。
    """
    idmap: dict[int, int] = {}
    tris: list[list[int]] = []
    mx_ = grid - 1

    def rec(ax, ay, bx, by, cx, cy):
        mx = (ax + bx) >> 1
        my = (ay + by) >> 1
        if abs(ax - cx) + abs(ay - cy) > 1 and errors[my * grid + mx] > max_error:
            rec(cx, cy, ax, ay, mx, my)
            rec(bx, by, cx, cy, mx, my)
        else:
            tri = []
            # 发射顺序是 a -> c -> b，不是 a -> b -> c：后者的有向面积恒为负
            # （两个顶层 rec 的有向面积都是 -mx_^2/2，对半分裂时符号不变，
            # 于是整棵树全 CW），与规则网格分支（_mesh_constants 的 [a0,a1,a2]
            # / [a1,a3,a2] 切法，实测 512/512 全正）方向相反。下游按三角形绕向
            # 算顶点法线，反了会让法线整体翻转 —— Cesium 开背面剔除后地形直接
            # 不可见，而且是 HTTP 全 200、任务 completed、前端不报错的静默失败。
            for px, py in ((ax, ay), (cx, cy), (bx, by)):
                p = py * grid + px
                idx = idmap.get(p)
                if idx is None:
                    idx = len(idmap)
                    idmap[p] = idx
                tri.append(idx)
            tris.append(tri)

    rec(0, 0, mx_, mx_, mx_, 0)
    rec(mx_, mx_, 0, 0, 0, mx_)

    verts = np.empty(len(idmap), dtype=np.int64)
    for p, i in idmap.items():
        verts[i] = p
    return verts, np.array(tris, dtype=np.int64)
