"""跨管线共享数据合同（§9.1）。

四条管线（地图 / DEM / 等高线 / 本地地形）此前各写一套「区域怎么表示」
「下载源是什么」「一块瓦片算成功还是失败」「产物在哪」。本包把这四件事
收成一份合同，管线只消费合同、不各自解释。

依赖方向：contracts → core / geo_validation，**不反向**。任何 manager、
engine、route 都可以 import 本包；本包不 import 它们。

模块：
  region      RegionSpec —— 统一坐标、几何、洞环与反经线
  region_tiles 区域 → 瓦片枚举（多边形按洞环精确取瓦片，不再退化成 bbox）
  source      SourceSnapshot —— 冻结下载源身份与 policy，产出 fingerprint
  outcome     TileOutcome / TaskState —— 瓦片级结果与任务状态机
  artifact    Artifact —— XYZ / GeoTIFF / MBTiles / terrain / contour 产物
  reservation ResourceReservation —— 网络、CPU、GDAL 与磁盘预算的准入凭据
"""

from src.contracts.artifact import Artifact, ArtifactKind
from src.contracts.outcome import (
    ACTIVE_TASK_STATES,
    GAP_OUTCOMES,
    TERMINAL_TASK_STATES,
    TaskState,
    TileOutcome,
    is_gap_outcome,
    outcome_from_db,
)
from src.contracts.region import (
    RegionSpec,
    RegionValidationError,
    split_antimeridian,
)
from src.contracts.region_tiles import (
    count_region_tiles,
    iter_region_tile_spans,
)
from src.contracts.reservation import (
    ResourceKind,
    ResourceReservation,
    ResourceRequest,
)
from src.contracts.source import SourceSnapshot

__all__ = [
    'ACTIVE_TASK_STATES',
    'Artifact',
    'ArtifactKind',
    'GAP_OUTCOMES',
    'RegionSpec',
    'RegionValidationError',
    'ResourceKind',
    'ResourceRequest',
    'ResourceReservation',
    'SourceSnapshot',
    'TERMINAL_TASK_STATES',
    'TaskState',
    'TileOutcome',
    'count_region_tiles',
    'is_gap_outcome',
    'iter_region_tile_spans',
    'outcome_from_db',
    'split_antimeridian',
]
