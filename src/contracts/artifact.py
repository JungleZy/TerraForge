"""Artifact —— 任务产物登记。

## 为什么要登记

改造前产物位置散在三层且各写各的：任务行上的 `output_path` / `output_dir`
字符串、四条 DELETE 路由里重复的目录命名规则、四个静态服务蓝图里再重复
一遍的目录命名规则。一个任务产出多种成果（XYZ 目录 + 每层 GeoTIFF +
MBTiles）时，没有任何地方能回答「这个任务到底产出了什么」。

§5.3 明确要求 `Artifact` 能表达「同一任务的第 N 种产物」，因为 MBTiles 是
**追加**的容器而不是替代 —— 一个地图任务可能同时有 XYZ 目录、GeoTIFF 与
MBTiles 三种产物。

## 为什么没有外键

与 `pending_deletions` / `retained_outputs` 两张表同一个理由（见
`database.py:678-680`）：产物行必须能比任务行活得久。用户删任务但保留文件
时，产物记录是「文件还在、任务没了」的唯一线索；删任务并删文件时，产物行
是后台清理线程的工作清单。挂上外键 CASCADE 就等于在最需要它的那一刻把它
删掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

__all__ = ['Artifact', 'ArtifactKind', 'PIPELINES']

#: 四条管线的标识。与任务表的对应关系写在 `task_table` 里，只此一份。
PIPELINES = ('map', 'dem', 'contour', 'local_terrain')

_PIPELINE_TABLES = {
    'map': 'tasks',
    'dem': 'dem_tasks',
    'contour': 'contour_tasks',
    'local_terrain': 'local_terrain_tasks',
}


class ArtifactKind(Enum):
    """产物形态。值即落库文本。"""

    #: 松散瓦片金字塔目录 `<root>/{z}/{x}/{y}.png`
    XYZ_DIR = 'xyz_dir'
    #: 单个 GeoTIFF（地图任务按 zoom 一个）
    GEOTIFF = 'geotiff'
    #: MBTiles 容器（影像 / 等高线 / 矢量各自成库，见 §5.3）
    MBTILES = 'mbtiles'
    #: quantized-mesh 地形目录（含 layer.json）
    TERRAIN_DIR = 'terrain_dir'
    #: 等高线样式化 PNG 瓦片目录
    CONTOUR_DIR = 'contour_dir'
    #: DEM 原始颗粒目录
    DEM_DIR = 'dem_dir'


@dataclass(frozen=True)
class Artifact:
    """一件产物。

    pipeline / task_id
        产出它的任务。**不是外键**（见模块 docstring）。
    kind
        形态。
    path
        绝对路径。目录类产物指目录，文件类产物指文件。
    fmt
        内容格式：`png` / `jpg` / `tif` / `pbf` / `terrain`。MBTiles 的
        `metadata.format` 直接用它。
    bytes_total / tile_count
        规模。目录类产物在完成时统计一次，之后不追踪。
    minzoom / maxzoom
        层级范围。非瓦片产物为 None。
    has_gaps
        产物是否带缺块。§13-3 要求「成果与历史永久带缺块标记」，标记就落在
        这里 —— 它跟着产物走，而不是跟着任务状态走，因为任务可以被删、
        产物可以被保留。
    meta
        形态相关的补充信息（MBTiles 的 vector_layers、GeoTIFF 的压缩方式……）。
    """

    pipeline: str
    task_id: int
    kind: ArtifactKind
    path: str
    fmt: str = ''
    bytes_total: int = 0
    tile_count: int = 0
    minzoom: Optional[int] = None
    maxzoom: Optional[int] = None
    has_gaps: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ''

    def __post_init__(self):
        if self.pipeline not in PIPELINES:
            raise ValueError(
                f"pipeline must be one of {PIPELINES}, got {self.pipeline!r}")
        if not self.path:
            raise ValueError("Artifact.path must not be empty")

    @property
    def task_table(self) -> str:
        return _PIPELINE_TABLES[self.pipeline]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'pipeline': self.pipeline,
            'task_id': self.task_id,
            'kind': self.kind.value,
            'path': self.path,
            'format': self.fmt,
            'bytes_total': self.bytes_total,
            'tile_count': self.tile_count,
            'minzoom': self.minzoom,
            'maxzoom': self.maxzoom,
            'has_gaps': bool(self.has_gaps),
            'meta': dict(self.meta),
            'created_at': self.created_at,
        }

    @classmethod
    def from_row(cls, row) -> 'Artifact':
        meta_raw = row['meta'] if 'meta' in row.keys() else ''
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return cls(
            pipeline=row['pipeline'],
            task_id=row['task_id'],
            kind=ArtifactKind(row['kind']),
            path=row['path'],
            fmt=row['format'] or '',
            bytes_total=row['bytes_total'] or 0,
            tile_count=row['tile_count'] or 0,
            minzoom=row['minzoom'],
            maxzoom=row['maxzoom'],
            has_gaps=bool(row['has_gaps']),
            meta=meta,
            created_at=row['created_at'] or '',
        )

    def meta_json(self) -> str:
        return json.dumps(self.meta, separators=(',', ':'), ensure_ascii=False)
