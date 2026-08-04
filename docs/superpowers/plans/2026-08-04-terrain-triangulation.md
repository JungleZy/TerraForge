# 地形自适应三角化与逐顶点法线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让地形切片用误差驱动的自适应三角化替代固定 65×65 规则网格（减面 73.7%~82.7%），并给每个顶点写入法线数据以支持光照。

**Architecture:** 两个互不依赖的批次。批次 A（Task 1-5）把 Martini/RTIN 接进 `cesiumlab_terrain.py`——注意其中的顶点重排与索引编码向量化**与自适应绑定，不可单独做**（理由见 Global Constraints）。批次 B（Task 6-8）加法线，走满网格 + ghost cells 路径，与批次 A 正交。Task 9 是前端。两个批次可以任意顺序做，也可以只做一个。

**Tech Stack:** Python 3.12 · numpy 1.26 · GDAL 3.8（仅采样用）· CesiumJS 1.143 · 无新增第三方依赖

**上级设计稿：** [`../specs/2026-08-04-terrain-triangulation-design.md`](../specs/2026-08-04-terrain-triangulation-design.md) —— 全部数字与选型依据在那里，**开工前至少读「结论先行」「插曲」两节**。

---

## Global Constraints

以下对**每个任务**生效：

- **`build_terrain` 的签名与返回契约不得改变**：`build_terrain(inputs, output_dir, *, min_level, max_level, tile_size, nodata, workers, progress_cb, stop_flag) -> {"total","rendered","failed"}`。新参数一律加在 `*` 之后并带默认值。`progress_cb(done, total)` 的调用时机、`stop_flag` 的检查点（串行每瓦片 / 并行批间）都不能动。
- **12 个测试文件不能破坏**：`test_dem_task_tiler.py`、`test_fix_terrain_pool_robustness.py`、`test_fix_terrain_vrt_cleanup.py`、`test_fix_terrain_gdal_import.py`、`test_fix_terrain_mesh_indices.py`、`test_fix_terrain_estimate_max_level.py`、`test_fix_terrain_sampler_resample.py`、`test_fix_terrain_static_gzip.py`、`test_fix_dem_start_tiling_race.py`、`test_fix_infra_e.py`、`test_local_terrain_api.py`、`test_pipeline_endpoints.py`。每个任务收尾都要跑全量 `uv run pytest tests/ -q`（约 7.5 分钟，969 passed 是基线）。
- **K 固定 0.15**，不做成用户可选。`max_error(z) = 0.15 * (180/2^z)/(grid-1) * 111320`。
- **DB schema 与 `/api/terrain/dem/<id>/start` 一律不动。** 切片参数不暴露给用户。
- **`triangulator` / `normals` 只作为 `build_terrain` 的默认参数存在**，UI/DB/API 全部不可见。用途是排障与测试注入。
- **顶点重排 + 索引编码向量化必须跟 Task 2 一起做。** 实测：规则网格下做重排会让索引段 gzip 从 233 B 涨到 613 B；只有换成 Martini（索引无周期性）后这个代价才消失。反过来，Martini 的拓扑逐瓦片变化会让 `_mesh_constants` 的 `lru_cache` 失效，纯 Python 的 `_high_water_mark_encode` 会变成每瓦片对约 24576 个元素跑一遍循环——所以向量化是刚需。**两件事绑死。**
- **不要动 `EdgeIndices` 的 `len(edge)`**：那几个字段 spec 定义就是顶点数，当前写法是对的。旁边的 `triangleCount` 才是 2026-08-04 修过的坑（`// 3`）。
- 用 `uv run` 执行所有 Python 命令，不要手动 activate venv。

---

## File Structure

| 文件 | 责任 | 本计划中的变化 |
|---|---|---|
| `src/services/terrain_tiling/rtin.py` | **新建**。纯算法：RTIN 误差表、误差计算、网格提取。只依赖 numpy，不碰 GDAL / IO | Task 1-2 创建 |
| `src/services/terrain_tiling/cesiumlab_terrain.py` | 采样 + 编码 + 切片调度 | Task 3-7 修改 |
| `src/services/terrain_tiling/dem_task_tiler.py` | 任务目录 → 切片的适配层 | Task 5 加字段 |
| `tests/test_rtin.py` | **新建**。RTIN 算法的单元测试 | Task 1-2 |
| `tests/test_terrain_normals.py` | **新建**。法线与 ghost cells 的测试 | Task 6-7 |
| `static/js/map.js` | 地形 provider 与光照 | Task 8 |
| `static/js/terrain_lighting.js` | **新建**。光照开关的 localStorage 持久化（仿 `theme.js`） | Task 8 |
| `templates/index.html` | 地图工具栏 | Task 8 |

把 RTIN 拆成独立文件的理由：`cesiumlab_terrain.py` 已有 657 行且混合了采样/编码/调度三种职责，再塞 150 行算法会更难读；RTIN 是纯函数、无 IO、无 GDAL 依赖，独立后测试不需要任何 mock。

---

## Task 1: RTIN 误差表与误差计算

**Files:**
- Create: `src/services/terrain_tiling/rtin.py`
- Test: `tests/test_rtin.py`

**Interfaces:**
- Consumes: 无（本计划第一个任务）
- Produces:
  - `rtin_tables(grid: int) -> dict[int, list[np.ndarray]]` —— 按 RTIN 层级分组的索引表，每层是 `[a, b, mid, left_child, right_child]` 五个 int64 数组。带 `lru_cache`。
  - `rtin_errors(heights_flat: np.ndarray, grid: int, pin_border: bool = True) -> np.ndarray` —— 长度 `grid*grid` 的 float64 误差数组。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_rtin.py`：

```python
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
    测出 93.4% 的假减面率（真值 84.8%）。判据：pin 之后，至少一个
    非边界的粗层分裂点也应变成 inf（因为它的子孙里含边界点）。
    """
    grid = 17
    rng = np.random.default_rng(0)
    h = rng.random(grid * grid) * 100.0
    err = rtin_errors(h, grid, pin_border=True).reshape(grid, grid)
    interior = err[1:-1, 1:-1]
    assert np.isinf(interior).any(), "inf 未向上传播到内部祖先节点"


def test_error_matches_naive_reference_on_random_terrain():
    """向量化实现必须与逐三角形的朴素参考实现逐元素相同。"""
    grid = 17
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'src.services.terrain_tiling.rtin'`

- [ ] **Step 3: 实现 `rtin.py`**

创建 `src/services/terrain_tiling/rtin.py`：

```python
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
    """
    t = grid - 1
    if t <= 0 or (t & (t - 1)) != 0:
        raise ValueError(f"grid must be 2^k+1, got {grid}")
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
    return {lvl: [np.array(x, np.int64) for x in v] for lvl, v in buckets.items()}


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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `uv run pytest tests/ -q`
Expected: 976 passed（969 基线 + 本任务新增 7）

