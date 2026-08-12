"""并发决策竞态 + 崩溃恢复的每任务日志(map 管线)。

覆盖两件事,都在 `src/services/task_manager.py`:

**B1 —— 第二次「接受缺块」/「补漏」会毁掉第一次的那一轮。**
旧实现里 `accept_gaps` / `refill_task` 在**自己**那把锁里装
`_refill_targets` / `_gap_accepted`,然后才调 `_start_run`;而 `_start_run` 把
线程登记在**另一段**临界区里。两段之间隔着 gap_decision 的 commit、一次时间
记录、一次 SELECT 和一次 socketio.emit。第二次调用落进这个窗口时:

  1. 「上一轮线程还活着吗」的闸门读到的线程尚未登记 —— 放行;
  2. 它覆写 `_refill_targets[task_id]`,装上**自己**那一份;
  3. 随后被 `_start_run` 的状态白名单拒绝(行已经是 retrying);
  4. 它 except 里的身份比较 `is targets` **认出的正是自己刚装的那一份**,
     于是 pop 掉 `_refill_targets`、discard 掉 `_gap_accepted`。

撤掉的是**第一轮正在用的**开关。后果:严格逐层缺块闸门重新武装(有洞的 zoom
全部跳过拼接)、产物一个不出、任务从 completed_with_gaps 退回
pending_decision、所有缺块被重新登记 —— 而 `tasks.gap_decision` 已经写着
'accept'。用户看到的只是一句「请稍后重试」,一句把他引向再点一次、再毁一次的
提示。实测 6 次并发命中 3 次。

修复的形状是「把两个事实合成一个」:开关的安装挪进 `_start_run` 里翻状态、
登记线程的那**同一段**临界区。「决定了接受」与「这一轮归我」从此不可分割,
拒绝路径根本走不到安装,也就没有什么需要撤 —— 那两个 except 分支整段删掉了。
(另一种形状是把 except 的撤销收窄成「本次调用确实是当前那一轮」,但它把
`_refill_targets` / `_gap_accepted` 继续暴露在窗口里,任何**别的**读者仍然
会读到一份半装好的状态。)

**B2 —— 进程被杀之后,任务自己的日志里没有任何解释。**
崩溃 / 断电 / 关窗口是四条管线里最常发生的真实终态转移。重启时
`_recover_orphan_running_tasks` 把 running 降级 paused、retrying 回落
pending_decision,但那两条 warning 只进全局日志。用户打开的是
`logs/tasks/map_<id>.log`,而那份日志在崩溃的那一瞬间戛然而止 —— 最后一行是
某个 zoom 的进度,任务看起来是凭空消失的(§4.5:任何终态都要能从**任务自己
的**日志解释原因)。
"""
import os
import sys
import shutil
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.contracts.outcome import TileOutcome  # noqa: E402


