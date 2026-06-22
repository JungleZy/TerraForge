"""
Configuration module for Google Maps Downloader
"""
import os
import sys
import secrets
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Config:
    """Application configuration class"""

    APP_VERSION = '0.0.6'

    # Secret key for Flask session management
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        SECRET_KEY = secrets.token_hex(32)
        logger.warning(
            "SECRET_KEY not set in environment variables. "
            "Generated a random key for this session. "
            "Set SECRET_KEY environment variable for production use."
        )

    # Base directory of the application
    # Support PyInstaller bundled mode
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running in PyInstaller bundle
        BASE_DIR = Path(sys.executable).parent.absolute()
    else:
        # Running in normal Python environment
        BASE_DIR = Path(__file__).parent.absolute()

    # Database configuration
    DATABASE_PATH = BASE_DIR / 'data' / 'map_downloader.db'

    # Download and cache directories
    DOWNLOADS_DIR = BASE_DIR / 'downloads'
    OUTPUT_DIR = DOWNLOADS_DIR  # Alias for output directory

    # Max request body size (bytes). Flask aborts larger uploads with HTTP 413
    # before reading them into memory — caps the local-terrain upload endpoint,
    # which reads files into memory. Default 2 GiB covers a batch of large
    # 1°x1° DEM GeoTIFFs; override with MAX_CONTENT_LENGTH env var if needed.
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 2 * 1024 * 1024 * 1024))
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

        logger.info("Initialized directories:")
        logger.info(f"  - Database: {Config.DATABASE_PATH}")
        logger.info(f"  - Downloads: {Config.DOWNLOADS_DIR}")
        logger.info(f"  - Cache: {Config.CACHE_DIR}")