- [ ] **Step 6: Commit**

```bash
git add src/services/terrain_tiling/rtin.py tests/test_rtin.py
git commit -m "feat(terrain): RTIN 误差表与按层级向量化的误差计算

同层三角形彼此独立，12 个层级用 np.maximum.at 批处理，实测比逐三角形循环
快 379x。表只依赖 grid 故带 lru_cache。

边界 inf 在自底向上传播【之前】注入 —— RTIN 误差从最细层往粗层传，设在
传播之后约束到不了祖先，边界满密度会完全失效。测试用逐三角形的朴素实现
对拍，逐元素相同。"
```

---

## Task 2: RTIN 网格提取（边界满密度）

**Files:**
- Modify: `src/services/terrain_tiling/rtin.py`
- Test: `tests/test_rtin.py`

**Interfaces:**
- Consumes: `rtin_errors(heights_flat, grid, pin_border) -> np.ndarray`（Task 1）
- Produces: `rtin_extract(errors: np.ndarray, grid: int, max_error: float) -> tuple[np.ndarray, np.ndarray]` —— 返回 `(vertex_grid_indices, triangles)`。前者是保留顶点的格点线性索引（int64，**按首次出现顺序排列**，这个顺序是 Task 3 的前提）；后者是 `(M,3)` int64 的三角形局部索引。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rtin.py`：

```python
from src.services.terrain_tiling.rtin import rtin_extract


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
    """
    grid = 17
    rng = np.random.default_rng(3)
    ha = rng.random(grid * grid) * 800.0
    hb = rng.random(grid * grid) * 800.0
    a2 = ha.reshape(grid, grid)
    b2 = hb.reshape(grid, grid)
    b2[:, 0] = a2[:, -1]          # 公共边高程一致
    va, _ = rtin_extract(rtin_errors(a2.reshape(-1), grid, True), grid, 5.0)
    vb, _ = rtin_extract(rtin_errors(b2.reshape(-1), grid, True), grid, 5.0)
    east_a = sorted(v // grid for v in va if v % grid == grid - 1)
    west_b = sorted(v // grid for v in vb if v % grid == 0)
    assert east_a == west_b == list(range(grid))


def test_extract_vertex_order_is_first_occurrence():
    """顶点必须按【首次出现顺序】编号 —— Task 3 的向量化 high-water-mark
    编码依赖这个规范形式，顺序错了编码就是错的。"""
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


def test_larger_max_error_yields_fewer_triangles():
    grid = 65
    rng = np.random.default_rng(5)
    err = rtin_errors(rng.random(grid * grid) * 300.0, grid, True)
    _, fine = rtin_extract(err, grid, 1.0)
    _, coarse = rtin_extract(err, grid, 50.0)
    assert len(coarse) < len(fine)
    assert len(fine) <= 2 * (grid - 1) * (grid - 1)


def test_triangle_count_never_exceeds_full_grid():
    grid = 17
    h = np.arange(grid * grid, dtype=np.float64)
    err = rtin_errors(h, grid, True)
    _, tris = rtin_extract(err, grid, 0.0)
    assert len(tris) == 2 * (grid - 1) * (grid - 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: FAIL —— `ImportError: cannot import name 'rtin_extract'`

- [ ] **Step 3: 实现 `rtin_extract`**

追加到 `src/services/terrain_tiling/rtin.py`：

```python
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
            for px, py in ((ax, ay), (bx, by), (cx, cy)):
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: PASS（12 passed）

- [ ] **Step 5: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 981 passed

- [ ] **Step 6: Commit**

```bash
git add src/services/terrain_tiling/rtin.py tests/test_rtin.py
git commit -m "feat(terrain): RTIN 网格提取，边界满密度保证跨瓦片无缝

边界 4*(grid-1) 个顶点全保留，相邻瓦片公共边顶点集合因此逐点一致 ——
测试直接构造两块共享公共边的地形来断言这一点。

顶点按【首次出现顺序】编号：这是 quantized-mesh high-water-mark 索引
编码可向量化的前提，不是随意的实现细节。"
```

---

## Task 3: high-water-mark 编码向量化

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py`（`_high_water_mark_encode` 附近）
- Test: `tests/test_fix_terrain_mesh_indices.py`

**Interfaces:**
- Consumes: `rtin_extract` 产出的规范顶点顺序（Task 2）
- Produces: `_hwm_encode(indices: np.ndarray) -> np.ndarray` —— 输入必须是规范形式（顶点按首次出现顺序编号），输出 uint32 编码数组。原 `_high_water_mark_encode` 保留不动，供规则网格分支使用。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_fix_terrain_mesh_indices.py`：

```python
from src.services.terrain_tiling.cesiumlab_terrain import (
    _high_water_mark_encode,
    _hwm_encode,
)


def test_vectorised_hwm_matches_loop_on_canonical_indices():
    """规范形式（顶点按首次出现顺序）下，向量化实现必须与循环版逐元素相同。"""
    rng = np.random.default_rng(2)
    for _ in range(20):
        n_tri = int(rng.integers(4, 400))
        n_vert = int(rng.integers(3, n_tri * 3))
        raw = rng.integers(0, n_vert, size=n_tri * 3)
        # 重排成规范形式：按首次出现顺序重新编号
        _, first = np.unique(raw, return_index=True)
        order = raw[np.sort(first)]
        remap = np.empty(int(raw.max()) + 1, np.int64)
        remap[order] = np.arange(len(order))
        canonical = remap[raw].astype(np.uint32)

        assert np.array_equal(_hwm_encode(canonical),
                              _high_water_mark_encode(canonical))


def test_vectorised_hwm_is_decodable():
    """编码后按 spec 的解码算法还原，必须得回原索引。"""
    canonical = np.array([0, 1, 2, 1, 3, 2, 4, 3, 1], dtype=np.uint32)
    enc = _hwm_encode(canonical)
    highest = 0
    out = []
    for code in enc:
        v = (highest - int(code)) % (1 << 32)
        out.append(v)
        if v == highest:
            highest += 1
    assert out == list(canonical)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_fix_terrain_mesh_indices.py -q`
Expected: FAIL —— `ImportError: cannot import name '_hwm_encode'`

- [ ] **Step 3: 实现向量化编码**

在 `src/services/terrain_tiling/cesiumlab_terrain.py` 的 `_high_water_mark_encode` 之后插入：

```python
def _hwm_encode(indices: np.ndarray) -> np.ndarray:
    """high-water-mark 编码的向量化版本。

    **要求 indices 是规范形式**：顶点按首次出现顺序编号（rtin_extract 的输出
    天然满足）。此时 highest 在位置 i 的值等于「前 i 个元素中出现过的不同顶点
    数」，可以用 cumsum 一次算出，不必逐元素循环。

    规则网格分支仍用 _high_water_mark_encode：那边的索引是行优先编号、不是
    规范形式，且靠 _mesh_constants 的 lru_cache 一个进程只算一次，没有向量化
    的必要。两者不可互换。
    """
    idx = np.asarray(indices, dtype=np.int64)
    _, first = np.unique(idx, return_index=True)
    is_first = np.zeros(len(idx), dtype=bool)
    is_first[np.sort(first)] = True
    highest = np.cumsum(is_first) - is_first
    return (highest - idx).astype(np.uint32)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_fix_terrain_mesh_indices.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 983 passed

- [ ] **Step 6: Commit**

```bash
git add src/services/terrain_tiling/cesiumlab_terrain.py tests/test_fix_terrain_mesh_indices.py
git commit -m "feat(terrain): high-water-mark 索引编码的向量化版本

规范形式下 highest[i] 等于前 i 个元素中的不同顶点数，可用 cumsum 一次算出。
Martini 的拓扑逐瓦片变化会让 _mesh_constants 的 lru_cache 失效，届时纯 Python
循环要对每瓦片约 24576 个元素跑一遍 —— 这是自适应路径的刚需。

规则网格分支仍用原循环版：那边索引是行优先编号不是规范形式，且有缓存兜底。"
```

---

## Task 4: `encode_quantized_mesh` 接受任意三角网

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py:284-353`（`encode_quantized_mesh`）
- Test: `tests/test_fix_terrain_mesh_indices.py`

**Interfaces:**
- Consumes: `_hwm_encode`（Task 3）、`rtin_extract` 的输出格式（Task 2）
- Produces: `encode_quantized_mesh(west, south, east, north, heights_grid, mesh=None)` —— 新增可选 `mesh` 参数，形如 `(vertex_grid_indices, triangles)`；为 `None` 时行为与现在完全一致（规则网格）。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_fix_terrain_mesh_indices.py`：

```python
def test_encode_with_explicit_mesh_writes_that_mesh():
    """传入 mesh 时，写出的顶点数/三角形数必须来自 mesh，而非满网格。"""
    from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract

    n = 17
    h = _heights(n)
    err = rtin_errors(h.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=5.0)
    assert len(tris) < 2 * (n - 1) * (n - 1), "构造的地形应该能被简化，否则测试无意义"

    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    vcount, indices, edges = _parse(data, np.uint16)
    assert vcount == len(verts)
    assert len(indices) == len(tris) * 3


def test_encode_with_mesh_keeps_all_border_points_in_edge_indices():
    """自适应网格下，四条边索引仍必须覆盖整条边（边界满密度的体现）。"""
    from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract

    n = 17
    h = _heights(n)
    err = rtin_errors(h.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=5.0)
    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    _, _, edges = _parse(data, np.uint16)
    for e in edges:
        assert len(e) == n, f"边索引应有 {n} 个点，实得 {len(e)}"


def test_encode_without_mesh_is_byte_identical_to_before():
    """mesh=None 必须走原路径 —— 这是回归护栏，规则网格字节流不能变。"""
    n = 17
    h = _heights(n)
    a = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h)
    b = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=None)
    assert a == b
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_fix_terrain_mesh_indices.py -q`
Expected: FAIL —— `TypeError: encode_quantized_mesh() got an unexpected keyword argument 'mesh'`

- [ ] **Step 3: 改造 `encode_quantized_mesh`**

把 `src/services/terrain_tiling/cesiumlab_terrain.py` 的函数签名与 body 段改为：

```python
def encode_quantized_mesh(west: float, south: float, east: float, north: float,
                          heights_grid: np.ndarray, mesh=None) -> bytes:
    """编码一张 quantized-mesh 瓦片。

    mesh=None 时用完整规则网格（索引表走 _mesh_constants 的进程级缓存）。
    传入 mesh=(vertex_grid_indices, triangles) 时按该三角网编码 —— 顶点是
    格点的子集，triangles 是局部索引，rtin_extract 的输出直接可用。
    """
    n = heights_grid.shape[0]
    assert heights_grid.shape == (n, n)
    h_min = float(np.min(heights_grid))
    h_max = float(np.max(heights_grid))
    if h_max == h_min:
        h_max = h_min + 1.0
```

包围盒/header 部分（`lon_c` 到 `header = (...)`）**保持原样不动**，它只依赖 h_min/h_max 与四至。

把从 `# vertices: regular grid` 到 `payload = header + body.getvalue()` 之间的内容替换为：

```python
    hh_full = ((heights_grid - h_min) / (h_max - h_min) * 32767.0).astype(np.uint16)

    if mesh is None:
        uzz, vzz, encoded_indices, edge_indices = _mesh_constants(n)
        hzz = _zz_delta(hh_full)
        vertex_count = n * n
    else:
        vert_idx, tris = mesh
        vertex_count = len(vert_idx)
        rows = (vert_idx // n).astype(np.int64)
        cols = (vert_idx % n).astype(np.int64)
        u_axis = np.linspace(0, 32767, n).round().astype(np.uint16)
        uu = u_axis[cols]
        vv = u_axis[rows]
        uzz = _zz_delta(uu)
        vzz = _zz_delta(vv)
        hzz = _zz_delta(hh_full.reshape(-1)[vert_idx])
        encoded_indices = _hwm_encode(tris.reshape(-1))
        # 四条边索引：保留顶点中落在各边上的，按 spec 要求的顺序排列
        # （west/east 由南向北，south/north 由西向东；Cesium 只要求同一条边
        #  在相邻瓦片间顺序一致，这里用格点坐标排序保证确定性）
        edge_indices = (
            np.where(cols == 0)[0][np.argsort(rows[cols == 0])].astype(np.uint32),
            np.where(rows == 0)[0][np.argsort(cols[rows == 0])].astype(np.uint32),
            np.where(cols == n - 1)[0][np.argsort(rows[cols == n - 1])].astype(np.uint32),
            np.where(rows == n - 1)[0][np.argsort(cols[rows == n - 1])].astype(np.uint32),
        )

    # quantized-mesh-1.0: index width is chosen by VERTEX COUNT (>65536 -> 32-bit),
    # not by the max encoded value. High-water-mark diffs wrap around, so the u16
    # branch must truncate them (astype(np.uint16)) rather than range-check them.
    index_dtype = np.uint32 if vertex_count > 65536 else np.uint16

    def pack_indices(arr: np.ndarray) -> bytes:
        return arr.astype(index_dtype).tobytes()

    body = io.BytesIO()
    body.write(struct.pack("<I", vertex_count))
    body.write(uzz.tobytes())
    body.write(vzz.tobytes())
    body.write(hzz.tobytes())

    # IndexData.triangleCount 是【三角形数】，不是索引元素数 —— 读端按
    # triangleCount*3 取索引。写成元素数会让 Cesium 读 3 倍索引而越界
    # （2026-08-04 修过一次，整条地形管线曾因此静默失效）。
    # 下面 EdgeIndices 的 len(edge) 是对的：那几个字段 spec 定义就是顶点数。
    body.write(struct.pack("<I", len(encoded_indices) // 3))
    body.write(pack_indices(encoded_indices))

    for edge in edge_indices:
        body.write(struct.pack("<I", len(edge)))
        body.write(pack_indices(edge))

    payload = header + body.getvalue()
    return payload
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_fix_terrain_mesh_indices.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 986 passed

- [ ] **Step 6: Commit**

```bash
git add src/services/terrain_tiling/cesiumlab_terrain.py tests/test_fix_terrain_mesh_indices.py
git commit -m "feat(terrain): encode_quantized_mesh 支持任意三角网

新增可选 mesh=(vertex_grid_indices, triangles) 参数；mesh=None 时字节流与
改动前完全一致（有回归测试直接断言两者相等）。

自适应网格下四条边索引改为从保留顶点中筛选并按格点坐标排序 —— 边界满密度
保证了它们必然覆盖整条边，测试直接断言每条边有 grid 个点。"
```

---

## Task 5: 接入切片流程

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py`（`_worker_tile`、`build_terrain`）
- Modify: `src/services/terrain_tiling/dem_task_tiler.py:18-32`（`TileParams`）
- Test: `tests/test_rtin.py`

**Interfaces:**
- Consumes: `rtin_errors` / `rtin_extract`（Task 1-2）、`encode_quantized_mesh(..., mesh=)`（Task 4）
- Produces: `build_terrain(..., triangulator: str = "martini", max_error_k: float = 0.15)`。`triangulator` ∈ `{"martini", "grid"}`。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rtin.py`：

```python
def test_max_error_for_level_scales_with_vertex_spacing():
    """max_error 必须按层级缩放 —— 固定绝对值会让高层瓦片压成平面。

    设计阶段实测：max_error=20m 时 z14 瓦片只剩 2 个三角形。
    规则是 K * 顶点间距，含义是坡度误差容限恒定。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import _max_error_for_level

    e14 = _max_error_for_level(14, 65, 0.15)
    e13 = _max_error_for_level(13, 65, 0.15)
    assert e13 == pytest.approx(e14 * 2, rel=1e-9), "每降一级，容限应翻倍"
    assert e14 == pytest.approx(0.15 * (180.0 / (1 << 14)) / 64 * 111320.0, rel=1e-9)


