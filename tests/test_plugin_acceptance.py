"""规格 §15 的验收标准（能测试化的四条）。

六条标准与证据的对应关系：

| # | 标准 | 覆盖 |
| - | ---- | ---- |
| 1 | 四个首发插件端到端跑通、产物落盘且登记进 artifacts | 本文件的 `test_all_four_extension_points_wire_up`（四类扩展点真的接得上）+ `tests/test_plugin_mvt.py::test_run_writes_pbf_mbtiles`（真跑一趟、真落 MBTiles、`artifacts` 里真有 `pipeline='plugin'` 那一行） |
| 2 | 关掉全部插件 → 系统行为与今天一致 | 本文件的 `test_all_plugins_disabled_keeps_core_intact` |
| 3 | 删掉插件目录 → 历史任务仍能显示、能删、能清产物 | 本文件的 `test_deleted_plugin_history_survives_and_stays_deletable` |
| 4 | import 期抛异常的插件不炸宿主 | `tests/test_plugin_registry.py::test_import_error_isolated` |
| 5 | 打包产物里 in-tree 插件可用、external 插件可从 exe 旁加载 | 静态部分由 `tests/test_plugin_nuitka_reachability.py` 钉住（漏登记就是打包丢模块）；frozen 冒烟是**发版前手工步骤**，见 `RELEASE_NOTES.md` 的「发版前手工验证」 |
| 6 | api_version major 不匹配拒载且原因可见 | `tests/test_plugin_registry.py::test_api_major_mismatch_rejected` |

注册表是**进程全局**的（`_RECORDS` / `sys.path` / `sys.modules`），而本文件会把
四个 builtin 插件全部启用。不还原就会漏给后面的测试文件：`list_sources()`
突然多出两个天地图源、`exporter_for('gpkg')` 突然非 None。所以每条用例结束都
`reset_for_tests()`。
"""

import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.plugins import registry  # noqa: E402

#: 四个首发插件的 id（`registry._BUILTIN` 的模块名对应的 MANIFEST id）。
FIRST_WAVE = ('artifact_meta', 'gpkg', 'mvt', 'tianditu')


@pytest.fixture(autouse=True)
def restore_registry():
    """进程全局的注册表在每条用例后还原——启用状态不许漏给别的测试文件。"""
    yield
    registry.reset_for_tests()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库 + 空的 external 插件根（只剩四个 builtin）。

    conftest.py 没有 `db` fixture（只有 autouse 的隔离夹具），按
    `tests/test_plugin_registry.py:20-39` 的既有写法在本文件里建一个。
    `BASE_DIR` 指到 tmp_path 顺带把 `_plugins_root()` 指空，仓库根真有
    `plugins/` 时结果也不会跟着变。
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


def _enable_all():
    for rec in registry.list_records():
        registry.set_enabled(rec.manifest.plugin_id, True)


# ---------------------------------------------------------------- 验收 1

def test_registry_has_all_four_first_wave(db):
    """四个首发插件全部在册且无加载错误。

    `load_error != ''` 的插件在界面上照样看得见（隔离铁律的另一半），所以
    「列表里有」不足以说明它是好的——错误串必须是空的。
    """
    registry.load_all()
    for pid in FIRST_WAVE:
        rec = registry.get_record(pid)
        assert rec is not None, f'{pid} 未注册'
        assert rec.load_error == '', f'{pid}: {rec.load_error}'
        assert rec.origin == 'builtin'
        assert rec.definition is not None


