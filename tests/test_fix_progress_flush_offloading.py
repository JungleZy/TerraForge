"""进度攒批落库(flush_progress_counts)必须挪出下载事件循环线程 —— M3 残留。

2026-08-03 审查报告的 M3 只修了主要阻塞源(`_stream_copy_tile` 已包
`asyncio.to_thread`),但同一个回调里的 flush 仍在事件循环上同步做
sqlite executemany + commit;M3 条目的「范围说明」点破了这一点,改法只列了
copy。DEM 侧的对照写法在 `dem_task_manager.py`(那边是包了的)。

本文件钉三件事:
1. 批次 flush 的 sqlite 写发生在工作线程,不占用下载事件循环;
2. 摘批必须原子 —— 并发回调在 flush 写盘期间登记的失败行一条都不能丢;
3. flush 写盘失败时批次退回队列,由后续 flush 补上,不静默丢失败行。

第 2、3 条在改动前就是绿的(同步 flush 天然原子),留作改动的护栏:它们才是
「把 flush 挪到别的线程」最容易踩坏的地方 —— executemany 执行期间 sqlite3
会释放 GIL,若工作线程直接读那三个待写列表,事件循环上交错 append 进来的新
元素会被随后的 clear() 一起抹掉,失败瓦片静默丢记录,完成判定的
failed_count>0 就守不住(报告「模式 2」那类静默失败)。
"""
import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


from src.contracts.outcome import TileOutcome  # noqa: E402


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


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


def _params(**overrides):
    from src.core.config import Config

    p = dict(
        name='t', north=1.0, south=0.0, east=1.0, west=0.0,
        zoom_min=10, zoom_max=10, style='roadmap',
        output_format='tiles_only', output_path=str(Config.DOWNLOADS_DIR),
    )
    p.update(overrides)
    return p


def _mark_running(task_id):
    """_execute_task 直接跑时需要任务处于 running(正常路径由 start_task 置位)。"""
    from src.core.database import get_connection

    conn = get_connection()
    try:
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def _task_row(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM tasks WHERE id = ?', (task_id,)
        ).fetchone()
    finally:
        conn.close()


def _tile_rows(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.cursor().execute(
            'SELECT * FROM task_tiles WHERE task_id = ? ORDER BY zoom, x, y', (task_id,)
        ).fetchall()
    finally:
        conn.close()


def _install_recording_connection(monkeypatch, tm_mod,
                                  before_write=None, after_write=None):
    """把 tm_mod.get_connection 换成记录「写操作发生在哪个线程」的代理。

    返回 writes 列表,元素是 (kind, thread_ident);kind ∈ {'counts','tiles'}。

    两个钩子的时机差别是有讲究的,别合并成一个:
    - before_write 在真实 SQL 之前调用 —— 注入「写盘失败」用它;
    - after_write  在真实 SQL 之后调用 —— 撑开「已写入、尚未清队列」那段窗口
      用它。摘批不原子的坏实现正是在这一段把交错 append 进来的新登记连同已
      写入的一起 clear 掉。窗口开在 SQL 之前测不出这个缺陷:executemany 迭代
      的是活列表,那时涌进来的新元素会被它一并写库,clear 抹掉的都是已落库的。
    """
    writes = []
    real_get_connection = tm_mod.get_connection

    class RecordingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            is_counts = 'UPDATE tasks' in sql and 'downloaded_tiles = MAX' in sql
            if is_counts:
                writes.append(('counts', threading.get_ident()))
                if before_write:
                    before_write('counts')
            result = self._conn.execute(sql, *args, **kwargs)
            if is_counts and after_write:
                after_write('counts')
            return result

        def executemany(self, sql, *args, **kwargs):
            is_tiles = 'task_tiles' in sql
            if is_tiles:
                writes.append(('tiles', threading.get_ident()))
                if before_write:
                    before_write('tiles')
            result = self._conn.executemany(sql, *args, **kwargs)
            if is_tiles and after_write:
                after_write('tiles')
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        tm_mod, 'get_connection', lambda *a, **kw: RecordingConnection(
            real_get_connection(*a, **kw)))
    return writes


# ---------- 1. flush 的 sqlite 写不占用事件循环线程 ----------