# 并发用例的重复次数。审计实测的翻车率是 6 次里 3 次,但下面的握手把交错
# **钉死**了(见 `_race_second_call` 的注释),所以这里不是在赌概率 —— 20 次
# 是为了同时暴露「握手本身有竞态」这类问题:它一旦松了,失败会是零星的。
RACE_ITERATIONS = 20


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Config 的四个落盘根 + BASE_DIR 全部指向 tmp_path,并建库。

    BASE_DIR 一定要打:每任务日志落在 `<BASE_DIR>/logs/tasks/`,不打就写进仓库
    (tests/test_no_repo_pollution.py 明令禁止),而本文件的 B2 用例正要去读它。
    """
    from src.core.config import Config
    from src.core import database
    from src.services.resource_scheduler import reset_scheduler

    monkeypatch.setattr(Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    # 调度器是进程单例,`_start_run` 每一轮都要申请任务位 + 网络额度;
    # 上一条用例(哪怕在别的文件)留下的 owner 键会撞「already holds a reservation」。
    reset_scheduler()
    yield tmp_path
    reset_scheduler()


def _write_png_tile(path, size=16, value=128):
    """写一张 GDAL 真的打得开的 PNG。

    拼接段跑的是真 GDAL(BuildVRT + Translate),塞占位字节进去会让
    `_assert_vrt_covers_tile_grid` 抱怨「gdalbuildvrt skipped intermediates」,
    而那是一条与本文件要测的东西完全无关的红。
    """
    from osgeo import gdal

    path.parent.mkdir(parents=True, exist_ok=True)
    mem = gdal.GetDriverByName('MEM').Create('', size, size, 3, gdal.GDT_Byte)
    for band_idx in range(1, 4):
        mem.GetRasterBand(band_idx).Fill(value)
    png = gdal.GetDriverByName('PNG').CreateCopy(str(path), mem)
    assert png is not None, f"无法写入测试瓦片 {path}"
    png = None
    mem = None


# 选区与 zoom 的取法:z6 一块瓦片 5.625°,0..10° 在两个方向上各跨 2 块 →
# 2x2 = 4 块。要 ≥4 块是因为用例要「留几块洞、其余照拼」,而 1 块的网格里
# 留一个洞就没有任何东西可拼,验不到「产物出没出」。
_BBOX = dict(north=10.0, south=0.0, east=10.0, west=0.0)
_ZOOM = 6


def _seed_gapped_task(gaps):
    """播一条停在 pending_decision 的地图任务,返回 (task_id, tiles, gap_tiles)。

    `gaps` 是 {瓦片序号: TileOutcome},其余瓦片写进 cache(= 已完成)。缺块瓦片
    只在 task_tiles 里留一行、盘上没有文件 —— 稀疏失败表的语义就是这样。

    **先落任务行,再按行反推缓存命名空间**:cache 目录是
    `<style_code>-<配置指纹8位>`,由 `snapshot_for_task_row` 决定。按裸样式码写
    会让运行时一块都命不中,任务转头去打真实上游,用例从「红」变成「挂几分钟」。

    缓存命名空间只由**配置指纹**决定,与 task_id 无关,所以同一个 tmp_path 下
    连跑多轮时上一轮补漏下下来的那块瓦片会被下一轮当成「已缓存」——「补漏只
    补目标」的断言于是收到一个空列表,假绿。每轮先清干净 cache。
    """
    from src.core.config import Config
    from src.core.database import get_connection
    from src.services.download_engine import DownloadEngine
    from src.services.source_registry import snapshot_for_task_row

    shutil.rmtree(Config.CACHE_DIR, ignore_errors=True)

    tiles = list(DownloadEngine().iter_tiles(
        _BBOX['north'], _BBOX['south'], _BBOX['east'], _BBOX['west'],
        _ZOOM, _ZOOM))
    assert len(tiles) >= 4, f"选区/zoom 只枚举出 {len(tiles)} 块瓦片,不够留洞"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles, gap_tiles)
            VALUES ('gapped', 'pending_decision', ?, ?, ?, ?, ?, ?,
                    'satellite', 'image_only', ?, ?, ?, ?, ?)
            """,
            (_BBOX['north'], _BBOX['south'], _BBOX['east'], _BBOX['west'],
             _ZOOM, _ZOOM, str(Config.DOWNLOADS_DIR), len(tiles),
             len(tiles) - len(gaps), len(gaps), len(gaps)),
        )
        task_id = cur.lastrowid
        conn.commit()

        cur.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        source = snapshot_for_task_row(cur.fetchone())

        gap_tiles = []
        for idx, tile in enumerate(tiles):
            outcome = gaps.get(idx)
            if outcome is None:
                _write_png_tile(tile.cache_path(source))
                continue
            gap_tiles.append(tile)
            cur.execute(
                """
                INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count,
                                        error_message)
                VALUES (?, ?, ?, ?, ?, 1, 'seeded')
                """,
                (task_id, tile.zoom, tile.x, tile.y, outcome.value),
            )
        conn.commit()
        return task_id, tiles, gap_tiles
    finally:
        conn.close()


def _task_row(task_id):
    from src.core.database import get_connection

    conn = get_connection()
    try:
        return conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    finally:
        conn.close()


def _product_dir(task_id):
    from src.core.config import Config

    return Config.DOWNLOADS_DIR / f'task_{task_id}'


