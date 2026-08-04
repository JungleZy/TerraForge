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

import math
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
    # spec: "To enforce proper byte alignment, padding is added before the IndexData
    # to ensure 2 byte alignment for IndexData16 and 4 byte alignment for IndexData32."
    # 解析必须照 spec 补齐，不能照实现写 —— 见 test_index_data_starts_on_a_spec_boundary。
    isize = np.dtype(index_dtype).itemsize
    pad = (-off) % isize
    assert data[off:off + pad] == b"\x00" * pad, (
        f"IndexData 之前的 {pad} 字节对齐 padding 缺失或非零：{data[off:off + pad]!r}"
    )
    off += pad
    # IndexData.triangleCount 按 spec 是【三角形数】，索引元素数 = triangleCount * 3。
    # 此前这里直接把该字段当索引元素数读（count=tri_count），与当时的编码端用了
    # 同一套错误约定，两边自洽所以测试通过 —— 而 Cesium 按 spec 读 triangleCount*3
    # 个索引，实测抛 RangeError: Invalid typed array length。解析必须照 spec 写，
    # 不能照实现写，否则这个测试测的是「实现和自己一致」而不是「实现符合 spec」。
    (tri_count,) = struct.unpack_from("<I", data, off)
    off += 4
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

    .5 的边界是真会踩到的：tile_size 恒为 2^k+1，n-1 是 2 的幂，i=(n-1)/2 时
    i*32767/(n-1) 恰好等于 16383.5（n=17 与 n=65 实测都是）。这里之所以仍能和生产端
    对上，不是因为没有 tie，而是因为两边都用 np.round —— numpy 的 round 是 half-to-even，
    16383.5 两边一致地进到 16384。若哪天生产端换成 int()/floor()/np.floor(x+0.5)，
    tie 的方向就会分叉，这条测试会红，那是它该红。
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


@pytest.mark.parametrize("n,index_dtype", [(17, np.uint16), (65, np.uint16), (257, np.uint32)])
def test_index_data_starts_on_a_spec_boundary(n, index_dtype):
    """IndexData 段必须按索引位宽对齐：uint16 补到 2 字节、uint32 补到 4 字节。

    spec 原文（CesiumGS/quantized-mesh README）："To enforce proper byte alignment,
    padding is added before the IndexData to ensure 2 byte alignment for
    IndexData16 and 4 byte alignment for IndexData32."

    这条不是形式主义，它决定读端从哪个字节开始读 triangleCount。IndexData 之前
    的字节数 = 88（header）+ 4（vertexCount）+ 6*vertexCount（u/v/h 三段 uint16），
    88 和 4 都是 4 的倍数，所以 6*vertexCount 在 vertexCount 为奇数时 ≡ 2 (mod 4)。
    而 tile_size 恒为 2^k+1 => vertexCount = (2^k+1)^2 **恒为奇数** =>
    **只要走 uint32 分支就必然缺 2 字节 padding**。
    实测 tile_size=257（66049 顶点）：编码端写 triangleCount=131072，Cesium 按 spec
    补齐 2 字节后读到的是 **2**，后面每个字段整体错位 —— 整块地形静默塌成两个
    三角形。与已修掉的 triangleCount bug 是同一种失效形态（HTTP 全 200 +
    任务 completed + 前端不报错 + 什么都不显示）。

    uint16 那两组（17/65）的 padding 长度恒为 0，列在这里是为了钉住「不该补的
    时候别乱补」—— 给规则网格无条件塞 2 字节会同时打红 golden 指纹。

    可达性：生产的 tile_size 硬编码 65（4225 顶点 => uint16，偏移 25442 本就是
    偶数），所以 uint32 分支目前只能从 CLI 的 --tile-size 走到。但那正是设计稿:406
    指定的排障出口，也是唯一能脱离 Flask 单独跑的入口。
    """
    data = encode_quantized_mesh(100.0, 30.0, 110.0, 40.0, _heights(n))

    # 完全独立地算偏移，不从被测字节流里反推。
    vcount = n * n
    width = np.dtype(index_dtype).itemsize
    assert (vcount > 65536) == (width == 4), f"n={n}: 用例自身的索引位宽预期写错了"
    off = HEADER_SIZE + 4 + vcount * 6
    pad = (-off) % width
    assert pad == (2 if (width == 4 and vcount % 2 == 1) else 0), "padding 长度的预期算错了"

    assert data[off:off + pad] == b"\x00" * pad, (
        f"n={n}: IndexData 之前应有 {pad} 字节零 padding，实得 {data[off:off + pad]!r}"
    )
    assert (off + pad) % width == 0

    (tri_count,) = struct.unpack_from("<I", data, off + pad)
    assert tri_count == 2 * (n - 1) ** 2, (
        f"n={n}: 按 spec 对齐后读到的 triangleCount={tri_count}，期望 {2 * (n - 1) ** 2}"
        f"（padding 缺失时 Cesium 在 tile_size=257 上读到的是 2）"
    )


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


