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
2. entry 路径闸 —— 清单层只拦「作者写下的字符串」，看不见文件系统；指向
   目录外的符号链接只能在加载期 resolve() 后拦。见 _resolve_entry。
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

#: `config_schema` 曾经登记在这里（作为 pipeline 的可选方法）。它现在是
#: `PluginDefinition.config_schema` 这个**字段**，由 dataclass 保证形制，
#: 不再需要签名闸——一件事一处实现。


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
        f'{method}{inspect.signature(fn)}')


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


def _resolve_entry(root: Path, m: PluginManifest) -> Path:
    """裁决 2：entry 必须落在插件目录内。

    双层：清单层（manifest.py 的 entry 允许清单）在声明期拦字符串写法
    （`../`、绝对路径、盘符、URL、空格）；这一层在加载期拦字符串挡不住的
    东西 —— 符号链接指向目录外、大小写不敏感文件系统上的等价路径 ——
    resolve() 之后做包含判断，不符拒载。两层缺一都不行：清单层看不见文件
    系统，加载层看不见「作者写了什么」。
    """
    base = root.resolve()
    entry = (base / m.entry).resolve()
    if not entry.is_relative_to(base) or entry == base:
        raise ManifestError(
            f'entry {m.entry!r} 越出插件目录（{base}），拒绝加载')
    if not entry.is_file():
        raise ManifestError(f'entry 文件不存在：{entry}')
    return entry


def _add_vendor_path(vendor: Path) -> None:
    """裁决 3：vendor 目录进 sys.path 末尾，同一目录只插一次。

    **append 而不是 insert(0)**：宿主必须赢。本仓大量依赖是函数内懒 import
    （src.contracts.source、GDAL、terrain builder），而 load_all 跑在这些
    懒 import 之前；vendor 抢在最前面意味着插件随手 vendor 的一个同名包能
    静默顶替宿主甚至 stdlib 的模块。vendor 的用途是补插件自己缺的库。

    幂等：重扫（load_all 可重复调用）不能让 sys.path 线性膨胀。记下**确实由
    本模块插入**的路径，reset_for_tests 撤回 —— 否则测试用的 tmp_path/vendor
    会永久留在 sys.path 上成为后续 import 的遮蔽源。生产期不撤：插件模块可能
    懒加载 vendor 里的子模块，运行中抽掉路径等于埋一个 ImportError。
    """
    path = str(vendor.resolve())
    if path in sys.path:
        return          # 已在（可能本来就在）——不插也不登记，撤回时才不会误删
    sys.path.append(path)
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
    try:
        spec.loader.exec_module(module)
        register = getattr(module, 'register', None)
        if not callable(register):
            raise ManifestError(f'{m.entry} 缺少 register() 函数')
        definition = register()
        if not isinstance(definition, PluginDefinition):
            raise ManifestError('register() 必须返回 PluginDefinition')
        _check_definition(m.plugin_id, definition)
    except BaseException:
        # 失败即撤：半初始化的模块留在 sys.modules 里，下次重扫或别的插件
        # `import tf_plugin_x` 会拿到一个执行到一半的模块对象。
        sys.modules.pop(module_name, None)
        if module_name in _PLUGIN_MODULES:
            _PLUGIN_MODULES.remove(module_name)
        raise
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


def _external_dirs():
    """`(plugins/ 下带 plugin.toml 的子目录, 扫描是否成功)`。

    扫描本身出错（权限、坏挂载）只记日志：宿主启动不该被一个不可读的插件目录
    打穿。但**必须把失败告诉调用方** —— `_prune_stale_rows` 会按「本轮没出现
    过」删行，扫不动时那等于把用户所有外部插件的启停与配置一次清空。
    """
    root = _plugins_root()
    try:
        if not root.is_dir():
            return [], True
        return [c for c in sorted(root.iterdir())
                if c.is_dir() and (c / 'plugin.toml').is_file()], True
    except OSError as e:
        logger.warning('插件目录扫描失败（%s）：%r', root, e)
        return [], False


