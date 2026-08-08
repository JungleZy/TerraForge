"""DEM 下载的两处现场问题：临时件落点，以及下载中看不到速度。

1. `downloads/dem/dem_task_N/Copernicus_..._DEM.tif.part` —— 原子写的临时件
   直接写进任务产物目录。用户看到的目录里混着半成品，而启动清扫
   （task_cleanup.sweep_startup_residue）只扫 CACHE_DIR，回收不到这些残留。
   修复后：启用缓存时临时件落在 `cache/dem/<name>.part.<pid>.<id>`，写完先
   原子落进缓存、再 link/copy 进任务目录；任务目录全程只出现最终产物。

2. 任务行上看不到下载速度 —— 单颗 COG 30-50MB、几分钟才下完，而颗粒级状态
   回调（downloading → completed）在这几分钟里一发都不出：前端 5s 就判过期
   （static/js/task_list.js 的 SPEED_STALE_MS）显示 0 B/s。修复后引擎按
   _BYTES_REPORT_MIN_INTERVAL 聚合上报在途字节（bytes_callback），任务管理器
   据此推速度并 emit。
"""

import asyncio
import importlib
import itertools
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _StubConfig:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeResp:
    def __init__(self, chunks, on_chunk=None):
        self.status = 200
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
        self.content = self
        self._chunks = chunks
        self._on_chunk = on_chunk

    async def iter_chunked(self, _n):
        for c in self._chunks:
            yield c
            if self._on_chunk is not None:
                self._on_chunk()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, proxy=None):
        return self._resp


def _make_engine(monkeypatch, tmp_path, resp, cache_enabled="true"):
    from src.core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    import src.services.dem_download_engine as dde

    engine = dde.DemDownloadEngine()
    engine.config = _StubConfig({
        "dem_cache_enabled": cache_enabled,
        "max_retries": "0",
        "request_timeout": "5",
        "concurrent_downloads": "2",
    })
    monkeypatch.setattr(dde.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(resp))
    monkeypatch.setattr(dde.aiohttp, "TCPConnector", lambda *a, **k: None)
    monkeypatch.setattr(dde.aiohttp, "CookieJar", lambda *a, **k: None)
    return engine


# ---------------------------------------------------------------------------
# 1. 临时件落点
# ---------------------------------------------------------------------------

def test_part_file_is_staged_in_cache_not_in_the_task_dir(monkeypatch, tmp_path):
    """下载全程任务目录里不得出现 .part；临时件在 cache/dem 下。"""
    out = tmp_path / "out"
    cache_dem = tmp_path / "cache" / "dem"
    snapshots = []

    def snapshot():
        snapshots.append((
            sorted(p.name for p in out.iterdir()),
            sorted(p.name for p in cache_dem.iterdir()),
        ))

    payload = b"y" * 30
    engine = _make_engine(
        monkeypatch, tmp_path,
        _FakeResp([payload[:10], payload[10:20], payload[20:]], on_chunk=snapshot),
    )

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30", granules=["tile/G.tif"], output_dir=out,
    ))

    assert snapshots, "回调没被触发，用例本身失效"
    for task_names, cache_names in snapshots:
        assert task_names == [], f"下载途中任务目录必须为空，实际: {task_names}"
        assert any(n.startswith("G.tif.part.") for n in cache_names), (
            f"临时件必须落在 cache/dem，实际: {cache_names}")

    # 收尾：两边都是完整产物，任何一边都不留临时件
    assert (out / "G.tif").read_bytes() == payload
    assert (cache_dem / "G.tif").read_bytes() == payload
    assert list(out.glob("*.part*")) == []
    assert list(cache_dem.glob("*.part*")) == []


