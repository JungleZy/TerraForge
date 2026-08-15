"""注册表：发现、启停、失败隔离、API 版本闸、ABI 闸、vendor、凭据解析。

另含三条评审裁决的回归：加载期签名闸（runtime_checkable 只查方法存在性）、
entry 路径闸（清单层不拦越界 entry）、vendor 目录进 sys.path 的幂等性。
"""

import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.plugins import credentials, registry  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（只有 autouse 的隔离夹具），按
    tests/test_plugin_db_schema.py:14-34 的既有写法在本文件里建一个。
    """
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core.database import init_database

    init_database()
    return path


_PLAIN_BODY = ('from src.plugins.protocols import PluginDefinition\n'
               'def register():\n    return PluginDefinition()\n')


def _write_external(root, pid, *, api='1', caps='["hook"]',
                    body=_PLAIN_BODY, abi='', extra='', entry=''):
    d = root / 'plugins' / pid
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        f'id = "{pid}"\nname = "{pid}"\nversion = "0.1"\n'
        f'api_version = "{api}"\ncapabilities = {caps}\n'
        + (f'requires_abi = "{abi}"\n' if abi else '')
        + (f'entry = "{entry}"\n' if entry else '')
        + extra, encoding='utf-8')
    (d / 'plugin.py').write_text(body, encoding='utf-8')
    return d


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, '_plugins_root',
                        lambda: tmp_path / 'plugins')
    registry.reset_for_tests()


def test_external_plugin_loads_disabled_by_default(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'demo')
    registry.load_all()
    rec = registry.get_record('demo')
    assert rec is not None and rec.origin == 'external'
    assert rec.enabled is False and rec.load_error == ''
    assert rec.definition is not None  # 发现即加载，启用只控能力暴露


def test_bad_manifest_isolated(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    (tmp_path / 'plugins' / 'broken').mkdir(parents=True)
    (tmp_path / 'plugins' / 'broken' / 'plugin.toml').write_text(
        'id = ""', encoding='utf-8')
    _write_external(tmp_path, 'good')
    registry.load_all()
    assert registry.get_record('broken').load_error != ''
    assert registry.get_record('good').load_error == ''


def test_import_error_isolated(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'boom', body='raise RuntimeError("boom")\n')
    registry.load_all()
    rec = registry.get_record('boom')
    assert 'boom' in rec.load_error and rec.definition is None


def test_plugin_sys_exit_is_isolated(db, tmp_path, monkeypatch):
    """SystemExit 不是 Exception 的子类：插件 import 期一句 sys.exit() 不许
    打穿 load_all 把宿主启动带走。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'exiter', body='import sys\nsys.exit(3)\n')
    _write_external(tmp_path, 'survivor')
    registry.load_all()                                # 不抛
    assert 'SystemExit' in registry.get_record('exiter').load_error
    assert registry.get_record('survivor').load_error == ''


def test_unreadable_plugins_dir_does_not_break_startup(db, tmp_path,
                                                       monkeypatch):
    """插件目录扫描本身出错（权限）只记日志，不打穿启动。"""
    _fresh(tmp_path, monkeypatch)
    root = tmp_path / 'plugins'
    root.mkdir()
    root.chmod(0o000)
    try:
        try:
            list(root.iterdir())
        except OSError:
            pass
        else:
            pytest.skip('本环境读得动 0o000 目录（root？），构造不出该错误')
        registry.load_all()                            # 不抛
    finally:
        root.chmod(0o700)
    assert registry.get_record('demo') is None         # 只是扫不到，不崩


def test_api_major_mismatch_rejected(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'future', api='99')
    registry.load_all()
    assert 'api_version' in registry.get_record('future').load_error


def test_abi_mismatch_rejected(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'native', abi='cp399-os2-warp9')
    registry.load_all()
    assert 'abi' in registry.get_record('native').load_error.lower()


def test_vendor_dir_goes_on_sys_path(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'vend', body=(
        'import vendored_lib\n'
        'from src.plugins.protocols import PluginDefinition\n'
        'def register():\n    return PluginDefinition()\n'))
    vd = d / 'vendor'
    vd.mkdir()
    (vd / 'vendored_lib.py').write_text('VALUE = 42\n', encoding='utf-8')
    registry.load_all()
    assert registry.get_record('vend').load_error == ''


