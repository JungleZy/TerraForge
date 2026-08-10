"""
Static terrain serving routes

Serve CesiumJS terrain resources (layer.json + .terrain files) from local downloads.
"""

import json
import logging
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, send_file

from src.core.config import Config
from src.core.database import get_connection
from src.services.config_manager import ConfigManager
from src.services.hillshade_preview import ensure_hillshade
from src.services.terrain_tiling.layer_json import normalize_parent_url
# 读存量 output_path 的【唯一一套口径】。曾经这里另有一份
# _resolve_dem_task_output_dir 走 geo_validation.resolve_output_dir，与 M10 的存量
# 归一（`database.normalize_stored_output_paths`）对相对值的解释不同 —— 见 P1#5。
from src.services.task_cleanup import resolve_stored_output_dir

logger = logging.getLogger(__name__)

terrain_static_bp = Blueprint("terrain_static", __name__, url_prefix="/terrain")

_GZIP_MAGIC = b"\x1f\x8b"


def _relative_parent_layer_json(target: Path):
    """存量 layer.json 的 parentUrl 按【响应期】归一；不需要改写时返回 None。

    磁盘上已经切好的任务里固化着切片当时的 `terrain_base_parent_url`，旧值是
    `http://localhost:5000/terrain/base`。瓦片现在可能由 5001 专用 origin 提供，
    那个地址会把父级请求绕回主连接池；远程访问时 `localhost` 更是指向客户端
    本机 —— 两种情况都是 404，而 Cesium 对这个 404 不报错：塞一个假的
    heightmap-1.0 图层，并把 heightmapStructure 写在**共享的** builder 上，于是
    任务自己的 quantized-mesh 瓦片也按 heightmap 解析（实测 4154 m 山峰解成
    -744 m，瓦片全 200，控制台干净）。

    只改响应、**不回写磁盘**：切片产物是用户数据，GET 不该改它；目录可能只读，
    也可能被拷到别处，回写既不可靠也没必要。改写口径同样窄：外部地形服务的
    scheme/host/port 不动，只做与写入侧一致的目录规整（见
    layer_json.normalize_parent_url）。

    返回 None = 「这份文件不用改」，由调用方走原来的 send_file 路径 —— 那条路径
    还负责 gzip 魔数探测与缓存头，在这里重复一遍只会漏掉其中一样。
    """
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None                      # 非 JSON / 读不了：保持原行为
    if not isinstance(data, dict):
        return None

    original = data.get("parentUrl")
    if not isinstance(original, str):    # 缺失或类型不对，不碰
        return None
    normalized = normalize_parent_url(original)
    if normalized == original:
        return None
    if normalized is None:
        data.pop("parentUrl", None)
    else:
        data["parentUrl"] = normalized
    return jsonify(data)


def _send_terrain_file(target: Path):
    """send_file wrapper for terrain resources.

    .terrain tiles are stored gzip-compressed on disk (see cesiumlab_terrain.py).
    Detect the gzip magic bytes and advertise Content-Encoding: gzip so browsers
    decompress transparently; plain files (layer.json) are served untouched.

    layer.json 例外：存量文件里的旧 parentUrl 在响应期归一（磁盘不动）。
    """
    if target.name == "layer.json":
        resp = _relative_parent_layer_json(target)
        if resp is not None:
            return resp
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

    0.2.4 起不再要求 base_dir 落在 DOWNLOADS_DIR 内。**但别把「base_dir 可信」
    当成前提** —— `/terrain/base` 的 base_dir 就是配置键
    `terrain_global_base_path`,而 `PUT /api/config` 是未鉴权的:它是用户(在既定
    的「可信环境」部署前提下)可写的值,不是 DB 任务行那种建任务时校验过的路径。

    实际成立的保证只有两条,且分属两层:

    1. 请求方能直接控制的只有 subpath,本函数把它锁死在 base_dir 之内;
    2. base_dir **本身**的可信度由 config_manager 的路径类校验负责 ——
       `terrain_global_base_path` 必须解析到 BASE_DIR/DOWNLOADS_DIR/CACHE_DIR
       之内(见 `_VALUE_RULES`)。

    第 2 条是 2026-08-08 评审补上的:在那之前该键零校验,设成 `/` 之后下面的
    包含检查恒真,`GET /terrain/base/etc/passwd` 直接返回 /etc/passwd。也就是说
    **这个包含检查是一个配置值与整块文件系统之间唯一的东西** —— 删掉它、或者
    放宽那条配置校验,任意文件读立刻复活。
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
    # 兜底值必须与 DEFAULT_CONFIGS 逐字一致：键缺失时用旧的 ./downloads/... 会让
    # 服务指向一个空目录，而解压去的是 assets/ —— 底图判为不可用后走 parentUrl
    # 兜底，那个 URL 又正好指向这里，404 → Cesium 塞假 heightmap 图层污染共享
    # builder（v0.2.8 修过的那条链）。
    value = ConfigManager().get("terrain_global_base_path", "./assets/terrain/base_z8")
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


@terrain_static_bp.route("/dem/<int:task_id>/<path:subpath>", methods=["GET"])
def terrain_dem_static(task_id: int, subpath: str):
    output_path = _get_dem_output_path(task_id)
    if output_path is None:
        abort(404)

    base_dir = (resolve_stored_output_dir(output_path)
                / f"dem_task_{task_id}" / "terrain_tiles")
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
# 无切片任务的源 DEM 晕渲预览（src/services/hillshade_preview.py）
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
    return resolve_stored_output_dir(output_path) / f"dem_task_{task_id}"


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

