"""
Configuration Manager Service

Provides centralized configuration management with validation and persistence.
Handles reading, updating, and validating application configuration stored in SQLite.
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime
import sqlite3
from database import get_connection_context, DEFAULT_CONFIGS

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    Configuration management service with validation

    Manages application configuration stored in the database config table.
    Provides methods to get, set, validate, and reset configuration values.

    Validation Rules:
        - concurrent_downloads: 1-100
        - request_timeout: 1-300 seconds
        - max_retries: 0-10
        - cache_max_size_mb: >= 0
        - history_retention_days: >= 0
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

        except Exception as e:
            logger.error(f'Failed to get config {key}: {e}')
            return default

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

                cursor.execute(
                    '''INSERT INTO config (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at''',
                    (key, value, datetime.now())
                )

                conn.commit()

                logger.info(f'Config updated: {key} = {value}')
                return True

        except sqlite3.Error as e:
            logger.error(f'Failed to set config {key}: {e}')
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

        except Exception as e:
            logger.error(f'Failed to get all configs: {e}')
            return {}

    def reset_to_defaults(self) -> bool:
        """
        Reset all configuration to default values

        Deletes all existing configuration and re-inserts 18 default values.
        Uses explicit transaction with rollback on error to ensure data safety.

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

            elif key == 'cache_max_size_mb':
                val = int(value)
                return val >= 0

            elif key == 'history_retention_days':
                val = int(value)
                return val >= 0

            elif key == 'map_center_lat':
                return self._is_valid_lat(value)

            elif key == 'map_center_lng':
                return self._is_valid_lng(value)

            elif key in ['map_initial_zoom', 'default_zoom_min', 'default_zoom_max']:
                val = int(value)
                return 0 <= val <= 21

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