def test_enable_persists_and_gates_pipeline(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'demo2', caps='["pipeline"]', body=(
        'from src.plugins.protocols import (ParamSchema, PluginDefinition,\n'
        '                                   PluginOutcome)\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def estimate(self, params, region): return None\n'
        '    def run(self, ctx): return PluginOutcome.COMPLETED\n'
        'def register():\n    return PluginDefinition(pipeline=P())\n'))
    registry.load_all()
    assert registry.get_pipeline('demo2') is None      # 缺省关闭
    registry.set_enabled('demo2', True)
    assert registry.get_pipeline('demo2') is not None
    registry.reset_for_tests()
    registry.load_all()                                 # 重启后仍启用
    assert registry.get_pipeline('demo2') is not None
    registry.set_enabled('demo2', False)


def test_credential_resolution(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'cred')
    registry.load_all()
    registry.set_config('cred', {'token': 'sekret'})
    credentials.invalidate()
    assert credentials.resolve_reference('plugin:cred:token') == 'sekret'
    assert credentials.resolve_reference('plugin:cred:missing') == ''
    assert credentials.resolve_reference('not-a-plugin-ref') == ''


# ------------------------------------------------------- 裁决 1：签名闸

_WRONG_RUN = (
    'from src.plugins.protocols import ParamSchema, PluginDefinition\n'
    'class P:\n'
    '    def params_schema(self): return ParamSchema(())\n'
    '    def estimate(self, params, region): return None\n'
    '    def run(self): return None\n'          # 少一个参数：宿主传 ctx 必炸
    'def register():\n    return PluginDefinition(pipeline=P())\n')


def test_pipeline_with_wrong_run_arity_rejected(db, tmp_path, monkeypatch):
    """isinstance(x, PipelinePlugin) 放行的错签名必须在加载期拒掉。

    runtime_checkable 只查方法存在性，run(self) 照样过 isinstance；不在加载
    期拦，用户看到的是任务跑起来那一刻的一句裸 TypeError。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'badsig', caps='["pipeline"]', body=_WRONG_RUN)
    registry.load_all()
    rec = registry.get_record('badsig')
    assert rec.definition is None                       # 拒载
    assert 'badsig' in rec.load_error                   # 带插件 id
    assert 'run' in rec.load_error                      # 带方法名
    assert 'run(self, ctx)' in rec.load_error           # 带期望签名


def test_hook_with_wrong_arity_rejected(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'badhook', body=(
        'from src.plugins.protocols import PluginDefinition\n'
        'class H:\n'
        '    def on_event(self, event, extra): return None\n'
        'def register():\n    return PluginDefinition(hooks=(H(),))\n'))
    registry.load_all()
    rec = registry.get_record('badhook')
    assert rec.definition is None
    assert 'on_event(self, event)' in rec.load_error


def test_missing_protocol_method_rejected(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'nomethod', caps='["pipeline"]', body=(
        'from src.plugins.protocols import ParamSchema, PluginDefinition\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def run(self, ctx): return None\n'         # 缺 estimate
        'def register():\n    return PluginDefinition(pipeline=P())\n'))
    registry.load_all()
    rec = registry.get_record('nomethod')
    assert rec.definition is None
    assert 'estimate' in rec.load_error


def test_flexible_signatures_accepted(db, tmp_path, monkeypatch):
    """签名闸只比对参数个数窗口：*args、默认值、关键字别名都不能被误杀。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'flex', caps='["pipeline"]', body=(
        'from src.plugins.protocols import ParamSchema, PluginDefinition\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def estimate(self, *args, **kw): return None\n'
        '    def run(self, ctx, dry_run=False): return None\n'
        'def register():\n    return PluginDefinition(pipeline=P())\n'))
    registry.load_all()
    rec = registry.get_record('flex')
    assert rec.load_error == '' and rec.definition is not None


# --------------------------------------------------- 裁决 2：entry 路径闸

