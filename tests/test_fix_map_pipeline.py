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


# ---------- output_path 规范化入库 + 存量相对路径兼容 ----------

def test_create_task_stores_resolved_absolute_output_path(isolated_config):
    """create_task 入库的必须是 resolve_output_dir() 解析后的绝对路径
    (与 dem_task_manager 同口径),而不是用户原始相对值 —— 存原始值时
    _execute_task 的 Path(task.output_path) 会按进程 CWD 解析。"""
    from core.config import Config
    from core.database import get_connection
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(output_path='sub dir'))

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT output_path FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()

    expected = str((Path(Config.DOWNLOADS_DIR) / 'sub dir').resolve())
    assert row['output_path'] == expected, (
        f"入库的必须是解析后的绝对路径,实际: {row['output_path']}"
    )


def test_execute_task_legacy_relative_output_path_ignores_cwd(isolated_config, monkeypatch):
    """存量行的相对 output_path 必须相对 Config.DOWNLOADS_DIR 解析,不按进程 CWD。

    旧版本只校验不改写,库里存的是用户原始相对值;CWD≠BASE_DIR(打包 exe 从
    快捷方式启动)时,旧代码会把产物写到 CWD 下、拼接白名单检查让 image 任务
    必败。修复后执行路径先做兼容归一化。
    """
    from core.config import Config
    from services.download_engine import DownloadEngine
    from services.task_manager import TaskManager

    # 全部瓦片预先进 cache,执行只走「复制到 output_path」阶段
    engine = DownloadEngine()
    tiles = list(engine.iter_tiles(1, 0, 1, 0, 10, 10))
    for tile in tiles:
        cache_path = tile.cache_path('s')
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'cached-tile')

    tm = TaskManager()  # 先建 manager,再插 running 行,免得被 orphan 回收降级
    # 存量行:output_path 是旧版本入库的相对原始值
    task_id = _seed_task_row(status='running', output_format='tiles_only',
                             output_path='legacy_out', total=len(tiles))

    # CWD 换到别处:相对路径若按 CWD 解析,产物会写到 stray/legacy_out 下
    stray = isolated_config / 'elsewhere'
    stray.mkdir()
    monkeypatch.chdir(stray)

    asyncio.run(tm._execute_task(task_id))

    expected_dir = Path(Config.DOWNLOADS_DIR) / 'legacy_out' / f'task_{task_id}'
    copied = list(expected_dir.rglob('*.png'))
    assert len(copied) == len(tiles), (
        f"产物必须落在 DOWNLOADS_DIR 下的任务目录,实际复制了 {len(copied)}/{len(tiles)} 块"
    )
    assert not (stray / 'legacy_out').exists(), "相对 output_path 不得按进程 CWD 解析"


# ---------- M2: 读取路径容忍历史遗留非法行(Task.from_row) ----------

def test_read_paths_tolerate_legacy_invalid_rows(isolated_config):
    """从 DB 行重建 Task 不能走 __post_init__ 严格校验。

    历史遗留行可能带着旧版本校验缺口写入的非法值(非法 style、north<=south、
    zoom_min>zoom_max)—— 严格构造会让一条坏行把 get_active_tasks /
    get_task_status 整个打成 500。写入路径(create_task)的严格校验不变。
    """
    from core.database import get_connection
    from services.task_manager import TaskManager

    # 直接 INSERT 非法行,模拟旧版本校验缺口写入的遗留数据
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('legacy', 'paused', 0, 1, 1, 0, 15, 10,
                    'not-a-style', 'weird-format', '/tmp', 5, 2, 1)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    tm = TaskManager()

    active = tm.get_active_tasks()  # 旧代码在这里整页 500(ValueError)
    assert [t['id'] for t in active] == [task_id]
    assert active[0]['style'] == 'not-a-style', "读取侧原样还原,不改写不校验"
    assert active[0]['zoom_min'] == 15 and active[0]['zoom_max'] == 10

    status = tm.get_task_status(task_id)
    assert status['status'] == 'paused'
    assert status['output_format'] == 'weird-format'

    # 写入路径仍走严格校验:同样的非法值必须被拒
    with pytest.raises(ValueError):
        tm.create_task(_params(style='not-a-style'))
    with pytest.raises(ValueError):
        tm.create_task(_params(zoom_min=15, zoom_max=10))