def test_part_file_falls_back_to_task_dir_when_cache_is_disabled(monkeypatch, tmp_path):
    """关掉缓存就没有缓存目录可用 —— 退回任务目录内原子写。

    强行走 CACHE_DIR 会让「不要缓存」的用户凭空多一次跨盘整份拷贝。
    """
    out = tmp_path / "out"
    seen = []

    payload = b"z" * 20
    engine = _make_engine(
        monkeypatch, tmp_path,
        _FakeResp([payload[:10], payload[10:]],
                  on_chunk=lambda: seen.append(sorted(p.name for p in out.iterdir()))),
        cache_enabled="false",
    )

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30", granules=["tile/G.tif"], output_dir=out,
    ))

    assert any(n.startswith("G.tif.part.") for names in seen for n in names), (
        f"关缓存时临时件应落在任务目录，实际: {seen}")
    assert (out / "G.tif").read_bytes() == payload
    assert list(out.glob("*.part*")) == []
    assert not (tmp_path / "cache" / "dem" / "G.tif").exists(), "关缓存时不得写缓存"


# ---------------------------------------------------------------------------
# 2. 在途字节
# ---------------------------------------------------------------------------

def test_bytes_callback_reports_in_flight_bytes_before_completion(monkeypatch, tmp_path):
    """在途字节必须在颗粒 completed **之前**报出来，总量与 payload 一致。"""
    payload = b"w" * 45
    engine = _make_engine(
        monkeypatch, tmp_path,
        _FakeResp([payload[:15], payload[15:30], payload[30:]]),
    )

    events = []

    async def progress(granule, status, error, size):
        events.append(("status", status))

    async def on_bytes(granule, n):
        events.append(("bytes", n))

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30", granules=["tile/G.tif"], output_dir=tmp_path / "out",
        progress_callback=progress, bytes_callback=on_bytes,
    ))

    byte_events = [n for kind, n in events if kind == "bytes"]
    assert sum(byte_events) == len(payload), f"在途字节总量对不上: {events}"
    completed_at = next(i for i, (kind, v) in enumerate(events)
                        if kind == "status" and v == "completed")
    first_bytes_at = next(i for i, (kind, _) in enumerate(events) if kind == "bytes")
    assert first_bytes_at < completed_at, (
        f"字节必须在颗粒完成前就报出来，否则中间几分钟前端拿不到速度: {events}")


def test_bytes_callback_exception_does_not_fail_the_download(monkeypatch, tmp_path):
    """回调异常不得被重试逻辑当成下载失败（会白下几十 MB）。"""
    payload = b"v" * 20
    engine = _make_engine(monkeypatch, tmp_path, _FakeResp([payload[:10], payload[10:]]))

    statuses = []

    async def progress(granule, status, error, size):
        statuses.append(status)

    async def on_bytes(granule, n):
        raise RuntimeError("boom")

    asyncio.run(engine.download_files(
        dataset="COP-DEM-GLO-30", granules=["tile/G.tif"], output_dir=tmp_path / "out",
        progress_callback=progress, bytes_callback=on_bytes,
    ))

    assert statuses[-1] == "completed", f"回调异常不应影响下载结果: {statuses}"
    assert (tmp_path / "out" / "G.tif").read_bytes() == payload


# ---------------------------------------------------------------------------
# 3. 任务管理器：在途字节 -> 速度推送
# ---------------------------------------------------------------------------

def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    dtm = importlib.import_module("src.services.dem_task_manager")
    return db, dtm


def _seed_dem_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'running', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 0, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO dem_files (task_id, granule_id, status, retry_count) "
            "VALUES (?, 'G00.tif', 'pending', 0)",
            (task_id,),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


class _Sock:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload))


