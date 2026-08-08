"""2026-08-08 评审 P1#3 / P1#4 的回归测试。

P1#3 暂停后十分钟内恢复不了,两个半边:
  a. `start_task` 对「上一轮线程还在收尾」和「真的在运行」用同一句
     `already running` —— 界面显示「已暂停」而每次点恢复都收到与之矛盾的报错。
  b. `stitch_tiles_with_gdal` 整段没有取消点(注释自称单层「十分钟级」),
     所以那个窗口真的有十分钟长。

P1#4 `_execute_task` 把整张网格物化成三个 list + 一个 per-tile dict,
抵消了 `download_tiles_batch`「tiles 可以是生成器、按批惰性消费」的设计。
"""
import asyncio
import importlib
import inspect
import itertools
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from src.core.config import Config
    from src.core import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    return tmp_path


class _FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, data=None, **kwargs):
        self.events.append((event, data))


def _params(**overrides):
    from src.core.config import Config

    p = dict(
        name='t', north=40.0, south=39.0, east=117.0, west=116.0,
        zoom_min=10, zoom_max=11, style='roadmap',
        output_format='tiles_only', output_path=str(Config.DOWNLOADS_DIR),
    )
    p.update(overrides)
    return p


def _seed_task_row(status='paused', output_format='tiles_only', total=1,
                   zoom_min=10, zoom_max=10):
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
            VALUES ('t', ?, 40.0, 39.0, 117.0, 116.0, ?, ?, 'roadmap',
                    ?, ?, ?, 0, 0)
            """,
            (status, zoom_min, zoom_max, output_format,
             str(Config.DOWNLOADS_DIR), total),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _set_status(task_id, status):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        conn.execute('UPDATE tasks SET status = ? WHERE id = ?', (status, task_id))
        conn.commit()
    finally:
        conn.close()


def _task_row(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    finally:
        conn.close()


class _LiveThread:
    """一条真在跑的线程,`is_alive()` 是真值而不是替身返回的常量。

    模拟的是「pause_task 已经提交 paused、置了停止标志,而上一轮
    _run_task 线程还卡在没有取消点的收尾步骤里」这个真实状态。
    """

    def __init__(self):
        self._release = threading.Event()
        self.thread = threading.Thread(
            target=self._release.wait, args=(30,), daemon=True)
        self.thread.start()

    def stop(self):
        self._release.set()
        self.thread.join(timeout=10)


# ---------------------------------------------------------------------------
# P1#3a:恢复被拒时的理由必须与界面一致
# ---------------------------------------------------------------------------


def test_resume_while_previous_run_winds_down_reports_stopping_not_running(
        isolated_config):
    """库里是 paused、上一轮线程仍活着 → 必须报「正在停止」而不是「already running」。

    旧行为:`start_task` 只看线程活没活,一律抛
    `ValueError("Task N is already running")`。用户看到的界面是「已暂停」
    (pause_task 先提交了状态),而每次点恢复都收到「正在运行」——两句话
    互相否定,且没有任何一句提示「等几秒再试」这个唯一正确的动作。

    这条断言不空转:它同时要求**不含** already running。只断言
    「抛了异常」会在旧代码上照样绿。
    """
    from src.services.task_manager import TaskManager, TaskStillStoppingError

    tm = TaskManager()
    task_id = _seed_task_row(status='paused')
    live = _LiveThread()
    tm.active_tasks[task_id] = live.thread
    try:
        with pytest.raises(TaskStillStoppingError) as excinfo:
            tm.resume_task(task_id)
    finally:
        live.stop()

    message = str(excinfo.value)
    assert 'already running' not in message, (
        f"界面显示「已暂停」,报错却说在运行 —— 两个互相矛盾的事实: {message}"
    )
    assert 'still stopping' in message and 'retry' in message, (
        f"文案必须说明「上一轮还在收尾、稍后重试」,实际: {message}"
    )
    assert _task_row(task_id)['status'] == 'paused', (
        '被拒的恢复不得改动任务状态'
    )


def test_genuinely_running_task_still_reports_already_running(isolated_config):
    """对照组:库里也是 running 时,文案必须仍是旧的 already running。

    没有这条,上一条可以靠「把 already running 整句删掉」通过 —— 那会让
    真正的重复启动也变成「稍后重试」,把一条永久性拒绝说成临时性拒绝。
    """
    from src.services.task_manager import TaskManager, TaskStillStoppingError

    tm = TaskManager()
    task_id = _seed_task_row(status='running')
    live = _LiveThread()
    tm.active_tasks[task_id] = live.thread
    try:
        with pytest.raises(ValueError) as excinfo:
            tm.start_task(task_id)
    finally:
        live.stop()

    assert not isinstance(excinfo.value, TaskStillStoppingError), (
        '真的在跑不是「正在停止」,不能给用户「等几秒就好」的暗示'
    )
    assert 'already running' in str(excinfo.value)


def test_dead_previous_thread_does_not_block_resume(isolated_config):
    """对照组:线程已经结束时,恢复必须照常放行。

    新增的分支只能收窄「拒绝」的理由,不能把它扩大到已经收尾的任务上。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager()
    task_id = _seed_task_row(status='paused')
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    tm.active_tasks[task_id] = dead

    started = []
    tm._run_task = lambda tid, stop_flag=None: started.append(tid)
    tm.resume_task(task_id)
    tm.active_tasks[task_id].join(timeout=10)

    assert started == [task_id]
    assert _task_row(task_id)['status'] == 'running'


