"""MBTiles 容器里的瓦片服务 —— **一条**路由通吃四条管线、三种数据类型。

## 为什么只有一条路由

§5.3 明确禁止「一种数据类型一条路由」：影像、等高线、未来的矢量瓦片（MVT）
在容器层面是同一件事 —— 一张 `tiles(zoom_level, tile_column, tile_row,
tile_data)` 表，差别只有 `metadata.format`。真按数据类型开三条路由，就会有三
份 XYZ→TMS 翻转、三份产物解析、三份 404 判定，而这三份迟早各自漂移（松散瓦片
那边的四个静态蓝图已经是这个下场：目录命名规则在路由、管理器、清理三处各抄了
一遍）。

所以 URL 里带的是 **pipeline + task_id**，不是数据类型：

    GET /mbtiles/<pipeline>/<task_id>/<z>/<x>/<y>.<ext>

`<ext>` **不参与选择产物** —— 一个 (pipeline, task_id) 只有一件 MBTiles 产物，
Content-Type 一律取自登记在 `artifacts` 表里的 `format`。让扩展名参与选择就等
于又造了一遍「按数据类型分路」。

但它也**不是被忽略的装饰**：与库的 format 不符时直接 404。以前忽略它的后果是
`.../1.pbf` 拿到一张 PNG、Content-Type 还写着 image/png —— 一个自相矛盾的响应，
并且带着 `immutable` 被缓存一年。这条路由不转码（库里只有一种 format，§5.3 没有
多图层容器语义），所以「说不出口的就别说」：拒掉，而不是谎报。

## 坐标口径

URL 里是 **XYZ**（y 自北向南），与另外四个静态蓝图、与前端 MapLibre/Leaflet
一致。MBTiles 规范内部存的是 TMS（y 自南向北），翻转由
`services/mbtiles.read_tile` 独家负责 —— 全仓库只有那一处做这个减法，这里绝不
自己再翻一次（翻两次等于没翻，表现是「地图上下颠倒但每块瓦片本身是对的」，
极难一眼看出来）。

## 与松散瓦片路由的关系

`/tiles`、`/contour`、`/terrain` 服务的是磁盘上摊开的瓦片目录；本路由服务的是
**同一份成果打包成单文件之后**的形态（`POST /api/tasks/<id>/export`）。两者可以
同时存在，谁都不是谁的替代 —— 导出 MBTiles 不删 XYZ 目录。
"""

import logging
from pathlib import Path
from typing import Optional

from flask import Blueprint, Response, abort, current_app

from src.contracts.artifact import PIPELINES, ArtifactKind
from src.services import artifact_store
from src.services.mbtiles import MBTilesError, read_tile

logger = logging.getLogger(__name__)

mbtiles_static_bp = Blueprint("mbtiles_static", __name__, url_prefix="/mbtiles")

# (pipeline, task_id) -> (库文件路径, Content-Type) 缓存。理由与
# tiles_static/contour_static/terrain_static 完全一致：浏览一次地图就是几百上千
# 次瓦片请求，而「这个任务的 MBTiles 在哪、是什么格式」在产物登记之后不再变。
# 每瓦片查一次 artifacts 表 = 每瓦片新开一个 sqlite 连接，纯浪费。
# 只缓存**正结果**：查不到不缓存，所以刚导出的任务立即可见，不存在的任务每次
# 都落库返 404（与直查 DB 行为一致）。删除任务时路由层必须调
# invalidate_known_task —— 任务行没了但 .mbtiles 文件可能还在（delete_files
# 默认 false），不失效的话已删任务的瓦片仍能被访问到。
# 缓存挂 app.extensions 而非模块级：测试 fresh-import app 时拿到干净缓存，
# 避免跨用例串库（生产单 app 语义相同）。
_CACHE_KEY = "mbtiles_static_known_tasks"

# format → Content-Type。MBTiles 1.3 规范只承认这四种（见
# services/mbtiles.SUPPORTED_FORMATS），这里不做别名归一 —— 登记进 artifacts
# 表的值已经被 MBTilesWriter 归一过了。认不出来的一律 octet-stream：宁可让
# 浏览器下载下来，也不要谎报一个会被解码失败的类型。
_CONTENT_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'webp': 'image/webp',
    # pbf 按规范是 gzip 过的 protobuf。不加 Content-Encoding: gzip ——
    # MapLibre 自己认这个 MIME 并解压，而声明了 gzip 又给未压缩的 pbf
    # （规范允许不压缩）会让浏览器在传输层就解码失败。
    'pbf': 'application/vnd.mapbox-vector-tile',
}
_DEFAULT_CONTENT_TYPE = 'application/octet-stream'

# URL 里的 `.<ext>` → 它声明的 format。别名只在这里归一（登记进 artifacts 表的
# 值已经被 MBTilesWriter 归一过了，`jpeg` 那种写法只会出现在**请求**里）。
_EXT_ALIASES = {'jpeg': 'jpg', 'mvt': 'pbf', 'vector.pbf': 'pbf'}


def _known_tasks() -> dict:
    return current_app.extensions.setdefault(_CACHE_KEY, {})