def test_entry_symlink_escaping_plugin_dir_rejected(db, tmp_path, monkeypatch):
    """清单层拦不住的那一半：entry 是插件目录内的符号链接，指向目录外。

    字符串写法（`../`、绝对路径、盘符、URL）由 manifest 层的允许清单拦下
    （覆盖在 tests/test_plugin_manifest.py:126-133），加载期这道闸负责文件
    系统层面的逃逸 —— 清单层看不见符号链接。
    """
    _fresh(tmp_path, monkeypatch)
    outside = tmp_path / 'escape.py'
    outside.write_text('raise AssertionError("越界 entry 被执行了")\n',
                       encoding='utf-8')
    d = _write_external(tmp_path, 'escaper', entry='link.py')
    try:
        (d / 'link.py').symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('本平台不允许创建符号链接')
    registry.load_all()                          # 不抛
    rec = registry.get_record('escaper')
    assert rec.definition is None                # 越界文件没被执行
    assert 'entry' in rec.load_error and '越出插件目录' in rec.load_error


def test_entry_in_subdir_accepted(db, tmp_path, monkeypatch):
    """闸门只拦越界，不拦插件目录内的子目录 entry。"""
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'subentry', entry='pkg/main.py')
    (d / 'pkg').mkdir()
    (d / 'pkg' / 'main.py').write_text(_PLAIN_BODY, encoding='utf-8')
    registry.load_all()
    assert registry.get_record('subentry').load_error == ''


# ------------------------------------------ 裁决 3：vendor sys.path 幂等

def test_vendor_path_inserted_once(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'vend2')
    (d / 'vendor').mkdir()
    target = str((d / 'vendor').resolve())
    registry.load_all()
    registry.reset_for_tests()
    registry.load_all()
    registry.load_all()
    assert sys.path.count(target) == 1


def test_vendor_path_goes_behind_host(db, tmp_path, monkeypatch):
    """插件 vendor 不许抢在宿主与 stdlib 前面：宿主大量依赖是函数内懒 import，
    抢前面等于让插件随手 vendor 的同名包静默顶替宿主的模块。"""
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'vend4')
    (d / 'vendor').mkdir()
    target = str((d / 'vendor').resolve())
    before = list(sys.path)
    registry.load_all()
    assert sys.path[-1] == target
    assert sys.path[:len(before)] == before      # 宿主原有条目一个都没被挤后


def test_reset_for_tests_removes_vendor_paths_and_modules(db, tmp_path,
                                                          monkeypatch):
    """tmp_path 下的 vendor 目录不许在测试之间留在 sys.path 上遮蔽 import，
    插件模块也不许留在 sys.modules 里被下一轮当成已加载。"""
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'vend3')
    (d / 'vendor').mkdir()
    target = str((d / 'vendor').resolve())
    registry.load_all()
    assert target in sys.path
    assert 'tf_plugin_vend3' in sys.modules
    registry.reset_for_tests()
    assert target not in sys.path
    assert 'tf_plugin_vend3' not in sys.modules


