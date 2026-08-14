"""产物元数据 sidecar：任务完成后在每个产物旁写一份 `<artifact>.tfmeta.json`
（形态、格式、规模、层级范围、has_gaps、生成时间）。下游工具不用开 SQLite
就能读到产物的关键属性。

## 覆盖范围（v1）：只有插件任务会落 sidecar

本钩子只在宿主调 `registry.dispatch_event` 时醒，而 v1 唯一的事件源是插件
任务的成功终态（`kind='task_completed'`）。核心四条管线（map / dem /
contour / local_terrain）**不会**发事件，因此它们的产物旁没有 sidecar ——
这是规格 §14 划定的范围（核心管线事件源统一是后续独立工作），不是 bug。

## 旁路铁律

钩子是旁路。写失败（目录只读、磁盘满、路径过长、sidecar 名被一个目录占了）
只落一条 warning，绝不向上抛：一个元数据文件写不出来，不该把一个已经成功的
任务打成失败。宿主 `dispatch_event` 那侧也包了 try/except，但那是兜底，不是
本模块可以省掉自己这层的理由 —— 钩子实例是公开的，将来可能从别处被直接调用。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.plugins.protocols import PluginDefinition

logger = logging.getLogger(__name__)

MANIFEST = {
    'id': 'artifact_meta',
    'name': '产物元数据 sidecar',
    'version': '1.0.0',
    'api_version': '1',
    'capabilities': ['hook'],
    'permissions': ['filesystem'],
    'description': '任务完成后为每个产物写一份 .tfmeta.json 元数据'
                   '（v1 只覆盖插件任务）。',
}

#: 追加在产物路径末尾的后缀。改名等于换掉下游约定，动它要同步文档。
SIDECAR_SUFFIX = '.tfmeta.json'


def _sidecar_path(artifact_path: str) -> Path:
    """`<artifact>` → `<artifact>.tfmeta.json`：**追加**后缀，不是替换。

    直接拼字符串而不走 `Path.with_suffix`：目录型产物（XYZ_DIR / TERRAIN_DIR
    等）没有后缀，而带点的目录名（`城区 2024.06`）在 with_suffix 眼里后缀是
    `.06` —— 用它就得先把原后缀读出来再拼回去，一步都省不掉还容易写反。
    外层套 `Path()` 是为了归一化尾随分隔符：`tiles/` 与 `tiles` 落同一个名字。
    """
    return Path(str(Path(artifact_path)) + SIDECAR_SUFFIX)


class ArtifactMetaHook:
    def on_event(self, event) -> None:
        if event.kind != 'task_completed':
            return          # v1 宿主只发这一个 kind；将来多出来的一律不管
        try:
            from src.services import artifact_store
            artifacts = artifact_store.list_artifacts(event.pipeline,
                                                      event.task_id)
        except Exception as e:      # 旁路铁律：取不到产物就什么都不写
            logger.warning('元数据钩子取产物失败（%s/%s）：%r',
                           event.pipeline, event.task_id, e)
            return
        for artifact in artifacts:
            self._write_sidecar(artifact)

    def _write_sidecar(self, artifact) -> None:
        sidecar = _sidecar_path(artifact.path)
        data = {
            'pipeline': artifact.pipeline,
            'task_id': artifact.task_id,
            'kind': artifact.kind.value,
            'format': artifact.fmt,
            'path': artifact.path,
            'bytes_total': artifact.bytes_total,
            'tile_count': artifact.tile_count,
            'minzoom': artifact.minzoom,
            'maxzoom': artifact.maxzoom,
            'has_gaps': artifact.has_gaps,
            'meta': artifact.meta,
            'generated_at': artifact.created_at,
        }
        try:
            sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding='utf-8')
        except (OSError, TypeError, ValueError) as e:
            # OSError：落盘失败。TypeError/ValueError：meta 里塞了不可序列化的
            # 东西（登记方给的是 JSON 解出来的 dict，但钩子不做这个假设）。
            logger.warning('产物元数据 sidecar 写入失败（%s）：%r', sidecar, e)


def register() -> PluginDefinition:
    return PluginDefinition(hooks=(ArtifactMetaHook(),))