def test_resume_route_maps_stopping_error_to_4xx_with_honest_message(
        monkeypatch, tmp_path):
    """POST /api/tasks/<id>/resume 必须回 4xx,且文案不与「已暂停」矛盾。

    路由层对 start/resume 统一 `except ValueError -> 400 + str(e)`,所以
    TaskStillStoppingError 继承 ValueError 就够 —— 这条钉住那个继承关系
    真的成立(改成继承 Exception 会变成 500,前端弹一个通用错误)。
    """
    from src.core import config

    monkeypatch.setattr(config.Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(config.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'CACHE_DIR', tmp_path / 'cache')
    for mod in ('app', 'src.core.database', 'src.services.task_manager',
                'src.services.dem_task_manager', 'src.routes', 'src.routes.api',
                'src.routes.dem_api', 'src.routes.contour_api'):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module('app')
    app_mod.app.config['TESTING'] = True
    client = app_mod.app.test_client()

    api_mod = importlib.import_module('src.routes.api')
    task_id = _seed_task_row(status='paused')

    live = _LiveThread()
    api_mod.task_manager.active_tasks[task_id] = live.thread
    try:
        resp = client.post(f'/api/tasks/{task_id}/resume')
    finally:
        live.stop()
        api_mod.task_manager.active_tasks.pop(task_id, None)

    assert 400 <= resp.status_code < 500, resp.get_json()
    error = resp.get_json()['error']
    assert 'already running' not in error, (
        f"界面是「已暂停」,接口却回「正在运行」: {error}"
    )
    assert 'still stopping' in error


# ---------------------------------------------------------------------------
# P1#3b:拼接必须有取消点
# ---------------------------------------------------------------------------


def _prime_cache_tiles(engine, tiles, style='m'):
    for tile in tiles:
        path = engine._get_cache_path(tile, style)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not-a-real-png')


@pytest.mark.parametrize('cpu_count', [1, 4])
def test_stitch_aborts_at_the_per_tile_georef_loop(isolated_config, monkeypatch,
                                                   cpu_count):
    """拼接中途置停止标志 → 逐瓦片配准循环里立刻抛 StitchCancelled。

    旧行为:`stitch_tiles_with_gdal` 整段没有任何取消点,单层 zoom 一拼
    「十分钟级」,期间 pause 只能改库、线程照跑到底。

    断言不是「抛了异常」而是「**没配准完**」—— 只断言抛异常的话,把检查点
    挪到函数最后一行也能通过,而那正是修复要消灭的东西。
    """
    from src.services.download_engine import DownloadEngine, StitchCancelled

    monkeypatch.setattr(os, 'cpu_count', lambda: cpu_count)

    engine = DownloadEngine()
    tiles = list(engine.iter_tiles(40.0, 39.0, 117.0, 116.0, 10, 10))
    assert len(tiles) > 8, '测试前提:该 zoom 至少 9 块瓦片,才能看出「没跑完」'
    _prime_cache_tiles(engine, tiles)

    stop = threading.Event()
    georefed = []

    def stub_georef(cache_path, tile, output_dir=None):
        georefed.append((tile.zoom, tile.x, tile.y))
        stop.set()  # 第一块配准完就相当于用户点了暂停
        return str(cache_path)

    monkeypatch.setattr(engine, '_add_georeference', stub_georef)

    out = isolated_config / 'downloads' / 'mosaic.tif'
    with pytest.raises(StitchCancelled):
        engine.stitch_tiles_with_gdal(
            tiles, 'm', str(out), 10, stop_flag=stop)

    # 串行路径恰好 1 块;并行路径最多再多 max_workers-1 块(已经进入
    # _add_georeference 的 worker 要跑完当前这块)。无论如何都远小于全量。
    assert len(georefed) <= cpu_count, (
        f"标志置位后仍配准了 {len(georefed)}/{len(tiles)} 块 —— 取消点没生效"
    )
    assert not out.exists(), '取消的拼接不得留下产物'


def test_stitch_with_flag_already_set_does_no_work_at_all(isolated_config):
    """开跑前标志就已置位 → 一块瓦片都不碰。

    暂停发生在上一层 zoom 期间时走的就是这条路;它同时证明检查点确实在
    循环体内部,而不是只挂在某个 GDAL 阶段之前。
    """
    from src.services.download_engine import DownloadEngine, StitchCancelled

    engine = DownloadEngine()
    tiles = list(engine.iter_tiles(40.0, 39.0, 117.0, 116.0, 10, 10))
    # 刻意**不**预置 cache 文件:真跑起来会先抛 FileNotFoundError,
    # 所以只要拿到的是 StitchCancelled,就证明一次配准都没发起。
    stop = threading.Event()
    stop.set()

    with pytest.raises(StitchCancelled):
        engine.stitch_tiles_with_gdal(
            tiles, 'm', str(isolated_config / 'downloads' / 'm.tif'), 10,
            stop_flag=stop)


def test_execute_task_threads_stop_flag_into_stitch_and_treats_cancel_as_stop(
        isolated_config):
    """_execute_task 必须把 stop_flag 传进拼接,且 StitchCancelled 不算失败。

    两件事一起钉:
      · 不传 stop_flag 的话引擎里的取消点是死的(旧行为等价);
      · 取消若被当成拼接失败,一次用户主动暂停会把任务写成 failed 并挂上
        「拼接失败」的错误信息,还会广播 task_stitch_failed。
    """
    from src.services.download_engine import StitchCancelled
    from src.services.task_manager import TaskManager

    sio = _FakeSocketIO()
    tm = TaskManager(socketio=sio)
    task_id = _seed_task_row(status='running', output_format='image_only',
                             total=1)

    # 让拼接阶段有活可干:预置该 zoom 的 cache 命中。
    tiles = list(tm.download_engine.iter_tiles(
        40.0, 39.0, 117.0, 116.0, 10, 10, task_id=task_id))
    _prime_cache_tiles(tm.download_engine, tiles)

    stop = threading.Event()
    seen = {}

    def spinning_stitch(tiles, style, output_path, zoom_level, stop_flag=None, **_):
        # 替身:在一个小迭代上自转,模拟真实拼接的逐瓦片循环。
        seen['stop_flag'] = stop_flag
        for i in range(100):
            if stop_flag is not None and stop_flag.is_set():
                seen['aborted_at'] = i
                raise StitchCancelled('cancelled')
            if i == 3:
                stop.set()  # 用户在拼接进行中点了暂停
        raise AssertionError('停止标志没有被观察到 —— 拼接跑满了整个循环')

    tm.download_engine.stitch_tiles_with_gdal = spinning_stitch

    asyncio.run(tm._execute_task(task_id, stop_flag=stop))

    assert seen['stop_flag'] is stop, (
        'stop_flag 没有被传进拼接 —— 引擎里的取消点收不到任何信号'
    )
    assert seen['aborted_at'] == 4, (
        f"应在标志置位后的下一圈就退出,实际第 {seen.get('aborted_at')} 圈"
    )
    row = _task_row(task_id)
    assert row['status'] != 'failed', '用户主动暂停不是拼接失败'
    assert not row['error_message'], f"不得写错误信息,实际: {row['error_message']}"
    assert not any(name == 'task_stitch_failed' for name, _ in sio.events)
    assert not any(name == 'task_completed' for name, _ in sio.events)


# ---------------------------------------------------------------------------
# P1#4:待下载集合惰性消费,不物化全网格
# ---------------------------------------------------------------------------


def test_download_receives_a_lazy_generator_not_a_materialised_grid(
        isolated_config):
    """喂给 download_tiles_batch 的必须是生成器,且按需产出。

    旧行为:`_execute_task` 把整张网格物化成 `tiles: List[Tile]`
    (外加 `completed_tiles`、它的快照 `cache_hit_tiles`、以及一个每瓦片
    一项的 `session_status` 字典)。引擎的 docstring 明写 tiles 可以是
    生成器、按 DOWNLOAD_BATCH_SIZE 惰性 islice,`create_task` 也早就改用
    `count_tiles` 而不是 `calculate_tiles` —— 硬上限改软告警之后,百万级
    瓦片是被文档化的用法。

    只断言 `isgenerator` 会是空转的:`iter(list)` 不是生成器,但
    `(t for t in whole_grid_list)` 是,而它背后照样躺着一份全网格列表。
    所以真正的断言是**峰值产出量**:消费方只 islice 了一批,枚举就必须
    只推进了一批。
    """
    from src.services.task_manager import TaskManager

    batch_size = 4
    tm = TaskManager()
    task_id = tm.create_task(_params(zoom_min=10, zoom_max=11))
    _set_status(task_id, 'running')

    all_tiles = list(tm.download_engine.iter_tiles(
        40.0, 39.0, 117.0, 116.0, 10, 11, task_id=task_id))
    assert len(all_tiles) > 3 * batch_size, (
        '测试前提:网格必须显著大于一批,否则「只推进一批」无从区分'
    )

    # 每一趟枚举各自计数:第 0 趟是 cache 分类(必然走完全网格),
    # 第 1 趟是喂给下载的待下载生成器 —— 它必须跟着消费走。
    produced = []
    real_iter_tiles = tm.download_engine.iter_tiles

    def instrumented_iter_tiles(*args, **kwargs):
        index = len(produced)
        produced.append(0)

        def counting():
            for tile in real_iter_tiles(*args, **kwargs):
                produced[index] += 1
                yield tile

        return counting()

    tm.download_engine.iter_tiles = instrumented_iter_tiles

    seen = {}
    downloaded = []

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None):
        seen['is_generator'] = inspect.isgenerator(tiles)
        tile_iterator = iter(tiles)
        peak = 0
        while True:
            # 复刻引擎的消费形态(download_engine.download_tiles_batch)
            batch = list(itertools.islice(tile_iterator, batch_size))
            if not batch:
                break
            peak = max(peak, produced[1] - len(downloaded))
            for tile in batch:
                cache_path = tile.cache_path(style)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(b'fresh-tile')
                await progress_callback(tile, 'completed', None)
                downloaded.append(tile)
        seen['peak_outstanding'] = peak

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert seen['is_generator'], '待下载清单仍是被物化过的序列'
    assert seen['peak_outstanding'] <= batch_size, (
        f"任一时刻超前产出 {seen['peak_outstanding']} 块(批大小 {batch_size})"
        f" —— 待下载集合被一次性物化了"
    )
    assert produced[0] == len(all_tiles), (
        'cache 分类那一趟本来就要走完全网格(每块瓦片一次 stat),'
        '这条确认上面的计数器确实挂上了'
    )
    assert len(downloaded) == len(all_tiles)
    assert _task_row(task_id)['downloaded_tiles'] == len(all_tiles)