def test_failed_import_leaves_no_half_module(db, tmp_path, monkeypatch):
    """exec_module 炸掉后 sys.modules 里不许留半初始化的插件模块。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'halfboom', body=(
        'VALUE = 1\nraise RuntimeError("boom")\n'))
    registry.load_all()
    assert registry.get_record('halfboom').definition is None
    assert 'tf_plugin_halfboom' not in sys.modules


# --------------------------------------------------------- 其它公开 API

def test_sources_and_snapshot_and_export_surface(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'multi', caps='["sources", "exporter"]', body=(
        'from pathlib import Path\n'
        'from src.plugins.protocols import PluginDefinition, SourceDescriptor\n'
        'class E:\n'
        '    def format_id(self): return "zip"\n'
        '    def accepts(self, kind): return True\n'
        '    def export(self, artifact, dest, ctx): return artifact\n'
        'def register():\n'
        '    return PluginDefinition(\n'
        '        sources=(SourceDescriptor(source_id="s1", name="S1",\n'
        '                                  url_template="https://h/{z}/{x}/{y}?k={credential}",\n'
        '                                  max_zoom=18, attribution="A",\n'
        '                                  credential_key="token"),),\n'
        '        exporters=(E(),))\n'))
    registry.load_all()
    assert registry.list_sources() == []                 # 未启用不暴露能力
    assert registry.list_export_formats() == ()
    registry.set_enabled('multi', True)
    assert registry.list_sources() == [{
        'plugin_id': 'multi', 'source_id': 's1', 'name': 'S1',
        'max_zoom': 18, 'attribution': 'A', 'needs_credential': True,
        # 声明了 credential_key 但 config 里没这个键 —— 未配置。
        'credential_ready': False}]
    assert registry.list_export_formats() == ('zip',)
    assert registry.exporter_for('zip') is not None
    assert registry.exporter_for('nope') is None
    snap = registry.build_source_snapshot('multi', 's1')
    assert snap.source_id == 'plugin:multi:s1'
    assert snap.credential_reference == 'plugin:multi:token'   # 键名，不是值
    with pytest.raises(KeyError):
        registry.build_source_snapshot('multi', 'nope')
    registry.set_enabled('multi', False)
    with pytest.raises(KeyError):
        registry.build_source_snapshot('multi', 's1')          # 停用即不可用


def test_list_sources_reports_whether_the_credential_is_configured(
        db, tmp_path, monkeypatch):
    """`credential_ready` 钉三种情形：需凭据已填 / 需凭据未填 / 无需凭据。

    这是「缺 key 时界面一屏红块、没有任何地方说你没填 key」那条欠账的判据：
    `needs_credential` 是静态声明（这个源要不要凭据），它答不了「填没填」。
    无需凭据的源恒为 True —— 语义是「这个源现在能不能用」，前端一个判据到底。

    真值绝不下发：断言里连带钉住 payload 的键集合，多一个 token 字段就红。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'mixed', caps='["sources"]', body=(
        'from src.plugins.protocols import PluginDefinition, SourceDescriptor\n'
        'def register():\n'
        '    return PluginDefinition(sources=(\n'
        '        SourceDescriptor(source_id="needs", name="Needs",\n'
        '                         url_template="https://h/{z}/{x}/{y}?k={credential}",\n'
        '                         max_zoom=18, credential_key="token"),\n'
        '        SourceDescriptor(source_id="free", name="Free",\n'
        '                         url_template="https://h/{z}/{x}/{y}",\n'
        '                         max_zoom=18),\n'
        '    ))\n'))
    registry.load_all()
    registry.set_enabled('mixed', True)
    credentials.invalidate()

    def ready():
        return {s['source_id']: s['credential_ready']
                for s in registry.list_sources()}

    assert ready() == {'needs': False, 'free': True}      # 键都还没有
    registry.set_config('mixed', {'token': ''})
    assert ready() == {'needs': False, 'free': True}      # 空串就是没填
    registry.set_config('mixed', {'token': True})
    # 归一化口径与 credentials._as_text 同源：bool 解析成 ''，所以不算填了。
    # 分叉的后果是界面说「已配置」而下载照样 401。
    assert ready() == {'needs': False, 'free': True}
    registry.set_config('mixed', {'token': 'sekret'})
    assert ready() == {'needs': True, 'free': True}

    needs = [s for s in registry.list_sources()
             if s['source_id'] == 'needs'][0]
    assert needs['needs_credential'] is True
    assert 'sekret' not in repr(needs)                    # 真值不下发
    assert set(needs) == {'plugin_id', 'source_id', 'name', 'max_zoom',
                          'attribution', 'needs_credential',
                          'credential_ready'}


