"""
Static terrain serving routes

Serve CesiumJS terrain resources (layer.json + .terrain files) from local downloads.
"""

import logging
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, send_file

from core.config import Config
from core.database import get_connection
from services.config_manager import ConfigManager
from services.geo_validation import resolve_output_dir
from services.hillshade_preview import ensure_hillshade
from services.task_cleanup import resolve_stored_output_dir

logger = logging.getLogger(__name__)

terrain_static_bp = Blueprint("terrain_static", __name__, url_prefix="/terrain")

_GZIP_MAGIC = b"\x1f\x8b"


def _send_terrain_file(target: Path):
    """send_file wrapper for terrain resources.

    .terrain tiles are stored gzip-compressed on disk (see cesiumlab_terrain.py).
    Detect the gzip magic bytes and advertise Content-Encoding: gzip so browsers
    decompress transparently; plain files (layer.json) are served untouched.
    """
    # 单 open：自己读出魔数后把同一文件对象交给 send_file（此前 send_file
    # 内部开一次、探测魔数又开一次，瓦片热路径上每请求两次 open）。
    f = None
    try:
        f = open(target, "rb")
        magic = f.read(2)
        f.seek(0)
    except OSError:
        # 与调用方 target.exists() 检查之间的竞态：文件刚被删掉时按 404 处理
        if f is not None:
            f.close()
        abort(404)
    resp = send_file(f, download_name=target.name)
    if magic == _GZIP_MAGIC:
        resp.headers["Content-Encoding"] = "gzip"
    # task_id 是 AUTOINCREMENT 不复用，同一 URL 的瓦片内容不变，可 immutable
    # 长缓存（同 tiles_static/contour_static）；layer.json 重切片后会变，不加。
    if target.suffix == ".terrain":
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_config_path(path_str: str) -> Path:
    """Resolve a stored/config path to an absolute Path.

    M10: 全项目只保留一套 output_path 解析口径 —— `resolve_stored_output_dir`。
    这里只是它的读侧薄封装（额外做一次 resolve，静态服务要拿真实路径做穿越
    校验）。此前读侧（前缀剥离）与写/删除侧（一律拼到 DOWNLOADS_DIR 下）是
    两套规则，同一个 `'./downloads/map'` 会解析成两个不同目录。
    """
    return resolve_stored_output_dir(path_str).resolve()


def _resolve_safe_file(base_dir: Path, subpath: str) -> Path:
    """把 subpath 限制在 base_dir 之内,防路径穿越。

    0.2.4 起不再要求 base_dir 落在 DOWNLOADS_DIR 内:base_dir 全部来自
    DB 任务行的 output_path(建任务时已校验)或管理员配置,不是请求方
    输入;请求方能控制的只有 subpath,把它锁在 base_dir 里即可。
    """
    base_dir = base_dir.resolve()

    # Guard against backslash-based traversal on Windows.
    subpath = (subpath or "").replace("\\", "/")
    target = (base_dir / subpath).resolve()

    if not _is_within(target, base_dir):
        abort(400, description="path traversal blocked")

    return target


# base 路径配置缓存：/terrain/base 是瓦片热路径，每请求 ConfigManager().get
# 都新开一次 sqlite 连接查一个几乎不变的配置项。缓存挂 app.extensions 而非
# 模块级（测试 fresh-import app 时拿到干净缓存，routes 模块不会被重导入），
# 加短 TTL：配置经 /api/config 改完后最多 TTL 秒生效，无需手动失效钩子。
_CACHE_KEY_BASE_PATH = "terrain_static_base_path"
_BASE_PATH_TTL_SECONDS = 5.0


def _base_path_cached() -> str:
    now = time.monotonic()
    entry = current_app.extensions.get(_CACHE_KEY_BASE_PATH)
    if entry is not None and now - entry[0] < _BASE_PATH_TTL_SECONDS:
        return entry[1]
    value = ConfigManager().get("terrain_global_base_path", "./downloads/terrain/base_z8")
    current_app.extensions[_CACHE_KEY_BASE_PATH] = (now, value)
    return value


# local 任务存在性缓存：同 contour_static._known_tasks —— 瓦片请求量大，存在性
# 永真（任务只在删除时消失），没必要每瓦片 SELECT id。只缓存正结果（查不到不
# 缓存，新任务立即可见）；删除任务时路由层必须调 invalidate_known_task，否则
# delete_files=false（磁盘瓦片保留）时已删任务的瓦片仍可访问，与直查 DB 不一致。
_CACHE_KEY_KNOWN_TASKS = "terrain_static_known_local_tasks"


def _known_local_tasks() -> set:
    return current_app.extensions.setdefault(_CACHE_KEY_KNOWN_TASKS, set())


def invalidate_known_task(task_id: int) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的存在性缓存项。"""
    _known_local_tasks().discard(task_id)


def _local_task_exists(task_id: int) -> bool:
    known = _known_local_tasks()
    if task_id in known:
        return True
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM local_terrain_tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    known.add(task_id)
    return True


@terrain_static_bp.route("/base/<path:subpath>", methods=["GET"])
def terrain_base_static(subpath: str):
    base_dir = _resolve_config_path(_base_path_cached())

    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return _send_terrain_file(target)


# dem 任务 output_path 缓存：地形瓦片请求是热路径，output_path 创建后永不变，
# 没必要每瓦片查一次。只缓存正结果（查不到不缓存，新任务立即可见）；删除任务时
# 路由层必须调 invalidate_dem_task —— 任务行没了但磁盘切片可能还在
# （delete_files 默认 false），不失效的话已删任务的瓦片还能访问到，与其他三条
# 静态瓦片路由（tiles/contour/local）的行为不一致。
# 缓存挂 app.extensions 而非模块级：测试每次 fresh-import app 都得到干净缓存。
_CACHE_KEY_DEM_OUTPUT_PATH = "terrain_static_dem_output_path"


def _dem_output_path_cache() -> dict:
    return current_app.extensions.setdefault(_CACHE_KEY_DEM_OUTPUT_PATH, {})


def invalidate_dem_task(task_id: int) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的 output_path 缓存项。"""
    _dem_output_path_cache().pop(task_id, None)


