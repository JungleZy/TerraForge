"""
Unit tests for ConfigManager service
"""

import pytest
import os
import sys
import tempfile
import shutil
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import database
from services.config_manager import ConfigManager
from core.config import Config


@pytest.fixture
def test_db(monkeypatch):
    """Create a temporary test database"""
    # Create temporary directory for test database
    temp_dir = tempfile.mkdtemp()
    test_db_path = Path(temp_dir) / 'test.db'

    # Override config paths (they must be Path objects — config.init_app
    # uses .parent / .mkdir on each). monkeypatch restores them at teardown.
    monkeypatch.setattr(Config, 'DATABASE_PATH', test_db_path)
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', Path(temp_dir) / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', Path(temp_dir) / 'cache')

    # Initialize test database
    database.init_database()

    yield str(test_db_path)

    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def config_manager(test_db):
    """Create ConfigManager instance with test database"""
    return ConfigManager()


def test_get_existing_config(config_manager):
    """Test getting an existing configuration value"""
    # Get a default config value
    value = config_manager.get('default_style')

    assert value is not None
    assert value == 'm'


def test_get_with_default(config_manager):
    """Test getting non-existent config with default value"""
    # Get a non-existent key with default
    value = config_manager.get('non_existent_key', 'default_value')

    assert value == 'default_value'

    # Get non-existent key without default
    value = config_manager.get('another_non_existent_key')

    assert value is None


def test_set_config(config_manager):
    """Test setting a configuration value"""
    # Set a valid config value
    result = config_manager.set('concurrent_downloads', '20')

    assert result is True

    # Verify the value was set
    value = config_manager.get('concurrent_downloads')
    assert value == '20'

    # Test setting invalid value (should raise ValueError)
    with pytest.raises(ValueError):
        config_manager.set('concurrent_downloads', '200')  # Out of range


def test_set_many_writes_all_in_one_transaction(config_manager):
    """set_many:批量写入一次提交，全部键可读回"""
    result = config_manager.set_many({
        'concurrent_downloads': '20',
        'request_timeout': '60',
        'max_retries': '3',
    })

    assert result is True
    assert config_manager.get('concurrent_downloads') == '20'
    assert config_manager.get('request_timeout') == '60'
    assert config_manager.get('max_retries') == '3'


def test_set_many_invalid_key_rejects_whole_batch(config_manager):
    """set_many:任一键校验失败整批拒绝，合法键也不落库（无半更新状态）"""
    before = config_manager.get('concurrent_downloads')

    with pytest.raises(ValueError):
        config_manager.set_many({
            'concurrent_downloads': '20',
            'request_timeout': '9999',  # Out of range
        })

    assert config_manager.get('concurrent_downloads') == before


def test_set_many_empty_mapping_is_noop(config_manager):
    assert config_manager.set_many({}) is True


def test_get_many_returns_dict_with_none_for_missing(config_manager):
    """get_many:一次取回多键；无配置行的键映射为 None（与 get 的 default=None 一致）"""
    config_manager.set('concurrent_downloads', '25')

    result = config_manager.get_many(
        ['concurrent_downloads', 'default_style', 'no_such_key'])

    assert result == {
        'concurrent_downloads': '25',
        'default_style': 'm',
        'no_such_key': None,
    }
    assert config_manager.get_many([]) == {}


def test_get_all(config_manager):
    """Test getting all configuration values"""
    all_configs = config_manager.get_all()

    # Should have all default configs (mirrors database.DEFAULT_CONFIGS)
    assert len(all_configs) == len(database.DEFAULT_CONFIGS)

    # Check some expected keys
    assert 'default_style' in all_configs
    assert 'concurrent_downloads' in all_configs
    assert 'map_center_lat' in all_configs

    # Check structure
    assert 'value' in all_configs['default_style']
    assert 'updated_at' in all_configs['default_style']


def test_validate_config_concurrent_downloads(config_manager):
    """Test validation for concurrent_downloads"""
    assert config_manager.validate_config('concurrent_downloads', '1') is True
    assert config_manager.validate_config('concurrent_downloads', '50') is True
    assert config_manager.validate_config('concurrent_downloads', '100') is True
    assert config_manager.validate_config('concurrent_downloads', '0') is False
    assert config_manager.validate_config('concurrent_downloads', '101') is False
    assert config_manager.validate_config('concurrent_downloads', 'invalid') is False


