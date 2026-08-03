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


def encode_quantized_mesh(west: float, south: float, east: float, north: float, heights_grid: np.ndarray) -> bytes:
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

    cor_lon = np.array([west, east, west, east, lon_c])
    cor_lat = np.array([south, south, north, north, lat_c])
    cor_h = np.array([h_min, h_min, h_min, h_min, h_max])
    cor_xyz = lonlat_to_ecef(cor_lon, cor_lat, cor_h)
    radius = float(np.max(np.linalg.norm(cor_xyz - np.array([cx, cy, cz]), axis=1)))

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

    # vertices: regular grid。u/v 与三角形索引、边索引只依赖 n,走进程级缓存;
    # 每瓦片只需现算高度相关的 hh/hzz。
    uzz, vzz, encoded_indices, edge_indices = _mesh_constants(n)
    hh = ((heights_grid - h_min) / (h_max - h_min) * 32767.0).astype(np.uint16)
    hzz = _zz_delta(hh)

    # quantized-mesh-1.0: index width is chosen by VERTEX COUNT (>65536 -> 32-bit),
    # not by the max encoded value. High-water-mark diffs wrap around, so the u16
    # branch must truncate them (astype(np.uint16)) rather than range-check them.
    vertex_count = n * n
    index_dtype = np.uint32 if vertex_count > 65536 else np.uint16

    def pack_indices(arr: np.ndarray) -> bytes:
        return arr.astype(index_dtype).tobytes()

    body = io.BytesIO()
    body.write(struct.pack("<I", vertex_count))
    body.write(uzz.tobytes())
    body.write(vzz.tobytes())
    body.write(hzz.tobytes())

    body.write(struct.pack("<I", len(encoded_indices)))
    body.write(pack_indices(encoded_indices))

    for edge in edge_indices:
        body.write(struct.pack("<I", len(edge)))
        body.write(pack_indices(edge))

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


def _worker_tile(task) -> Tuple[float, float] | None:
    """Returns (min, max) heights of the written tile, or None on failure.

    逐瓦片容错:单个坏块(如 ReadAsArray 返回 None、磁盘写失败)只记日志计失败,
    不让一个瓦片炸掉整个任务 —— 与 contour 的 _render_contour_tile_core 同款。
    """
    z, x, y, west, south, east, north, tile_size, out_dir = task
    assert _WORKER_SAMPLER is not None

    try:
        scheme = GeographicTilingScheme(tile_size=tile_size)
        lons = np.linspace(west, east, tile_size, dtype=np.float64)
        lats = np.linspace(south, north, tile_size, dtype=np.float64)
        llon, llat = np.meshgrid(lons, lats)
        heights = _WORKER_SAMPLER.sample(llon, llat)

        data = encode_quantized_mesh(west, south, east, north, heights)
        out_path = Path(out_dir) / str(z) / str(x)
        if out_path not in _WORKER_MKDIRS:
            out_path.mkdir(parents=True, exist_ok=True)
            _WORKER_MKDIRS.add(out_path)
        tile_file = out_path / f"{y}.terrain"
        # compresslevel=6:quantized-mesh 是高熵二进制,9 级比 6 级压缩率提升
        # 可忽略但 CPU 开销明显更大,瓦片量大时值得。
        with gzip.open(tile_file, "wb", compresslevel=6) as f:
            f.write(data)
        return float(np.min(heights)), float(np.max(heights))
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
) -> dict:
    # progress_cb(done, total): 逐瓦片进度回调（done = rendered+failed，terrain
    # 没有 contour 的 skipped 态），串行/并行每 tally 一次调一次；调用方自行
    # 节流（范本：contour_engine.build_contour_tiles）。stop_flag: threading.Event
    # 式协作停止 —— 串行每瓦片检查、并行批间检查；置位后提前收尾（已生成的
    # 瓦片保留，layer.json/meta.json 照常按已处理部分写出）。
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
                        yield (z, x, y, west, south, east, north, tile_size, str(out))

        sampler.ds = None
        sampler.band = None

        t0 = time.time()
        h_min_global = float("inf")
        h_max_global = float("-inf")
        done = 0
        failed = 0
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
            mn, mx = result
            h_min_global = min(h_min_global, mn)
            h_max_global = max(h_max_global, mx)
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
                _run_serial()

        _ = time.time() - t0

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
            "extensions": [],
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
        # 调用方(dem_task_tiler/local_terrain_task_manager)此前忽略返回值,保持兼容。
        return {"total": total, "rendered": done, "failed": failed}
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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
