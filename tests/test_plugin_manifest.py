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
