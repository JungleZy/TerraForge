"""插件管理与插件任务路由。统一挂在 /api/plugins 前缀下。

一条铁律贯穿本模块：**任何时候都实时查注册表**，绝不在模块级缓存
`PluginRecord` / 管线对象。插件可以在运行期被启停（`/enable`、`/disable`）、
也可以被重扫（`registry.load_all()`），缓存一份就等于让界面显示上一轮的世界。

管理器同理：`get_plugin_task_manager()` 每次现取，不像四条核心管线那样由
`app_factory` 往模块全局里注入 —— 插件任务管理器全进程只有一份，注册表也是，
再复制一个模块级引用只会多一个会陈旧的指针。
"""

from __future__ import annotations

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
    try:
        tid = manager.create_task(pid, payload)
    except KeyError as e:
        # KeyError = 插件不可用（未装/禁用/加载失败/schema 坏了）。
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if payload.get('auto_start'):
        manager.start_task(tid)
    return jsonify({'success': True, 'task_id': tid})


@plugins_bp.route('/tasks', methods=['GET'])
def list_plugin_tasks():
    return jsonify({'success': True,
                    'tasks': get_plugin_task_manager().list_tasks(
                        _flag('active'))})


@plugins_bp.route('/tasks/<int:tid>', methods=['GET'])
def get_plugin_task(tid):
    task = get_plugin_task_manager().get_task(tid)
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({'success': True, 'task': task})


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
    manager = get_plugin_task_manager()
    if manager.get_task(tid) is None:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({'success': True, **manager.gap_summary(tid)})


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
    dest = Path(source.path).with_suffix(f'.{fmt}')
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