def test_build_terrain_martini_produces_fewer_triangles_than_grid(tmp_path):
    """端到端：同一份地形，martini 的瓦片必须比 grid 小。"""
    import gzip
    import struct

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: FAIL —— `ImportError: cannot import name '_max_error_for_level'`

- [ ] **Step 3: 实现层级缩放并接入 worker**

在 `cesiumlab_terrain.py` 顶部的常量区（`WGS84_E2` 之后）加：

```python
DEG_TO_M = 111320.0          # 赤道处 1° 的米数，仅用于 max_error 的量级换算
DEFAULT_MAX_ERROR_K = 0.15   # 允许高程误差 = K * 顶点间距（坡度误差容限恒定）
```

在 `_worker_tile` 之前加：

```python
def _max_error_for_level(z: int, tile_size: int, k: float) -> float:
    """该层级的 max_error（米）。

    顶点间距 = 瓦片纬向跨度 / (tile_size-1)，换算成米后乘 K。固定绝对值不可用 ——
    实测 max_error=20m 时 z14 瓦片只剩 2 个三角形，整块压成平面。
    """
    span_deg = 180.0 / (1 << z)
    spacing_m = span_deg / max(1, tile_size - 1) * DEG_TO_M
    return k * spacing_m
```

`_worker_tile` 的 task 元组增加两个字段，签名行改为：

