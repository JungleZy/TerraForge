"""随包底图的启动预热：状态机 + 节流广播。

⚠️ 本文件不碰真实解压。`ensure_base_unpacked` 一律打桩 —— 真解压是 167 MB /
4.3 万个文件，而且会落进仓库的 assets/terrain（CI 里测试跑在 Nuitka 打包之前，
解出来的东西会被打进三个平台的产物）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeSocketIO:
    """记录 emit 载荷；start_background_task 只登记不执行，由用例自己调。

    不真起线程：线程让断言变成「等多久算够」的竞猜，是 flaky 的标准来源。
    执行体是个普通函数，直接调它就能覆盖全部逻辑。
    """

    def __init__(self, emit_raises=False, spawn_raises=False):
        self.events = []
        self.tasks = []
        self._emit_raises = emit_raises
        self._spawn_raises = spawn_raises

    def emit(self, name, payload=None):
        if self._emit_raises:
            raise RuntimeError("client gone")
        self.events.append((name, payload))

    def start_background_task(self, target, *args, **kwargs):
        if self._spawn_raises:
            raise RuntimeError("can't start new thread")
        self.tasks.append((target, args, kwargs))

    # 用例便利：只看 base_unpack_progress 的载荷序列
    def payloads(self):
        from src.services.base_terrain_warmup import EVENT_NAME
        return [p for n, p in self.events if n == EVENT_NAME]


@pytest.fixture(autouse=True)
def _clean_state():
    """模块级单例在同一个 pytest 进程里跨用例残留，不清就得靠用例顺序。"""
    from src.services import base_terrain_warmup as w
    w.reset_state()
    yield
    w.reset_state()


def test_ready_base_skips_the_thread_entirely(monkeypatch):
    """底图已就位 → 直接 ready，连后台任务都不起。

    这是 99% 的启动路径。起线程再让它立刻退出也能得到同样的状态，但那要多付
    一次线程创建，而启动路径上的每一毫秒都在用户盯着空白页的时间里。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: True)
    sio = FakeSocketIO()

    w.start_warmup(sio)

    assert sio.tasks == [], "已就位时不该起后台任务"
    assert w.snapshot() == {"phase": "ready", "fraction": 1.0, "error": None}
    assert sio.payloads()[-1]["phase"] == "ready"


def test_missing_base_starts_a_background_task_and_reaches_ready(monkeypatch, tmp_path):
    """底图缺失 → 起后台任务；执行体跑完后状态是 ready。"""
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    sio = FakeSocketIO()

    w.start_warmup(sio)

    assert w.snapshot()["phase"] == "running", "起线程之前就该置 running"
    assert len(sio.tasks) == 1
    target, args, _ = sio.tasks[0]

    target(*args)   # 手动执行后台体

    assert w.snapshot() == {"phase": "ready", "fraction": 1.0, "error": None}
    assert sio.payloads()[-1]["phase"] == "ready"


def test_repeated_start_does_not_spawn_a_second_task(monkeypatch):
    """已经在跑时再调 start_warmup → 不起第二个。

    生产上 create_app 只调一次，但这条挡的是「有人为了『保险』在别处又调了一次」，
    那会让两个线程同时抢跨进程锁，后到的那个白等几分钟。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: None)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    w.start_warmup(sio)

    assert len(sio.tasks) == 1


def test_spawn_failure_does_not_wedge_the_state_at_running(monkeypatch):
    """起后台任务本身失败 → 状态落到 failed，而不是永远卡在 running。

    start_warmup 是先在锁里置 running、再调 start_background_task。这一调用抛
    异常（起线程/greenlet 失败）时，phase 已经是 running 了 —— 而 running 那道
    闸会挡掉之后的每一次重试，状态永远转下去，没有任何东西告诉用户已经没救了。
    与「后台线程漏出的异常不能让状态停在 running」是同一条硬约束，只是入口不同。

    异常本身要往上冒：起不了线程是真问题，不该静默。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    sio = FakeSocketIO(spawn_raises=True)

    with pytest.raises(RuntimeError):
        w.start_warmup(sio)

    state = w.snapshot()
    assert state["phase"] == "failed", "起线程失败后状态卡在 running 就再也退不出来了"
    assert state["error"], "必须带上原因"
    assert sio.payloads()[-1]["phase"] == "failed"


def test_failed_state_allows_a_retry(monkeypatch, tmp_path):
    """失败之后再调 start_warmup 会重新尝试。

    语义是「确保底图在解压或已就位」，不是「一辈子只跑一次」。生产上不会发生
    （create_app 只调一次），但「失败 → 修好前提 → 重试成功」是要能走通的。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise RuntimeError("Read-only file system")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()
    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])
    assert w.snapshot()["phase"] == "failed"

    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    w.start_warmup(sio)
    assert len(sio.tasks) == 2, "failed 之后必须能重试"
    sio.tasks[1][0](*sio.tasks[1][1])
    assert w.snapshot()["phase"] == "ready"


def test_unpack_failure_lands_in_failed_with_the_reason(monkeypatch):
    """解压抛 RuntimeError → phase=failed 且 error 带得上原因。

    最常见的原因是 assets/ 不可写（装在 Program Files、只读介质）。状态栏要
    hover 出这句话，error 是空的话用户只能看到「底图不可用」而无从下手。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise RuntimeError("随包底图解压失败：[Errno 30] Read-only file system")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    state = w.snapshot()
    assert state["phase"] == "failed"
    assert "Read-only" in state["error"]
    assert sio.payloads()[-1]["phase"] == "failed"