class _RaceResult:
    """一次并发对撞的观测结果。

    `at_register` / `after_second` 是本轮两个开关在窗口**两端**的快照:
    线程刚登记完时一份、第二次调用收场之后一份。用例比这两份而不是只看终态,
    是因为「撤销」与「读走」之间只隔几行 —— 只钉终态的话,一个把撤销收窄成
    「概率上撞不上」的假修复照样能绿。
    """

    __slots__ = ('errors', 'second_entered_window', 'at_register', 'after_second')

    def __init__(self):
        self.errors = []
        self.second_entered_window = False
        self.at_register = None
        self.after_second = None


def _race_second_call(tm, task_id, call):
    """在「决定已落库、本轮线程尚未登记」的窗口里再发一次同样的调用。

    交错是**钉死的**,不靠 sleep 撞运气。四个事件把两条线程编成一条确定的
    序列,每个握手点都挑在一个两版实现都会经过的位置上:

        T1(主线程)                     T2(第二次调用)
        ── accept_gaps/refill_task 的闸门 + (旧实现)在这里装开关
        _set_gap_decision
          set  t2_start ────────────────▶ 进 accept_gaps/refill_task
                                          闸门放行(此刻还没有任何线程登记)
                                          — 旧实现在这里装上**自己**那份开关 —
                                          _set_gap_decision
          wait t2_past_guard ◀─────────── set t2_past_guard
        _start_run:翻状态 + 登记线程        wait t1_registered
        _record_time_action:快照 ①
          set  t1_registered ───────────▶ 进 _start_run → 被拒
                                          (旧实现的 except 在这里撤掉开关)
          wait t2_done      ◀──────────── set t2_done
        _record_time_action:快照 ②
        _execute_task:读那两个开关

    `_record_time_action` 是紧跟线程登记之后的第一个调用点(两版实现都是),
    所以在它里面挡住 T1,就精确地把 T2 的「被拒 + 撤销」塞进了「线程已登记、
    开关尚未被 `_execute_task` 读走」这一段 —— 也就是审计描述的那个窗口。

    `second_entered_window` 记的是「T2 确实走过了自己那道闸门」。没有它,一次
    「T2 在闸门就被挡掉」的退化交错会让整条用例变成假绿:什么都没对撞,终态
    当然是对的。
    """
    main = threading.current_thread()
    t2_start = threading.Event()
    t2_past_guard = threading.Event()
    t1_registered = threading.Event()
    t2_done = threading.Event()
    result = _RaceResult()

    real_decision = tm._set_gap_decision
    real_time_action = tm._record_time_action

    def snapshot():
        with tm._state_lock:
            return (tm._refill_targets.get(task_id), task_id in tm._gap_accepted)

    def set_gap_decision(tid, decision):
        real_decision(tid, decision)
        if threading.current_thread() is main:
            t2_start.set()
            assert t2_past_guard.wait(30), '第二次调用没能走到 _set_gap_decision'
        else:
            # 走到这一行就说明 T2 已经过了自己那道「上一轮线程还活着吗」的闸门。
            result.second_entered_window = True
            t2_past_guard.set()
            assert t1_registered.wait(30), '第一轮没能登记线程'

    def record_time_action(tid, action):
        real_time_action(tid, action)
        if threading.current_thread() is main and not t2_done.is_set():
            result.at_register = snapshot()
            t1_registered.set()
            assert t2_done.wait(30), '第二次调用没能收场'
            result.after_second = snapshot()

    def second():
        assert t2_start.wait(30), '第一轮没能走到 _set_gap_decision'
        try:
            call()
        except BaseException as e:   # noqa: BLE001 —— 拿到什么记什么,由用例断言
            result.errors.append(e)
        finally:
            # 兜底放行:T2 若在到达 _set_gap_decision 之前就被拒(不是本用例要
            # 的交错,但不能让 T1 挂死在 wait 上,那会把一次「交错没命中」变成
            # 一次 30 秒超时)。退化交错由 second_entered_window 断言兜住。
            t2_past_guard.set()
            t1_registered.set()
            t2_done.set()

    tm._set_gap_decision = set_gap_decision
    tm._record_time_action = record_time_action
    t2 = threading.Thread(target=second, name='second-decision', daemon=True)
    t2.start()
    try:
        call()
    finally:
        t2.join(60)
        del tm._set_gap_decision
        del tm._record_time_action
    assert not t2.is_alive(), '第二次调用的线程没退出'
    assert result.second_entered_window, (
        '第二次调用在闸门就被挡掉了,根本没进窗口 —— 这一轮什么都没对撞')
    assert result.at_register is not None and result.after_second is not None, (
        '没能在窗口两端取到快照 —— 握手失效了')
    return result


