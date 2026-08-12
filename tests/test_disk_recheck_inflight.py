"""运行中磁盘复查（§4.2）—— 四条管线在**跑到一半**时也会重估剩余空间。

改造前只有「按下开始」那一刻的准入判决（`check_budget`），跑起来之后再没有任何
检查：`recheck_remaining` 只有地形物化一个调用点，而那个调用点还站在
`if len(inputs) == 1: return inputs[0]` 之后 —— 单幅切片作业（最常见的形态）
整个过程一次都没查过。三条下载/渲染管线压根没有调用点。

于是失败形态永远是同一个：写到一半 ENOSPC。GTiff / COG 边写边落盘，留下一份
**非空**的半成品，而断点判定是「存在且非空就跳过」，下一轮把截断文件当成写好的
（见 `disk_budget` 模块 docstring 的第一段）。用户看到的是一句 GDAL I/O error，
而他需要看到的是「剩下的活要 25.9 MiB，只剩 1.0 MiB，腾出 24.9 MiB」。

本文件钉住的契约：
1. `RunningRecheck`：首次必查、判死 sticky、节流、**绝不抛**。
2. `remaining_map_estimate`：已下好的瓦片必须从剩余量里扣掉 —— 不扣就会在
   任务 90% 时把它判死。
3. 单幅地形切片作业**也**复查（那条 return 之前）。
4. 地图 / DEM 下载循环判死时按停止标记那条路径收手，落 **paused**（可恢复）
   并把判决原因写进任务自己的日志与 error_message。
5. 等高线渲染循环在批边界上认这个闸门。
6. `ContourParams.disk_recheck` 真的能传到引擎。
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

def test_first_call_checks_and_a_failing_verdict_is_sticky(free_space):
    """首次调用必查（排队期间盘上发生了什么谁也不知道），判死之后闸门永久关闭。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1)
    calls = []
    gate = RunningRecheck('/anywhere', lambda: (calls.append(1), _estimate(100 * MIB))[1],
                          owner=('map', 1, 'download'), config_manager=_FakeConfig(),
                          min_interval=3600)

    first = gate.blocking_verdict()
    assert first is not None and first.ok is False
    assert first.shortfall_bytes > 0
    assert len(calls) == 1

    # sticky：即使节流窗口很长，后续调用照样返回同一个判决（调用方按它写终态）。
    assert gate.blocking_verdict() is first
    assert gate.blocked is first
    assert len(calls) == 1, '判死之后不该再重估剩余工作量'


def test_a_passing_verdict_returns_none_but_is_recorded(free_space):
    """通过时返回 None（循环继续），但判决要留下来 —— 估算错时第一件事是看数字。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1024)
    seen = []
    gate = RunningRecheck('/anywhere', _estimate(10 * MIB), config_manager=_FakeConfig(),
                          on_verdict=seen.append)

    assert gate.blocking_verdict() is None
    assert gate.blocked is None
    assert gate.verdict is not None and gate.verdict.ok is True
    assert seen and seen[0] is gate.verdict


def test_the_interval_throttles_real_checks(free_space):
    """批边界可以是亚秒级的：每批一次 statvfs + 调度器快照是纯浪费。"""
    from src.services.disk_budget import RunningRecheck

    free_space(1024)
    calls = []
    gate = RunningRecheck('/anywhere', lambda: (calls.append(1), _estimate(1))[1],
                          config_manager=_FakeConfig(), min_interval=3600)

    for _ in range(50):
        gate.blocking_verdict()
    assert len(calls) == 1, '节流窗口内只该真的查一次'

    # force 是给「这一层马上要写几十 GB」那种明确的大动作用的。
    gate.blocking_verdict(force=True)
    assert len(calls) == 2


def test_it_never_raises_into_the_download_loop(free_space):
    """调用点全在下载/渲染热循环里：一个次要检查的环境问题不该打死任务。"""
    from src.services.disk_budget import RunningRecheck

    def boom():
        raise OSError('the volume went away')

    gate = RunningRecheck('/anywhere', boom, config_manager=_FakeConfig())
    assert gate.blocking_verdict() is None
    assert gate.blocked is None

    # on_verdict 抛出同样不能传染 —— 判决是主路径，记录是次要 sink。
    free_space(1024)
    noisy = RunningRecheck('/anywhere', _estimate(1), config_manager=_FakeConfig(),
                           on_verdict=lambda v: (_ for _ in ()).throw(RuntimeError('log died')))
    assert noisy.blocking_verdict() is None
    assert noisy.verdict is not None


def test_a_none_estimate_skips_the_check(free_space):
    """算不出剩余量（分母还没上报）时跳过，而不是拿 0 去判死。"""
    from src.services.disk_budget import RunningRecheck

    free_space(0)
    gate = RunningRecheck('/anywhere', lambda: None, config_manager=_FakeConfig())
    assert gate.blocking_verdict() is None
    assert gate.blocked is None


# ---------------------------------------------------------------------------
# 2. remaining_map_estimate
# ---------------------------------------------------------------------------

def test_downloaded_tiles_are_deducted_from_the_remaining_estimate():
    """**回归**：不扣已下好的瓦片 = 任务跑到 90% 反而被判死。

    `estimate_map_task(cached_tiles=...)` 只折 network/cache，产物按整份算（那是
    准入的正确口径）。运行中复查拿它当剩余量，就会在几乎跑完时仍然要求整份松散
    镜像的空间。
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


