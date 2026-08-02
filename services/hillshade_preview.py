"""
无地形切片任务的源 DEM 晕渲预览。

dem 任务「只下载不切片」是常态（切片是详情里另起的作业）；local_terrain 任务
的切片也可能被删。这两种情况下历史预览不该只 flyTo —— 这里把任务的源
*_dem.tif 按需渲染成一张灰度晕渲 PNG（VRT 马赛克 -> gdal DEMProcessing
hillshade -> 限制宽度的 PNG），缓存在任务产物目录，前端以单图矩形叠加
（SingleTileImageryProvider + 栅格范围）。

产物布局（与删除逻辑兼容：delete_files=true 删任务目录时一并清掉）：
  dem:          <output_path>/dem_task_<id>/preview_hillshade.png(.json)
  local_terrain: local_task_<id>/preview_hillshade.png(.json)（栅格源在 source/）
"""

import json
import logging
import os
import threading
from pathlib import Path

from services.terrain_tiling.vrt_builder import list_dem_tifs

logger = logging.getLogger(__name__)

HILLSHADE_PNG_NAME = "preview_hillshade.png"
HILLSHADE_META_NAME = "preview_hillshade.json"

# 预览图的宽度上限：1°x1° 的 30m DEM 是 3600+px，原尺寸 PNG 几 MB 且浏览器
# 单图叠加本来就会重采样，没理由全尺寸出图。
_MAX_WIDTH = 1600

# task 目录 -> 渲染锁：首次渲染是秒级活，双击/多标签并发触发时只渲染一次。
# 锁本身不持数据，模块级即可（不存在跨测试串库问题）。
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _read_cached(png_path: Path, meta_path: Path):
    if png_path.exists() and meta_path.exists():
        try:
            bounds = json.loads(meta_path.read_text(encoding="utf-8"))["bounds"]
            if isinstance(bounds, list) and len(bounds) == 4:
                return bounds
        except (ValueError, KeyError, OSError):
            pass  # 元数据损坏：掉下去重渲染
    return None


def _render(raster_dir: Path, png_path: Path):
    """渲染 raster_dir 下的 *_dem.tif -> png_path，返回 [w, s, e, n]；无源文件返回 None。"""
    from osgeo import gdal  # 惰性 import：不走路径拼接的进程不背 osgeo 依赖

    tifs = list_dem_tifs(raster_dir)
    if not tifs:
        return None

    vrt_path = f"/vsimem/hillshade_{os.getpid()}_{threading.get_ident()}.vrt"
    try:
        vrt = gdal.BuildVRT(vrt_path, [str(t) for t in tifs])
        if vrt is None:
            raise RuntimeError(f"gdal.BuildVRT failed for {raster_dir}")
        gt = vrt.GetGeoTransform()
        west, north = gt[0], gt[3]
        east = west + vrt.RasterXSize * gt[1]
        south = north + vrt.RasterYSize * gt[5]
        vrt = None

        hs = gdal.DEMProcessing("", vrt_path, "hillshade", format="MEM")
        if hs is None:
            raise RuntimeError(f"gdal.DEMProcessing failed for {raster_dir}")
        x, y = hs.RasterXSize, hs.RasterYSize
        if x > _MAX_WIDTH:
            y = max(1, round(y * _MAX_WIDTH / x))
            x = _MAX_WIDTH
        tmp_path = png_path.with_name(png_path.stem + ".tmp.png")
        out = gdal.Translate(str(tmp_path), hs, format="PNG", width=x, height=y)
        if out is None:
            raise RuntimeError(f"gdal.Translate failed for {png_path}")
        out = None
        hs = None
        # 原子替换：渲染中途被杀不会留下半截 PNG 被当成缓存
        os.replace(tmp_path, png_path)
        return [west, south, east, north]
    finally:
        gdal.Unlink(vrt_path)


def ensure_hillshade(raster_dir: Path, out_dir: Path):
    """确保晕渲预览已渲染，返回 (png_path, bounds)；无源文件返回 None。

    raster_dir: 源 *_dem.tif 所在目录（dem 任务目录 / local 任务的 source/）
    out_dir:    预览 PNG/元数据的落盘目录（任务产物目录，随 delete_files 一起清）
    """
    png_path = out_dir / HILLSHADE_PNG_NAME
    meta_path = out_dir / HILLSHADE_META_NAME

    bounds = _read_cached(png_path, meta_path)
    if bounds is not None:
        return png_path, bounds
    if not raster_dir.is_dir():
        return None

    lock = _lock_for(str(out_dir))
    with lock:
        # 双检：等锁期间另一个请求可能已渲染完
        bounds = _read_cached(png_path, meta_path)
        if bounds is not None:
            return png_path, bounds
        bounds = _render(raster_dir, png_path)
        if bounds is None:
            return None
        meta_path.write_text(json.dumps({"bounds": bounds}), encoding="utf-8")
        logger.info(f"Hillshade preview rendered: {png_path} bounds={bounds}")
        return png_path, bounds