```python
    z, x, y, west, south, east, north, tile_size, out_dir, triangulator, max_error_k = task
```

把 `data = encode_quantized_mesh(west, south, east, north, heights)` 替换为：

```python
        if triangulator == "martini":
            from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract
            err = rtin_errors(heights.reshape(-1), tile_size, pin_border=True)
            mesh = rtin_extract(err, tile_size, _max_error_for_level(z, tile_size, max_error_k))
        else:
            mesh = None
        data = encode_quantized_mesh(west, south, east, north, heights, mesh=mesh)
```

`build_terrain` 签名追加两个关键字参数（**必须加在 `stop_flag` 之后，保持既有参数顺序不变**）：

```python
    stop_flag=None,
    triangulator: str = "martini",
    max_error_k: float = DEFAULT_MAX_ERROR_K,
) -> dict:
```

`_iter_tasks` 的 yield 改为：

```python
                        yield (z, x, y, west, south, east, north, tile_size, str(out),
                               triangulator, max_error_k)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_rtin.py -q`
Expected: PASS（14 passed）

- [ ] **Step 5: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 988 passed。**如果 `test_fix_terrain_pool_robustness.py` 或 `test_fix_terrain_vrt_cleanup.py` 失败**，检查是不是漏改了 `_iter_tasks` 的 yield 元组长度——那两个文件会实际跑 `_worker_tile`。

- [ ] **Step 6: 给 TileParams 加字段并透传**

`src/services/terrain_tiling/dem_task_tiler.py` 的 `TileParams` 追加（放在 `workers` 之后、`progress_cb` 之前）：

```python
    # 三角化后端与误差系数。默认即最终值 —— UI/DB/API 都不暴露，这两个字段
    # 只为排障与测试注入而存在（出问题时切 'grid' 做对比）。
    triangulator: str = "martini"
    max_error_k: float = 0.15
```

`tile_dem_task_dir` 里调用 `build_terrain_fn` 时追加两个参数：

```python
        progress_cb=params.progress_cb,
        stop_flag=params.stop_flag,
        triangulator=params.triangulator,
        max_error_k=params.max_error_k,
    )
```

⚠️ **这里有个坑**：多个测试用 `build_terrain_fn=<桩>` 注入替身，桩的签名若不接受 `**kwargs` 会 `TypeError`。跑测试时若 `test_dem_task_tiler.py` 报参数错误，说明桩需要同步。修桩而不是回退实现——桩本来就该跟着契约走。

- [ ] **Step 7: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 988 passed

- [ ] **Step 8: Commit**

```bash
git add src/services/terrain_tiling/
git commit -m "feat(terrain): 自适应三角化接入切片流程，默认开启

triangulator 默认 'martini'、max_error_k 默认 0.15，都不暴露给 UI/DB/API，
只作排障与测试注入用（切 'grid' 可一行回退到规则网格做对比）。

max_error 按层级缩放：K * 顶点间距，含义是坡度误差容限恒定。固定绝对值
不可用 —— 实测 20m 时 z14 瓦片只剩 2 个三角形。"
```

