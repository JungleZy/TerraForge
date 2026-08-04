"""
C2 fix: quantized-mesh triangle index bit-width must be chosen by VERTEX COUNT
(>65536 -> uint32, else uint16), per the quantized-mesh-1.0 spec — not by the
max encoded value. High-water-mark encoding wraps differences around, so the
old arr.max()-based check effectively always picked uint32, which a spec-
compliant Cesium reader (uint16 for <=65536 vertices) misreads, shifting every
following field.

These tests parse the encoded byte stream with the spec-mandated layout and
assert both the index width and the field offsets that follow the indices.
"""

import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.cesiumlab_terrain import encode_quantized_mesh

HEADER_SIZE = 88  # <ddd ff ddd d ddd


def _expected_triangles(n: int) -> list[int]:
    idx = []
    for j in range(n - 1):
        for i in range(n - 1):
            a0 = j * n + i
            a1 = j * n + (i + 1)
            a2 = (j + 1) * n + i
            a3 = (j + 1) * n + (i + 1)
            idx.extend([a0, a1, a2])
            idx.extend([a1, a3, a2])
    return idx


def _hwm_decode(raw: np.ndarray) -> list[int]:
    mod = 1 << (raw.dtype.itemsize * 8)
    highest = 0
    out = []
    for r in raw:
        code = (highest - int(r)) % mod
        out.append(code)
        if code == highest:
            highest += 1
    return out


def _parse(data: bytes, index_dtype) -> tuple[int, np.ndarray, list[np.ndarray]]:
    """Parse a quantized-mesh payload assuming the given index width. Asserts
    the stream is consumed exactly (no trailing/short bytes)."""
    off = HEADER_SIZE
    (vcount,) = struct.unpack_from("<I", data, off)
    off += 4
    # u/v/h zigzag deltas are always uint16.
    off += vcount * 2 * 3
    # IndexData.triangleCount 按 spec 是【三角形数】，索引元素数 = triangleCount * 3。
    # 此前这里直接把该字段当索引元素数读（count=tri_count），与当时的编码端用了
    # 同一套错误约定，两边自洽所以测试通过 —— 而 Cesium 按 spec 读 triangleCount*3
    # 个索引，实测抛 RangeError: Invalid typed array length。解析必须照 spec 写，
    # 不能照实现写，否则这个测试测的是「实现和自己一致」而不是「实现符合 spec」。
    (tri_count,) = struct.unpack_from("<I", data, off)
    off += 4
    isize = np.dtype(index_dtype).itemsize
    icount = tri_count * 3
    indices = np.frombuffer(data, dtype=index_dtype, count=icount, offset=off)
    off += icount * isize
    edges = []
    for _ in range(4):
        (ecount,) = struct.unpack_from("<I", data, off)
        off += 4
        edges.append(np.frombuffer(data, dtype=index_dtype, count=ecount, offset=off))
        off += ecount * isize
    assert off == len(data), f"stream not consumed exactly: off={off} len={len(data)}"
    return vcount, indices, edges


def _zigzag_decode(z: np.ndarray) -> np.ndarray:
    """zigzag 逆变换。编码端是 (v << 1) ^ (v >> 31)，逆变换是 (z >> 1) ^ -(z & 1)。"""
    z = z.astype(np.int64)
    return (z >> 1) ^ -(z & 1)


