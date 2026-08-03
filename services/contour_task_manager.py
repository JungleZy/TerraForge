"""
Contour Task Manager

上传驱动的高程→等高线渲染管线：用户上传 GeoTIFF DEM（数据处理不下载，
远程 DEM 下载统一在「数据下载」的 DEM 任务里做），直接用
contour_task_tiler 渲染等高线 PNG 瓦片。渲染引擎按 warp 后的 DEM 实际
覆盖决定瓦片范围，任务 bbox 只用于历史记录地图展示（创建时从上传文件
的范围并集算出来）。

dataset='upload' 即上传任务；早期版本下载驱动的任务（dataset 为
ASTGTM.003 / COP-DEM-GLO-30，由已删除的下载驱动 create_task 创建）
仍走旧的下载→渲染路径恢复执行。Lifecycle/threading mirror DemTaskManager
(active_tasks + stop_flags + orphan recovery)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from core.config import Config
from core.database import get_connection, utc_now_iso
from services.config_manager import ConfigManager
from services.dem_download_engine import DemDownloadEngine
from services.geo_validation import MAX_ZOOM, coerce_number, validate_zoom
from services.dem_granules import coverage_bbox

logger = logging.getLogger(__name__)

_ALLOWED_EXT = (".tif", ".tiff")


class UploadFile(Protocol):
    """上传文件对象（werkzeug FileStorage 或兼容替身）：路由直接把它传给
    manager，save() 分块流式落盘，不再 Tuple[str, bytes] 一次性读进内存。"""

    filename: str

    def save(self, dst) -> None: ...

# Web Mercator 赤道处 z0 单像素地面分辨率（米/像素，256px 瓦片）
_EQUATOR_M_PER_PX_Z0 = 156543.03392
# 自动层级算不出来（tif 读不出分辨率）时的兜底，即原固定默认值
_FALLBACK_MAX_ZOOM = 15
# 估算层级在“像素恰好匹配”的层级上再多给的过采样级数（保证线条平滑）
_AUTO_ZOOM_OVERSAMPLE = 1
# 渲染进度落库/广播的最小间隔(秒):引擎逐瓦片回调,高 zoom 大区域百万级
# 瓦片,不节流就是百万次写事务+广播
_RENDER_PROGRESS_MIN_INTERVAL = 0.5
# 下载进度广播的最小间隔(秒),对齐 dem_task_manager 的 1s 时间窗;
# 落库不节流(每颗粒终态必须如实记账),只节流 emit
_DOWNLOAD_PROGRESS_EMIT_MIN_INTERVAL = 1.0


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


# M7: 'skipped' 也算「已终结的下载项」。404 的颗粒（海洋 / 覆盖范围外 ——
# Copernicus GLO-30 对海面本来就没瓦片）由引擎有意上报 skipped，是部分成功
# 语义；但计数增量此前只认 completed/failed，收尾判定又把 skipped 算作已终结、
# 任务照常 completed。结果终态下 downloaded_files + failed_files < total_files
# 这个不变量被破坏：记录面板渲染「已完成 · 4 / 10 文件」，详情弹窗给一个
# **已完成任务** 40% 的进度条，下载过程中进度条同样封顶、「预计剩余」偏大。
# 磁盘产物与后续切片都是对的 —— 纯计数/展示口径问题。
_DONE_STATUSES = ("completed", "skipped")


def _status_count_deltas(old_status: Optional[str], new_status: str) -> tuple[int, int]:
    downloaded_delta = int(new_status in _DONE_STATUSES) - int(old_status in _DONE_STATUSES)
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

        # 廉价前置校验只有扩展名；空文件判定推迟到落盘后按实际大小查
        # （流式上传下读内容判空本身就违背不全部读进内存的目的）。
        valid: List[UploadFile] = []
        for f in files:
            ext = Path(f.filename or "").suffix.lower()
            if ext not in _ALLOWED_EXT:
                raise ValueError(f"Unsupported file type: {f.filename} (only .tif/.tiff)")
            valid.append(f)
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
        Path(output_path).mkdir(parents=True, exist_ok=True)

        # 上传先全量落盘到任务目录旁的暂存目录,再进 DB 事务。此前 INSERT
        # 隐式 BEGIN 的写事务里逐文件 f.save,GB 级上传期间占死 WAL 唯一
        # 写者,其他写方 30s busy_timeout 后 500。现在写事务里只剩毫秒级
        # 行写入;暂存目录与最终任务目录同盘,事务内 os.replace 改名即就位。
        staging = Path(tempfile.mkdtemp(prefix="contour_upload_", dir=output_path))
        task_dir: Optional[Path] = None
        try:
            staged: List[Tuple[str, int]] = []  # (stored_name, size)
            for idx, f in enumerate(valid, start=1):
                stored = f"upload_{idx}_dem.tif"
                dest = staging / stored
                f.save(dest)  # FileStorage.save 分块拷贝，不全部读进内存
                size = dest.stat().st_size
                if size == 0:
                    raise ValueError(f"Empty file: {f.filename}")
                staged.append((stored, size))
            saved = [staging / stored for stored, _ in staged]

            # bbox/最高层级按暂存文件预读(GDAL 只读头部元数据,毫秒级),
            # 不拖进后面的写事务。bbox 只用于历史记录地图展示,读不出保持 0。
            extent = _union_tif_extent_lonlat(saved)
            north, south, east, west = extent if extent else (0.0, 0.0, 0.0, 0.0)
            if auto_zoom_max:
                # 最高层级按 DEM 原始分辨率自动计算；读不出分辨率时用兜底默认值
                px = _finest_pixel_size_3857(saved)
                zoom_max = estimate_max_zoom(px, zoom_min) if px else max(zoom_min, _FALLBACK_MAX_ZOOM)

            conn = get_connection()
            try:
                cur = conn.cursor()
                # 下载计数即上传计数：没有下载阶段，文件行直接记 completed。
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
                    VALUES (?, 'pending', ?, ?, ?, ?, 'upload', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
                    """,
                    (name, north, south, east, west, interval, background, shade, zoom_min,
                     zoom_max, output_path, line_mid, line_idx, tint_breaks, tint_colors,
                     len(valid), len(staged)),
                )
                task_id = cur.lastrowid

                task_dir = Path(output_path) / f"contour_task_{task_id}"
                task_dir.mkdir(parents=True, exist_ok=True)
                for stored, size in staged:
                    dest = task_dir / stored
                    os.replace(staging / stored, dest)  # 同盘改名,毫秒级
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO contour_files (task_id, granule_id, kind, status, local_path, size_bytes, retry_count)
                        VALUES (?, ?, 'dem', 'completed', ?, ?, 0)
                        """,
                        (task_id, stored, str(dest), size),
                    )
                conn.commit()
                return task_id
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception:
            # 文件已落盘后失败（空文件/DB 异常等）：rowid 复用后
            # 残留 tif 会被下一个同 id 任务按 *_dem.tif 扫进渲染，清掉整个
            # 任务目录与暂存目录。best-effort，清理失败不掩盖原异常。
            if task_dir is not None:
                shutil.rmtree(task_dir, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # 全部 os.replace 后暂存目录已空;部分失败路径走上面的 except
        shutil.rmtree(staging, ignore_errors=True)

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
                    (utc_now_iso(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"Contour task {task_id} could not be started (status changed)")
                conn.commit()
                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(target=self._run_task, args=(task_id, stop_flag),
                                      daemon=True, name=f"ContourTask-{task_id}")
                self.active_tasks[task_id] = th
            try:
                th.start()
            except Exception:
                # L2: commit 与 thread.start() 之间的异常会留下「DB 是 running、
                # 线程从未启动」的任务。回退成 paused（可重新 start/resume）并
                # 清理登记 —— 与 task_manager/dem_task_manager 的下载管线一致。
                with self._state_lock:
                    if self.active_tasks.get(task_id) is th:
                        self.active_tasks.pop(task_id, None)
                    if self.stop_flags.get(task_id) is stop_flag:
                        self.stop_flags.pop(task_id, None)
                cur.execute(
                    "UPDATE contour_tasks SET status='paused' WHERE id=? AND status='running'",
                    (task_id,),
                )
                conn.commit()
                raise
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
                # U2: 终态任务不能静默返回成功 —— 路由会回 {"success": true},
                # 用户以为取消生效了。map/dem 都抛 ValueError,contour 跟上。
                raise ValueError(
                    f"Cannot cancel contour task {task_id} with status '{row['status']}'")
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

    def list_tasks(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        # SQLite LIMIT -1 表示不限行数：limit=-1 会绕过上限拉全表，钳到 [1, 100]
        limit = max(1, min(int(limit or 100), 100))
        conn = get_connection()
        try:
            # status='active' 是路由层契约的特殊值（同 /api/history_all）：
            # 展开成活动三态；其余取值（含 None）维持原行为。
            if status == 'active':
                rows = conn.execute(
                    "SELECT * FROM contour_tasks "
                    "WHERE status IN ('pending','running','paused') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
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

            # L1: 进程被硬杀时 'downloading' 会残留在 contour_files 里（该状态由
            # dem_download_engine 上报、经本类的进度回调落库；正常暂停会回写
            # pending）。不重新入队的话这些颗粒既不会被重下、也不会阻止任务置
            # completed —— 渲染在缺几块 1° DEM 的输入上出图，成品带缺口而任务
            # 报成功。dem_task_manager 早已这么做（C4），contour 没跟上。
            cur.execute(
                "UPDATE contour_files SET status='pending' WHERE task_id=? AND status='downloading'",
                (task_id,))
            conn.commit()

            dem_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='dem' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()]
            att_granules = [r["granule_id"] for r in cur.execute(
                "SELECT granule_id FROM contour_files WHERE task_id=? AND kind='water' AND status IN ('pending','failed') ORDER BY granule_id",
                (task_id,)).fetchall()] if want_water else []

            stop_ev = asyncio.Event()
            if stop_flag and stop_flag.is_set():
                stop_ev.set()

            # 进度记账（同步 sqlite I/O：新开连接 + SELECT + UPDATE + commit）
            # 整体放 worker 线程 —— 回调在下载事件循环里被 await，直接在循环
            # 上跑会堵住所有并发颗粒的下载协程（对齐 dem_task_manager）。
            def _record_progress(granule_id: str, status: str, error: Optional[str],
                                 size_bytes: Optional[int]) -> Optional[Dict[str, Any]]:
                tile_conn = get_connection()
                try:
                    c = tile_conn.cursor()
                    existing = c.execute("SELECT status FROM contour_files WHERE task_id=? AND granule_id=?",
                                         (task_id, granule_id)).fetchone()
                    old_status = existing["status"] if existing else None
                    c.execute(
                        "UPDATE contour_files SET status=?, error_message=?, size_bytes=?, local_path=? WHERE task_id=? AND granule_id=?",
                        # 引擎按扁平 basename 落盘(dem_download_engine.download_files:
                        # Copernicus granule 是 name/name.tif 嵌套),写库路径保持一致
                        (status, error, size_bytes, str(output_dir / Path(granule_id).name), task_id, granule_id),
                    )
                    d_delta, f_delta = _status_count_deltas(old_status, status)
                    if d_delta or f_delta:
                        c.execute(
                            "UPDATE contour_tasks SET downloaded_files=MAX(downloaded_files+?,0), failed_files=MAX(failed_files+?,0) WHERE id=?",
                            (d_delta, f_delta, task_id),
                        )
                    tile_conn.commit()
                    trow = c.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                    return dict(trow) if trow else None
                finally:
                    tile_conn.close()

            # emit 节流：距上次广播不足 _DOWNLOAD_PROGRESS_EMIT_MIN_INTERVAL
            # 且计数没变时只落库不广播（同 dem）；计数变化或到窗口必发，
            # payload 结构不变（task 整行 + task_type + phase='download'）。
            last_emit: Dict[str, Any] = {"at": float("-inf"), "counts": None}

            async def progress(granule_id: str, status: str, error: Optional[str], size_bytes: Optional[int]):
                row = await asyncio.to_thread(_record_progress, granule_id, status, error, size_bytes)
                if not row or not self.socketio:
                    return
                counts = (row["downloaded_files"], row["failed_files"])
                now = time.monotonic()
                if now - last_emit["at"] < _DOWNLOAD_PROGRESS_EMIT_MIN_INTERVAL and counts == last_emit["counts"]:
                    return
                last_emit["at"] = now
                last_emit["counts"] = counts
                row["task_type"] = "contour"
                row["phase"] = "download"
                self.socketio.emit("task_progress", row)

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
                       SUM(CASE WHEN status NOT IN ('completed','skipped','failed')
                                THEN 1 ELSE 0 END) AS pending_count
                FROM contour_files WHERE task_id=? AND kind='dem'
                """,
                (task_id,),
            ).fetchone()
            failed_count = counts["failed_count"] or 0
            pending_count = counts["pending_count"] or 0
            if failed_count > 0 or pending_count > 0:
                msg = f"{failed_count} DEM file(s) failed, {pending_count} pending"
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status='running'",
                            (msg, utc_now_iso(), task_id))
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

            # 渲染进度节流 + 连接复用:引擎 _emit 逐瓦片回调(见
            # contour_engine._emit),不节流时每次回调都是"新连接 + UPDATE +
            # 又一连接 SELECT 全行 + socketio.emit",百万级瓦片会把渲染拖垮、
            # 把前端打爆。这里:计数维护在内存,距上次落库不足
            # _RENDER_PROGRESS_MIN_INTERVAL 且未处理完时只记内存;落库复用同
            # 一连接;emit 载荷的静态字段在进入渲染阶段时取一次全行,之后每次
            # 只覆盖内存里的计数字段。引擎 BrokenProcessPool 回退重跑会把计数
            # 清零重报(contour_engine.py),时间节流天然兼容 —— 最终计数由
            # 下方收尾用 render_counts 重写,不依赖回调逐次累计。
            progress_conn = get_connection()
            try:
                prow = progress_conn.execute(
                    "SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
                base_payload = dict(prow) if prow else None
                # last_flush 初始 -inf:首次回调(进入渲染阶段的 0/total)必落库
                render_state = {"done": 0, "total": total_tiles, "last_flush": float("-inf")}

                def _flush_render_progress():
                    progress_conn.execute(
                        "UPDATE contour_tasks SET rendered_tiles=?, total_tiles=? WHERE id=?",
                        (render_state["done"], render_state["total"], task_id))
                    progress_conn.commit()
                    render_state["last_flush"] = time.monotonic()
                    if self.socketio and base_payload is not None:
                        payload = dict(base_payload)
                        payload["rendered_tiles"] = render_state["done"]
                        payload["total_tiles"] = render_state["total"]
                        payload["task_type"] = "contour"
                        payload["phase"] = "render"
                        # U1：同上 —— 渲染循环里的 emit 抛出会把整个任务记 failed。
                        try:
                            self.socketio.emit("task_progress", payload)
                        except Exception as emit_error:
                            logger.warning(
                                f"Contour task {task_id}: emit render progress "
                                f"failed (ignored): {emit_error}")

                def render_progress(done: int, total: int):
                    # done 是 processed(rendered+skipped+failed,见 contour_engine._emit):
                    # skipped 瓦片也计入进度,否则进度条停在如 72% 就直接 completed。
                    # 渲染期间 rendered_tiles 列暂存这个 processed 进度,收尾时在下面
                    # 用真实 rendered/failed 重写。
                    render_state["done"] = done
                    render_state["total"] = total
                    if done < total and \
                            time.monotonic() - render_state["last_flush"] < _RENDER_PROGRESS_MIN_INTERVAL:
                        return
                    _flush_render_progress()

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
                # 渲染结束(正常完成/暂停/部分失败)强制 flush:节流窗口内最后
                # 一段计数不丢。渲染异常由外层 except 标 failed,无需再 flush。
                _flush_render_progress()
            finally:
                progress_conn.close()

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
                            (msg, utc_now_iso(), task_id))
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
                        (utc_now_iso(), task_id))
            conn.commit()
            if cur.rowcount and self.socketio:
                self.socketio.emit("task_completed", {"task_id": task_id, "task_type": "contour", "status": "completed"})

        except Exception as e:
            try:
                cur = conn.cursor()
                # 'completed' 也要排除:上面的 emit("task_completed") 抛异常时会走到
                # 这里,不能把已经完成的任务改判 failed
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled','paused','completed')",
                            (str(e), utc_now_iso(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark contour task {task_id} failed: {e2}")
            raise
        finally:
            conn.close()
