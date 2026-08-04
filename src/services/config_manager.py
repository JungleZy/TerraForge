"""
Configuration Manager Service

Provides centralized configuration management with validation and persistence.
Handles reading, updating, and validating application configuration stored in SQLite.
"""

import logging
from typing import Optional, Dict, Any
import sqlite3
from src.core.database import (
    get_connection_context, DEFAULT_CONFIGS, normalize_default_save_path, utc_now_iso,
)
from src.services.system_proxy import mask_url_userinfo

logger = logging.getLogger(__name__)

# 写入/更新 config 行的 upsert 语句，set 与 set_many 共用（抽常量避免两处漂移）
_CONFIG_UPSERT_SQL = '''INSERT INTO config (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at'''


def _mask_log_value(key: str, value: str) -> str:
    """日志用的脱敏值：凭据/代理 userinfo 不进日志（与 set 的口径一致）。"""
    if key in {'earthdata_username', 'earthdata_password'}:
        return '***'
    if key == 'proxy_url':
        # user:pass@ 形式的代理凭据不进日志（host 保留便于排查）
        return mask_url_userinfo(value)
    return value


class ConfigManager:
    """
    Configuration management service with validation

    Manages application configuration stored in the database config table.
    Provides methods to get, set, validate, and reset configuration values.

    Validation Rules:
        - concurrent_downloads: 1-100
        - request_timeout: 1-300 seconds
        - max_retries: 0-10
        - map_center_lat: -90 to 90
        - map_center_lng: -180 to 180
        - map_initial_zoom: 0-21
        - default_zoom_min: 0-21
        - default_zoom_max: 0-21
    """

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Get configuration value by key

        Args:
            key: Configuration key to retrieve
            default: Default value to return if key not found

        Returns:
            Configuration value as string, or default if not found
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    'SELECT value FROM config WHERE key = ?',
                    (key,)
                )

                row = cursor.fetchone()

                if row:
                    return row['value']
                return default

        except sqlite3.Error as e:
            # 区分“无行”（上面返回 default）与“出错”：锁/IO 异常若静默吞成默认值，
            # earthdata_username 会被读成 '' 导致莫名 401，极难排查。记 error 后抛出。
            logger.error(f'Failed to get config {key}: {e}')
            raise

    def get_many(self, keys) -> Dict[str, Optional[str]]:
        """
        Get multiple configuration values in one connection / one query

        Args:
            keys: Iterable of configuration keys to retrieve

        Returns:
            Dict mapping each requested key to its stored value; keys without a
            config row map to None (mirrors get(key, default=None) semantics).
        """
        keys = list(keys)
        if not keys:
            return {}
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                placeholders = ','.join('?' * len(keys))
                cursor.execute(
                    f'SELECT key, value FROM config WHERE key IN ({placeholders})',
                    keys
                )

                result = {key: None for key in keys}
                for row in cursor.fetchall():
                    result[row['key']] = row['value']
                return result

        except sqlite3.Error as e:
            # 与 get 同口径：锁/IO 异常不静默吞成默认值，记 error 后抛出
            logger.error(f'Failed to get configs {keys}: {e}')
            raise

    def set(self, key: str, value: str) -> bool:
        """
        Set configuration value with validation and timestamp update

        Args:
            key: Configuration key to set
            value: Configuration value to set

        Returns:
            True if successful

        Raises:
            ValueError: If validation fails for the given key-value pair
            sqlite3.Error: If database operation fails
        """
        # Validate the configuration value
        if not self.validate_config(key, value):
            raise ValueError(f'Invalid value for config key {key}: {value}')

        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute(_CONFIG_UPSERT_SQL, (key, value, utc_now_iso()))

                conn.commit()

                # Avoid leaking secrets into logs (still allow saving them normally).
                logger.info(f'Config updated: {key} = {_mask_log_value(key, value)}')
                return True

        except sqlite3.Error as e:
            logger.error(f'Failed to set config {key}: {e}')
            raise

    def set_many(self, items: Dict[str, str]) -> bool:
        """
        Set multiple configuration values in a single transaction

        Validates every item up front, then writes all rows with one executemany
        and a single commit — a failure rolls the whole batch back, so callers
        never observe a half-updated configuration (unlike looping over set()).

        Args:
            items: Mapping of configuration key -> value

        Returns:
            True if successful (empty mapping is a no-op success)

        Raises:
            ValueError: If validation fails for any key-value pair
            sqlite3.Error: If database operation fails
        """
        # 全部键先校验完再碰库：任一键非法就整体拒绝，不产生半更新状态
        for key, value in items.items():
            if not self.validate_config(key, value):
                raise ValueError(f'Invalid value for config key {key}: {value}')

        if not items:
            return True

        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                try:
                    now = utc_now_iso()
                    cursor.executemany(
                        _CONFIG_UPSERT_SQL,
                        [(key, value, now) for key, value in items.items()]
                    )
                    conn.commit()
                except sqlite3.Error:
                    # 显式回滚，保证整批原子性（单连接单事务，要么全成要么全不成）
                    conn.rollback()
                    raise

                for key, value in items.items():
                    logger.info(f'Config updated: {key} = {_mask_log_value(key, value)}')
                return True

        except sqlite3.Error as e:
            logger.error(f'Failed to set configs {sorted(items)}: {e}')
            raise

    def get_all(self) -> Dict[str, Any]:
        """
        Get all configuration values as dictionary

        Returns:
            Dictionary with all configuration key-value pairs
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute('SELECT key, value, updated_at FROM config')
                rows = cursor.fetchall()

                result = {}
                for row in rows:
                    result[row['key']] = {
                        'value': row['value'],
                        'updated_at': row['updated_at']
                    }

                return result

        except sqlite3.Error as e:
            logger.error(f'Failed to get all configs: {e}')
            raise

    def reset_to_defaults(self) -> bool:
        """
        Reset all configuration to default values

        Deletes all existing configuration and re-inserts the DEFAULT_CONFIGS
        rows (44 as of 0.2.5). Uses explicit transaction with rollback on error
        to ensure data safety.

        M6：重插之后必须跑一次 default_save_path 归一化。DEFAULT_CONFIGS 里
        那一项是相对值 './downloads'，而 validate_config 自己会判它非法 ——
        本方法绕过了 set/set_many 的校验，不归一的话 reset 会把一个非法值写
        回库，之后地图/DEM 建任务全部 400「保存路径必须是绝对路径」（重启时
        init_database 会静默修好，所以现象很不自明）。

        Returns:
            True if successful

        Raises:
            sqlite3.Error: If database operation fails
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                try:
                    # Delete all existing config
                    cursor.execute('DELETE FROM config')

                    # Insert default configurations
                    cursor.executemany(
                        'INSERT INTO config (key, value) VALUES (?, ?)',
                        DEFAULT_CONFIGS
                    )

                    normalize_default_save_path(cursor)

                    conn.commit()
                    logger.info('Configuration reset to defaults')
                    return True

                except sqlite3.Error as e:
                    conn.rollback()
                    logger.error(f'Failed to reset config to defaults: {e}')
                    raise

        except sqlite3.Error:
            raise

    def validate_config(self, key: str, value: str) -> bool:
        """
        Validate configuration value based on key-specific rules

        Args:
            key: Configuration key
            value: Configuration value to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            # Validation rules for specific keys
            if key == 'concurrent_downloads':
                val = int(value)
                return 1 <= val <= 100

            elif key == 'request_timeout':
                val = int(value)
                return 1 <= val <= 300

            elif key == 'max_retries':
                val = int(value)
                return 0 <= val <= 10

            elif key == 'default_save_path':
                # 绝对路径 + 至少两级深度(与建任务同一口径,0.2.4 起全盘可选);
                # 相对/浅层都拒绝 —— 存一个建任务时必被 400 的值没有意义
                from src.services.geo_validation import require_absolute_output_dir
                try:
                    require_absolute_output_dir(str(value))
                    return True
                except ValueError:
                    return False

            elif key == 'map_center_lat':
                return self._is_valid_lat(value)

            elif key == 'map_center_lng':
                return self._is_valid_lng(value)

            elif key in ['map_initial_zoom', 'default_zoom_min', 'default_zoom_max']:
                val = int(value)
                return 0 <= val <= 21

            elif key == 'tile_servers':
                # 逗号分隔的瓦片服务器列表：每个条目必须是 Google 别名/主机
                # 或含 {x}/{y}/{z} 的完整 XYZ 模板（tile_url_probe 统一语义）
                from src.services.tile_url_probe import validate_server_list
                ok, _err = validate_server_list(str(value))
                return ok

            # For keys without specific validation, accept any value
            return True

        except (ValueError, TypeError):
            return False

    def _is_valid_lat(self, value: str) -> bool:
        """
        Validate latitude value

        Args:
            value: Latitude value as string

        Returns:
            True if valid latitude (-90 to 90), False otherwise
        """
        try:
            lat = float(value)
            return -90 <= lat <= 90
        except (ValueError, TypeError):
            return False

    def _is_valid_lng(self, value: str) -> bool:
        """
        Validate longitude value

        Args:
            value: Longitude value as string

        Returns:
            True if valid longitude (-180 to 180), False otherwise
        """
        try:
            lng = float(value)
            return -180 <= lng <= 180
        except (ValueError, TypeError):
            return False