def _prune_stale_rows() -> None:
    """删掉 `plugins` 表里本轮没出现过的行。**只在完整扫描之后调。**

    没有它，这张表只增不减，两个后果：

    1. 任何一次瞬时加载失败都永久留下一行垃圾。`_load_one` 连 manifest 都没
       解析出来时会**退化用模块名/目录名当 id**（builtin 的
       `src.plugins.builtin.tianditu_source` → `tianditu_source`），而真 id 是
       `tianditu`。这种行永远不会被覆盖，也永远不会被清掉，而
       `GET /api/plugins` 只读内存 `_RECORDS`，面板上看不见它们。
    2. **同名 id 继承。** 用户删掉插件 A、再装一个恰好同 id 的插件 B，B 直接
       继承 A 的 `enabled=1` 与 `config_json`（含 token）—— `_upsert_row` 对
       已存在的行只更新版本与错误，启停与配置是「用户的决定」不覆盖。
       `_reject_id_conflict` 只管同一轮扫描内的撞车，管不了前任留下的行。

    这一趟同时把存量机器上已有的垃圾行（本仓 0.4.0 那四行回落 id）清掉，
    所以不需要额外的一次性迁移：它每次启动都跑，比一次性迁移覆盖得更全。
    """
    keep = set(_RECORDS)
    conn = get_connection()
    try:
        rows = [r['id'] for r in conn.execute('SELECT id FROM plugins')]
        stale = [pid for pid in rows if pid not in keep]
        if not stale:
            return
        conn.executemany('DELETE FROM plugins WHERE id = ?',
                         [(pid,) for pid in stale])
        conn.commit()
        logger.info('清理了 %d 行已不存在的插件登记：%s', len(stale),
                    ', '.join(sorted(stale)))
    except Exception as e:
        # 清理是收尾动作，失败只是留着垃圾行，不该打穿启动。
        logger.warning('插件登记行清理失败：%r', e)
    finally:
        conn.close()


def load_all() -> None:
    """启动时调用一次。可重复调用（重扫）；测试先 reset_for_tests。

    **不收 socketio。** 曾经有一个 `socketio=None` 形参，体内一行都没引用过，
    而 app_factory 一直在传 —— 留着它就是在暗示插件能拿到 socketio，而 §5 的
    设计恰恰是拿不到（插件只能碰 `TaskContext`）。
    """
    with _LOCK:
        _RECORDS.clear()
        for module_name in _BUILTIN:
            _load_one(module_name, 'builtin', None)
        externals, scan_ok = _external_dirs()
        for child in externals:
            _load_one(str(child / 'plugin.toml'), 'external', child)
        if scan_ok:
            _prune_stale_rows()
        failed = sum(1 for r in _RECORDS.values() if r.load_error)
        ok = len(_RECORDS) - failed
        # 加载失败的插件**不算就绪**。原来这行无条件 INFO「插件注册表就绪：
        # 4 个插件（启用 0 个）」，四个 builtin 全 ModuleNotFoundError 时长
        # 得一模一样 —— 运维扫 INFO 会判成成功，而实际可用数是 0。
        log = logger.warning if failed else logger.info
        log('插件注册表就绪：%d 个（可用 %d / 失败 %d，启用 %d）',
            len(_RECORDS), ok, failed,
            sum(1 for r in _RECORDS.values() if r.enabled))


def _reject_id_conflict(m: PluginManifest, root: Optional[Path]) -> None:
    """id 撞车：先到者赢，来晚的一律不加载。

    没有这道守卫，一个外部插件只要声明别人的 id 就能顶替它：内存记录被覆盖，
    DB 行被就地改成自己的 origin，而 enabled 是从旧行读回来的 —— 用户当年为
    内置插件打开的开关，连同那一行里存的凭据（config_json 按 id 取），原样交
    给了这个新插件。触发不必是恶意，两个插件撞名就够。

    错误记录只登记在「目录名」这把空闲的 key 上，绝不写回被撞的那把 —— 否则
    守卫本身就把在册记录换成了错误记录。目录名也被占则只记日志。
    """
    held = _RECORDS[m.plugin_id]
    logger.warning('插件 id 冲突：%s 声明的 id %r 已被 %s 插件占用，跳过加载',
                   root, m.plugin_id, held.origin)
    key = root.name if root is not None else ''
    if not key or key in _RECORDS:
        return
    err = (f'ManifestError: 插件 id {m.plugin_id!r} 已被 {held.origin} '
           f'插件占用（先到者赢），本插件未加载')
    stub = PluginManifest(plugin_id=key, name=key, version=m.version,
                          api_version=API_MAJOR)
    try:
        enabled = _upsert_row(stub, 'external', err)
    except Exception:
        logger.exception('插件 id 冲突的错误登记失败：%s', root)
        return
    _RECORDS[key] = PluginRecord(stub, 'external', root, enabled, err, None)


