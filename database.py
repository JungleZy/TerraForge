"""
Database initialization and connection management for Google Maps Downloader
"""
import sqlite3
import logging
from contextlib import contextmanager
from config import Config

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULT_CONFIGS = [
    ('default_save_path', './downloads'),
    ('default_style', 'm'),
    ('default_zoom_min', '10'),
    ('default_zoom_max', '15'),
    ('default_output_format', 'both'),
    ('concurrent_downloads', '10'),
    ('request_timeout', '30'),
    ('max_retries', '3'),
    ('proxy_url', ''),
    ('tile_servers', 'mts0,mts1,mts2,mts3'),
    ('cache_enabled', 'true'),
    ('cache_max_size_mb', '1000'),
    ('dem_cache_enabled', 'true'),
    ('history_retention_days', '90'),
    ('map_center_lat', '39.9'),
    ('map_center_lng', '116.4'),
    ('map_initial_zoom', '10'),
    ('gdal_compression', 'LZW'),
    ('gdal_resampling', 'cubic'),
    # Earthdata Login (for NASA LP DAAC protected datasets, e.g., ASTGTM.003)
    ('earthdata_username', ''),
    ('earthdata_password', ''),
    # Terrain defaults
    ('terrain_global_base_path', './downloads/terrain/base_z8'),
    ('terrain_global_base_maxzoom', '8'),
    ('terrain_local_maxzoom', '14'),
    # Contour (等高线) defaults
    ('contour_default_interval', '50'),
    ('contour_color_intermediate', '#9C6B3F'),
    ('contour_color_index', '#7A4F2A'),
    ('contour_color_label', '#7A4F2A'),
    ('contour_width_intermediate', '0.5'),
    ('contour_width_index', '1.2'),
    ('contour_background', '#FFFFFF'),
    ('contour_index_step', '5'),
]


def get_connection():
    """
    Get SQLite database connection with Row factory and foreign keys enabled

    Returns:
        sqlite3.Connection: Database connection with Row factory enabled

    Note:
        Caller is responsible for closing the connection.
        Consider using get_connection_context() for automatic cleanup.
    """
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    # Enable foreign key constraints
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


@contextmanager
def get_connection_context():
    """
    Context manager for database connections with automatic cleanup

    Usage:
        with get_connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute(...)
            conn.commit()

    Yields:
        sqlite3.Connection: Database connection with automatic cleanup
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_database():
    """
    Initialize database schema and default configuration

    Creates:
        - tasks table: stores download task information
        - task_tiles table: stores individual tile download status
        - config table: stores application configuration

    Inserts default configuration values for all settings

    Raises:
        sqlite3.Error: If database initialization fails
    """
    # Initialize application directories
    Config.init_app()

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Create tasks table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                north REAL NOT NULL,
                south REAL NOT NULL,
                east REAL NOT NULL,
                west REAL NOT NULL,
                zoom_min INTEGER NOT NULL,
                zoom_max INTEGER NOT NULL,
                style TEXT NOT NULL,
                output_format TEXT NOT NULL,
                output_path TEXT NOT NULL,
                total_tiles INTEGER DEFAULT 0,
                downloaded_tiles INTEGER DEFAULT 0,
                failed_tiles INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        # Create index on tasks(status) for performance
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
        ''')

        # Time tracking support (backwards compatible with older DBs)
        try:
            cursor.execute('''
                ALTER TABLE tasks
                ADD COLUMN total_running_seconds INTEGER DEFAULT 0
            ''')
            logger.info("Added total_running_seconds column to tasks table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass
            else:
                raise

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_time_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('start', 'pause', 'resume', 'complete')),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_time_records_task_id
            ON task_time_records(task_id, timestamp DESC)
        ''')

        # Create task_tiles table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_tiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                zoom INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, zoom, x, y)
            )
        ''')

        # Create index on task_tiles for performance
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_tiles_status
            ON task_tiles(task_id, status)
        ''')

        # Create config table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create DEM tasks table (e.g., ASTER GDEM V3 / ASTGTM.003)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dem_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                north REAL NOT NULL,
                south REAL NOT NULL,
                east REAL NOT NULL,
                west REAL NOT NULL,
                dataset TEXT NOT NULL,
                output_path TEXT NOT NULL,
                download_num INTEGER DEFAULT 0,
                download_swb INTEGER DEFAULT 0,
                total_files INTEGER DEFAULT 0,
                downloaded_files INTEGER DEFAULT 0,
                failed_files INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_dem_tasks_status
            ON dem_tasks(status)
        ''')

        # Create DEM files table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dem_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                granule_id TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                local_path TEXT,
                size_bytes INTEGER,
                FOREIGN KEY (task_id) REFERENCES dem_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, granule_id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_dem_files_status
            ON dem_files(task_id, status)
        ''')

        # Create DEM terrain jobs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dem_terrain_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                maxzoom INTEGER NOT NULL,
                parent_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES dem_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_dem_terrain_jobs_status
            ON dem_terrain_jobs(status)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS local_terrain_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT NOT NULL,
                source_dir TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                total_files INTEGER DEFAULT 0,
                uploaded_files INTEGER DEFAULT 0,
                failed_files INTEGER DEFAULT 0,
                maxzoom INTEGER NOT NULL,
                parent_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_local_terrain_tasks_status
            ON local_terrain_tasks(status)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS local_terrain_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                original_filename TEXT,
                stored_filename TEXT NOT NULL,
                local_path TEXT,
                size_bytes INTEGER,
                status TEXT NOT NULL DEFAULT 'uploaded',
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES local_terrain_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, stored_filename)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_local_terrain_files_status
            ON local_terrain_files(status)
        ''')

        # 等高线任务表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contour_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                north REAL NOT NULL,
                south REAL NOT NULL,
                east REAL NOT NULL,
                west REAL NOT NULL,
                dataset TEXT NOT NULL DEFAULT 'ASTGTM.003',
                contour_interval REAL NOT NULL,
                background TEXT DEFAULT '#FFFFFF',
                zoom_min INTEGER NOT NULL,
                zoom_max INTEGER NOT NULL,
                output_path TEXT,
                total_files INTEGER DEFAULT 0,
                downloaded_files INTEGER DEFAULT 0,
                failed_files INTEGER DEFAULT 0,
                total_tiles INTEGER DEFAULT 0,
                rendered_tiles INTEGER DEFAULT 0,
                failed_tiles INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        # 等高线 DEM 文件表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contour_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                granule_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                local_path TEXT,
                size_bytes INTEGER,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES contour_tasks(id) ON DELETE CASCADE
            )
        ''')

        # Per-task contour background (backwards compatible with older DBs).
        # SQLite fills existing rows with the constant default '#FFFFFF'.
        try:
            cursor.execute('''
                ALTER TABLE contour_tasks
                ADD COLUMN background TEXT DEFAULT '#FFFFFF'
            ''')
            logger.info("Added background column to contour_tasks table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass
            else:
                raise

        # Insert default configuration values using executemany for efficiency
        cursor.executemany(
            'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
            DEFAULT_CONFIGS
        )

        conn.commit()
        logger.info('Database initialized successfully')

    except sqlite3.Error as e:
        logger.error(f'Database initialization failed: {e}')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    init_database()
