"""
Task and Tile data models
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from pathlib import Path
from config import Config


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

    def to_dict(self) -> dict:
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

    @property
    def cache_path(self) -> Path:
        """Get the cache file path for this tile"""
        return Config.CACHE_DIR / str(self.task_id) / str(self.zoom) / str(self.x) / f"{self.y}.png"
