"""
Task and Tile data models
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from enum import Enum
from config import Config
from services.geo_validation import validate_bbox, validate_zoom


class TaskStatus(Enum):
    """Task status enumeration"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


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
    IMAGE_ONLY = "image_only"  # Only stitched image, do not copy tiles out

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
    output_format: str = "png"  # png, jpg
    output_path: str = ""
    total_tiles: int = 0
    downloaded_tiles: int = 0
    failed_tiles: int = 0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    def __post_init__(self):
        """Validate task parameters after initialization"""
        # Normalize style from shorthand if needed
        self.style = MapStyle.from_shorthand(self.style)

        # Normalize output_format from shorthand if needed
        self.output_format = OutputFormat.from_shorthand(self.output_format)

        # 四至校验(共用规则:纬度 ±90、经度 ±180、north>south、east>west、
        # 拒绝 NaN/inf/非数字;详见 services/geo_validation.py 模块 docstring)
        self.north, self.south, self.east, self.west = validate_bbox(
            self.north, self.south, self.east, self.west
        )

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
        }

    @property
    def progress_percent(self) -> float:
        """Calculate download progress percentage"""
        if self.total_tiles == 0:
            return 0.0
        return (self.downloaded_tiles / self.total_tiles) * 100.0


@dataclass
class Tile:
    """Tile data model for individual map tiles"""
    task_id: int
    zoom: int
    x: int
    y: int
    status: str = "pending"  # pending, downloading, completed, failed
    retry_count: int = 0
    error_message: Optional[str] = None

    def cache_path(self, style: str) -> Path:
        """Get the cache file path for this tile

        Args:
            style: Map style code (roadmap, satellite, hybrid, terrain)

        Returns:
            Path object for cache file location

        Note: Always uses .png extension as tiles are downloaded as PNG
        regardless of the final output format specified in the task.
        Cache path format: cache/{style}/{zoom}/{x}/{y}.png
        Cache is shared across all tasks for efficiency.
        """
        return Config.CACHE_DIR / style / str(self.zoom) / str(self.x) / f"{self.y}.png"

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
