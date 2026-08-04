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


def _heights(n: int) -> np.ndarray:
    yy, xx = np.mgrid[0:n, 0:n]
    return (100.0 + xx * 2.5 + yy * 1.25).astype(np.float64)


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
