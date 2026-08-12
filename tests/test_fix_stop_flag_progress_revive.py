"""置位停止标志后进度广播必须立刻停止 —— 否则任务状态会自己「复活」。

## 缺陷

点「暂停」后界面变成「已暂停」，约 0.5 秒后自己翻回「运行中」，并**永久**停在
那里 —— 刷新页面才恢复。

四个环节叠加而成：

1. `pause_task()` 只 `UPDATE tasks SET status='paused'` + 置位 stop flag，
   **不发任何进度事件**；
2. 下载循环不是立刻停的，要跑到当前批次边界，期间 `progress_callback` 照发；
3. 那个载荷里的 `'status': task.status` 取自**内存** Task 对象 —— pause 只改
   库、不碰它，所以仍然是 `'running'`；
4. 前端把这发推送合并进任务行，「已暂停」被覆盖回「运行中」。而库里已经是
   paused，收尾时 `_complete_task` 看到它直接 return，**再也不发**
   `task_completed` —— 没有任何事件能把界面纠正回来。

`delete_task` 走同一条路（同样置位 stop flag、同样不改内存对象），只是它连行
都删了，那几发进度打在不存在的任务上。

## 修法

`progress_callback` 的广播分支加 `_is_stop_requested` 守卫：stop 一旦被请求就
不再广播进度。状态迁移本来就不该由进度流承载 —— pause 由 `pause_task` 自己
广播库里的真值。计数在收尾时落库，前端下次拉列表即准。

## 为什么必须有对照组

两发进度之间没有 `PROGRESS_EMIT_MIN_INTERVAL`(0.5s) 的间隔，靠的是
`done_tiles >= task.total_tiles` 那条「必发」分支绕过节流。少了对照组，
「第二发没出现」无法区分是守卫拦的还是节流拦的 —— 那样第一条测试会假绿。
"""
import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.contracts.outcome import TileOutcome  # noqa: E402


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
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

    def emit(self, event, payload):
        self.events.append((event, payload))