def test_pending_generator_skips_exactly_the_cache_hits(isolated_config):
    """断点续传:生成器产出的必须恰好是「缓存里没有的那些」,顺序不变。

    待下载集合改成生成器之后它由「枚举 + 与命中清单归并」重建,归并错位
    会让已缓存的瓦片被重下、或缺口被跳过 —— 前者浪费带宽,后者是产物缺块
    却报完成。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(zoom_min=10, zoom_max=11))
    _set_status(task_id, 'running')

    all_tiles = list(tm.download_engine.iter_tiles(
        40.0, 39.0, 117.0, 116.0, 10, 11, task_id=task_id))
    # 挑一批**不连续**的瓦片做命中:连续前缀的归并即便写错也可能碰巧对。
    cached = [t for i, t in enumerate(all_tiles) if i % 3 == 0]
    _prime_cache_tiles(tm.download_engine, cached)
    expected = [(t.zoom, t.x, t.y) for i, t in enumerate(all_tiles) if i % 3]

    got = []

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None):
        for tile in tiles:
            got.append((tile.zoom, tile.x, tile.y))
            cache_path = tile.cache_path(style)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'fresh-tile')
            await progress_callback(tile, 'completed', None)

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    assert got == expected, '待下载生成器与命中清单归并错位'
    assert _task_row(task_id)['downloaded_tiles'] == len(all_tiles)


def test_backfill_copies_only_the_cache_hit_prefix(isolated_config):
    """补拷线程只复制枚举时命中的那一段,不跟着下载新增的尾巴跑。

    旧实现靠 `cache_hit_tiles = list(completed_tiles)` 拿快照,那是第二份
    全网格列表(P1#4 点名的三个 list 之一)。改成按下标只走前缀之后,若
    误写成直接 `for tile in completed_tiles`,list 的迭代器会一路跟进下载
    回调 append 进来的新元素 —— 每块下载的瓦片被复制两次。

    竞态必须被消掉才能稳定复现:补拷线程在复制第一块命中瓦片时被闸门挡住,
    直到下载把全部瓦片报完(completed_tiles 已长出尾巴)才放行。此时
    「按下标只走前缀」与「for-in 列表对象」的行为差异是确定的。
    """
    from collections import Counter

    from src.services.task_manager import TaskManager

    tm = TaskManager()
    task_id = tm.create_task(_params(zoom_min=10, zoom_max=10))
    _set_status(task_id, 'running')

    all_tiles = list(tm.download_engine.iter_tiles(
        40.0, 39.0, 117.0, 116.0, 10, 10, task_id=task_id))
    cached = all_tiles[:2]
    assert len(all_tiles) > len(cached) + 2
    _prime_cache_tiles(tm.download_engine, cached)

    download_finished = threading.Event()
    backfill_gated = threading.Event()
    copies = []
    copies_lock = threading.Lock()
    real_copy = TaskManager._stream_copy_tile

    def counting_copy(self, tile, style_code, output_base, made_dirs, lock):
        in_backfill = threading.current_thread().name.endswith('-backfill')
        with copies_lock:
            copies.append((in_backfill, (tile.zoom, tile.x, tile.y)))
        if in_backfill and not backfill_gated.is_set():
            # 把补拷线程按在它复制的第一块上,直到下载把 completed_tiles
            # 追加完毕 —— 否则「补拷是否跟着尾巴跑」取决于线程调度,断言会飘。
            backfill_gated.set()
            download_finished.wait(30)
        return real_copy(self, tile, style_code, output_base, made_dirs, lock)

    tm._stream_copy_tile = counting_copy.__get__(tm, TaskManager)

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None):
        for tile in tiles:
            cache_path = tile.cache_path(style)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b'fresh-tile')
            await progress_callback(tile, 'completed', None)
        download_finished.set()

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    # 边下边复制阶段的两条写入路径互补:命中的那 N 块由补拷线程写一次,
    # 其余由下载回调写一次,合起来每块瓦片恰好一次。(结尾复制阶段是另一段
    # 独立的循环,不经 _stream_copy_tile,所以这里数到的就是边下边复制。)
    backfill_copies = [key for in_backfill, key in copies if in_backfill]
    assert backfill_copies == [(t.zoom, t.x, t.y) for t in cached], (
        f"补拷线程复制了 {len(backfill_copies)} 块,命中的只有 {len(cached)} 块"
        f" —— 它跟着下载往 completed_tiles 追加的尾巴一起跑了"
    )
    per_tile = Counter(key for _, key in copies)
    assert per_tile, '一次复制都没发生 —— 测试本身没跑到边下边复制'
    assert max(per_tile.values()) == 1, (
        f"有瓦片在边下边复制阶段被写了两次: "
        f"{[k for k, n in per_tile.items() if n > 1][:3]}"
    )
    assert len(per_tile) == len(all_tiles), '命中 + 下载的瓦片必须一块不少'


# ---------------------------------------------------------------------------
# 边下边复制的临时件:名字要带 pid,失败要自己清
# (2026-08-08 评审的 `*.part.*` 登记表条目,由 DeletionCleanup 交接过来)
# ---------------------------------------------------------------------------


def _copy_fixture(isolated_config):
    from src.models.task import Tile
    from src.services.task_manager import TaskManager

    tm = TaskManager()
    tile = Tile(task_id=1, zoom=10, x=3, y=4, status='pending', retry_count=0)
    src = tile.cache_path('m')
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'tile-bytes')
    out = isolated_config / 'downloads' / 'task_1'
    return tm, tile, out


def test_stream_copy_temp_name_carries_the_pid(isolated_config, monkeypatch):
    """临时件名必须是 `.part.<pid>.<thread_ident>`,pid 在前。

    旧名字只有线程 id。task_cleanup 的 `*.part.*` 登记表用 `_part_owner_pid`
    从这个槽位读 pid 再和活进程比对,拿到线程 id 会把「另一个活进程正在写」
    和「上次进程留下的垃圾」判反 —— 那边只能加量级兜底当作「归属未知」。
    """
    import shutil as shutil_mod

    from src.services.task_cleanup import _part_owner_pid

    tm, tile, out = _copy_fixture(isolated_config)
    seen = {}
    real_copy2 = shutil_mod.copy2

    def spy_copy2(src, dst):
        seen['tmp'] = os.path.basename(str(dst))
        return real_copy2(src, dst)

    monkeypatch.setattr('src.services.task_manager.shutil.copy2', spy_copy2)
    tm._stream_copy_tile(tile, 'm', out, set(), threading.Lock())

    assert _part_owner_pid(seen['tmp']) == os.getpid(), (
        f"临时件名 {seen['tmp']} 里解析不出本进程 pid"
    )
    assert str(threading.get_ident()) in seen['tmp'], (
        '线程 id 段不能丢:下载回调线程与补拷线程会写同一块瓦片的临时件'
    )


def test_stream_copy_removes_its_temp_file_when_the_copy_fails(
        isolated_config, monkeypatch):
    """copy2/replace 抛异常时,临时件必须被生产者自己删掉。

    任务输出目录不在任何启动清扫根里(清扫只走 CACHE_DIR,遍历每个历史任务的
    {z}/{x} 目录被评估后否决),所以没有第二次机会:磁盘满 / 目标盘掉线会在
    每个瓦片目录下留一堆 .part 文件,而下次复制的「同尺寸跳过」判定看的是
    dest,永远不会覆盖它们。
    """
    import shutil as shutil_mod

    tm, tile, out = _copy_fixture(isolated_config)
    leftovers = {}
    real_copy2 = shutil_mod.copy2

    def failing_copy2(src, dst):
        real_copy2(src, dst)  # 先真的把临时件写出来
        leftovers['tmp'] = str(dst)
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr('src.services.task_manager.shutil.copy2', failing_copy2)

    with pytest.raises(OSError):
        tm._stream_copy_tile(tile, 'm', out, set(), threading.Lock())

    from pathlib import Path
    assert not Path(leftovers['tmp']).exists(), (
        f"复制失败后残留了 {leftovers['tmp']}"
    )


def test_engine_reports_each_tile_exactly_once(isolated_config):
    """引擎的每个出口都是「回调一次后立刻 return」,每块瓦片每次运行只上报一次。

    这是删掉 `session_status`(每瓦片一项、从不裁剪的全网格字典)之后
    `_status_count_deltas` 的 old_status 口径所依赖的前提:回调里
    `old_status` 只从稀疏失败表的内存镜像推,不再记「本次运行已上报过什么」。
    前提一旦被破坏,重复上报会重复计数 —— 所以把它钉在引擎这一侧。

    覆盖 `_download_single_tile` 的全部五个出口。
    """
    from src.models.task import Tile
    from src.services import download_engine as de

    engine = de.DownloadEngine()
    style = 'm'

    async def count_callbacks(tile, exit_case, monkey):
        calls = []

        async def cb(t, status, error, size_bytes=None):
            calls.append(status)

        await engine._download_single_tile(
            tile=tile, style=style, session=None, cache_enabled=True,
            progress_callback=cb)
        return calls

    def fresh_tile(y):
        return Tile(task_id=0, zoom=10, x=1, y=y, status='pending', retry_count=0)

    results = {}

    async def run_all():
        # ① cache 命中
        tile = fresh_tile(1)
        path = engine._get_cache_path(tile, style)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'cached')
        results['cache_hit'] = await count_callbacks(tile, 'cache_hit', None)

        # ② 网络成功
        tile = fresh_tile(2)

        async def ok_download(*a, **k):
            return b'\x89PNG\r\n\x1a\nbytes'

        engine.download_tile = ok_download
        results['network_ok'] = await count_callbacks(tile, 'network', None)

        # ③ 落 cache 失败
        tile = fresh_tile(3)
        real_open = de.aiofiles.open

        def boom_open(*a, **k):
            raise OSError('disk full')

        de.aiofiles.open = boom_open
        try:
            results['cache_write_fail'] = await count_callbacks(tile, 'cw', None)
        finally:
            de.aiofiles.open = real_open

        # ④ 下载失败
        tile = fresh_tile(4)

        async def bad_download(*a, **k):
            raise RuntimeError('404')

        engine.download_tile = bad_download
        results['network_fail'] = await count_callbacks(tile, 'fail', None)

        # ⑤ 用户取消
        tile = fresh_tile(5)

        async def cancelled_download(*a, **k):
            raise de.DownloadCancelled('stopped')

        engine.download_tile = cancelled_download
        results['cancelled'] = await count_callbacks(tile, 'cancel', None)

    asyncio.run(run_all())

    assert results['cache_hit'] == ['completed']
    assert results['network_ok'] == ['completed']
    assert results['cache_write_fail'] == ['failed']
    assert results['network_fail'] == ['failed']
    assert results['cancelled'] == [], (
        '取消的瓦片不上报 —— 上报了就会被计成一次真实结果'
    )