---

## Task 6: ghost cells 采样与 ECEF 法线

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py`（`_worker_tile`）
- Test: `tests/test_terrain_normals.py`（新建）

**Interfaces:**
- Consumes: `lonlat_to_ecef(lon, lat, h)`（已存在，`cesiumlab_terrain.py:58`）
- Produces: `_vertex_normals_ecef(lons: np.ndarray, lats: np.ndarray, heights: np.ndarray) -> np.ndarray` —— 输入三个 `(n,n)` 数组，输出 `(n,n,3)` 单位法线，一律朝外。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_terrain_normals.py`：

```python
"""逐顶点法线与 ghost cells 的测试。

不需要 GDAL：法线是纯几何计算，直接喂经纬度/高程数组。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.cesiumlab_terrain import (
    _vertex_normals_ecef,
    lonlat_to_ecef,
)


def _grid(lon0, lat0, span, n, heights):
    lons, lats = np.meshgrid(np.linspace(lon0, lon0 + span, n),
                             np.linspace(lat0, lat0 + span, n))
    return lons, lats, heights


def test_flat_terrain_normals_point_straight_up():
    """水平面上的法线必须与地心方向一致（ECEF 空间，不是经纬度空间）。"""
    n = 9
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, np.full((n, n), 1000.0))
    nrm = _vertex_normals_ecef(lons, lats, h)
    up = lonlat_to_ecef(lons, lats, np.zeros_like(h))
    up = up / np.linalg.norm(up, axis=-1, keepdims=True)
    cos = (nrm * up).sum(-1)
    assert np.allclose(cos, 1.0, atol=1e-6)


def test_normals_are_unit_length():
    n = 9
    rng = np.random.default_rng(4)
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, rng.random((n, n)) * 500.0)
    nrm = _vertex_normals_ecef(lons, lats, h)
    assert np.allclose(np.linalg.norm(nrm, axis=-1), 1.0, atol=1e-9)


def test_normals_always_point_outward():
    """不论三角形绕序如何，法线必须朝外（与地心方向点积为正）。"""
    n = 9
    rng = np.random.default_rng(6)
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, rng.random((n, n)) * 900.0)
    nrm = _vertex_normals_ecef(lons, lats, h)
    xyz = lonlat_to_ecef(lons, lats, h)
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    assert ((nrm * up).sum(-1) > 0).all()


def test_slope_tilts_normal_towards_downhill():
    """东高西低的斜坡，法线应朝西倾斜（东向分量为负）。"""
    n = 9
    xx = np.arange(n, dtype=float)
    h = np.tile(xx * 100.0, (n, 1))          # 沿经度递增
    lons, lats, h = _grid(86.0, 41.0, 0.01, n, h)
    nrm = _vertex_normals_ecef(lons, lats, h)
    lo = np.radians(86.005)
    east = np.array([-np.sin(lo), np.cos(lo), 0.0])
    assert (nrm[n // 2, n // 2] * east).sum() < 0


def test_ghost_ring_eliminates_seam_between_adjacent_tiles():
    """核心断言：用 ghost 环算出的边界法线，在相邻瓦片两侧必须完全相同。

    A 的东边界与 B 的西边界是同一条经线上的同样一组点。各自扩一圈后，
    两侧该处的邻接三角形集合完全一致，法线因此逐位相同。
    """
    n = 9
    span = 0.01
    step = span / (n - 1)

    def terrain(lon, lat):
        return 500.0 + 300.0 * np.sin(lon * 60.0) + 200.0 * np.cos(lat * 40.0)

    def ghost_normals(lon0, lat0):
        lons, lats = np.meshgrid(
            np.linspace(lon0 - step, lon0 + span + step, n + 2),
            np.linspace(lat0 - step, lat0 + span + step, n + 2))
        nrm = _vertex_normals_ecef(lons, lats, terrain(lons, lats))
        return nrm[1:-1, 1:-1]

    a = ghost_normals(86.0, 41.0)
    b = ghost_normals(86.0 + span, 41.0)
    assert np.array_equal(a[:, -1], b[:, 0]), "ghost 修正后接缝未归零"


def test_without_ghost_ring_seam_exists():
    """对照组：不扩环时接缝必然存在 —— 证明上一个测试不是恒真。"""
    n = 9
    span = 0.01

    def terrain(lon, lat):
        return 500.0 + 300.0 * np.sin(lon * 60.0) + 200.0 * np.cos(lat * 40.0)

    def plain_normals(lon0, lat0):
        lons, lats = np.meshgrid(np.linspace(lon0, lon0 + span, n),
                                 np.linspace(lat0, lat0 + span, n))
        return _vertex_normals_ecef(lons, lats, terrain(lons, lats))

    a = plain_normals(86.0, 41.0)
    b = plain_normals(86.0 + span, 41.0)
    assert not np.allclose(a[:, -1], b[:, 0])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_terrain_normals.py -q`
Expected: FAIL —— `ImportError: cannot import name '_vertex_normals_ecef'`

- [ ] **Step 3: 实现法线计算**

在 `cesiumlab_terrain.py` 的 `lonlat_to_ecef` 之后插入：