def _decode_uvh(data: bytes, vcount: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把 VertexData 的 u/v/height 三段反解回原值。

    三段各 vcount 个 uint16，都是 zigzag(相邻差分)，所以逆序是「zigzag 逆 -> 累加」。
    只断言计数（vcount/triangleCount/边长度）是测不出顶点内容的：u/v 互换、量化尺度
    写错、高程取错顶点，产出的字节流长度分毫不差，Cesium 也能正常解码正常渲染，
    只是地形整个错位或变平 —— 又一款「HTTP 200 + 任务 completed + 前端不报错」的
    静默失效。要钉住这些就必须把顶点数据真解出来比对。
    """
    off = HEADER_SIZE + 4
    raw = np.frombuffer(data, dtype=np.uint16, count=vcount * 3, offset=off)
    out = []
    for k in range(3):
        out.append(np.cumsum(_zigzag_decode(raw[k * vcount:(k + 1) * vcount])))
    return out[0], out[1], out[2]


def _expected_uv_axis(n: int) -> np.ndarray:
    """u/v 量化轴：n 个格点均匀铺满 0..32767（spec 的说法，与生产实现无关地独立算）。

    这里没有浮点歧义：quantized-mesh 的 tile_size 恒为 2^k+1，n-1 是 2 的幂，
    i*32767/(n-1) 在 float64 下精确可表示，round 不会踩到 .5 的边界歧义。
    """
    return np.round(np.arange(n) * 32767 / (n - 1)).astype(np.int64)


def _heights(n: int) -> np.ndarray:
    yy, xx = np.mgrid[0:n, 0:n]
    return (100.0 + xx * 2.5 + yy * 1.25).astype(np.float64)


def _rtin_mesh(n: int, max_error: float = 5.0):
    """构一张真被简化过的自适应网格，返回 (heights, verts, tris)。"""
    from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract

    h = _heights(n)
    err = rtin_errors(h.reshape(-1), n, pin_border=True)
    verts, tris = rtin_extract(err, n, max_error=max_error)
    assert len(tris) < 2 * (n - 1) * (n - 1), "构造的地形应该能被简化，否则测试无意义"
    return h, verts, tris


def test_triangle_count_field_holds_triangle_count_not_index_count():
    """IndexData.triangleCount 必须写【三角形数】，不是索引元素数。

    写成索引元素数时，Cesium 会去读 triangleCount*3 个索引而越界，实测
    `RangeError: Invalid typed array length: 73728`（tile_size=65 时 24576*3），
    整个地形管线因此静默失效 —— 请求全 200、任务标 completed、前端不报错，
    但一片地形都渲染不出来。
    """
    for n in (17, 65):
        data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, _heights(n))
        off = HEADER_SIZE + 4 + (n * n) * 2 * 3   # header + vertexCount + u/v/h
        (tri_count,) = struct.unpack_from("<I", data, off)
        assert tri_count == 2 * (n - 1) * (n - 1), (
            f"n={n}: triangleCount={tri_count}, 期望 {2*(n-1)*(n-1)}"
            f"（若等于 {6*(n-1)*(n-1)} 则是误写成了索引元素数）"
        )


def test_small_mesh_uses_uint16_indices_and_correct_offsets():
    n = 17  # 289 vertices -> spec mandates uint16 indices
    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, _heights(n))

    vcount, indices, edges = _parse(data, np.uint16)

    assert vcount == n * n
    assert len(indices) == 3 * 2 * (n - 1) * (n - 1)
    assert _hwm_decode(indices) == _expected_triangles(n)

    west, south, east, north = edges  # edge indices are NOT high-water-mark encoded
    assert list(west) == [j * n for j in range(n)]
    assert list(south) == list(range(n))
    assert list(east) == [j * n + (n - 1) for j in range(n)]
    assert list(north) == [(n - 1) * n + i for i in range(n)]


def test_mesh_with_exactly_65536_vertices_stays_uint16():
    n = 256  # 65536 vertices: NOT > 65536 -> still uint16
    data = encode_quantized_mesh(100.0, 30.0, 110.0, 40.0, _heights(n))
    vcount, indices, edges = _parse(data, np.uint16)
    assert vcount == 65536
    assert _hwm_decode(indices)[:6] == _expected_triangles(n)[:6]


def test_mesh_above_65536_vertices_uses_uint32_indices():
    n = 257  # 66049 vertices > 65536 -> uint32
    data = encode_quantized_mesh(100.0, 30.0, 110.0, 40.0, _heights(n))
    vcount, indices, edges = _parse(data, np.uint32)
    assert vcount == 66049
    assert _hwm_decode(indices)[:6] == _expected_triangles(n)[:6]
    assert list(edges[0]) == [j * n for j in range(n)]  # edges are raw, not HWM-encoded


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


# 规则网格路径的 golden 字节指纹。
#
# 来历：commit 85a532c（encode_quantized_mesh 新增 mesh 参数）**之前**的实现，
# 对下列固定输入产出的 payload body 的 sha256：
#   bbox = (west=100.0, south=30.0, east=101.0, north=31.0)
#   heights = _heights(n)（本文件顶部那个 100 + 2.5x + 1.25y 的斜面）
#   n ∈ {17, 65, 257}   —— 65 是生产默认 tile_size，257 走 uint32 索引分支
#
# 为什么需要这个：上面的 test_encode_without_mesh_is_byte_identical_to_before 名字
# 说的是「与改动前一致」，实测它做不到 —— 它比的是同一版本内 encode(...) 与
# encode(..., mesh=None)，只能证明默认参数是 None。把 mesh is None 分支里的 uzz
# 整个改错，那条测试连同全部地形测试照样全绿。真正的跨版本护栏只能是钉死的指纹。
#
# 为什么只哈希 body 不哈希整包：header 那 88 字节是 ECEF 经纬度换算 + np.linalg.norm
# 的 float64 结果，跨平台/跨 BLAS 有末位 ULP 漂移的可能，而本项目 Linux 与 Windows
# 都要出包，拿它当指纹会变成偶发红灯。body 全是整数量化结果（IEEE754 的 +-*/ 保证
# 正确舍入，处处一致），且顶点/索引/边全在 body 里 —— 要守的就是它。
#
# 这个哈希红了不代表它自己脆：它意味着规则网格的字节流真的变了。请先确认那是有意的，
# 再更新常量，不要直接删掉这个测试。
_GOLDEN_REGULAR_GRID_BODY_SHA256 = {
    17: "0772d3632a187bc6400e98ab953361f7ae286b24d4f13a83868534592bb9a792",
    65: "2d9da4adc5721d4f0805876db352ca0d534c192f0b742117fbcfea2daa3aaf6b",
    257: "d7868ff5919837613f792c69976d5ce6e272ef60750716e2b3f7a62fe09391b8",
}


@pytest.mark.parametrize("n", sorted(_GOLDEN_REGULAR_GRID_BODY_SHA256))
def test_regular_grid_bytes_match_golden_fingerprint(n):
    """mesh=None 的字节流必须与 85a532c 之前逐字节一致（见上方常量的注释）。"""
    import hashlib

    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, _heights(n))
    got = hashlib.sha256(data[HEADER_SIZE:]).hexdigest()
    assert got == _GOLDEN_REGULAR_GRID_BODY_SHA256[n], (
        f"n={n}: 规则网格字节流变了。got={got} "
        f"expected={_GOLDEN_REGULAR_GRID_BODY_SHA256[n]}"
    )


def test_encode_with_mesh_writes_correct_vertex_uvh():
    """自适应分支的顶点数据必须逐个对上：u 来自列、v 来自行、高程来自该顶点。

    只断言 vcount 是测不出内容的 —— u/v 互换、量化尺度 32767 写成 32766、
    高程取错顶点，三种都不改变任何长度字段。
    """
    n = 17
    h, verts, tris = _rtin_mesh(n)
    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    vcount, _, _ = _parse(data, np.uint16)
    assert vcount == len(verts)

    u, v, hq = _decode_uvh(data, vcount)
    axis = _expected_uv_axis(n)
    rows, cols = verts // n, verts % n
    # 顶点不能全落在对角线上，否则 u/v 互换这个变异测不出来
    assert (rows != cols).any()

    assert np.array_equal(u, axis[cols]), "u 必须由格点【列】决定"
    assert np.array_equal(v, axis[rows]), "v 必须由格点【行】决定"

    # 高程：量化公式与生产同源，但这里要钉的是「哪个格点的高程进了哪个槽位」
    h_min, h_max = float(h.min()), float(h.max())
    expected_h = ((h - h_min) / (h_max - h_min) * 32767.0).astype(np.uint16).reshape(-1)
    assert np.array_equal(hq, expected_h[verts].astype(np.int64))


def test_encode_with_mesh_edge_indices_are_sorted_and_on_the_right_edge():
    """四条边索引：槽位不能串，边内顺序必须严格递增。

    槽位串了或顺序乱了，相邻瓦片的公共边顶点对不上 -> 地形裂缝，同样是静默的。
    代码注释写着「按 spec 要求的顺序排列」，这条测试负责让那句话可证伪。
    """
    n = 17
    h, verts, tris = _rtin_mesh(n)
    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    vcount, _, edges = _parse(data, np.uint16)
    u, v, _ = _decode_uvh(data, vcount)

    west, south, east, north = edges
    # (槽位名, 该边索引, 恒定的那一轴, 恒定值, 递增的那一轴)
    cases = [
        ("west", west, u, 0, v),
        ("south", south, v, 0, u),
        ("east", east, u, 32767, v),
        ("north", north, v, 32767, u),
    ]
    for name, edge, const_axis, const_val, vary_axis in cases:
        assert len(edge) == n, f"{name}: 边索引应有 {n} 个点，实得 {len(edge)}"
        assert (const_axis[edge] == const_val).all(), (
            f"{name}: 该边上所有点的定值轴必须恒为 {const_val}，实得 "
            f"{sorted(set(const_axis[edge].tolist()))}（槽位串了？）"
        )
        along = vary_axis[edge]
        assert (np.diff(along) > 0).all(), f"{name}: 边内顺序必须严格递增，实得 {along.tolist()}"
