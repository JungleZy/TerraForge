"""插件管理与插件任务路由。统一挂在 /api/plugins 前缀下。

一条铁律贯穿本模块：**任何时候都实时查注册表**，绝不在模块级缓存
`PluginRecord` / 管线对象。插件可以在运行期被启停（`/enable`、`/disable`）、
也可以被重扫（`registry.load_all()`），缓存一份就等于让界面显示上一轮的世界。

管理器同理：`get_plugin_task_manager()` 每次现取，不像四条核心管线那样由
`app_factory` 往模块全局里注入 —— 插件任务管理器全进程只有一份，注册表也是，
再复制一个模块级引用只会多一个会陈旧的指针。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from src.plugins import registry
from src.plugins.task_manager import get_plugin_task_manager
from src.services.task_cleanup import (purge_registered_artifacts,
                                       record_retained_output)

logger = logging.getLogger(__name__)

plugins_bp = Blueprint('plugins', __name__, url_prefix='/api/plugins')

#: 查询串里的真值写法，与四条核心管线的 `?delete_files=` 同一套口径。
_TRUTHY = ('1', 'true', 'yes')


def _flag(name: str) -> bool:
    return (request.args.get(name) or '').lower() in _TRUTHY


#: 允许出到浏览器的任务列。**白名单而不是黑名单**：plugin_tasks 将来加列时，
#: 默认是「不外发」而不是「自动泄漏」。少的两列各有理由：
#: - `params_json` —— 见 `_public_params`，凭据就藏在里面。
#: - `region_json` —— 四至已经在 north/south/east/west 里，前端画历史区域用的
#:   是那四个数；整份 RegionSpec（多部件 + 孔洞）没有消费者，给了就是白传。
#:   T10 面板真要回显原始区域时再加，那时它是一次有理由的白名单变更。
_TASK_PUBLIC_COLUMNS = (
    'id', 'plugin_id', 'name', 'status',
    'north', 'south', 'east', 'west', 'zoom_min', 'zoom_max',
    'output_path',
    'total_items', 'downloaded_items', 'failed_items',
    'gap_tiles', 'gap_decision', 'total_running_seconds',
    'created_at', 'started_at', 'completed_at', 'error_message',
)


def _public_params(plugin_id: str, params_json: str):
    """任务参数里可以给前端看的那部分；拿不到 schema 时返回 None（整个不给）。

    为什么必须过滤：`ParamSpec.type` 允许 `'credential'`，而 `create_task` 把
    整份参数原样落进 `params_json`（`task_manager.py:141-150` 的注释说明了为什么
    它不能在落库时剥——剥了插件重跑时就读不到值）。约束因此落在**序列化这一层**：
    凭据的口径是「不进哈希、不进日志、不进任务行」（credentials.py），一路吐到
    浏览器是这条口径最直接的破法。T12（天地图源插件，`credential_key='token'`）
    落地那一刻就会真的踩到。

    白名单口径是「插件自己声明、且不是凭据的键」：
    - 声明过 → 是插件的表单字段，前端本来就是照 `/<pid>/schema` 渲染它们的；
    - `type == 'credential'` → 一律剔除；
    - 宿主键（name/bbox/output_path/zoom_*）不在这里给 —— 它们已经是任务列了，
      同一个值出两份只会让前端有两个真相来源。

    schema 拿不到（插件卸载了、加载失败、`params_schema()` 自己抛了）就返回
    None：宁可前端少一块回显，也不能在「不知道哪个键是凭据」的情况下猜着给。
    用 `get_record().definition` 而不是 `registry.get_pipeline()`：插件被**禁用**
    时定义仍在，任务详情不该因为顺手禁了个插件就少半个页面。
    """
    record = registry.get_record(plugin_id)
    definition = record.definition if record is not None else None
    pipeline = getattr(definition, 'pipeline', None)
    if pipeline is None:
        return None
    try:
        specs = pipeline.params_schema().specs
    except Exception as e:
        logger.warning('插件 %s 的参数 schema 不可用，任务参数整份不外发：%r',
                       plugin_id, e)
        return None
    visible = {s.key for s in specs if s.type != 'credential'}
    try:
        stored = json.loads(params_json or '{}')
    except ValueError:
        return None
    if not isinstance(stored, dict):
        return None
    return {k: v for k, v in stored.items() if k in visible}


def _public_task(row: dict) -> dict:
    """任务行 → 给浏览器的 dict。唯一的出口，两个读端点都走它。"""
    out = {k: row.get(k) for k in _TASK_PUBLIC_COLUMNS}
    params = _public_params(row.get('plugin_id') or '',
                            row.get('params_json') or '{}')
    if params is not None:
        out['params'] = params
    return out


@plugins_bp.route('', methods=['GET'])
def list_plugins():
    """插件列表。加载失败的插件**也在列表里**（带 load_error）——
    隔离铁律的另一半：坏插件不许打穿宿主，但必须在界面上看得见。"""
    out = []
    for rec in registry.list_records():
        m = rec.manifest
        out.append({'id': m.plugin_id, 'name': m.name, 'version': m.version,
                    'origin': rec.origin, 'enabled': rec.enabled,
                    'load_error': rec.load_error,
                    'capabilities': list(m.capabilities),
                    'description': m.description,
                    'permissions': list(m.permissions),
                    'has_ui': bool(m.ui_assets)})
    return jsonify({'success': True, 'plugins': out})


@plugins_bp.route('/<pid>/enable', methods=['POST'])
def enable_plugin(pid):
    try:
        registry.set_enabled(pid, True)
    except KeyError:
        return jsonify({'error': '未知插件'}), 404
    return jsonify({'success': True})


@plugins_bp.route('/<pid>/disable', methods=['POST'])
def disable_plugin(pid):
    try:
        registry.set_enabled(pid, False)
    except KeyError:
        return jsonify({'error': '未知插件'}), 404
    return jsonify({'success': True})


@plugins_bp.route('/<pid>/config', methods=['GET'])
def get_plugin_config(pid):
    if registry.get_record(pid) is None:
        return jsonify({'error': '未知插件'}), 404
    return jsonify({'success': True, 'config': registry.get_config(pid)})


@plugins_bp.route('/<pid>/config', methods=['PUT'])
def put_plugin_config(pid):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    if registry.get_record(pid) is None:
        return jsonify({'error': '未知插件'}), 404
    errors = registry.set_config(pid, payload)
    if errors:
        return jsonify({'success': False, 'errors': errors}), 400
    return jsonify({'success': True})


@plugins_bp.route('/sources', methods=['GET'])
def list_plugin_sources():
    return jsonify({'success': True, 'sources': registry.list_sources()})


@plugins_bp.route('/<pid>/schema', methods=['GET'])
def plugin_params_schema(pid):
    """声明式任务表单的 schema。dataclass → dict 逐字段展开，
    不把内部对象序列化给前端。

    没有管线能力（或插件被禁用）时返回空数组而不是 404：表单渲染器拿到
    `params: []` 就是「这个插件没有可填参数」，一个正常状态。
    """
    pipeline = registry.get_pipeline(pid)
    if pipeline is None:
        return jsonify({'success': True, 'params': []})
    schema = pipeline.params_schema()
    return jsonify({'success': True, 'params': [
        {'key': s.key, 'type': s.type, 'label': s.label,
         'default': s.default, 'required': s.required,
         'min': s.min, 'max': s.max, 'choices': list(s.choices)}
        for s in schema.specs]})


@plugins_bp.route('/<pid>/tasks', methods=['POST'])
def create_plugin_task(pid):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    manager = get_plugin_task_manager()
    # `auto_start` 是**请求**上的动作开关，不是任务参数：留在 body 里喂给
    # `create_task` 会被插件 schema 的未知键闸门判成「参数非法」（实测
    # `400 参数非法：auto_start=unknown param`——这个键在修之前压根用不了），
    # 而且它还会跟着 params_json 落库，重跑时变成一个没人解释的残留键。
    auto_start = bool(payload.pop('auto_start', False))
    try:
        tid = manager.create_task(pid, payload)
    except KeyError as e:
        # KeyError = 插件不可用（未装/禁用/加载失败/schema 坏了）。
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    payload_out = {'success': True, 'task_id': tid}
    if auto_start:
        # 起不起来与建没建成是两件事。任务行**已经建好了**，把整个请求判成失败
        # 会让用户以为什么都没发生，而盘上／库里多了一条 pending 任务；插件正好
        # 在这两步之间被禁用（另一个标签页点了 /disable）就是这条路。
        # 所以：200 + task_id 照给，另附 started/start_error 让前端提示
        # 「已创建，但启动失败：<原因>」。
        try:
            manager.start_task(tid)
            payload_out['started'] = True
        except (KeyError, ValueError) as e:
            logger.warning('插件任务 %s 创建成功但自动启动失败：%r', tid, e)
            payload_out['started'] = False
            payload_out['start_error'] = str(e)
    return jsonify(payload_out)


@plugins_bp.route('/tasks', methods=['GET'])
def list_plugin_tasks():
    tasks = get_plugin_task_manager().list_tasks(_flag('active'))
    return jsonify({'success': True,
                    'tasks': [_public_task(t) for t in tasks]})


@plugins_bp.route('/tasks/<int:tid>', methods=['GET'])
def get_plugin_task(tid):
    task = get_plugin_task_manager().get_task(tid)
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({'success': True, 'task': _public_task(task)})


@plugins_bp.route('/tasks/<int:tid>/start', methods=['POST'])
def start_plugin_task(tid):
    try:
        get_plugin_task_manager().start_task(tid)
    except KeyError:
        # 行不存在 —— 与 GET /tasks/<tid> 同一档，不能混进 400。
        return jsonify({'error': '任务不存在'}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'success': True})


@plugins_bp.route('/tasks/<int:tid>/gaps', methods=['GET'])
def plugin_task_gaps(tid):
    """缺块摘要。载荷与瓦片管线的 `GET /api/tasks/<id>/gaps` 逐键同形
    （`task_id / total / by_outcome / explained / decision / status / samples`）——
    §13-3 的决策界面对两条管线只该有一套判据，尤其 `explained`（是否只有
    `no_data`）：那是「该不该问用户」的开关，前端自己再推一遍就是第二套实现。
    """
    try:
        summary = get_plugin_task_manager().gap_summary(tid)
    except KeyError:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({'success': True, **summary})


@plugins_bp.route('/tasks/<int:tid>/accept-gaps', methods=['POST'])
def accept_plugin_task_gaps(tid):
    try:
        get_plugin_task_manager().accept_gaps(tid)
    except KeyError:
        return jsonify({'error': '任务不存在'}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'success': True})


@plugins_bp.route('/tasks/<int:tid>', methods=['DELETE'])
def delete_plugin_task(tid):
    delete_files = _flag('delete_files')
    manager = get_plugin_task_manager()
    # 目录必须在删行**之前**问出来 —— task_output_dir 读的就是那一行。
    task_dir = manager.task_output_dir(tid)
    try:
        outcome = manager.delete_task(tid, delete_files=delete_files)
    except KeyError:
        return jsonify({'error': '任务不存在'}), 404
    payload = {'success': outcome.row_deleted, **outcome._asdict()}

    # 文件也删了的那条路上，登记产物跟着走：那些行唯一的用途是「文件还在哪」，
    # 文件没了就是纯垃圾。**只销行不够** —— 插件导出的产物（GPKG / MBTiles）
    # 落在 `plugin_task_<id>/` 的**同级**，rmtree 任务目录碰不到它们；销了行又
    # 等于把唯一记得它们的东西删掉。purge_registered_artifacts 先删这类落在
    # 目录外的文件、再销行，且绝不抛（跑在一次已经成功的删除之后）。
    # 插件登记的 path 归属由 `TaskContext.register_artifact` 在登记期校验
    # （必须落在 output_dir 内，见 src/plugins/task_context.py），所以这条
    # unlink 原语拿不到任务目录之外的任意路径。
    if delete_files:
        purge_registered_artifacts('plugin', tid, task_dir)

    # delete_files=false 是删除对话框的默认。行一走，
    # `<output_path>/plugin_task_<id>/` 就没有任何 DB 引用了 —— 启动清扫只认
    # pending_deletions 和几张任务表，从此谁都找不回它。登记一行把引用接回来。
    # 【只登记，不删文件】—— 用户说了保留，一个字节都不动。
    if outcome.row_deleted and not delete_files and task_dir is not None:
        try:
            if task_dir.exists():
                record_retained_output(task_dir)
                payload['files_retained_path'] = str(task_dir)
        except OSError as e:
            logger.warning('插件任务 %s：保留产物目录无法 stat：%r', tid, e)
    return jsonify(payload)


def _export_dest(source, fmt: str) -> Path:
    """导出目标路径：源产物的同级，加上目标格式的后缀。

    **不能直接 `with_suffix`**，它替换的是「最后一个点之后的东西」，而产物名里
    的点未必是扩展名：
    - `城区 2024.06`（按月份命名的成果目录）→ `城区 2024.gpkg`，月份没了，
      同一年的两个月份导出**互相覆盖**；
    - `dem_v1.5` → `dem_v1.gpkg`，版本号没了，同上；
    - 源本身就是 `.gpkg` 而目标也是 gpkg → 目标**等于源**，导出器写在自己的
      输入上。

    所以只在「现有后缀正是生产者自己声明的那个格式」时才替换 —— 那种情况下它
    确实是扩展名（`a.tif` + `fmt='tif'` → `a.gpkg`）—— 其余一律追加，宁可
    `城区 2024.06.gpkg` 有点长，也不许丢名字或撞源文件。声明格式与目标格式相同
    时也追加，否则又回到「目标等于源」。
    """
    src = Path(source.path)
    declared = f'.{source.fmt.lower()}' if source.fmt else ''
    if declared and declared != f'.{fmt}' and src.suffix.lower() == declared:
        return src.with_suffix(f'.{fmt}')
    return src.with_name(f'{src.name}.{fmt}')


@plugins_bp.route('/export/<int:tid>', methods=['POST'])
def export_plugin_task(tid):
    payload = request.get_json(silent=True) or {}
    fmt = str(payload.get('format') or '').strip().lower()
    exporter = registry.exporter_for(fmt)
    if exporter is None:
        return jsonify({'error': f'未知导出格式：{fmt!r}',
                        'supported_formats':
                            list(registry.list_export_formats())}), 400
    if get_plugin_task_manager().get_task(tid) is None:
        return jsonify({'error': '任务不存在'}), 404

    from src.plugins.protocols import ExportContext
    from src.services import artifact_store

    artifacts = [a for a in artifact_store.list_artifacts('plugin', tid)
                 if exporter.accepts(a.kind)]
    if not artifacts:
        return jsonify({'error': '该任务没有可由此格式导出的产物'}), 400
    source = artifacts[0]
    dest = _export_dest(source, fmt)
    ctx = ExportContext(
        task_id=tid,
        log=lambda msg, level='info': logger.info('[export:%s] %s', fmt, msg),
        progress=lambda done, total: None)
    try:
        result = exporter.export(source, dest, ctx)
    except Exception as e:
        logger.exception('插件导出失败')
        return jsonify({'error': str(e)}), 500
    artifact_store.record_artifact(result)
    return jsonify({'success': True, 'path': str(dest)})


@plugins_bp.route('/<pid>/assets/<path:filename>', methods=['GET'])
def plugin_asset(pid, filename):
    """插件 UI 资产。两道门都必须过：

    1. 落地包含判断（`resolve()` 之后必须真的在插件目录内）—— manifest 层的
       声明期检查看不到符号链接、大小写不敏感文件系统与 URL 编码的 `%2e%2e`。
    2. `manifest.ui_assets` 白名单 —— 插件目录里还躺着 plugin.py、vendor/、
       凭据文件；只有插件自己声明要给浏览器的那几个文件可以出去。
    """
    rec = registry.get_record(pid)
    if rec is None or rec.root is None:
        return jsonify({'error': '资产不可用'}), 404
    root = rec.root.resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return jsonify({'error': '资产不存在'}), 404
    if filename not in rec.manifest.ui_assets:
        return jsonify({'error': '资产未在 manifest 声明'}), 403
    return send_file(target)