def _load_one(source: str, origin: str, root: Optional[Path]) -> None:
    """加载一个插件；任何失败落成 load_error 记录，绝不向上抛。"""
    # 逐个报名字。第三方插件可以在模块级做无超时网络请求，那会让整个应用永远
    # 起不来，而在这之前日志停在「Database initialized successfully」之后一片
    # 空白，没有一行说明卡在谁身上。**不做超时**：那要另起线程执行第三方
    # import，代价大于收益。5 个插件不构成噪音。
    logger.info('加载插件：%s', source)
    record = None
    manifest: Optional[PluginManifest] = None   # 解析到了就用真的那份登记错误
    try:
        if origin == 'builtin':
            manifest, definition = _load_builtin_definition(source)
            _check_api_version(manifest)
        else:
            manifest = load_manifest_toml(Path(source))
            if manifest.plugin_id in _RECORDS:
                _reject_id_conflict(manifest, root)
                return
            _check_api_version(manifest)
            _check_abi(manifest)
            definition = _load_external_definition(root, manifest)
        enabled = _upsert_row(manifest, origin, '')
        record = PluginRecord(manifest, origin, root, enabled, '', definition)
    except (Exception, SystemExit) as e:
        # SystemExit 不是 Exception 的子类：插件 import 期一句 sys.exit() 会
        # 打穿 load_all 把宿主启动带走，那正是隔离铁律要防的事。
        logger.warning('插件加载失败：%s', source, exc_info=True)
        err = f'{type(e).__name__}: {e}'
        try:
            if manifest is None:
                # 连 manifest 都没解析出来（坏 TOML）才退化用目录名/模块名。
                pid = (source.rsplit('.', 1)[-1] if origin == 'builtin'
                       else Path(source).parent.name)
                manifest = PluginManifest(plugin_id=pid, name=pid, version='',
                                          api_version=API_MAJOR)
            enabled = _upsert_row(manifest, origin, err)
            record = PluginRecord(manifest, origin, root, enabled, err, None)
        except Exception:
            logger.exception('插件连错误登记都失败：%s', source)
    if record is None:
        return
    held = _RECORDS.get(record.manifest.plugin_id)
    if held is not None and held is not record:
        # 兜底不变量：任何路径都不许覆盖在册记录（例如坏 TOML 的目录名恰好
        # 撞上一个已加载插件的 id）。
        logger.warning('插件 id 冲突：%s 想登记 %r，该 id 已被 %s 插件占用，跳过',
                       source, record.manifest.plugin_id, held.origin)
        return
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
        cfg = json.loads(row['config_json'] or '{}')
    except json.JSONDecodeError:
        return {}
    # 合法 JSON 但不是对象（'[1,2]'）时下游 cfg.get 会 AttributeError；
    # credentials._as_text 那边同口径。
    return cfg if isinstance(cfg, dict) else {}


def set_config(plugin_id: str, values: Mapping[str, Any]) -> Dict[str, str]:
    """插件若声明了 `definition.config_schema` 则先过校验。返回错误表，空 = 已存。

    schema 只认 `PluginDefinition.config_schema` 这**一个**位置（理由见
    protocols.py 那个字段的注释：挂在 pipeline 上时纯数据源插件的配置完全不
    过校验）。

    有 schema 时落盘的是校验器洗出来的 clean 而不是 raw：schema 声明 int 就
    该存 int（消费者是 provider.snapshot(cfg) 与凭据解析），default 该回填，
    JSON null 不该进库。unknown 键已经被 errors 拦在前面，clean 不会丢东西。
    """
    rec = get_record(plugin_id)
    if rec is None:
        return {'_': '未知插件'}
    stored: Mapping[str, Any] = values
    schema = getattr(rec.definition, 'config_schema', None)
    if schema is not None:
        from src.plugins.params import validate_params
        clean, errors = validate_params(schema, values)
        if errors:
            return errors
        stored = clean
    conn = get_connection()
    try:
        conn.execute('UPDATE plugins SET config_json = ? WHERE id = ?',
                     (json.dumps(stored, ensure_ascii=False), plugin_id))
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
    凭据永不进指纹、日志与任务行（规格 §6）。

    两条腿都走，顺序与 list_sources 一致（静态描述符先、provider 后）：只问
    provider 会让静态描述符声明的源「列得出来、取不到快照」。
    """
    from src.contracts.source import SourceSnapshot
    definition = _enabled_definition(plugin_id)
    if definition is None:
        raise KeyError(f'插件不可用：{plugin_id!r}')
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
    if definition.source_provider is not None:
        return definition.source_provider.snapshot(source_id,
                                                   get_config(plugin_id))
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
