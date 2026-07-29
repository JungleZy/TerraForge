"""
Contour Task Manager

上传驱动的高程→等高线渲染管线：用户上传 GeoTIFF DEM（数据处理不下载，
远程 DEM 下载统一在「数据下载」的 DEM 任务里做），直接用
contour_task_tiler 渲染等高线 PNG 瓦片。渲染引擎按 warp 后的 DEM 实际
覆盖决定瓦片范围，任务 bbox 只用于历史记录地图展示（创建时从上传文件
的范围并集算出来）。

dataset='upload' 即上传任务；早期版本下载驱动的任务（dataset 为
ASTGTM.003 / COP-DEM-GLO-30，由 create_task 创建）仍走旧的下载→渲染
路径恢复执行。Lifecycle/threading mirror DemTaskManager
(active_tasks + stop_flags + orphan recovery)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import Config
from database import get_connection
from services.config_manager import ConfigManager
from services.dem_download_engine import DemDownloadEngine
from services.geo_validation import MAX_ZOOM, coerce_number, validate_bbox, validate_zoom
from services.dem_granules import (
    tiles_for_bbox, astgtm_v3_granules_for_tile, astwbd_v1_att_granules_for_tile,
    copernicus_glo30_granules_for_tile, coverage_bbox,
)

logger = logging.getLogger(__name__)

_ALLOWED_EXT = (".tif", ".tiff")
UploadFile = Tuple[str, bytes]  # (original_filename, content_bytes)

# Web Mercator 赤道处 z0 单像素地面分辨率（米/像素，256px 瓦片）
_EQUATOR_M_PER_PX_Z0 = 156543.03392
# 自动层级算不出来（tif 读不出分辨率）时的兜底，即原固定默认值
_FALLBACK_MAX_ZOOM = 15
# 估算层级在“像素恰好匹配”的层级上再多给的过采样级数（保证线条平滑）
_AUTO_ZOOM_OVERSAMPLE = 1


def estimate_max_zoom(pixel_size_3857_m: float, zoom_min: int) -> int:
    """按 DEM 原始分辨率（换算成 EPSG:3857 米/像素）估算最高瓦片层级：
    取瓦片分辨率恰好细于一个 DEM 像素的层级，再加过采样级数，
    结果夹在 [zoom_min, MAX_ZOOM]。pixel_size <= 0 视为未知，给满级。"""
    if pixel_size_3857_m <= 0:
        return MAX_ZOOM
    z = math.ceil(math.log2(_EQUATOR_M_PER_PX_Z0 / pixel_size_3857_m)) + _AUTO_ZOOM_OVERSAMPLE
    return min(MAX_ZOOM, max(zoom_min, z))


def _finest_pixel_size_3857(paths: Sequence[Path]) -> Optional[float]:
    """上传 tif 中最细的像素尺寸，换算成 EPSG:3857 米/像素。
    地理坐标系（度）：x 向像素在 3857 下 ≈ deg*111320（与纬度无关），
    y 向还要除以 cos(纬度)，取两者较小值；
    投影坐标系：像素在 3857 下放大 1/cos(中心纬度)。
    GDAL 不可用或全部读失败返回 None。"""
    try:
        from osgeo import gdal, osr
    except Exception:
        return None
    best: Optional[float] = None
    for p in paths:
        try:
            ds = gdal.Open(str(p))
            if ds is None:
                continue
            gt = ds.GetGeoTransform()
            w, h = ds.RasterXSize, ds.RasterYSize
            srs = osr.SpatialReference()
            srs.ImportFromWkt(ds.GetProjection())
            cx = gt[0] + (w / 2) * gt[1] + (h / 2) * gt[2]
            cy = gt[3] + (w / 2) * gt[4] + (h / 2) * gt[5]
            if srs.IsGeographic():
                lat = cy
                cos_lat = max(1e-6, math.cos(math.radians(lat)))
                px = min(abs(gt[1]) * 111320.0,
                         abs(gt[5]) * 111320.0 / cos_lat)
            else:
                src = osr.SpatialReference()
                src.ImportFromWkt(ds.GetProjection())
                src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                tgt = osr.SpatialReference()
                tgt.ImportFromEPSG(4326)
                tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                ct = osr.CoordinateTransformation(src, tgt)
                lat = ct.TransformPoint(cx, cy)[1]
                cos_lat = max(1e-6, math.cos(math.radians(lat)))
                unit = srs.GetLinearUnits() or 1.0
                px = min(abs(gt[1]), abs(gt[5])) * unit / cos_lat
            if px > 0:
                best = px if best is None else min(best, px)
            ds = None
        except Exception as e:
            logger.warning(f"读取上传 tif 分辨率失败 {p}: {e}")
    return best


def _union_tif_extent_lonlat(paths: Sequence[Path]) -> Optional[Tuple[float, float, float, float]]:
    """上传 tif 的 WGS84 范围并集，返回 (north, south, east, west)；
    GDAL 不可用或全部读失败时返回 None（任务仍可渲染，只是历史地图没框）。"""
    try:
        from osgeo import gdal, osr
    except Exception:
        return None
    west = south = float("inf")
    east = north = float("-inf")
    found = False
    for p in paths:
        try:
            ds = gdal.Open(str(p))
            if ds is None:
                continue
            gt = ds.GetGeoTransform()
            w, h = ds.RasterXSize, ds.RasterYSize
            src = osr.SpatialReference()
            src.ImportFromWkt(ds.GetProjection())
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            tgt = osr.SpatialReference()
            tgt.ImportFromEPSG(4326)
            tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            ct = osr.CoordinateTransformation(src, tgt)
            for px, py in ((0, 0), (w, 0), (w, h), (0, h)):
                x = gt[0] + px * gt[1] + py * gt[2]
                y = gt[3] + px * gt[4] + py * gt[5]
                lon, lat = ct.TransformPoint(x, y)[:2]
                west = min(west, lon); east = max(east, lon)
                south = min(south, lat); north = max(north, lat)
            found = True
            ds = None
        except Exception as e:
            logger.warning(f"读取上传 tif 范围失败 {p}: {e}")
    return (north, south, east, west) if found else None


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status == "completed") - int(old_status == "completed")
    failed_delta = int(new_status == "failed") - int(old_status == "failed")
    return downloaded_delta, failed_delta


def _parse_csv_floats(raw: str) -> tuple:
    parts = [x.strip() for x in str(raw).split(",") if x.strip() != ""]
    return tuple(float(x) for x in parts)


def _parse_csv_colors(raw: str) -> tuple:
    parts = [x.strip() for x in str(raw).split(",") if x.strip() != ""]
    for c in parts:
        if not c.startswith("#"):
            raise ValueError(f"Invalid color '{c}' (expect #RRGGBB)")
    return tuple(parts)


def validate_tint(breaks_raw: str, colors_raw: str) -> tuple:
    """校验分层设色自定义输入，返回规范化的 (breaks_csv, colors_csv)。
    两者都留空 = 用默认方案；只给一个 = 报错。断点必须递增，颜色数 = 断点数+1。"""
    breaks_raw = (breaks_raw or "").strip()
    colors_raw = (colors_raw or "").strip()
    if not breaks_raw and not colors_raw:
        return "", ""
    if not breaks_raw or not colors_raw:
        raise ValueError("分层断点与分层颜色必须同时提供（或都留空用默认方案）")
    breaks = _parse_csv_floats(breaks_raw)
    colors = _parse_csv_colors(colors_raw)
    if len(breaks) < 1:
        raise ValueError("分层断点至少 1 个")
    if any(b2 <= b1 for b1, b2 in zip(breaks, breaks[1:])):
        raise ValueError("分层断点必须严格递增")
    if len(colors) != len(breaks) + 1:
        raise ValueError(f"分层颜色数({len(colors)})必须等于断点数({len(breaks)})+1")
    return ",".join(str(b) for b in breaks), ",".join(colors)


def style_for_task(config, task) -> "ContourStyle":
    """全局默认方案（config）+ 任务级配色覆盖。task 是 contour_tasks 行
    （dict/Row 均可，缺列视为未覆盖）。可单测，无 GDAL 依赖。"""
    from dataclasses import replace

    from services.contour_engine import ContourStyle

    def _get(key, default=""):
        try:
            v = task[key]
        except (KeyError, IndexError):
            v = None
        return default if v in (None, "") else v

    style = ContourStyle.from_config(config)
    style = replace(style, background=task["background"] or "#FAF6EC")

    line_mid = _get("line_color_intermediate")
    line_idx = _get("line_color_index")
    if line_mid or line_idx:
        style = replace(
            style,
            color_intermediate=line_mid or style.color_intermediate,
            color_index=line_idx or style.color_index,
            color_label=line_idx or style.color_index,  # 标签跟随计曲线
        )

    tint_breaks = _get("tint_breaks")
    tint_colors = _get("tint_colors")
    if tint_breaks and tint_colors:
        style = replace(
            style,
            hypsometric_breaks=_parse_csv_floats(tint_breaks),
            hypsometric_colors=_parse_csv_colors(tint_colors),
        )
    return style


class ContourTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.engine = DemDownloadEngine()
        self.active_tasks: Dict[int, threading.Thread] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM contour_tasks WHERE status = 'running'")
            task_ids = [row["id"] for row in cur.fetchall()]
            if task_ids:
                cur.executemany(
                    "UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'",
                    [(tid,) for tid in task_ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan contour tasks (paused): {task_ids}")
        except Exception as e:
            logger.error(f"Failed to recover contour orphan tasks: {e}")
            conn.rollback()
        finally:
            conn.close()

    def create_task(self, params: dict) -> int:
        name = params.get("name") or "Contour Task"
        # 四至共用校验(范围/顺序/NaN/类型),见 services/geo_validation.py
        north, south, east, west = validate_bbox(
            params.get("north"), params.get("south"),
            params.get("east"), params.get("west"),
        )
        dataset = params.get("dataset") or "COP-DEM-GLO-30"
        if dataset not in ("ASTGTM.003", "COP-DEM-GLO-30"):
            raise ValueError(f"Unsupported dataset: {dataset}")

        interval_raw = params.get("contour_interval")
        if interval_raw in (None, ""):
            interval_raw = self.config.get("contour_default_interval", "50")
        interval = coerce_number(interval_raw, 'contour_interval')
        if interval <= 0:
            raise ValueError(f"contour_interval must be > 0, got {interval}")

        zoom_min = validate_zoom(params.get("zoom_min", 12), 'zoom_min')
        zoom_max = validate_zoom(params.get("zoom_max", 14), 'zoom_max')
        if zoom_min > zoom_max:
            raise ValueError(f"zoom_min ({zoom_min}) must be <= zoom_max ({zoom_max})")

        background = params.get("background") or "#FAF6EC"
        if background != "transparent" and not str(background).startswith("#"):
            background = "#FAF6EC"

        def _flag(key: str, default: int = 1) -> int:
            return 1 if str(params.get(key, default)).strip().lower() in ("1", "true", "yes", "on") else 0
        terrain_shade = _flag("terrain_shade")
        water = _flag("water")

        output_path = params.get("output_path") or str(Path(Config.DOWNLOADS_DIR) / "dem")

        tiles = tiles_for_bbox(north=north, south=south, east=east, west=west)
        dem_granules: List[str] = []
        for t in tiles:
            if dataset == "COP-DEM-GLO-30":
                dem_granules.extend(copernicus_glo30_granules_for_tile(t))
            else:
                dem_granules.extend(astgtm_v3_granules_for_tile(t, include_num=False, include_swb=False))
        att_granules: List[str] = []
        if water:
            for t in tiles:
                att_granules.extend(astwbd_v1_att_granules_for_tile(t))
        total_files = len(dem_granules) + len(att_granules)

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO contour_tasks (
                    name, status, north, south, east, west, dataset,
                    contour_interval, background, terrain_shade, water,
                    zoom_min, zoom_max, output_path,
                    total_files, downloaded_files, failed_files,
                    total_tiles, rendered_tiles, failed_tiles
                )
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
                """,
                (name, north, south, east, west, dataset,
                 interval, background, terrain_shade, water,
                 zoom_min, zoom_max, output_path, total_files),
            )
            task_id = cur.lastrowid
            file_rows = [(task_id, g, "dem") for g in dem_granules] + \
                        [(task_id, g, "water") for g in att_granules]
            cur.executemany(
                "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count) VALUES (?, ?, ?, 'pending', 0)",
                file_rows,
            )
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_task_with_files(
        self,
        name: str,
        files: Sequence[UploadFile],
        contour_interval: Any = None,
        zoom_min: Any = 10,
        zoom_max: Any = None,
        background: Optional[str] = None,
        terrain_shade: Any = 1,
        line_color_intermediate: Optional[str] = None,
        line_color_index: Optional[str] = None,
        tint_breaks: Optional[str] = None,
        tint_colors: Optional[str] = None,
    ) -> int:
        """上传驱动的创建：校验并落盘上传的 GeoTIFF，建 contour_tasks 行
        （dataset='upload', water=0），bbox 取上传文件的范围并集（仅供历史
        记录地图展示）。zoom_max 传 None/空串时按 DEM 原始分辨率自动计算
        最高层级（读不出分辨率时兜底 _FALLBACK_MAX_ZOOM）。文件命名为
        upload_<i>_dem.tif —— 渲染阶段的 vrt_builder.list_dem_tifs 按
        *_dem.tif 扫描任务目录。
        """
        name = (name or "等高线瓦片").strip() or "等高线瓦片"

        valid: List[UploadFile] = []
        for original, content in files:
            ext = Path(original or "").suffix.lower()
            if ext not in _ALLOWED_EXT:
                raise ValueError(f"Unsupported file type: {original} (only .tif/.tiff)")
            if not content:
                raise ValueError(f"Empty file: {original}")
            valid.append((original, content))
        if not valid:
            raise ValueError("No valid .tif/.tiff files uploaded")

        if contour_interval in (None, ""):
            contour_interval = self.config.get("contour_default_interval", "50")
        interval = coerce_number(contour_interval, 'contour_interval')
        if interval <= 0:
            raise ValueError(f"contour_interval must be > 0, got {interval}")

        zoom_min = validate_zoom(zoom_min, 'zoom_min')
        # zoom_max 留空/None = 按 DEM 分辨率自动计算（文件落盘后在下面算）
        auto_zoom_max = zoom_max in (None, "")
        if not auto_zoom_max:
            zoom_max = validate_zoom(zoom_max, 'zoom_max')
            if zoom_min > zoom_max:
                raise ValueError(f"zoom_min ({zoom_min}) must be <= zoom_max ({zoom_max})")

        background = background or "#FAF6EC"
        if background != "transparent" and not str(background).startswith("#"):
            background = "#FAF6EC"
        shade = 1 if str(terrain_shade).strip().lower() in ("1", "true", "yes", "on") else 0

        # 配色自定义（可选，空 = 默认方案）
        line_mid = (line_color_intermediate or "").strip()
        line_idx = (line_color_index or "").strip()
        for c in (line_mid, line_idx):
            if c and not c.startswith("#"):
                raise ValueError(f"Invalid color '{c}' (expect #RRGGBB)")
        tint_breaks, tint_colors = validate_tint(tint_breaks, tint_colors)

        output_path = str(Path(Config.DOWNLOADS_DIR) / "dem")

        conn = get_connection()
        try:
            cur = conn.cursor()
            # 先建行拿 id（bbox 先填 0，算完范围再更新）。下载计数即上传计数：
            # 没有下载阶段，文件行直接记 completed。
            cur.execute(
                """
                INSERT INTO contour_tasks (
                    name, status, north, south, east, west, dataset,
                    contour_interval, background, terrain_shade, water,
                    zoom_min, zoom_max, output_path,
                    line_color_intermediate, line_color_index, tint_breaks, tint_colors,
                    total_files, downloaded_files, failed_files,
                    total_tiles, rendered_tiles, failed_tiles
                )
                VALUES (?, 'pending', 0, 0, 0, 0, 'upload', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
                """,
                (name, interval, background, shade, zoom_min,
                 zoom_min if auto_zoom_max else zoom_max,
                 output_path, line_mid, line_idx, tint_breaks, tint_colors, len(valid)),
            )
            task_id = cur.lastrowid

            task_dir = Path(output_path) / f"contour_task_{task_id}"
            task_dir.mkdir(parents=True, exist_ok=True)

            saved: List[Path] = []
            for idx, (original, content) in enumerate(valid, start=1):
                stored = f"upload_{idx}_dem.tif"
                dest = task_dir / stored
                dest.write_bytes(content)
                saved.append(dest)
                cur.execute(
                    """
                    INSERT INTO contour_files (task_id, granule_id, kind, status, local_path, size_bytes, retry_count)
                    VALUES (?, ?, 'dem', 'completed', ?, ?, 0)
                    """,
                    (task_id, stored, str(dest), len(content)),
                )

            extent = _union_tif_extent_lonlat(saved)
            if extent:
                north, south, east, west = extent
                cur.execute(
                    "UPDATE contour_tasks SET north=?, south=?, east=?, west=? WHERE id=?",
                    (north, south, east, west, task_id),
                )
            if auto_zoom_max:
                # 最高层级按 DEM 原始分辨率自动计算；读不出分辨率时用兜底默认值
                px = _finest_pixel_size_3857(saved)
                zoom_max = estimate_max_zoom(px, zoom_min) if px else max(zoom_min, _FALLBACK_MAX_ZOOM)
                cur.execute(
                    "UPDATE contour_tasks SET zoom_max=? WHERE id=?",
                    (zoom_max, task_id),
                )
            cur.execute(
                "UPDATE contour_tasks SET downloaded_files=? WHERE id=?",
                (len(saved), task_id),
            )
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            with self._state_lock:
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(f"Contour task {task_id} is already running")
                cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                if row["status"] not in ("pending", "paused"):
                    raise ValueError(f"Cannot start contour task {task_id} with status '{row['status']}'")
                cur.execute(
                    "UPDATE contour_tasks SET status='running', started_at=? WHERE id=? AND status IN ('pending','paused')",
                    (datetime.now(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"Contour task {task_id} could not be started (status changed)")
                conn.commit()
                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag),
                                      daemon=True, name=f"ContourTask-{task_id}")
                self.active_tasks[task_id] = th
            th.start()
        finally:
            conn.close()

    def pause_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                raise ValueError(f"Cannot pause contour task {task_id} with status '{row['status']}'")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def resume_task(self, task_id: int) -> None:
        self.start_task(task_id)

    def cancel_task(self, task_id: int) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE contour_tasks SET status='cancelled' WHERE id=? AND status IN ('pending','running','paused')", (task_id,))
            if cur.rowcount == 0:
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
            conn.commit()
            with self._state_lock:
                if task_id in self.stop_flags:
                    self.stop_flags[task_id].set()
        finally:
            conn.close()

    def delete_task(self, task_id: int) -> None:
        """删除任务行。与 start_task 同一把 _state_lock 锁内复查 active 线程 +
        DB 状态:运行中(active 线程存活或 status='running')抛 ValueError 拒绝 ——
        路由层此前绕开 manager 锁直查 DB 再删,与正在跑的任务线程存在
        check-then-act 竞态。磁盘产物清理由路由层负责(delete_files)。"""
        conn = get_connection()
        try:
            cur = conn.cursor()
            with self._state_lock:
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(
                        f"Cannot delete running contour task {task_id}. Pause or cancel it first.")
                row = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                if not row:
                    raise ValueError(f"Contour task {task_id} not found")
                if row["status"] == "running":
                    raise ValueError(
                        f"Cannot delete running contour task {task_id}. Pause or cancel it first.")
                cur.execute("DELETE FROM contour_tasks WHERE id=?", (task_id,))
                conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Contour task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = min(int(limit or 100), 100)
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM contour_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _update_render_counts(self, task_id: int, rendered: int,
                              total: Optional[int] = None,
                              failed: Optional[int] = None) -> None:
        """渲染进度落库。渲染期间 rendered_tiles 存的是 processed 进度
        (rendered+skipped+failed,见 render_progress);任务收尾时调用方用
        真实 rendered/failed 重写一遍,让 rendered_tiles/failed_tiles 列语义如实。
        total/failed 传 None 表示不动对应列。"""
        sets = ["rendered_tiles=?"]
        vals: List[Any] = [rendered]
        if total is not None:
            sets.append("total_tiles=?")
            vals.append(total)
        if failed is not None:
            sets.append("failed_tiles=?")
            vals.append(failed)
        vals.append(task_id)
        conn = get_connection()
        try:
            conn.execute(f"UPDATE contour_tasks SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()

    def _run_task(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        try:
            asyncio.run(self._execute(task_id, stop_flag))
        except Exception as e:
            logger.error(f"Contour task {task_id} thread failed: {e}")
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)

    async def _execute(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            task = cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError(f"Contour task {task_id} not found")

            dataset = task["dataset"]
            is_upload = dataset == "upload"
            output_dir = Path(task["output_path"]) / f"contour_task_{task_id}"
            want_water = bool(task["water"])

            dem_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='dem' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()]
            att_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='water' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()] if want_water else []

            stop_ev = asyncio.Event()
            if stop_flag and stop_flag.is_set():
                stop_ev.set()

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                tile_conn = get_connection()
                try:
                    c = tile_conn.cursor()
                    existing = c.execute("SELECT status FROM contour_files WHERE task_id=? AND granule_id=?",
                                         (task_id, granule_id)).fetchone()
                    old_status = existing["status"] if existing else None
                    c.execute(
                        "UPDATE contour_files SET status=?, error_message=?, size_bytes=?, local_path=? WHERE task_id=? AND granule_id=?",
                        (status, error, size_bytes, str(output_dir / granule_id), task_id, granule_id),
                    )
                    d_delta, f_delta = _status_count_deltas(old_status, status)
                    if d_delta or f_delta:
                        c.execute(
                            "UPDATE contour_tasks SET downloaded_files=MAX(downloaded_files+?,0), failed_files=MAX(failed_files+?,0) WHERE id=?",
                            (d_delta, f_delta, task_id),
                        )
                    tile_conn.commit()
                    trow = c.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                    if trow and self.socketio:
                        payload = dict(trow)
                        payload["task_type"] = "contour"
                        payload["phase"] = "download"
                        self.socketio.emit("task_progress", payload)
                finally:
                    tile_conn.close()

            # 上传任务没有下载阶段：文件行创建时就是 completed，直接进渲染。
            if not is_upload:
                async def stop_watcher():
                    while True:
                        if stop_flag and stop_flag.is_set():
                            stop_ev.set()
                            return
                        await asyncio.sleep(0.2)

                watcher = asyncio.create_task(stop_watcher())
                try:
                    await self.engine.download_files(
                        dataset=dataset, granules=dem_granules, output_dir=output_dir,
                        progress_callback=progress, stop_flag=stop_ev,
                    )
                    # Water (ASTWBD) is best-effort: tiles with no water bodies may have
                    # no att granule (404), which must not fail the task.
                    if att_granules and not stop_ev.is_set():
                        await self.engine.download_files(
                            dataset="ASTWBD.001", granules=att_granules, output_dir=output_dir,
                            progress_callback=progress, stop_flag=stop_ev,
                        )
                finally:
                    watcher.cancel()

            if stop_ev.is_set():
                return

            current = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not current or current["status"] in ("cancelled", "paused"):
                return

            counts = cur.execute(
                """
                SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count
                FROM contour_files WHERE task_id=? AND kind='dem'
                """,
                (task_id,),
            ).fetchone()
            failed_count = counts["failed_count"] or 0
            pending_count = counts["pending_count"] or 0
            if failed_count > 0 or pending_count > 0:
                msg = f"{failed_count} DEM file(s) failed, {pending_count} pending"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            # ---- One-stop render phase: DEM downloaded -> contour tiles ----
            from services.contour_task_tiler import ContourParams, tile_contour_task_dir
            from services.contour_engine import count_tiles

            style = style_for_task(self.config, task)
            interval = float(task["contour_interval"])
            zoom_min = int(task["zoom_min"]); zoom_max = int(task["zoom_max"])
            if is_upload:
                # 上传任务的覆盖就是 DEM 文件本身的范围（不是 1° granule 并集），
                # 预计算没有意义 —— total 由渲染引擎 warp 后按实际覆盖上报。
                total_tiles = 0
            else:
                # Contours render over the whole downloaded DEM (union of 1° granule
                # tiles), not just the framed bbox, so count tiles over that coverage.
                cov_n, cov_s, cov_e, cov_w = coverage_bbox(task["north"], task["south"], task["east"], task["west"])
                total_tiles = count_tiles(cov_n, cov_s, cov_e, cov_w, zoom_min, zoom_max)

            def render_progress(done: int, total: int):
                # done 是 processed(rendered+skipped+failed,见 contour_engine._emit):
                # skipped 瓦片也计入进度,否则进度条停在如 72% 就直接 completed。
                # 渲染期间 rendered_tiles 列暂存这个 processed 进度,收尾时在下面
                # 用真实 rendered/failed 重写。
                self._update_render_counts(task_id, rendered=done, total=total)
                if self.socketio:
                    trow = self.get_task(task_id)
                    payload = dict(trow)
                    payload["task_type"] = "contour"
                    payload["phase"] = "render"
                    self.socketio.emit("task_progress", payload)

            # 立即推一次 render 阶段事件:DEM 下载完进入切片时,前端要马上从"下载 DEM"
            # 切到"渲染瓦片 0/total",不必手动刷新。warp 大区域可能耗时数十秒、期间无
            # 瓦片产出,这一发确保用户看到已进入渲染阶段而非卡在下载 100%。
            logger.info(f"Contour task {task_id}: 进入渲染阶段, 预计 {total_tiles} 瓦片")
            render_progress(0, total_tiles)

            try:
                workers = int(self.config.get("contour_workers", "0") or 0)
            except (TypeError, ValueError):
                workers = 0
            params = ContourParams(interval=interval, zoom_min=zoom_min, zoom_max=zoom_max,
                                   style=style, shade=bool(task["terrain_shade"]), water=want_water,
                                   workers=workers)
            render_counts = tile_contour_task_dir(
                task_dir=output_dir, out_dir=output_dir / "contour_tiles",
                params=params, progress_cb=render_progress, stop_flag=stop_flag,
            )

            if stop_flag and stop_flag.is_set():
                return
            # 列语义收尾:rendered_tiles/failed_tiles 落回真实渲染/失败数
            # (渲染期间 rendered_tiles 暂存的是 processed 进度,见 render_progress)。
            # total_tiles 不动 —— 引擎 warp 后上报的 total 已由进度回调写入,
            # render_counts["total"] 为 0(如假 tiler/异常边界)时不能把它冲掉。
            self._update_render_counts(
                task_id,
                rendered=render_counts.get("rendered", 0),
                failed=render_counts.get("failed", 0),
            )
            if render_counts.get("rendered", 0) == 0:
                msg = "No contour tiles rendered (check DEM coverage / interval / zoom range)"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": msg})
                return

            # 诊断:部分瓦片渲染失败(被 _render_contour_tile_core 的 except 吞成 failed)
            # 仍会标 completed,瓦片会缺。记 warning 便于排查"切片不完整"——failed 大说明
            # 是渲染异常,failed=0 但缺层多半是低 zoom 无等高线穿过的设计性 skip。
            failed_tiles = render_counts.get("failed", 0)
            if failed_tiles > 0:
                logger.warning(
                    f"Contour task {task_id}: {failed_tiles} 个瓦片渲染失败 "
                    f"(rendered={render_counts.get('rendered', 0)}, total={render_counts.get('total', 0)}),切片可能不完整"
                )

            cur.execute("UPDATE contour_tasks SET status='completed', completed_at=? WHERE id=? AND status='running'",
                        (datetime.now(), task_id))
            conn.commit()
            if cur.rowcount and self.socketio:
                self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "contour", "status": "completed"})

        except Exception as e:
            try:
                cur = conn.cursor()
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled','paused')",
                            (str(e), datetime.now(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark contour task {task_id} failed: {e2}")
            raise
        finally:
            conn.close()