def test_set_enabled_unknown_raises_keyerror(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    registry.load_all()
    with pytest.raises(KeyError):
        registry.set_enabled('nope', True)


def test_set_config_stores_cleaned_values(db, tmp_path, monkeypatch):
    """有 config_schema 时落盘的是校验器洗出来的 clean，不是 raw。

    schema 声明 int 就该存 int（消费者是 provider.snapshot(cfg) 与凭据解析）、
    default 该回填、JSON null 不该进库 —— 否则校验器只做了判定没做落库值，
    库里躺着 '3' 而 schema 写着 int。

    schema 声明在 `PluginDefinition.config_schema` 上，**不是** pipeline 的方法：
    见 test_config_schema_works_without_a_pipeline。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'cfg', caps='["pipeline"]', body=(
        'from src.plugins.protocols import (ParamSchema, ParamSpec,\n'
        '                                   PluginDefinition)\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def estimate(self, params, region): return None\n'
        '    def run(self, ctx): return None\n'
        'def register():\n'
        '    return PluginDefinition(pipeline=P(), config_schema=ParamSchema((\n'
        '        ParamSpec(key="n", type="int", label="N", min=1, max=9),\n'
        '        ParamSpec(key="mode", type="str", label="M", default="fast"),\n'
        '        ParamSpec(key="token", type="credential", label="T",\n'
        '                  required=False),\n'
        '    )))\n'))
    registry.load_all()
    assert registry.get_record('cfg').load_error == ''
    assert registry.set_config('cfg', {'n': 99}) != {}       # 越界 → 错误表
    assert registry.get_config('cfg') == {}                   # 没落盘
    assert registry.set_config('cfg', {'n': '3', 'token': None}) == {}
    assert registry.get_config('cfg') == {'n': 3, 'mode': 'fast'}
    assert registry.set_config('nope', {}) == {'_': '未知插件'}


def test_config_schema_works_without_a_pipeline(db, tmp_path, monkeypatch):
    """只有 `sources` 能力的插件，配置一样要过 schema 校验。

    schema 曾经只从 `pipeline.config_schema()` 上找，于是天地图这类
    `PluginDefinition(sources=...)` 的纯数据源插件配置**完全不过校验**：
    `{"tokn": ...}` 拼错键名照单全收 → `resolve_reference` 返回 ''
    → URL 里那段变空 → 每块瓦片 401，而没有任何地方告诉用户键名拼错了。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'srconly', caps='["sources"]', body=(
        'from src.plugins.protocols import (ParamSchema, ParamSpec,\n'
        '                                   PluginDefinition, SourceDescriptor)\n'
        'def register():\n'
        '    return PluginDefinition(\n'
        '        sources=(SourceDescriptor(source_id="a", name="A",\n'
        '                                  url_template="https://h/{z}/{x}/{y}",\n'
        '                                  max_zoom=18,\n'
        '                                  credential_key="token"),),\n'
        '        config_schema=ParamSchema((\n'
        '            ParamSpec(key="token", type="credential", label="T",\n'
        '                      required=True),\n'
        '        )))\n'))
    registry.load_all()
    assert registry.get_record('srconly').load_error == ''
    # 拼错的键名必须被拦下并**指名道姓**，而不是静默落库。
    errors = registry.set_config('srconly', {'tokn': 'x'})
    assert 'tokn' in errors and errors['tokn'] == 'unknown param'
    assert registry.get_config('srconly') == {}
    assert registry.set_config('srconly', {'token': 'real'}) == {}
    assert registry.get_config('srconly') == {'token': 'real'}


def test_get_config_ignores_non_object_json(db, tmp_path, monkeypatch):
    """config_json 是合法 JSON 但不是对象时返回 {}，别让下游 cfg.get 炸。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'weird')
    registry.load_all()
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE plugins SET config_json = '[1,2]' WHERE id = 'weird'")
        conn.commit()
    finally:
        conn.close()
    assert registry.get_config('weird') == {}


# --------------------------------------------- C1：凭据取值类型必须归一

def test_credential_value_is_text_on_hit_and_miss(db, tmp_path, monkeypatch):
    """命中缓存与未命中必须返回同一个值、同一个类型（都是 str）。

    两条 return 各写一份归一化的后果是「第一张瓦片成功、TTL 内之后全部
    TypeError」：下游是 url.replace('{credential}', v)。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'typed')
    registry.load_all()
    registry.set_config('typed', {'num': 12345, 'tok': 'abc',
                                  'flag': False, 'zero': 0, 'nil': None,
                                  'obj': {'a': 1}})
    keys = ('num', 'tok', 'flag', 'zero', 'nil', 'obj', 'absent')
    miss = {}
    for k in keys:
        credentials.invalidate()                 # 每次都强制走未命中路径
        miss[k] = credentials.resolve_reference(f'plugin:typed:{k}')
    hit = {k: credentials.resolve_reference(f'plugin:typed:{k}') for k in keys}
    assert miss == hit
    assert all(isinstance(v, str) for v in hit.values())
    assert hit['num'] == '12345' and hit['tok'] == 'abc'
    assert hit['zero'] == '0'                  # 数值照实 str 化，不吞
    # 布尔/结构化值不可能是凭据：给空串，别把 'False'/"{'a': 1}" 拼进 URL
    assert hit['flag'] == '' and hit['obj'] == ''
    assert hit['nil'] == '' and hit['absent'] == ''
    tpl = 'https://h/{z}/{x}/{y}?k={credential}'
    assert tpl.replace('{credential}', hit['num']).endswith('k=12345')


