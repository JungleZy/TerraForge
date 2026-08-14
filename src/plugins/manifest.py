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
_SAFE_SEG_RE = re.compile(r'^[A-Za-z0-9._\-]+$')
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


def _reject_escaping_path(value: str, field: str) -> None:
    """路径字段只许「插件目录内的相对路径」——写成允许清单,不是拒绝清单。

    资产路由拿 ui.assets 当白名单,加载器拿 entry 当要 import 的文件,两个都是
    第一道门。拒绝清单补不完:绝对路径、盘符、UNC、`..\\`、`~`、`http://`、
    尾随空格的 `.. /`、`....//`、空串 —— 每一类都得单独一条分支,漏一条就是一个
    洞(前两轮就是这么漏的)。所以反过来写:按两种分隔符切段,每段必须是安全字符
    组成的非空段且不是纯点号段,其余一律拒。

    这只是声明期检查。真正的落地检查(resolve() + 目录包含判断)必须由资产路由和
    加载器在请求期/加载期再做一次 —— 符号链接、大小写不敏感文件系统、URL 编码的
    %2e%2e 都不在清单层的视野里。
    """
    if not value:
        raise ManifestError(f'{field} 不许为空')
    for seg in _PATH_SEP_RE.split(value):
        if not _SAFE_SEG_RE.match(seg) or seg.strip('.') == '':
            raise ManifestError(
                f'{field} 只许插件目录内的相对路径（每段限字母/数字/点/下划线/'
                f'中划线，不许空段、纯点号段、空白、盘符或协议前缀）：{value!r}')


def manifest_from_dict(d: Mapping) -> PluginManifest:
    if not isinstance(d, Mapping):
        raise ManifestError('manifest must be a table/dict')
    pid = str(d.get('id') or '')
    if not _ID_RE.match(pid):
        raise ManifestError(
            f'非法插件 id：{pid!r}（小写字母/数字/中划线/下划线，字母开头）')
    fields = {k: str(d.get(k) or '').strip()
              for k in ('name', 'version', 'api_version')}
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise ManifestError(f'必填字段为空：{missing}')
    name, version, api_version = (fields['name'], fields['version'],
                                  fields['api_version'])
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
        _reject_escaping_path(a, 'ui.assets')
    entry = str(d.get('entry') or 'plugin.py')
    _reject_escaping_path(entry, 'entry')
    return PluginManifest(
        plugin_id=pid, name=name, version=version, api_version=api_version,
        capabilities=caps, entry=entry,
        requires_abi=str(d.get('requires_abi') or ''),
        permissions=perms, ui_assets=assets,
        description=str(d.get('description') or ''))


def load_manifest_toml(path: Path) -> PluginManifest:
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        # UnicodeDecodeError 不被前两者覆盖,必须显式列:tomllib.load() 是先
        # b.decode() 再 parse,中文 Windows 上记事本存的 GBK plugin.toml 炸在
        # decode 阶段。漏了它,用户看到的是一段 codec 报错而不是本模块的错误。
        raise ManifestError(f'plugin.toml 读取/解析失败：{e}') from e
    return manifest_from_dict(data)
