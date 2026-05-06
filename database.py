"""
Database initialization and connection management for Google Maps Downloader
"""
import sqlite3
from config import Config


def get_connection():
    """
    Get SQLite database connection with Row factory

    Returns:
        sqlite3.Connection: Database connection with Row factory enabled
    """
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    """
    Initialize database schema and default configuration

    Creates:
        - tasks table: stores download task information
        - task_tiles table: stores individual tile download status
        - config table: stores application configuration

    Inserts default configuration values for all settings
    """
    # Initialize application directories
    Config.init_app()

    conn = get_connection()
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

    # Insert default configuration values
    default_configs = [
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
        ('history_retention_days', '90'),
        ('map_center_lat', '39.9'),
        ('map_center_lng', '116.4'),
        ('map_initial_zoom', '10'),
        ('gdal_compression', 'LZW'),
        ('gdal_resampling', 'cubic'),
    ]

    for key, value in default_configs:
        cursor.execute(
            'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
            (key, value)
        )

    conn.commit()
    conn.close()

    print('Database initialized successfully')


if __name__ == '__main__':
    init_database()