def test_validate_config_request_timeout(config_manager):
    """Test validation for request_timeout"""
    assert config_manager.validate_config('request_timeout', '1') is True
    assert config_manager.validate_config('request_timeout', '150') is True
    assert config_manager.validate_config('request_timeout', '300') is True
    assert config_manager.validate_config('request_timeout', '0') is False
    assert config_manager.validate_config('request_timeout', '301') is False


def test_validate_config_max_retries(config_manager):
    """Test validation for max_retries"""
    assert config_manager.validate_config('max_retries', '0') is True
    assert config_manager.validate_config('max_retries', '5') is True
    assert config_manager.validate_config('max_retries', '10') is True
    assert config_manager.validate_config('max_retries', '-1') is False
    assert config_manager.validate_config('max_retries', '11') is False


def test_validate_config_latitude(config_manager):
    """Test validation for latitude"""
    assert config_manager.validate_config('map_center_lat', '0') is True
    assert config_manager.validate_config('map_center_lat', '39.9') is True
    assert config_manager.validate_config('map_center_lat', '-90') is True
    assert config_manager.validate_config('map_center_lat', '90') is True
    assert config_manager.validate_config('map_center_lat', '-90.1') is False
    assert config_manager.validate_config('map_center_lat', '90.1') is False


def test_validate_config_longitude(config_manager):
    """Test validation for longitude"""
    assert config_manager.validate_config('map_center_lng', '0') is True
    assert config_manager.validate_config('map_center_lng', '116.4') is True
    assert config_manager.validate_config('map_center_lng', '-180') is True
    assert config_manager.validate_config('map_center_lng', '180') is True
    assert config_manager.validate_config('map_center_lng', '-180.1') is False
    assert config_manager.validate_config('map_center_lng', '180.1') is False


def test_validate_config_zoom(config_manager):
    """Test validation for zoom levels"""
    assert config_manager.validate_config('map_initial_zoom', '0') is True
    assert config_manager.validate_config('map_initial_zoom', '10') is True
    assert config_manager.validate_config('map_initial_zoom', '21') is True
    assert config_manager.validate_config('map_initial_zoom', '-1') is False
    assert config_manager.validate_config('map_initial_zoom', '22') is False

    assert config_manager.validate_config('default_zoom_min', '5') is True
    assert config_manager.validate_config('default_zoom_max', '15') is True


def test_reset_to_defaults(config_manager):
    """Test resetting configuration to defaults"""
    # Modify a config value
    config_manager.set('concurrent_downloads', '20')
    assert config_manager.get('concurrent_downloads') == '20'

    # Reset to defaults
    result = config_manager.reset_to_defaults()
    assert result is True

    # Verify value is back to default
    assert config_manager.get('concurrent_downloads') == '50'

    # Verify all defaults are present (mirrors database.DEFAULT_CONFIGS)
    all_configs = config_manager.get_all()
    assert len(all_configs) == len(database.DEFAULT_CONFIGS)


def test_is_valid_lat(config_manager):
    """Test latitude validation helper"""
    assert config_manager._is_valid_lat('0') is True
    assert config_manager._is_valid_lat('45.5') is True
    assert config_manager._is_valid_lat('-90') is True
    assert config_manager._is_valid_lat('90') is True
    assert config_manager._is_valid_lat('-91') is False
    assert config_manager._is_valid_lat('91') is False
    assert config_manager._is_valid_lat('invalid') is False


def test_is_valid_lng(config_manager):
    """Test longitude validation helper"""
    assert config_manager._is_valid_lng('0') is True
    assert config_manager._is_valid_lng('120.5') is True
    assert config_manager._is_valid_lng('-180') is True
    assert config_manager._is_valid_lng('180') is True
    assert config_manager._is_valid_lng('-181') is False
    assert config_manager._is_valid_lng('181') is False
    assert config_manager._is_valid_lng('invalid') is False