def test_invalidate_during_read_does_not_refill_stale(db, tmp_path, monkeypatch):
    """读 DB 是在锁外做的：期间用户改了配置，这次的读法不许回填缓存。

    否则旧 token 会再活满一个 TTL —— 用户刚换过凭据，下载线程继续 401 一分钟。
    """
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'race')
    registry.load_all()
    registry.set_config('race', {'token': 'old'})
    credentials.invalidate()

    from src.core import database as database_mod
    real_get_connection = database_mod.get_connection

    def racing_get_connection(*a, **kw):
        conn = real_get_connection(*a, **kw)
        credentials.invalidate('race')       # 模拟：读 DB 期间配置被改
        return conn

    monkeypatch.setattr(database_mod, 'get_connection', racing_get_connection)
    assert credentials.resolve_reference('plugin:race:token') == 'old'
    assert 'race' not in credentials._CACHE   # 过期的读法没被写进缓存


# --------------------------- C2：外部插件不许顶替已在册的 id

def test_external_cannot_hijack_builtin_id(db, tmp_path, monkeypatch):
    """撞 builtin id 的外部插件必须被跳过，builtin 记录原样保留。"""
    _fresh(tmp_path, monkeypatch)
    victim_id = registry._BUILTIN[-1].rsplit('.', 1)[-1]
    d = tmp_path / 'plugins' / 'evil'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        f'id = "{victim_id}"\nname = "evil"\nversion = "9"\n'
        'api_version = "1"\ncapabilities = ["hook"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(
        'from pathlib import Path\n'
        'Path(__file__).with_name("ran.txt").write_text("x")\n'
        'from src.plugins.protocols import PluginDefinition\n'
        'def register():\n    return PluginDefinition()\n', encoding='utf-8')
    registry.load_all()
    held = registry.get_record(victim_id)
    assert held.origin == 'builtin' and held.manifest.name != 'evil'
    assert not (d / 'ran.txt').exists()          # 冒名者的代码根本没跑
    rec = registry.get_record('evil')            # 错误落在目录名这把 key 上
    assert rec is not None and rec.definition is None and '占用' in rec.load_error
    conn = sqlite3.connect(db)
    try:
        row = conn.execute('SELECT origin FROM plugins WHERE id = ?',
                           (victim_id,)).fetchone()
    finally:
        conn.close()
    assert row[0] == 'builtin'                   # DB 行没被就地改写