def _seed_running_single_tile_task():
    """zoom 0 的任务:全球只有一块瓦片 (0/0/0),total_tiles 恒为 1。

    单瓦片是刻意的 —— 让每一发进度都满足 `done_tiles >= total_tiles`,
    从而走「必发」分支绕开时间节流,把变量收敛到只剩 stop 守卫。
    """
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
            VALUES ('cancel-race', 'running', 1, 0, 1, 0, 0, 0,
                    'satellite', 'tiles_only', ?, 1, 0, 0)
            """,
            (str(Config.DOWNLOADS_DIR),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _run_two_progress_pushes(tm, sio, task_id, stop, *, set_stop_between):
    """跑一次 _execute_task,在同一批次里报两发进度。

    第一发之后按 set_stop_between 决定是否置位 stop flag（模拟用户点取消），
    然后报第二发。返回两发各自之后累计的 task_progress 条数。
    """
    counts = {}

    async def fake_batch(tiles, style, progress_callback, stop_flag=None,
                         source=None, **_):
        # tiles 是生成器(引擎按 DOWNLOAD_BATCH_SIZE 惰性 islice,任务侧不再
        # 物化全网格清单),不能下标取。
        # source= / max_concurrency= 是引擎新增的关键字参数;不收下 source
        # 就只能按旧 style 码写缓存,而 _execute_task 读的是指纹命名空间。
        tile = next(iter(tiles))
        cache_path = tile.cache_path(source or style)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b'fresh-tile')

        # 第 1 发:任务确实在跑,必须广播。status 是 `TileOutcome` 的值
        # ('success',旧 'completed')。
        await progress_callback(tile, TileOutcome.SUCCESS.value, None)
        counts['first'] = sum(1 for e, _ in sio.events if e == 'task_progress')

        if set_stop_between:
            # pause_task / delete_task 做的事:改库(或删行) + 置位 stop
            # flag,内存里的 task.status 保持 'running' 不变。
            stop.set()

        # 第 2 发:同样满足 done >= total,不会被节流拦掉。
        await progress_callback(tile, TileOutcome.SUCCESS.value, None)
        counts['second'] = sum(1 for e, _ in sio.events if e == 'task_progress')

        return [{'tile': tile, 'status': TileOutcome.SUCCESS.value}]

    tm.download_engine.download_tiles_batch = fake_batch
    asyncio.run(tm._execute_task(task_id, stop_flag=stop))
    return counts


def test_progress_broadcast_stops_once_stop_requested(isolated_config):
    """stop flag 置位后不得再广播 task_progress(否则前端状态被复活)。"""
    from src.services.task_manager import TaskManager

    sio = _FakeSocketIO()
    tm = TaskManager(socketio=sio)
    task_id = _seed_running_single_tile_task()
    stop = threading.Event()

    counts = _run_two_progress_pushes(
        tm, sio, task_id, stop, set_stop_between=True)

    assert counts['first'] > 0, (
        '运行中的进度必须照常广播 —— 守卫不能把正常进度也掐掉'
    )
    assert counts['second'] == counts['first'], (
        f"stop flag 置位后不得再广播 task_progress,"
        f"实际又发了 {counts['second'] - counts['first']} 发"
    )


def test_progress_still_broadcasts_without_stop_request(isolated_config):
    """对照组:不置位 stop flag 时第二发必须照发。

    这一条是上一条的有效性证明:它确认两发之间没有被节流拦住,所以上一条
    看到的「第二发消失」只能是 stop 守卫的作用。
    """
    from src.services.task_manager import TaskManager

    sio = _FakeSocketIO()
    tm = TaskManager(socketio=sio)
    task_id = _seed_running_single_tile_task()
    stop = threading.Event()

    counts = _run_two_progress_pushes(
        tm, sio, task_id, stop, set_stop_between=False)

    assert counts['second'] > counts['first'], (
        '不置 stop flag 时第二发进度必须照发 —— 否则它是被节流拦的,'
        '上一条测试就是假绿'
    )


def test_removing_the_guard_reproduces_the_bug(isolated_config, monkeypatch):
    """反证:把守卫打成恒 False,第二发立刻复现。

    这条钉的是「拦住第二发的确实是 stop 守卫」。没有它,前两条测试无法排除
    「其实是别的机制(节流、批次边界、engine 自己的 stop 处理)顺手拦掉了」
    这种可能 —— 那种情况下守卫可以被整条删掉而测试全绿。
    """
    from src.services.task_manager import TaskManager

    monkeypatch.setattr(TaskManager, '_is_stop_requested',
                        lambda self, task_id, stop_flag=None: False)

    sio = _FakeSocketIO()
    tm = TaskManager(socketio=sio)
    task_id = _seed_running_single_tile_task()
    stop = threading.Event()

    counts = _run_two_progress_pushes(
        tm, sio, task_id, stop, set_stop_between=True)

    assert counts['second'] > counts['first'], (
        '关掉守卫后第二发进度应当复现(这正是缺陷本身)。若这里也不发,'
        '说明拦住它的是别的机制,前两条测试并没有在测守卫'
    )


def test_stop_requested_falls_back_to_registered_flag(isolated_config):
    """pause_task 置位的是 self.stop_flags[task_id] 那一份。

    _execute_task 拿到的 stop_flag 参数与登记表里的是同一个 Event 对象
    (start_task 建好后同时放进 stop_flags 和线程参数)。这里直接验证
    _is_stop_requested 在只有登记表被置位时也返回 True —— 守卫不能只认
    传进来的那个引用。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager(socketio=_FakeSocketIO())
    task_id = _seed_running_single_tile_task()

    assert tm._is_stop_requested(task_id) is False

    flag = threading.Event()
    tm.stop_flags[task_id] = flag
    assert tm._is_stop_requested(task_id) is False

    flag.set()
    assert tm._is_stop_requested(task_id) is True, (
        '只置位登记表里的 flag 时守卫也必须生效 —— pause_task 走的就是这条路'
    )
