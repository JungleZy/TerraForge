"""运行中磁盘复查（§4.2）—— 四条管线在**跑到一半**时也会重估剩余空间。

2026-08 起复查是**纯观测**：估算超过可用空间既不拒绝启动也不中途叫停
（见 `disk_budget` 模块 docstring 头部）。复查存在的理由：失败形态永远是
同一个 —— 写到一半 ENOSPC，GTiff / COG 边写边落盘留下一份**非空**半成品，
断点判定是「存在且非空就跳过」，下一轮把截断文件当成写好的。用户看到的是
一句 GDAL I/O error，而任务日志里那条 disk_recheck 事件能告诉他「剩下的活
要 25.9 MiB，只剩 1.0 MiB」。

本文件钉住的契约：
1. `RunningRecheck`：首次必查、节流、**绝不抛**、判决无论通过与否都留下，
   且永不叫停循环。
2. `remaining_map_estimate`：已下好的瓦片必须从剩余量里扣掉 —— 不扣就会在
   任务 90% 时还按整份报数。
3. 单幅地形切片作业**也**复查（那条 return 之前），且复查不通过照样直通。
4. 地图 / DEM 下载与等高线渲染在磁盘不足时**照常跑完**，判决数字进任务日志。
5. `ContourParams.disk_recheck` 真的能传到引擎。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

MIB = 1024 * 1024


class _FakeConfig:
    """只认预算三键的配置替身（关掉安全系数与保留量，让算式一眼可对）。"""

    def __init__(self, **values):
        self._values = {
            'disk_budget_enabled': 'true',
            'disk_reserve_mb': '0',
            'disk_safety_factor': '1.0',
        }
        self._values.update(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def get_many(self, keys):
        return {k: self._values.get(k) for k in keys}


def _estimate(peak):
    from src.services.disk_budget import DiskEstimate

    return DiskEstimate(network_bytes=0, cache_bytes=0, temp_bytes=0,
                        output_bytes=peak, peak_bytes=peak)


@pytest.fixture()
def free_space(monkeypatch):
    """把 `free_bytes` 换成一个可调的旋钮，返回设置函数。"""
    from src.services import disk_budget

    state = {'free': 1024 * MIB}
    monkeypatch.setattr(disk_budget, 'free_bytes', lambda path: state['free'])

    def _set(mib):
        state['free'] = int(mib * MIB)

    return _set


# ---------------------------------------------------------------------------
# 1. RunningRecheck 自身
# ---------------------------------------------------------------------------

def test_first_call_checks_and_records_the_verdict(free_space):
    """首次调用必查（排队期间盘上发生了什么谁也不知道），判负的判决也要留下来
    —— 但只是数字，不再是命令。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1)
    calls = []
    recheck = RunningRecheck('/anywhere',
                             lambda: (calls.append(1), _estimate(100 * MIB))[1],
                             owner=('map', 1, 'download'), config_manager=_FakeConfig(),
                             min_interval=3600)

    verdict = recheck.poll()
    assert verdict is not None and verdict.ok is False
    assert verdict.shortfall_bytes > 0
    assert recheck.verdict is verdict
    assert len(calls) == 1


def test_a_passing_verdict_is_returned_and_recorded(free_space):
    """通过的判决同样返回并留下 —— 估算错的时候第一件事就是回头看这行的数字。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1024)
    seen = []
    recheck = RunningRecheck('/anywhere', _estimate(10 * MIB),
                             config_manager=_FakeConfig(), on_verdict=seen.append)

    verdict = recheck.poll()
    assert verdict is not None and verdict.ok is True
    assert recheck.verdict is verdict
    assert seen and seen[0] is verdict


def test_the_interval_throttles_real_checks(free_space):
    """批边界可以是亚秒级的：每批一次 statvfs + 调度器快照是纯浪费。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1024)
    calls = []
    recheck = RunningRecheck('/anywhere', lambda: (calls.append(1), _estimate(1))[1],
                             config_manager=_FakeConfig(), min_interval=3600)

    for _ in range(50):
        assert recheck.poll() is None or len(calls) == 1
    assert len(calls) == 1, '节流窗口内只该真的查一次'

    assert recheck.poll(force=True) is not None
    assert len(calls) == 2


def test_it_never_raises_into_the_download_loop(free_space):
    """调用点全在下载/渲染热循环里：一个次要检查的环境问题不该打死任务。"""
    from src.services.disk_budget import RunningRecheck

    def boom():
        raise OSError('the volume went away')

    recheck = RunningRecheck('/anywhere', boom, config_manager=_FakeConfig())
    assert recheck.poll() is None
    assert recheck.verdict is None

    # on_verdict 抛出同样不能传染 —— 判决是主路径，记录是次要 sink。
    free_space(1024)
    noisy = RunningRecheck('/anywhere', _estimate(1), config_manager=_FakeConfig(),
                           on_verdict=lambda v: (_ for _ in ()).throw(RuntimeError('log died')))
    assert noisy.poll() is not None
    assert noisy.verdict is not None