def test_hijacker_inherits_neither_switch_nor_credentials(db, tmp_path,
                                                          monkeypatch):
    """顶替的真正代价：enabled 与 config_json 按 id 取，冒名者会继承凭据。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'victim')
    registry.load_all()
    registry.set_enabled('victim', True)
    registry.set_config('victim', {'token': 'sekret'})
    # 目录名排在 victim 之后，保证 victim 先在册（先到者赢）
    d = tmp_path / 'plugins' / 'zzimpostor'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id = "victim"\nname = "impostor"\nversion = "9"\n'
        'api_version = "1"\ncapabilities = ["hook"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(_PLAIN_BODY, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    rec = registry.get_record('victim')
    assert rec.manifest.name == 'victim' and rec.root.name == 'victim'
    assert rec.enabled is True and rec.load_error == ''
    assert registry.get_config('victim') == {'token': 'sekret'}
    assert credentials.resolve_reference('plugin:victim:token') == 'sekret'
    impostor = registry.get_record('zzimpostor')
    assert impostor.definition is None and '占用' in impostor.load_error
    assert impostor.enabled is False             # 没继承 victim 的开关


# ------------------- I1：两条源腿都要能取到快照

def test_snapshot_covers_static_and_provider_sources(db, tmp_path, monkeypatch):
    """list_sources 合并列出静态描述符与 provider，取快照必须两条腿都走。"""
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'both', caps='["sources"]', body=(
        'from src.contracts.source import SourceSnapshot\n'
        'from src.plugins.protocols import PluginDefinition, SourceDescriptor\n'
        'class Prov:\n'
        '    def list_sources(self):\n'
        '        return (SourceDescriptor(source_id="dyn", name="D",\n'
        '                                 url_template="https://d/{z}/{x}/{y}",\n'
        '                                 max_zoom=10),)\n'
        '    def snapshot(self, source_id, cfg):\n'
        '        if source_id != "dyn":\n'
        '            raise KeyError(source_id)\n'
        '        return SourceSnapshot(source_id="plugin:both:dyn",\n'
        '                              url_template="https://d/{z}/{x}/{y}")\n'
        '    def authorize(self, headers, cfg): return None\n'
        'def register():\n'
        '    return PluginDefinition(\n'
        '        sources=(SourceDescriptor(source_id="static", name="S",\n'
        '                                  url_template="https://s/{z}/{x}/{y}",\n'
        '                                  max_zoom=12),),\n'
        '        source_provider=Prov())\n'))
    registry.load_all()
    registry.set_enabled('both', True)
    assert [s['source_id'] for s in registry.list_sources()] == ['static', 'dyn']
    assert registry.build_source_snapshot('both', 'static').source_id \
        == 'plugin:both:static'
    assert registry.build_source_snapshot('both', 'dyn').source_id \
        == 'plugin:both:dyn'
    with pytest.raises(KeyError):
        registry.build_source_snapshot('both', 'nope')
    registry.set_enabled('both', False)


def test_dispatch_event_survives_hook_exception(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'hooky', body=(
        'from pathlib import Path\n'
        'from src.plugins.protocols import PluginDefinition\n'
        'MARK = Path(__file__).with_name("mark.txt")\n'
        'class Bad:\n'
        '    def on_event(self, event): raise RuntimeError("hook boom")\n'
        'class Good:\n'
        '    def on_event(self, event): MARK.write_text("hit")\n'
        'def register():\n'
        '    return PluginDefinition(hooks=(Bad(), Good()))\n'))
    registry.load_all()
    registry.set_enabled('hooky', True)
    from src.plugins.protocols import TaskEvent

    registry.dispatch_event(TaskEvent(kind='task_completed', task_id=1,
                                      pipeline='plugin', plugin_id='hooky'))
    assert (tmp_path / 'plugins' / 'hooky' / 'mark.txt').read_text() == 'hit'


def test_builtin_failures_are_isolated_not_fatal(db, tmp_path, monkeypatch):
    """一个 builtin 模块缺失只能落 load_error，不许抛穿 load_all。

    四个首发插件（T12-T15）现在都真的在树上，拿真名字已经构造不出这个失败，
    所以往 _BUILTIN 末尾塞一个不存在的模块名把不变量钉住：缺的那个有 builtin
    行 + 非空 load_error，邻居插件不受影响。
    """
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, '_BUILTIN',
                        registry._BUILTIN + ('src.plugins.builtin.nope',))
    registry.load_all()                                  # 不抛
    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute('SELECT id, origin FROM plugins').fetchall())
    finally:
        conn.close()
    assert rows.get('nope') == 'builtin'                 # 退化用模块名登记
    assert registry.get_record('nope').load_error != ''
    # 隔离：缺的那个不许带倒邻居。挑 tianditu 而不是数「干净加载的有几个」——
    # 后者会跟着 _BUILTIN 的落地进度飘，而这条不变量与进度无关。
    neighbor = registry.get_record('tianditu')
    assert neighbor is not None and neighbor.load_error == ''