def test_a_single_input_tiling_job_rechecks_the_disk(tmp_path, free_space):
    """**回归**：复查曾站在 `if len(inputs) == 1: return` 之后 —— 单幅作业
    （一个 1°×1° 颗粒、一次上传，最常见的形态）整个切片过程一次都没查过。"""
    from src.services.terrain_tiling import cesium_terrain

    only = _fake_source(tmp_path / 'a.tif')
    free_space(1)

    with pytest.raises(RuntimeError) as excinfo:
        cesium_terrain.build_input_raster([only], work_dir=str(tmp_path), owner=None)
    reason = str(excinfo.value)
    assert 'not enough disk space' in reason
    assert 'the remaining work' in reason, '措辞必须是「剩下的活」而不是「这个任务」'


def test_a_single_input_job_is_not_charged_for_materialisation(tmp_path, free_space):
    """单幅直通一个字节都不物化：把中间栅格算进去就是拿不存在的开销判死任务。"""
    from src.services.terrain_tiling import cesium_terrain

    a = _fake_source(tmp_path / 'a.tif')
    b = _fake_source(tmp_path / 'b.tif')

    single = cesium_terrain._materialise_budget_verdict(
        [a], str(tmp_path), owner=None, materialise=False)
    multi = cesium_terrain._materialise_budget_verdict(
        [a, b], str(tmp_path), owner=None, materialise=True)

    assert single.required_bytes * 2 < multi.required_bytes


def test_a_single_input_job_still_runs_when_the_disk_is_fine(tmp_path, free_space):
    """闸门不是「总是拦」：空间够时单幅照旧直通返回源路径本身。"""
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


def test_a_map_download_stops_with_a_terminal_reason_when_the_disk_fills(
        isolated_config, monkeypatch, free_space):
    """盘在下载途中不够了：按停止标记那条路径收手，落 paused + 原因。

    落 paused 而不是 failed 是硬要求 —— failed 不在 RESUMABLE_STATE_VALUES 里，
    判成 failed 等于「腾出空间也点不动恢复」。
    """
    from src.services.task_logging import read_task_log
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=None)
    task_id = _seed_map_task()

    downloaded = []

    async def _never_gets_here(**kwargs):
        downloaded.append(kwargs['tile'])
        return {'tile': kwargs['tile'], 'status': 'success'}

    monkeypatch.setattr(tm.download_engine, '_download_single_tile', _never_gets_here)
    free_space(1)

    asyncio.run(tm._execute_task(task_id))

    row = _row('tasks', task_id)
    assert row['status'] == 'paused', '必须是可恢复的状态'
    assert 'not enough disk space' in (row['error_message'] or '')
    assert downloaded == [], '判死之后一块瓦片都不该再下'

    messages = '\n'.join(e['message'] for e in read_task_log('map', task_id))
    assert 'EVENT terminal status=paused' in messages
    assert 'reason=disk_budget' in messages
    assert 'not enough disk space' in messages


