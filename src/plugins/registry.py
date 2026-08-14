"""插件注册表：发现、加载、启停、失败隔离、四类能力的查询入口。

两条腿（规格 §10）：
- builtin：_BUILTIN 硬编码名单（与 i18n catalog 同一理由——Nuitka 静态
  分析扫不到动态发现），manifest 取模块级 MANIFEST dict；
- external：扫 Config.BASE_DIR/plugins/<id>/plugin.toml，importlib 按
  文件位置载入，vendor/ 子目录进 sys.path。

隔离：任何一个插件的 manifest/import/register 异常都只落它自己的
load_error，宿主与其他插件不受影响。

加载期三道闸（前两道是评审裁决，缺了它们失败会推迟到运行期）：
1. 签名闸 —— runtime_checkable 的 isinstance 只查方法**存在性**，
   `def run(self)` 照样过；不在加载期比对参数个数，用户看到的是任务跑起来
   那一刻的一句裸 TypeError。见 _check_definition。
2. entry 路径闸 —— manifest 层不校验 entry，`entry = "../../x.py"` 会让
   加载器执行插件目录外的任意文件。见 _resolve_entry。
3. api_version / requires_abi —— manifest 层只存不比，闸门在这里。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from src.core.config import Config
from src.core.database import get_connection
from src.plugins import credentials
from src.plugins.manifest import (ManifestError, PluginManifest,
                                  current_abi_tag, load_manifest_toml,
                                  manifest_from_dict)
from src.plugins.protocols import (API_MAJOR, Exporter, PipelinePlugin,
                                   PluginDefinition, SourceProvider, TaskEvent,
                                   TaskHook)

logger = logging.getLogger(__name__)

#: in-tree 插件硬编码名单。新增 builtin 插件：① 这里加一行；
#: ② src/app_factory.py 可达性清单加一行。
_BUILTIN: Tuple[str, ...] = (
    'src.plugins.builtin.tianditu_source',
    'src.plugins.builtin.mvt_pipeline',
    'src.plugins.builtin.gpkg_exporter',
    'src.plugins.builtin.artifact_meta',
)


@dataclass
class PluginRecord:
    manifest: PluginManifest
    origin: str                       # 'builtin' | 'external'
    root: Optional[Path]
    enabled: bool
    load_error: str
    definition: Optional[PluginDefinition]


_LOCK = threading.RLock()
_RECORDS: Dict[str, PluginRecord] = {}
#: 本模块插进 sys.path 的 vendor 目录；reset_for_tests 据此撤回。
_VENDOR_PATHS: List[str] = []
#: 本模块塞进 sys.modules 的插件模块名；reset_for_tests 据此撤回，避免
#: 上一个测试（或上一次重扫）的插件代码残留成后续加载的幽灵。
_PLUGIN_MODULES: List[str] = []


def _plugins_root() -> Path:
    """external 插件目录：exe 旁（打包）/ 仓库根（源码）的 plugins/。
    测试用 monkeypatch 替换本函数指向 tmp_path。"""
    return Path(Config.BASE_DIR) / 'plugins'


# ---------------------------------------------------------------- 持久化

def _upsert_row(m: PluginManifest, origin: str, load_error: str) -> bool:
    """登记/刷新插件行，返回 enabled 现值。已存在行只更新版本与错误——
    启停与配置是用户的决定，不是发现的副产物。"""
    conn = get_connection()
    try:
        row = conn.execute('SELECT enabled FROM plugins WHERE id = ?',
                           (m.plugin_id,)).fetchone()
        if row is None:
            conn.execute(
                'INSERT INTO plugins (id, enabled, version, origin, load_error)'
                ' VALUES (?, 0, ?, ?, ?)',
                (m.plugin_id, m.version, origin, load_error))
            enabled = False
        else:
            conn.execute(
                'UPDATE plugins SET version = ?, origin = ?, load_error = ?'
                ' WHERE id = ?',
                (m.version, origin, load_error, m.plugin_id))
            enabled = bool(row['enabled'])
        conn.commit()
        return enabled
    finally:
        conn.close()


# ---------------------------------------------------------------- 加载闸

def _check_api_version(m: PluginManifest) -> None:
    if (m.api_version or '').split('.')[0] != API_MAJOR:
        raise ManifestError(
            f'api_version {m.api_version!r} 与宿主 {API_MAJOR}.x 不兼容')


def _check_abi(m: PluginManifest) -> None:
    if m.requires_abi and m.requires_abi != current_abi_tag():
        raise ManifestError(
            f'ABI 不匹配：插件需要 {m.requires_abi}，宿主是 {current_abi_tag()}')


#: 四类扩展点各自的协议与「方法名 → 宿主实际传的位置实参名」。参数名只用来
#: 拼错误消息里的期望签名，比对的是个数。
_MEMBER_CONTRACTS: Tuple[Tuple[str, Any,
                              Tuple[Tuple[str, Tuple[str, ...]], ...]], ...] = (
    ('source_provider', SourceProvider, (
        ('list_sources', ()),
        ('snapshot', ('source_id', 'cfg')),
        ('authorize', ('headers', 'cfg')),
    )),
    ('pipeline', PipelinePlugin, (
        ('params_schema', ()),
        ('estimate', ('params', 'region')),
        ('run', ('ctx',)),
    )),
    ('exporters', Exporter, (
        ('format_id', ()),
        ('accepts', ('kind',)),
        ('export', ('artifact', 'dest', 'ctx')),
    )),
    ('hooks', TaskHook, (
        ('on_event', ('event',)),
    )),
)

#: 协议之外、但宿主确实会调的可选方法（set_config 调 config_schema()）：
#: 有就必须能按宿主的调法调通，没有则跳过。
_OPTIONAL_METHODS: Dict[str, Tuple[Tuple[str, Tuple[str, ...]], ...]] = {
    'pipeline': (('config_schema', ()),),
}


def _positional_window(fn) -> Optional[Tuple[int, Optional[int]]]:
    """(最少, 最多) 可接受的位置实参个数；*args 时最多为 None（无上限）。

    拿不到签名（C 扩展、奇异 callable）返回 None —— 那种情况下签名闸没有
    可信输入，宁可放过也不误杀。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    low = 0
    high: Optional[int] = 0
    for p in sig.parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            high = None
        elif p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            if high is not None:
                high += 1
            if p.default is p.empty:
                low += 1
        elif p.kind is p.KEYWORD_ONLY and p.default is p.empty:
            # 宿主只按位置调用，必填的关键字参数注定填不上 → 空窗口，必然不符。
            return (1, 0)
    return (low, high)


