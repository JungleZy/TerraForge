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

@pytest.mark.parametrize('entry', [
    '../../etc/passwd.py',
    '/etc/passwd',
    'sub/../../escape.py',
])
def test_entry_escaping_plugin_dir_rejected(db, tmp_path, monkeypatch, entry):
    """清单层不拦 entry，加载器必须 resolve() + 包含判断后拒载（写错，不抛）。"""
    _fresh(tmp_path, monkeypatch)
    (tmp_path / 'escape.py').write_text(
        'raise AssertionError("越界 entry 被执行了")\n', encoding='utf-8')
    _write_external(tmp_path, 'escaper', entry=entry)
    registry.load_all()                          # 不抛
    rec = registry.get_record('escaper')
    assert rec.definition is None
    assert 'entry' in rec.load_error


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


def test_reset_for_tests_removes_vendor_paths(db, tmp_path, monkeypatch):
    """tmp_path 下的 vendor 目录不许在测试之间留在 sys.path 上遮蔽 import。"""
    _fresh(tmp_path, monkeypatch)
    d = _write_external(tmp_path, 'vend3')
    (d / 'vendor').mkdir()
    target = str((d / 'vendor').resolve())
    registry.load_all()
    assert target in sys.path
    registry.reset_for_tests()
    assert target not in sys.path


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
        'max_zoom': 18, 'attribution': 'A', 'needs_credential': True}]
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


def test_set_enabled_unknown_raises_keyerror(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    registry.load_all()
    with pytest.raises(KeyError):
        registry.set_enabled('nope', True)


def test_set_config_validates_against_pipeline_schema(db, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    _write_external(tmp_path, 'cfg', caps='["pipeline"]', body=(
        'from src.plugins.protocols import (ParamSchema, ParamSpec,\n'
        '                                   PluginDefinition)\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def config_schema(self):\n'
        '        return ParamSchema((ParamSpec(key="n", type="int",\n'
        '                                     label="N", min=1, max=9),))\n'
        '    def estimate(self, params, region): return None\n'
        '    def run(self, ctx): return None\n'
        'def register():\n    return PluginDefinition(pipeline=P())\n'))
    registry.load_all()
    assert registry.get_record('cfg').load_error == ''
    assert registry.set_config('cfg', {'n': 99}) != {}      # 越界 → 错误表
    assert registry.get_config('cfg') == {}                  # 没落盘
    assert registry.set_config('cfg', {'n': 3}) == {}
    assert registry.get_config('cfg') == {'n': 3}
    assert registry.set_config('nope', {}) == {'_': '未知插件'}


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
    """_BUILTIN 里列了尚未落地的模块（T12-T15）：只能落 load_error，不许抛穿。

    刻意不断言「全部失败」——T12-T15 落地后这些模块会真的加载成功；这里断言
    的是不变量：缺模块的那些必须有 builtin 行 + 非空 load_error。
    """
    import importlib.util

    _fresh(tmp_path, monkeypatch)
    registry.load_all()                                  # 不抛
    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute('SELECT id, origin FROM plugins').fetchall())
    finally:
        conn.close()
    missing = []
    for module_name in registry._BUILTIN:
        try:
            found = importlib.util.find_spec(module_name) is not None
        except (ImportError, AttributeError, ValueError):
            found = False
        if not found:
            missing.append(module_name.rsplit('.', 1)[-1])
    assert missing, '_BUILTIN 全部存在时本用例失去意义，请改断言'
    for pid in missing:
        assert rows.get(pid) == 'builtin'
        assert registry.get_record(pid).load_error != ''
