"""把已有的瓦片目录打包成 MBTiles。**影像与等高线共用这一条写入端。**

§10 阶段 2 的门槛写得很死：「同一套写入端能产出影像库与等高线库」。所以打包
逻辑不能挂在 `TaskManager` 上 —— 那样等高线要么没有，要么就是第二份。它住在
这里，四条管线（今天是两条）通过 `pipeline` 参数复用同一个函数、同一个
`MBTilesWriter`、同一套 metadata 规则。

## 为什么 MBTiles 是「追加」而不是「第四种 output_format」

§5.3 把它定为**通用产物容器**，`Artifact` 要能表达「同一任务的第 N 种产物」。
把它做成 `output_format` 的一个取值会有一个很难看的后果：那个值下不再产出
松散 XYZ 目录，而 MBTiles 恰恰是**从那个目录打包**来的，`/tiles/<id>/` 预览
路由也从那里取文件 —— 为了拿容器把原料和预览一起砍掉。

所以：`output_format` 语义不变，另有一个正交的 `tasks.export_mbtiles` 开关，
以及一个对已完成任务随时可用的导出动作。两条路径走同一个函数。

## 一个库只装一种 format

规范没有多图层容器语义（§5.3）。影像、等高线、将来的 MVT 各自成库，
文件名带管线前缀。混装换来的只是不可移植。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from src.contracts.artifact import Artifact, ArtifactKind, PIPELINES
from src.contracts.outcome import TaskState
from src.core.database import get_connection, utc_now_iso
from src.services.geo_validation import sanitize_filename

logger = logging.getLogger(__name__)

__all__ = ['export_task_mbtiles', 'tile_dir_for', 'ExportError']


class ExportError(ValueError):
    """导出失败。继承 ValueError 让路由层现有的 400 映射原样生效。"""


# 每条管线的「松散瓦片目录」在哪、瓦片是什么扩展名、写进 MBTiles 的 format
# 是什么。**这张表是本模块唯一的管线分支**，核心其余部分只认目录不认管线 ——
# §13-4 的插件契约要求「核心不为任何单一数据源开特例」，一张查找表是那条
# 要求允许的形态，散在代码里的 if pipeline == ... 不是。
_PIPELINE_TILE_LAYOUT = {
    # 地图任务：output_path/task_<id>/{z}/{x}/{y}.png（见 routes/tiles_static.py 的
    # 布局契约），拼接出来的 GeoTIFF 与它同级、不进容器。
    'map': ('task_{id}', '', '.png', 'png', 'baselayer'),
    # 等高线任务：output_path/contour_task_<id>/contour_tiles/{z}/{x}/{y}.png
    # （contour_task_manager 里 out_dir 的构造）。等高线是叠加层不是底图。
    'contour': ('contour_task_{id}', 'contour_tiles', '.png', 'png', 'overlay'),
}


def _table_for(pipeline: str) -> str:
    return {'map': 'tasks', 'contour': 'contour_tasks'}[pipeline]


def _row_get(row, key, default=None):
    """任务行取列，缺列/缺键一律给默认值。

    四张任务表的列不是同一套（`gap_tiles` 只在 `tasks` 上），而 `sqlite3.Row`
    对不存在的列名抛 IndexError —— 直接下标就会把「这条管线没有那一列」变成
    一次导出失败。
    """
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _infer_has_gaps(row) -> bool:
    """任务行 → 「这套瓦片有洞吗」。**两个导出入口共用的唯一判据。**

    为什么推断必须住在这里：本函数有两个调用方 —— 跑完自动导出（建任务时勾的
    `tasks.export_mbtiles`）和用户在成果页点的导出按钮。两边各自推断，就是它们
    实际分叉的地方：按钮那条路一个字都不传，`has_gaps` 默认 False，于是
    `completed_with_gaps` 任务导出的 MBTiles 被登记成「无缺块」，紧挨着它的
    `xyz_dir` / `geotiff` 兄弟行写着 True。§13-3 要的是「成果与历史永久带缺块
    标记」，同一任务的产物里有一件说谎，这条要求就作废了。

    判据看**列**不看管线：`tasks.gap_tiles` 是缺块总数（含 no_data）；
    `contour_tasks` 没有那一列，它的等价事实是 `failed_tiles`（渲染失败 = 成品
    上的洞）。map 表两列都有且 gap_tiles ⊇ failed_tiles，取或不改变结论。
    状态口径复用 `TaskState.has_gaps`，不在这里抄字面量 —— `pending_decision`
    同样带洞（用户还没决定补不补），只认 `completed_with_gaps` 会漏掉它。
    """
    status = str(_row_get(row, 'status', '') or '')
    try:
        if TaskState(status).has_gaps:
            return True
    except ValueError:
        # 未知状态字面量（存量行、手工改库）不作数，继续看计数列。
        pass
    return bool(_row_get(row, 'gap_tiles', 0)) or bool(_row_get(row, 'failed_tiles', 0))


def _fetch_row(pipeline: str, task_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            f'SELECT * FROM {_table_for(pipeline)} WHERE id = ?', (task_id,)).fetchone()
    finally:
        conn.close()


def tile_dir_for(pipeline: str, task_id: int, output_path: str) -> Path:
    """管线 + 任务 + 存储的 output_path → 松散瓦片金字塔目录。

    `output_path` 一律先过 `task_cleanup.resolve_stored_output_dir` —— 那是全项目
    唯一的「存的路径怎么读回来」规则（`geo_validation.resolve_output_dir` 是写入
    侧的校验器，读取侧用它会把落在 DOWNLOADS_DIR 之外的合法产物判成越界）。
    """
    from src.services.task_cleanup import resolve_stored_output_dir
    if pipeline not in _PIPELINE_TILE_LAYOUT:
        raise ExportError(f'pipeline {pipeline!r} has no tile pyramid to package')
    task_dir_tpl, sub, _ext, _fmt, _type = _PIPELINE_TILE_LAYOUT[pipeline]
    root = Path(resolve_stored_output_dir(output_path)) / task_dir_tpl.format(id=task_id)
    return root / sub if sub else root


def export_task_mbtiles(pipeline: str, task_id: int, *,
                        has_gaps: Optional[bool] = None,
                        attribution: str = '') -> Dict[str, object]:
    """把一个任务的瓦片目录打包成 MBTiles，登记 Artifact，返回摘要。

    Args:
        has_gaps: 缺不缺块。**默认 None = 「调用方没说」**，此时按任务行推断
            （见 `_infer_has_gaps`）。显式传 False 是**覆盖**，意思是「调用方
            断言这套瓦片是完整的」—— 之所以不能把 False 当默认值，是因为那样
            「没说」和「断言没洞」就分不开了，而「没说」的正确动作是去查事实，
            不是登记一个乐观的谎。

    Raises:
        ExportError: 管线不支持、任务不存在、目录不存在或一块瓦片都没有。
        **校验问题不抛** —— 见下面 `validate_mbtiles` 那一段。

    幂等：同一任务重复导出会覆盖同一个文件（`MBTilesWriter` 走
    `.part.<pid>` + `os.replace`），Artifact 行按唯一键 REPLACE。
    """
    if pipeline not in PIPELINES:
        raise ExportError(f'unknown pipeline {pipeline!r}')
    if pipeline not in _PIPELINE_TILE_LAYOUT:
        raise ExportError(
            f'{pipeline} tasks do not produce a tile pyramid; MBTiles export applies '
            f'to {sorted(_PIPELINE_TILE_LAYOUT)}')

    row = _fetch_row(pipeline, task_id)
    if row is None:
        raise ExportError(f'task {task_id} not found')

    _tpl, _sub, extension, fmt, layer_type = _PIPELINE_TILE_LAYOUT[pipeline]
    tile_root = tile_dir_for(pipeline, task_id, _row_get(row, 'output_path', '') or '')
    if not tile_root.is_dir():
        raise ExportError(f'no tile directory to package at {tile_root}')

    if has_gaps is None:
        has_gaps = _infer_has_gaps(row)

    name = sanitize_filename(_row_get(row, 'name', '') or f'{pipeline}_task_{task_id}')
    out_path = tile_root.parent / f'{name}.mbtiles'

    if not attribution:
        # 影像库带上源署名；等高线是本地渲染产物，没有第三方署名可带。
        if pipeline == 'map':
            try:
                from src.services.source_registry import snapshot_for_task_row
                attribution = snapshot_for_task_row(row).attribution or ''
            except Exception:
                attribution = ''

    from src.services.mbtiles import MBTilesWriter, MBTilesError, validate_mbtiles
    try:
        with MBTilesWriter(out_path, fmt=fmt, name=name, attribution=attribution,
                           layer_type=layer_type) as writer:
            added = writer.add_dir(tile_root, extension=extension)
            if not added:
                raise ExportError(f'tile directory {tile_root} contains no {extension} tiles')
            info = writer.finalize()
    except MBTilesError as e:
        raise ExportError(str(e)) from e

    # §10 阶段 2 的门槛是「容器**可自动校验**」。校验器只在有人手动调用时才跑，
    # 那半个门槛就等于没实现：出厂的库到底合不合规，全仓没有任何地方留下过判决。
    # 所以在写入端收尾处强制跑一遍，判决进 `Artifact.meta` —— 产物索引与界面
    # 从此读到的是「校验过、结论是什么」，而不是「未知」。
    # **不抛**：文件已经落盘且绝大多数问题（metadata 缺一个可选键、声明的
    # bounds 与实际瓦片外包框差一点）不影响它能用。把已经产出的成品因为一份
    # 体检报告删掉/报错，比记下问题有害得多。ok=false 时点名问题写 warning，
    # 用户在日志与产物详情两处都能看到。
    validation = validate_mbtiles(out_path)
    if not validation.get('ok'):
        logger.warning(
            f'{pipeline} 任务 {task_id} 导出的 MBTiles 未通过校验：{out_path} '
            f'—— {"；".join(str(p) for p in validation.get("problems") or ["未知问题"])}')

    from src.services.artifact_store import record_artifact
    record_artifact(Artifact(
        pipeline=pipeline, task_id=task_id, kind=ArtifactKind.MBTILES,
        path=str(out_path), fmt=fmt, bytes_total=int(info.get('bytes') or 0),
        tile_count=int(info.get('tile_count') or 0),
        minzoom=info.get('minzoom'), maxzoom=info.get('maxzoom'),
        has_gaps=bool(has_gaps),
        meta={'bounds': info.get('bounds'), 'source_dir': str(tile_root),
              'validation': validation},
        created_at=utc_now_iso(),
    ))
    logger.info(f'{pipeline} 任务 {task_id} 已导出 MBTiles：{out_path} '
                f'（{info.get("tile_count")} 块，has_gaps={bool(has_gaps)}，'
                f'校验 {"通过" if validation.get("ok") else "有问题"}）')
    result = dict(info)
    result['has_gaps'] = bool(has_gaps)
    result['validation'] = validation
    result['pipeline'] = pipeline
    result['task_id'] = task_id
    return result
