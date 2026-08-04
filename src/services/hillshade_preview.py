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

        # M13: 先缩后算，别先算后缩。此前是对原分辨率直接做 MEM 晕渲、算完再
        # Translate 到 1600px —— 实测 12000×12000 的 VRT 就是 +143MB / 5.4s，
        # 线性外推到 36000×36000（10°×10° ASTER）约 1.24GB / ~50s，而产物最终
        # 只是一张 1600px 宽的 PNG。上游没有面积/颗粒数上限，路由又是在 Flask
        # 请求线程里同步调用、无超时、无尺寸预检。Linux overcommit 下常见的失败
        # 形态不是 MemoryError，而是写满时被 OOM killer 杀掉整个进程。
        #
        # 副作用要知情：缩略图上算出的坡度按已变粗的像元尺寸计算，起伏会比
        # 全分辨率结果更平缓、更平滑。对预览图可接受（甚至更干净），但这是
        # 视觉行为变化，不是纯性能优化。
        #
        # 注意这条改法【不覆盖】BuildVRT 取并集范围的问题：两片相距 10° 的
        # 小 tif 仍会合出一张巨大的稀疏 VRT，只是现在按 1600px 缩略图分配内存，
        # 预览图绝大部分仍是 nodata 空白。要治那条得按各输入实际范围裁剪或分别出图。
        src_path = vrt_path
        thumb_path = None
        thumb = None
        vrt_info = gdal.Open(vrt_path)
        full_x = vrt_info.RasterXSize if vrt_info is not None else 0
        full_y = vrt_info.RasterYSize if vrt_info is not None else 0
        vrt_info = None
        if full_x > _MAX_WIDTH:
            thumb_y = max(1, round(full_y * _MAX_WIDTH / full_x))
            thumb_path = f"/vsimem/hillshade_thumb_{os.getpid()}_{threading.get_ident()}.vrt"
            thumb = gdal.Translate(thumb_path, vrt_path, format="VRT",
                                   width=_MAX_WIDTH, height=thumb_y)
            if thumb is None:
                raise RuntimeError(f"gdal.Translate (thumbnail VRT) failed for {raster_dir}")
            thumb = None
            src_path = thumb_path

        hs = None
        out = None
        try:
            hs = gdal.DEMProcessing("", src_path, "hillshade", format="MEM")
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
            # 异常路径同样要放掉句柄，否则 MEM 数据集会一直占着内存
            out = None
            hs = None
            if thumb_path:
                gdal.Unlink(thumb_path)
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