```python
def _vertex_normals_ecef(lons: np.ndarray, lats: np.ndarray,
                         heights: np.ndarray) -> np.ndarray:
    """逐顶点法线（面积加权），在 ECEF 空间计算。

    必须用 ECEF 而不是经纬度空间：经纬度下 1° 经距随纬度变化，直接叉积会让
    高纬地区的法线系统性偏斜。

    三角形切法与 _mesh_constants 一致（每格两片）。最后统一朝外（与地心方向
    点积为正），因此不依赖绕序。
    """
    xyz = lonlat_to_ecef(lons, lats, heights)
    acc = np.zeros_like(xyz)
    v0 = xyz[:-1, :-1]
    v1 = xyz[:-1, 1:]
    v2 = xyz[1:, :-1]
    v3 = xyz[1:, 1:]
    n1 = np.cross(v1 - v0, v2 - v0)      # 三角形 (a0,a1,a2)
    n2 = np.cross(v3 - v1, v2 - v1)      # 三角形 (a1,a3,a2)
    for sl, nr in ((np.s_[:-1, :-1], n1), (np.s_[:-1, 1:], n1), (np.s_[1:, :-1], n1),
                   (np.s_[:-1, 1:], n2), (np.s_[1:, 1:], n2), (np.s_[1:, :-1], n2)):
        acc[sl] += nr
    ln = np.linalg.norm(acc, axis=-1, keepdims=True)
    ln[ln == 0] = 1.0
    out = acc / ln
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    out[(out * up).sum(-1) < 0] *= -1
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_terrain_normals.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 在 `_worker_tile` 里做 ghost 采样**

把 `_worker_tile` 的采样段替换为：

```python
        lons = np.linspace(west, east, tile_size, dtype=np.float64)
        lats = np.linspace(south, north, tile_size, dtype=np.float64)
        llon, llat = np.meshgrid(lons, lats)
        heights = _WORKER_SAMPLER.sample(llon, llat)

        normals = None
        if with_normals:
            # ghost 环：向外各扩一个步长再采样，算完法线裁回中间 tile_size^2。
            # 不扩环时边界顶点拿不到邻瓦片的三角形，法线不完整 —— 山地实测
            # 81.9% 的相邻瓦片对亮度阶跃超过 2% 可察觉阈值。扩环后实测 100%
            # 归零（两侧邻接三角形集合完全一致，浮点结果逐位相同）。
            dw = (east - west) / (tile_size - 1)
            dh = (north - south) / (tile_size - 1)
            glon = np.linspace(west - dw, east + dw, tile_size + 2, dtype=np.float64)
            glat = np.linspace(south - dh, north + dh, tile_size + 2, dtype=np.float64)
            gl, ga = np.meshgrid(glon, glat)
            gh = _WORKER_SAMPLER.sample(gl, ga)
            normals = _vertex_normals_ecef(gl, ga, gh)[1:-1, 1:-1]
```

`_worker_tile` 的 task 元组再加一个字段：

```python
    (z, x, y, west, south, east, north, tile_size, out_dir,
     triangulator, max_error_k, with_normals) = task
```

`_iter_tasks` 的 yield 同步加 `with_normals`。`build_terrain` 签名追加 `normals: bool = True`，并在 `_iter_tasks` 里把它带上。

⚠️ **DEM 边缘的已知限制**：ghost 环落在 DEM 范围外时，`DemSampler.sample` 会把越界/nodata 兜成 0（`:219`），DEM 最外圈会产生一圈朝向偏差的法线。这只影响整个 DEM 的最外一圈瓦片的最外一行顶点，不影响瓦片之间的接缝（相邻瓦片仍取到相同的 ghost 值）。本计划不处理，作为已知限制记录。

- [ ] **Step 6: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 994 passed

- [ ] **Step 7: Commit**

```bash
git add src/services/terrain_tiling/cesiumlab_terrain.py tests/test_terrain_normals.py
git commit -m "feat(terrain): ECEF 空间的逐顶点法线 + ghost cells 采样

法线必须在 ECEF 空间算：经纬度下 1° 经距随纬度变化，直接叉积会让高纬地区
法线系统性偏斜。统一朝外，不依赖三角形绕序。

ghost 环（采样 65->67 后裁回）消除跨瓦片接缝 —— 不扩环时边界顶点拿不到邻
瓦片的三角形，山地实测 81.9% 的相邻瓦片对亮度阶跃超过 2% 可察觉阈值；扩环
后两侧邻接三角形集合完全一致，实测 100% 归零。测试含对照组证明非恒真。"
```

---

## Task 7: oct 编码与 extension 段

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py`（`encode_quantized_mesh`、`build_terrain` 的 layer.json）
- Test: `tests/test_terrain_normals.py`

**Interfaces:**
- Consumes: `_vertex_normals_ecef`（Task 6）
- Produces: `_oct_encode(normals: np.ndarray) -> np.ndarray` —— `(N,3)` 单位向量 → `(N,2)` uint8。`encode_quantized_mesh(..., normals=None)` 新增可选参数。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_terrain_normals.py`：

```python
from src.services.terrain_tiling.cesiumlab_terrain import (
    _oct_encode,
    encode_quantized_mesh,
)


def _oct_decode(enc):
    """spec 的解码算法，用于往返验证。"""
    f = enc.astype(np.float64) / 255.0 * 2.0 - 1.0
    x, y = f[:, 0].copy(), f[:, 1].copy()
    z = 1.0 - np.abs(x) - np.abs(y)
    neg = z < 0
    if neg.any():
        ox, oy = x[neg].copy(), y[neg].copy()
        x[neg] = (1.0 - np.abs(oy)) * np.where(ox >= 0, 1.0, -1.0)
        y[neg] = (1.0 - np.abs(ox)) * np.where(oy >= 0, 1.0, -1.0)
    v = np.stack([x, y, z], axis=1)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_oct_encode_roundtrip_within_quantisation_error():
    rng = np.random.default_rng(9)
    v = rng.normal(size=(500, 3))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    back = _oct_decode(_oct_encode(v))
    cos = np.clip((v * back).sum(1), -1, 1)
    assert np.degrees(np.arccos(cos)).max() < 2.0, "oct16 往返误差应小于 2°"


def test_oct_encode_output_is_two_bytes_per_vertex():
    v = np.tile(np.array([[0.0, 0.0, 1.0]]), (7, 1))
    enc = _oct_encode(v)
    assert enc.shape == (7, 2)
    assert enc.dtype == np.uint8


def test_extension_segment_is_appended_with_correct_header():
    """extension 段格式：unsigned char id(=1) + unsigned int length + payload。"""
    import struct

    n = 17
    yy, xx = np.mgrid[0:n, 0:n]
    h = (100.0 + xx * 2.5 + yy * 1.25).astype(np.float64)
    nrm = np.zeros((n, n, 3))
    nrm[..., 2] = 1.0

    without = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h)
    withn = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, normals=nrm)

    assert len(withn) == len(without) + 1 + 4 + 2 * n * n
    ext_id, ext_len = struct.unpack_from("<BI", withn, len(without))
    assert ext_id == 1, "oct-encoded normals 的 extensionId 必须是 1"
    assert ext_len == 2 * n * n