def test_batch_flush_runs_off_the_event_loop_thread(isolated_config, monkeypatch):
    """批次 flush 必须跑在工作线程上。

    会让本用例翻红的生产改动:把回调里的 flush 改回事件循环上同步调用
    (即 M3 修复前的写法)。

    收尾那一次 flush(下载循环结束的 finally)仍留在事件循环上 —— 那时已无
    并发回调,同步执行更简单可靠,异常路径上也不该再引入新的挂起点。所以
    断言是「事件循环线程上最多只发生一次计数写」,而不是「一次都没有」。
    """
    import src.services.task_manager as tm_mod

    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    # zoom 12-13 = 708 块,跨过 3 个批次(PROGRESS_DB_FLUSH_INTERVAL=200)
    task_id = tm.create_task(_params(zoom_min=12, zoom_max=13))
    _mark_running(task_id)

    writes = _install_recording_connection(monkeypatch, tm_mod)

    loop_thread = {}

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None, **_):
        loop_thread['ident'] = threading.get_ident()
        for tile in tiles:
            await progress_callback(tile, TileOutcome.SUCCESS.value, None)
        return [{'tile': t, 'status': TileOutcome.SUCCESS.value} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    asyncio.run(tm._execute_task(task_id))

    count_writes = [ident for kind, ident in writes if kind == 'counts']
    assert len(count_writes) >= 4, (
        f"708 块瓦片应有 3 次批次 flush + 1 次收尾 flush,实际计数写 "
        f"{len(count_writes)} 次"
    )

    on_loop = [i for i in count_writes if i == loop_thread['ident']]
    assert len(on_loop) <= 1, (
        f"{len(on_loop)} 次进度落库跑在下载事件循环线程上 —— 批次 flush 必须走 "
        f"asyncio.to_thread,只有收尾那次允许留在事件循环上。"
        f"事件循环 ident={loop_thread['ident']},各次写入线程={count_writes}"
    )

    # 落库结果不受挪线程影响
    row = _task_row(task_id)
    assert row['downloaded_tiles'] == 708
    assert row['failed_tiles'] == 0


# ---------- 2. 摘批原子性:并发回调交错时一条失败行都不能丢 ----------

def test_concurrent_callbacks_lose_no_failed_rows_while_flushing(
        isolated_config, monkeypatch):
    """并发回调 + flush 在别的线程写盘时,失败行与计数必须一条不差。

    会让本用例翻红的生产改动:让工作线程直接读 pending_tile_* 三个列表并在
    executemany 之后 clear() —— executemany 期间 sqlite3 释放 GIL,事件循环
    上交错 append 进来的登记会被那次 clear() 连带抹掉。
    """
    import src.services.task_manager as tm_mod

    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params(zoom_min=12, zoom_max=13))  # 708 块
    _mark_running(task_id)

    # 在 SQL **执行之后** 小睡,撑开「已写入、尚未清队列」那段窗口 —— 坏实现
    # 的 clear() 落在这之后,会把这段时间里交错 append 进来的登记一并抹掉。
    # 睡在 SQL 之前是测不出来的(见 _install_recording_connection 的说明)。
    def slow_after_write(kind):
        import time
        time.sleep(0.005)

    _install_recording_connection(monkeypatch, tm_mod, after_write=slow_after_write)

    async def report(i, tile, progress_callback):
        # 上报必须**分散在时间轴上**,否则测不出东西:708 个协程若中间没有 await
        # 点,事件循环会在 to_thread 的调度间隙里把它们一口气跑完,executemany
        # 开始迭代时列表早已是全量 —— 坏实现的 clear() 抹掉的全是已落库的行,
        # 碰巧无害。真实生产里每次回调前有网络 IO,上报天然分散,新登记会落进
        # executemany 正在执行的那几毫秒。这里用递增延迟复现同一分布。
        await asyncio.sleep(i * 0.0002)
        await progress_callback(tile, TileOutcome.RETRYABLE_FAILURE.value, 'boom')

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None, **_):
        await asyncio.gather(
            *(report(i, t, progress_callback) for i, t in enumerate(tiles)))
        return [{'tile': t, 'status': TileOutcome.RETRYABLE_FAILURE.value,
                 'error': 'boom'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    asyncio.run(tm._execute_task(task_id))

    rows = _tile_rows(task_id)
    assert len(rows) == 708, (
        f"708 块全失败,稀疏失败表应有 708 行,实际 {len(rows)} 行 —— "
        f"丢行意味着完成判定的 failed_count>0 守不住,任务会被误判 completed"
    )
    row = _task_row(task_id)
    assert row['failed_tiles'] == 708, (
        f"failed_tiles 应为 708,实际 {row['failed_tiles']}"
    )


# ---------- 3. flush 写盘失败时批次退回队列,由后续 flush 补上 ----------

def test_failed_flush_returns_batch_to_queue(isolated_config, monkeypatch):
    """第一次 flush 写盘抛异常后,那批登记不能丢 —— 后续 flush 必须补上。

    会让本用例翻红的生产改动:摘批后不管写成功与否都丢弃批次(同步版是
    executemany 成功后才 clear,失败时数据留在队列里等下次重试)。
    """
    import src.services.task_manager as tm_mod

    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = tm.create_task(_params(zoom_min=12, zoom_max=13))  # 708 块
    _mark_running(task_id)

    state = {'boom_left': 1}

    def maybe_boom(kind):
        # 只炸第一次 tiles 写:那批 200 行必须退回队列,由下一次 flush 带走
        if kind == 'tiles' and state['boom_left'] > 0:
            state['boom_left'] -= 1
            raise RuntimeError('disk full (injected)')

    _install_recording_connection(monkeypatch, tm_mod, before_write=maybe_boom)

    async def fake_download_tiles_batch(tiles, style, progress_callback,
                                        stop_flag=None, **_):
        for tile in tiles:
            await progress_callback(tile, TileOutcome.RETRYABLE_FAILURE.value, 'boom')
        return [{'tile': t, 'status': TileOutcome.RETRYABLE_FAILURE.value,
                 'error': 'boom'} for t in tiles]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch
    asyncio.run(tm._execute_task(task_id))

    assert state['boom_left'] == 0, '注入的异常没被触发,本用例没测到东西'
    rows = _tile_rows(task_id)
    assert len(rows) == 708, (
        f"一次写盘失败后应由后续 flush 补上全部 708 行,实际 {len(rows)} 行"
    )
