"""C 组修复回归测试 —— 瓦片管线(task_manager / download_engine / models)。

覆盖:
- C5 : create_task 拒绝越界 output_path;task.name 消毒后才能拼进输出文件名
- I5 : failed 任务允许重新 start(续传入口:待下载集合由磁盘 cache 枚举推导)
- I8 : stitch 中间文件放每次 stitch 私有的临时目录,不读/不写/不删共享 cache
- I15: 预计瓦片数超过 WARN_TILES_THRESHOLD 只记警告不拒绝(0.1.4 起软阈值)
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from core.config import Config
    from core import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


def _params(**overrides):
    p = dict(
        name='t', north=40.0, south=39.0, east=117.0, west=116.0,
        zoom_min=10, zoom_max=11, style='roadmap',
        output_format='tiles_only', output_path='downloads',
    )
    p.update(overrides)
    return p


def _seed_task_row(name='t', status='pending', output_format='tiles_only',
                   output_path=None, total=0):
    from core.config import Config
    from core.database import get_connection

    if output_path is None:
        output_path = str(Config.DOWNLOADS_DIR)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES (?, ?, 1, 0, 1, 0, 10, 10, 'satellite', ?, ?, ?, 0, 0)
            """,
            (name, status, output_format, output_path, total),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _write_png_tile(path, size=16, value=128):
    """写一张真实可被 GDAL 打开的 PNG,模拟下载好的瓦片缓存。"""
    from osgeo import gdal

    path.parent.mkdir(parents=True, exist_ok=True)
    mem = gdal.GetDriverByName('MEM').Create('', size, size, 3, gdal.GDT_Byte)
    for band_idx in range(1, 4):
        mem.GetRasterBand(band_idx).Fill(value)
    png = gdal.GetDriverByName('PNG').CreateCopy(str(path), mem)
    assert png is not None, f"无法写入测试瓦片 {path}"
    png = None
    mem = None


# ---------- C5: output_path 越界拒绝 ----------

def test_create_task_rejects_output_path_outside_downloads(isolated_config):
    from services.task_manager import TaskManager

    tm = TaskManager()
    with pytest.raises(ValueError, match='output_path'):
        tm.create_task(_params(output_path='../../outside'))
    with pytest.raises(ValueError, match='output_path'):
        tm.create_task(_params(output_path='/etc/evil'))


def test_create_task_accepts_output_path_inside_downloads(isolated_config):
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(output_path='downloads'))
    assert isinstance(task_id, int)


# ---------- C5: task.name 消毒后才拼输出文件名 ----------

def test_stitch_output_filename_sanitizes_task_name(isolated_config):
    from core.config import Config
    from services.download_engine import DownloadEngine
    from services.task_manager import TaskManager

    # 完成态由磁盘 cache 推导(task_tiles 只存失败瓦片):把任务枚举出的
    # 全部瓦片写进 cache,拼接阶段才会认为它们已完成。
    engine = DownloadEngine()
    tiles = list(engine.iter_tiles(1, 0, 1, 0, 10, 10))
    for tile in tiles:
        _write_png_tile(tile.cache_path('s'))

    tm = TaskManager()  # 先建 manager,再插 running 行,免得被 orphan 回收降级

    task_id = _seed_task_row(name='../../evil', status='running',
                             output_format='image_only', total=len(tiles))

    asyncio.run(tm._execute_task(task_id))

    task_dir = Path(Config.DOWNLOADS_DIR) / f'task_{task_id}'
    produced = list(task_dir.glob('*.tif'))
    assert produced, f"拼接产物应生成在任务目录 {task_dir} 内"
    for p in produced:
        assert '..' not in p.name, f"输出文件名含有未消毒的 '..': {p.name}"
    assert not (isolated_config / 'evil_zoom_10.tif').exists(), (
        "任务名未消毒,拼接产物被写到了任务目录之外"
    )


# ---------- I5: failed 任务允许重新 start ----------

