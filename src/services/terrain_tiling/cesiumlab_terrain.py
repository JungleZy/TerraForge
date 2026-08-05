#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cesiumlab_terrain.py
====================
Vendored from:
  E:/EarthVisLabApps/cesiumlab/4.0.17/python_terrain/cesiumlab_terrain.py

Purpose:
  DEM (any GDAL-readable raster) -> Cesium quantized-mesh-1.0 terrain tiles
  compatible with cesiumlab 4.0.17 output layout:
    out/
      layer.json
      meta.json
      {z}/{x}/{y}.terrain
"""

from __future__ import annotations

import argparse
import functools
import gzip
import io
import itertools
import json
import logging
import math
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np

try:
    from osgeo import gdal, osr
except ImportError as e:
    # ImportError (not sys.exit/SystemExit) so `except Exception` callers — e.g.
    # dem_task_tiler's lazy-import wrapper and the tiling job thread — can catch
    # it and mark the job failed instead of dying silently with status 'running'.
    raise ImportError(
        "Missing GDAL Python bindings: pip install gdal (or conda install -c conda-forge gdal)"
    ) from e

# Avoid gdal.UseExceptions() here. It attempts to import osgeo.gdal_array, which may be absent
# depending on how GDAL Python bindings were built. This tiler doesn't require gdal_array.

logger = logging.getLogger(__name__)

WGS84_A = 6378137.0
WGS84_B = 6356752.3142451793
WGS84_E2 = 1.0 - (WGS84_B * WGS84_B) / (WGS84_A * WGS84_A)

DEG_TO_M = 111320.0          # 赤道处 1° **经度** 的米数，仅用于 max_error 的量级换算
DEFAULT_MAX_ERROR_K = 0.15   # 允许高程误差 = K * 顶点间距（见 _max_error_for_level）


def lonlat_to_ecef(lon_deg: np.ndarray, lat_deg: np.ndarray, h: np.ndarray) -> np.ndarray:
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + h) * cos_lat * np.cos(lon)
    y = (n + h) * cos_lat * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h) * sin_lat
    return np.stack([x, y, z], axis=-1)


def _vertex_normals_ecef(lons: np.ndarray, lats: np.ndarray,
                         heights: np.ndarray) -> np.ndarray:
    """逐顶点法线（面积加权），在 ECEF 空间计算。输入 (n,n)，输出 (n,n,3) 单位向量。

    **必须用 ECEF 而不是经纬度空间**：经纬度下 1° 经距按 cos(纬度) 收缩，直接在
    (lon·111320, lat·111320, h) 上叉积等于把地形东西向拉伸 1/cos(lat) 倍，法线
    随纬度系统性偏斜。实测把同一块真实地形（天山 N42E086 的 z11 瓦片，65×65，
    高差 1758 m）搬到各纬度，把经纬度空间的结果按 ENU→ECEF 转回来再比，
    夹角中位数：lat0 0.05° / lat20 0.45° / lat42 **2.37°** / lat60 6.80° /
    lat80 28.40°。赤道≈0、随纬度单调放大，正是「东西向拉伸」这个解释本身的验证。

    lons/lats 可以是 (n,n) 网格，也可以是 (1,n)/(n,1) 的广播轴 —— 后者让
    lonlat_to_ecef 里的 radians/sin/cos/sqrt 只算 n 次而不是 n²，结果与
    meshgrid 版**逐位相同**（5 组 bbox × 3 种 n × 2 种高程场实测 0 差异），
    heights 的形状 (n,n) 会把结果撑回 (n,n,3)。_tile_normals 走的是广播那条。

    三角形切法与 _mesh_constants 一致（每格两片：(a0,a1,a2) 与 (a1,a3,a2)），
    权重用未归一化的叉积 —— 其模长 = 2×三角形面积，累加即天然面积加权。
    最后统一朝外（与地心方向点积为正），因此**不依赖三角形绕序**：RTIN 绕向
    在本轮改过一次（dad22bd77），法线不该再被同一件事绊一遍。
    """
    xyz = lonlat_to_ecef(lons, lats, heights)
    acc = np.zeros_like(xyz)
    v0 = xyz[:-1, :-1]      # a0 = (j,   i)
    v1 = xyz[:-1, 1:]       # a1 = (j,   i+1)
    v2 = xyz[1:, :-1]       # a2 = (j+1, i)
    v3 = xyz[1:, 1:]        # a3 = (j+1, i+1)
    n1 = np.cross(v1 - v0, v2 - v0)      # 三角形 (a0,a1,a2)
    n2 = np.cross(v3 - v1, v2 - v1)      # 三角形 (a1,a3,a2)
    # 每片面法线散给它的三个顶点。切片是基本索引（视图），+= 就地累加，
    # 不需要 np.add.at —— 六条语句各自命中的元素集合内部无重复。
    for sl, nr in ((np.s_[:-1, :-1], n1), (np.s_[:-1, 1:], n1), (np.s_[1:, :-1], n1),
                   (np.s_[:-1, 1:], n2), (np.s_[1:, 1:], n2), (np.s_[1:, :-1], n2)):
        acc[sl] += nr
    ln = np.linalg.norm(acc, axis=-1, keepdims=True)
    ln[ln == 0] = 1.0
    out = acc / ln
    up = xyz / np.linalg.norm(xyz, axis=-1, keepdims=True)
    out[(out * up).sum(-1) < 0] *= -1
    return out


def _oct_encode(normals: np.ndarray) -> np.ndarray:
    """单位向量 (N,3) -> oct16 编码 (N,2) uint8。

    quantized-mesh-1.0 的 Oct-Encoded Per-Vertex Normals 扩展（extensionId=1）。
    算法与 Cesium 的 `AttributeCompression.octEncode` 一致：先按 L1 范数投影到
    八面体，z<0 的那一半折叠到外圈（|x|+|y|>1 就是解码端区分上下半球的唯一
    依据），再把 [-1,1] 映射到 [0,255]。

    **舍入必须是 half-up，不能用 np.round。** Cesium 的 toSNorm 是
    `Math.round((clamp(e,-1,1)*.5+.5)*255)`，而 JS 的 Math.round 平局一律进位；
    numpy 的 np.round 是 half-to-even。平局不是理论边界：t=(p*0.5+0.5)*255
    精确落在 k+0.5 上的 double 实测有 2164 个（在 p=(2(k+0.5)/255)-1 附近用
    nextafter 逐 double 扫出来的），其中 744 个 k 是偶数 —— 那些点上两种舍入
    差 1 个 LSB。
    ⚠️ **别把它说成"修了一个 bug"**：实测真实地形法线（天山 z9-12 共 36 张
    瓦片、304200 个分量）平局出现 **0 次**，且 1 个 LSB 的视觉后果本来也可以
    忽略。这里要的是"与 Cesium 的编码器逐位一致"这句话能成立而不是大概成立 ——
    代价是一行 floor，那就没有理由用一个已知会分歧的写法。
    x 恒在 [0,255]：floor 与 x-floor(x) 都是精确运算，所以下面这个写法与
    ES 规范里 Math.round 对 x>=0 的定义逐位相同。

    l1==0 的兜底：_vertex_normals_ecef 的输出是单位向量（模长为 0 的地方被置成
    1 再除），L1 范数因此恒 >=1，生产路径到不了这里；留着是因为零向量会让 p 变
    NaN，而 NaN 转 uint8 在 numpy 里只刷一条 RuntimeWarning（默认不中断）、
    落出来的字节值是平台相关的 —— 又是一条"不报错但字节是垃圾"的路。
    """
    v = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    l1 = np.abs(v).sum(axis=1, keepdims=True)
    l1 = np.where(l1 == 0.0, 1.0, l1)
    p = v[:, :2] / l1
    # ⚠️ 这里的 z 是 **ECEF 的 Z 轴**（指北极），不是"法线朝下"：法线基本沿地心
    # 方向，符号约等于 sin(纬度)。实测同一块地形搬到各纬度，z<0 的比例
    # lat-42 100% / lat0 45% / lat+5 0.9% / lat+42 **0%** —— 折叠分支是**南半球
    # 专属**路径，本项目手头的 DEM 全在 N28~N52，删掉它在现有数据上零症状。
    neg = v[:, 2] < 0.0
    if neg.any():
        px = p[neg, 0].copy()
        py = p[neg, 1].copy()
        # signNotZero: e<0 ? -1 : 1 —— 0 和 -0.0 都算正号（JS 里 -0<0 为 false，
        # numpy 里 -0.0>=0.0 为 True，两边一致）
        p[neg, 0] = (1.0 - np.abs(py)) * np.where(px >= 0.0, 1.0, -1.0)
        p[neg, 1] = (1.0 - np.abs(px)) * np.where(py >= 0.0, 1.0, -1.0)
    t = (np.clip(p, -1.0, 1.0) * 0.5 + 0.5) * 255.0
    floor = np.floor(t)
    return (floor + (t - floor >= 0.5)).astype(np.uint8)


@dataclass
class GeographicTilingScheme:
    tile_size: int = 17

    def tile_count(self, level: int) -> Tuple[int, int]:
        return 2 << level, 1 << level

    def tile_extent_deg(self, level: int, x: int, y: int) -> Tuple[float, float, float, float]:
        nx, ny = self.tile_count(level)
        west = -180.0 + 360.0 * x / nx
        east = -180.0 + 360.0 * (x + 1) / nx
        south = -90.0 + 180.0 * y / ny
        north = -90.0 + 180.0 * (y + 1) / ny
        return west, south, east, north

    def estimate_max_level(self, pixel_size_deg: float) -> int:
        if pixel_size_deg <= 0:
            return 14
        # 瓦片顶点网格是 tile_size x tile_size（见 _worker_tile 的 linspace：
        # 端点含边界），即 tile_size-1 个采样间隔均分瓦片的 180°/2^z 纬度跨度；
        # 间隔追上源像素尺寸即可。此前硬编码 180/64（=65 顶点网格），无视
        # self.tile_size，tile_size=17 时自动层级少算约 2 级（M22）。
        intervals = max(1, self.tile_size - 1)
        deg_per_tile_pixel = 180.0 / intervals
        ratio = deg_per_tile_pixel / pixel_size_deg
        return max(0, int(math.ceil(math.log2(ratio))))


def build_input_vrt(inputs: list[str], vrt_path: str | None = None) -> str:
    if len(inputs) == 1:
        return inputs[0]
    if vrt_path is None:
        import tempfile

        fd, vrt_path = tempfile.mkstemp(suffix=".vrt", prefix="cesiumlab_terrain_")
        os.close(fd)
    opts = gdal.BuildVRTOptions(resampleAlg="bilinear", addAlpha=False)
    vrt = gdal.BuildVRT(vrt_path, inputs, options=opts)
    if vrt is None:
        raise RuntimeError(f"gdal.BuildVRT failed for inputs={inputs}")
    vrt.FlushCache()
    vrt = None
    return vrt_path


class DemSampler:
    def __init__(self, path: str, nodata: float | None = None):
        src = gdal.Open(path, gdal.GA_ReadOnly)
        if src is None:
            raise FileNotFoundError(path)

        srs = osr.SpatialReference()
        wkt = src.GetProjection()
        if wkt:
            srs.ImportFromWkt(wkt)
        else:
            srs.ImportFromEPSG(4326)
        target = osr.SpatialReference()
        target.ImportFromEPSG(4326)

        if not srs.IsSame(target):
            src = gdal.AutoCreateWarpedVRT(src, srs.ExportToWkt(), target.ExportToWkt(), gdal.GRA_Bilinear)

        self.ds = src
        gt = src.GetGeoTransform()
        self.gt = gt
        self.cols = src.RasterXSize
        self.rows = src.RasterYSize

        band = src.GetRasterBand(1)
        if nodata is None:
            nodata = band.GetNoDataValue()
        self.nodata = nodata
        self.band = band
        self.pixel_size_deg = abs(gt[1])
        x0, y0 = gdal.ApplyGeoTransform(gt, 0, 0)
        x1, y1 = gdal.ApplyGeoTransform(gt, self.cols, self.rows)
        self.bounds = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def sample(self, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
        gt = self.gt
        det = gt[1] * gt[5] - gt[2] * gt[4]
        px = (gt[5] * (lons - gt[0]) - gt[2] * (lats - gt[3])) / det
        py = (-gt[4] * (lons - gt[0]) + gt[1] * (lats - gt[3])) / det

        x0 = int(math.floor(np.nanmin(px))) - 1
        y0 = int(math.floor(np.nanmin(py))) - 1
        x1 = int(math.ceil(np.nanmax(px))) + 1
        y1 = int(math.ceil(np.nanmax(py))) + 1
        x0c = max(0, x0)
        y0c = max(0, y0)
        x1c = min(self.cols, x1)
        y1c = min(self.rows, y1)
        if x1c <= x0c or y1c <= y0c:
            return np.zeros_like(lons, dtype=np.float32)

        # Let GDAL downsample oversized windows instead of reading them at
        # native resolution: low-zoom tiles over a large DEM would otherwise
        # allocate the full source window per worker process (OOM). Sampling
        # density never needs more than the output grid plus a small margin.
        win_w = x1c - x0c
        win_h = y1c - y0c
        buf_w = min(win_w, lons.shape[1] + 2)
        buf_h = min(win_h, lons.shape[0] + 2)
        if buf_w < win_w or buf_h < win_h:
            # GDAL resampling is center-based: buffer pixel j represents the
            # source around x0c + (j+0.5)*scale - 0.5. Expand the read window by
            # half a buffer pixel (+1 to keep the original 1px margin) so edge
            # samples still fall inside the represented range instead of being
            # clamped to the outermost buffer pixel.
            pad_x = int(math.ceil(win_w / buf_w / 2.0)) + 1
            pad_y = int(math.ceil(win_h / buf_h / 2.0)) + 1
            x0c = max(0, x0c - pad_x)
            y0c = max(0, y0c - pad_y)
            x1c = min(self.cols, x1c + pad_x)
            y1c = min(self.rows, y1c + pad_y)
            win_w = x1c - x0c
            win_h = y1c - y0c
        arr = self.band.ReadAsArray(
            x0c, y0c, win_w, win_h,
            buf_xsize=buf_w, buf_ysize=buf_h,
            resample_alg=gdal.GRA_Bilinear,
        ).astype(np.float32)
        if self.nodata is not None:
            arr = np.where(arr == self.nodata, np.nan, arr)

        # Map source pixel coords into the resampled buffer. GDAL's resampling
        # is center-based: buffer pixel j represents source position
        # (j+0.5)*scale - 0.5 (verified against gdal 3.x RasterIO — its
        # "bilinear" downsample is a tent filter centred exactly there), so the
        # inverse mapping is p/sx - 0.5*(1 - 1/sx). Do NOT "simplify" this to
        # p/sx - 0.5: that introduces a +0.5 source-pixel shift (measured on a
        # synthetic linear-field DEM; pinned by test_fix_terrain_sampler_resample).
        # The correction vanishes when buf == win.
        sx = win_w / buf_w
        sy = win_h / buf_h
        lpx = (px - x0c) / sx - 0.5 * (1.0 - 1.0 / sx)
        lpy = (py - y0c) / sy - 0.5 * (1.0 - 1.0 / sy)
        ix = np.clip(np.floor(lpx).astype(int), 0, arr.shape[1] - 2)
        iy = np.clip(np.floor(lpy).astype(int), 0, arr.shape[0] - 2)
        fx = np.clip(lpx - ix, 0, 1)
        fy = np.clip(lpy - iy, 0, 1)
        v00 = arr[iy, ix]
        v10 = arr[iy, ix + 1]
        v01 = arr[iy + 1, ix]
        v11 = arr[iy + 1, ix + 1]
        v0 = v00 * (1 - fx) + v10 * fx
        v1 = v01 * (1 - fx) + v11 * fx
        v = v0 * (1 - fy) + v1 * fy
        v = np.where(np.isnan(v), 0.0, v).astype(np.float32)
        return v


def _zigzag(v: np.ndarray) -> np.ndarray:
    return ((v << 1) ^ (v >> 31)).astype(np.uint32)


def _high_water_mark_encode(indices: np.ndarray) -> np.ndarray:
    out = np.empty_like(indices, dtype=np.uint32)
    highest = 0
    for i, code in enumerate(indices):
        out[i] = highest - code
        if code == highest:
            highest += 1
    return out


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


def _zz_delta(a: np.ndarray) -> np.ndarray:
    """zigzag-delta 编码:行优先展平 -> 相邻差分 -> zigzag。原先是
    encode_quantized_mesh 里的闭包,提到模块级是为了让 _mesh_constants 复用
    同一份实现(字节流必须与原逻辑逐字节等价)。"""
    flat = a.reshape(-1).astype(np.int32)
    d = np.empty_like(flat)
    d[0] = flat[0]
    d[1:] = flat[1:] - flat[:-1]
    return _zigzag(d).astype(np.uint16)


# encode_quantized_mesh 里有一大块只依赖网格尺寸 n(=tile_size)的常量:三角形索引、
# 高水位编码、四条边索引、u/v 的 zigzag-delta。此前每瓦片重算一遍(n=17 时三角形
# 索引是双重 for 拼 24576 元素 list + 逐元素 Python 循环高水位编码),纯属浪费。
# worker 是独立进程,进程内按 n 缓存一次即可;每瓦片只剩高度相关的 hzz 要现算。
# 缓存用的就是原来的计算代码,保证 quantized-mesh 字节流逐字节等价
# (test_fix_terrain_mesh_indices 按 spec 逐字段解析字节流)。
@functools.lru_cache(maxsize=None)
def _mesh_constants(n: int):
    u = np.linspace(0, 32767, n, dtype=np.uint16)
    v = np.linspace(0, 32767, n, dtype=np.uint16)
    uu, vv = np.meshgrid(u, v)
    uzz = _zz_delta(uu)
    vzz = _zz_delta(vv)

    # indices: 2 triangles per cell
    idx = []
    for j in range(n - 1):
        for i in range(n - 1):
            a0 = j * n + i
            a1 = j * n + (i + 1)
            a2 = (j + 1) * n + i
            a3 = (j + 1) * n + (i + 1)
            idx.extend([a0, a1, a2])
            idx.extend([a1, a3, a2])
    indices = np.array(idx, dtype=np.uint32)
    encoded_indices = _high_water_mark_encode(indices)

    # edge indices
    ar = np.arange(n, dtype=np.uint32)
    west_idx = ar * n
    south_idx = ar
    east_idx = ar * n + (n - 1)
    north_idx = (n - 1) * n + ar
    return uzz, vzz, encoded_indices, (west_idx, south_idx, east_idx, north_idx)


def encode_quantized_mesh(west: float, south: float, east: float, north: float,
                          heights_grid: np.ndarray, mesh=None, normals=None) -> bytes:
    """编码一张 quantized-mesh 瓦片。

    mesh=None 时用完整规则网格（索引表走 _mesh_constants 的进程级缓存）。
    传入 mesh=(vertex_grid_indices, triangles) 时按该三角网编码 —— 顶点是
    格点的子集，triangles 是局部索引，rtin_extract 的输出直接可用。

    normals 是**满网格**的逐顶点法线 (n,n,3)（或已展平的 (n*n,3)）。传 mesh 时
    自动按 mesh[0] 取子集：数量跟随简化后的顶点，值仍来自满网格计算 ——
    粗几何 + 精细法线，类似 normal mapping。设计稿:209 定的就是这个，
    K 质量验证阶段的着色图给出了实测支撑：几何面晕渲从 K=0.15 起就有明显三角
    刻面，而逐顶点法线着色平滑无刻面。
    """
    n = heights_grid.shape[0]
    assert heights_grid.shape == (n, n)
    h_min = float(np.min(heights_grid))
    h_max = float(np.max(heights_grid))
    if h_max == h_min:
        h_max = h_min + 1.0

    lon_c = 0.5 * (west + east)
    lat_c = 0.5 * (south + north)
    h_c = 0.5 * (h_min + h_max)
    cx, cy, cz = lonlat_to_ecef(np.array([lon_c]), np.array([lat_c]), np.array([h_c]))[0]

    # 外接球半径：直接对【这张瓦片自己的 n×n 顶点网格】取最远点。
    #
    # 为什么不再用手挑的候选表。它先后被证伪两次：
    #   1. 最早只取 4 个 h_min 角点 + 中心点，漏了 h_max 那 4 个角点（同样的
    #      经纬跨度在更高高度上对应更大的物理距离）—— 实测缺 0.35 m（1° 瓦片）
    #      到 55 m（10° 瓦片）。
    #   2. 补齐 8 角点后 z0 仍然缺 15.1 km：z0 跨 lat -90..90，四个经纬角点
    #      全部退化到南北两极这两个物理点上，最远点跑到了西/东边界的中点
    #      （赤道上）。于是又追加了 4 个 h_max 边中点。
    # 第二次修补在当前切片方案下缺口确实归零，但**裕度也精确是零**：沿一条
    # 经线（经度固定、纬度变化）的最远点只有在经度跨度 ≳ 179.233° 时才会离开
    # 端点、跑到边的内部去，位置约 lat* ≈ -148.4·lat_c。z0 的经度跨度精确是
    # 180°，正落在这个退化区间里；它没出事的唯一原因是 z0 的 lat_c 恰好为 0
    # 于是 lat* = 0，那个内部极大点正好就是西边中点、被候选表撞上了。把 z0 的
    # 南边界挪 0.1° 就缺 255 m，挪 0.4° 缺 3528 m。**任何人改动切片方案的纬度
    # 分割都会把这个洞重新打开**，而这个位置已经栽过两次了。
    #
    # 顶点网格没有这个问题：它就是这张瓦片真正编码出去的那些点，不依赖任何
    # 「极值在哪」的推断。所有三角形都落在顶点的凸包里、球是凸集，所以包住全部
    # 顶点就等于包住全部几何；自适应网格的顶点是格点的子集，同样被覆盖。
    # 代价是 tile_size=65 时 4225 个点走一次向量化的 lonlat_to_ecef。
    #
    # 包不住的后果：Cesium 视锥剔除可能把边角恰在视锥边缘的瓦片误剔 ——
    # HTTP 200、任务 completed、前端不报错，瓦片就是不显示。
    # 用广播而不是 meshgrid：lon 只有 n 个不同值、lat 也只有 n 个，喂
    # (1,n) 与 (n,1) 进去，lonlat_to_ecef 里的 radians/sin/cos/sqrt 就只算 n 次而
    # 不是 n² 次，结果因广播仍是 (n,n,3) 且与 meshgrid 版逐位相同
    # （实测 n∈{65,257} × 3 组 bbox 全部 0 差异）。这不是提前优化 —— 实测
    # （tile_size=65）：候选表 encode 0.091 ms/次，meshgrid 版 0.376，广播版 0.214；
    # 整条切片链路（天山 z6-11 共 224 张、auto、交错 best-of-4）
    # 1.47~1.51 s -> meshgrid 版 +11%、广播版 +5.8%（1.55~1.59 s）。
    # heights_grid 的行是纬度（南->北）、列是经度（西->东），与 _worker_tile 里
    # meshgrid(lons, lats) 的形状一致。
    grid_xyz = lonlat_to_ecef(
        np.linspace(west, east, n, dtype=np.float64)[None, :],
        np.linspace(south, north, n, dtype=np.float64)[:, None],
        heights_grid,
    )
    radius = float(np.linalg.norm(grid_xyz - np.array([cx, cy, cz]), axis=-1).max())
    # 读端拿到的顶点是【量化过】的，与上面这些精确格点差一点点：高程差最多一个
    # 量化步 (h_max-h_min)/32767，经纬各差最多一个 u/v 步长。距离函数是
    # 1-Lipschitz 的，位置误差的上界可以原样加进半径 —— 于是「包住」这件事不再
    # 依赖任何实测，而是可证的。这一项在 1° 瓦片上约 7 m（相对 1e-4）、z0 上约
    # 1.2 km（相对 1.4e-4），比要防的 15 km 缺口小四个数量级；而方向上宁大勿小：
    # 半径偏大只是少剔掉一点瓦片，偏小就是地形直接看不见。
    radius += (h_max - h_min) / 32767.0 + (WGS84_A + abs(h_max)) * math.radians(
        abs(east - west) + abs(north - south)
    ) / 32767.0

    a = WGS84_A
    b = WGS84_B
    if a > 0 and b > 0:
        sx_e, sy_e, sz_e = cx / a, cy / a, cz / b
        s_mag = math.sqrt(sx_e * sx_e + sy_e * sy_e + sz_e * sz_e) or 1.0
        nx_e, ny_e, nz_e = sx_e / s_mag, sy_e / s_mag, sz_e / s_mag
        scale = max(s_mag, 1.0) + (h_max + radius) / a
        hox = nx_e * scale * a
        hoy = ny_e * scale * a
        hoz = nz_e * scale * b
    else:
        hox = hoy = hoz = 0.0

    header = (
        struct.pack("<ddd", float(cx), float(cy), float(cz))
        + struct.pack("<ff", h_min, h_max)
        + struct.pack("<ddd", float(cx), float(cy), float(cz))
        + struct.pack("<d", float(radius))
        + struct.pack("<ddd", float(hox), float(hoy), float(hoz))
    )

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
        # 四条边索引：保留顶点中落在各边上的。
        #
        # 【槽位】是硬约束：Cesium 按 west/south/east/north 的槽位决定裙边（skirt）
        # 往哪个方向位移（createVerticesFromQuantizedTerrainMesh.js：west 经度 -δ、
        # east +δ、south 纬度 -δ、north +δ），串了就错。
        #
        # 【顺序】不是。quantized-mesh-1.0 spec 对边内顺序只字未提，且 Cesium 1.143.0
        # 拿到这四个数组后会无条件复制并各自重排（west 按 v 升序、east 按 v 降序、
        # south 按 u 降序、north 按 u 升序 —— 见该文件返回的 westIndicesSouthToNorth /
        # eastIndicesNorthToSouth / southIndicesEastToWest / northIndicesWestToEast），
        # 生产端给什么顺序都会被覆盖。这里按格点坐标升序只是为了输出确定性
        # （同输入同字节，便于 golden 比对），不是在满足 spec 要求。
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

    # spec: "To enforce proper byte alignment, padding is added before the IndexData
    # to ensure 2 byte alignment for IndexData16 and 4 byte alignment for IndexData32."
    #
    # 到这里的绝对偏移 = 88(header) + 4(vertexCount) + 6*vertexCount（u/v/h 三段
    # uint16）。88 和 4 都是 4 的倍数，所以偏移 ≡ 2*vertexCount (mod 4) ——
    # uint16 分支永远已经对齐（补 0 字节，规则网格的 golden 指纹不受影响），
    # 而 tile_size 恒为 2^k+1 => vertexCount=(2^k+1)^2 恒为奇数 =>
    # **uint32 分支必然要补 2 字节**。
    #
    # 漏掉这 2 字节的后果是整段 IndexData 错位：实测 tile_size=257 编码端写
    # triangleCount=131072，Cesium 的 parseQuantizedMesh 按 spec 补齐后读到 **2**，
    # 整块地形塌成两个三角形，且 HTTP 全 200、任务 completed、前端不报错 ——
    # 与已修掉的 triangleCount bug 同一种失效形态。
    # 生产 tile_size 硬编码 65（uint16）故此前休眠；触发入口是 CLI --tile-size。
    # len(header) 而不是字面量 88：对齐是相对【整个瓦片】的起点算的，header 若变长
    # （Task 6/7 要加 extension 段是加在末尾，但难保没人动前面）这里要跟着变。
    body.write(b"\x00" * ((-(len(header) + body.tell())) % np.dtype(index_dtype).itemsize))

    # IndexData.triangleCount 是【三角形数】，不是索引元素数 —— 读端按
    # triangleCount*3 取索引。此前这里写的是 len(encoded_indices)（即元素数），
    # Cesium 于是去读 3 倍的索引而越界，抛 RangeError: Invalid typed array length
    # （tile_size=65 时 24576*3=73728），所有瓦片解码失败、地形一片都渲染不出来，
    # 且全程 HTTP 200、任务标 completed、前端不报错。
    # 下面 EdgeIndices 的 len(edge) 是对的：那几个字段 spec 定义就是顶点数。
    body.write(struct.pack("<I", len(encoded_indices) // 3))
    body.write(pack_indices(encoded_indices))

    for edge in edge_indices:
        body.write(struct.pack("<I", len(edge)))
        body.write(pack_indices(edge))

    # Oct-Encoded Per-Vertex Normals 扩展段（spec 的 ExtensionHeader:
    # unsigned char extensionId; unsigned int extensionLength;）。
    #
    # 追加在 EdgeIndices 之后、整条流的末尾，前面一个字节都不动 —— 于是
    # normals=None 时字节流与本函数长期以来的输出逐字节相同（golden 指纹不受
    # 影响）。扩展段不需要对齐 padding：Cesium 读它是 `new Uint8Array(buffer,
    # offset, vertexCount*2)`，Uint8Array 没有对齐要求（IndexData 那里要补是因为
    # Uint16Array/Uint32Array 有）。
    #
    # extensionLength 用小端：Cesium 读它是 `S.getUint32(a,
    # littleEndianExtensionSize)`，而 littleEndianExtensionSize 只有在 layer.json
    # 声明成老的 "vertexnormals" 时才是 false；我们声明的是 "octvertexnormals"，
    # 走小端。
    #
    # ⚠️ 长度校验不是防御性冗余：Cesium 取法线用的是 **vertexCount*2**，
    # 根本不看 extensionLength。多写了它静默只用前一截，少写了直接 RangeError
    # 整张瓦片解码失败 —— 前者正是本项目栽过五次的"什么都不报就是不对"。
    # 在编码端一次性挡掉，比让读端去猜强。
    if normals is not None:
        nrm = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
        if mesh is not None:
            nrm = nrm[mesh[0]]
        if len(nrm) != vertex_count:
            raise ValueError(
                f"normals count {len(nrm)} != vertex count {vertex_count}")
        oct_bytes = _oct_encode(nrm).tobytes()
        body.write(struct.pack("<BI", 1, len(oct_bytes)))
        body.write(oct_bytes)

    payload = header + body.getvalue()
    return payload


_WORKER_SAMPLER: DemSampler | None = None
_WORKER_NODATA: float | None = None
# worker 进程内已创建过的 {z}/{x} 目录集合:每瓦片都 mkdir(parents=True,
# exist_ok=True) 是一次多余 syscall,去重后每目录只建一次。去重只在单次切片
# 任务内有效 —— _worker_init(并行:每 worker 进程一次;串行:_run_serial 调)
# 会清空,避免输出目录被删后同进程重跑时跳过 mkdir 导致写瓦片失败。
_WORKER_MKDIRS: set = set()


def _worker_init(input_path: str, nodata: float | None) -> None:
    global _WORKER_SAMPLER, _WORKER_NODATA
    _WORKER_SAMPLER = DemSampler(input_path, nodata=nodata)
    _WORKER_NODATA = nodata
    _WORKER_MKDIRS.clear()


def _max_error_for_level(z: int, tile_size: int, k: float) -> float:
    """该层级的 max_error（米）。

    顶点间距 = 瓦片纬向跨度 / (tile_size-1)，换算成米后乘 K。固定绝对值不可用 ——
    实测 max_error=20m 时 z14 瓦片只剩 2 个三角形，整块压成平面。
    （地理切片方案下瓦片的经向跨度 360/2^(z+1) 与纬向跨度 180/2^z 恒等，
    所以 span_deg 对两个方向都成立。）

    DEG_TO_M 只是**量级换算**，不要理解成「坡度误差容限恒定」：它取的是赤道处
    1° 经度的度长（111320 m）。实测（tile_size=65）南北向由此得到的米数精确到
    0.2~0.7%，但东西向的真实顶点间距按 cos(纬度) 收缩，于是等效 K 按 1/cos(纬度)
    放大 —— lat41 → 0.199、lat52 → 0.243、lat70 → 0.437、lat80 → 0.862，
    约 62°N 以上东西向就越过了设计稿给 K 定的 0.3 上限。

    公式保持原样，因为影响很小且方向是「高纬更宽松」而非更严：同一份物理地形
    投到不同纬度，减面率 80.1%（赤道）→ 83.7%（80°），只差 3.6pp（边界满密度的
    地板占大头），本项目实际数据在 N28~N52，差 1~2pp。更重要的是**绝对高程
    容限与纬度无关**（z14 恒为 2.87 m），这条才是质量上的硬保证。
    """
    span_deg = 180.0 / (1 << z)
    spacing_m = span_deg / max(1, tile_size - 1) * DEG_TO_M
    return k * spacing_m


# compresslevel=6:quantized-mesh 是高熵二进制,9 级比 6 级压缩率提升可忽略但
# CPU 开销明显更大,瓦片量大时值得。实测把两个后端都升到 9 对择优比值最多动
# 0.008,救不了场,所以维持 6。
#
# mtime=0 是【必须】的,不是洁癖:gzip 头带 4 字节时间戳,gzip.open 写的是
# time.time(),于是同一份数据两次切片的磁盘字节不同。实测后果 —— 一条比较磁盘
# 字节的接线测试在干净树上连跑 8 次挂 1 次,差异恰好落在头部第 4 字节。逐瓦片
# 择优要拿两次压缩结果比大小、再把胜出的那份原样落盘,更是要求确定性。
# (顺带:gzip.compress 不写 FNAME 字段,而 gzip.open 会把文件名写进 gzip 头。
#  省下的是 len("{y}.terrain")+1 字节,随 y 的位数变 —— 实测 9.terrain 省 10、
#  12.terrain 省 11、373.terrain 省 12。这里以前写死"少 11 字节",不准。)
_GZIP_LEVEL = 6


def _gzip_tile(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=_GZIP_LEVEL, mtime=0)


def _choose_tile_bytes(mart_gz: bytes, grid_gz: bytes) -> Tuple[bytes, str]:
    """逐瓦片择优:取 **gzip 后**更小的那份,平局取 martini。

    为什么比的是 gzip 后:瓦片 gzip 落盘(见 _worker_tile)、terrain_static.py
    检测 gzip magic 后带 Content-Encoding: gzip 原样发出,所以 gzip 后的字节
    既是磁盘占用也是传输量,未压缩字节在生产里不代表任何东西。

    为什么需要择优(实测 112584 张配对瓦片,10 个真实 DEM):gzip 后规则网格
    ≈0.91 字节/三角形、martini ≈4.04 字节/三角形(贵 4.43 倍)—— 规则网格的
    u/v 是等差 zigzag-delta、索引是周期性 HWM 流,gzip 几乎白送;martini 打散后
    两者都成了高熵数据。于是**减面必须 >77.4% 才在字节上打平**,而山地只做到
    74.8%(gzip 后 +17.6% 净亏)、丘陵 75.2%(+9.8%),只有平缓地形 88.0%(-54.2%)
    是赚的。择优把变大的部分全部避免 => 全局净省 27.6%,且每张瓦片字节严格
    ≤ min(两者),**不可能变差**。调大 K 也能让 gzip 转正,但 K=0.30 实测会把
    整条沟谷拉成直线(结构性丢失),所以 K 保持 0.15 —— 质量完全不让步。

    平局取 martini:顶点更少,Cesium 侧内存与 GPU 上传更省。

    ⚠️ **判据是纯字节数,没有任何质量下限** —— "取更小的那个"读起来无条件安全,
    其实结构上偏向退化网格:一张塌掉的瓦片正因为塌掉了才字节最少,于是会被主动
    选中。实测在高程里注入一个 NaN,误差表被污染、martini 塌成 2 个三角形
    (gzip 后 65 字节) 对 grid 的 243 字节,auto **会选那张塌掉的**。
    这条路上唯一的闸门是 DemSampler.sample 的 NaN 兜底(它兜的是最终插值结果,
    不只是 nodata 替换,所以任何来源的 NaN 都拦得住)。谁要动那个兜底,
    先回来看这一段。
    """
    if len(mart_gz) <= len(grid_gz):
        return mart_gz, "martini"
    return grid_gz, "grid"


def _tile_normals(sampler: "DemSampler", west: float, south: float, east: float,
                  north: float, tile_size: int) -> np.ndarray:
    """一张瓦片的逐顶点法线 (tile_size, tile_size, 3)，用 ghost 环消跨瓦片接缝。

    ghost 环 = 向外各扩一个顶点步长采 (tile_size+2)²，算完法线裁回中间那圈。
    不扩环时边界顶点只能拿到本瓦片内的三角形，法线天生不完整，而且接缝**沿瓦片
    边界排成直线**（马赫带把它强化成可见网格，比同幅度的随机噪声显眼得多）。

    **归零是有条件的，条件在采样器上**。两侧邻接三角形集合一致 ⇒ 逐位相同，
    这一步只在 sampler 是「位置的纯函数」时成立。生产的 DemSampler.sample
    **不是**：读窗口由传入网格的 bbox 决定、再降采样到 buf = grid+2，相邻瓦片
    窗口不同 ⇒ 同一经纬点取到不同高程（实测同一批 65² 点，用 65² 窗口取 vs 用
    67² 窗口取再裁回，最大差 1.96 m）。于是：
      - 网格比源像素密、不发生降采样的层级 —— 天山 1″ DEM + tile_size=65 即
        z14（estimate_max_level 也正是 14）—— 实测 8/8 相邻瓦片边**逐位相同**；
      - 中间层只是大幅压小、不归零：z11 实测 199 对相邻瓦片边，边界法线夹角
        中位 2.42°→**0.238°**、最大 42.4°→3.27°；不扩环时 45.4% 的边界顶点
        Lambert 亮度阶跃超过 2% 可察觉阈值（86.4% 的瓦片对至少有一个顶点超）。
    ⚠️ 中间层的残差**不是法线的锅**：同一现象让瓦片自身的 65² 高程网格在公共边
    上就对不齐（实测 z9 差 35 m、z10–13 差 3~6 m、z14 差 0），几何裂缝先于法线
    接缝存在。属既有缺陷，不在本轮范围，但别把它误算成 ghost 环没生效。

    从 _worker_tile 里抽出来是为了可测：Task 6 阶段法线还没有消费者，内联时
    「ghost 不扩」「裁切下标写错」两种变异在任何测试下都是静默存活的（实测）。

    ⚠️ 已知限制：ghost 环落在 DEM 外时 DemSampler.sample 兜成 0，整个 DEM 最外
    一圈瓦片的最外一行顶点法线会朝向偏差（实测跨界瓦片最西列法线与地心方向
    夹角中位 3.70°）。这一条**确实不影响接缝** —— 相邻瓦片在同一地理位置取到
    同一个兜底 0：在采样器为纯函数的 z14 上实测天山 DEM 西边界 121 组边界瓦片
    对，0 组有差异。修它要让边缘顶点退化成只用内侧三角形，本轮不做。

    为什么不复用这里的采样结果去掉 _worker_tile 的那次满网格采样：GDAL 在低
    zoom 按读窗口降采样，67² 窗口与 65² 窗口取到的高程不同，复用会让瓦片几何
    随 normals 开关变化 —— 法线就从「附加数据」变成「改动既有产物」。
    """
    dw = (east - west) / (tile_size - 1)
    dh = (north - south) / (tile_size - 1)
    glon = np.linspace(west - dw, east + dw, tile_size + 2, dtype=np.float64)
    glat = np.linspace(south - dh, north + dh, tile_size + 2, dtype=np.float64)
    gl, ga = np.meshgrid(glon, glat)
    gh = sampler.sample(gl, ga)
    # 法线只喂广播轴（trig 算 n 次而非 n²，与 meshgrid 版逐位相同，见
    # _vertex_normals_ecef 的 docstring）；meshgrid 仍要给 sampler.sample 用。
    # 实测 n=67：_vertex_normals_ecef 0.580 ms → 0.508 ms（省 12.5%）。
    return _vertex_normals_ecef(glon[None, :], glat[:, None], gh)[1:-1, 1:-1]


def _worker_tile(task) -> Tuple[float, float, str] | None:
    """Returns (min, max, backend) of the written tile, or None on failure.

    backend ∈ {'martini','grid'} 是这张瓦片**实际落盘**的那个后端,由 build_terrain
    汇总成 chose_martini / chose_grid 计数 —— 排障时它直接告诉运维「这批 DEM 是
    什么地形」(全 grid = 山地,全 martini = 平缓)。

    逐瓦片容错:单个坏块(如 ReadAsArray 返回 None、磁盘写失败)只记日志计失败,
    不让一个瓦片炸掉整个任务 —— 与 contour 的 _render_contour_tile_core 同款。
    """
    (z, x, y, west, south, east, north, tile_size, out_dir,
     triangulator, max_error_k, with_normals) = task
    assert _WORKER_SAMPLER is not None

    try:
        scheme = GeographicTilingScheme(tile_size=tile_size)
        lons = np.linspace(west, east, tile_size, dtype=np.float64)
        lats = np.linspace(south, north, tile_size, dtype=np.float64)
        llon, llat = np.meshgrid(lons, lats)
        heights = _WORKER_SAMPLER.sample(llon, llat)

        # 法线基于**满网格**算、两个三角化后端共用同一份：简化只动几何，光照
        # 细节保留（粗几何 + 精细法线，类似 normal mapping）。encode 那边传了
        # mesh 时会自己按保留顶点取子集。
        normals = _tile_normals(_WORKER_SAMPLER, west, south, east, north,
                                tile_size) if with_normals else None

        if triangulator in ("auto", "martini"):
            # 自适应三角化：按高程误差驱动细分，标准档（K=0.15）减面 73%~83%。
            # pin_border=True 是跨瓦片无缝的唯一保证，不要关。改成 False 实测：
            # 三角形塌 98.7%、公共边顶点数从满密度掉到 2~5、相邻瓦片出现真裂缝。
            # 守它的是 test_rtin.py 的接线测试（断言落盘瓦片四条边各 tile_size
            # 个顶点）—— 这行以前是零守卫的，关掉全量测试一条不红。
            # 惰性 import：rtin 只依赖 numpy，但放在这里可以让「切 grid 回退」
            # 在 rtin 出问题时是真的完全绕开它。
            from src.services.terrain_tiling.rtin import rtin_errors, rtin_extract
            err = rtin_errors(heights.reshape(-1), tile_size, pin_border=True)
            mesh = rtin_extract(err, tile_size, _max_error_for_level(z, tile_size, max_error_k))
        else:
            mesh = None

        if triangulator == "auto":
            # 两个后端各编一遍、各压一遍，取小者。多出来的成本是第二次编码 +
            # 第二次 gzip（rtin_errors 本身已向量化，不是大头）。
            # **两条分支都要传 normals**：只传一条的话，另一条胜出的瓦片就没有
            # 法线段，而 layer.json 照样声明 octvertexnormals、HTTP 照样 200、
            # Cesium 照样不报错 —— 只是那些瓦片不受光照。
            mart_gz = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                       heights, mesh=mesh,
                                                       normals=normals))
            grid_gz = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                       heights, mesh=None,
                                                       normals=normals))
            blob, backend = _choose_tile_bytes(mart_gz, grid_gz)
        else:
            blob = _gzip_tile(encode_quantized_mesh(west, south, east, north,
                                                    heights, mesh=mesh,
                                                    normals=normals))
            backend = triangulator

        out_path = Path(out_dir) / str(z) / str(x)
        if out_path not in _WORKER_MKDIRS:
            out_path.mkdir(parents=True, exist_ok=True)
            _WORKER_MKDIRS.add(out_path)
        (out_path / f"{y}.terrain").write_bytes(blob)
        return float(np.min(heights)), float(np.max(heights)), backend
    except Exception as e:
        logger.warning(f"terrain 瓦片生成失败 z={z} x={x} y={y}: {e!r}")
        return None


def build_terrain(
    inputs: list[str],
    output_dir: str,
    *,
    min_level: int = 0,
    max_level: int | None = None,
    tile_size: int = 17,
    nodata: float | None = None,
    workers: int = 0,
    progress_cb=None,
    stop_flag=None,
    triangulator: str = "auto",
    max_error_k: float = DEFAULT_MAX_ERROR_K,
    normals: bool = True,
) -> dict:
    # progress_cb(done, total): 逐瓦片进度回调（done = rendered+failed，terrain
    # 没有 contour 的 skipped 态），串行/并行每 tally 一次调一次；调用方自行
    # 节流（范本：contour_engine.build_contour_tiles）。stop_flag: threading.Event
    # 式协作停止 —— 串行每瓦片检查、并行批间检查；置位后提前收尾（已生成的
    # 瓦片保留，layer.json/meta.json 照常按已处理部分写出）。
    #
    # triangulator: 'auto' = 逐瓦片择优（默认，两个后端都编一遍取 gzip 后更小的，
    # 见 _choose_tile_bytes 里的实测依据），'martini' / 'grid' = 强制单一后端，
    # 排障时用。max_error_k: 允许高程误差 = K * 顶点间距（见 _max_error_for_level）。
    # 三者都不暴露给 UI/DB/API，只为排障与测试注入而存在。
    #
    # normals: 逐顶点法线（ECEF 空间 + ghost 环，见 _worker_tile / _vertex_normals_ecef），
    # 落盘成 oct-encoded 扩展段（每顶点 2 字节，见 encode_quantized_mesh 末尾），
    # 同时决定下面 layer.json 的 extensions 字段。默认开。
    #
    # 成本（天山 N42E086，z0-11 共 907 张，tile_size=65，workers=4，交错 best-of-4）：
    # 墙钟 0.932s → 1.266s，**+35.8%**。其中约 28pp 是采样+叉积（Task 6 在同一
    # 口径下测的 +27.7%，那时法线还没有消费者），其余是多编一段 + 多压一次。
    # 字节代价随后端差得很远（4 个真实 DEM、z8-12、3120 张配对瓦片，gzip 后总量）：
    # **grid +55.4%、martini 只 +22.5%** —— 法线字节正比于顶点数，而 grid 恒是
    # 4225 个顶点、martini 中位 589~1751 个。于是逐瓦片择优的平衡整体倒向 martini：
    #   天山 martini 325→**467** / 黄土 38→**187** / 华北 725→730 / 荷兰 621→639，
    #   3120 张里 314 张从 grid 翻成 martini，**反向翻转 0 张**。
    # 「auto 不可能比任一单后端差」不受影响（3120 张实测 0 处违反）。
    # ⚠️ 用 build_terrain 直接做实验时必须显式传 tile_size=65 —— 本函数默认值是
    # 17，生产走的是 TileParams.tile_size=65，两者的择优落点完全不同
    # （ts=17 时 martini 减面空间太小，实测张张选 grid）。
    #
    # 下面这两条校验在入口一次性做掉，不放进 _worker_tile —— 那里的 try/except
    # 是逐瓦片容错，配置错在那里只会变成每张瓦片一行 warning：
    #   - triangulator 拼错（'martni'）会静默走 else 分支退回规则网格，作业
    #     rendered==total 完美完成，没有任何信号说自适应根本没开（实测）。
    #   - tile_size 不是 2^k+1 时 rtin_tables 抛 ValueError，实测 10/10 瓦片
    #     全失败。生产路径恒为 65（TileParams.tile_size）不受影响，但 CLI 的
    #     --tile-size 收任意整数，且默认开自适应之后这是【新引入】的失败面 ——
    #     改成入口即报，错误直指病因而不是刷 N 行瓦片级 warning。
    #
    # 'auto' 在 tile_size 上与 'martini' 一样严，**不静默降级成纯 grid**：
    # 静默降级正是这个项目栽过三次的失败形态（作业 completed、前端不报错、
    # 什么都不显示）。生产默认 tile_size=65 是合法的，能触发这条就说明配置错了，
    # 应该当场暴露而不是悄悄退化成一个字节更大的产物。
    if triangulator not in ("auto", "martini", "grid"):
        raise ValueError(
            f"triangulator must be 'auto', 'martini' or 'grid', got {triangulator!r}")
    if triangulator in ("auto", "martini"):
        t = int(tile_size) - 1
        if t < 2 or (t & (t - 1)) != 0:
            raise ValueError(
                f"triangulator={triangulator!r} requires tile_size = 2^k+1 and >= 3, "
                f"got {tile_size} (use triangulator='grid' for arbitrary sizes)")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    input_path = build_input_vrt(inputs)
    # 多输入时 build_input_vrt 落在 tempfile.mkstemp 的临时 .vrt，此前从不删除，
    # 每次多输入切片泄漏一个临时文件（M18）。删除时机必须在进程池消费完之后
    # （worker 在 initializer 里各自打开这个文件），所以挂在整个切片过程的
    # finally 上，best-effort 删除。
    temp_vrt = input_path if input_path not in inputs else None
    try:
        sampler = DemSampler(input_path, nodata=nodata)
        scheme = GeographicTilingScheme(tile_size=tile_size)

        if max_level is None:
            max_level = scheme.estimate_max_level(sampler.pixel_size_deg)

        src_w, src_s, src_e, src_n = sampler.bounds

        def _tile_ranges():
            # compute intersected tile ranges per level
            for z in range(min_level, max_level + 1):
                nx, ny = scheme.tile_count(z)
                # project bbox to tile index range (clamp)
                ix0 = max(0, int(math.floor((src_w + 180.0) / 360.0 * nx)))
                ix1 = min(nx - 1, int(math.floor((src_e + 180.0) / 360.0 * nx)))
                iy0 = max(0, int(math.floor((src_s + 90.0) / 180.0 * ny)))
                iy1 = min(ny - 1, int(math.floor((src_n + 90.0) / 180.0 * ny)))

                if z <= 4:
                    # 低 zoom 仍然出图（根瓦片缺失会让 Cesium 的单层 provider
                    # 路径直接 404），但 available 只声明真正相交的那部分 ——
                    # 见下方 available_per_level 的注释。
                    x0, x1, y0, y1 = 0, nx - 1, 0, ny - 1
                else:
                    x0, x1, y0, y1 = ix0, ix1, iy0, iy1
                yield z, x0, x1, y0, y1, ix0, ix1, iy0, iy1

        available_per_level = []
        # Count tiles arithmetically (no list) — materializing every tile task tuple
        # OOMs at high max_level over large areas (same pitfall contour fixed).
        total = 0
        for z, x0, x1, y0, y1, ix0, ix1, iy0, iy1 in _tile_ranges():
            if x1 < x0 or y1 < y0:
                available_per_level.append([])
                continue
            # M12: available 只声明与 DEM **真正相交** 的瓦片范围（ix*），而不是
            # 实际出图的整个矩形（x*）。z<=4 时后者被强制成全球，于是每个任务都
            # 会声明「我有全球 z0-4 地形」—— 而那些瓦片的采样窗口落在 DEM 之外，
            # 返回全 0 被兜成 hmin=0/hmax=1 的平面，或者被边缘像素钳位拉成台地
            # （实测东半球 z0 瓦片 hmin=913 hmax=2502 m）。
            # Cesium 1.143 的 requestTileGeometry 取「第一个 availability 声明可用
            # 的层」，而父层是在子层【之后】才追加的 —— 于是 parentUrl 指向的
            # base_z8 的 z0-4 永远不会被请求，整个低层被这些假地形盖掉。
            # 只收窄声明、仍照常出图：base 不存在时单层 provider 不查 availability，
            # 根瓦片仍能拿到文件，不会退化成「地形完全不加载」。
            a0, a1, b0, b1 = max(x0, ix0), min(x1, ix1), max(y0, iy0), min(y1, iy1)
            if a1 < a0 or b1 < b0:
                available_per_level.append([])
            else:
                available_per_level.append(
                    [{"startX": a0, "startY": b0, "endX": a1, "endY": b1}])
            total += (x1 - x0 + 1) * (y1 - y0 + 1)

        def _iter_tasks():
            for z, x0, x1, y0, y1, _ix0, _ix1, _iy0, _iy1 in _tile_ranges():
                if x1 < x0 or y1 < y0:
                    continue
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        west, south, east, north = scheme.tile_extent_deg(z, x, y)
                        yield (z, x, y, west, south, east, north, tile_size, str(out),
                               triangulator, max_error_k, normals)

        sampler.ds = None
        sampler.band = None

        t0 = time.time()
        h_min_global = float("inf")
        h_max_global = float("-inf")
        done = 0
        failed = 0
        # 逐瓦片择优的落点统计。排障价值：全 grid = 这批 DEM 是山地/粗糙地形，
        # 全 martini = 平缓地形；两者混合才是 auto 在干活。强制单一后端时全部
        # 落在对应那一侧，可用来确认「切 grid 回退」真的切过去了。
        chose = {"martini": 0, "grid": 0}
        workers = int(workers or 0)
        if workers <= 0:
            # 保守默认:每个 worker 都要打开 GDAL dataset 并逐瓦片读 DEM 窗口,worker
            # 太多内存吃紧;无脑 cpu_count 在多核机器上可能把内存打爆、worker 被 OS
            # 杀掉(BrokenProcessPool)。封顶 4,与 contour 一致;显式传 workers 可覆盖。
            workers = min(4, os.cpu_count() or 1)

        def _emit() -> None:
            if progress_cb is not None:
                # done 按 processed(rendered+failed) 计:失败瓦片也是处理完的
                # 瓦片,不计的话进度条会停在 <100% 就结束(同 contour_engine._emit)。
                progress_cb(done + failed, total)

        def _tally(result) -> None:
            nonlocal h_min_global, h_max_global, done, failed
            if result is None:
                failed += 1
                _emit()
                return
            mn, mx, backend = result
            h_min_global = min(h_min_global, mn)
            h_max_global = max(h_max_global, mx)
            chose[backend] += 1
            done += 1
            _emit()

        def _run_serial() -> None:
            _worker_init(input_path, nodata)
            for task in _iter_tasks():
                # 串行:stop_flag 每瓦片即时检查(同 contour 的 _render_serial)。
                if stop_flag is not None and stop_flag.is_set():
                    break
                _tally(_worker_tile(task))

        if workers == 1 or total <= 4:
            _run_serial()
        else:
            # 并行:worker 在 initializer 里各自打开 dataset。瓦片按 batch 从生成器取:
            # Executor.map 会把传入 iterable 一次性全部 submit 成 Future,直接接生成器
            # 等于变相物化整张任务列表,高 max_level 大区域会 OOM(同 contour 的坑)。
            from concurrent.futures import ProcessPoolExecutor
            from concurrent.futures.process import BrokenProcessPool
            BATCH = 512  # 批内并行,批间检查 stop_flag;不物化整张瓦片列表。
            # 2048 -> 512(同 contour_engine):停止/暂停要等当前批跑完才生效,
            # 批小 4 倍响应约快 4 倍;单批 list 内存本身可忽略。
            try:
                with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(input_path, nodata)) as ex:
                    tasks = _iter_tasks()
                    while True:
                        if stop_flag is not None and stop_flag.is_set():
                            break
                        batch = list(itertools.islice(tasks, BATCH))
                        if not batch:
                            break
                        for result in ex.map(_worker_tile, batch, chunksize=8):
                            _tally(result)
            except BrokenProcessPool as e:
                # worker 进程被异常终止(多 worker 内存耗尽被 OS 杀常见)。进程级崩溃
                # except 兜不住,会让整个任务失败 -> 回退串行重跑保证切片完整:已生成
                # 的 .terrain 被覆盖,未生成的补齐。串行单进程内存压力小,通常能跑完。
                # 计数清零重跑后 progress_cb 会从 0 重新上报(同 contour 的回退),
                # 调用方的时间节流天然兼容,最终计数由收尾覆盖,不依赖逐次累计。
                logger.warning(f"并行切片 worker 崩溃({e!r}),回退串行重跑整个任务")
                h_min_global = float("inf")
                h_max_global = float("-inf")
                done = 0
                failed = 0
                chose["martini"] = 0
                chose["grid"] = 0
                _run_serial()

        # 收尾日志：择优落点是排障时最直接的一条信息 —— 全 grid 说明这批 DEM
        # 是山地/粗糙地形（martini 在 gzip 后反而更大），全 martini 说明是平缓
        # 地形。没有这行，chose_* 计数只有直接调 build_terrain 的人看得到。
        logger.info(
            f"terrain 切片完成 triangulator={triangulator} total={total} "
            f"rendered={done} failed={failed} chose_martini={chose['martini']} "
            f"chose_grid={chose['grid']} 用时 {time.time() - t0:.1f}s")

        layer_name = Path(inputs[0]).stem if len(inputs) == 1 else f"{Path(inputs[0]).stem}+{len(inputs)-1}"
        layer = {
            "tilejson": "1.0",
            "name": layer_name,
            "description": "",
            "version": "1.0.0",
            "format": "quantized-mesh-1.0",
            "attribution": "",
            "scheme": "tms",
            "tiles": ["{z}/{x}/{y}.terrain"],
            "projection": "EPSG:4326",
            "bounds": [-180, -90, 180, 90],
            "valid_bounds": list(sampler.bounds),
            "minzoom": min_level,
            "maxzoom": max_level,
            "available": available_per_level,
            # 不声明的话法线写了也是白写：Cesium 的 CesiumTerrainProvider 拿这个
            # 数组决定 layer.hasVertexNormals，而 requestTileGeometry 只有在
            # `_requestVertexNormals && hasVertexNormals` 时才把 extensions 放进
            # Accept 头、解析时也只有这时才去取 OCT_VERTEX_NORMALS 段。声明成老的
            # "vertexnormals" 会把 extensionLength 切成大端读，别写混。
            "extensions": ["octvertexnormals"] if normals else [],
        }
        (out / "layer.json").write_text(json.dumps(layer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        meta = {
            "minLevel": min_level,
            "maxLevel": max_level,
            "minHeight": h_min_global,
            "maxHeight": h_max_global,
            "bounds": list(sampler.bounds),
            "tileSize": tile_size,
            "scheme": "EPSG:4326",
        }
        (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # 计数结构对照 contour 的 build_contour_tiles(无 skipped:terrain 没有跳过态)。
        # chose_martini + chose_grid == rendered:每张成功瓦片恰好落在一个后端上。
        return {"total": total, "rendered": done, "failed": failed,
                "chose_martini": chose["martini"], "chose_grid": chose["grid"]}
    finally:
        if temp_vrt:
            # 串行路径的全局 worker sampler 可能还持有该 .vrt 的句柄（Windows 上
            # 打开中的文件删不掉），先释放再删；并行路径 pool 退出后 worker 已回收。
            if _WORKER_SAMPLER is not None:
                _WORKER_SAMPLER.ds = None
                _WORKER_SAMPLER.band = None
            try:
                os.remove(temp_vrt)
            except OSError as e:
                logger.warning(f"Failed to remove temp VRT {temp_vrt}: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DEM -> Cesium quantized-mesh-1.0 terrain tiles (cesiumlab compatible)")
    ap.add_argument("--input", "-i", required=True, action="append")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--min-level", type=int, default=0)
    ap.add_argument("--max-level", type=int, default=None)
    ap.add_argument("--tile-size", type=int, default=17)
    ap.add_argument("--nodata", type=float, default=None)
    ap.add_argument("--workers", "-j", type=int, default=0)
    # 排障开关：'auto' 逐瓦片择优（默认，与生产一致），'martini'/'grid' 强制
    # 单一后端做对比。auto/martini 要求 tile_size = 2^k+1（默认 17 满足），
    # 传 64 之类会在 build_terrain 入口报错并指向这个 flag。
    ap.add_argument("--triangulator", choices=("auto", "martini", "grid"), default="auto")
    ap.add_argument("--max-error-k", type=float, default=DEFAULT_MAX_ERROR_K)
    args = ap.parse_args(argv)

    import glob

    inputs: list[str] = []
    for spec in args.input:
        if os.path.isdir(spec):
            for ext in ("*.tif", "*.tiff", "*.img", "*.hgt", "*.vrt"):
                inputs.extend(sorted(glob.glob(os.path.join(spec, ext))))
        elif any(c in spec for c in "*?["):
            inputs.extend(sorted(glob.glob(spec)))
        else:
            inputs.append(spec)
    inputs = [p for p in inputs if os.path.isfile(p)]
    if not inputs:
        sys.exit("no input files matched")

    build_terrain(
        inputs,
        args.output,
        min_level=args.min_level,
        max_level=args.max_level,
        tile_size=args.tile_size,
        nodata=args.nodata,
        workers=args.workers,
        triangulator=args.triangulator,
        max_error_k=args.max_error_k,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
