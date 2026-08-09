"""
Database initialization and connection management for TerraForge
"""
import os
import sqlite3
import logging
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from src.core.config import Config

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
    # 手动 proxy_url 为空时自动发现可用代理（环境变量 / Windows PAC / 本机与
    # WSL 宿主的常见代理端口，逐个用真实瓦片验证）。见 services/proxy_autodetect。
    # 手动填了 proxy_url 就以手动值为准，这个开关不参与。
    ('proxy_auto_detect', 'true'),
    ('tile_servers', 'mts0,mts1,mts2,mts3'),
    # 地图底图源。与 tile_servers 分开的理由见 services/basemap_source.py：
    # 两者是不同用途的地址（底图给页面看、tile_servers 是下载源），不是不同的
    # 出网路径 —— 底图瓦片自 0.2.12 起由 routes/basemap_static.py 在服务端转发
    # （同源 /basemap/{z}/{x}/{y}），所以它**吃** proxy_url。默认 Esri 卫星影像。
    ('basemap_source', 'esri'),
    ('cache_enabled', 'true'),
    ('dem_cache_enabled', 'true'),
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
    # 解压后的全球底图与分卷同目录（assets/ 是随包分发的数据，downloads/ 是用户
    # 产出，解压出来的底图属于前者）。改这个默认值的同时**必须**跑
    # migrate_base_path_to_assets —— INSERT OR IGNORE 只对新建的库生效，存量行还是
    # 旧路径，两处不一致会让底图被判为不可用并掉进 heightmap 陷阱（见该函数）。
    ('terrain_global_base_path', './assets/terrain/base_z8'),
    # 随包分发的 base 实际只到 z7（z8 一层占 76% 体积、顶点间距 1.2 km 只在贴近看
    # DEM 外围时才用得上）。⚠️ 这个键**全项目零消费** —— 没有任何代码读它，base 的
    # 层级由 layer.json 的 available 决定。保留是为了兼容存量 config 行；真要用它
    # 之前先确认有没有第二处事实来源，否则又是一个「改了没反应」的假旋钮。
    # （注意上面的 terrain_global_base_path 不是零消费：terrain_static、
    # dem_task_manager、local_terrain_task_manager 三处都读它。）
    ('terrain_global_base_maxzoom', '7'),
    ('terrain_local_maxzoom', '14'),
    # 切片档位：precision / balanced / speed，语义是相对 maxzoom 的层级偏移
    # （+1 / 0 / -1）。取值表在 src/services/geo_validation.TILING_QUALITY_OFFSETS，
    # 这里只放默认值。选型实测见 docs/reference/terrain/tiling-presets-measured.md。
    ('terrain_quality_preset', 'balanced'),
    # 地形光照法线（oct 编码扩展段）。默认关：前端 enableLighting 默认也是关的，
    # 而法线吃 +35%~+100% 字节、约 2.1 倍切片时间，几何精度分毫不涨。
    # ⚠️ 关着切出来的瓦片，事后想开只能重切 —— 法线是烘焙进瓦片的。
    ('terrain_vertex_normals', 'false'),
    # 必须是**目录**：Cesium 会 appendForwardSlash() 后再拼 layer.json。
    # 带 /layer.json 会让它请求 .../layer.json/layer.json 得 404，而它的 404
    # 处理是塞一个假 heightmap 图层并污染共享 builder => 本任务自己的
    # quantized-mesh 瓦片也按 heightmap 解析，高程全错且不报错
    # （实测 4154 m 山峰解成 -744 m）。详见 layer_json.normalize_parent_url。
    ('terrain_base_parent_url', 'http://localhost:5000/terrain/base'),
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


def get_connection(check_same_thread: bool = True):
    """
    Get SQLite database connection with Row factory and foreign keys enabled

    Args:
        check_same_thread: 传 False 才允许跨线程使用该连接。默认 True（sqlite3
            的默认值）——绝大多数调用方都是「建连接、用完关掉」，同线程即可。
            唯一的 False 使用者是 task_manager 的进度攒批连接：它在下载事件
            循环上建立，实际写盘被 asyncio.to_thread 挪到工作线程执行（M3）。
            SQLite 本身默认编译为 serialized 模式，跨线程共享连接是安全的，
            check_same_thread 只是 Python 层的线程亲和断言；但调用方仍必须
            自行保证同一时刻只有一个线程在用它（那边靠 in-flight 标志串行化）。

    Returns:
        sqlite3.Connection: Database connection with Row factory enabled

    Note:
        Caller is responsible for closing the connection.
        Consider using get_connection_context() for automatic cleanup.
    """
    conn = sqlite3.connect(Config.DATABASE_PATH, check_same_thread=check_same_thread)
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