def _expected_text(method: str, params: Tuple[str, ...]) -> str:
    return f'{method}(self' + ''.join(f', {p}' for p in params) + ')'


def _check_method(plugin_id: str, member: str, obj: Any,
                  method: str, params: Tuple[str, ...]) -> None:
    fn = getattr(obj, method, None)
    if not callable(fn):
        raise ManifestError(
            f'插件 {plugin_id!r} 的 {member} 缺少方法 '
            f'{_expected_text(method, params)}')
    window = _positional_window(fn)
    if window is None:
        return
    low, high = window
    argc = len(params)
    if low <= argc and (high is None or argc <= high):
        return
    raise ManifestError(
        f'插件 {plugin_id!r} 的 {member}.{method} 签名不符：宿主按 '
        f'{_expected_text(method, params)} 调用，实际是 '
        f'{method}{inspect.signature(fn)}（缺 self 是因为它是绑定方法）')


def _check_definition(plugin_id: str, definition: PluginDefinition) -> None:
    """裁决 1：isinstance 之后再按参数个数比签名，不符直接拒载。

    runtime_checkable 的 isinstance 只查方法存在性，挡不住参数个数写错的
    插件；那种插件会在任务跑起来那一刻炸成一句裸 TypeError，用户既看不出
    是哪个插件的问题，也已经排了半天队。
    """
    for member, protocol, specs in _MEMBER_CONTRACTS:
        value = getattr(definition, member, None)
        if value is None:
            continue
        objs = value if isinstance(value, (tuple, list)) else (value,)
        for obj in objs:
            if not isinstance(obj, protocol):
                # 粗筛：runtime_checkable 只查方法存在性，所以走到这里必定
                # 是真缺方法；把缺的名字直接报出来。
                missing = [name for name, _p in specs
                           if not callable(getattr(obj, name, None))]
                raise ManifestError(
                    f'插件 {plugin_id!r} 的 {member} 不满足 '
                    f'{protocol.__name__} 协议：缺 {", ".join(missing)}')
            for method, params in specs:
                _check_method(plugin_id, member, obj, method, params)
            for method, params in _OPTIONAL_METHODS.get(member, ()):
                if getattr(obj, method, None) is not None:
                    _check_method(plugin_id, member, obj, method, params)


