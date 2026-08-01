"""
Static terrain serving routes

Serve CesiumJS terrain resources (layer.json + .terrain files) from local downloads.
"""

import logging
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, send_file

from core.config import Config
from core.database import get_connection
from services.config_manager import ConfigManager

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
    """
    Resolve config path to an absolute Path.

    Rule:
      - Absolute paths stay absolute.
      - Relative paths starting with ./downloads (or downloads) are rebased onto Config.DOWNLOADS_DIR.
      - Other relative paths are resolved against Config.BASE_DIR.
    """
    p = Path((path_str or "").strip())
    if p.is_absolute():
        return p.resolve()

    # Normalize common "downloads-relative" forms.
    s = (path_str or "").strip().replace("\\", "/")
    if s.startswith("./downloads/"):
        return (Path(Config.DOWNLOADS_DIR) / s[len("./downloads/"):]).resolve()
    if s == "./downloads":
        return Path(Config.DOWNLOADS_DIR).resolve()
    if s.startswith("downloads/"):
        return (Path(Config.DOWNLOADS_DIR) / s[len("downloads/"):]).resolve()
    if s == "downloads":
        return Path(Config.DOWNLOADS_DIR).resolve()

    return (Path(Config.BASE_DIR) / p).resolve()


def _resolve_safe_file(base_dir: Path, subpath: str) -> Path:
    downloads_root = Path(Config.DOWNLOADS_DIR).resolve()

    base_dir = base_dir.resolve()
    if not _is_within(base_dir, downloads_root):
        abort(400, description="base_dir must be under downloads")

    # Guard against backslash-based traversal on Windows.
    subpath = (subpath or "").replace("\\", "/")
    target = (base_dir / subpath).resolve()

    if not _is_within(target, base_dir):
        abort(400, description="path traversal blocked")
    if not _is_within(target, downloads_root):
        abort(400, description="file must be under downloads")

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


@terrain_static_bp.route("/dem/<task_id>/<path:subpath>", methods=["GET"])
def terrain_dem_static(task_id: str, subpath: str):
    base_dir = Path(Config.DOWNLOADS_DIR) / "dem" / f"dem_task_{task_id}" / "terrain_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return _send_terrain_file(target)


@terrain_static_bp.route("/local/<int:task_id>/<path:subpath>", methods=["GET"])
def terrain_local_static(task_id: int, subpath: str):
    # Confirm the task exists, but DO NOT trust the absolute output_dir stored at
    # creation time: in frozen/PyInstaller mode DOWNLOADS_DIR is anchored to the
    # executable's directory, so a stored absolute path breaks if the executable
    # is moved. Recompute the path from the current DOWNLOADS_DIR (same approach
    # as terrain_dem_static) so serving survives relocation.
    if not _local_task_exists(task_id):
        abort(404)

    base_dir = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}" / "terrain_tiles"
    target = _resolve_safe_file(base_dir, subpath)
    if not target.exists() or target.is_dir():
        abort(404)
    return _send_terrain_file(target)