def invalidate_known_task(task_id: int, pipeline: Optional[str] = None) -> None:
    """任务删除时由路由层调用（请求上下文内），清掉该任务的缓存项。

    `pipeline` 缺省时清掉四条管线下同 id 的全部条目：调用点是四条 DELETE 路由，
    每条都只知道自己那条管线，但让每条路由去记「我是哪个 pipeline 字符串」等于
    把 PIPELINES 这张表又抄一遍到四个地方。同 id 跨管线的多余失效只是让下一次
    请求多查一次库。
    """
    known = _known_tasks()
    if pipeline is not None:
        known.pop((pipeline, task_id), None)
        return
    for name in PIPELINES:
        known.pop((name, task_id), None)


def _resolve(pipeline: str, task_id: int):
    """(库文件路径, Content-Type, format)；该任务没有 MBTiles 产物时返回 None。"""
    known = _known_tasks()
    cached = known.get((pipeline, task_id))
    if cached is not None:
        return cached

    # 同一任务可以有多件产物（XYZ 目录 + 每层 GeoTIFF + MBTiles，见
    # contracts/artifact 的模块 docstring），按 kind 过滤而不是取第一件。
    # 重复导出走 INSERT OR REPLACE 的唯一键 (pipeline, task_id, kind, path)，
    # 路径变了就会有第二行 —— 取**最后**一行（list_artifacts 按 id 升序），
    # 那是最近一次导出的位置。
    artifacts = [a for a in artifact_store.list_artifacts(pipeline, task_id)
                 if a.kind is ArtifactKind.MBTILES]
    if not artifacts:
        return None
    artifact = artifacts[-1]

    path = Path(artifact.path)
    if not path.is_file():
        # 产物行比任务行活得久，也可能比**文件**活得久：用户在文件管理器里删了
        # 库，或者产物落在一块没挂载的外接盘上。这时不缓存 —— 盘一挂回来就该
        # 立刻可用，不必等重启。
        return None

    resolved = (path, _CONTENT_TYPES.get(artifact.fmt, _DEFAULT_CONTENT_TYPE),
                str(artifact.fmt or '').strip().lower())
    known[(pipeline, task_id)] = resolved
    return resolved


@mbtiles_static_bp.route(
    "/<pipeline>/<int:task_id>/<int:z>/<int:x>/<int:y>.<ext>", methods=["GET"])
def mbtiles_tile(pipeline: str, task_id: int, z: int, x: int, y: int, ext: str):
    # pipeline 会进 SQL 的参数位（不拼串），但它同时是缓存键的一半 —— 不校验
    # 的话任意字符串都能在 app.extensions 里占一个条目，等于给了一条无界内存
    # 增长的路径。白名单来自 contracts.artifact.PIPELINES，不在这里抄第二份。
    if pipeline not in PIPELINES:
        abort(404)

    resolved = _resolve(pipeline, task_id)
    if resolved is None:
        abort(404)
    path, content_type, fmt = resolved

    # `ext` 以前被完全忽略：`/mbtiles/map/7/3/2/1.pbf` 会拿到一张 PNG，
    # Content-Type 却是 image/png —— 一个自相矛盾的响应，而且它会被
    # Cache-Control: immutable 缓存一年。
    # **选择拒绝而不是「照 ext 转」**：库里只有一种 format（§5.3 没有多图层容器
    # 语义），转码不在这条路由的职责范围内，而谎报格式是会被永久缓存的错误。
    # 归一只做请求侧的别名（.jpeg → jpg）：artifacts 表里的值早被写入端归一过。
    if _EXT_ALIASES.get(ext.lower(), ext.lower()) != fmt:
        abort(404)

    try:
        data = read_tile(path, z, x, y)
    except MBTilesError as e:
        # read_tile 只在**打不开或读不出来**时抛（库损坏、被独占、中途被删）——
        # 坐标越界它自己返回 None（见那里的 docstring）。所以走到这里就说明
        # 缓存里那条记录已经不可信了，撤掉它：否则文件恢复之后仍会一直报错，
        # 直到进程重启。对用户仍然是 404 —— 「这块瓦片取不到」，而不是 500。
        _known_tasks().pop((pipeline, task_id), None)
        logger.warning(
            f"MBTiles read failed ({pipeline}/{task_id} {z}/{x}/{y}): {e}")
        abort(404)

    if data is None:
        # 稀疏是 MBTiles 的常态，不是异常：缺块的任务
        # （completed_with_gaps）、以及任何区域形状不是矩形的任务，格网四角
        # 本来就没有瓦片。地图控件也会常规性地请求越界坐标。
        abort(404)

    response = Response(data, mimetype=content_type)
    # 与另外四个静态瓦片路由同一条策略：task_id 是 AUTOINCREMENT 不复用，
    # 同一 URL 的内容永不变，可 immutable 长缓存。
    # 注意「永不变」在这里有一处例外：补漏后**重新导出**会覆盖同一个库。那是
    # 用户主动发起的低频动作，且导出后前端拿到的是新的产物列表 —— 让它带一次
    # 陈旧缓存，好过让每一块瓦片都回源。
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