def _resolve_entry(root: Path, m: PluginManifest) -> Path:
    """裁决 2：entry 必须落在插件目录内。

    manifest 层只把 entry 当字符串存着，所以 `entry = "../../etc/passwd.py"`
    这类越界写法的闸门只能在这里 —— resolve() 之后做包含判断（顺带挡掉
    绝对路径与符号链接逃逸），不符拒载。
    """
    base = root.resolve()
    entry = (base / m.entry).resolve()
    if entry == base or not entry.is_relative_to(base):
        raise ManifestError(
            f'entry {m.entry!r} 越出插件目录（{base}），拒绝加载')
    if not entry.is_file():
        raise ManifestError(f'entry 文件不存在：{entry}')
    return entry


def _add_vendor_path(vendor: Path) -> None:
    """裁决 3：vendor 目录进 sys.path，同一目录只插一次。

    重扫（load_all 可重复调用）不能让 sys.path 无限膨胀；记下插过的路径，
    reset_for_tests 撤回 —— 否则测试用的 tmp_path/vendor 会永久留在 sys.path
    上，成为后续 import 的遮蔽源。生产期不撤：插件模块可能懒加载 vendor 里的
    子模块，运行中抽掉路径等于埋一个 ImportError。
    """
    path = str(vendor.resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    if path not in _VENDOR_PATHS:
        _VENDOR_PATHS.append(path)


# ---------------------------------------------------------------- 加载

def _load_external_definition(root: Path, m: PluginManifest) -> PluginDefinition:
    vendor = root / 'vendor'
    if vendor.is_dir():
        _add_vendor_path(vendor)
    entry = _resolve_entry(root, m)
    module_name = f'tf_plugin_{m.plugin_id}'
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ManifestError(f'entry 无法作为 Python 模块加载：{entry}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module      # 插件内相对 import 需要
    if module_name not in _PLUGIN_MODULES:
        _PLUGIN_MODULES.append(module_name)
    spec.loader.exec_module(module)
    register = getattr(module, 'register', None)
    if not callable(register):
        raise ManifestError(f'{m.entry} 缺少 register() 函数')
    definition = register()
    if not isinstance(definition, PluginDefinition):
        raise ManifestError('register() 必须返回 PluginDefinition')
    _check_definition(m.plugin_id, definition)
    return definition


def _load_builtin_definition(module_name: str) -> Tuple[PluginManifest,
                                                        PluginDefinition]:
    module = __import__(module_name, fromlist=['MANIFEST', 'register'])
    m = manifest_from_dict(getattr(module, 'MANIFEST'))
    register = getattr(module, 'register', None)
    definition = register() if callable(register) else PluginDefinition()
    if not isinstance(definition, PluginDefinition):
        raise ManifestError('register() 必须返回 PluginDefinition')
    _check_definition(m.plugin_id, definition)
    return m, definition


def load_all(socketio=None) -> None:
    """启动时调用一次。可重复调用（重扫）；测试先 reset_for_tests。"""
    with _LOCK:
        _RECORDS.clear()
        for module_name in _BUILTIN:
            _load_one(module_name, 'builtin', None)
        root = _plugins_root()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / 'plugin.toml').is_file():
                    _load_one(str(child / 'plugin.toml'), 'external', child)
        logger.info('插件注册表就绪：%d 个插件（启用 %d 个）',
                    len(_RECORDS),
                    sum(1 for r in _RECORDS.values() if r.enabled))


def _load_one(source: str, origin: str, root: Optional[Path]) -> None:
    """加载一个插件；任何失败落成 load_error 记录，绝不向上抛。"""
    record = None
    try:
        if origin == 'builtin':
            m, definition = _load_builtin_definition(source)
            _check_api_version(m)
        else:
            m = load_manifest_toml(Path(source))
            _check_api_version(m)
            _check_abi(m)
            definition = _load_external_definition(root, m)
        enabled = _upsert_row(m, origin, '')
        record = PluginRecord(m, origin, root, enabled, '', definition)
    except Exception as e:
        logger.warning('插件加载失败：%s', source, exc_info=True)
        pid = (source.rsplit('.', 1)[-1] if origin == 'builtin'
               else Path(source).parent.name)
        err = f'{type(e).__name__}: {e}'
        try:
            m = PluginManifest(plugin_id=pid, name=pid, version='',
                               api_version=API_MAJOR)
            enabled = _upsert_row(m, origin, err)
            record = PluginRecord(m, origin, root, enabled, err, None)
        except Exception:
            logger.exception('插件连错误登记都失败：%s', source)
    if record is not None:
        _RECORDS[record.manifest.plugin_id] = record


def reset_for_tests() -> None:
    """清空注册表并撤回本模块对进程全局的改动（sys.path / sys.modules）。

    撤回是必需的：测试的插件目录在 tmp_path 下，留着会遮蔽后续 import，也让
    「重扫」拿到上一轮的插件模块对象。测试因此不需要自己手写 sys.modules 清单。
    """
    with _LOCK:
        _RECORDS.clear()
        for path in _VENDOR_PATHS:
            while path in sys.path:
                sys.path.remove(path)
        _VENDOR_PATHS.clear()
        for name in _PLUGIN_MODULES:
            sys.modules.pop(name, None)
        _PLUGIN_MODULES.clear()
    credentials.invalidate()


# ---------------------------------------------------------------- 查询与启停

def list_records() -> List[PluginRecord]:
    with _LOCK:
        return sorted(_RECORDS.values(), key=lambda r: r.manifest.plugin_id)


def get_record(plugin_id: str) -> Optional[PluginRecord]:
    with _LOCK:
        return _RECORDS.get(plugin_id)


def _enabled_definition(plugin_id: str) -> Optional[PluginDefinition]:
    rec = get_record(plugin_id)
    if rec is None or not rec.enabled or rec.definition is None:
        return None
    return rec.definition


def set_enabled(plugin_id: str, enabled: bool) -> None:
    rec = get_record(plugin_id)
    if rec is None:
        raise KeyError(f'未知插件：{plugin_id!r}')
    conn = get_connection()
    try:
        conn.execute('UPDATE plugins SET enabled = ? WHERE id = ?',
                     (1 if enabled else 0, plugin_id))
        conn.commit()
    finally:
        conn.close()
    with _LOCK:
        rec.enabled = bool(enabled)


def get_config(plugin_id: str) -> dict:
    conn = get_connection()
    try:
        row = conn.execute('SELECT config_json FROM plugins WHERE id = ?',
                           (plugin_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        return json.loads(row['config_json'] or '{}')
    except json.JSONDecodeError:
        return {}


def set_config(plugin_id: str, values: Mapping[str, Any]) -> Dict[str, str]:
    """插件若定义了 pipeline.config_schema() 则先过校验。返回错误表，空 = 已存。"""
    rec = get_record(plugin_id)
    if rec is None:
        return {'_': '未知插件'}
    if rec.definition is not None and rec.definition.pipeline is not None:
        schema_fn = getattr(rec.definition.pipeline, 'config_schema', None)
        if callable(schema_fn):
            from src.plugins.params import validate_params
            _, errors = validate_params(schema_fn(), values)
            if errors:
                return errors
    conn = get_connection()
    try:
        conn.execute('UPDATE plugins SET config_json = ? WHERE id = ?',
                     (json.dumps(values, ensure_ascii=False), plugin_id))
        conn.commit()
    finally:
        conn.close()
    credentials.invalidate(plugin_id)
    return {}


# ---------------------------------------------------------------- 能力查询

def list_sources() -> List[dict]:
    out: List[dict] = []
    for rec in list_records():
        definition = _enabled_definition(rec.manifest.plugin_id)
        if definition is None:
            continue
        descriptors = list(definition.sources)
        if definition.source_provider is not None:
            descriptors.extend(definition.source_provider.list_sources())
        for d in descriptors:
            out.append({
                'plugin_id': rec.manifest.plugin_id,
                'source_id': d.source_id, 'name': d.name,
                'max_zoom': d.max_zoom, 'attribution': d.attribution,
                'needs_credential': bool(d.credential_key),
            })
    return out


def build_source_snapshot(plugin_id: str, source_id: str):
    """描述符 → SourceSnapshot。credential_reference 是键名不是值——
    凭据永不进指纹、日志与任务行（规格 §6）。"""
    from src.contracts.source import SourceSnapshot
    definition = _enabled_definition(plugin_id)
    if definition is None:
        raise KeyError(f'插件不可用：{plugin_id!r}')
    if definition.source_provider is not None:
        return definition.source_provider.snapshot(source_id,
                                                   get_config(plugin_id))
    for d in definition.sources:
        if d.source_id == source_id:
            return SourceSnapshot(
                source_id=f'plugin:{plugin_id}:{d.source_id}',
                url_template=d.url_template,
                style='p',
                subdomains=tuple(d.subdomains),
                credential_reference=(
                    f'plugin:{plugin_id}:{d.credential_key}'
                    if d.credential_key else ''),
                attribution=d.attribution,
                usage_policy=d.usage_policy,
            )
    raise KeyError(f'插件 {plugin_id!r} 没有数据源 {source_id!r}')


def get_pipeline(plugin_id: str) -> Optional[PipelinePlugin]:
    definition = _enabled_definition(plugin_id)
    return definition.pipeline if definition else None


def iter_exporters() -> Iterator[Tuple[str, Exporter]]:
    for rec in list_records():
        definition = _enabled_definition(rec.manifest.plugin_id)
        if definition:
            for exporter in definition.exporters:
                yield rec.manifest.plugin_id, exporter


def exporter_for(fmt: str) -> Optional[Exporter]:
    for _pid, exporter in iter_exporters():
        if exporter.format_id() == fmt:
            return exporter
    return None


def list_export_formats() -> Tuple[str, ...]:
    return tuple(sorted({e.format_id() for _p, e in iter_exporters()}))


def iter_hooks() -> Iterator[Tuple[str, TaskHook]]:
    for rec in list_records():
        definition = _enabled_definition(rec.manifest.plugin_id)
        if definition:
            for hook in definition.hooks:
                yield rec.manifest.plugin_id, hook


def dispatch_event(event: TaskEvent) -> None:
    """钩子分发。旁路铁律：任何钩子异常只记日志，绝不影响任务。"""
    for plugin_id, hook in iter_hooks():
        try:
            hook.on_event(event)
        except Exception as e:
            logger.warning('插件钩子失败（%s，已忽略）：%r', plugin_id, e)
