"""
Database initialization and connection management for TerraForge
"""
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from core.config import Config

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串（带 +00:00 时区标记）。

    所有手写时间戳（started_at/completed_at/task_time_records 等）统一走这里：
    存字符串而非 datetime 对象（Python 3.12 起 sqlite3 默认 datetime 适配器
    已弃用），且带时区标记，前端可直接 new Date() 正确解析。表默认值
    CURRENT_TIMESTAMP 本身是 UTC，保留不动。
    """
    return utc_now().isoformat(timespec='seconds')


def parse_db_timestamp(value) -> datetime:
    """Parse a DB timestamp string into an aware UTC datetime.

    新格式带时区标记直接解析；历史遗留的裸格式（'YYYY-MM-DD HH:MM:SS[.ffffff]'，
    本地时间或 CURRENT_TIMESTAMP 的 UTC）按 UTC 处理 —— 旧本地时间行会有
    一个时区的偏移，可接受，好过 aware/naive 相减直接 TypeError。
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

# Default configuration values
DEFAULT_CONFIGS = [
    ('default_save_path', './downloads'),
    ('default_style', 'm'),
    ('default_zoom_min', '10'),
    ('default_zoom_max', '15'),
    ('default_output_format', 'both'),
    ('concurrent_downloads', '50'),
    ('request_timeout', '30'),
    ('max_retries', '3'),
    ('proxy_url', ''),
    ('tile_servers', 'mts0,mts1,mts2,mts3'),
    ('cache_enabled', 'true'),
    ('cache_max_size_mb', '1000'),
    ('dem_cache_enabled', 'true'),
    ('history_retention_days', '90'),
    ('map_center_lat', '29.56'),
    ('map_center_lng', '106.55'),
    ('map_initial_zoom', '3'),
    ('gdal_compression', 'LZW'),
    ('gdal_resampling', 'cubic'),
    # 瓦片拼接(stitch)中间产物临时目录;留空 = 系统临时目录。
    # 形制同 contour_warp_tmpdir,配置页同样不暴露(高级排障键)。
    ('stitch_tmpdir', ''),
    # Earthdata Login (for NASA LP DAAC protected datasets, e.g., ASTGTM.003)
    ('earthdata_username', ''),
    ('earthdata_password', ''),
    # Terrain defaults
    ('terrain_global_base_path', './downloads/terrain/base_z8'),
    ('terrain_global_base_maxzoom', '8'),
    ('terrain_local_maxzoom', '14'),
    ('terrain_base_parent_url', 'http://localhost:5000/terrain/base/layer.json'),
    # Contour (等高线) defaults
    ('contour_default_interval', '50'),
    ('contour_warp_tmpdir', ''),
    ('contour_color_intermediate', '#9C6B3F'),
    ('contour_color_index', '#7A4F2A'),
    ('contour_color_label', '#7A4F2A'),
    ('contour_width_intermediate', '0.5'),
    ('contour_width_index', '1.2'),
    ('contour_background', '#FAF6EC'),
    ('contour_index_step', '5'),
    ('contour_detail_zoom', '14'),
    ('contour_zoom_scaling', 'standard'),
    # Terrain coloring: hypsometric tints + hillshade + water (ASTWBD).
    # breaks = N elevation breakpoints (m) -> N+1 color bands (incl. <first and >last).
    ('contour_hypsometric_breaks', '0,200,500,1000,2000,3000,4000,5000'),
    ('contour_hypsometric_colors', '#5E8C61,#8FBF6F,#B6CF7E,#DCD98E,#D9B97E,#C49A6C,#AC7F58,#8E6246,#F0EAE2'),
    ('contour_hillshade_azimuth', '315'),
    ('contour_hillshade_altitude', '45'),
    ('contour_hillshade_vert_exag', '1.0'),
    ('contour_hillshade_blend', 'soft'),
    ('contour_water_color_ocean', '#6BAED6'),
    ('contour_water_color_inland', '#9ECAE1'),
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
    # WAL + busy_timeout：下载线程高频写进度，HTTP 线程并发读，默认 rollback
    # journal 下读写互斥、写者遇锁立即报 "database is locked"。WAL 让读不阻塞写，
    # busy_timeout 让写者等待而非立刻报错。busy_timeout 必须先于 journal_mode 设置：
    # 切换 journal_mode 本身也要拿库锁，多实例同时启动时若 busy_timeout 还没生效，
    # journal_mode 这一步就直接 database is locked。journal_mode 持久化在库文件上，
    # busy_timeout 是每连接设置，所以在唯一的连接入口统一开启（init_database
    # 也走这里）。对 tmp_path 测试库同样生效。
    # synchronous 也是每连接设置：WAL 下 NORMAL 只在 checkpoint 时刷盘，崩溃
    # 不损库（至多丢最后一个已提交事务），比默认 FULL 省大量 fsync。
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
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
        - task_tiles table: sparse table of failed tiles only (completion state
          is derived from the on-disk tile cache, not from this table)
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

        # 历史列表(/api/history、history_all)按 created_at DESC 排序分页,
        # 无索引时任务多了每次列表都全表排序
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at
            ON tasks(created_at)
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

        # 迁移(稀疏失败表重构):task_tiles 不再物化「每块瓦片一行」—— 瓦片
        # 集合是 bbox+zoom 的纯函数,可由 DownloadEngine.iter_tiles 确定性
        # 枚举;完成态以磁盘 cache 文件为准,表里只保留失败瓦片。清掉旧版本
        # 写入的 pending/completed 全量行(大任务可达数十万行);恢复下载走
        # cache 枚举,不依赖它们。failed 行保留 —— 恢复时要重试这些瓦片。
        # 用 user_version 做一次性幂等标记:只对旧库(version < 1)执行一次大
        # DELETE,之后启动直接跳过,避免每次启动都全表扫描持写锁。
        if cursor.execute('PRAGMA user_version').fetchone()[0] < 1:
            cursor.execute("DELETE FROM task_tiles WHERE status != 'failed'")
            cursor.execute('PRAGMA user_version = 1')
            logger.info("Migrated task_tiles to sparse failed-only table (user_version=1)")

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

        # list_tasks 按 created_at DESC 排序(与 tasks 表同理)
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_dem_tasks_created_at
            ON dem_tasks(created_at)
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
                rendered_tiles INTEGER DEFAULT 0,
                total_tiles INTEGER DEFAULT 0,
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

        # list_tasks 按 created_at DESC 排序(与 tasks 表同理)
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_local_terrain_tasks_created_at
            ON local_terrain_tasks(created_at)
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
                background TEXT DEFAULT '#FAF6EC',
                terrain_shade INTEGER DEFAULT 1,
                water INTEGER DEFAULT 1,
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

        # list_tasks 按 created_at DESC 排序(与 tasks 表同理)
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contour_tasks_created_at
            ON contour_tasks(created_at)
        ''')

        # 等高线 DEM 文件表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contour_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                granule_id TEXT NOT NULL,
                kind TEXT DEFAULT 'dem',
                status TEXT NOT NULL DEFAULT 'pending',
                local_path TEXT,
                size_bytes INTEGER,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY (task_id) REFERENCES contour_tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, granule_id)
            )
        ''')

        # 与 dem_files 对齐的唯一约束：新库靠上面的 UNIQUE(task_id, granule_id)，
        # 存量库（CREATE TABLE IF NOT EXISTS 不会改旧表）补唯一索引兜底。
        # 建索引前先删重复行（保留最小 rowid），否则 CREATE UNIQUE INDEX 直接失败。
        # 用索引名做一次性幂等标记：索引已存在就跳过去重扫描。
        if not cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_contour_files_task_granule'"
        ).fetchone():
            cursor.execute('''
                DELETE FROM contour_files
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM contour_files GROUP BY task_id, granule_id
                )
            ''')
            cursor.execute('''
                CREATE UNIQUE INDEX idx_contour_files_task_granule
                ON contour_files(task_id, granule_id)
            ''')

        # Per-task contour background (backwards compatible with older DBs).
        # SQLite fills existing rows with the constant default '#FAF6EC'.
        try:
            cursor.execute('''
                ALTER TABLE contour_tasks
                ADD COLUMN background TEXT DEFAULT '#FAF6EC'
            ''')
            logger.info("Added background column to contour_tasks table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass
            else:
                raise

        # Terrain coloring columns (backwards compatible with older DBs).
        for table, coldef in (
            ("contour_tasks", "terrain_shade INTEGER DEFAULT 1"),
            ("contour_tasks", "water INTEGER DEFAULT 1"),
            ("contour_files", "kind TEXT DEFAULT 'dem'"),
            # 按任务自定义配色（空串 = 用 ContourStyle 的默认方案）：
            # 普通等高线 / 计曲线颜色，分层设色断点与颜色（CSV，颜色数 = 断点数+1）。
            ("contour_tasks", "line_color_intermediate TEXT DEFAULT ''"),
            ("contour_tasks", "line_color_index TEXT DEFAULT ''"),
            ("contour_tasks", "tint_breaks TEXT DEFAULT ''"),
            ("contour_tasks", "tint_colors TEXT DEFAULT ''"),
            # DEM 地形切片进度（dem_task_manager._run_tiling_job 节流落库，
            # 前端详情弹窗轮询读取）：渲染中 rendered_tiles 是 processed 进度。
            ("dem_terrain_jobs", "rendered_tiles INTEGER DEFAULT 0"),
            ("dem_terrain_jobs", "total_tiles INTEGER DEFAULT 0"),
        ):
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
                logger.info(f"Added column '{coldef}' to {table}")
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
