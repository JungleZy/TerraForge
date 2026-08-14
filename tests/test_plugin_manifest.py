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


# —— 资产/入口路径：写成允许清单。绝对路径、Windows 语义（Linux 上的 pathlib
# 不认盘符和反斜杠）、协议前缀、`~`、空白与纯点号段一律拒。

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


@pytest.mark.parametrize('asset', [
    'http://evil/x.js',        # 'http:' 曾经从单字母盘符闸底下走过去
    'https://evil/x.js',
    '~/x.js',                  # shell 展开成家目录
    '.. /evil.js',             # 尾随空格伪装的 ..
    '....//evil.js',           # 某些归一化会把它折成 ../
    '',                        # 空串指向插件目录本身
    '   ',                     # 纯空白
    'a\tb.js',                 # 制表符
    'panel.js ',               # 尾随空格：和 panel.js 是两个不同的文件名
])
def test_ui_assets_allowlist_rejects_the_rest(asset):
    with pytest.raises(ManifestError, match=r'ui\.assets'):
        manifest_from_dict({**GOOD, 'ui': {'assets': [asset]}})


@pytest.mark.parametrize('entry', [
    '../../etc/passwd.py', r'..\..\evil.py', '/abs/plugin.py', 'C:/evil.py',
    'http://evil/p.py', '~/p.py', 'a b.py',
])
def test_entry_path_rejected(entry):
    """entry 是加载器要 import 的文件，类型闸/路径闸不能只给 ui.assets。"""
    with pytest.raises(ManifestError, match='entry'):
        manifest_from_dict({**GOOD, 'entry': entry})


def test_entry_must_not_be_a_table():
    """{'a': 1} 被 str() 成 "{'a': 1}" 当文件名存下来，是静默的坏数据。"""
    with pytest.raises(ManifestError, match='entry'):
        manifest_from_dict({**GOOD, 'entry': {'a': 1}})


def test_entry_subdir_allowed():
    m = manifest_from_dict({**GOOD, 'entry': 'pkg/main.py'})
    assert m.entry == 'pkg/main.py'


@pytest.mark.parametrize('entry', [None, ''])
def test_entry_falls_back_to_default(entry):
    """entry 缺省或写成空串都用 plugin.py——空不是错，是「没写」。"""
    m = manifest_from_dict({**GOOD, 'entry': entry})
    assert m.entry == 'plugin.py'


# —— 必填字段：报错必须点名是哪个空的，别让插件作者三选一猜。

@pytest.mark.parametrize('field', ['name', 'version', 'api_version'])
def test_missing_field_names_itself(field):
    with pytest.raises(ManifestError, match=f'必填字段为空.*{field}'):
        manifest_from_dict({**GOOD, field: ''})


def test_missing_fields_listed_together():
    with pytest.raises(ManifestError) as ei:
        manifest_from_dict({**GOOD, 'name': '', 'version': '  '})
    assert 'name' in str(ei.value) and 'version' in str(ei.value)


# —— 非 UTF-8 的 plugin.toml：中文 Windows 上记事本默认存 GBK，是首周就会撞上的
# 事故。tomllib.load() 先 decode 再 parse，UnicodeDecodeError 既不是 OSError
# 也不是 TOMLDecodeError，漏了它用户看到的是一段 codec 报错。

def test_non_utf8_toml_becomes_manifest_error(tmp_path):
    p = tmp_path / 'plugin.toml'
    p.write_bytes(
        'id = "demo"\nname = "演示"\nversion = "0.1.0"\n'
        'api_version = "1"\n'.encode('gbk'))
    with pytest.raises(ManifestError, match='读取/解析失败'):
        load_manifest_toml(p)


def test_malformed_and_missing_toml_become_manifest_error(tmp_path):
    bad = tmp_path / 'plugin.toml'
    bad.write_text('id = "demo"\n[[[oops\n', encoding='utf-8')
    with pytest.raises(ManifestError, match='读取/解析失败'):
        load_manifest_toml(bad)
    with pytest.raises(ManifestError, match='读取/解析失败'):
        load_manifest_toml(tmp_path / 'nope.toml')
