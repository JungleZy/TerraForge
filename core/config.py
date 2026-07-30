"""
Configuration module for TerraForge
"""
import os
import sys
import secrets
import logging
from pathlib import Path

from core.bundle import bundle_dir

logger = logging.getLogger(__name__)


class Config:
    """Application configuration class"""

    APP_VERSION = '0.1.3'

    # Secret key for Flask session management
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        SECRET_KEY = secrets.token_hex(32)
        # 不在这里直接 logger.warning —— 本类在 import 时就执行,警告会刷在启动横幅
        # 前面,而且在 reloader 父子进程里各打一遍。改为置标记,由 app.py 在打印完
        # 启动横幅后统一提示一次。
        SECRET_KEY_WAS_GENERATED = True
    else:
        SECRET_KEY_WAS_GENERATED = False

    # Base directory of the application
    # 打包模式(Nuitka standalone):可写数据(数据库/下载/缓存)放在可执行文件旁边
    if bundle_dir() is not None:
        BASE_DIR = Path(sys.executable).parent.absolute()
    else:
        # Running in normal Python environment
        # (core/ 的上一级才是项目根目录)
        BASE_DIR = Path(__file__).parent.parent.absolute()

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

        logger.debug("Initialized directories:")
        logger.debug(f"  - Database: {Config.DATABASE_PATH}")
        logger.debug(f"  - Downloads: {Config.DOWNLOADS_DIR}")
        logger.debug(f"  - Cache: {Config.CACHE_DIR}")