def test_all_four_extension_points_wire_up(db):
    """四类扩展点各有一个首发插件真的接上了宿主的查询入口。

    这条才是「通用框架」而不是「四个特例」的证据：宿主一侧四个入口
    （`list_sources` / `get_pipeline` / `exporter_for` / `iter_hooks`）分别被
    sources / pipeline / exporter / hook 四类能力填满，且填它们的是四个互不
    相干的插件。少任何一类，框架就只是「某个具体功能的活动板房」。
    """
    registry.load_all()
    _enable_all()

    # ① sources：天地图两个源（影像 + 注记），都声明需要凭据
    sources = registry.list_sources()
    tianditu = {s['source_id']: s for s in sources
                if s['plugin_id'] == 'tianditu'}
    assert sorted(tianditu) == ['cia', 'img'], sources
    assert all(s['needs_credential'] for s in tianditu.values())
    assert all(s['max_zoom'] == 18 for s in tianditu.values())

    # 凭据缝：快照里存的是**键名**，URL 模板里留的是 `{credential}` 字面量
    snapshot = registry.build_source_snapshot('tianditu', 'img')
    assert snapshot.credential_reference == 'plugin:tianditu:token'
    assert '{credential}' in snapshot.url_template
    assert 'tk=' in snapshot.url_template

    # ② pipeline：mvt
    from src.plugins.protocols import Exporter, PipelinePlugin, TaskHook

    pipeline = registry.get_pipeline('mvt')
    assert pipeline is not None
    assert isinstance(pipeline, PipelinePlugin)
    assert pipeline.params_schema().keys()      # 声明式表单非空

    # ③ exporter：gpkg
    exporter = registry.exporter_for('gpkg')
    assert exporter is not None
    assert isinstance(exporter, Exporter)
    assert 'gpkg' in registry.list_export_formats()
    assert dict(registry.iter_exporters())['gpkg'] is exporter

    # ④ hook：artifact_meta
    hooks = dict(registry.iter_hooks())
    assert 'artifact_meta' in hooks
    assert isinstance(hooks['artifact_meta'], TaskHook)


# ---------------------------------------------------------------- 验收 2

def test_all_plugins_disabled_keeps_core_intact(isolated_app, tmp_path,
                                                monkeypatch):
    """全部插件关闭 → 四个扩展点一齐熄灯，核心 REST 照旧。

    「与今天逐字一致」在测试里的可判形式：宿主侧四个查询入口全空 + 任务中心
    的五路 UNION 里没有 plugin 段的行。插件**仍在列表里**（可见但不生效）——
    关插件不等于卸插件。
    """
    from src.plugins import registry as reg

    # external 根指到不存在的目录：只剩四个 builtin，仓库根真有 plugins/ 时
    # 这条用例的结果也不跟着变。
    monkeypatch.setattr(reg, '_plugins_root', lambda: tmp_path / 'nowhere')
    reg.reset_for_tests()
    reg.load_all()
    for rec in reg.list_records():
        reg.set_enabled(rec.manifest.plugin_id, False)

    client = isolated_app.app.test_client()

    # 宿主侧：四类能力一个都不暴露
    assert reg.list_sources() == []
    assert reg.get_pipeline('mvt') is None
    assert reg.exporter_for('gpkg') is None
    assert reg.list_export_formats() == ()
    assert list(reg.iter_hooks()) == []

    # API 侧：同一件事的外部可观测面
    assert client.get('/api/plugins/sources').get_json()['sources'] == []
    assert client.get('/api/plugins/mvt/schema').get_json()['params'] == []

    # 插件列表照样列全四个（带 enabled=False）——关不等于卸
    listed = client.get('/api/plugins').get_json()['plugins']
    assert sorted(p['id'] for p in listed) == list(FIRST_WAVE)
    assert not any(p['enabled'] for p in listed)
    assert not any(p['load_error'] for p in listed)

    # 任务中心：五路 UNION 仍然 200，且没有 plugin 段的行
    resp = client.get('/api/history_all')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert [t for t in payload['tasks'] if t['task_type'] == 'plugin'] == []

    # 导出：没有任何插件导出器，格式表为空且 400 时把空表说清楚
    bad = client.post('/api/plugins/export/1', json={'format': 'gpkg'})
    assert bad.status_code == 400
    assert bad.get_json()['supported_formats'] == []


# ---------------------------------------------------------------- 验收 3

_GONE_PLUGIN = '''
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   PluginOutcome)


class P:
    def params_schema(self):
        return ParamSchema((ParamSpec(key='depth', type='int', label='深度'),))

    def estimate(self, params, region):
        return None

    def run(self, ctx):
        return PluginOutcome.COMPLETED
'''