def test_a_none_estimate_skips_the_check(free_space):
    """算不出剩余量（分母还没上报）时跳过，而不是拿 0 去虚报。"""
    from src.services.disk_budget import RunningRecheck

    free_space(0)
    recheck = RunningRecheck('/anywhere', lambda: None, config_manager=_FakeConfig())
    assert recheck.poll() is None
    assert recheck.verdict is None


# ---------------------------------------------------------------------------
# 2. remaining_map_estimate
# ---------------------------------------------------------------------------

def test_downloaded_tiles_are_deducted_from_the_remaining_estimate():
    """**回归**：不扣已下好的瓦片 = 任务跑到 90% 还在按整份报数。

    `estimate_map_task(cached_tiles=...)` 只折 network/cache，产物按整份算（那是
    启动时估算的正确口径）。运行中复查拿它当剩余量，就会在几乎跑完时仍然按整份
    松散镜像的空间报数。
    """
    from src.contracts.region import RegionSpec
    from src.services import disk_budget

    region = RegionSpec.from_bbox(north=1.0, south=0.0, east=1.0, west=0.0)
    full = disk_budget.estimate_map_task(region, 10, 10, 'tiles_only', 's')
    assert full.tile_count > 0

    half = disk_budget.remaining_map_estimate(full, full.tile_count // 2)
    done = disk_budget.remaining_map_estimate(full, full.tile_count)

    assert half.peak_bytes < full.peak_bytes
    assert done.peak_bytes < half.peak_bytes
    assert done.network_bytes == 0
    assert done.tile_count == 0
    assert done.detail['remaining_ratio'] == 0.0
    # 全部下完之后 tiles_only 任务不再需要任何空间（没有拼接产物）。
    assert done.peak_bytes == 0


def test_the_stitched_mosaic_is_not_discounted_by_downloaded_tiles():
    """逐层拼接产物跑在整轮下载**之后** —— 一个字节都还没写，不能按进度折。"""
    from src.contracts.region import RegionSpec
    from src.services import disk_budget

    region = RegionSpec.from_bbox(north=1.0, south=0.0, east=1.0, west=0.0)
    full = disk_budget.estimate_map_task(region, 10, 10, 'png', 's')
    stitched = int(full.detail['stitched_bytes'])
    assert stitched > 0

    done = disk_budget.remaining_map_estimate(full, full.tile_count)
    assert done.output_bytes == stitched
    assert done.temp_bytes == full.temp_bytes


# ---------------------------------------------------------------------------
# 3. 地形切片：单幅输入也复查
# ---------------------------------------------------------------------------

def _fake_source(path, mib=8):
    path.write_bytes(b'\0' * (mib * MIB))
    return str(path)


def test_a_single_input_tiling_job_rechecks_but_never_blocks(tmp_path, free_space,
                                                             caplog):
    """**回归**：复查曾站在 `if len(inputs) == 1: return` 之后 —— 单幅作业
    （一个 1°×1° 颗粒、一次上传，最常见的形态）整个切片过程一次都没查过。

    拦截语义移除后它依然是纯观测：判负不抛、照常直通，数字进日志。"""
    import logging

    from src.services.terrain_tiling import cesium_terrain

    only = _fake_source(tmp_path / 'a.tif')
    free_space(1)

    with caplog.at_level(logging.WARNING):
        assert cesium_terrain.build_input_raster(
            [only], work_dir=str(tmp_path), owner=None) == only, '判负也必须照常直通'
    assert 'not enough disk space' in caplog.text
    assert 'the remaining work' in caplog.text, '措辞必须是「剩下的活」而不是「这个任务」'


def test_a_single_input_job_is_not_charged_for_materialisation(tmp_path, free_space):
    """单幅直通一个字节都不物化：把中间栅格算进去就是拿不存在的开销虚报。"""
    from src.services.terrain_tiling import cesium_terrain

    a = _fake_source(tmp_path / 'a.tif')
    b = _fake_source(tmp_path / 'b.tif')

    single = cesium_terrain._materialise_budget_verdict(
        [a], str(tmp_path), owner=None, materialise=False)
    multi = cesium_terrain._materialise_budget_verdict(
        [a, b], str(tmp_path), owner=None, materialise=True)

    assert single.required_bytes * 2 < multi.required_bytes


def test_a_single_input_job_still_runs_when_the_disk_is_fine(tmp_path, free_space):
    """空间够时单幅照旧直通返回源路径本身。"""
    from src.services.terrain_tiling import cesium_terrain

    only = _fake_source(tmp_path / 'a.tif')
    free_space(4096)
    assert cesium_terrain.build_input_raster(
        [only], work_dir=str(tmp_path), owner=None) == only


# ---------------------------------------------------------------------------
# 4. 地图下载循环
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    from src.core import database
    from src.core.config import Config
    from src.services.resource_scheduler import reset_scheduler

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    reset_scheduler()
    yield tmp_path
    reset_scheduler()


def _seed_map_task(status='running'):
    from src.core.config import Config
    from src.core.database import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('disk', ?, 1, 0, 1, 0, 10, 10, 'satellite', 'tiles_only',
                    ?, 0, 0, 0)
            """,
            (status, str(Config.DOWNLOADS_DIR)),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO task_time_records (task_id, action, timestamp) "
            "VALUES (?, 'start', ?)",
            (task_id, '2026-08-12T00:00:00'),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _row(table, task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return dict(conn.cursor().execute(
            f'SELECT * FROM {table} WHERE id = ?', (task_id,)).fetchone())
    finally:
        conn.close()


def test_a_map_download_keeps_running_when_the_disk_fills(
        isolated_config, monkeypatch, free_space):
    """盘不够也不再叫停：任务照常跑完，「还差多少」留在任务日志里。"""
    from src.services.task_logging import read_task_log
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=None)
    task_id = _seed_map_task()

    downloaded = []

    async def _ok(**kwargs):
        tile = kwargs['tile']
        downloaded.append(tile)
        path = tile.cache_path(kwargs.get('source') or 's')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x89PNG\r\n\x1a\n')
        cb = kwargs.get('progress_callback')
        if cb is not None:
            await cb(tile, 'success', None, len(b'\x89PNG\r\n\x1a\n'))
        return {'tile': tile, 'status': 'success'}

    monkeypatch.setattr(tm.download_engine, '_download_single_tile', _ok)
    free_space(1)

    asyncio.run(tm._execute_task(task_id))

    row = _row('tasks', task_id)
    assert row['status'] != 'paused', '磁盘不足不再暂停任务'
    assert downloaded, '判负之后瓦片照样要下'

    messages = '\n'.join(e['message'] for e in read_task_log('map', task_id))
    assert 'EVENT disk_recheck ok=False' in messages
    assert 'not enough disk space' in messages


def test_a_map_download_runs_normally_when_the_disk_is_fine(
        isolated_config, monkeypatch, free_space):
    """空间够时任务照常跑完，且判决要记进任务日志。"""
    from src.services.task_logging import read_task_log
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=None)
    task_id = _seed_map_task()

    async def _ok(**kwargs):
        tile = kwargs['tile']
        path = tile.cache_path(kwargs.get('source') or 's')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x89PNG\r\n\x1a\n')
        cb = kwargs.get('progress_callback')
        if cb is not None:
            await cb(tile, 'success', None, len(b'\x89PNG\r\n\x1a\n'))
        return {'tile': tile, 'status': 'success'}

    monkeypatch.setattr(tm.download_engine, '_download_single_tile', _ok)
    free_space(4096)

    asyncio.run(tm._execute_task(task_id))

    assert _row('tasks', task_id)['status'] != 'paused'
    messages = '\n'.join(e['message'] for e in read_task_log('map', task_id))
    assert 'EVENT disk_recheck ok=True' in messages


# ---------------------------------------------------------------------------
# 5. DEM 下载循环
# ---------------------------------------------------------------------------

def test_the_dem_engine_polls_the_recheck_but_never_stops(isolated_config, tmp_path,
                                                          free_space):
    """DEM 的批就是颗粒（一颗 30-50 MB）。颗粒边界仍复查（数字进日志），但判负
    不再叫停：颗粒照常走既有路径。这里用「已下好」快速路径避开网络。"""
    from src.services.dem_download_engine import DemDownloadEngine
    from src.services.disk_budget import RunningRecheck

    free_space(1)
    recheck = RunningRecheck(tmp_path / 'out', _estimate(100 * MIB),
                             owner=('dem', 7, 'download'), config_manager=_FakeConfig())

    out = tmp_path / 'out'
    out.mkdir()
    (out / 'G.tif').write_bytes(b'\x00' * 10)

    reports = []

    async def progress(granule, status, error, size):
        reports.append((granule, status))

    asyncio.run(DemDownloadEngine().download_files(
        dataset='COP-DEM-GLO-30', granules=['tile/G.tif'],
        output_dir=out, progress_callback=progress,
        disk_recheck=recheck))

    assert reports == [('tile/G.tif', 'completed')], '判负后颗粒照常走既有路径'
    assert recheck.verdict is not None and recheck.verdict.ok is False


def test_the_dem_manager_runs_to_completion_with_the_verdict_logged(
        isolated_config, monkeypatch, free_space):
    """判负不再暂停：任务照跑到底，判决数字留在任务日志里。"""
    from src.core.config import Config
    from src.core.database import get_connection
    from src.services.dem_task_manager import DemTaskManager
    from src.services.task_logging import read_task_log

    # 管理器先构造:它的 __init__ 会做启动孤儿恢复,把任何 running 行降级成
    # paused。先插行再构造的话,这条用例测的就是恢复而不是复查了。
    mgr = DemTaskManager(socketio=None)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-disk', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 0, 0)
            """,
            (str(Config.DOWNLOADS_DIR),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO dem_files (task_id, granule_id, status) VALUES (?, 'G.tif', 'pending')",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_download_files(dataset, granules, output_dir, progress_callback=None,
                                  bytes_callback=None, stop_flag=None,
                                  max_concurrent=None, disk_recheck=None):
        # 复刻真引擎的观测点：颗粒边界上 poll 一次，然后照跑。
        assert disk_recheck is not None, '管理器必须把复查器交给引擎'
        disk_recheck.poll(force=True)
        await progress_callback(granules[0], 'completed', None, 10)

    mgr.engine.download_files = fake_download_files
    free_space(1)

    asyncio.run(mgr._execute(task_id))

    row = _row('dem_tasks', task_id)
    assert row['status'] == 'completed', '磁盘不足不再暂停任务'
    messages = '\n'.join(e['message'] for e in read_task_log('dem', task_id))
    assert 'EVENT disk_recheck ok=False' in messages
    assert 'not enough disk space' in messages


# ---------------------------------------------------------------------------
# 6. 等高线渲染循环
# ---------------------------------------------------------------------------

def test_contour_params_carry_the_recheck_to_the_engine(tmp_path):
    """ContourParams.disk_recheck 必须真的到达引擎；没有它时**不许**多传这个
    关键字（tests 里的 fake_build 替身是按老签名写死的）。"""
    from src.services.contour_engine import ContourStyle
    from src.services.contour_task_tiler import ContourParams, tile_contour_task_dir

    task_dir = tmp_path / 'src'
    task_dir.mkdir()
    (task_dir / 'ASTGTMV003_N39E116_dem.tif').write_bytes(b'x')
    sentinel = object()
    seen = {}

    def fake_build(dem_tifs, out_dir, interval, zoom_min, zoom_max, style,
                   progress_cb=None, stage_cb=None, stop_flag=None, shade=False,
                   water=False, att_tifs=None, workers=0, **extra):
        seen['extra'] = extra
        return {'total': 0, 'rendered': 0, 'failed': 0, 'skipped': 0}

    base = dict(interval=50, zoom_min=12, zoom_max=12, style=ContourStyle())
    tile_contour_task_dir(task_dir, tmp_path / 'out', ContourParams(**base),
                         build_contour_fn=fake_build)
    assert seen['extra'] == {}, '没有复查器时不许多传关键字'

    tile_contour_task_dir(task_dir, tmp_path / 'out',
                         ContourParams(disk_recheck=sentinel, **base),
                         build_contour_fn=fake_build)
    assert seen['extra'] == {'disk_recheck': sentinel}


def test_the_contour_render_loop_polls_but_never_stops(tmp_path, free_space):
    """渲染循环在与 stop_flag **同一批检查点**上复查（数字留下来），但判负不再
    叫停：瓦片照常渲完。"""
    gdal = pytest.importorskip('osgeo.gdal')
    np = pytest.importorskip('numpy')
    pytest.importorskip('matplotlib')

    from src.services.contour_engine import ContourStyle, build_contour_tiles
    from src.services.disk_budget import RunningRecheck

    dem = tmp_path / 'ASTGTMV003_N39E116_dem.tif'
    drv = gdal.GetDriverByName('GTiff')
    ds = drv.Create(str(dem), 60, 60, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((116.0, 1.0 / 60, 0, 40.0, 0, -1.0 / 60))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(
        np.tile(np.linspace(0, 6000, 60).astype('float32'), (60, 1)))
    ds.FlushCache()
    ds = None

    free_space(1)
    recheck = RunningRecheck(tmp_path / 'tiles', _estimate(100 * MIB),
                             owner=('contour', 3, 'render'), config_manager=_FakeConfig())

    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=tmp_path / 'tiles', interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(), workers=1,
        disk_recheck=recheck)

    assert counts['total'] >= 1
    assert counts['rendered'] == counts['total'], '判负之后瓦片照样要渲完'
    assert recheck.verdict is not None and recheck.verdict.ok is False
