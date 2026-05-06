"""
Configuration data model
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ConfigModel:
    """Configuration key-value data model"""
    key: str
    value: str
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert ConfigModel to dictionary"""
        return {
            'key': self.key,
            'value': self.value,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
