"""
Task and Tile data models
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from enum import Enum
from config import Config


class TaskStatus(Enum):
    """Task status enumeration"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class MapStyle(Enum):
    """Map style enumeration"""
    ROADMAP = "roadmap"
    SATELLITE = "satellite"
    HYBRID = "hybrid"
    TERRAIN = "terrain"


class OutputFormat(Enum):
    """Output format enumeration"""
    PNG = "png"
    JPG = "jpg"


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
        # Validate latitude bounds
        if self.north <= self.south:
            raise ValueError(
                f"north ({self.north}) must be greater than south ({self.south})"
            )

        # Validate latitude range
        if not (-90 <= self.south <= 90):
            raise ValueError(f"south ({self.south}) must be between -90 and 90")
        if not (-90 <= self.north <= 90):
            raise ValueError(f"north ({self.north}) must be between -90 and 90")

        # Validate longitude range
        if not (-180 <= self.west <= 180):
            raise ValueError(f"west ({self.west}) must be between -180 and 180")
        if not (-180 <= self.east <= 180):
            raise ValueError(f"east ({self.east}) must be between -180 and 180")

        # Validate zoom levels
        if self.zoom_min > self.zoom_max:
            raise ValueError(
                f"zoom_min ({self.zoom_min}) must be less than or equal to zoom_max ({self.zoom_max})"
            )

        if not (0 <= self.zoom_min <= 21):
            raise ValueError(f"zoom_min ({self.zoom_min}) must be between 0 and 21")

        if not (0 <= self.zoom_max <= 21):
            raise ValueError(f"zoom_max ({self.zoom_max}) must be between 0 and 21")

        # Validate status
        valid_statuses = [s.value for s in TaskStatus]
        if self.status not in valid_statuses:
            raise ValueError(
                f"status ({self.status}) must be one of {valid_statuses}"
            )

        # Validate style
        valid_styles = [s.value for s in MapStyle]
        if self.style not in valid_styles:
            raise ValueError(
                f"style ({self.style}) must be one of {valid_styles}"
            )

        # Validate output format
        valid_formats = [f.value for f in OutputFormat]
        if self.output_format not in valid_formats:
            raise ValueError(
                f"output_format ({self.output_format}) must be one of {valid_formats}"
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
