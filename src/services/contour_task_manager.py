"""
Contour Task Manager

上传驱动的高程→等高线渲染管线：用户上传 GeoTIFF DEM（数据处理不下载，
远程 DEM 下载统一在「数据下载」的 DEM 任务里做），直接用
contour_task_tiler 渲染等高线 PNG 瓦片。渲染引擎按 warp 后的 DEM 实际
覆盖决定瓦片范围，任务 bbox 只用于历史记录地图展示（创建时从上传文件
的范围并集算出来）。

dataset 只有两种可达取值。'upload' 是用户上传的 tif（文件落在任务目录里）；
'dem_task' 是引用某个已完成 DEM 下载任务的目录 —— 零拷贝，源 tif 留在原地
不拷进来，产物仍落在本等高线任务自己的目录里，所以删除该等高线任务不会动
源 DEM 任务的文件。

早期版本还有下载驱动的任务（dataset 为 ASTGTM.003 / COP-DEM-GLO-30），它的
create_task 与执行分支都已删除 —— _execute 只留一行守卫，把这类存量行直接
判失败并说明原因。Lifecycle/threading mirror DemTaskManager
(active_tasks + stop_flags + orphan recovery)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from src.core.config import Config
from src.core.database import get_connection, utc_now_iso
from src.services.config_manager import ConfigManager
from src.services.geo_validation import MAX_ZOOM, coerce_number, validate_zoom
from src.services.task_cleanup import (fail_stranded_running_task,
                                       resolve_stored_output_dir)

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
# 等高线间距下限(米)。UI 的 min="1" 从来没被真正执行(提交路径 parseFloat 后
# 直接 FormData 提交,不走 checkValidity),而间距相对起伏过小时级数会炸开:
# 0.1m 间距叠 1000m 起伏 ≈ 单瓦片 1 万条 trace,瓦片内部又没有停止检查,
# 暂停/删除都打不断。这里就是那个 min="1" 的服务端执行点。
_MIN_CONTOUR_INTERVAL = 1.0


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


def _union_tif_extent_lonlat(
    paths: Sequence[Path],
    display_names: Optional[Sequence[str]] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """上传 tif 的 WGS84 范围并集，返回 (north, south, east, west)。

    这同时是「上传的到底是不是可读栅格」的唯一闸门 —— 创建路径本来就要为
    bbox 把每个文件用 GDAL 打开一次，顺手把结论用掉，不必再读一遍。

    两种读失败必须分开：
    * GDAL 装不上（ImportError）→ 返回 None，宽容放过。此时任何本地校验都做
      不了，卡住用户没有意义，渲染阶段自会因为缺 GDAL 而失败。
    * GDAL 在位但这个文件打不开 / 读不出角点 → 抛 ValueError（路由转 400）。
      以前这里只记一条 warning、bbox 保持 (0,0,0,0)，任务照常 201；真正的
      失败要等到 warp 之后，以一句原始 GDAL 报错落在一个 failed 任务上 ——
      用户上传了一个 .tif 后缀的 zip 也要等几分钟才知道。

    display_names 与 paths 一一对应，用来在报错里说**用户自己那个文件名**：
    上传路径已经把文件改名成 upload_<i>_dem.tif 落到暂存目录了，报那个名字
    等于让用户自己去数第几个文件。缺省时用磁盘上的名字。
    """
    try:
        from osgeo import gdal, osr
    except ImportError:
        return None
    west = south = float("inf")
    east = north = float("-inf")
    found = False
    for idx, p in enumerate(paths):
        try:
            ds = gdal.Open(str(p))
            if ds is None:
                raise ValueError("GDAL 打不开这个文件")
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
            try:
                shown = display_names[idx]
            except (TypeError, IndexError):
                shown = Path(p).name
            raise ValueError(f"不是可读的 GeoTIFF 栅格: {shown} ({e})") from e
    return (north, south, east, west) if found else None


def _parse_csv_floats(raw: str) -> tuple:
    parts = [x.strip() for x in str(raw).split(",") if x.strip() != ""]
    return tuple(float(x) for x in parts)


# 严格 #RGB / #RRGGBB / #RRGGBBAA。只在 matplotlib 装不上时兜底用。
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def validate_color(value: str, field: str = "color") -> str:
    """校验单个颜色值，返回原值；非法抛 ValueError（路由转 400）。

    判据用渲染器自己的解析器 matplotlib.colors.to_rgba —— 「创建时收下的」
    必须恰好等于「渲染时画得出的」。以前只查 `#` 前缀，于是 '#zzzzzz' 一路
    通到 per-tile 渲染，在那个吞异常的 except 里把**每一张**瓦片记成 failed，
    任务最后报「No contour tiles rendered (check DEM coverage / interval /
    zoom range)」—— 指着三个都正确的参数，而且要在整轮 warp + 全量瓦片
    (数分钟到数小时)之后才报。同一个值 /api/contour/style_preview 直接 400。

    不用纯正则：正则会连 'red'、'#FAF6ECFF' 这些渲染器接得住的值一起拒掉，
    那是另一种口径分歧。matplotlib 缺失时才退回正则 —— 此时渲染本来也跑不了，
    但「#zzzzzz 必须被拒」这条不能因为依赖缺失而失效。
    """
    v = str(value).strip()
    try:
        from matplotlib.colors import to_rgba
    except ImportError:
        if not _HEX_COLOR_RE.match(v):
            raise ValueError(f"Invalid {field} '{value}' (expect #RRGGBB)")
        return v
    try:
        to_rgba(v)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid {field} '{value}': {e}") from e
    return v


def _parse_csv_colors(raw: str) -> tuple:
    parts = [x.strip() for x in str(raw).split(",") if x.strip() != ""]
    for c in parts:
        validate_color(c, "tint color")
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
    """全局默认方案（config）+ 任务级配色覆盖 + detail_zoom 夹到 zoom_max。
    task 是 contour_tasks 行（dict/Row 均可，缺列视为未覆盖）。可单测，无
    GDAL 依赖。"""
    from dataclasses import replace

    from src.services.contour_engine import ContourStyle

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

    # detail_zoom 夹到本任务真正会产出的最高层级。interval_for_zoom 按
    # (detail_zoom - z) 沿 1-2-5 阶梯放粗，而 detail_zoom 默认 14、zoom_max
    # 却是按 DEM 分辨率自动算出来的：粗源算出 zoom_max=9 时，用户填的 50m 在
    # **最细**的那一层就已经被放粗成 2500m，而 API 响应和界面都不报告这件事,
    # 用户看到的只是「我填的间距被无视了」。夹住之后 zoom_max 这一层恒等于
    # 用户填的间距，更低层照旧逐级放粗（间距只依赖 zoom，跨瓦片仍然对得上）。
    zoom_max = _get("zoom_max", default=None)
    if zoom_max is not None:
        try:
            zmax = int(zoom_max)
        except (TypeError, ValueError):
            zmax = None
        if zmax is not None and zmax < style.detail_zoom:
            style = replace(style, detail_zoom=zmax)
    return style


class ContourTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
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

    def _normalize_render_params(
        self,
        contour_interval: Any = None,
        zoom_min: Any = 10,
        zoom_max: Any = None,
        background: Optional[str] = None,
        terrain_shade: Any = 1,
        line_color_intermediate: Optional[str] = None,
        line_color_index: Optional[str] = None,
        tint_breaks: Optional[str] = None,
        tint_colors: Optional[str] = None,
    ) -> Dict[str, Any]:
        """两个构造方法（上传 / 复用 DEM 任务目录）共用的间距、层级、配色校验。

        auto_zoom_max=True 时返回的 zoom_max 仍是调用方传进来的空值 —— 自动
        层级要按源 tif 的实际分辨率算，而这里只做不碰文件的纯参数校验。
        """
        if contour_interval in (None, ""):
            contour_interval = self.config.get("contour_default_interval", "50")
        interval = coerce_number(contour_interval, 'contour_interval')
        if interval < _MIN_CONTOUR_INTERVAL:
            raise ValueError(
                f"contour_interval must be >= {_MIN_CONTOUR_INTERVAL:g}, "
                f"got {interval:g}")

        zoom_min = validate_zoom(zoom_min, 'zoom_min')
        # zoom_max 留空/None = 按 DEM 分辨率自动计算（源文件就位后由调用方算）
        auto_zoom_max = zoom_max in (None, "")
        if not auto_zoom_max:
            zoom_max = validate_zoom(zoom_max, 'zoom_max')
            if zoom_min > zoom_max:
                raise ValueError(f"zoom_min ({zoom_min}) must be <= zoom_max ({zoom_max})")

        # 背景色空值走默认；'transparent' 是合法特值；其余必须是渲染器认得的
        # 颜色。以前非 '#' 开头的值被静默换成默认色、而 '#zzzzzz' 因为带 '#'
        # 被放过（然后在 _build_render_ctx 里炸掉整个任务）——两头都不对。
        background = background or "#FAF6EC"
        if str(background).strip().lower() != "transparent":
            background = validate_color(background, "background")
        shade = 1 if str(terrain_shade).strip().lower() in ("1", "true", "yes", "on") else 0

        # 配色自定义（可选，空 = 默认方案）
        line_mid = (line_color_intermediate or "").strip()
        line_idx = (line_color_index or "").strip()
        if line_mid:
            validate_color(line_mid, "line_color_intermediate")
        if line_idx:
            validate_color(line_idx, "line_color_index")
        tint_breaks, tint_colors = validate_tint(tint_breaks, tint_colors)

        return {
            "interval": interval,
            "zoom_min": zoom_min,
            "zoom_max": zoom_max,
            "auto_zoom_max": auto_zoom_max,
            "background": background,
            "shade": shade,
            "line_mid": line_mid,
            "line_idx": line_idx,
            "tint_breaks": tint_breaks,
            "tint_colors": tint_colors,
        }

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

        render = self._normalize_render_params(
            contour_interval, zoom_min, zoom_max, background, terrain_shade,
            line_color_intermediate, line_color_index, tint_breaks, tint_colors)
        interval = render["interval"]
        zoom_min = render["zoom_min"]
        zoom_max = render["zoom_max"]
        auto_zoom_max = render["auto_zoom_max"]
        background = render["background"]
        shade = render["shade"]
        line_mid = render["line_mid"]
        line_idx = render["line_idx"]
        tint_breaks = render["tint_breaks"]
        tint_colors = render["tint_colors"]

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
            # 不拖进后面的写事务。bbox 只用于历史记录地图展示,读不出保持 0;
            # 但「GDAL 在位却打不开」是硬错误 —— 这一步同时是栅格校验闸门,
            # 报错要说用户自己那个文件名而不是暂存后的 upload_N_dem.tif。
            extent = _union_tif_extent_lonlat(
                saved, display_names=[f.filename for f in valid])
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
            # 任务目录。best-effort，清理失败不掩盖原异常。
            if task_dir is not None:
                shutil.rmtree(task_dir, ignore_errors=True)
            raise
        finally:
            # 暂存目录必须走 finally：成功路径是 try 内的 `return task_id`
            # 退出的，跟在 try/except 之后的清理语句永远执行不到 —— 每个
            # 成功的上传任务都会泄漏一个空的 contour_upload_* 目录。
            # （同族的 local_terrain_task_manager 只是因为 return 后面还有
            # 语句才碰巧执行到。）
            shutil.rmtree(staging, ignore_errors=True)

    def create_task_from_dem_task(
        self,
        name: str,
        dem_task_id: Any,
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
        """复用某个已完成 DEM 下载任务的目录当源（dataset='dem_task'）。

        源 tif 零拷贝：contour_files.local_path 直接指向 DEM 任务目录里的原
        文件，本任务目录只放产物（contour_tiles）。所以删任务只会清自己的
        目录，源 DEM 任务的数据不受影响。没有下载阶段，文件行建的时候就是
        completed。
        """
        from src.services.terrain_tiling.vrt_builder import list_dem_tifs

        name = (name or "等高线瓦片").strip() or "等高线瓦片"
        dem_task_id = int(dem_task_id)

        conn = get_connection()
        try:
            dem_row = conn.execute(
                "SELECT status, output_path FROM dem_tasks WHERE id=?", (dem_task_id,)).fetchone()
        finally:
            conn.close()
        if not dem_row:
            raise ValueError(f"DEM task {dem_task_id} not found")
        # 与 dem_task_manager.start_tiling 同一道闸门：没下完的任务数据残缺，
        # 在它上面渲染会"成功"产出带缺口的等高线瓦片。
        if dem_row["status"] != "completed":
            raise ValueError(
                f"Cannot use DEM task {dem_task_id} with status "
                f"'{dem_row['status']}'; wait for the download to complete"
            )

        source_dir = resolve_stored_output_dir(dem_row["output_path"]) / f"dem_task_{dem_task_id}"
        tifs = list_dem_tifs(source_dir)
        if not tifs:
            raise ValueError(f"No DEM tifs found under {source_dir}")

        render = self._normalize_render_params(
            contour_interval, zoom_min, zoom_max, background, terrain_shade,
            line_color_intermediate, line_color_index, tint_breaks, tint_colors)
        zoom_min = render["zoom_min"]
        zoom_max = render["zoom_max"]

        # bbox 只用于历史记录地图展示，读不出保持 0（同上传路径）。
        extent = _union_tif_extent_lonlat(tifs)
        north, south, east, west = extent if extent else (0.0, 0.0, 0.0, 0.0)
        if render["auto_zoom_max"]:
            # 最高层级按 DEM 原始分辨率自动计算；读不出分辨率时用兜底默认值
            px = _finest_pixel_size_3857(tifs)
            zoom_max = estimate_max_zoom(px, zoom_min) if px else max(zoom_min, _FALLBACK_MAX_ZOOM)

        output_path = str(Path(Config.DOWNLOADS_DIR) / "dem")
        Path(output_path).mkdir(parents=True, exist_ok=True)

        task_dir: Optional[Path] = None
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO contour_tasks (
                    name, status, north, south, east, west, dataset,
                    contour_interval, background, terrain_shade, water,
                    zoom_min, zoom_max, output_path,
                    line_color_intermediate, line_color_index, tint_breaks, tint_colors,
                    source_dem_task_id,
                    total_files, downloaded_files, failed_files,
                    total_tiles, rendered_tiles, failed_tiles
                )
                VALUES (?, 'pending', ?, ?, ?, ?, 'dem_task', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (name, north, south, east, west, render["interval"], render["background"],
                 render["shade"], zoom_min, zoom_max, output_path, render["line_mid"],
                 render["line_idx"], render["tint_breaks"], render["tint_colors"],
                 dem_task_id, len(tifs), len(tifs)),
            )
            task_id = cur.lastrowid

            # 只建产物目录（contour_tiles 的落点）；源 tif 不拷进来。
            task_dir = Path(output_path) / f"contour_task_{task_id}"
            task_dir.mkdir(parents=True, exist_ok=True)
            for tif in tifs:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO contour_files (task_id, granule_id, kind, status, local_path, size_bytes, retry_count)
                    VALUES (?, ?, 'dem', 'completed', ?, ?, 0)
                    """,
                    (task_id, tif.name, str(tif), tif.stat().st_size),
                )
            conn.commit()
            return task_id
        except Exception:
            conn.rollback()
            # rowid 复用后残留目录会被下一个同 id 任务当成自己的产物目录，清掉。
            # 只清本任务目录 —— 源 DEM 目录是别人的数据，绝不能动。
            if task_dir is not None:
                shutil.rmtree(task_dir, ignore_errors=True)
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

    def delete_task(self, task_id: int, artifact_dir=None, on_row_gone=None):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        砍掉「取消」之后这是唯一的销毁动作，任何状态都能调 —— 不再有运行中拒删。

        on_row_gone 由调用方给：清 /contour 静态路由缓存的那个 hook 依赖 Flask
        请求上下文（走 current_app.extensions），放在这里等于让服务层持有一个
        只对路由调用方有效的回调，非路由调用方那里它会静默失效。
        """
        from src.services.task_deletion import delete_task_row

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="contour_tasks",
            artifact_dir=artifact_dir,
            on_row_gone=on_row_gone,
        )

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
        failure = None
        try:
            asyncio.run(self._execute(task_id, stop_flag))
        except Exception as e:
            logger.error(f"Contour task {task_id} thread failed: {e}")
            failure = e
        finally:
            with self._state_lock:
                deregistered = (
                    self.active_tasks.get(task_id) is threading.current_thread())
                if deregistered:
                    self.active_tasks.pop(task_id, None)
                if stop_flag is None or self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            if deregistered:
                # 行还停在 running 就是搁死了（理由与竞态分析见 helper 的 docstring）。
                fail_stranded_running_task(
                    'contour_tasks', task_id,
                    f'线程异常: {failure}' if failure is not None else '')

    async def _execute(self, task_id: int, stop_flag: Optional[threading.Event] = None) -> None:
        conn = get_connection()
        try:
            cur = conn.cursor()
            task = cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError(f"Contour task {task_id} not found")

            dataset = task["dataset"]
            # 只有本地源可执行：'upload'（文件已落在任务目录里）与 'dem_task'
            # （零拷贝引用某个已完成 DEM 下载任务的目录）。下载驱动的那半程连同
            # 它的 create_task 一起删掉了 —— 两个构造器只会写这两个值，那段从
            # dem_task_manager 拷来的 ~110 行没有任何用户可达路径能执行到，于是
            # 一直在无声漂移（emit 节流的 counts 豁免、per-callback SELECT *、
            # 终态 emit 未包 try 三处都已与 dem 侧不一致），注释却仍声称对齐。
            # 存量下载行在这里直接判失败并说清原因，而不是靠一份跑不起来的拷贝
            # 假装还支持。
            if dataset not in ("upload", "dem_task"):
                raise ValueError(
                    f"数据源 '{dataset}' 是已移除的下载驱动类型，本任务无法执行；"
                    f"请先在「数据下载」里下好 DEM，再用该 DEM 任务新建等高线任务")
            # output_dir 语义不变：本任务自己的目录 —— 产物落点，上传源也在这里。
            # 存储的 output_path 一律走 resolve_stored_output_dir（全仓唯一一套
            # 口径，/contour 静态路由与删除路径用的也是它）：frozen exe 被搬动后
            # BASE_DIR 会变，直接 Path(存储值) 会把瓦片写到旧的绝对路径下，而
            # 路由按新根去取 —— 瓦片明明在盘上却永久 404。
            output_dir = resolve_stored_output_dir(task["output_path"]) / f"contour_task_{task_id}"
            # source_dir 是渲染读源 DEM 的目录。dem_task 来源每次执行都重新解析
            # （downloads 根目录可能被改过）；源任务行没了、或目录里已经没有 tif
            # 时必须抛 —— 否则渲染会在空输入上静默产出 0 张瓦片。
            source_dir = output_dir
            source_dem_task_id = task["source_dem_task_id"]
            if source_dem_task_id is not None:
                from src.services.terrain_tiling.vrt_builder import list_dem_tifs

                source_dem_task_id = int(source_dem_task_id)
                dem_row = cur.execute(
                    "SELECT output_path FROM dem_tasks WHERE id=?", (source_dem_task_id,)).fetchone()
                if not dem_row:
                    raise ValueError(f"Source DEM task {source_dem_task_id} not found")
                source_dir = (resolve_stored_output_dir(dem_row["output_path"])
                              / f"dem_task_{source_dem_task_id}")
                if not list_dem_tifs(source_dir):
                    raise ValueError(f"No DEM tifs found under {source_dir}")
            want_water = bool(task["water"])

            # 本地源没有下载阶段（文件行建的时候就是 completed），但用户可能在
            # 进入渲染之前就按了暂停/删除。
            if stop_flag and stop_flag.is_set():
                return

            current = cur.execute("SELECT status FROM contour_tasks WHERE id=?", (task_id,)).fetchone()
            # 只剩 "paused" 要挡：用户明确按了暂停，收尾不得改写它。行不在了
            # （被删）同样直接退出。
            if not current or current["status"] == "paused":
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

            # ---- Render phase: local DEM -> contour tiles ----
            from src.services.contour_task_tiler import ContourParams, tile_contour_task_dir

            style = style_for_task(self.config, task)
            interval = float(task["contour_interval"])
            zoom_min = int(task["zoom_min"]); zoom_max = int(task["zoom_max"])
            # 覆盖范围就是 DEM 文件本身的范围（不是 1° granule 并集），预计算
            # 没有意义 —— total 由渲染引擎 warp 完按实际覆盖上报。
            total_tiles = 0

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

                # 行还在吗。删除运行中的任务会把行 DELETE 掉，而 base_payload 是
                # 【渲染开始前】的整行快照,里面 status='running' —— 行没了还继续
                # emit,前端那边 key 既不在时间流也不在活动集(deleteTask 刚摘干净),
                # 于是走 static/js/tasks.js 的 prependStreamRow 把行插回来;而
                # 停止后本方法直接 return、再不发任何终态事件,那行就永久卡在
                # 「运行中」,只能刷新页面才消失。
                #
                # 判据只能是「行还在吗」,不能是 stop_flag.is_set():暂停同样置停止
                # 标志,但暂停时行还在、那一发收尾 flush 是对的(保住节流窗口内最后
                # 一段计数)。拿 stop_flag 拦会把暂停一起误伤。
                # 一旦确认行没了就记住 —— 删除不可逆,不必反复回查。
                row_alive = {"ok": True}

                def _stage_row_alive() -> bool:
                    """prepare 阶段的闸门。它没有 DB 写、拿不到 rowcount,只能自己查。

                    时序:进入渲染阶段那一发 render_progress(0, total) 在 tiler 之前
                    就跑过,所以 warp 期间 row_alive 里已经有一次 rowcount 结论 ——
                    但删除可以发生在那之后、warp 期间,那个结论会过期。所以只信任
                    「已确认行没了」这一侧短路,仍认为活着时必须再查一次。
                    每次 stage emit 一条按主键的 SELECT,复用同一连接:stage emit 本身
                    已被 _RENDER_PROGRESS_MIN_INTERVAL 节流,频率与 flush 同量级。
                    """
                    if not row_alive["ok"]:
                        return False
                    if progress_conn.execute(
                            "SELECT 1 FROM contour_tasks WHERE id=?",
                            (task_id,)).fetchone() is None:
                        row_alive["ok"] = False
                    return row_alive["ok"]

                def _flush_render_progress():
                    cur_flush = progress_conn.execute(
                        "UPDATE contour_tasks SET rendered_tiles=?, total_tiles=? WHERE id=?",
                        (render_state["done"], render_state["total"], task_id))
                    progress_conn.commit()
                    render_state["last_flush"] = time.monotonic()
                    # rowcount=0 → 行已被删除:这次 UPDATE 本来就是空转,emit 才是
                    # 有害的那一半。「写完看 rowcount 再决定发不发」是本仓既有约定
                    # (见 CLAUDE.md 的删除约定、task_deletion.delete_task_row)。
                    if cur_flush.rowcount == 0:
                        row_alive["ok"] = False
                    if not row_alive["ok"]:
                        return
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
                # 渲染开始之前的准备阶段（warp 到 3857、建金字塔）。它跑在 total
                # 算出来之前 —— 上传任务的 total_tiles 建任务时写死为 0，于是界面
                # 在整个 warp 期间显示「0 / 0 瓦片 · 0%」不动，看起来像卡死。
                #
                # ⚠️ phase 必须用新值，不能复用 'render'：多个测试按
                # `p.get('phase') == 'render'` 过滤事件并数个数，混进来会打挂它们。
                stage_state = {"last_emit": float("-inf")}
                _STAGE_LABELS = {"warp": "预处理 DEM", "overview": "建金字塔"}

                def render_stage(phase: str, fraction: float) -> None:
                    now = time.monotonic()
                    edge = fraction <= 0.0 or fraction >= 1.0
                    if not edge and \
                            now - stage_state["last_emit"] < _RENDER_PROGRESS_MIN_INTERVAL:
                        return
                    stage_state["last_emit"] = now
                    if not (self.socketio and base_payload is not None):
                        return
                    if not _stage_row_alive():
                        return
                    payload = dict(base_payload)
                    payload["task_type"] = "contour"
                    payload["phase"] = "prepare"
                    payload["stage"] = phase
                    payload["stage_label"] = _STAGE_LABELS.get(phase, phase)
                    payload["stage_fraction"] = max(0.0, min(1.0, float(fraction)))
                    # U1：这发 emit 被 GDAL 的进度回调同步调用，抛出会一路穿透；
                    # 而 GDAL 把回调抛异常当成「用户请求中止」，warp 会直接失败。
                    try:
                        self.socketio.emit("task_progress", payload)
                    except Exception as emit_error:
                        logger.warning(
                            f"Contour task {task_id}: emit prepare stage failed "
                            f"(ignored): {emit_error}")

                render_counts = tile_contour_task_dir(
                    task_dir=source_dir, out_dir=output_dir / "contour_tiles",
                    params=params, progress_cb=render_progress,
                    stage_cb=render_stage, stop_flag=stop_flag,
                )
                # 渲染结束(正常完成/暂停/已删除/部分失败)强制 flush:节流窗口内
                # 最后一段计数不丢。已删除时 UPDATE 是空转、emit 被 rowcount 闸掉,
                # 走到这里不必分支。渲染异常由外层 except 标 failed,无需再 flush。
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
                # 'completed' 也要排除:上面的 emit("task_completed") 抛异常时会
                # 走到这里,不能把已经完成的任务改判 failed。'paused' 排除的理由
                # 不同 —— 它是用户的明确意图,失败兜底不该把它抢走。
                cur.execute("UPDATE contour_tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('paused','completed')",
                            (str(e), utc_now_iso(), task_id))
                conn.commit()
                if cur.rowcount and self.socketio:
                    self.socketio.emit("task_failed", {"task_id": task_id, "task_type": "contour", "status": "failed", "error_message": str(e)})
            except Exception as e2:
                logger.error(f"Failed to mark contour task {task_id} failed: {e2}")
            raise
        finally:
            conn.close()
