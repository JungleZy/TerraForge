"""2026-08-09 全项目评审的两条 P0 + 一条同族 P1。

三条都属于本仓反复栽的同一类：**静默产出错数据** —— 产物看着正常、任务
completed、日志零告警，用户只能自己拿 GIS 去量才发现不对。

- **T1**（`cesium_terrain`）：`available` 把「与 DEM 有交集」当成「有数据」。
  交集可以只有一个像素，而 DEM 之外的采样点被 `DemSampler` 用最外圈源像素钳位
  外推成台地 —— 于是一张 99.5% 是假地形的瓦片被声明为可用，Cesium 取首个声明
  可用的层，底图（只到 z7）永远没机会出场。
- **T2**（`task_manager`）：拼接段拿到的是 `completed_tiles`，失败瓦片根本不在
  里面，于是用一个比任务网格小的集合拼出一张「完整」的图；恢复时
  「文件在就跳过」又把那张残缺图当成品保留下来。
- **P1**（同一段代码）：最后一次进度落库失败只记一条 log，而完成判定读的是库
  —— 失败瓦片的行就在那次 flush 里，丢了它任务就被判成 completed。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


# ======================================================================
# T1：available 的覆盖率闸门
# ======================================================================

def _levels(z):
    """该层的 (nx, ny)。与 GeographicTilingScheme.tile_count 同口径。"""
    return 2 * 2 ** z, 2 ** z


# 本地地形上传的常见规模：0.05° 见方（约 5.5 km）。
_TINY_AOI = (100.0, 38.95, 100.05, 39.0)
# 一块标准 granule。
_GRANULE = (100.0, 44.0, 101.0, 45.0)


@pytest.mark.parametrize('z', [8, 9, 10])
def test_a_tiny_aoi_is_not_declared_available_on_shallow_levels(z):
    """小 AOI 在浅层只占瓦片的百分之几，整片是钳位外推值，不能声明可用。

    生产路径恒以 min_level=8 起切（底图独占 z0-7，见 dem_task_tiler），所以 z8
    就是这类任务最浅的一层；实测该层真实数据只占 0.51% 面积。
    """
    from src.services.terrain_tiling.cesium_terrain import (
        intersecting_tile_range, well_covered_tile_range,
    )

    nx, ny = _levels(z)
    assert intersecting_tile_range(nx, ny, *_TINY_AOI) is not None, (
        "前提：这些层确实与 DEM 相交 —— 否则本用例证明不了「相交但覆盖率不足」"
    )
    assert well_covered_tile_range(nx, ny, *_TINY_AOI) is None


@pytest.mark.parametrize('z', [11, 12, 13, 14])
def test_the_same_tiny_aoi_is_declared_once_the_tiles_get_small_enough(z):
    """闸门只砍最浅的几层：瓦片每深一级缩小一半，覆盖率必然追上来。

    没有这一条，上一条用例可以被「永远返回 None」满足 —— 那等于把用户的地形
    整个丢掉。
    """
    from src.services.terrain_tiling.cesium_terrain import well_covered_tile_range

    assert well_covered_tile_range(*_levels(z), *_TINY_AOI) is not None


def test_a_full_granule_keeps_the_range_it_always_had():
    """正常规模的 DEM 不受影响 —— 闸门不能把真实数据的层一起砍掉。"""
    from src.services.terrain_tiling.cesium_terrain import (
        intersecting_tile_range, well_covered_tile_range,
    )

    for z in (8, 12, 14):
        nx, ny = _levels(z)
        assert well_covered_tile_range(nx, ny, *_GRANULE) is not None
    # z8：1°×1° 的 granule 恰好铺满 2x2 格，两轴都远超阈值，一格都不该被削掉。
    nx, ny = _levels(8)
    assert (well_covered_tile_range(nx, ny, *_GRANULE)
            == intersecting_tile_range(nx, ny, *_GRANULE))


def test_a_global_dem_is_declared_on_every_level():
    """全球栅格每一格都是满覆盖，闸门必须完全透明。"""
    from src.services.terrain_tiling.cesium_terrain import (
        intersecting_tile_range, well_covered_tile_range,
    )

    for z in (0, 4, 8):
        nx, ny = _levels(z)
        world = (-180.0, -90.0, 180.0, 90.0)
        assert (well_covered_tile_range(nx, ny, *world)
                == intersecting_tile_range(nx, ny, *world))


def test_a_barely_touching_column_is_trimmed_but_the_solid_ones_stay():
    """削的是「沾了一点边」的首尾行列，中间整列不受影响。

    z10 上 granule 的西端列只被覆盖 11%（0.0195° / 0.1758°），东侧与南北则是
    整格。闸门必须精确到这一列，而不是整层放弃或整层放行。
    """
    from src.services.terrain_tiling.cesium_terrain import (
        intersecting_tile_range, well_covered_tile_range,
    )

    nx, ny = _levels(10)
    ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, *_GRANULE)
    cx0, cx1, cy0, cy1 = well_covered_tile_range(nx, ny, *_GRANULE)
    assert (cx0, cx1) == (ix0 + 1, ix1), "只有覆盖不足的西端那一列该被削掉"
    assert (cy0, cy1) == (iy0, iy1), "南北两端是整格覆盖，不该被动"


def test_layer_json_of_a_tiny_aoi_declares_nothing_on_the_shallow_levels(tmp_path):
    """端到端：真跑一次切片，layer.json 不能声明那几层。

    纯函数的用例证明判据对，这一条证明判据**接上了** —— 上一轮 T1 的教训正是
    「算式改对了但调用点还有一处没改」。
    """
    pytest.importorskip('osgeo')
    import json
    from osgeo import gdal, osr

    from src.services.terrain_tiling.cesium_terrain import build_terrain

    west, south, east, north = _TINY_AOI
    src = tmp_path / 'tiny.tif'
    ds = gdal.GetDriverByName('GTiff').Create(str(src), 64, 64, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([west, (east - west) / 64.0, 0, north, 0, -(north - south) / 64.0])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    import numpy as np
    band.WriteArray(np.linspace(500, 3500, 64 * 64).reshape(64, 64).astype('float32'))
    ds = None

    out = tmp_path / 'terrain'
    build_terrain(inputs=[str(src)], output_dir=str(out),
                  min_level=8, max_level=11, tile_size=17, workers=1)

    layer = json.loads((out / 'layer.json').read_text())
    available = layer['available']
    # available[0] 对应 minzoom（=8）。前三层（z8/z9/z10）在这个 AOI 上全是
    # 钳位外推，必须为空；最深那层有真实数据，必须非空。
    assert available[0] == [] and available[1] == [] and available[2] == []
    assert available[3], "最深一层必须仍然声明可用，否则用户的地形整个丢了"


# ======================================================================
# T2 / P1：拼接闸门与进度落库
# ======================================================================

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from src.core import database
    from src.core.config import Config

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


def _params(**overrides):
    from src.core.config import Config

    p = dict(
        name='stitchgate', north=1.0, south=0.0, east=1.0, west=0.0,
        zoom_min=10, zoom_max=10, style='roadmap',
        output_format='image_only', output_path=str(Config.DOWNLOADS_DIR),
    )
    p.update(overrides)
    return p


def _task_row(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    finally:
        conn.close()


def _mark_running(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def _install_fakes(tm, fail_keys=()):
    """替身下载 + 替身拼接。返回记录了每次拼接调用的列表。

    fail_keys 里的 (zoom,x,y) 上报 failed，其余上报 completed 并写出 cache 文件
    （产物复制阶段与恢复枚举都以 cache 为准）。
    """
    stitch_calls = []

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        for tile in list(tiles):
            key = (tile.zoom, tile.x, tile.y)
            if key in fail_keys:
                await progress_callback(tile, 'failed', 'boom')
            else:
                cache_path = tile.cache_path('m')
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(b'tile-bytes')
                await progress_callback(tile, 'completed', None)
        return []

    def fake_stitch(tiles, style, output_path, zoom_level, extra_allowed_dir=None,
                    stop_flag=None):
        stitch_calls.append((zoom_level, len(list(tiles)), output_path))
        from pathlib import Path
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'MOSAIC')
        return output_path

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    tm.download_engine.stitch_tiles_with_gdal = fake_stitch
    return stitch_calls


def test_a_zoom_with_failed_tiles_is_not_stitched(isolated_config):
    """核心：网格不全就不拼。

    旧行为拼出来的是一张地理范围比选区小的 GeoTIFF —— 而引擎侧的
    `_assert_vrt_covers_tile_grid` 结构上抓不到它（期望值由它收到的那批瓦片
    推出，边缘瓦片缺失时期望跟着缩小）。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params())
    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10,
                                                   task_id=task_id))
    doomed = (all_tiles[0].zoom, all_tiles[0].x, all_tiles[0].y)
    stitch_calls = _install_fakes(tm, fail_keys={doomed})
    _mark_running(task_id)

    asyncio.run(tm._execute_task(task_id))

    assert stitch_calls == [], "本层还有失败瓦片时一次拼接都不该发生"
    row = _task_row(task_id)
    assert row['status'] == 'failed'
    assert '未拼接' in (row['error_message'] or ''), (
        "错误信息必须说明这一层没有拼接产物，否则用户以为只是少了几块瓦片"
    )