def _install_gone_plugin(root):
    d = root / 'plugins' / 'gone'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id = "gone"\nname = "已卸载的插件"\nversion = "0.1"\n'
        'api_version = "1"\ncapabilities = ["pipeline"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(
        _GONE_PLUGIN + '\n\ndef register():\n'
        '    return PluginDefinition(pipeline=P())\n', encoding='utf-8')
    return d


def test_deleted_plugin_history_survives_and_stays_deletable(
        isolated_app, tmp_path, monkeypatch):
    """插件目录删掉后，它的历史任务仍能显示、能删、能连产物一起清掉。

    这条守的是「插件的任务行归宿主管」：任务表、删除路径、产物登记都在宿主
    这一侧，插件消失只让**参数回显**降级（不知道哪个键是凭据就整份不给），
    不让任何一行变成删不掉的僵尸。
    """
    from src.plugins import registry as reg

    monkeypatch.setattr(reg, '_plugins_root', lambda: tmp_path / 'plugins')
    plugin_dir = _install_gone_plugin(tmp_path)
    reg.reset_for_tests()
    reg.load_all()
    assert reg.get_record('gone') is not None

    # 一条已完成的历史任务 + 一份落盘产物 + 一行产物登记
    out_root = tmp_path / 'out'
    task_dir = out_root / 'plugin_task_1'
    task_dir.mkdir(parents=True)
    product = task_dir / 'demo.mbtiles'
    product.write_bytes(b'x' * 16)

    # `isolated_app` 把 Config.DATABASE_PATH 指到了 tmp_path 下的库。
    from src.core.config import Config
    dbpath = str(Config.DATABASE_PATH)
    conn = sqlite3.connect(dbpath)
    conn.execute(
        "INSERT INTO plugin_tasks (id, plugin_id, name, status, output_path,"
        " north, south, east, west, zoom_min, zoom_max, params_json)"
        " VALUES (1, 'gone', '旧任务', 'completed', ?, 31, 30, 121.1, 121,"
        " 3, 5, '{\"depth\": 3, \"token\": \"SECRET\"}')",
        (str(out_root),))
    conn.execute(
        "INSERT INTO artifacts (pipeline, task_id, kind, path, format,"
        " bytes_total) VALUES ('plugin', 1, 'mbtiles', ?, 'pbf', 16)",
        (str(product),))
    conn.commit()
    conn.close()

    # 插件目录删掉 → 重扫 → 注册表里没有它了
    import shutil
    shutil.rmtree(plugin_dir)
    reg.reset_for_tests()
    reg.load_all()
    assert reg.get_record('gone') is None

    client = isolated_app.app.test_client()

    # ① 仍能显示：插件任务列表与任务中心都还看得见这一行
    listed = client.get('/api/plugins/tasks').get_json()['tasks']
    assert [t['id'] for t in listed] == [1]
    assert listed[0]['plugin_id'] == 'gone' and listed[0]['name'] == '旧任务'
    # schema 拿不到 → 参数整份不外发（凭据宁可不显示也不能猜着给）
    assert 'params' not in listed[0]
    assert 'SECRET' not in json.dumps(listed[0], ensure_ascii=False)

    detail = client.get('/api/plugins/tasks/1')
    assert detail.status_code == 200
    history = client.get('/api/history_all').get_json()['tasks']
    assert [t['id'] for t in history if t['task_type'] == 'plugin'] == [1]

    # ② 能删、能清产物
    resp = client.delete('/api/plugins/tasks/1?delete_files=1')
    assert resp.status_code == 200 and resp.get_json()['success'] is True
    assert not task_dir.exists(), '产物目录未被清掉'

    conn = sqlite3.connect(dbpath)
    assert conn.execute('SELECT COUNT(*) FROM plugin_tasks').fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM artifacts"
                        " WHERE pipeline = 'plugin'").fetchone()[0] == 0
    conn.close()