def normalize_default_save_path(cursor) -> None:
    """把 config 表里的相对 default_save_path 归一成绝对路径（幂等）。

    0.2.3 起保存路径一律绝对（建任务/配置保存都拒相对值），UI 不该再有相对值
    可显示。相对值的历史语义是「相对 BASE_DIR/CWD」（'./downloads' 就是
    DOWNLOADS_DIR 本身），不是 resolve_output_dir 的「相对 DOWNLOADS_DIR」——
    按后者归一会把 './downloads' 错置成 downloads/downloads。

    已是绝对值时跳过；归一结果越界的保留原值只警告 —— 启动不能因配置值崩掉，
    用户在配置页改正即可。

    **两个调用点（M6）**：`init_database()` 与 `ConfigManager.reset_to_defaults()`。
    后者 DELETE 全表再按 DEFAULT_CONFIGS 重插，会把 `'./downloads'` 这个
    **validate_config 自己会判非法**的值写回库，之后地图/DEM 建任务全部 400
    （等高线与本地地形的 output_path 硬编码，不读该键，不受影响）。归一必须
    跟着走，否则 reset 就是一条绕开校验的旁路。
    """
    from pathlib import Path as _Path

    cursor.execute("SELECT value FROM config WHERE key = 'default_save_path'")
    row = cursor.fetchone()
    if not row:
        return
    _raw = row[0] or ''
    _p = _Path(_raw).expanduser()
    if not _raw or _p.is_absolute():
        return
    try:
        _root = _Path(Config.DOWNLOADS_DIR).resolve()
        _cand = (_root.parent / _p).resolve()
        if _cand == _root or _root in _cand.parents:
            cursor.execute(
                "UPDATE config SET value = ? WHERE key = 'default_save_path'",
                (str(_cand),))
            logger.info(f'Normalized default_save_path to absolute: {_cand}')
        else:
            logger.warning(
                f'default_save_path 相对值 {_raw!r} 归一后越出 '
                f'DOWNLOADS_DIR,保留原值(请在配置页改成绝对路径)')
    except Exception as e:
        logger.warning(f'default_save_path 归一化跳过({e!r}),保留原值')


_OUTPUT_PATH_TABLES = ('tasks', 'dem_tasks', 'contour_tasks', 'local_terrain_tasks')


def normalize_stored_output_paths(cursor) -> int:
    """把四张任务表里的相对 output_path 一次性归一成绝对路径（M10）。

    返回改写的行数。用 `PRAGMA user_version` 做幂等标记（>=2 时直接跳过），
    与稀疏失败表那次迁移同一套做法 —— 避免每次启动都全表扫描持写锁。

    **为什么必须做**：0.2.3 起 create_task 入库的已经是绝对路径，但**存量行
    从未被归一过**，于是同一个字段在下游有多套解释并存（写/删除侧、读侧、
    按进程 CWD）。收敛解析口径只解决了「以后」，这段负责把「以前」也拉齐 ——
    否则解析歧义会永久保留在数据里。受影响的是 commit 38e3e30fc（2026-07-29，
    约 v0.0.9 及更早的多个真实发布版本）之前建的任务行。

    归一用 `resolve_stored_output_dir`（延迟 import 避免 core → services 的
    模块级依赖），与读侧、删除侧共用同一套规则。
    """
    from src.services.task_cleanup import resolve_stored_output_dir

    if cursor.execute('PRAGMA user_version').fetchone()[0] >= 2:
        return 0

    changed = 0
    for table in _OUTPUT_PATH_TABLES:
        try:
            rows = cursor.execute(
                f'SELECT id, output_path FROM {table}').fetchall()
        except Exception:
            continue  # 表不存在（旧库）时跳过
        for row in rows:
            raw = (row['output_path'] if hasattr(row, 'keys') else row[1]) or ''
            if not raw:
                continue
            from pathlib import Path as _Path
            if _Path(str(raw)).expanduser().is_absolute():
                continue
            try:
                resolved = str(resolve_stored_output_dir(raw))
            except Exception as e:
                logger.warning(f'{table}#{row[0]} output_path 归一化跳过（{e!r}）')
                continue
            cursor.execute(
                f'UPDATE {table} SET output_path = ? WHERE id = ?',
                (resolved, row[0] if not hasattr(row, 'keys') else row['id']))
            changed += 1

    cursor.execute('PRAGMA user_version = 2')
    if changed:
        logger.info(
            f'Normalized {changed} legacy relative output_path row(s) '
            f'to absolute (user_version=2)')
    return changed


