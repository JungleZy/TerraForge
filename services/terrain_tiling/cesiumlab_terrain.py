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
import gzip
import io
import json
import math
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor
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
        deg_per_tile_pixel = 180.0 / 64.0
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
        # is center-based (buffer pixel j <- source (j+0.5)*scale-0.5, verified
        # against gdal 3.x RasterIO), so apply the half-pixel correction to keep
        # sample positions unbiased; it vanishes when buf == win.
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

    # vertices: regular grid
    u = np.linspace(0, 32767, n, dtype=np.uint16)
    v = np.linspace(0, 32767, n, dtype=np.uint16)
    uu, vv = np.meshgrid(u, v)
    hh = ((heights_grid - h_min) / (h_max - h_min) * 32767.0).astype(np.uint16)

    # zigzag-delta encode
    def zz_delta(a: np.ndarray) -> np.ndarray:
        flat = a.reshape(-1).astype(np.int32)
        d = np.empty_like(flat)
        d[0] = flat[0]
        d[1:] = flat[1:] - flat[:-1]
        return _zigzag(d).astype(np.uint16)

    uzz = zz_delta(uu)
    vzz = zz_delta(vv)
    hzz = zz_delta(hh)

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
    west_idx = np.array([j * n for j in range(n)], dtype=np.uint32)
    south_idx = np.array([i for i in range(n)], dtype=np.uint32)
    east_idx = np.array([j * n + (n - 1) for j in range(n)], dtype=np.uint32)
    north_idx = np.array([(n - 1) * n + i for i in range(n)], dtype=np.uint32)

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

    body.write(struct.pack("<I", len(indices)))
    body.write(pack_indices(encoded_indices))

    for edge in (west_idx, south_idx, east_idx, north_idx):
        body.write(struct.pack("<I", len(edge)))
        body.write(pack_indices(edge))

    payload = header + body.getvalue()
    return payload


_WORKER_SAMPLER: DemSampler | None = None
_WORKER_NODATA: float | None = None


def _worker_init(input_path: str, nodata: float | None) -> None:
    global _WORKER_SAMPLER, _WORKER_NODATA
    _WORKER_SAMPLER = DemSampler(input_path, nodata=nodata)
    _WORKER_NODATA = nodata


def _worker_tile(task) -> Tuple[float, float]:
    z, x, y, west, south, east, north, tile_size, out_dir = task
    assert _WORKER_SAMPLER is not None

    scheme = GeographicTilingScheme(tile_size=tile_size)
    lons = np.linspace(west, east, tile_size, dtype=np.float64)
    lats = np.linspace(south, north, tile_size, dtype=np.float64)
    llon, llat = np.meshgrid(lons, lats)
    heights = _WORKER_SAMPLER.sample(llon, llat)

    data = encode_quantized_mesh(west, south, east, north, heights)
    out_path = Path(out_dir) / str(z) / str(x)
    out_path.mkdir(parents=True, exist_ok=True)
    tile_file = out_path / f"{y}.terrain"
    with gzip.open(tile_file, "wb") as f:
        f.write(data)
    return float(np.min(heights)), float(np.max(heights))


def build_terrain(
    inputs: list[str],
    output_dir: str,
    *,
    min_level: int = 0,
    max_level: int | None = None,
    tile_size: int = 17,
    nodata: float | None = None,
    workers: int = 0,
) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    input_path = build_input_vrt(inputs)
    sampler = DemSampler(input_path, nodata=nodata)
    scheme = GeographicTilingScheme(tile_size=tile_size)

    if max_level is None:
        max_level = scheme.estimate_max_level(sampler.pixel_size_deg)

    src_w, src_s, src_e, src_n = sampler.bounds
    h_min_global = float("inf")
    h_max_global = float("-inf")

    all_tasks = []
    available_per_level = []

    # compute intersected tile ranges per level
    for z in range(min_level, max_level + 1):
        nx, ny = scheme.tile_count(z)
        # project bbox to tile index range (clamp)
        ix0 = max(0, int(math.floor((src_w + 180.0) / 360.0 * nx)))
        ix1 = min(nx - 1, int(math.floor((src_e + 180.0) / 360.0 * nx)))
        iy0 = max(0, int(math.floor((src_s + 90.0) / 180.0 * ny)))
        iy1 = min(ny - 1, int(math.floor((src_n + 90.0) / 180.0 * ny)))

        if z <= 4:
            x0, x1, y0, y1 = 0, nx - 1, 0, ny - 1
        else:
            x0, x1, y0, y1 = ix0, ix1, iy0, iy1

        if x1 < x0 or y1 < y0:
            available_per_level.append([])
            continue

        available_per_level.append([{"startX": x0, "startY": y0, "endX": x1, "endY": y1}])

        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                west, south, east, north = scheme.tile_extent_deg(z, x, y)
                all_tasks.append((z, x, y, west, south, east, north, tile_size, str(out)))

    total = len(all_tasks)
    sampler.ds = None
    sampler.band = None

    t0 = time.time()
    done = 0
    workers = int(workers or 0)
    if workers <= 0:
        workers = os.cpu_count() or 1

    if workers == 1 or total <= 4:
        _worker_init(input_path, nodata)
        for task in all_tasks:
            mn, mx = _worker_tile(task)
            h_min_global = min(h_min_global, mn)
            h_max_global = max(h_max_global, mx)
            done += 1
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(input_path, nodata)) as ex:
            for mn, mx in ex.map(_worker_tile, all_tasks, chunksize=8):
                h_min_global = min(h_min_global, mn)
                h_max_global = max(h_max_global, mx)
                done += 1

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