# 自适应分支的测试全部按 n 参数化。17 是历史用例，65 是生产默认
# （TileParams.tile_size，见 dem_task_tiler.py）—— 在补这条之前，mesh= 路径
# 从来没有在生产实际使用的网格尺寸下跑过。
_ADAPTIVE_N = [17, 65]


@pytest.mark.parametrize("n", _ADAPTIVE_N)
def test_encode_with_explicit_mesh_writes_that_mesh(n):
    """传入 mesh 时，写出的顶点数/三角形数必须来自 mesh，而非满网格。"""
    h, verts, tris = _rtin_mesh(n)

    data = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    vcount, indices, edges = _parse(data, np.uint16)
    assert vcount == len(verts)
    assert len(indices) == len(tris) * 3


@pytest.mark.parametrize("n", _ADAPTIVE_N)
def test_encode_with_mesh_keeps_all_border_points_in_edge_indices(n):
    """自适应网格下，四条边索引仍必须覆盖整条边（边界满密度的体现）。"""
    h, verts, tris = _rtin_mesh(n)
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
#
# n=257 的值更新过一次（补上 spec 要求的 uint32 对齐 padding 那轮）：
#   旧值 d7868ff5919837613f792c69976d5ce6e272ef60750716e2b3f7a62fe09391b8
# 变更范围已实测确认**只是那 2 个 padding 字节** —— 把新 body 里偏移 396298
# （= 4 + 6*66049，body 内相对偏移）处的 2 个字节剔掉后重新哈希，与旧值逐字符一致。
# n=17 / n=65 走 uint16、padding 长度为 0，两个值一字未动，跨版本护栏没有断。
_GOLDEN_REGULAR_GRID_BODY_SHA256 = {
    17: "0772d3632a187bc6400e98ab953361f7ae286b24d4f13a83868534592bb9a792",
    65: "2d9da4adc5721d4f0805876db352ca0d534c192f0b742117fbcfea2daa3aaf6b",
    257: "8e2fe2b02ef79f8d55a12809ba7bec05270035778d90fe30cfe6fe95c5faab56",
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


# ---------------------------------------------------------------------------
# QuantizedMeshHeader —— 开头那 88 字节
#
# 布局（= encode_quantized_mesh 里 struct.pack 的顺序）：
#   center (3×float64) | minH,maxH (2×float32) | boundingSphere center (3×float64)
#   | radius (float64) | horizonOcclusionPoint (3×float64)
#
# 为什么这里用数值容差、而不是像 body 那样钉一个 sha256：这 88 字节里有 80 字节最终
# 来自 lonlat_to_ecef 的 np.sin / np.cos。IEEE-754 **不要求**超越函数正确舍入 ——
# glibc 与 MSVC 的 libm 实现不同，numpy 的 sin/cos 还按 CPU 特性（AVX512/AVX2/SSE）
# 做 SIMD 分派，同一台机器换个特性档末位就可能变。本项目 Linux 与 Windows 都要出
# Nuitka 包，拿它当指纹就是偶发红灯。1e-6 米的容差留出约 1000 ULP（float64 在 5e6
# 量级的 ULP 约 9e-10 米），ULP 漂移进不来；而任何真实的编码错误（分量互换、写 0、
# 取错基准点）都是公里级的，一撞就死。
# 剩下 8 字节的 minH/maxH 是 float64 -> float32 的**直接转换**，IEEE 精确、跨平台
# 逐位可复现，所以那两个字段精确断言，不给容差。
#
# 这段此前是零覆盖的（golden 只钉 data[88:]）。实测四个 header 变异——radius 强制写 0、
# horizonOcclusionPoint 全写 0、center 的 x/y 互换、minH/maxH 互换——全部骗过了当时的
# 1001 条测试。前两个在 Cesium 里的后果是视锥剔除 / 地平线遮挡剔除把每张瓦片剔掉：
# 又一款「HTTP 全 200 + 任务 completed + 前端不报错 + 什么都不显示」，与已修掉的
# triangleCount bug 是同一种失效形态。
# ---------------------------------------------------------------------------

_HEADER_FMT = "<dddffddddddd"  # 3*8 + 2*4 + 3*8 + 8 + 3*8 = 88
assert struct.calcsize(_HEADER_FMT) == HEADER_SIZE

# (bbox, n)。带一组南半球+西经的 bbox：ECEF 三个分量在那里全变号，
# 分量互换/取绝对值一类的错误在只测东北半球时可能蒙混过关。
# 最后一组是地理切片方案的 z0 瓦片（180°×180°，跨南北两极）—— build_terrain
# 里 `if z <= 4` 强制出全球图，每个 DEM 任务都会真的生成这两张。它是退化情形：
# 四个经纬角点全部塌到南北极这两个物理点上，离瓦片中心最远的点跑到了
# **西/东边界的中点**（赤道上），任何「取角点」的推理在这里都会失效。
#
# 最后一组是把 z0 的南边界挪了 0.1° 的**假想**瓦片。当前的地理切片方案生不出它，
# 但它是外接球那条推理唯一的裕度所在：沿一条经线的最远点只有在经度跨度 ≳179.233°
# 时才会离开端点跑到边内部去，位置约 lat* ≈ -148.4·lat_c；z0 的跨度精确是 180°，
# 正在这个退化区间里，只是因为 z0 的 lat_c 恰好为 0（=> lat* = 0，极值点正好落在
# 西边中点上）才没出事。南边界一挪，「角点 + 边中点」那套候选表立刻缺 220 m
# （挪 0.4° 缺 3493 m）。谁要是以「切片方案生不出这张瓦片」为由删掉它，就等于把
# 「有人改动纬度分割」这个触发条件重新变成静默失败。
_HEADER_CASES = [
    ((100.0, 30.0, 101.0, 31.0), 17),
    ((100.0, 30.0, 110.0, 40.0), 257),
    ((-70.0, -40.0, -69.0, -39.0), 17),
    ((-180.0, -90.0, 0.0, 90.0), 17),
    ((-180.0, -89.9, 0.0, 90.0), 65),
]
_HEADER_IDS = ["ne-1deg-n17", "ne-10deg-n257", "sw-1deg-n17", "z0-west-hemisphere-n17",
               "z0-south-shifted-n65"]


def _parse_header(data: bytes) -> dict:
    f = struct.unpack_from(_HEADER_FMT, data, 0)
    return {
        "center": np.array(f[0:3], dtype=np.float64),
        "min_h": f[3],
        "max_h": f[4],
        "bs_center": np.array(f[5:8], dtype=np.float64),
        "radius": f[8],
        "hop": np.array(f[9:12], dtype=np.float64),
    }


def _expected_height_range(n: int) -> tuple[float, float]:
    """独立于实现算 _heights(n) 的极值：h = 100 + 2.5x + 1.25y，x,y ∈ [0, n-1]。

    刻意不写 _heights(n).min()/.max() —— 那和生产端读的是同一个数组，
    「取错了轴」「拿的是子网格」这类错误会一起错、一起对上。
    """
    return 100.0, 100.0 + 3.75 * (n - 1)


_SURFACE_SAMPLES = 361   # 每个方向的采样数；z0（180° 跨度）上 = 0.5°/格


def _tile_prism_max_dist(bbox, n, center):
    """稠密采样【瓦片所在的高程棱柱】，返回 (采样到的最大距离, 采样残差上界)。

    棱柱 = bbox 这块经纬范围 × 高程区间 [h_min, h_max]。编码出去的顶点必然
    落在这个棱柱里（经纬在 bbox 内、高程在 [h_min,h_max] 内），所以这个量是
    **半径的独立上界**：它完全不看实现的顶点数组，只由 bbox 和高程极值决定。
    用途也只剩这一个 —— 拦住「半径被吹大」（拿整球半径、把 h 加两遍、×2）。

    ⚠️ 它**不再**当半径的下界用。以前那版断言的是「球必须包住整个棱柱表面」，
    那个要求太强了：棱柱的 h_max 那一层在「DEM 实际只有 h_min 那么高」的经纬
    位置上根本没有任何几何，Cesium 只渲染字节流里那些顶点连成的三角形。
    按棱柱要求半径会让球无谓地变大，也会掩盖「半径到底有没有包住真顶点」这个
    真问题。下界改由 _decoded_vertices_ecef 提供 —— 那是从字节流里解出来的、
    真会被渲染的点。顶点是棱柱的子集，所以新保证严格弱于旧保证，
    但它才是**正确的**保证：定义外接球的是几何，不是包围盒。

    采样方式：lon/lat 各 _SURFACE_SAMPLES 个等分点，高程只取 h_min / h_max
    两层。**只取两层是严格的，不是近似** —— lonlat_to_ecef 对 h 是仿射的
    （P(h) = A + h·n̂，n̂ 是单位椭球法向），所以 |P(h) - center| 是 h 的凸函数，
    在 [h_min, h_max] 上的最大值必在端点取到。

    残差上界：任取棱柱上一点 P，同层必有采样点 Q 使 |P-Q| ≤ 半个格子对角线 D/2，
    于是 d(P) ≤ d(Q) + D/2 ≤ dmax + D/2。这里的 D 直接从采样出来的 ECEF 点
    量出来（两条对角线取大），不是估的。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import lonlat_to_ecef

    w, s, e, nn = bbox
    h_min, h_max = _expected_height_range(n)
    llon, llat = np.meshgrid(
        np.linspace(w, e, _SURFACE_SAMPLES, dtype=np.float64),
        np.linspace(s, nn, _SURFACE_SAMPLES, dtype=np.float64),
    )
    c = np.asarray(center, dtype=np.float64)

    dmax = 0.0
    diag = 0.0
    for h in (h_min, h_max):
        pts = lonlat_to_ecef(llon, llat, np.full_like(llon, h))
        dmax = max(dmax, float(np.linalg.norm(pts - c, axis=-1).max()))
        diag = max(
            diag,
            float(np.linalg.norm(pts[1:, 1:] - pts[:-1, :-1], axis=-1).max()),
            float(np.linalg.norm(pts[1:, :-1] - pts[:-1, 1:], axis=-1).max()),
        )
    return dmax, diag / 2.0


# 「剥掉量化余量后的半径」与「字节流里顶点最大距离」允许差多少米。
# 实测 15 组配置（n ∈ {17,65,257} × 1°瓦片 / z0 / z0南边界挪0.1° / 随机 -400~8848 m
# 地形 / 全平地形）里这个差最大 0.103 m，取 10 倍留余量。
# 上限这一侧不能取「可证的」_quantisation_slack（z0 上 1223 m）—— 那个界太松，
# 会把这条测试要抓的 220 m 回归整个吞掉。见 test_header_bounding_sphere_radius_
# encloses_every_shipped_vertex 的 docstring 第 2 条。
_GEOMETRIC_RADIUS_TOL = 1.0


def _quantisation_slack(bbox, hdr) -> float:
    """从精确格点到字节流里那个量化过的顶点，位置最多挪多远（米）。

    u/v/height 三段各自量化到 0..32767 的整数格上，所以单个顶点的坐标误差不超过
    一个量化步：高程 (maxH-minH)/32767，经度 (east-west)/32767，纬度
    (north-south)/32767。经纬换成弧长时用 (WGS84_A + |maxH|) 当半径 —— 那是地球
    上任何一点到自转轴/地心距离的上界，所以算出来的是弧长的上界。
    距离函数 1-Lipschitz（|d(P)-d(Q)| ≤ |P-Q|），三项直接相加就是距离的误差上界。

    全部输入取自 bbox 与 header 的 minH/maxH，不读实现的任何中间变量。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import WGS84_A

    w, s, e, nn = bbox
    return (
        (hdr["max_h"] - hdr["min_h"]) / 32767.0
        + (WGS84_A + abs(hdr["max_h"])) * math.radians(abs(e - w) + abs(nn - s)) / 32767.0
    )


def _decoded_vertices_ecef(data: bytes, bbox) -> np.ndarray:
    """把字节流里的顶点按 spec 还原成 ECEF —— 这是 Cesium 真正会渲染的那些点。

    刻意只经过【字节流 + spec 公式】，不碰实现内部的任何数组，也不重算一遍
    实现挑候选点的那套逻辑。三段 u/v/height 都是 zigzag(相邻差分) 的 uint16，
    反解后按 spec 的线性映射还原：
        lon = west  + (east - west)   * u / 32767
        lat = south + (north - south) * v / 32767
        height = minH + (maxH - minH) * h / 32767
    minH/maxH 取 header 里那两个 float32 —— 那正是 Cesium 拿去 lerp 的值。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import lonlat_to_ecef

    w, s, e, nn = bbox
    (vcount,) = struct.unpack_from("<I", data, HEADER_SIZE)
    u, v, h = _decode_uvh(data, vcount)
    hdr = _parse_header(data)
    return lonlat_to_ecef(
        w + (e - w) * u / 32767.0,
        s + (nn - s) * v / 32767.0,
        hdr["min_h"] + (hdr["max_h"] - hdr["min_h"]) * h / 32767.0,
    )


@pytest.mark.parametrize("bbox,n", _HEADER_CASES, ids=_HEADER_IDS)
def test_header_min_max_height_are_exact_and_not_swapped(bbox, n):
    """minH/maxH 必须精确等于高程极值，且 minH 在前。

    互换后瓦片高度范围倒挂；这 8 字节是 float64->float32 的直接转换，跨平台逐位
    可复现，所以不给容差 —— 给了容差反而会让「min/max 互换」在近似平坦的 DEM 上溜过去。
    """
    data = encode_quantized_mesh(*bbox, _heights(n))
    hdr = _parse_header(data)
    exp_min, exp_max = _expected_height_range(n)
    assert exp_min < exp_max, "构造的高程必须有起伏，否则互换测不出来"
    assert hdr["min_h"] == float(np.float32(exp_min)), (
        f"minH={hdr['min_h']} 期望 {exp_min}（若等于 {exp_max} 则是 min/max 写反了）"
    )
    assert hdr["max_h"] == float(np.float32(exp_max)), (
        f"maxH={hdr['max_h']} 期望 {exp_max}（若等于 {exp_min} 则是 min/max 写反了）"
    )
    assert hdr["min_h"] < hdr["max_h"]


@pytest.mark.parametrize("bbox,n", _HEADER_CASES, ids=_HEADER_IDS)
def test_header_center_is_the_tile_centre_in_ecef(bbox, n):
    """center 与 boundingSphere center 都必须是瓦片中心 (lon_c, lat_c, h_c) 的 ECEF。

    逐分量断言，不是断言集合或范数 —— x/y 互换会保持范数不变，只有逐分量才杀得掉。
    center 错位 = 全部顶点解码基准点错位，地形整体平移。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import lonlat_to_ecef

    w, s, e, nn = bbox
    data = encode_quantized_mesh(*bbox, _heights(n))
    hdr = _parse_header(data)

    lon_c = 0.5 * (w + e)
    lat_c = 0.5 * (s + nn)
    h_min, h_max = _expected_height_range(n)
    h_c = 0.5 * (h_min + h_max)
    exp = lonlat_to_ecef(np.array([lon_c]), np.array([lat_c]), np.array([h_c]))[0]

    for axis, got, want in zip("xyz", hdr["center"], exp):
        assert got == pytest.approx(want, abs=1e-6), (
            f"center.{axis}={got!r} 期望 {want!r}（差 {got - want:.3e} m）"
        )
    for axis, got, want in zip("xyz", hdr["bs_center"], exp):
        assert got == pytest.approx(want, abs=1e-6), (
            f"boundingSphere.center.{axis}={got!r} 期望 {want!r}"
        )

    # 完全不经过 lonlat_to_ecef 的独立交叉验证：ECEF 里 (x, y) = k·(cos lon, sin lon)
    # 且 k>0，所以 atan2(y, x) 就是经度本身；纬度用地心纬度近似地理纬度（WGS84 下
    # 二者相差 <0.2°）。x/y 互换会把经度打到 90-lon，任一分量写 0 也当场露馅。
    got_lon = math.degrees(math.atan2(hdr["center"][1], hdr["center"][0]))
    assert got_lon == pytest.approx(lon_c, abs=1e-9), (
        f"由 center 反解的经度 {got_lon} != {lon_c}（x/y 互换？）"
    )
    got_lat = math.degrees(
        math.atan2(hdr["center"][2], math.hypot(hdr["center"][0], hdr["center"][1]))
    )
    assert got_lat == pytest.approx(lat_c, abs=0.25), (
        f"由 center 反解的地心纬度 {got_lat} 与地理纬度 {lat_c} 差得太远"
    )


@pytest.mark.parametrize("bbox,n", _HEADER_CASES, ids=_HEADER_IDS)
def test_header_bounding_sphere_radius_encloses_every_shipped_vertex(bbox, n):
    """radius 必须包住【字节流里那些顶点】—— 写 0/写小/写大都会被 Cesium 拿去剔除。

    radius=0 时每张瓦片都被剔掉，地形全不可见，且全程 HTTP 200 + 任务 completed。

    **下界的期望值从字节流里解出来**（_decoded_vertices_ecef），不从实现挑候选点的
    那套逻辑里抄。这条测试的前两版都栽在「期望值镜像了实现的假设」上：
      - 第一版断言「球包住这 8 个角点」，而 z0 的最远点根本不在角点上（四个经纬
        角点全塌到南北两极），实测 2/2 张 z0 瓦片缺 15114 m，测试却是绿的；
      - 第二版补了 4 个 h_max 边中点，缺口在当前切片方案下确实归零，但裕度也
        精确是零 —— 见 _HEADER_CASES 里 z0-south-shifted 那条注释。
    顶点集合没有这个问题：它就是渲染的对象本身，不需要论证「极值在哪」。
    三角形都在顶点凸包里、球是凸集，所以「包住全部顶点」= 「包住全部几何」。

    记 D = 精确格点的最远距离、dv = 字节流里顶点的最远距离、
    slack = _quantisation_slack 给出的量化位移上界。实现写的是 radius = D + slack。

    三道断言各司其职：
      1. `radius >= dv`（几乎零松弛）—— 定义性质，且是**可证**的：精确格点与它
         量化后的样子逐点差不超过 slack，max 是 1-Lipschitz 的，所以 D >= dv - slack，
         于是 radius = D + slack >= dv。实现里那个余量存在的意义就在这 ——
         它把「包住」从实测事实变成可证事实。
      2. `|(radius - slack) - dv| <= 1 m` —— **真正有牙的那条**。它要求剥掉余量之后
         的几何部分**就是**顶点最大距离，不多不少。
         为什么容差取 1 m 而不是可证的 slack：slack 是个极松的上界（z0 上 1223 m，
         因为它按「u/v 各错一个量化步 => 位置错一整段弧长」直接相加，而实际上极值
         附近的距离对位置是二阶不敏感的）。实测 15 组配置（n∈{17,65,257} ×
         1°/z0/z0南边界挪0.1°/随机-400~8848m 地形/全平）里 |D - dv| 最大只有
         **0.103 m**。**必须用实测的紧界**：这条测试要抓的回归（半径退回「角点 +
         边中点」候选表）在 z0 南边界挪 0.1° 时的缺口是 **220 m** —— 用 1223 m 的
         可证界去卡，那个回归会原样溜过去（实测：M5 变异下全量 1045 条一条不红）。
         1 m 是实测最坏值的约 10 倍，离 220 m 还差两个数量级，不会偶发红灯。
         这条断言与实现共享 slack 的算式（_quantisation_slack）—— 那是有意的：
         余量被删掉或改小时它当场红（实测 M7 变异）。**承重的那半（顶点最大距离）
         是从字节流独立解出来的，不是从实现的候选点逻辑里抄的。**
      3. `radius <= 棱柱采样 + 残差` —— 一道完全不看顶点数组的独立天花板，
         只由 bbox 和高程极值决定（_tile_prism_max_dist）。

    1e-6 m 的浮点余量：dv 走的是几千个点的 SIMD 归约，实现走的是另一个形状的
    数组，末位差异不共模。1e-6 m 在 9e6 量级上约 500 ULP。
    """
    data = encode_quantized_mesh(*bbox, _heights(n))
    hdr = _parse_header(data)
    verts = _decoded_vertices_ecef(data, bbox)
    dv = float(np.linalg.norm(verts - hdr["bs_center"], axis=-1).max())
    slack = _quantisation_slack(bbox, hdr)
    prism, resid = _tile_prism_max_dist(bbox, n, hdr["center"])

    assert hdr["radius"] > 0.0, "radius 必须为正 —— 写 0 会让 Cesium 剔掉每张瓦片"
    assert hdr["radius"] >= dv - 1e-6, (
        f"radius={hdr['radius']:.3f} 小于字节流里顶点的最大距离 {dv:.3f}"
        f"（缺 {dv - hdr['radius']:.3f} m），包不住实际渲染的几何"
    )
    assert abs((hdr["radius"] - slack) - dv) <= _GEOMETRIC_RADIUS_TOL, (
        f"剥掉量化余量 {slack:.3f} m 之后 radius 的几何部分是 "
        f"{hdr['radius'] - slack:.3f}，而字节流里顶点的最大距离是 {dv:.3f}"
        f"（差 {hdr['radius'] - slack - dv:+.3f} m，容差 {_GEOMETRIC_RADIUS_TOL} m）——"
        f"半径不是按【全部顶点】算的"
    )
    assert hdr["radius"] <= prism + resid, (
        f"radius={hdr['radius']:.3f} 超出高程棱柱的采样最大距离 {prism:.3f} "
        f"加采样残差 {resid:.3f}"
    )


def test_bounding_sphere_radius_also_covers_the_adaptive_mesh():
    """自适应网格的顶点是格点的子集，同一个 radius 必须照样包住它。

    header 由 bbox + 高程极值决定、两条分支逐字节相同（见
    test_header_is_identical_between_regular_and_adaptive_paths），所以这条真正
    验的是「子集也被覆盖」这个推论没有被某种奇怪的实现打破 —— 比如哪天有人
    改成「按 mesh 里实际用到的顶点算半径」，规则网格分支就会和自适应分支分叉。
    """
    bbox = (100.0, 30.0, 101.0, 31.0)
    n = 65
    h, verts_idx, tris = _rtin_mesh(n)
    data = encode_quantized_mesh(*bbox, h, mesh=(verts_idx, tris))
    hdr = _parse_header(data)
    pts = _decoded_vertices_ecef(data, bbox)
    dv = float(np.linalg.norm(pts - hdr["bs_center"], axis=-1).max())
    assert len(pts) < n * n, "构造的网格必须真被简化过，否则这条测试没意义"
    assert hdr["radius"] >= dv - 1e-6, (
        f"自适应分支 radius={hdr['radius']:.3f} 包不住它自己的顶点（最远 {dv:.3f}）"
    )


@pytest.mark.parametrize("bbox,n", _HEADER_CASES, ids=_HEADER_IDS)
def test_header_horizon_occlusion_point_is_beyond_the_tile_along_the_centre_ray(bbox, n):
    """horizonOcclusionPoint 必须落在「地心 -> 瓦片中心」这条射线上、且在瓦片之外。

    全写 0 时地平线遮挡剔除失效，瓦片会成片消失 —— 同样是静默失败。
    断言的是它的两个几何性质（方向、量级），不复述 scale 的具体算式：
    在椭球缩放空间（x/a, y/a, z/b）里，它的方向必须与 center 一致，模长必须 >=1
    （在单位球面之外）且 > center 的模长（在瓦片之外）。
    """
    from src.services.terrain_tiling.cesiumlab_terrain import WGS84_A, WGS84_B

    data = encode_quantized_mesh(*bbox, _heights(n))
    hdr = _parse_header(data)
    hop, center = hdr["hop"], hdr["center"]

    assert np.isfinite(hop).all(), f"horizonOcclusionPoint 含非有限值：{hop}"
    scale_v = np.array([WGS84_A, WGS84_A, WGS84_B])
    hop_s = hop / scale_v
    ctr_s = center / scale_v
    hop_mag = float(np.linalg.norm(hop_s))
    ctr_mag = float(np.linalg.norm(ctr_s))

    assert hop_mag >= 1.0, (
        f"缩放空间模长 {hop_mag} < 1，遮挡点跑到椭球内部了（全写 0 时为 0）"
    )
    assert hop_mag > ctr_mag, (
        f"缩放空间模长 {hop_mag} 不大于瓦片中心的 {ctr_mag}，遮挡点没在瓦片之外"
    )
    # 上限必须跟着瓦片尺寸走，不能是常数：遮挡点本来就要退到瓦片之外，瓦片越大退得
    # 越远。z0（180°×180°，外接球半径 9020 km）实测 2.41 —— 旧的 `<= 2.0` 是拿 1° 瓦片
    # （实测 1.012）标定的魔数，加进 z0 用例后当场变红，而那不是缺陷。
    # 换成「不超过瓦片中心 + 2 个外接球直径」：对小瓦片反而比 2.0 紧得多
    # （1° 瓦片上限 1.025），量级写错 / 单位搞混那类错误照样一撞就死。
    ceiling = ctr_mag + 2.0 * (hdr["radius"] + hdr["max_h"]) / WGS84_B
    assert hop_mag <= ceiling, (
        f"缩放空间模长 {hop_mag} 超出 {ceiling}（瓦片中心 {ctr_mag} + 2 个外接球直径），大得离谱"
    )
    for axis, got, want in zip("xyz", hop_s / hop_mag, ctr_s / ctr_mag):
        assert got == pytest.approx(want, abs=1e-9), (
            f"horizonOcclusionPoint 的方向分量 {axis} 与 center 不一致："
            f"{got} vs {want}（分量互换 / 变号？）"
        )


def test_header_is_identical_between_regular_and_adaptive_paths():
    """header 只由 bbox + 高程极值决定，不该随三角网变化。

    Task 6/7 还要继续改 encode_quantized_mesh，这条是防止 header 被三角网分支污染。
    """
    n = 17
    h, verts, tris = _rtin_mesh(n)
    a = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h)
    b = encode_quantized_mesh(100.0, 30.0, 101.0, 31.0, h, mesh=(verts, tris))
    assert a[:HEADER_SIZE] == b[:HEADER_SIZE]


