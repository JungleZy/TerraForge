"""
Configuration module for Google Maps Downloader
"""
import os
from pathlib import Path


class Config:
    """Application configuration class"""

    # Secret key for Flask session management
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    # Base directory of the application
    BASE_DIR = Path(__file__).parent.absolute()

    # Database configuration
    DATABASE_PATH = BASE_DIR / 'data' / 'map_downloader.db'

    # Download and cache directories
    DOWNLOADS_DIR = BASE_DIR / 'downloads'
    CACHE_DIR = BASE_DIR / 'cache'

    @staticmethod
    def init_app():
        """
        Initialize application directories
        Creates necessary directories if they don't exist
        """
        # Create data directory for database
        Config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Create downloads directory
        Config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        # Create cache directory
        Config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

        print(f"Initialized directories:")
        print(f"  - Database: {Config.DATABASE_PATH}")
        print(f"  - Downloads: {Config.DOWNLOADS_DIR}")
        print(f"  - Cache: {Config.CACHE_DIR}")