def _join_run(tm, task_id):
    """等这一轮的后台线程收工(补漏走的是后台线程,不像接受缺块是同步的)。"""
    thread = tm.active_tasks.get(task_id)
    if thread is not None:
        thread.join(60)
        assert not thread.is_alive(), f"Task {task_id} 的执行线程没退出"


# --------------------------------------------------------------------------
# B1-a:并发「接受缺块」
# --------------------------------------------------------------------------

def test_concurrent_accept_gaps_does_not_cancel_the_first_run(isolated_config):
    """第二次「接受缺块」被拒,而**第一轮照样跑完并出产物**。

    旧实现在这里的失败形状是完整的一串,所以断言也钉一串:状态没退回
    pending_decision、产物真的在盘上、gap_decision 与状态互相自洽。只钉状态
    的话,「状态对了但产物没出」这种半修复照样能绿。
    """
    from src.services.task_manager import TaskManager

    for i in range(RACE_ITERATIONS):
        tm = TaskManager()
        task_id, tiles, _ = _seed_gapped_task({0: TileOutcome.RETRYABLE_FAILURE})

        race = _race_second_call(tm, task_id, lambda: tm.accept_gaps(task_id))
        _join_run(tm, task_id)

        assert len(race.errors) == 1, (
            f"第 {i + 1} 轮:第二次「接受缺块」必须被明确拒绝(一次决策只该有"
            f"一轮执行),实际拿到 {race.errors!r}")
        assert isinstance(race.errors[0], ValueError), (
            f"第 {i + 1} 轮:第二次调用抛的是 {race.errors[0]!r},"
            f"路由层按 ValueError→400 映射,别的类型会变成 500")

        # 窗口两端的快照。第一份钉的是**原子性**:线程刚登记完,两个开关就已经
        # 在位了 —— 装它们的是 `_start_run` 翻状态、登记线程的那同一段临界区。
        # 第二份钉的是**不可撤销**:第二次调用已经被拒并走完了自己的收场,
        # 而这一轮的开关一个字节都没变(旧实现在这里读到的是 (None, False))。
        assert race.at_register == (set(), True), (
            f"第 {i + 1} 轮:线程都登记了,「接受缺块」的开关却还没装上 —— "
            f"「决定接受」与「这一轮归我」不是同一个事实,窗口还开着。"
            f"实际 {race.at_register!r}")
        assert race.after_second == (set(), True), (
            f"第 {i + 1} 轮:第二次调用把第一轮正在用的开关撤掉了。实际 "
            f"{race.after_second!r} —— (None, False) 就是那条竞态的现场。")

        row = _task_row(task_id)
        assert row['status'] == 'completed_with_gaps', (
            f"第 {i + 1} 轮:第一轮被第二次点击毁掉了 —— 状态是 {row['status']!r}。"
            f"旧实现在这里是 pending_decision:第二次调用的 except 按身份比较"
            f"撤掉的是**它自己刚装上去的**那一份,而那一份此刻正被第一轮使用。")
        assert row['gap_decision'] == 'accept', (
            f"第 {i + 1} 轮:gap_decision 与状态脱节 —— 用户的决定丢了")

        products = sorted(_product_dir(task_id).glob('*.tif'))
        assert products, (
            f"第 {i + 1} 轮:产物一张都没出。`_gap_accepted` 被撤掉之后逐层缺块"
            f"闸门重新武装,有洞的 zoom 全部跳过拼接 —— 这正是那条竞态最贵的"
            f"后果,状态断言单独是抓不到它的。")
        assert products[0].stat().st_size > 0, f"第 {i + 1} 轮:产物是个空文件"

        # 本轮的两个开关必须已经被 `_run_task` 的 finally 清干净:留着的话下一次
        # 普通启动会继承它,变成「只下上一轮那几块」的静默半量运行。
        assert task_id not in tm._refill_targets
        assert task_id not in tm._gap_accepted