_OLD_BASE_PATH = './downloads/terrain/base_z8'
_NEW_BASE_PATH = './assets/terrain/base_z8'


def migrate_base_path_to_assets(cursor) -> bool:
    """底图缓存位置 downloads/ → assets/ 的一次性迁移（user_version 2 → 3）。

    **只改 DEFAULT_CONFIGS 不够**：它走 INSERT OR IGNORE，只对新建的库生效。
    存量库那行还是旧路径，于是解压去新位置、服务与可用性判定按旧位置 → 底图
    判为不可用 → 走 parentUrl 兜底 → 那个 URL 指向服务旧空路径的 /terrain/base
    → 404 → Cesium 塞假 heightmap 图层污染共享 builder → 任务自己的
    quantized-mesh 瓦片也按 heightmap 解析，高程全错且零报错。正是 v0.2.8 刚修过
    的那条链。

    只在该行**仍等于旧默认值**时改写：用户自定义过的路径不动。
    旧位置已有底图时搬过去，不删掉重解压 224 MB —— 但**必须原子落地**：同盘先试
    `os.replace`（一次改名）；跨盘退化成 copytree 时先拷进同级 `.part` 暂存目录、
    拷完才 `os.replace` 上去。直接 `shutil.move` 到最终位置是不行的：跨盘时它退化成
    copytree+rmtree，可被中断，而 `os.walk` 先拷根级文件 —— 中断后典型状态是
    **有 layer.json 没有瓦片层级**，而 `layer_json.parent_url_if_base_available`
    只看 layer.json 判可用，于是又回到上面那条「高程全错且零报错」的链，并且因为
    user_version 已经推到 3 而永不重试。见 docs/reviews/2026-08-08-full-project-review.md 的 T2。
    搬不动就留着旧目录，新位置重新解压 —— 多占一份磁盘，但不会坏。

    整段包在 try 里且 `PRAGMA user_version = 3` 无条件执行：迁移出问题也不能
    阻断启动，更不能每次启动重试一遍（config 表缺失的畸形库会无限刷 warning）。
    这条不变 —— 上面的原子性保证了「没搬成」等价于「没搬」，重解压那条路仍然通。
    """
    if cursor.execute('PRAGMA user_version').fetchone()[0] >= 3:
        return False

    changed = False
    try:
        row = cursor.execute(
            "SELECT value FROM config WHERE key = 'terrain_global_base_path'"
        ).fetchone()
        current = (row['value'] if row is not None and hasattr(row, 'keys')
                   else (row[0] if row else None))
        if current is not None and str(current).strip() == _OLD_BASE_PATH:
            cursor.execute(
                "UPDATE config SET value = ? WHERE key = 'terrain_global_base_path'",
                (_NEW_BASE_PATH,))
            changed = True

            old_dir = Path(Config.DOWNLOADS_DIR) / 'terrain' / 'base_z8'
            new_dir = Path(Config.BASE_DIR) / 'assets' / 'terrain' / 'base_z8'
            if old_dir.is_dir() and not new_dir.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                staging = new_dir.with_name(new_dir.name + '.part')
                try:
                    try:
                        # 同盘:一次改名,不可能留半棵树。
                        os.replace(old_dir, new_dir)
                    except OSError:
                        # 跨盘(含 Windows 跨卷):拷到同级 .part,拷完才改名上去。
                        shutil.rmtree(staging, ignore_errors=True)
                        shutil.copytree(old_dir, staging)
                        os.replace(staging, new_dir)
                        shutil.rmtree(old_dir, ignore_errors=True)
                    logger.info(f'底图缓存已搬到 {new_dir}')
                except OSError as e:
                    # 暂存目录必须清掉:留着就是 224 MB 无主残留,五类启动清扫都不认它。
                    shutil.rmtree(staging, ignore_errors=True)
                    logger.warning(
                        f'底图缓存搬迁失败（{e!r}），旧目录保留在 {old_dir}，'
                        f'新位置会重新解压')
    except Exception as e:
        logger.warning(f'terrain_global_base_path 迁移跳过（{e!r}）')

    cursor.execute('PRAGMA user_version = 3')
    if changed:
        logger.info('terrain_global_base_path 迁移到 assets/ (user_version=3)')
    return changed