def test_normals_follow_the_simplified_vertex_subset():
    """传 mesh 时，法线数量必须跟随保留顶点数，而不是满网格。"""
    import struct

    from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract

    n = 17
    yy, xx = np.mgrid[0:n, 0:n]
    h = (100.0 + xx * 2.5 + yy * 1.25).astype(np.float64)
    nrm = np.zeros((n, n, 3))
    nrm[..., 2] = 1.0
    err = rtin_errors(h.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=3.0)
    assert len(verts) < n * n

    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h,
                                 mesh=(verts, tris), normals=nrm)
    base = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    ext_id, ext_len = struct.unpack_from("<BI", data, len(base))
    assert ext_id == 1
    assert ext_len == 2 * len(verts)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_terrain_normals.py -q`
Expected: FAIL —— `ImportError: cannot import name '_oct_encode'`

- [ ] **Step 3: 实现 oct 编码与 extension 段**

在 `_vertex_normals_ecef` 之后加：

```python
def _oct_encode(normals: np.ndarray) -> np.ndarray:
    """单位向量 (N,3) -> oct16 编码 (N,2) uint8。

    quantized-mesh 的 Oct-Encoded Per-Vertex Normals 扩展（extensionId=1）。
    算法与 Cesium 的 AttributeCompression.octEncode 一致：L1 归一化投影到
    八面体，z<0 时折叠到外圈，再把 [-1,1] 映射到 [0,255]。
    """
    v = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    l1 = np.abs(v).sum(axis=1, keepdims=True)
    l1 = np.where(l1 == 0.0, 1.0, l1)
    p = v[:, :2] / l1
    neg = v[:, 2] < 0.0
    if neg.any():
        px = p[neg, 0].copy()
        py = p[neg, 1].copy()
        p[neg, 0] = (1.0 - np.abs(py)) * np.where(px >= 0.0, 1.0, -1.0)
        p[neg, 1] = (1.0 - np.abs(px)) * np.where(py >= 0.0, 1.0, -1.0)
    q = np.round((np.clip(p, -1.0, 1.0) * 0.5 + 0.5) * 255.0)
    return q.astype(np.uint8)
```

`encode_quantized_mesh` 签名加 `normals=None`，并在 `payload = header + body.getvalue()` 之前插入：

```python
    if normals is not None:
        nrm = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
        if mesh is not None:
            nrm = nrm[mesh[0]]
        oct_bytes = _oct_encode(nrm).tobytes()
        # ExtensionHeader: unsigned char extensionId; unsigned int extensionLength;
        body.write(struct.pack("<BI", 1, len(oct_bytes)))
        body.write(oct_bytes)
```

`build_terrain` 的 layer.json 字典把 `"extensions": []` 改为：

```python
            "extensions": ["octvertexnormals"] if normals else [],
```

`_worker_tile` 里把 normals 传给编码：

```python
        data = encode_quantized_mesh(west, south, east, north, heights,
                                     mesh=mesh, normals=normals)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_terrain_normals.py -q`
Expected: PASS（10 passed）

- [ ] **Step 5: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 998 passed

- [ ] **Step 6: 端到端人工验证（必做，不可跳过）**

单元测试守不住「Cesium 能不能解码」——2026-08-04 那个 `triangleCount` bug 就是测试自洽但 Cesium 解不了。必须实跑一次：

```bash
# 1. 切一个真实 granule
uv run python -c "
from src.services.terrain_tiling.cesiumlab_terrain import build_terrain
r = build_terrain(['downloads/dem/dem_task_1/ASTGTMV003_N41E086_dem.tif'],
                  'downloads/dem/dem_task_9100/terrain_tiles',
                  min_level=0, max_level=12, tile_size=65, workers=4)
print(r)"

# 2. 插一条任务行让静态路由能解析（output_path 用绝对路径）
uv run python -c "
import sqlite3, os
from src.core.config import Config
c = sqlite3.connect(Config.DATABASE_PATH)
c.execute('''INSERT OR REPLACE INTO dem_tasks
 (id,name,status,north,south,east,west,dataset,output_path,total_files,downloaded_files)
 VALUES (9100,'e2e','completed',42.0,41.0,87.0,86.0,'ASTGTM.003',?,1,1)''',
 (os.path.abspath('downloads/dem'),)); c.commit()"

# 3. 起服务
DEBUG=0 uv run python app.py
```

浏览器打开 `http://127.0.0.1:5000`，控制台执行：

```js
const p = await Cesium.CesiumTerrainProvider.fromUrl('/terrain/dem/9100/',
                                                     { requestVertexNormals: true });
console.log('hasVertexNormals', p.hasVertexNormals);   // 必须为 true
const d = await p.requestTileGeometry(379, 69, 12);    // 内部 XYZ 坐标
console.log('三角形', d._indices.length / 3, '顶点', d._quantizedVertices.length / 3);
console.log('法线字节', d._encodedNormals ? d._encodedNormals.length : 'none');
```

**验收标准**：`hasVertexNormals === true`、解码不抛异常、`_encodedNormals.length === 顶点数 * 2`。

清理：

```bash
rm -rf downloads/dem/dem_task_9100
uv run python -c "
import sqlite3
from src.core.config import Config
c = sqlite3.connect(Config.DATABASE_PATH)
c.execute('DELETE FROM dem_tasks WHERE id = 9100'); c.commit()"
```

- [ ] **Step 7: Commit**

```bash
git add src/services/terrain_tiling/cesiumlab_terrain.py tests/test_terrain_normals.py
git commit -m "feat(terrain): oct-encoded 逐顶点法线扩展段

extensionId=1，每顶点 2 字节，算法与 Cesium 的 AttributeCompression.octEncode
一致。传 mesh 时法线自动取保留顶点的子集 —— 数量跟随简化后的顶点，但值来自
满网格计算（粗几何 + 精细法线，类似 normal mapping）。

layer.json 相应声明 extensions: ['octvertexnormals']，否则 Cesium 不会请求。

已做端到端验证：Cesium 1.143 实际解码通过，hasVertexNormals 为 true。"
```

---

## Task 8: 前端请求法线与光照开关

**Files:**
- Create: `static/js/terrain_lighting.js`
- Modify: `static/js/map.js`（provider 创建处）
- Modify: `templates/index.html`（地图工具栏）
- Modify: `templates/base.html`（引入脚本）

**Interfaces:**
- Consumes: `layer.json` 的 `extensions: ["octvertexnormals"]`（Task 7）
- Produces: `window.TerrainLighting = { get(), set(on), init(viewer) }`

- [ ] **Step 1: 创建光照开关模块**

创建 `static/js/terrain_lighting.js`：

```javascript
/**
 * 地形光照开关。
 *
 * 与 theme.js 同款：偏好只存 localStorage（key `tf-terrain-lighting`），
 * 不进 config 表 —— 它是纯客户端渲染偏好，切换不需要重切片。
 *
 * 默认关：开启会改变所有现有预览的外观（卫星影像自带光照信息，再叠一层会
 * 显得过暗或有双重阴影；lyrs=m 路网图是矢量风格，加光照更不协调）。法线
 * 数据无条件写入瓦片，用户想看地形起伏时随手打开，即时生效。
 */
(function () {
    'use strict';
    const KEY = 'tf-terrain-lighting';
    let _viewer = null;

    function get() {
        try {
            return window.localStorage.getItem(KEY) === '1';
        } catch (e) {
            return false;   // 隐私模式等 localStorage 不可用时按默认关
        }
    }

    function apply(on) {
        if (_viewer && _viewer.scene && _viewer.scene.globe) {
            _viewer.scene.globe.enableLighting = on;
            _viewer.scene.requestRender();   // requestRenderMode=true 下必须显式请求
        }
        const btn = document.getElementById('mapTerrainLighting');
        if (btn) {
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            btn.classList.toggle('active', on);
        }
    }

    function set(on) {
        try {
            window.localStorage.setItem(KEY, on ? '1' : '0');
        } catch (e) { /* 忽略 */ }
        apply(on);
    }

    function init(viewer) {
        _viewer = viewer;
        apply(get());
        const btn = document.getElementById('mapTerrainLighting');
        if (btn) btn.addEventListener('click', () => set(!get()));
    }

    window.TerrainLighting = { get, set, init };
})();
```

