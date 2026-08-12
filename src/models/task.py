"""
Task and Tile data models
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from enum import Enum
from src.core.config import Config
from src.core.database import parse_db_timestamp
from src.contracts.region import RegionSpec, RegionValidationError
from src.services.geo_validation import validate_bbox, validate_zoom


class TaskStatus(Enum):
    """任务状态的合法取值集合。

    ⚠️ **权威状态机在 `src/contracts/outcome.py:TaskState`**，那里带转换规则
    （哪些状态可以恢复、哪些算活动、哪些是终态）与每一条的存在理由。本枚举
    只服务一个用途：`Task.__post_init__` 的成员检查。两处的取值必须一致，
    由 `TaskState` 的定义为准 —— 这里之所以没有直接 import 它，是因为 models
    层刻意不依赖 contracts 之外的运行时语义，而 `Task` 的构造校验只需要一张
    字面量表。

    `cancelled` 不在其中：它已由 `database.migrate_cancelled_tasks_to_failed`
    迁成 `failed`，目标状态机里也没有它。
    """
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    # 补漏重跑期间。没有它，补漏与首次下载在历史里长得一模一样。
    RETRYING = "retrying"
    # 有缺块、等用户决定（补漏 / 接受并导出）。§13-3 允许显式导出部分成果，
    # 那就必须有一个状态承载「等你决定」，否则「默认严格不产出」等于静默卡住。
    PENDING_DECISION = "pending_decision"
    # 用户已显式接受缺块。**不是静默成功**：产物与历史永久带缺块标记。
    COMPLETED_WITH_GAPS = "completed_with_gaps"


class MapStyle(Enum):
    """Map style enumeration"""
    ROADMAP = "roadmap"
    SATELLITE = "satellite"
    HYBRID = "hybrid"
    TERRAIN = "terrain"

    @classmethod
    def from_shorthand(cls, value: str) -> str:
        """Convert shorthand notation to full style name

        Args:
            value: Style value (can be shorthand or full name)

        Returns:
            Full style name

        Raises:
            ValueError: If value is not a valid style
        """
        # Shorthand mapping
        shorthand_map = {
            'r': 'roadmap',
            's': 'satellite',
            'h': 'hybrid',
            't': 'terrain',
            # Legacy shorthand support
            'm': 'roadmap',
            'y': 'hybrid',
        }

        # None/非字符串输入:抛 ValueError(路由层映射 400)而不是
        # AttributeError(经通用 except 变 500)
        if not isinstance(value, str):
            valid_options = list(shorthand_map.keys()) + [s.value for s in cls]
            raise ValueError(
                f"style ({value}) must be one of {valid_options}"
            )

        # If it's already a full name, validate and return
        if value in [s.value for s in cls]:
            return value

        # Try to convert from shorthand
        if value.lower() in shorthand_map:
            return shorthand_map[value.lower()]

        # Invalid value
        valid_options = list(shorthand_map.keys()) + [s.value for s in cls]
        raise ValueError(
            f"style ({value}) must be one of {valid_options}"
        )



class OutputFormat(Enum):
    """Output format enumeration"""
    PNG = "png"
    JPG = "jpg"
    BOTH = "both"
    TILES_ONLY = "tiles_only"  # Only download tiles, no merging
    IMAGE_ONLY = "image_only"  # Stitched image; tiles are still copied out (preview source)

    @classmethod
    def from_shorthand(cls, value: str) -> str:
        """Convert shorthand notation to full format name

        Args:
            value: Format value (can be shorthand or full name)

        Returns:
            Full format name

        Raises:
            ValueError: If value is not a valid format
        """
        # Shorthand mapping
        shorthand_map = {
            'p': 'png',
            'j': 'jpg',
            'b': 'both',
            't': 'tiles_only',
            'i': 'image_only',
        }

        # None/非字符串输入:抛 ValueError(路由层映射 400)而不是
        # AttributeError(经通用 except 变 500)
        if not isinstance(value, str):
            valid_options = list(shorthand_map.keys()) + [f.value for f in cls]
            raise ValueError(
                f"output_format ({value}) must be one of {valid_options}"
            )

        # If it's already a full name, validate and return
        if value in [f.value for f in cls]:
            return value

        # Try to convert from shorthand
        if value.lower() in shorthand_map:
            return shorthand_map[value.lower()]

        # Invalid value
        valid_options = list(shorthand_map.keys()) + [f.value for f in cls]
        raise ValueError(
            f"output_format ({value}) must be one of {valid_options}"
        )


class TileStatus(Enum):
    """Tile status enumeration"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"