# --------------------------------------------------------------------------
# B1-b:并发「补漏」
# --------------------------------------------------------------------------

def test_concurrent_refill_keeps_the_first_runs_target_set(isolated_config):
    """第二次「补漏」被拒,而第一轮**仍然只补它自己那几个洞**。

    这里的失败形状与「接受缺块」不同:被撤掉的 `_refill_targets` 让第一轮读到
    None,而 None 的语义是「不限目标」= 把全部缺块重新下一遍。所以断言钉的是
    **交给下载器的瓦片集合**,而不只是终态 —— 光看终态,全量重下也是绿的。

    播两个洞、只有一个值得重试:
      · retryable_failure —— 超时那一类,补漏的目标;
      · no_data —— 上游明确说过这里没有数据,`_retryable_gap_keys` 不收它,
        再问一遍只是浪费配额。
    目标集合没了的话,下载器会同时收到这两块。
    """
    from src.contracts.outcome import TileOutcome as TO
    from src.services.source_registry import snapshot_for_task_row
    from src.services.task_manager import TaskManager

    for i in range(RACE_ITERATIONS):
        tm = TaskManager()
        task_id, tiles, gap_tiles = _seed_gapped_task({
            0: TO.RETRYABLE_FAILURE,
            1: TO.NO_DATA,
        })
        retryable_tile, no_data_tile = gap_tiles
        source = snapshot_for_task_row(_task_row(task_id))

        handed_to_downloader = []

        async def fake_download(tiles=None, style=None, progress_callback=None,
                                stop_flag=None, **_kw):
            """替身下载器:记下它被要求下哪些瓦片,并把它们「下」下来。

            必须真的写 cache 并回调 success,否则补漏跑完洞还在,终态断言测的
            就成了别的东西。
            """
            for tile in tiles:
                handed_to_downloader.append((tile.zoom, tile.x, tile.y))
                _write_png_tile(tile.cache_path(source))
                await progress_callback(tile, TO.SUCCESS.value, None, 128)

        tm.download_engine.download_tiles_batch = fake_download

        race = _race_second_call(tm, task_id, lambda: tm.refill_task(task_id))
        _join_run(tm, task_id)

        assert len(race.errors) == 1, (
            f"第 {i + 1} 轮:第二次「补漏」必须被拒,实际 {race.errors!r}")

        target_key = (retryable_tile.zoom, retryable_tile.x, retryable_tile.y)
        assert race.at_register == ({target_key}, False), (
            f"第 {i + 1} 轮:线程都登记了,补漏的目标集合却还没装上。"
            f"实际 {race.at_register!r}")
        assert race.after_second == ({target_key}, False), (
            f"第 {i + 1} 轮:第二次调用撤掉了第一轮的目标集合。实际 "
            f"{race.after_second!r} —— None 的语义是「不限目标」= 全量重下。")

        assert handed_to_downloader == [
            (retryable_tile.zoom, retryable_tile.x, retryable_tile.y)], (
            f"第 {i + 1} 轮:第一轮的目标集合被第二次调用撤掉了 —— 下载器收到的"
            f"是 {handed_to_downloader},而不是那一块值得重试的洞。"
            f"`_refill_targets` 变回 None 的语义是「不限目标」,于是连"
            f"no_data({no_data_tile.zoom},{no_data_tile.x},{no_data_tile.y})"
            f"也被重新问了一遍。")

        row = _task_row(task_id)
        assert row['status'] == 'completed_with_gaps', (
            f"第 {i + 1} 轮:补漏跑完之后状态是 {row['status']!r}。洞补上了,"
            f"只剩 no_data(上游说过没有)—— 那一层照拼,产物永久带 has_gaps 标记。")
        assert row['gap_decision'] == 'refill'
        assert sorted(_product_dir(task_id).glob('*.tif')), (
            f"第 {i + 1} 轮:补漏跑完了却没有产物")
        assert task_id not in tm._refill_targets


# --------------------------------------------------------------------------
# B2:SIGKILL → 重启,任务自己的日志里要有解释
# --------------------------------------------------------------------------

