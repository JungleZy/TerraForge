"""plugin.toml 解析与校验：必填、id 形状、能力白名单、API 版本、ABI 标签。"""

import sys

import pytest

from src.plugins.manifest import (ManifestError, PluginManifest,
                                  current_abi_tag, load_manifest_toml,
                                  manifest_from_dict)

GOOD = {'id': 'demo', 'name': '演示', 'version': '0.1.0',
        'api_version': '1', 'capabilities': ['pipeline']}


def test_minimal_manifest_ok():
    m = manifest_from_dict(GOOD)
    assert m.plugin_id == 'demo' and m.entry == 'plugin.py' \
        and m.requires_abi == '' and m.ui_assets == ()


def test_id_shape_enforced():
    for bad in ('', 'A', 'a b', '../x', 'a/b', '中文'):
        with pytest.raises(ManifestError):
            manifest_from_dict({**GOOD, 'id': bad})


def test_capabilities_whitelist():
    with pytest.raises(ManifestError):
        manifest_from_dict({**GOOD, 'capabilities': ['root_shell']})


def test_api_version_required():
    with pytest.raises(ManifestError):
        manifest_from_dict({**GOOD, 'api_version': ''})


def test_toml_roundtrip(tmp_path):
    p = tmp_path / 'plugin.toml'
    p.write_text(
        'id = "demo"\nname = "演示"\nversion = "0.1.0"\n'
        'api_version = "1"\ncapabilities = ["pipeline"]\n'
        '[ui]\nassets = ["panel.js"]\n', encoding='utf-8')
    m = load_manifest_toml(p)
    assert m.ui_assets == ('panel.js',)


def test_ui_assets_traversal_rejected():
    with pytest.raises(ManifestError):
        manifest_from_dict({**GOOD, 'ui': {'assets': ['../evil.js']}})


def test_abi_tag_format():
    tag = current_abi_tag()
    assert tag.startswith(f'cp{sys.version_info.major}{sys.version_info.minor}-')


# —— 类型闸：坏类型必须变成 ManifestError(ValueError)，不能是 AttributeError /
# TypeError。registry 只 except ManifestError/ValueError，别的异常会抛穿启动。

def test_ui_must_be_table():
    with pytest.raises(ManifestError, match='ui'):
        manifest_from_dict({**GOOD, 'ui': 'panel.js'})


def test_capabilities_must_be_array():
    with pytest.raises(ManifestError, match='capabilities'):
        manifest_from_dict({**GOOD, 'capabilities': 7})


def test_capabilities_string_not_split_into_chars():
    """capabilities = "pipeline" 是常见手误，不能被逐字符拆成 ['p','i',...]。"""
    with pytest.raises(ManifestError, match='capabilities 必须是字符串数组'):
        manifest_from_dict({**GOOD, 'capabilities': 'pipeline'})


def test_permissions_must_be_array():
    with pytest.raises(ManifestError, match='permissions'):
        manifest_from_dict({**GOOD, 'permissions': 'network'})


def test_ui_assets_must_be_array():
    """字符串会被拆成单字符资产，静悄悄污染白名单——必须报错。"""
    with pytest.raises(ManifestError, match=r'ui\.assets 必须是字符串数组'):
        manifest_from_dict({**GOOD, 'ui': {'assets': 'panel.js'}})


# —— 资产白名单：绝对路径与 .. 一律拒，Windows 语义也要拦（Linux 上的 pathlib
# 不认盘符和反斜杠，只判 Path.parts 会漏）。

@pytest.mark.parametrize('asset', [
    'C:/evil.js', r'C:\evil.js', '/etc/passwd', r'\\srv\share\x.js'])
def test_ui_assets_absolute_rejected(asset):
    with pytest.raises(ManifestError, match=r'ui\.assets'):
        manifest_from_dict({**GOOD, 'ui': {'assets': [asset]}})


@pytest.mark.parametrize('asset', [
    '../evil.js', r'..\evil.js', 'a/../../evil.js', r'a\..\..\evil.js'])
def test_ui_assets_traversal_rejected_both_separators(asset):
    with pytest.raises(ManifestError, match=r'ui\.assets'):
        manifest_from_dict({**GOOD, 'ui': {'assets': [asset]}})


def test_nested_asset_path_allowed():
    """收紧的是越界，不是子目录——sub/panel.js 必须还能用。"""
    m = manifest_from_dict({**GOOD, 'ui': {'assets': ['sub/panel.js']}})
    assert m.ui_assets == ('sub/panel.js',)