def test_failed_task_can_be_restarted(isolated_config):
    from core.database import get_connection
    from services.download_engine import DownloadEngine
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = _seed_task_row(status='failed')

    # 稀疏表语义下「无待下载瓦片」= 枚举出的瓦片全部已在 cache 里
    # (完成态从磁盘 cache 推导,不再看 task_tiles 的 pending/failed 行)。
    engine = DownloadEngine()
    for tile in engine.iter_tiles(1, 0, 1, 0, 10, 10):
        cache_path = tile.cache_path('s')
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'cached-tile')

    tm.start_task(task_id)  # 旧代码在这里 raise ValueError

    thread = tm.active_tasks.get(task_id)
    assert thread is not None, "failed 任务重新 start 后应有后台线程"
    thread.join(timeout=30)
    assert not thread.is_alive()

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT status FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()
    # 无待下载瓦片(全部命中 cache):重跑直接走到完成 —— 证明 start 这条路真的通了
    assert row['status'] == 'completed'


def test_completed_task_still_cannot_be_started(isolated_config):
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = _seed_task_row(status='completed')
    with pytest.raises(ValueError, match='Cannot start'):
        tm.start_task(task_id)


# ---------- I8: stitch 中间文件私有目录 ----------

def test_stitch_ignores_and_preserves_shared_cache_intermediates(isolated_config):
    """共享 cache 里的同名垃圾中间文件, stitch 既不读也不删。

    旧代码:中间文件按 style/z/x/y 命名写进共享 cache, exists() 短路会复用
    垃圾文件把拼接带崩;finally 又会把共享文件 unlink 掉(并发互删的根因)。
    修复后:中间文件在每次 stitch 私有的临时目录里生成, cache 只读瓦片本体。
    """
    from core.config import Config
    from models.task import Tile
    from services.download_engine import DownloadEngine, GEOREF_SUFFIX

    cache_dir = Path(Config.CACHE_DIR)
    out_dir = Path(Config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = DownloadEngine()
    zoom, x, y = 10, 843, 387
    tiles = [
        Tile(task_id=0, zoom=zoom, x=x, y=y),
        Tile(task_id=0, zoom=zoom, x=x + 1, y=y),
    ]
    for t in tiles:
        _write_png_tile(t.cache_path('m'), size=16)

    # 同名垃圾中间文件:旧代码的 exists() 短路会直接拿去喂 BuildVRT
    tile_png = tiles[0].cache_path('m')
    junk = tile_png.with_name(f"{tile_png.stem}{GEOREF_SUFFIX}.tif")
    junk.write_bytes(b'not a real geotiff')

    out_path = out_dir / 'mosaic.tif'
    engine.stitch_tiles_with_gdal(tiles, 'm', str(out_path), zoom, target_epsg=3857)

    assert out_path.exists(), "stitch 必须用自己生成的中间文件,不能被共享 cache 垃圾带崩"
    assert junk.read_bytes() == b'not a real geotiff', (
        "stitch 不得改写/删除共享 cache 里的文件(并发互删的根因)"
    )
    new_intermediates = [p for p in cache_dir.rglob(f'*{GEOREF_SUFFIX}.tif') if p != junk]
    assert new_intermediates == [], (
        f"stitch 不应在共享 cache 里写中间文件: {new_intermediates}"
    )


# ---------- I15: 瓦片数阈值（0.1.4 起为软阈值） ----------

def test_create_task_over_threshold_is_allowed(isolated_config, monkeypatch):
    """0.1.4 放开硬上限：超阈值只记警告、不拒绝创建
    （是否继续由用户在前端大任务确认框里决定，服务端不替用户做决定）。"""
    import services.task_manager as tm_mod

    monkeypatch.setattr(tm_mod, 'WARN_TILES_THRESHOLD', 5)
    tm = tm_mod.TaskManager()
    task_id = tm.create_task(_params())  # zoom 10-11 跨 1°x1°，远超 5 张
    assert isinstance(task_id, int)


def test_create_task_over_100k_tiles_for_real(isolated_config):
    """真实阈值下超 10 万张也能创建——守住「不再 400」的接线。"""
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(
        north=42.0, south=38.0, east=119.0, west=115.0,
        zoom_min=15, zoom_max=15,  # 约 17 万张瓦片
    ))
    assert isinstance(task_id, int)


def test_create_task_under_threshold_still_works(isolated_config):
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params())
    assert isinstance(task_id, int)
