"""
Services module for Google Maps Downloader

This module provides core business logic services:
- ConfigManager: Configuration management with validation
- DownloadEngine: Tile download and processing engine
- TaskManager: Download task lifecycle management
"""

from services.config_manager import ConfigManager

__all__ = ['ConfigManager']