def _row_get(row, key, default=None):
    """sqlite3.Row 没有 .get()——按列名安全取值（列不存在时回退默认值）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _unwrapped_east_of(region_spec):
    """`region_spec` 列 → 跨反经线时 RegionSpec 归一后的 east,否则 None。

    返回 None 有三种含义,对本函数的调用方是同一件事「这一行不许出现 east>180」:
    没有 region_spec(裸四角任务)、region_spec 解不出来(旧版本/脏数据)、
    region 存在但不跨界。

    解不出来时**不抛**:读取路径上的坏行不该把列表接口打成 500(与
    `Task.from_row` 绕过校验同一条理由),写入路径上则退回严格的 ±180 口径 ——
    降级的方向永远是更严,不是更松。
    """
    if not region_spec:
        return None
    try:
        spec = RegionSpec.from_json(region_spec)
    except (RegionValidationError, ValueError, TypeError):
        return None
    return spec.bbox_east if spec.crosses_antimeridian else None


@dataclass
class Task:
    """Task data model for map download tasks"""
    id: Optional[int] = None
    name: str = ""
    status: str = "pending"  # pending, running, completed, failed, paused
    north: float = 0.0
    south: float = 0.0
    east: float = 0.0
    west: float = 0.0
    zoom_min: int = 0
    zoom_max: int = 18
    style: str = "roadmap"  # roadmap, satellite, hybrid, terrain
    output_format: str = "png"  # png, jpg, both, tiles_only, image_only
    output_path: str = ""
    total_tiles: int = 0
    downloaded_tiles: int = 0
    failed_tiles: int = 0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    # L3: tasks 表里由 pause/complete 累计的运行秒数。前端 calculateTimeInfo
    # 优先用它，字段缺失时才回退按 started_at 算墙钟（那个分支本是给不写该列的
    # dem/contour/local 三条管线兜底的）。不输出它会让 /api/tasks?status=active
    # 的 paused 行在页面刷新后一直显示错误耗时。
    total_running_seconds: float = 0.0
    # --- 共享数据合同（§9.1）落到任务行上的四个字段 ---
    # 三个 TEXT 列都用空串而不是 None 作缺省：它们的落库列是
    # `TEXT DEFAULT ''`，而存量行读出来就是空串。让内存态和落库态形状一致，
    # 消费方只需要判 falsy 一次。
    #
    # source_snapshot 是 SourceSnapshot.to_json() 的原文；不在这里反序列化，
    # 因为 models 层不认识 services，而 SourceSnapshot 的构造要读配置才有
    # 兜底路径（见 source_registry.snapshot_for_task_row）。
    source_snapshot: str = ""
    source_fingerprint: str = ""
    # RegionSpec.to_json()。四至列仍然是权威的外接矩形（历史列表、统计、
    # 足迹渲染都读它们，且存量行只有它们），这一列是多边形/洞环/反经线的载体。
    region_spec: str = ""
    # 成品上有几个洞（no_data + 各类失败，= task_tiles 的行数）。
    # 与 failed_tiles 不是一回事：后者保留原义供进度条与存量 UI 使用，
    # 在存在 no_data 的任务上两者不再相等。
    gap_tiles: int = 0
    # 用户对缺块的显式决定：'' 未决 / 'accept' 接受并导出 / 'refill' 要求补漏。
    gap_decision: str = ""

    def __post_init__(self):
        """Validate task parameters after initialization"""
        # Normalize style from shorthand if needed
        self.style = MapStyle.from_shorthand(self.style)

        # Normalize output_format from shorthand if needed
        self.output_format = OutputFormat.from_shorthand(self.output_format)

        # 四至校验(共用规则:纬度 ±90、经度 ±180、north>south、east>west、
        # 拒绝 NaN/inf/非数字;详见 src/services/geo_validation.py 模块 docstring)
        #
        # 跨反经线的任务是唯一的例外,判据是**任务自己的 region_spec**:
        # `RegionSpec` 把 170..-170 归一成 west=170 / east=190,tasks 表的四至列
        # 于是存着一个 (180, 360] 内的未回绕 east(dem_tasks 早就这么存了,
        # 例如 east=181.0)。下游不会被它绊住 —— `RegionSpec.antimeridian_parts`
        # 把它拆成两段各自落在 ±180 内的矩形,`region_tiles.iter_region_tile_spans`
        # 与 `dem_granules` 的枚举都按段走,最终瓦片号一律 `x % n` 回到合法域。
        #
        # 为什么判据必须是「region 真的跨界」而不是「有 region」:后者会放行
        # east=250 配一个普通多边形这种自相矛盾的行,而 250 在下游会被当成
        # 未回绕坐标展开成横跨大半个地球的下载区。
        unwrapped_east = _unwrapped_east_of(self.region_spec)
        self.north, self.south, self.east, self.west = validate_bbox(
            self.north, self.south, self.east, self.west,
            allow_unwrapped_east=unwrapped_east is not None,
        )
        # 放行之后再对账:四至列必须逐字派生自 RegionSpec.bbox。两者对不上说明
        # 有人把「跨界的 region」和「另一个东界」拼在了同一行,而下游(枚举、
        # 足迹渲染、磁盘估算)一半读列、一半读 region,会各算各的。
        if self.east > 180.0 and abs(self.east - unwrapped_east) > 1e-9:
            raise ValueError(
                f"east ({self.east}) does not match the task's region_spec "
                f"(RegionSpec.bbox east is {unwrapped_east}); the four bbox columns "
                f"must be derived from RegionSpec.bbox verbatim")

        # Validate zoom levels
        self.zoom_min = validate_zoom(self.zoom_min, 'zoom_min')
        self.zoom_max = validate_zoom(self.zoom_max, 'zoom_max')
        if self.zoom_min > self.zoom_max:
            raise ValueError(
                f"zoom_min ({self.zoom_min}) must be less than or equal to zoom_max ({self.zoom_max})"
            )

        # Validate status
        valid_statuses = [s.value for s in TaskStatus]
        if self.status not in valid_statuses:
            raise ValueError(
                f"status ({self.status}) must be one of {valid_statuses}"
            )

    @classmethod
    def from_row(cls, row) -> "Task":
        """Reconstruct a Task from a DB row WITHOUT running __post_init__ validation.

        读取路径专用:历史遗留行可能带着旧版本校验缺口写入的非法值(非法
        style、越界四至、zoom_min>zoom_max 等),走严格构造会让一条坏行把
        get_active_tasks 这类列表接口整个打成 500。读取侧原样还原行内容;
        写入路径(create_task)仍走 __init__ + __post_init__ 严格校验。
        """
        task = cls.__new__(cls)
        task.id = row['id']
        task.name = row['name']
        task.status = row['status']
        task.north = row['north']
        task.south = row['south']
        task.east = row['east']
        task.west = row['west']
        task.zoom_min = row['zoom_min']
        task.zoom_max = row['zoom_max']
        task.style = row['style']
        task.output_format = row['output_format']
        task.output_path = row['output_path']
        task.total_tiles = row['total_tiles']
        task.downloaded_tiles = row['downloaded_tiles']
        task.failed_tiles = row['failed_tiles']
        # 三个时间戳必须走同一个解析器。它们在库里的形态**不一样**:
        # created_at 来自表默认值 CURRENT_TIMESTAMP → 朴素的 'YYYY-MM-DD HH:MM:SS';
        # started_at / completed_at 由 utc_now_iso() 写入 → 带 '+00:00'。
        # 裸 fromisoformat 会让同一个 Task 对象里既有 naive 又有 aware 的
        # datetime,to_dict 也就吐出两种形状。今天前端的 parseTaskDate 帮着兜住了,
        # 所以没有可见故障 —— 真正的代价是陷阱:任何 Python 侧写
        # `utc_now() - task.started_at` 的代码,碰上存量行就 TypeError
        # (can't subtract offset-naive and offset-aware datetimes)。
        # parse_db_timestamp 正是为收口这件事写的,别处已经在用。
        task.created_at = parse_db_timestamp(row['created_at']) if row['created_at'] else None
        task.started_at = parse_db_timestamp(row['started_at']) if row['started_at'] else None
        task.completed_at = parse_db_timestamp(row['completed_at']) if row['completed_at'] else None
        task.error_message = row['error_message']
        task.total_running_seconds = _row_get(row, 'total_running_seconds', 0.0) or 0.0
        # 五个新列一律走 _row_get：它们由本次改造的 ALTER 补上，而测试里手搓的
        # 老表、用户的旧备份都可能没有。缺列必须回退，不能让一行旧数据把
        # 历史列表打成 500 —— 这正是 from_row 绕过 __post_init__ 的同一条理由。
        task.source_snapshot = _row_get(row, 'source_snapshot', '') or ''
        task.source_fingerprint = _row_get(row, 'source_fingerprint', '') or ''
        task.region_spec = _row_get(row, 'region_spec', '') or ''
        task.gap_tiles = _row_get(row, 'gap_tiles', 0) or 0
        task.gap_decision = _row_get(row, 'gap_decision', '') or ''
        return task

    def to_dict(self) -> Dict[str, Any]:
        """Convert Task to dictionary"""
        return {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'north': self.north,
            'south': self.south,
            'east': self.east,
            'west': self.west,
            'zoom_min': self.zoom_min,
            'zoom_max': self.zoom_max,
            'style': self.style,
            'output_format': self.output_format,
            'output_path': self.output_path,
            'total_tiles': self.total_tiles,
            'downloaded_tiles': self.downloaded_tiles,
            'failed_tiles': self.failed_tiles,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message,
            'total_running_seconds': self.total_running_seconds,
            # 新增键是纯追加：上面 20 个键的名字与取值一个字没动，前端与
            # 存量测试对 to_dict 的断言全部不受影响。
            'source_fingerprint': self.source_fingerprint,
            'region_spec': self.region_spec,
            'gap_tiles': self.gap_tiles,
            'gap_decision': self.gap_decision,
        }

    @property
    def progress_percent(self) -> float:
        """Calculate download progress percentage"""
        if self.total_tiles == 0:
            return 0.0
        return (self.downloaded_tiles / self.total_tiles) * 100.0


@dataclass(slots=True)
class Tile:
    """Tile data model for individual map tiles

    slots=True：下载循环会成万地实例化 Tile，省掉每实例 __dict__ 的内存开销。
    字段集固定（无人动态挂属性，from_row 那套 __new__ 绕过构造的写法只用在
    Task 上），加 slots 不改变任何现有行为。CI/打包基线已升到 Python 3.12，
    dataclass 的 slots 参数（3.10+）可直接使用。
    """
    task_id: int
    zoom: int
    x: int
    y: int
    status: str = "pending"  # 见 src/contracts/outcome.py:TileOutcome
    retry_count: int = 0
    error_message: Optional[str] = None

    def cache_path(self, source) -> Path:
        """这块瓦片在共享缓存里的路径。

        Args:
            source: `SourceSnapshot`（新路径）**或**单字符 style 码（旧路径）。
                两种都收：调用点分布在下载引擎、拼接、收尾复制与缓存清理里，
                一次性全改会让这个改动的爆炸半径远大于必要。带快照调用的
                路径落进指纹命名空间，带 style 码调用的落进旧的单级目录。

        路径形态：
            带快照   cache/{style}-{fingerprint}/{zoom}/{x}/{y}.png
            带 style 码 cache/{style}/{zoom}/{x}/{y}.png   （存量形态）

        实现是一行 `getattr`，刻意不 import `source_registry`：models 层被
        contracts、routes 和四条管线共同依赖，往里塞一条 services 的 import
        就会在 `services/__init__` 已经 eager import ConfigManager 的前提下
        绕出环。`cache_namespace` 是 SourceSnapshot 的公开属性，取它一个字段
        不构成对那个模块的依赖，也不会产生第二处路径规则 —— 规则本身
        （「命名空间 / z / x / y.png」）只此一份。

        扩展名恒为 `.png`：缓存里放的是上游原始字节，后缀不参与任何判定
        （真正的内容校验是 download_engine.looks_like_image 的魔数比对）。
        缓存跨任务共享，不带 task_id。
        """
        namespace = getattr(source, 'cache_namespace', source)
        return Config.CACHE_DIR / str(namespace) / str(self.zoom) / str(self.x) / f"{self.y}.png"

    def to_dict(self) -> Dict[str, Any]:
        """Convert Tile to dictionary"""
        return {
            'task_id': self.task_id,
            'zoom': self.zoom,
            'x': self.x,
            'y': self.y,
            'status': self.status,
            'retry_count': self.retry_count,
            'error_message': self.error_message,
        }