def test_speed_is_pushed_while_the_granule_is_still_downloading(monkeypatch, tmp_path):
    """颗粒下完之前就必须推出带非零速度的 task_progress。

    这正是现场看不到速度的根因：过去只有状态回调触发 emit，一颗 30-50MB 的
    COG 中间几分钟一发都没有，前端 5s 后就把行上的速度显示成 0 B/s。

    **时钟必须注进去，不能用真 time.monotonic()。** 三个回调在这里是背靠背跑的，
    中间没有真实 I/O；而 Windows 上 `time.monotonic()` 的分辨率约 15.6 ms
    （GetTickCount64），三个样本很可能落在同一个 tick 上 → `SpeedMeter.bps()` 的
    `dt <= 0` 分支返回 0.0 → 断言红。Linux/macOS 是纳秒级所以一直是绿的。
    v0.2.12 的发版构建被这条打断过一次（同一提交的前一轮 Windows 上它还是绿的
    —— 典型的靠时钟分辨率碰运气）。`SpeedMeter(clock=...)` 这个参数本来就是为
    可测性留的，用它把 dt 钉成确定值，产品代码一个字不用改：真实下载的跨度
    远大于 15.6 ms，这不是产品缺陷。
    """
    db, dtm = _setup(monkeypatch, tmp_path)
    # 节流窗口不是本用例的被测对象：不关掉的话第一发（downloading）之后的
    # 在途推送会被 1s 窗口吞掉，用例只能靠 sleep 等，纯属 flaky 来源。
    monkeypatch.setattr(dtm, "_PROGRESS_EMIT_MIN_INTERVAL", 0.0)

    # 每次读表都前进 10 ms —— 足够让窗口跨度恒为正，又远小于 SpeedMeter 默认的
    # 3 秒窗口，不会把样本挤出窗外。
    ticks = itertools.count(0.0, 0.010)
    real_meter_cls = dtm.SpeedMeter
    monkeypatch.setattr(
        dtm, "SpeedMeter",
        lambda *a, **k: real_meter_cls(*a, clock=lambda: next(ticks), **k))

    sock = _Sock()
    mgr = dtm.DemTaskManager(socketio=sock)
    task_id = _seed_dem_task(db, tmp_path / "out")

    async def fake_download_files(dataset, granules, output_dir, progress_callback,
                                  stop_flag, bytes_callback=None):
        g = granules[0]
        await progress_callback(g, "downloading", None, None)
        await bytes_callback(g, 4 * 1024 * 1024)
        await progress_callback(g, "completed", None, 8 * 1024 * 1024)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    progress_events = [p for e, p in sock.events if e == "task_progress"]
    in_flight = [p for p in progress_events if p["downloaded_files"] == 0]
    assert in_flight, f"颗粒完成前必须至少推一发进度: {[e for e, _ in sock.events]}"
    assert in_flight[-1]["download_speed_bps"] > 0, (
        f"在途推送必须带非零速度: {in_flight[-1]}")


def test_completed_size_bytes_is_never_counted_as_network_bytes(monkeypatch, tmp_path):
    """状态回调只推时间窗（record(0)），字节一律来自 bytes_callback。

    size_bytes 是双重用途的（还要写进 dem_files.size_bytes 列），缓存命中 /
    文件已存在时引擎照样上报真实大小；把它当网络字节会让速度虚高一个数量级，
    在途字节已经记过一遍时更是直接翻倍。
    """
    db, dtm = _setup(monkeypatch, tmp_path)

    class _RecordingMeter:
        def __init__(self, *a, **k):
            self.records = []

        def record(self, n_bytes):
            self.records.append(n_bytes)

        def bps(self):
            return 1.0

    meter = _RecordingMeter()
    monkeypatch.setattr(dtm, "SpeedMeter", lambda *a, **k: meter)

    mgr = dtm.DemTaskManager(socketio=_Sock())
    task_id = _seed_dem_task(db, tmp_path / "out")

    async def fake_download_files(dataset, granules, output_dir, progress_callback,
                                  stop_flag, bytes_callback=None):
        g = granules[0]
        await progress_callback(g, "downloading", None, None)
        await bytes_callback(g, 1024)
        await progress_callback(g, "completed", None, 999999)

    mgr.engine.download_files = fake_download_files
    asyncio.run(mgr._execute(task_id))

    assert meter.records == [0, 1024, 0], (
        f"只有在途字节能进吞吐计，状态回调一律 record(0)，实际: {meter.records}")