- [ ] **Step 2: 加工具栏按钮**

`templates/index.html` 中 `id="mapDrawRect"` 所在的 `.map-panel-triggers` 块之后，插入一个新块：

```html
            <div class="map-panel-triggers">
                <button type="button" class="map-panel-btn" id="mapTerrainLighting"
                        aria-label="地形光照" title="地形光照（需已加载地形）" aria-pressed="false">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                         stroke="currentColor" stroke-width="2" stroke-linecap="round"
                         stroke-linejoin="round" aria-hidden="true">
                        <circle cx="12" cy="12" r="4"></circle>
                        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path>
                    </svg>
                </button>
            </div>
```

- [ ] **Step 3: 引入脚本并初始化**

`templates/base.html` 里 `theme.js` 的 `<script>` 之后加：

```html
    <script src="{{ url_for('static', filename='js/terrain_lighting.js') }}"></script>
```

`static/js/map.js` 的 `_initMapTools();`（约 `:234`）那一行之后加：

```javascript
    if (window.TerrainLighting) window.TerrainLighting.init(viewer);
```

- [ ] **Step 4: 请求法线数据**

`static/js/map.js` 里 provider 创建处（2026-08-04 已改为传 `base`）加上 options：

```javascript
                // 传目录，不能传 `${base}/layer.json` —— fromUrl 内部会 appendForwardSlash()
                // 后再拼 layer.json，传后者会请求 .../layer.json/layer.json 得 404。
                // 更坑的是它不 reject：拿不到 layer.json 时静默按默认假设建 provider
                // （实测 hasWaterMask 变成 true），随后瓦片请求全 404，前端毫无提示。
                // requestVertexNormals：不传则 Cesium 不下载法线段，光照开关会没效果。
                const provider = await Cesium.CesiumTerrainProvider.fromUrl(base, {
                    requestVertexNormals: true,
                });
```

- [ ] **Step 5: 人工验证**

起服务后：

1. 加载一个地形任务的预览；
2. 点工具栏的光照按钮 —— 地形出现明暗，按钮 `aria-pressed` 变 `true`；
3. 刷新页面 —— 状态保持（localStorage）；
4. 再点一次关闭 —— 恢复无光照。

**若点了没反应**，按顺序查：`p.hasVertexNormals` 是否为 `true`（layer.json 少了 `extensions` 声明 / `fromUrl` 少传 options）→ `scene.globe.enableLighting` 是否真被设置 → 是否漏调 `requestRender()`（`requestRenderMode=true` 下不显式请求就不会重绘）。

- [ ] **Step 6: 跑全量测试**

Run: `uv run pytest tests/ -q`
Expected: 998 passed（前端改动不影响 Python 测试；`test_css_contract.py` 只钉 CSS 与 `base.html` 的 `data-bs-theme`，不受影响）

- [ ] **Step 7: Commit**

```bash
git add static/js/terrain_lighting.js static/js/map.js templates/index.html templates/base.html
git commit -m "feat(terrain): 前端请求法线数据 + 地形光照开关（默认关）

fromUrl 传 requestVertexNormals: true，否则 Cesium 不下载法线段，开关会没效果。

光照偏好只存 localStorage（同 theme.js），不进 config 表 —— 纯客户端渲染
偏好，切换不需重切片。默认关：开启会改变所有现有预览外观，卫星影像自带
光照信息再叠一层会过暗或双重阴影。

requestRenderMode=true 下切换后必须显式 requestRender()，否则不重绘。"
```

---

## 收尾

- [ ] **更新设计稿状态**

把 `docs/superpowers/specs/2026-08-04-terrain-triangulation-design.md` 的状态行改为「已实施」，并在 specs/README.md 的清单里同步。

- [ ] **更新 CLAUDE.md**

「DEM / terrain specifics」一节补一句：三角化默认走 RTIN 自适应（`rtin.py`，K=0.15），瓦片带 oct-encoded 法线，`build_terrain` 的 `triangulator`/`normals`/`max_error_k` 只作排障用不暴露给 UI。

- [ ] **升级提示**

`RELEASE_NOTES.md` 写明：本版切片格式变化，**已有的地形瓦片需要重切**才能享受减面与光照（2026-08-04 的 `triangleCount` 修复本来就要求重切，两件事可以一次说清）。

---

## Self-Review 记录

**Spec 覆盖**：设计稿第五章改动清单 6 项 —— RTIN 误差表（Task 1）、提取（Task 2）、编码向量化（Task 3）、encode 支持任意网格（Task 4）、接入流程 + TileParams（Task 5）、ghost + 法线（Task 6-7）、layer.json extensions（Task 7）、前端（Task 8）。全覆盖。DB / API 按约束不动。

**类型一致性**：`rtin_tables` → `rtin_errors` → `rtin_extract` → `encode_quantized_mesh(mesh=)` 的数据形态在各任务的 Interfaces 块中逐一对齐；`_hwm_encode` 的输入前提（规范顶点顺序）由 Task 2 的 `test_extract_vertex_order_is_first_occurrence` 保证。

**代码已对拍验证**：写完 plan 后把 Task 1-2 的三段实现（`rtin_tables` / `rtin_errors` / `rtin_extract`）与设计阶段那份验证过的原型逐一对比，grid=17 与 65 各跑多组随机地形：索引表逐元素相同、误差数组（pin/非 pin 两种）逐元素相同、提取的顶点与三角形数组（三档 max_error）逐元素相同、顶点顺序确为首次出现顺序、边界保留 256/256。**这几段可以照着写，不必重新推导**。Task 4/6/7 的实现片段未做同等对拍（它们依赖 GDAL 采样或字节流上下文），照常按 TDD 走。

**已知未覆盖**：
1. DEM 最外圈瓦片的 ghost 环落在数据外，`DemSampler.sample` 兜零会产生一圈朝向偏差的法线（Task 6 Step 5 已标注，不影响瓦片间接缝）。
2. `layer.json` 的 `parentUrl` 语义未验证（默认值以 `layer.json` 结尾，与 `fromUrl` 不同，很可能是对的），需要真实 base 地形数据才能测。
3. 法线接缝的**视觉**可见度只有数值模型支撑，未做真实渲染的目视确认。