def _get_dem_output_path(task_id: int):
    cache = _dem_output_path_cache()
    if task_id in cache:
        return cache[task_id]
    conn = get_connection()
    try:
        row = conn.execute("SELECT output_path FROM dem_tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    cache[task_id] = row["output_path"]
    return row["output_path"]


def _resolve_dem_task_output_dir(output_path: str) -> Path:
    """与切片写入侧 dem_task_manager._resolve_task_output_dir 同口径：

    绝对路径原样；相对路径（存量任务行的历史值）按 DOWNLOADS_DIR 解析。
    读侧必须镜像写侧 —— 此前这里硬编码 DOWNLOADS_DIR/dem，用户自定义保存路径
    （仍在 DOWNLOADS_DIR 内，合法）时切片写在 <output_path>/dem_task_<id>/，
    路由读不到，地形预览 404 退化成「仅定位到区域」。
    """
    p = Path(output_path)
    if p.is_absolute():
        return p
    return Path(resolve_output_dir(output_path))


@terrain_static_bp.route("/dem/<int:task_id>/<path:subpath>", methods=["GET"])
def terrain_dem_static(task_id: int, subpath: str):
    output_path = _get_dem_output_path(task_id)
    if output_path is None:
        abort(404)

    base_dir = _resolve_dem_task_output_dir(output_path) / f"dem_task_{task_id}" / "terrain_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return _send_terrain_file(target)


@terrain_static_bp.route("/local/<int:task_id>/<path:subpath>", methods=["GET"])
def terrain_local_static(task_id: int, subpath: str):
    # Confirm the task exists, but DO NOT trust the absolute output_dir stored at
    # creation time: in frozen/PyInstaller mode DOWNLOADS_DIR is anchored to the
    # executable's directory, so a stored absolute path breaks if the executable
    # is moved. local 任务的产物目录是固定布局（DOWNLOADS_DIR/terrain/local_task_<id>），
    # 直接从当前 DOWNLOADS_DIR 重算即可，serving 因此能扛住 exe 挪目录。
    # （dem 任务产物目录随任务 output_path 走、不是固定布局，只能按任务行解析，
    #   见 terrain_dem_static。）
    if not _local_task_exists(task_id):
        abort(404)

    base_dir = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}" / "terrain_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return _send_terrain_file(target)


# ---------------------------------------------------------------------------
# 无切片任务的源 DEM 晕渲预览（services/hillshade_preview.py）
#
# 路由尾段是静态串（hillshade / hillshade.png），Werkzeug 的静态优先于
# <path:subpath> 通配，不会被上面的瓦片路由吃掉。
# ---------------------------------------------------------------------------


def _hillshade_json(task_dir: Path, raster_dir: Path, png_url: str):
    result = ensure_hillshade(raster_dir, task_dir)
    if result is None:
        abort(404)
    _, bounds = result
    return jsonify({"url": png_url, "bounds": bounds})


def _hillshade_png(task_dir: Path, raster_dir: Path):
    result = ensure_hillshade(raster_dir, task_dir)
    if result is None:
        abort(404)
    png_path, _ = result
    resp = send_file(str(png_path))
    # 同任务的源文件不变则内容不变；但缓存文件可能被手动删掉重建，不用 immutable
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def _dem_task_dir_or_404(task_id: int) -> Path:
    output_path = _get_dem_output_path(task_id)
    if output_path is None:
        abort(404)
    return _resolve_dem_task_output_dir(output_path) / f"dem_task_{task_id}"


def _local_task_dir_or_404(task_id: int) -> Path:
    if not _local_task_exists(task_id):
        abort(404)
    return Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"


@terrain_static_bp.route("/dem/<int:task_id>/hillshade", methods=["GET"])
def terrain_dem_hillshade(task_id: int):
    task_dir = _dem_task_dir_or_404(task_id)
    return _hillshade_json(task_dir, task_dir, f"/terrain/dem/{task_id}/hillshade.png")


@terrain_static_bp.route("/dem/<int:task_id>/hillshade.png", methods=["GET"])
def terrain_dem_hillshade_png(task_id: int):
    task_dir = _dem_task_dir_or_404(task_id)
    return _hillshade_png(task_dir, task_dir)


@terrain_static_bp.route("/local/<int:task_id>/hillshade", methods=["GET"])
def terrain_local_hillshade(task_id: int):
    task_dir = _local_task_dir_or_404(task_id)
    return _hillshade_json(task_dir, task_dir / "source", f"/terrain/local/{task_id}/hillshade.png")


@terrain_static_bp.route("/local/<int:task_id>/hillshade.png", methods=["GET"])
def terrain_local_hillshade_png(task_id: int):
    task_dir = _local_task_dir_or_404(task_id)
    return _hillshade_png(task_dir, task_dir / "source")