def test_resume_restitches_the_zoom_that_gained_tiles(isolated_config):
    """T2 的完整链：第一轮缺瓦片 → 不拼；补齐后必须真的拼，且用全量网格。"""
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params())
    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10,
                                                   task_id=task_id))
    doomed = (all_tiles[0].zoom, all_tiles[0].x, all_tiles[0].y)

    _install_fakes(tm, fail_keys={doomed})
    _mark_running(task_id)
    asyncio.run(tm._execute_task(task_id))
    assert _task_row(task_id)['status'] == 'failed'

    # 第二轮：上游恢复正常。
    stitch_calls = _install_fakes(tm, fail_keys=set())
    _mark_running(task_id)
    asyncio.run(tm._execute_task(task_id))

    assert len(stitch_calls) == 1, "补齐之后必须拼一次"
    assert stitch_calls[0][1] == len(all_tiles), (
        "拼接必须拿到完整网格 —— 少一块就是一张范围偏小的图"
    )
    assert _task_row(task_id)['status'] == 'completed'


def test_a_mosaic_left_by_an_incomplete_run_is_rebuilt(isolated_config):
    """旧版本留下的残缺 mosaic 不能被短路当成品。

    判据是「这一层本轮有没有下到新瓦片」：补齐的那一块必然落在这里。
    """
    from pathlib import Path

    from src.core.config import Config
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params())
    stale = Path(Config.DOWNLOADS_DIR) / f'task_{task_id}' / 'stitchgate_zoom_10.tif'
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b'PARTIAL-MOSAIC-FROM-AN-OLDER-RELEASE')

    stitch_calls = _install_fakes(tm)
    _mark_running(task_id)
    asyncio.run(tm._execute_task(task_id))

    assert len(stitch_calls) == 1, "本轮下到了新瓦片，那张旧图必须重拼"
    assert stale.read_bytes() == b'MOSAIC'
    assert _task_row(task_id)['status'] == 'completed'


