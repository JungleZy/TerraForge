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

from services.terrain_tiling.cesiumlab_terrain import encode_quantized_mesh

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
    (icount,) = struct.unpack_from("<I", data, off)
    off += 4
    isize = np.dtype(index_dtype).itemsize
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