# 迁移后写进 error_message 的说明：没有它，用户看到的是一条没有原因的失败。
_CANCELLED_MIGRATION_NOTE = '此版本移除了「取消任务」，该任务原为已取消'


def migrate_cancelled_tasks_to_failed(cursor) -> int:
    """存量 'cancelled' 行迁成 'failed'（「取消任务」被移除后的一次性收尾）。

    枚举里没有 cancelled 之后，后端读这些行是安全的（`Task.from_row` 走
    `cls.__new__` 不校验，所有状态判定都是 `IN ('pending','running','paused')`
    的正列表语义），坏的是**渲染层**：前端两张状态词表跟着枚举收敛到五态，
    老行落到 `|| '未知'` 兜底，用户看到一列「未知」。

    为什么迁成 failed 而不是别的：failed 是终态，语义最接近「没跑完」；而且
    start_task 已经收回了 failed 白名单，这批陈年任务不会诈尸回活动列表。
    **绝不能迁成 paused** —— start_task 收 paused，那等于把它们全部复活成
    「可恢复」。error_message 写明来历，否则用户只会看到一条没有原因的失败。

    `WHERE status='cancelled'` 天然幂等：迁完就没有行可匹配，不需要
    user_version 闸门（新库同样是零行匹配的空转）。
    """
    moved = 0
    for table in ('tasks', 'dem_tasks', 'contour_tasks', 'local_terrain_tasks'):
        cursor.execute(
            f"UPDATE {table} SET status='failed', error_message=? "
            "WHERE status='cancelled'",
            (_CANCELLED_MIGRATION_NOTE,))
        moved += cursor.rowcount
    if moved:
        logger.info(f"{moved} 条已取消任务迁移为 failed（「取消任务」已移除）")
    return moved


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
                quality TEXT DEFAULT 'balanced',
                -- DEFAULT NULL 与下面的 effective_maxzoom 同理、理由更硬：0 是
                -- 合法取值（「明确关闭法线」），不能再兼职当「未知」用。
                -- 三态：NULL = 这一行没有记录过法线状态，0 = 明确关，1 = 明确开。
                -- 为什么不能回填 0：本列出现之前切的作业，切片器的默认是**开**
                -- （docs/reference/terrain/tiling-presets-measured.md 第三节，
                -- 落地前默认 = auto + 法线开），回填 0 会让详情面板对着一批真的
                -- 带光照的产物断言「未开启（无光照数据）」—— 正好说反，而且用户
                -- 无从分辨。宁可显示「未知」。
                -- 已知局限：v0.2.13 装过的库里，存量行已被迁移回填成 0，和「用户
                -- 真的关了法线」在库里逐位相同，无法区分 —— 所以刻意不做数据修
                -- 复迁移：猜错方向的批量改写比一个错标签更糟。
                vertex_normals INTEGER DEFAULT NULL,
                -- 切片**实际**切到的最深层级（档位偏移 + [0,21] 钳位之后，由
                -- build_terrain 回报）。maxzoom 那一列存的是用户填的**基准**
                -- 层级，precision/speed 两档下两者差 1 —— 详情面板与 layer.json
                -- 对不上就是因为此前只有基准值落库。NULL = 还没切完 / 存量行，
                -- 消费方回落到 maxzoom 并说明那是基准值。
                effective_maxzoom INTEGER DEFAULT NULL,
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

        # 待删产物清单。删除任务时若用户选了「同时删除磁盘产物」，先在这里记
        # 一行、再删任务行（同一事务）；后台清理线程删成功后清掉该行。进程被
        # 强杀（SIGKILL / 关窗）时行会留下来，由启动清扫补删
        # （task_cleanup._sweep_pending_deletions）。
        #
        # 刻意没有外键：任务行先删、这行后删，反过来关联就悬空了 —— 这张表存在
        # 的意义恰恰是「任务已经不在了，但产物还在」。
        # path UNIQUE：同一目录重复入队没有意义，用 INSERT OR IGNORE 幂等。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 保留产物登记表。删任务时用户**没**勾「同时删除磁盘产物」的那一支:
        # 任务行(以及 ON DELETE CASCADE 掉的 dem_terrain_jobs 行)一走,
        # <output_path>/<pipeline>_task_<id>/ 这个目录就再没有任何 DB 引用 ——
        # 启动清扫只认 pending_deletions 和几张任务表,从此永远扫不到它。
        # 代价不只是「用户自己知道文件在哪」:多幅 DEM 物化的中间栅格
        # (cesiumlab_terrain_<pid>_*.tif,与源数据同量级)就落在这个目录直下,
        # 而 task_cleanup._materialised_sweep_roots 的扫描根正是从
        # dem_terrain_jobs.output_dir 推出来的 —— 引用一断,GB 级残留同时失去
        # 唯一的回收入口。
        #
        # 为什么不复用 pending_deletions 加一个 flag:那张表的每一个消费者读到
        # 一行的含义都是「删掉它」,往里塞一行「永远不许删」是给下一个维护者埋雷
        # (2026-08-08 评审 P1#6 就是这一类误读)。两张表、两个动词,不混。
        # 同样刻意没有外键:任务行先删、这行后写,关联天生悬空。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS retained_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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
                quality TEXT DEFAULT 'balanced',
                -- 三态，语义同 dem_terrain_jobs.vertex_normals（NULL = 未记录）。
                vertex_normals INTEGER DEFAULT NULL,
                -- 实际切到的最深层级，语义同 dem_terrain_jobs.effective_maxzoom。
                effective_maxzoom INTEGER DEFAULT NULL,
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

        # contour_tasks 是四张任务表里唯一漏了 status 索引的一张（另三张都有
        # idx_*_status）。它的按状态查询有两处:list_tasks 的 status='active'
        # 展开、以及 __init__ 里的孤儿恢复。表小,今天的运行代价可以忽略 ——
        # 补上是为了消掉这份不对称:下一个在这里加状态查询的人既没有索引、
        # 也没有任何信号提示本该有一个。IF NOT EXISTS 让存量库下次启动自动补建,
        # 不需要 user_version 迁移。
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contour_tasks_status
            ON contour_tasks(status)
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
            # 非空表示该等高线任务的源 DEM 是某个已完成 DEM 下载任务的目录
            # （零拷贝引用，源文件不拷进本任务目录），此时 dataset='dem_task'。
            ("contour_tasks", "source_dem_task_id INTEGER DEFAULT NULL"),
            # DEM 地形切片进度（dem_task_manager._run_tiling_job 节流落库，
            # 前端详情弹窗轮询读取）：渲染中 rendered_tiles 是 processed 进度。
            ("dem_terrain_jobs", "rendered_tiles INTEGER DEFAULT 0"),
            ("dem_terrain_jobs", "total_tiles INTEGER DEFAULT 0"),
            # 三档预设（precision/balanced/speed）与法线开关。两张地形任务表
            # 都要：DEM 切片走 dem_terrain_jobs，本地地形走 local_terrain_tasks。
            # 必须带 DEFAULT —— tests/test_terrain_api.py 有不列新列的裸 INSERT。
            ("dem_terrain_jobs", "quality TEXT DEFAULT 'balanced'"),
            # vertex_normals 的 DEFAULT NULL 与下面 effective_maxzoom 同理，
            # 但更要命：0 是合法取值（明确关闭），拿它兼职「未知」就等于对存量
            # 行撒谎 —— 本列出现之前切的作业，切片器默认是**开**法线
            # （docs/reference/terrain/tiling-presets-measured.md 第三节），
            # 回填 0 会让详情面板断言「未开启（无光照数据）」，正好说反。
            # 存量行必须留在 NULL，面板据此显示「未知」。
            # 已知局限：v0.2.13 装过的库已经把存量行回填成 0，与「用户真的关了」
            # 在库里逐位相同 —— 不做数据修复迁移，猜错方向的批量改写更糟。
            ("dem_terrain_jobs", "vertex_normals INTEGER DEFAULT NULL"),
            ("local_terrain_tasks", "quality TEXT DEFAULT 'balanced'"),
            ("local_terrain_tasks", "vertex_normals INTEGER DEFAULT NULL"),
            # 实际切到的最深层级（档位偏移 + 钳位之后的产物事实）。同样两张表
            # 都要。DEFAULT NULL 是有语义的、不是省事：0 是合法层级，不能拿它
            # 当「未知」用，存量行与未切完的作业必须与「真的切到了 z0」可区分。
            ("dem_terrain_jobs", "effective_maxzoom INTEGER DEFAULT NULL"),
            ("local_terrain_tasks", "effective_maxzoom INTEGER DEFAULT NULL"),
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

        normalize_default_save_path(cursor)
        normalize_stored_output_paths(cursor)
        migrate_base_path_to_assets(cursor)
        migrate_cancelled_tasks_to_failed(cursor)

        conn.commit()
        logger.info('Database initialized successfully')

    except sqlite3.Error as e:
        logger.error(f'Database initialization failed: {e}')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    init_database()