def _task_log_text(task_id):
    from src.services.task_logging import task_log_path

    path = task_log_path('map', task_id)
    assert path.exists(), (
        f"logs/tasks/map_{task_id}.log 根本没建出来 —— 用户打开任务日志会看到"
        f"「无日志」,而库里状态已经变了")
    return path.read_text(encoding='utf-8')


@pytest.mark.parametrize('crashed_status,recovered_status,keyword', [
    ('running', 'paused', '退出'),
    ('retrying', 'pending_decision', '退出'),
])
def test_restart_explains_the_crash_in_the_tasks_own_log(
        isolated_config, crashed_status, recovered_status, keyword):
    """进程被杀之后重启,`logs/tasks/map_<id>.log` 里必须留下一句解释。

    这是四条管线里**最常发生**的真实终态转移(关窗口、断电、任务管理器结束
    进程),而它以前只写全局日志。用户打开任务日志看到的是崩溃那一刻戛然而止
    的进度行,没有任何终态 —— §4.5 的门槛(任何终态都能从任务自己的日志解释
    原因)在最常见的那条路上是不成立的。

    模拟 SIGKILL 的方式就是「库里留着一行活动态、进程里没有任何线程」:那正是
    `_recover_orphan_running_tasks` 在 `__init__` 里看到的东西,它的 docstring
    也是这么论证的(此刻 active_tasks 必然是空的,所以活动态行必然是遗骸)。
    """
    from src.core.config import Config
    from src.core.database import get_connection
    from src.services.task_manager import TaskManager

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('killed', ?, 1, 0, 1, 0, 0, 0, 'satellite', 'tiles_only',
                    ?, 1, 0, 0)
            """,
            (crashed_status, str(Config.DOWNLOADS_DIR)),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    TaskManager()   # 重启:__init__ 里跑 _recover_orphan_running_tasks

    assert _task_row(task_id)['status'] == recovered_status

    text = _task_log_text(task_id)
    assert f'status={recovered_status}' in text, (
        f"任务日志里没有终态事件 —— 库里已经是 {recovered_status},日志却没提。"
        f"实际内容:\n{text}")
    assert 'process_restart' in text, (
        f"没写清楚是**为什么**变成这个状态。'已降级' 三个字不解释任何东西,"
        f"用户要知道的是「进程死了」。实际内容:\n{text}")
    assert keyword in text, f"缺少给人读的那一句。实际内容:\n{text}"



def test_stranded_retry_fallback_is_explained_in_the_tasks_own_log(isolated_config):
    """补漏线程搁死 → 行回落 pending_decision,这一笔也要写进任务日志。

    这是本文件之外的第二个「改了行却只写全局日志」的点。它与
    `fail_stranded_running_task` 那条网的区别只是状态清单:那条只认 running,
    而补漏 /「接受缺块」跑的是 retrying。没有这行日志,用户看到的最后一句是
    `EVENT thread_finished failed=False`,库里却写着 pending_decision 和一句
    「补漏未完成」—— 两份记录当面打架,排查无从下手(§4.5)。

    造「搁死」的方式是让 `_execute_task` 静默 return:那正是引擎里那几个
    `if stop_flag: return` 出口的形状,没有异常可捕,行原样停在 retrying。
    """
    from src.services.task_manager import TaskManager

    tm = TaskManager()
    task_id, _tiles, _gaps = _seed_gapped_task({0: TileOutcome.RETRYABLE_FAILURE})

    from src.core.database import get_connection
    conn = get_connection()
    try:
        conn.execute("UPDATE tasks SET status = 'retrying' WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    async def _quiet_return(tid, stop_flag=None, *, tlog, **_granted):
        return

    tm._execute_task = _quiet_return
    tm._run_status[task_id] = 'retrying'
    tm.active_tasks[task_id] = threading.current_thread()
    tm._run_task(task_id)

    assert _task_row(task_id)['status'] == 'pending_decision', (
        '搁死的 retrying 行没有回落 —— 它既不在 RESUMABLE 也不在 REFILLABLE 里,'
        '用户每个按钮都会被拒,只剩重启进程')

    text = _task_log_text(task_id)
    assert 'retry_thread_stranded' in text, (
        f"补偿改了行却没在任务日志里留下终态事件。实际内容:\n{text}")
    assert '补漏未完成' in text, (
        f"没写清楚回落的原因。实际内容:\n{text}")