@pytest.mark.parametrize("n", _ADAPTIVE_N)
def test_encode_with_mesh_writes_correct_vertex_uvh(n):
    """自适应分支的顶点数据必须逐个对上：u 来自列、v 来自行、高程来自该顶点。

    只断言 vcount 是测不出内容的 —— u/v 互换、量化尺度 32767 写成 32766、
    高程取错顶点，三种都不改变任何长度字段。
    """
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


@pytest.mark.parametrize("n", _ADAPTIVE_N)
def test_encode_with_mesh_edge_indices_are_sorted_and_on_the_right_edge(n):
    """四条边索引：槽位不能串（后果严重），边内顺序确定且无重复（后果轻，但值得钉）。

    **槽位**是要害：Cesium 按 westIndices/southIndices/eastIndices/northIndices 的
    槽位决定用哪一轴当排序键、以及裙边（skirt）往哪个方向位移。槽位串了，裙边朝向
    和边界匹配都会错。

    **顺序**则被高估过。实测 Cesium 1.143.0 的
    Workers/createVerticesFromQuantizedTerrainMesh.js 会无条件复制并重排这四条边
    （west 按 v 升序、east 按 v 降序、south 按 u 降序、north 按 u 升序），生产端给
    什么顺序都会被覆盖；quantized-mesh-1.0 spec 对顺序也只字未提。所以「顺序乱了会
    导致地形裂缝」是不成立的。这半条断言保留的理由换成两点：它顺带钉住了「同一条边上
    没有重复顶点」（严格递增 => 无重复），以及编码输出的确定性（同输入同字节）。
    """
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
