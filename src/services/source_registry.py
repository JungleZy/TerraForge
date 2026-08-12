"""SourceSnapshot 的**唯一**生产点，以及缓存命名空间的路径规则。

## 为什么要有这个模块

改造前「下载源是谁」这件事没有任何一个地方能回答。`tasks` 表只有一列
`style TEXT`（存的是 `'satellite'` 这种人类名字），真实 URL 是**每块瓦片、
每次重试**从 `config.tile_servers` 现展开的（`download_engine.get_tile_url`
→ `tile_url_probe.expand_server_entry`），而磁盘缓存的目录只按一个 style 码
分区（`cache/s/{z}/{x}/{y}.png`）。三个后果，全都静默：

1. 编辑服务器列表后恢复旧任务 —— 已缓存的瓦片来自旧源、新下的来自新源，
   两者写进同一个目录、拼进同一张 GeoTIFF，没有任何提示；
2. 两个 style 相同、服务器不同的任务共用一个命名空间，互相投毒；
3. 事后无法回答「这块瓦片是谁给的」。

而且服务器列表的读取带 60 秒 TTL 缓存（`download_engine._tile_servers`），
所以改配置的生效时刻是「最多一分钟后的某个瞬间」，跨越一次任务运行。

本模块把「源身份」从隐式的运行时展开变成**任务创建时刻冻结的一份快照**：
指纹进任务行、进缓存目录名、进产物 metadata、进任务日志。

## 缓存命名空间

    改造前： cache/<style_code>/{z}/{x}/{y}.png
    改造后： cache/<style_code>-<fingerprint8>/{z}/{x}/{y}.png

仍然是**一级**目录，所以 `task_cleanup._CACHE_PART_MAX_DEPTH = 4` 无需改动，
`get_cache_stats` 的「一级目录 = 一个分类」也仍然成立。前缀保留 style 码是
为了让缓存管理页仍然能说「卫星影像 1.2 GB」而不是一串十六进制。

不做任何 slug 化。GeoDownloader 的缓存键先 slug 再用（`tile_cache/mod.rs:147`），
`"天地图 IMG"` 和 `img` 会塌缩到同一个文件；这里可变的那一半是 sha256 摘要，
结构上不可能塌缩。

## 存量缓存

命名空间一变，`cache/s/` 这些目录就再也不会被命中 —— 放着等于几十 GB 静默
失效，删掉更糟。`migrate_legacy_cache_namespaces` 把它们**改名**归入当前配置
对应的指纹下。这个归属是一个假设（「现有缓存是当前这套 tile_servers 下出来
的」），但它是唯一可得的假设，而且代价有界：混着的瓦片本来就已经混在一个
目录里了，搬迁不会让情况更糟，之后换源才开始真正隔离。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

from src.contracts.source import SourceSnapshot
from src.core.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    'STYLE_CODES',
    'STYLE_NAMES',
    'style_code_for',
    'snapshot_for_style',
    'snapshot_for_task_row',
    'cache_root_for',
    'tile_cache_path',
    'namespace_matches',
    'list_cache_namespaces',
    'migrate_legacy_cache_namespaces',
]

# 人类样式名 → Google vt 的 lyrs 码。**全项目唯一一份。**
#
# `src/services/task_manager.py` 的 `STYLE_MAP` 是同一张表的副本，已改为从这里
# 导入 —— 缓存目录名由这张表的值决定，两处不一致就等于两套缓存命名空间。
STYLE_CODES = {
    'roadmap': 'm',
    'satellite': 's',
    'hybrid': 'y',
    'terrain': 't',
}

# 反向表：码 → 人类名。快照的 source_id 用人类名（诊断日志里 `satellite` 比
# `s` 有意义得多），而缓存目录用码（短）。
STYLE_NAMES = {code: name for name, code in STYLE_CODES.items()}

# 默认样式码。与 `task_manager._execute_task` 对未知样式的兜底一致 —— 那里
# 写的是 `STYLE_MAP.get(task.style, 'm')`。
DEFAULT_STYLE_CODE = 'm'

# 署名与政策文本。刻意**不为任何具体图源编造法律声明**：本项目不内置未经
# 政策审核的下载源（§11），tile_servers 的内容完全由使用者决定，所以这里
# 只能陈述这个事实本身。真实的 attribution 由图源向导让用户自己填。
_GENERIC_ATTRIBUTION = ''
_GENERIC_USAGE_POLICY = (
    'Tile source is operator-configured; bulk-download permission, quota and '
    'attribution obligations are the operator\'s responsibility.'
)


def _row_get(row, key, default=None):
    """`sqlite3.Row` 没有 `.get()`；列不存在时回退默认值。

    形制与 `src/models/task.py:_row_get` 相同。存量库里 `source_snapshot`
    这一列可能还没被 ALTER 上去（新列在 init_database 里补，但读旧备份、
    读测试里手搓的老表都会缺），缺列必须是「回退」而不是异常。
    """
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def style_code_for(style) -> str:
    """人类样式名 / 单字符码 / 传统缩写 → 单字符码。

    三种入参都要接：任务行里存的是人类名（`'satellite'`），下载路径上传的是
    码（`'s'`），而 `config.default_style` 的出厂值是传统缩写 `'m'`。未知值
    回退 `'m'`，与改造前 `STYLE_MAP.get(task.style, 'm')` 的行为逐位一致 ——
    这里不能改成抛错：`Task.from_row` 刻意绕过校验就是为了让一行脏数据不至于
    打爆整个历史列表。
    """
    raw = (style or '').strip()
    if raw in STYLE_CODES:
        return STYLE_CODES[raw]
    if raw in STYLE_NAMES:
        return raw
    return DEFAULT_STYLE_CODE


def _config_manager(config_manager=None):
    if config_manager is not None:
        return config_manager
    from src.services.config_manager import ConfigManager
    return ConfigManager()


def _server_entries(config_manager=None, cursor=None) -> List[str]:
    """当前配置的服务器条目列表。

    `cursor` 非空时直接读 config 表：调用方（数据库迁移）正处在一个**未提交**
    的事务里，另开一条连接会读到迁移前的旧值，而迁移恰恰可能刚写过配置。
    """
    from src.services.tile_url_probe import parse_server_list
    raw = ''
    if cursor is not None:
        try:
            row = cursor.execute(
                "SELECT value FROM config WHERE key = 'tile_servers'").fetchone()
            raw = (row[0] if row else '') or ''
        except Exception as e:  # 表还不存在 / 库被锁：退回默认列表即可
            logger.warning(f'读取 tile_servers 失败（{e!r}），按默认列表处理')
    else:
        try:
            raw = _config_manager(config_manager).get('tile_servers', '') or ''
        except Exception as e:
            logger.warning(f'读取 tile_servers 失败（{e!r}），按默认列表处理')
    return parse_server_list(raw)


def snapshot_for_style(style_code: str, config_manager=None, *, cursor=None
                       ) -> SourceSnapshot:
    """当前配置 + 样式 → 一份快照。**任务创建时刻调用一次，之后不再调用。**

    `url_template` 取服务器列表**第一条**的展开结果。为什么只取第一条而不是
    全部：模板是身份的核心，而轮换服务器只是同一份内容的多个出口
    （`download_tile` 按 `(x+y+attempt) % len(servers)` 轮换，四台机器给的是
    同一批瓦片）。整份列表仍然进 `server_list` 参与指纹 —— 换掉其中任何一台
    都算换源，因为没人能保证第五台镜像和前四台内容一致。
    """
    code = style_code_for(style_code)
    entries = _server_entries(config_manager, cursor=cursor)
    from src.services.tile_url_probe import expand_server_entry
    template = expand_server_entry(entries[0], code)
    return SourceSnapshot(
        source_id=STYLE_NAMES.get(code, code),
        url_template=template,
        server_list=tuple(entries),
        style=code,
        tile_scheme='xyz',
        crs='EPSG:3857',
        attribution=_GENERIC_ATTRIBUTION,
        usage_policy=_GENERIC_USAGE_POLICY,
    )


def snapshot_for_task_row(row, config_manager=None) -> SourceSnapshot:
    """任务行 → 快照。存量行（没有 `source_snapshot` 列 / 该列为空）现推。

    现推出来的快照用的是**当前**配置，所以对一个建于换源之前的旧任务，它
    指向的命名空间可能与那些瓦片实际所在的目录不同。这不是 bug 而是唯一
    诚实的答案：那批瓦片的真实来源没人记录过。存量缓存目录由
    `migrate_legacy_cache_namespaces` 一次性归到「迁移当刻的当前配置」下，
    与这里的现推口径一致，所以绝大多数存量任务仍然命中。
    """
    stored = SourceSnapshot.from_json(_row_get(row, 'source_snapshot'))
    if stored is not None:
        return stored
    return snapshot_for_style(_row_get(row, 'style'), config_manager)


def cache_root_for(snapshot) -> Path:
    """快照 → 它的缓存根目录。接受 `SourceSnapshot` 或已算好的命名空间字符串。"""
    namespace = getattr(snapshot, 'cache_namespace', snapshot)
    return Path(Config.CACHE_DIR) / str(namespace)


def tile_cache_path(snapshot, zoom: int, x: int, y: int) -> Path:
    """快照 + 瓦片坐标 → 缓存文件路径。

    扩展名恒为 `.png`，与改造前一致：缓存里放的是上游给什么就是什么的原始
    字节，`.png` 只是个不参与判定的后缀（真正的内容校验是
    `download_engine.looks_like_image` 的魔数比对）。
    """
    return cache_root_for(snapshot) / str(zoom) / str(x) / f'{y}.png'


def namespace_matches(namespace: str, snapshot) -> bool:
    return str(namespace) == getattr(snapshot, 'cache_namespace', snapshot)


def list_cache_namespaces() -> List[str]:
    """`cache/` 下所有看起来是源命名空间的一级目录名。

    `dem` / `basemap` 这类固定分类不在其中 —— 判据是
    `SourceSnapshot.is_namespace`（`<前缀>-<8 位十六进制>`），不是黑名单。
    """
    root = Path(Config.CACHE_DIR)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(e.name for e in entries
                  if e.is_dir() and SourceSnapshot.is_namespace(e.name))


def migrate_legacy_cache_namespaces(cursor) -> int:
    """`cache/<style_code>` → `cache/<style_code>-<fingerprint>`。

    由 `database.migrate_cache_to_source_namespace`（user_version 5 → 6）调用。

    **永不抛异常。** 缓存搬迁失败最坏是缓存失效重下，让它阻断启动才是真正的
    事故 —— 这条约定与 `migrate_base_path_to_assets` 的处理一致。

    目标目录已存在时跳过、不合并：两个目录都可能含来源不明的瓦片，把它们并
    在一起只会制造一个更难查的问题。
    """
    root = Path(Config.CACHE_DIR)
    if not root.is_dir():
        return 0

    moved = 0
    for code in sorted(STYLE_NAMES):
        legacy = root / code
        if not legacy.is_dir():
            continue
        try:
            target = root / snapshot_for_style(code, cursor=cursor).cache_namespace
        except Exception as e:
            logger.warning(f'缓存目录 {code} 的目标命名空间算不出来（{e!r}），跳过')
            continue
        if target.exists():
            logger.warning(
                f'缓存目录 {legacy.name} 未搬迁：目标 {target.name} 已存在。'
                f'两个目录都保留 —— 合并来源不明的瓦片只会制造更难查的问题。')
            continue
        try:
            os.replace(legacy, target)
        except OSError as e:
            logger.warning(f'缓存目录 {legacy.name} → {target.name} 搬迁失败（{e!r}），保留原状')
            continue
        moved += 1
        logger.info(f'缓存目录 {legacy.name} → {target.name}')
    return moved
