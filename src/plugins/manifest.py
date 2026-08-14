"""plugin.toml 的解析与校验。external 插件从 TOML 读；builtin 插件从
模块里的 MANIFEST dict 读——同一个校验函数，同一批错误。"""

from __future__ import annotations

import platform
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

_ID_RE = re.compile(r'^[a-z][a-z0-9_\-]{0,63}$')
_CAPABILITIES = frozenset({'sources', 'pipeline', 'exporter', 'hook'})
_PERMISSIONS = frozenset({'network', 'filesystem', 'subprocess'})
_WIN_DRIVE_RE = re.compile(r'^[A-Za-z]:')
_PATH_SEP_RE = re.compile(r'[/\\]')


class ManifestError(ValueError):
    """插件清单非法。registry 捕获后写 plugins.load_error，绝不向上抛穿启动。"""


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    api_version: str
    capabilities: Tuple[str, ...] = ()
    entry: str = 'plugin.py'
    requires_abi: str = ''          # 如 'cp312-linux-x86_64'；'' = 纯 Python
    permissions: Tuple[str, ...] = ()
    ui_assets: Tuple[str, ...] = ()
    description: str = ''


def current_abi_tag() -> str:
    """带二进制 vendor 的插件在 manifest 声明 requires_abi，不匹配拒载——
    否则用户拿到的是一段看不懂的 ImportError。"""
    return (f'cp{sys.version_info.major}{sys.version_info.minor}'
            f'-{sys.platform}-{platform.machine() or "unknown"}')


def _str_tuple(value: Any, field: str) -> Tuple[str, ...]:
    """把清单里的字符串数组字段收成 tuple。类型不对一律 ManifestError:
    字符串自己就可迭代,放过去会被逐字符拆开(assets = "panel.js" 会变成 8 个
    单字符资产,静悄悄污染白名单),所以 str/bytes 必须当错误拦掉。"""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ManifestError(
            f'{field} 必须是字符串数组，实际是 {type(value).__name__}')
    return tuple(str(v) for v in value)


def _reject_escaping_asset(a: str) -> None:
    """资产服务路由拿 ui.assets 当白名单,这是第一道门:绝对路径与 .. 一律拒。

    不能用 pathlib 判 —— Linux 上的 Path 不认 Windows 语义,`..\\evil.js` 和
    `C:/evil.js` 都会被当成一个普通文件名放过去。所以按两种分隔符自己切,
    并显式认盘符与 UNC 前缀。
    """
    if a.startswith('/') or a.startswith('\\') or _WIN_DRIVE_RE.match(a):
        raise ManifestError(f'ui.assets 不许用绝对路径：{a!r}')
    if '..' in _PATH_SEP_RE.split(a):
        raise ManifestError(f'ui.assets 不许越出插件目录：{a!r}')


def manifest_from_dict(d: Mapping) -> PluginManifest:
    if not isinstance(d, Mapping):
        raise ManifestError('manifest must be a table/dict')
    pid = str(d.get('id') or '')
    if not _ID_RE.match(pid):
        raise ManifestError(
            f'非法插件 id：{pid!r}（小写字母/数字/中划线/下划线，字母开头）')
    name = str(d.get('name') or '').strip()
    version = str(d.get('version') or '').strip()
    api_version = str(d.get('api_version') or '').strip()
    if not name or not version or not api_version:
        raise ManifestError('name / version / api_version 均必填')
    caps = _str_tuple(d.get('capabilities'), 'capabilities')
    unknown = set(caps) - _CAPABILITIES
    if unknown:
        raise ManifestError(f'未知 capabilities：{sorted(unknown)}')
    perms = _str_tuple(d.get('permissions'), 'permissions')
    bad_perms = set(perms) - _PERMISSIONS
    if bad_perms:
        raise ManifestError(f'未知 permissions：{sorted(bad_perms)}')
    ui = d.get('ui')
    if ui is None:
        ui = {}
    if not isinstance(ui, Mapping):
        raise ManifestError(f'ui 必须是 table，实际是 {type(ui).__name__}')
    assets = _str_tuple(ui.get('assets'), 'ui.assets')
    for a in assets:
        _reject_escaping_asset(a)
    return PluginManifest(
        plugin_id=pid, name=name, version=version, api_version=api_version,
        capabilities=caps, entry=str(d.get('entry') or 'plugin.py'),
        requires_abi=str(d.get('requires_abi') or ''),
        permissions=perms, ui_assets=assets,
        description=str(d.get('description') or ''))


def load_manifest_toml(path: Path) -> PluginManifest:
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f'plugin.toml 读取/解析失败：{e}') from e
    return manifest_from_dict(data)
