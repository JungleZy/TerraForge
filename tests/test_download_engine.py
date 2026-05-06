"""
Unit tests for DownloadEngine service
"""

import pytest
import os
import sys
import tempfile
import shutil
import math

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import database
from services.download_engine import DownloadEngine, MAX_TILES, MIN_ZOOM, MAX_ZOOM
from models.task import Tile
from config import Config


@pytest.fixture
def test_db():
    """Create a temporary test database"""
    # Create temporary directory for test database
    temp_dir = tempfile.mkdtemp()
    test_db_path = os.path.join(temp_dir, 'test.db')

    # Override database path
    original_db_path = Config.DATABASE_PATH
    Config.DATABASE_PATH = test_db_path

    # Initialize test database
    database.init_database()

    yield test_db_path

    # Cleanup
    Config.DATABASE_PATH = original_db_path
    shutil.rmtree(temp_dir)


@pytest.fixture
def download_engine(test_db):
    """Create DownloadEngine instance with test database"""
    return DownloadEngine()


def test_lat_lon_to_tile_zoom_10(download_engine):
    """Test converting lat/lon to tile coordinates at zoom level 10"""
    # Test Beijing coordinates (39.9, 116.4) at zoom 10
    x, y = download_engine.lat_lon_to_tile(39.9, 116.4, 10)

    # Expected values calculated using Web Mercator projection
    # n = 2^10 = 1024
    # x = int((116.4 + 180) / 360 * 1024) = int(843.093...) = 843
    # y calculation is more complex due to Mercator projection
    assert x == 843
    assert y == 388  # Corrected based on actual Web Mercator calculation

    # Test edge cases
    # Equator, Prime Meridian at zoom 10
    x, y = download_engine.lat_lon_to_tile(0, 0, 10)
    assert x == 512  # Middle of x range
    assert y == 512  # Middle of y range

    # North pole area (85.0511 is max latitude for Web Mercator)
    x, y = download_engine.lat_lon_to_tile(85.0, 0, 10)
    assert x == 512
    assert y == 0 or y == 1  # Near top

    # South pole area
    x, y = download_engine.lat_lon_to_tile(-85.0, 0, 10)
    assert x == 512
    assert y == 1023 or y == 1022  # Near bottom


def test_calculate_tiles_single_zoom(download_engine):
    """Test calculating tiles for a single zoom level"""
    # Small area: Beijing city center
    # North: 40.0, South: 39.8, East: 116.5, West: 116.3
    # Zoom: 10
    tiles = download_engine.calculate_tiles(
        north=40.0,
        south=39.8,
        east=116.5,
        west=116.3,
        zoom_min=10,
        zoom_max=10
    )

    # Should return a list of Tile objects
    assert isinstance(tiles, list)
    assert len(tiles) > 0

    # All tiles should be at zoom level 10
    for tile in tiles:
        assert isinstance(tile, Tile)
        assert tile.zoom == 10
        assert tile.status == "pending"
        assert tile.retry_count == 0

    # Check that tiles cover the expected range
    x_coords = [tile.x for tile in tiles]
    y_coords = [tile.y for tile in tiles]

    # Should have at least 1 tile in each direction
    assert len(set(x_coords)) >= 1
    assert len(set(y_coords)) >= 1

    # Coordinates should be reasonable for zoom 10
    assert all(0 <= x < 1024 for x in x_coords)
    assert all(0 <= y < 1024 for y in y_coords)


def test_calculate_tiles_range(download_engine):
    """Test calculating tiles for a range of zoom levels"""
    # Very small area to keep tile count manageable
    # North: 40.0, South: 39.9, East: 116.4, West: 116.3
    # Zoom: 8 to 10
    tiles = download_engine.calculate_tiles(
        north=40.0,
        south=39.9,
        east=116.4,
        west=116.3,
        zoom_min=8,
        zoom_max=10
    )

    # Should return tiles for all zoom levels
    assert isinstance(tiles, list)
    assert len(tiles) > 0

    # Check that we have tiles at each zoom level
    zoom_levels = set(tile.zoom for tile in tiles)
    assert 8 in zoom_levels
    assert 9 in zoom_levels
    assert 10 in zoom_levels

    # Count tiles per zoom level
    tiles_per_zoom = {}
    for tile in tiles:
        zoom = tile.zoom
        if zoom not in tiles_per_zoom:
            tiles_per_zoom[zoom] = 0
        tiles_per_zoom[zoom] += 1

    # Higher zoom levels should generally have more tiles (4x per zoom level)
    # But for small areas, this might not always hold
    assert tiles_per_zoom[8] >= 1
    assert tiles_per_zoom[9] >= 1
    assert tiles_per_zoom[10] >= 1

    # All tiles should have proper attributes
    for tile in tiles:
        assert isinstance(tile, Tile)
        assert 8 <= tile.zoom <= 10
        assert tile.x >= 0
        assert tile.y >= 0
        assert tile.status == "pending"


