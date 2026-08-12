"""
Configuration module for TerraForge
"""
import os
import sys
import secrets
import logging
from pathlib import Path

from src.core.bundle import bundle_dir

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024


def _parse_max_content_length():
    """解析 MAX_CONTENT_LENGTH 环境变量;非法值回退默认值并警告。

    直接 int(os.environ.get(...)) 会在 import 期对非法值抛 ValueError,且报错
    不含变量名,排查困难。
    """
    raw = os.environ.get('MAX_CONTENT_LENGTH')
    if raw is None:
        return _DEFAULT_MAX_CONTENT_LENGTH
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "MAX_CONTENT_LENGTH 环境变量值 %r 不是合法整数,回退默认值 %d",
            raw, _DEFAULT_MAX_CONTENT_LENGTH)
        return _DEFAULT_MAX_CONTENT_LENGTH


class Config:
    """Application configuration class"""

    # 单一事实源：app.py 的启动横幅、scripts/push-release.{sh,bat} 的 tag、
    # CI Release 都从这里取。改它时 RELEASE_NOTES.md 顶部标题必须同步 ——
    # tests/test_fix_build_scripts.py 有契约用例钉住两者相等。
    APP_VERSION = '0.3.5'

    # Secret key for Flask session management
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        SECRET_KEY = secrets.token_hex(32)
        # 不在这里直接 logger.warning —— 本类在 import 时就执行,警告会刷在启动横幅
        # 前面,而且在 reloader 父子进程里各打一遍。改为置标记,由 app.py 的
        # create_app 统一提示一次(WSGI 部署也能看到)。
        SECRET_KEY_WAS_GENERATED = True
    else:
        SECRET_KEY_WAS_GENERATED = False

    # Base directory of the application
    # 打包模式(Nuitka standalone):可写数据(数据库/下载/缓存)放在可执行文件旁边
    if bundle_dir() is not None:
        BASE_DIR = Path(sys.executable).parent.absolute()
    else:
        # Running in normal Python environment
        # (src/core/ 的上两级才是项目根目录 —— 少数一级会把 data/downloads/cache
        #  建到 src/ 里面,程序照常启动但等于换了一份空数据,不抛任何异常)
        BASE_DIR = Path(__file__).parent.parent.parent.absolute()

    # Database configuration
    DATABASE_PATH = BASE_DIR / 'data' / 'map_downloader.db'

    # Download and cache directories
    DOWNLOADS_DIR = BASE_DIR / 'downloads'
    OUTPUT_DIR = DOWNLOADS_DIR  # Alias for output directory

    # Max request body size (bytes). Flask aborts larger uploads with HTTP 413
    # before reading the body — caps the local-terrain upload endpoint, which
    # streams files to disk in chunks (shutil.copyfileobj). Default 2 GiB
    # covers a batch of large 1°x1° DEM GeoTIFFs; override with
    # MAX_CONTENT_LENGTH env var if needed (非法值回退默认,见 _parse_max_content_length).
    MAX_CONTENT_LENGTH = _parse_max_content_length()
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