def test_a_pure_rerun_still_reuses_the_existing_mosaic(isolated_config):
    """短路本身不能被闸门误伤 —— 大 zoom 单层拼接是十分钟级活。

    全部命中 cache、一块新瓦片都没下的重跑，必须仍然跳过拼接。
    """
    from pathlib import Path

    from src.core.config import Config
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params())

    _install_fakes(tm)
    _mark_running(task_id)
    asyncio.run(tm._execute_task(task_id))  # 第一轮：下齐 + 拼好

    mosaic = Path(Config.DOWNLOADS_DIR) / f'task_{task_id}' / 'stitchgate_zoom_10.tif'
    mosaic.write_bytes(b'ALREADY-GOOD')

    stitch_calls = _install_fakes(tm)  # 计数器清零
    _mark_running(task_id)
    asyncio.run(tm._execute_task(task_id))

    assert stitch_calls == [], "全部命中 cache 的重跑不该重算拼接"
    assert mosaic.read_bytes() == b'ALREADY-GOOD'
    assert _task_row(task_id)['status'] == 'completed'


def test_a_failed_progress_flush_fails_the_task(isolated_config):
    """进度落库失败 → 计数与失败行都不可信 → 不许判 completed。

    小任务整轮只有这一次 flush：一次 `database is locked` 就能把「N 块瓦片
    失败」写成「completed，无 error_message」，而 completed 是终态、
    start_task 拒绝重启，用户没有自愈路径。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params())
    all_tiles = list(tm.download_engine.iter_tiles(1.0, 0.0, 1.0, 0.0, 10, 10,
                                                   task_id=task_id))
    doomed = (all_tiles[0].zoom, all_tiles[0].x, all_tiles[0].y)
    _install_fakes(tm, fail_keys={doomed})

    original = TaskManager._write_progress_batch

    def exploding(self, *args, **kwargs):
        raise Exception('database is locked')

    TaskManager._write_progress_batch = exploding
    try:
        _mark_running(task_id)
        asyncio.run(tm._execute_task(task_id))
    finally:
        TaskManager._write_progress_batch = original

    row = _task_row(task_id)
    assert row['status'] == 'failed', (
        "落库失败时库里看不到那条失败瓦片记录，完成判定不能只信库"
    )
    assert '不可信' in (row['error_message'] or '')