def test_a_map_download_runs_normally_when_the_disk_is_fine(
        isolated_config, monkeypatch, free_space):
    """闸门不是「总是拦」—— 空间够时任务照常跑完，且判决要记进任务日志。"""
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

def test_the_dem_engine_stops_before_touching_the_network(isolated_config, tmp_path,
                                                          free_space):
    """DEM 的批就是颗粒（一颗 30-50 MB）。判死时不抛、不发请求，颗粒回写 pending。"""
    from src.services.dem_download_engine import DemDownloadEngine
    from src.services.disk_budget import RunningRecheck

    free_space(1)
    gate = RunningRecheck(tmp_path / 'out', _estimate(100 * MIB),
                          owner=('dem', 7, 'download'), config_manager=_FakeConfig())

    reports = []

    async def progress(granule, status, error, size):
        reports.append((granule, status))

    asyncio.run(DemDownloadEngine().download_files(
        dataset='COP-DEM-GLO-30', granules=['tile/G.tif'],
        output_dir=tmp_path / 'out', progress_callback=progress,
        disk_recheck=gate))

    assert reports == [('tile/G.tif', 'pending')], '一次都没尝试过的颗粒回 pending'
    assert gate.blocked is not None


def test_the_dem_manager_pauses_the_task_with_the_verdict(
        isolated_config, monkeypatch, free_space):
    """引擎收手之后，管理器必须补上状态与原因 —— 只收手不解释就是一个凭空
    自己暂停了的任务。"""
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
        # 复刻真引擎：颗粒边界上问一次闸门，判死就按暂停那条路径收手。
        assert disk_recheck is not None, '管理器必须把闸门交给引擎'
        if disk_recheck.blocking_verdict() is not None:
            await progress_callback(granules[0], 'pending', None, None)
            return
        await progress_callback(granules[0], 'completed', None, 10)

    mgr.engine.download_files = fake_download_files
    free_space(1)

    asyncio.run(mgr._execute(task_id))

    row = _row('dem_tasks', task_id)
    assert row['status'] == 'paused'
    assert 'not enough disk space' in (row['error_message'] or '')
    messages = '\n'.join(e['message'] for e in read_task_log('dem', task_id))
    assert 'EVENT terminal status=paused' in messages
    assert 'reason=disk_budget' in messages


# ---------------------------------------------------------------------------
# 6. 等高线渲染循环
# ---------------------------------------------------------------------------

def test_contour_params_carry_the_gate_to_the_engine(tmp_path):
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
    assert seen['extra'] == {}, '没有闸门时不许多传关键字'

    tile_contour_task_dir(task_dir, tmp_path / 'out',
                         ContourParams(disk_recheck=sentinel, **base),
                         build_contour_fn=fake_build)
    assert seen['extra'] == {'disk_recheck': sentinel}


def test_the_contour_render_loop_honours_the_gate(tmp_path, free_space):
    """渲染循环在与 stop_flag **同一批检查点**上认这个闸门（一张都不渲）。"""
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
    gate = RunningRecheck(tmp_path / 'tiles', _estimate(100 * MIB),
                          owner=('contour', 3, 'render'), config_manager=_FakeConfig())

    counts = build_contour_tiles(
        dem_tifs=[dem], out_dir=tmp_path / 'tiles', interval=50,
        zoom_min=10, zoom_max=11, style=ContourStyle(), workers=1,
        disk_recheck=gate)

    assert counts['total'] >= 1, '分母照常算出来（判死不等于没活）'
    assert counts['rendered'] == 0
    assert gate.blocked is not None
    assert not list((tmp_path / 'tiles').rglob('*.png'))
