"""天地图源插件：描述符形状、快照冻结、凭据引用。

这个插件是「纯数据」路线的样板：WMTS 的 `tk` 落在宿主的 `{credential}`
占位符上，所以一行逻辑都没有。因此本文件钉的是**数据本身**：
  · 模板 format 之后 {z}/{x}/{y}/{s}/{credential} 必须还是字面量占位符
    （双花括号转义写错的话，测试之外要等到第一次真下载才发现）；
  · 凭据只以键名形态出现在快照里，真值只在 plugins.config_json 里；
  · 未启用时不暴露源（启停是能力开关，不是加载开关）。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.plugins import credentials, registry  # noqa: E402
from src.plugins.builtin import tianditu_source  # noqa: E402
from src.plugins.manifest import manifest_from_dict  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库。写法同 tests/test_plugin_registry.py:20-39（conftest 无 db）。

    BASE_DIR 一起指到 tmp_path：registry._plugins_root() 由它派生，否则
    load_all() 会连带扫开发机上真实的 plugins/ 目录。
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


@pytest.fixture
def loaded(db):
    """load_all() 之后启用 tianditu，收尾复位注册表（含凭据缓存）。"""
    registry.reset_for_tests()
    registry.load_all()
    yield registry
    registry.reset_for_tests()


def test_builtin_plugin_loads(loaded):
    rec = registry.get_record('tianditu')
    assert rec is not None and rec.origin == 'builtin'
    assert rec.load_error == ''
    assert 'sources' in rec.manifest.capabilities


def test_manifest_passes_validation():
    """MANIFEST 走的是 external 插件同一个校验器——id 形状、能力/权限白名单。"""
    m = manifest_from_dict(tianditu_source.MANIFEST)
    assert m.plugin_id == 'tianditu'
    assert m.capabilities == ('sources',) and m.permissions == ('network',)


def test_sources_listed_only_when_enabled(loaded):
    assert [s for s in registry.list_sources()
            if s['plugin_id'] == 'tianditu'] == []
    registry.set_enabled('tianditu', True)
    sources = [s for s in registry.list_sources()
               if s['plugin_id'] == 'tianditu']
    assert {s['source_id'] for s in sources} == {'img', 'cia'}
    assert all(s['needs_credential'] for s in sources)
    registry.set_enabled('tianditu', False)


def test_snapshot_shape(loaded):
    registry.set_enabled('tianditu', True)
    snap = registry.build_source_snapshot('tianditu', 'img')
    assert snap.credential_reference == 'plugin:tianditu:token'
    assert '{credential}' in snap.url_template
    assert '{z}' in snap.url_template and '{s}' in snap.url_template
    assert snap.subdomains
    assert '天地图' in snap.attribution
    registry.set_enabled('tianditu', False)


def test_template_placeholders_survive_format():
    """双花括号转义写错 → format 之后占位符没了/剩下 `{{`，源直接是死的。

    连 WMTS 的行列映射一起钉住：TILEMATRIX=z、TILEROW=y、TILECOL=x。写反
    行列不会报错，只会静默拿到转置的瓦片。
    """
    for d in tianditu_source.register().sources:
        tpl = d.url_template
        for token in ('{z}', '{x}', '{y}', '{s}', '{credential}'):
            assert token in tpl, (d.source_id, token, tpl)
        assert '{{' not in tpl and '}}' not in tpl
        assert 'TILEMATRIX={z}&TILEROW={y}&TILECOL={x}' in tpl
        assert 'tk={credential}' in tpl
        assert tpl.startswith('https://t{s}.tianditu.gov.cn/')

    by_id = {d.source_id: d for d in tianditu_source.register().sources}
    assert '/img_w/wmts?' in by_id['img'].url_template
    assert 'LAYER=img&' in by_id['img'].url_template
    assert '/cia_w/wmts?' in by_id['cia'].url_template
    assert 'LAYER=cia&' in by_id['cia'].url_template


def test_credential_lives_only_in_config_json(loaded, db):
    """真值只在 plugins.config_json 里；快照的任何字段都不许出现它。

    这是上一轮评审定下的硬约束：宿主无法判断一个字符串是不是密码，所以
    序列化层的 type=='credential' 过滤只是纵深防御，不是依赖项。插件侧的
    保证是「token 从来没进过描述符/参数」。
    """
    registry.set_enabled('tianditu', True)
    assert registry.set_config('tianditu', {'token': 'SEKRET-TK'}) == {}

    snap = registry.build_source_snapshot('tianditu', 'cia')
    assert 'SEKRET-TK' not in snap.to_json()
    assert snap.credential_reference == 'plugin:tianditu:token'
    assert '{credential}' in snap.url_template

    # 反面：引用确实能解析出真值（否则上面的断言靠“压根没配”也能过）。
    assert credentials.resolve_reference('plugin:tianditu:token') == 'SEKRET-TK'
    registry.set_enabled('tianditu', False)


def test_no_credential_shaped_params_declared():
    """纯数据插件不该声明任何参数——凭据尤其不许经参数进任务行/日志。"""
    definition = tianditu_source.register()
    assert definition.pipeline is None and definition.source_provider is None
    assert definition.exporters == () and definition.hooks == ()
    assert {d.credential_key for d in definition.sources} == {'token'}