# ---------- M6: start_task commit 后、thread.start() 前异常不留假 running ----------

def test_start_task_emit_failure_leaves_no_phantom_running(isolated_config):
    """状态翻转 commit 之后 emit 抛异常:任务不能停在 'running' 空转。

    旧实现 except 里的 conn.rollback() 对已 commit 的事务无效,留下
    status='running' 但线程从未启动的假运行任务(UI 永远显示在跑,
    且 pause/resume 语义全错)。修复后显式回补为 failed 并清掉线程登记。
    """
    from core.database import get_connection
    from services.task_manager import TaskManager

    class ExplodingSocketIO:
        def emit(self, event, payload):
            raise RuntimeError('socketio boom')

    tm = TaskManager(socketio=ExplodingSocketIO())
    task_id = _seed_task_row(status='pending')

    with pytest.raises(RuntimeError, match='socketio boom'):
        tm.start_task(task_id)

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT status, error_message FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row['status'] != 'running', "线程从未启动,状态不得停在 running"
    assert row['status'] == 'failed'
    assert 'socketio boom' in row['error_message']
    assert task_id not in tm.active_tasks, "未启动的线程不得留在 active_tasks"
    assert task_id not in tm.stop_flags


def test_start_task_success_still_emits_and_runs(isolated_config):
    """回归护栏:正常 start 不受回补逻辑影响 —— 状态 running、线程在跑。"""
    import threading

    from core.database import get_connection
    from services.task_manager import TaskManager

    class FakeSocketIO:
        def __init__(self):
            self.events = []

        def emit(self, event, payload):
            self.events.append((event, payload))

    tm = TaskManager(socketio=FakeSocketIO())
    task_id = _seed_task_row(status='pending')

    # 把下载挡在门内,保证断言时线程仍在执行(状态必须是 running)
    gate = threading.Event()

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        await asyncio.to_thread(gate.wait, 30)
        return [{'tile': t, 'status': 'completed'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    tm.start_task(task_id)

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT status FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row['status'] == 'running'
    assert any(name == 'task_progress' for name, _ in tm.socketio.events)

    thread = tm.active_tasks.get(task_id)
    assert thread is not None
    gate.set()
    thread.join(timeout=30)
    assert not thread.is_alive()


# ---------- M7: pause 状态翻转与时长累计同事务 ----------

def test_pause_task_accumulates_running_time_without_post_commit_helpers(
    isolated_config, monkeypatch
):
    """pause 必须在同一事务内完成状态翻转 + 时长累计 + pause 记录。

    旧实现先 commit 'paused',再另开连接 _update_total_running_time ——
    窗口内并发 resume 写入的新 'resume' 记录会让 elapsed≈0,整段运行
    时长被吞。修复后这两个 commit 后助手不再参与 pause:把它们替换成
    必炸的桩,pause 仍必须完整落库(状态、累计时长、pause 记录)。
    """
    from datetime import datetime, timedelta, timezone

    from core.database import get_connection
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = _seed_task_row(status='running')

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO task_time_records (task_id, action, timestamp)"
            " VALUES (?, 'start', ?)",
            (task_id, (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    def boom(*args, **kwargs):
        raise AssertionError("pause 不得再经过 commit 后的时长/记录助手")

    monkeypatch.setattr(tm, '_update_total_running_time', boom)
    monkeypatch.setattr(tm, '_record_time_action', boom)

    tm.pause_task(task_id)

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT status, total_running_seconds FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
        actions = [
            r['action'] for r in conn.cursor().execute(
                'SELECT action FROM task_time_records WHERE task_id = ?', (task_id,)
            ).fetchall()
        ]
    finally:
        conn.close()

    assert row['status'] == 'paused'
    assert 8 <= row['total_running_seconds'] <= 60, (
        f"start 至今的整段时长必须累计上,实际 {row['total_running_seconds']}s"
    )
    assert 'pause' in actions, "pause 时间记录必须与状态翻转同库同事务落库"


# ---------- M8/#9: 大任务物化与事件循环阻塞 ----------

def test_download_tiles_batch_creates_coroutines_in_batches(isolated_config, monkeypatch):
    """分批创建协程:任一时刻存活的下载协程数不超过批大小。

    旧实现对全部瓦片一次性预建协程再 gather,百万级瓦片就是百万个待调度
    协程同时挂在事件循环上。结果顺序与「每块瓦片一条结果」的语义不变。
    """
    import services.download_engine as de_mod
    from models.task import Tile
    from services.config_manager import ConfigManager

    monkeypatch.setattr(de_mod, 'DOWNLOAD_BATCH_SIZE', 3)
    ConfigManager().set('concurrent_downloads', '10')  # 信号量大于批大小,才能看出分批效果

    engine = de_mod.DownloadEngine()

    active = 0
    peak = 0

    async def fake_single(tile, style, session, cache_enabled,
                          progress_callback=None, proxy_url='', stop_flag=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {'tile': tile, 'status': 'completed'}

    engine._download_single_tile = fake_single

    tiles = [Tile(task_id=1, zoom=0, x=i, y=0) for i in range(10)]
    results = asyncio.run(engine.download_tiles_batch(tiles, 's'))

    assert [r['tile'].x for r in results] == list(range(10)), "结果顺序必须与输入一致"
    assert all(r['status'] == 'completed' for r in results)
    assert 0 < peak <= 3, (
        f"任一时刻存活协程不得超过一个批次(3),实测峰值 {peak} —— 协程被一次性全建了"
    )


def test_execute_task_enumerates_tiles_only_once(isolated_config):
    """completed 清单与待下载清单由同一遍枚举产出,不再有两遍全量 stat。

    旧实现下载前枚举一遍(物化待下载列表),下载后再枚举+stat 一遍重建
    completed 列表。修复后:枚举一遍,cache 命中直接进 completed 清单,
    下载成功的瓦片按结果并入 —— iter_tiles 只应被调用一次。
    """
    from core.config import Config
    from core.database import get_connection
    from services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(zoom_min=10, zoom_max=10))

    all_tiles = list(tm.download_engine.iter_tiles(
        40.0, 39.0, 117.0, 116.0, 10, 10, task_id=task_id
    ))
    assert len(all_tiles) > 2

    # 预置部分 cache 命中(完成态由磁盘 cache 推导)
    cached = all_tiles[:2]
    for tile in cached:
        cache_path = tile.cache_path('m')  # roadmap → 'm'
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'cached-tile')

    conn = get_connection()
    try:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    iter_calls = 0
    real_iter_tiles = tm.download_engine.iter_tiles

    def counting_iter_tiles(*args, **kwargs):
        nonlocal iter_calls
        iter_calls += 1
        return real_iter_tiles(*args, **kwargs)

    tm.download_engine.iter_tiles = counting_iter_tiles

    downloaded = []

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        results = []
        for tile in tiles:
            # 模拟真实下载:落 cache(复制/完成判定的真实输入)再报完成
            cache_path = tile.cache_path(style)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'fresh-tile')
            await progress_callback(tile, 'completed', None)
            results.append({'tile': tile, 'status': 'completed'})
            downloaded.append(tile)
        return results

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert iter_calls == 1, (
        f"iter_tiles 被调用了 {iter_calls} 次 —— completed 清单仍在第二遍枚举重建"
    )
    assert len(downloaded) == len(all_tiles) - len(cached), "cache 命中的瓦片不应重下"

    conn = get_connection()
    try:
        row = conn.cursor().execute(
            'SELECT status, downloaded_tiles FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row['status'] == 'completed'
    assert row['downloaded_tiles'] == len(all_tiles)

    # tiles_only:复制阶段的输入 = cache 命中 + 本次下载,一块都不能少
    # (output_path='downloads' 解析为 DOWNLOADS_DIR/downloads)
    copied = list(
        (Path(Config.DOWNLOADS_DIR) / 'downloads' / f'task_{task_id}').rglob('*.png')
    )
    assert len(copied) == len(all_tiles)
