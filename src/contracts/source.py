"""SourceSnapshot —— 冻结下载源身份，产出 fingerprint。

## 它堵的洞

改造前 `tasks` 表关于下载源只存一列 `style TEXT`（`'satellite'`），真实 URL
是**请求时**从 `config.tile_servers` 现展开的（`tile_url_probe.expand_server_entry`），
而磁盘缓存的命名空间只有一个 style 码（`cache/s/{z}/{x}/{y}.png`）。后果：

- 改了服务器列表再恢复旧任务 → 新旧两个来源的瓦片混进同一个成品，无任何提示；
- 两个 style 相同、服务器不同的任务 → 共用一个缓存命名空间，互相投毒；
- 事后无法回答「这块瓦片是谁给的」。

GeoDownloader 踩的是同一个坑且更浅：缓存键是 `source.id`（`downloader.rs:183`），
URL 模板以 `INSERT OR IGNORE` 写入（`store.rs:66-77`），改了 URL 既不刷新也不
失效；键还经过 slug 化（`tile_cache/mod.rs:147`），`"天地图 IMG"` 和 `img` 会
塌缩到同一个文件。**本实现不做任何 slug**：命名空间的可变部分是十六进制
摘要，不可能塌缩。

## 形制来源

`src/services/basemap_source.py:106-122` 的 `source_version()` 已经在**显示侧**
解决过同一个问题：它取 `sha256(上游模板)[:8]`，刻意哈希**解析后的模板**而不是
源名字，因为 `'custom'` / `'download_source'` 这类名字会指向变化的上游。这里把
同一形制推广到下载侧，并且扩到整套身份字段。

## 凭据

`credential_reference` 只存**引用**（配置键名，例如 `earthdata_password`），
不存值；`header_names` 只存 header 名字，不存值。fingerprint 因此可以安全地
进日志、进产物 metadata、进目录名。这是 §4.3 的硬性边界：
「密码与 Token 不进入 fingerprint 明文」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

__all__ = ['SourceSnapshot', 'FINGERPRINT_LENGTH', 'source_id_of']

# 摘要取多少位十六进制。8 位（32 bit）在几十个源的量级上碰撞概率可忽略，
# 而目录名越短越好读；basemap_source.source_version 也是 8 位，保持一致。
FINGERPRINT_LENGTH = 8

# 命名空间目录名的合法字符集由 task_cleanup._CATEGORY_NAME_RE 约束
# （缓存分类 = cache/ 下的一级目录名）。style 码是 m/s/y/t 单字符，
# 分隔符用 '-'，摘要是小写十六进制 —— 全部落在该正则内。
NAMESPACE_SEPARATOR = '-'


def _canonical(value) -> Any:
    """把字段收成可稳定序列化的形态。

    tuple/list → list（JSON 只有 array）；None → ''（避免 `null` 与 `""`
    产生两个不同的指纹，而它们语义相同：都是「没配」）。
    """
    if value is None:
        return ''
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


@dataclass(frozen=True)
class SourceSnapshot:
    """一次任务创建时刻的下载源身份快照。**不可变。**

    source_id
        源的稳定标识。内置源用 style 名（`satellite`），自定义源用用户给的名字。
        它**不**参与去重语义 —— 参与的是 fingerprint。
    url_template
        解析后的完整 URL 模板（含 `{z}/{x}/{y}` 占位符）。这是身份的核心。
    server_list
        轮换服务器条目（配置里的原始写法，例如 `('mts0','mts1')`）。
    style
        单字符 style 码（`m`/`s`/`y`/`t`），决定缓存目录的可读前缀。
    tile_scheme
        `xyz` 或 `tms`。写 MBTiles 时决定 `tile_row` 要不要翻转 ——
        GeoLibre 的读取端 `lib.rs:3789` 正是按 metadata 里的 scheme 分支，
        写入端必须自洽（§5.3）。
    crs
        瓦片的坐标系，目前恒为 `EPSG:3857`。留字段是因为 DEM 颗粒源是 4326，
        同一个快照类型要能描述它们。
    subdomains
        `{s}` 占位符的取值集合。
    header_names
        请求要带的自定义 header **名字**（不含值）。
    credential_reference
        凭据在 config 表里的键名，或空串。**永不含凭据本体。**
    attribution
        署名文本，写进 MBTiles metadata 的 `attribution`。
    usage_policy
        批量下载政策的一句话摘要 / 链接，进任务日志与诊断包。
    """

    source_id: str
    url_template: str
    server_list: Tuple[str, ...] = ()
    style: str = 'm'
    tile_scheme: str = 'xyz'
    crs: str = 'EPSG:3857'
    subdomains: Tuple[str, ...] = ()
    header_names: Tuple[str, ...] = ()
    credential_reference: str = ''
    attribution: str = ''
    usage_policy: str = ''

    def __post_init__(self):
        if not self.source_id:
            raise ValueError("SourceSnapshot.source_id must not be empty")
        if not self.url_template:
            raise ValueError("SourceSnapshot.url_template must not be empty")
        if self.tile_scheme not in ('xyz', 'tms'):
            raise ValueError(
                f"tile_scheme must be 'xyz' or 'tms', got {self.tile_scheme!r}")
        for name in self.header_names:
            if ':' in name or '\n' in name:
                raise ValueError(
                    f"header_names holds header NAMES only, got {name!r} — "
                    f"a value would leak into the fingerprint and the logs")
        # 凭据本体不得出现在快照里。这条检查是廉价的护栏，不是完备的秘密检测：
        # 它拦的是「顺手把 token 塞进 credential_reference」这种最常见的误用。
        if any(ch.isspace() for ch in self.credential_reference):
            raise ValueError(
                "credential_reference must be a config key name, not a value")

    # ---- 身份 -------------------------------------------------------

    def identity_payload(self) -> Dict[str, Any]:
        """参与 fingerprint 的字段。

        刻意**不含** attribution / usage_policy：它们是展示文本，改一个错别字
        不应该让整个缓存命名空间失效、让所有任务重下。
        """
        return {
            'source_id': self.source_id,
            'url_template': self.url_template,
            'server_list': _canonical(self.server_list),
            'style': self.style,
            'tile_scheme': self.tile_scheme,
            'crs': self.crs,
            'subdomains': _canonical(self.subdomains),
            'header_names': _canonical(self.header_names),
            'credential_reference': self.credential_reference,
        }

    @property
    def fingerprint(self) -> str:
        """身份摘要（8 位小写十六进制）。

        `sort_keys=True` 是身份的一部分：字典序变了摘要就变了，等于缓存全失效。
        """
        blob = json.dumps(self.identity_payload(), sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:FINGERPRINT_LENGTH]

    @property
    def cache_namespace(self) -> str:
        """磁盘缓存的一级目录名，例如 `s-1a2b3c4d`。

        前缀保留 style 码是为了让 `cache/` 目录仍然人类可读、缓存管理页仍然
        能按「卫星图 / 路网图」归类；后缀保证换源即换命名空间。
        """
        return f"{self.style}{NAMESPACE_SEPARATOR}{self.fingerprint}"

    @staticmethod
    def style_of_namespace(namespace: str) -> str:
        """从命名空间目录名反推 style 码。缓存统计页给分类起名要用。"""
        return namespace.split(NAMESPACE_SEPARATOR, 1)[0] if namespace else ''

    @staticmethod
    def is_namespace(name: str) -> bool:
        """目录名看起来是不是一个源命名空间（而不是 `dem` / `basemap`）。"""
        if NAMESPACE_SEPARATOR not in name:
            return False
        head, _, tail = name.partition(NAMESPACE_SEPARATOR)
        return (bool(head) and len(tail) == FINGERPRINT_LENGTH
                and all(c in '0123456789abcdef' for c in tail))

    # ---- 序列化 -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        data = {
            'source_id': self.source_id,
            'url_template': self.url_template,
            'server_list': list(self.server_list),
            'style': self.style,
            'tile_scheme': self.tile_scheme,
            'crs': self.crs,
            'subdomains': list(self.subdomains),
            'header_names': list(self.header_names),
            'credential_reference': self.credential_reference,
            'attribution': self.attribution,
            'usage_policy': self.usage_policy,
            'fingerprint': self.fingerprint,
        }
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(',', ':'), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SourceSnapshot':
        return cls(
            source_id=str(data.get('source_id') or ''),
            url_template=str(data.get('url_template') or ''),
            server_list=tuple(data.get('server_list') or ()),
            style=str(data.get('style') or 'm'),
            tile_scheme=str(data.get('tile_scheme') or 'xyz'),
            crs=str(data.get('crs') or 'EPSG:3857'),
            subdomains=tuple(data.get('subdomains') or ()),
            header_names=tuple(data.get('header_names') or ()),
            credential_reference=str(data.get('credential_reference') or ''),
            attribution=str(data.get('attribution') or ''),
            usage_policy=str(data.get('usage_policy') or ''),
        )

    @classmethod
    def from_json(cls, text) -> Optional['SourceSnapshot']:
        """落库文本 → 快照。空串 / 坏数据一律返回 None（存量行没有这一列）。"""
        if not text:
            return None
        if isinstance(text, (bytes, bytearray)):
            text = text.decode('utf-8')
        if isinstance(text, dict):
            try:
                return cls.from_dict(text)
            except ValueError:
                return None
        try:
            return cls.from_dict(json.loads(text))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def summary(self) -> str:
        """一行摘要，进任务日志。URL 模板本身不含凭据（凭据走 header/查询串
        由 credential_reference 指向），可以原样打印。"""
        servers = ','.join(self.server_list) if self.server_list else '-'
        return (f'{self.source_id} fp={self.fingerprint} style={self.style} '
                f'scheme={self.tile_scheme} servers={servers} tpl={self.url_template}')


def source_id_of(raw) -> str:
    """`tasks.source_snapshot` 列原文 → 快照里的 `source_id`；空 / 坏行回落空串。

    存在的理由是**展示侧读的是它、不是 `style` 列**。插件源任务的 `style` 列
    是谎话：`registry.build_source_snapshot` 把快照的 style 冻成 `'p'`（它只
    决定缓存命名空间的可读前缀），而任务行里落的仍是提交那一刻样式下拉的值 ——
    于是一个天地图任务在历史里显示成「路线图」。快照的 `source_id` 是行上唯一
    的真身份：内置源是 style 名（`satellite`），插件源是 `plugin:<pid>:<sid>`。

    只取一个字段却仍走 `from_json`：坏行的口径（空串而不是抛）必须与读取侧的
    其余部分完全一致，多写一份 `json.loads` 就是第二份口径。
    """
    snapshot = SourceSnapshot.from_json(raw)
    return snapshot.source_id if snapshot else ''