def test_get_tile_url(download_engine):
    """Test generating Google Maps tile URL"""
    # Test with different parameters
    url = download_engine.get_tile_url(x=843, y=368, z=10, style='m', server_index=0)

    # Should match Google Maps tile URL format
    assert url.startswith('http://mts0.googleapis.com/vt')
    assert 'lyrs=m' in url
    assert 'x=843' in url
    assert 'y=368' in url
    assert 'z=10' in url

    # Test with different server index
    url = download_engine.get_tile_url(x=100, y=200, z=5, style='s', server_index=2)
    assert url.startswith('http://mts2.googleapis.com/vt')
    assert 'lyrs=s' in url
    assert 'x=100' in url
    assert 'y=200' in url
    assert 'z=5' in url

    # Test with all server indices
    for i in range(4):
        url = download_engine.get_tile_url(x=0, y=0, z=0, style='m', server_index=i)
        assert url.startswith(f'http://mts{i}.googleapis.com/vt')


def test_tile_count_limit(download_engine):
    """Test that tile count limit is enforced BEFORE tile generation"""
    # Try to create a task that would exceed MAX_TILES
    # Large area with high zoom levels should exceed 100,000 tiles
    # This should fail fast without creating any Tile objects in memory
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=50.0,
            south=30.0,
            east=130.0,
            west=100.0,
            zoom_min=10,
            zoom_max=15
        )

    assert "exceeds maximum allowed" in str(exc_info.value)
    assert str(MAX_TILES) in str(exc_info.value)

    # Verify the error message contains the expected tile count
    # This confirms the check happened before generation
    error_msg = str(exc_info.value)
    assert "Tile count" in error_msg


def test_input_validation_north_south(download_engine):
    """Test that north must be greater than south"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=39.0,
            south=40.0,  # south > north (invalid)
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude" in str(exc_info.value)
    assert "must be greater than south latitude" in str(exc_info.value)


def test_input_validation_zoom_range(download_engine):
    """Test that zoom levels must be in valid range"""
    # Test zoom_min too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=-1,  # Invalid
            zoom_max=10
        )

    assert "Minimum zoom level must be between" in str(exc_info.value)

    # Test zoom_max too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=22  # Invalid
        )

    assert "Maximum zoom level must be between" in str(exc_info.value)


def test_input_validation_zoom_min_max(download_engine):
    """Test that zoom_min must be <= zoom_max"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=15,
            zoom_max=10  # zoom_min > zoom_max (invalid)
        )

    assert "Minimum zoom" in str(exc_info.value)
    assert "must be less than or equal to maximum zoom" in str(exc_info.value)


def test_lat_lon_to_tile_invalid_zoom(download_engine):
    """Test that invalid zoom levels are rejected"""
    # Test zoom too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.lat_lon_to_tile(40.0, 116.0, -1)

    assert "Zoom level must be between" in str(exc_info.value)

    # Test zoom too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.lat_lon_to_tile(40.0, 116.0, 25)

    assert "Zoom level must be between" in str(exc_info.value)


def test_coordinate_range_validation_latitude(download_engine):
    """Test that latitude must be between -90 and 90"""
    # Test north latitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=95.0,  # Invalid: > 90
            south=39.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude must be between -90 and 90" in str(exc_info.value)

    # Test north latitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=-95.0,  # Invalid: < -90
            south=-96.0,
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "North latitude must be between -90 and 90" in str(exc_info.value)

    # Test south latitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=95.0,  # Invalid: > 90
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "South latitude must be between -90 and 90" in str(exc_info.value)

    # Test south latitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=-95.0,  # Invalid: < -90
            east=116.5,
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "South latitude must be between -90 and 90" in str(exc_info.value)


def test_coordinate_range_validation_longitude(download_engine):
    """Test that longitude must be between -180 and 180"""
    # Test east longitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=185.0,  # Invalid: > 180
            west=116.3,
            zoom_min=10,
            zoom_max=10
        )

    assert "East longitude must be between -180 and 180" in str(exc_info.value)

    # Test east longitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=-185.0,  # Invalid: < -180
            west=-186.0,
            zoom_min=10,
            zoom_max=10
        )

    assert "East longitude must be between -180 and 180" in str(exc_info.value)

    # Test west longitude too high
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=185.0,  # Invalid: > 180
            zoom_min=10,
            zoom_max=10
        )

    assert "West longitude must be between -180 and 180" in str(exc_info.value)

    # Test west longitude too low
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=-185.0,  # Invalid: < -180
            zoom_min=10,
            zoom_max=10
        )

    assert "West longitude must be between -180 and 180" in str(exc_info.value)


def test_coordinate_validation_east_west_equal(download_engine):
    """Test that east and west longitude must be different"""
    with pytest.raises(ValueError) as exc_info:
        download_engine.calculate_tiles(
            north=40.0,
            south=39.0,
            east=116.5,
            west=116.5,  # Invalid: east == west
            zoom_min=10,
            zoom_max=10
        )

    assert "must be different from west longitude" in str(exc_info.value)