def test_missing_parts_is_a_failure_not_a_silent_ready(monkeypatch):
    """ensure_base_unpacked 返回 None（分卷被删）→ 同样是 failed。

    返回 None 在 base_terrain 的契约里不是异常，但在用户视角与解压失败是同一件
    事：底图不可用。当成成功处理的话状态栏会安静地什么都不显示，而地形产出从此
    不自包含 —— 又一款静默降级。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: None)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    state = w.snapshot()
    assert state["phase"] == "failed"
    assert state["error"], "分卷缺失必须给出原因，不能是空 error 的 failed"


def test_progress_is_throttled_but_the_terminal_event_always_fires(monkeypatch, tmp_path):
    """上万次回调只发少数几条，但终态一定发得出去。

    解压是流式读，readinto 每次调用都触发 stage_cb —— 167 MB 下上万次，不节流
    会把前端打爆。而终态被节流窗口吃掉就永远不发了，前端进度条卡在 97% 转到
    天荒地老 —— 所以终态必须绕过节流。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def fake_unpack(stage_cb=None):
        for i in range(500):
            stage_cb("base_unpack", i / 500.0)
        return tmp_path

    monkeypatch.setattr(w, "ensure_base_unpacked", fake_unpack)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    payloads = sio.payloads()
    running = [p for p in payloads if p["phase"] == "running"]
    assert len(running) < 20, f"节流没生效：500 次回调发出了 {len(running)} 条"
    assert payloads[-1] == {"phase": "ready", "fraction": 1.0, "error": None}


def test_throttle_window_actually_releases_and_fraction_advances(monkeypatch, tmp_path):
    """把节流窗口设成 0 → 每一次回调都发得出去，且 fraction 单调递增。

    上一条用例的 500 次回调在 1 ms 内跑完，所以它只证明了「1 ms 内最多发 1 条」
    和「终态绕过节流」。它证明不了**窗口会正常释放**：把 _EMIT_MIN_INTERVAL 改成
    巨大值、或者把时间戳写成永不过期的形式，那条用例照样全绿，而线上表现是进度条
    在几分钟的解压里冻在 0% —— 这个特性一半的价值就没了。fraction 也一样：上一条
    不看载荷里的数值，「永远上报 0.0」在它眼里同样是绿的。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "_EMIT_MIN_INTERVAL", 0)

    def fake_unpack(stage_cb=None):
        for i in range(500):
            stage_cb("base_unpack", i / 500.0)
        return tmp_path

    monkeypatch.setattr(w, "ensure_base_unpacked", fake_unpack)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    # start_warmup 自己会先发一条 running（fraction=0.0），那条不是节流的产物，
    # 从这里切掉，只数后台执行体发出来的。
    baseline = len(sio.payloads())
    sio.tasks[0][0](*sio.tasks[0][1])

    running = [p for p in sio.payloads()[baseline:] if p["phase"] == "running"]
    assert len(running) == 500, f"窗口没释放：500 次回调只发出 {len(running)} 条"
    fractions = [p["fraction"] for p in running]
    assert fractions == sorted(fractions), "fraction 必须随解压推进"
    assert fractions[0] == 0.0 and fractions[-1] == 499 / 500.0


def test_event_name_is_pinned_to_the_wire_string():
    """钉住线上事件名。

    FakeSocketIO.payloads() 是从被测模块 import EVENT_NAME 来过滤的，所以把
    EVENT_NAME 改成别的值，其余用例全绿 —— 而前端监听写的是字面量，会静默失联。
    """
    from src.services import base_terrain_warmup as w

    assert w.EVENT_NAME == "base_unpack_progress"


def test_emit_failure_does_not_break_the_state_machine(monkeypatch, tmp_path):
    """emit 抛异常（客户端断开）不能影响解压与状态。

    与 base_terrain._emit、cesiumlab_terrain._gdal_stage_callback 同一条既有约定：
    一次广播故障不该让整件事失败。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)
    monkeypatch.setattr(w, "ensure_base_unpacked", lambda stage_cb=None: tmp_path)
    sio = FakeSocketIO(emit_raises=True)

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    assert w.snapshot()["phase"] == "ready"


def test_unexpected_exception_still_lands_in_failed(monkeypatch):
    """契约是 RuntimeError，但漏出别的异常时状态不能停在 running。

    后台线程里的异常会静默吞掉线程，状态永远是 running —— 前端进度条一直转，
    没有任何东西告诉用户已经没救了。
    """
    from src.services import base_terrain_warmup as w

    monkeypatch.setattr(w, "is_base_ready", lambda _p: False)

    def boom(stage_cb=None):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(w, "ensure_base_unpacked", boom)
    sio = FakeSocketIO()

    w.start_warmup(sio)
    sio.tasks[0][0](*sio.tasks[0][1])

    assert w.snapshot()["phase"] == "failed"


def test_snapshot_returns_a_copy(monkeypatch):
    """调用方改不动内部状态。

    返回内部 dict 的话，connect handler 把它 emit 出去的同时后台线程还在改它 ——
    序列化到一半状态变了，客户端收到 phase=running / fraction=1.0 这种撕裂组合。
    """
    from src.services import base_terrain_warmup as w

    first = w.snapshot()
    first["phase"] = "tampered"
    assert w.snapshot()["phase"] == "idle"
