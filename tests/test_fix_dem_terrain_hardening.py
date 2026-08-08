"""DEM / 地形链路的一组加固（2026-08-08 全项目复查 P1#7、P1#8、P1#9 + 四条 P2）。

每个用例都钉一条「旧代码会红」的不变量：

- P1#7 逐瓦片进度落库没兜底：一次 `database is locked` 让 99% 已落盘的切片作业记 failed。
- P1#8 串行路径漏 `_WORKER_SAMPLER`：释放块挂在 `if temp_input:` 下，单输入 tif 时恒不执行。
- P1#9 URS 重定向是**子串**判断：攻击者主机能骗到 BasicAuth 明文凭据。
- P2 meta.json 可能写出裸 `Infinity`（非合法 JSON）。
- P2 停止之后还接着 graft 底图（4.3 万硬链接），失败还把用户取消记成 failed。
- P2 晕渲预览的 `.tmp.png` 在失败路径不清理。
- P2（第 8 项，可选）进程池启动方式：本用例钉住「有意保持平台默认 fork」这个决定。
"""

import concurrent.futures
import importlib
import json
import os
import sqlite3
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from osgeo import gdal  # noqa: E402

from src.services import hillshade_preview  # noqa: E402
from src.services.earthdata_client import (  # noqa: E402
    EarthdataAuthError,
    EarthdataClient,
)
from src.services.terrain_tiling import cesiumlab_terrain as ct  # noqa: E402
from src.services.terrain_tiling import dem_task_tiler as tiler  # noqa: E402


def _make_dem(path, cols=60, rows=60, deg_per_px=0.01, west=116.0, north=40.0):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((west, deg_per_px, 0.0, north, 0.0, -deg_per_px))
    ds.GetRasterBand(1).WriteArray(
        np.arange(cols * rows, dtype=np.float32).reshape(rows, cols)
    )
    ds.FlushCache()
    ds = None


# ---------------------------------------------------------------------------
# P1#7：进度落库失败不许作废切片产物
# ---------------------------------------------------------------------------


def _setup_dem_manager(monkeypatch, tmp_path):
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


def _seed_completed_dem_task(db, output_path):
    """切片只接受 completed 的下载任务（M16），所以种子行必须是 completed。"""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('t', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?, 1, 1, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


class _ProgressWriteFails:
    """只让进度那条 UPDATE 抛 database is locked，其余 SQL 原样透传。

    精确复刻现场：get_connection 是 WAL + busy_timeout=30000，真正撞锁的是
    切片期间高频的进度写，而收尾那条 status UPDATE 走的是 cursor()，不受影响。
    """

    def __init__(self, inner):
        self._inner = inner

    def execute(self, sql, *args, **kwargs):
        if "rendered_tiles" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_progress_flush_db_failure_does_not_discard_the_tiling_job(monkeypatch, tmp_path):
    """进度落库失败不能把切片作业判 failed。

    旧行为：_flush_tiling_progress 里的 execute/commit 是裸调用（只有紧随其后的
    socketio.emit 被 try 包住）。它经 progress_cb 在瓦片循环里被同步调用，异常
    穿透 ex.map → ProcessPoolExecutor → tile_dem_task_dir → _run_tiling_job 的
    catch-all，把作业记成 failed。切片没有恢复模型（_worker_tile 不跳过已存在的
    瓦片），重跑要从 z8 全量重算 —— 一次 database is locked 报废几小时的活。

    这里断言两件事，缺一都可能是空断言：作业最终是 completed（不是 failed），
    以及切片器**真的被调用过**（旧代码在 tile_dem_task_dir 之前的
    tiling_progress(0, 0) 就已经抛了，切片器一次都没进）。
    """
    db, dtm = _setup_dem_manager(monkeypatch, tmp_path)
    mgr = dtm.DemTaskManager(socketio=None)
    task_id = _seed_completed_dem_task(db, tmp_path / "out")

    real_get_connection = dtm.get_connection
    monkeypatch.setattr(
        dtm, "get_connection", lambda *a, **kw: _ProgressWriteFails(real_get_connection(*a, **kw))
    )

    tiled = []

    def fake_tiler(task_dir, out_dir, params, build_terrain_fn=None):
        # 模拟 build_terrain 在瓦片循环里同步回调进度
        for i in range(1, 4):
            params.progress_cb(i, 3)
        tiled.append(True)
        return {"total": 3, "rendered": 3, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th is not None:
        th.join(timeout=10)

    assert tiled == [True], "进度落库失败把切片器整个挡在门外了"
    job = mgr.get_tiling_job(task_id)
    assert job["status"] == "completed", (
        f"进度是展示字段，落库失败不该作废产物，实际 status={job['status']} "
        f"error={job['error_message']}")


# ---------------------------------------------------------------------------
# P1#8：串行路径必须释放 _WORKER_SAMPLER
# ---------------------------------------------------------------------------


def test_serial_build_releases_worker_sampler(tmp_path):
    """单输入 tif 走串行切片后，模块级 _WORKER_SAMPLER 必须被释放。

    旧行为：释放块整段挂在 `if temp_input:` 下，而 temp_input 只有多输入
    （build_input_raster 物化出临时文件）时才非 None —— 单输入 tif 恒为 None，
    于是三条串行路径（workers==1 / total<=4 / BrokenProcessPool 回退）都会留下
    一个打开着的 GDAL dataset + 块缓存直到进程退出。Windows 上源文件因此被占，
    remove_task_dir_if_safe 的 rmtree(ignore_errors=True) 静默失败，
    delete_task 报删除成功而上传的 tif 还在盘上。

    断言全局是 None 而不只是 ds 被清空：留着对象等于把释放时机压在 GC 上。
    """
    dem = tmp_path / "dem.tif"
    _make_dem(dem)

    ct._WORKER_SAMPLER = None
    ct.build_terrain([str(dem)], str(tmp_path / "out"),
                     min_level=0, max_level=1, workers=1)

    assert ct._WORKER_SAMPLER is None, (
        "串行路径跑完仍握着 worker sampler：源 DEM 的 GDAL 句柄泄漏到进程结束")


def test_multi_input_build_still_releases_sampler_and_removes_temp(tmp_path):
    """多输入（temp_input 非 None）的老路径不能被这次改动弄回归。

    释放挪出 `if temp_input:` 之后，临时物化文件的删除必须仍然发生 —— 顺序也
    仍然是「先放句柄再删文件」，否则 Windows 上删不掉。
    """
    a = tmp_path / "a.tif"
    b = tmp_path / "b.tif"
    _make_dem(a, west=116.0, north=40.0)
    _make_dem(b, west=116.6, north=40.0)
    out = tmp_path / "out"

    ct._WORKER_SAMPLER = None
    ct.build_terrain([str(a), str(b)], str(out), min_level=0, max_level=1, workers=1)

    assert ct._WORKER_SAMPLER is None
    # 物化产物落在输出目录的父目录旁边，不该留下来
    leftovers = [p for p in tmp_path.iterdir()
                 if p.is_file() and p.suffix == ".tif" and p not in (a, b)]
    assert leftovers == [], f"多输入物化的临时文件没删干净：{leftovers}"


# ---------------------------------------------------------------------------
# P1#9：凭据只能发给 Earthdata 域下的主机
# ---------------------------------------------------------------------------


ATTACKER_URL = "https://attacker.example/cb?next=https://urs.earthdata.nasa.gov/oauth"
FILE_URL = "https://data.lpdaac.earthdatacloud.nasa.gov/protected/file.hdf"


class _RecordingResp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    """记录每次 get 的 (url, auth)，按序吐预置响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("auth")))
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self._responses.pop(0)


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_credentials_never_sent_to_non_earthdata_host():
    """指向攻击者主机的重定向不能拿到 BasicAuth 凭据。

    旧行为：`if "urs.earthdata.nasa.gov" in loc` 是**子串**匹配，
    https://attacker.example/cb?next=https://urs.earthdata.nasa.gov/oauth
    直接过关，随后 _login_and_resolve 把明文用户名口令发给 attacker.example。

    断言的是「所有非 Earthdata 域的请求都没带 auth」，不是日志字符串：
    这条链路本来就允许对中间重定向再跟一跳（无凭据），所以「一次请求都不发」
    是错误的期望，「发了但不带凭据」才是真不变量。
    """
    session = _RecordingSession([
        _RecordingResp(302, {"Location": ATTACKER_URL}),
        # 跟一跳中间重定向（不带凭据），拿到非 303 → 报重定向链异常
        _RecordingResp(500, {}),
    ])
    client = EarthdataClient("test-user", "s3cret-password")

    with pytest.raises(RuntimeError):
        _run(client.get_signed_url(session, FILE_URL))

    from urllib.parse import urlparse
    for url, auth in session.calls:
        host = urlparse(url).hostname or ""
        if not (host == "earthdata.nasa.gov" or host.endswith(".earthdata.nasa.gov")):
            assert auth is None, f"把 BasicAuth 凭据发给了 {host}"

    assert any(urlparse(u).hostname == "attacker.example" for u, _ in session.calls), (
        "用例没走到攻击者主机那一跳，断言是空的")


def test_login_and_resolve_refuses_non_urs_authorize_url():
    """带凭据发请求的那一侧自己要有闸：非 URS 主机直接拒，且一个请求都不发。"""
    session = _RecordingSession([])
    client = EarthdataClient("test-user", "s3cret-password")

    with pytest.raises(EarthdataAuthError):
        _run(client._login_and_resolve(session=session, file_url=FILE_URL,
                                       authorize_url=ATTACKER_URL))

    assert session.calls == [], "拒绝之前就不该发出任何请求"


def test_http_urs_url_is_not_trusted():
    """http:// 的 URS 主机也不给凭据 —— BasicAuth 在明文信道上就是明文口令。"""
    session = _RecordingSession([])
    client = EarthdataClient("test-user", "s3cret-password")

    with pytest.raises(EarthdataAuthError):
        _run(client._login_and_resolve(
            session=session, file_url=FILE_URL,
            authorize_url="http://urs.earthdata.nasa.gov/oauth/authorize"))

    assert session.calls == []


def test_real_urs_redirect_still_gets_credentials():
    """真 URS 主机必须照旧走登录流程 —— 别把闸修成谁都进不来。"""
    urs = "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x"
    login_cb = "https://data.lpdaac.earthdatacloud.nasa.gov/login?code=authcode"
    signed = "https://abc123.cloudfront.net/file.hdf?Signature=signed"
    session = _RecordingSession([
        _RecordingResp(302, {"Location": urs}),
        _RecordingResp(302, {"Location": login_cb}),
        _RecordingResp(200, {}),
        _RecordingResp(303, {"Location": signed}),
    ])
    client = EarthdataClient("test-user", "s3cret-password")

    assert _run(client.get_signed_url(session, FILE_URL)) == signed
    assert session.calls[1][1] is not None, "URS authorize 那一跳必须带凭据"


# ---------------------------------------------------------------------------
# P2：meta.json 不许写出裸 Infinity
# ---------------------------------------------------------------------------


def test_meta_json_is_valid_json_when_every_tile_fails(tmp_path, monkeypatch):
    """全部瓦片失败时 meta.json 仍必须是合法 JSON。

    旧行为：h_min/h_max 的哨兵是 ±inf，只有成功瓦片才收窄；rendered==0 时
    json.dumps 按默认 allow_nan=True 写出裸 `Infinity`，任何严格解析器
    （JSON.parse / Go / Rust）读 meta.json 直接报错。

    注意 Python 的 json.loads **接受** Infinity —— 只断言「能 loads」是空断言，
    所以这里同时断言原始文本里没有 Infinity，并用 parse_constant 把它变成硬错。
    """
    dem = tmp_path / "dem.tif"
    _make_dem(dem)
    out = tmp_path / "out"

    monkeypatch.setattr(ct, "_worker_tile", lambda task: None)  # 每张瓦片都失败
    counts = ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1, workers=1)
    assert counts["total"] > 0 and counts["rendered"] == 0, "用例没造出「全失败」的前提"

    raw = (out / "meta.json").read_text(encoding="utf-8")
    assert "Infinity" not in raw, f"meta.json 写出了非法 JSON：{raw}"

    def _boom(name):
        raise AssertionError(f"meta.json 含非法 JSON 常量 {name}")

    meta = json.loads(raw, parse_constant=_boom)
    assert meta["minHeight"] is None and meta["maxHeight"] is None


def test_meta_json_keeps_real_heights_when_tiles_render(tmp_path):
    """正常路径的高度范围不能被归一化改掉（别把 None 变成默认值）。"""
    dem = tmp_path / "dem.tif"
    _make_dem(dem)
    out = tmp_path / "out"

    ct.build_terrain([str(dem)], str(out), min_level=0, max_level=1, workers=1)
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert isinstance(meta["minHeight"], float) and isinstance(meta["maxHeight"], float)
    assert meta["minHeight"] <= meta["maxHeight"]


# ---------------------------------------------------------------------------
# P2：停止之后不许再植底图
# ---------------------------------------------------------------------------


def _make_task_dir(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "A_dem.tif").write_bytes(b"")
    return task_dir


def test_stopped_tiling_skips_graft_and_merge(tmp_path, monkeypatch):
    """build_terrain 因 stop_flag 提前返回后，不许再 graft/merge。

    旧行为：stop_flag 只传给 build_terrain，它提前返回后代码照跑
    graft_base_into（4.3 万硬链接 / 518 个目录）+ merge_base_availability。
    DEM/local terrain 的唯一停止入口是**删除任务**，输出目录马上要被 rmtree ——
    用户点了删除还得干等一整轮 graft；graft 失败（磁盘满）还会把一个用户取消的
    作业记成 failed，错误文案指向随包底图，指错方向。
    """
    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"
    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    calls = []
    stop = threading.Event()

    def fake_build_terrain(**kwargs):
        calls.append("tile")
        # 模拟「切到一半被叫停」：已切的瓦片和 layer.json 照常落盘
        out = tmp_path / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 9, "available": []}', encoding="utf-8")
        stop.set()
        return {"total": 100, "rendered": 7, "failed": 0}

    monkeypatch.setattr(tiler, "ensure_base_unpacked", lambda **kw: base)
    monkeypatch.setattr(tiler, "ungraft_base_from", lambda *a: 0)
    monkeypatch.setattr(tiler, "graft_base_into",
                        lambda *a: calls.append("graft") or {"linked": 0})
    monkeypatch.setattr(tiler, "merge_base_availability",
                        lambda *a: calls.append("merge"))

    counts = tiler.tile_dem_task_dir(
        task_dir, out_dir,
        tiler.TileParams(maxzoom=9, parent_url="", stop_flag=stop),
        build_terrain_fn=fake_build_terrain)

    assert calls == ["tile"], f"停止后仍执行了 {calls[1:]}"
    assert counts["rendered"] == 7 and counts["total"] == 100, (
        "停止分支也要如实返回计数，管理器靠它区分「停了」和「什么都没切出来」")


def test_stopped_tiling_without_layer_json_is_not_an_error(tmp_path, monkeypatch):
    """停止得够早、连 layer.json 都没写出来时，不能报 FileNotFoundError。

    旧行为：layer.json 存在性校验排在停止检查之前（根本没有停止检查），
    一个用户取消的作业会以「Missing layer.json」记成 failed。
    """
    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"
    stop = threading.Event()

    def fake_build_terrain(**kwargs):
        # 停得太早：一个瓦片都没落盘，layer.json 也还没写
        stop.set()
        return {"total": 100, "rendered": 0, "failed": 0}

    monkeypatch.setattr(tiler, "ensure_base_unpacked", lambda **kw: None)

    counts = tiler.tile_dem_task_dir(
        task_dir, out_dir,
        tiler.TileParams(maxzoom=9, parent_url="https://example.com/p", stop_flag=stop),
        build_terrain_fn=fake_build_terrain)

    assert counts["total"] == 100 and counts["rendered"] == 0


def test_unstopped_tiling_still_grafts(tmp_path, monkeypatch):
    """没停止时 graft/merge 必须照旧发生 —— 别把新加的闸开成常闭。"""
    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"
    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    calls = []

    def fake_build_terrain(**kwargs):
        out = tmp_path / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 9, "available": []}', encoding="utf-8")
        return {"total": 10, "rendered": 10, "failed": 0}

    monkeypatch.setattr(tiler, "ensure_base_unpacked", lambda **kw: base)
    monkeypatch.setattr(tiler, "ungraft_base_from", lambda *a: 0)
    monkeypatch.setattr(tiler, "graft_base_into",
                        lambda *a: calls.append("graft") or {"linked": 0})
    monkeypatch.setattr(tiler, "merge_base_availability",
                        lambda *a: calls.append("merge"))

    tiler.tile_dem_task_dir(
        task_dir, out_dir,
        tiler.TileParams(maxzoom=9, parent_url="", stop_flag=threading.Event()),
        build_terrain_fn=fake_build_terrain)

    assert calls == ["graft", "merge"]


# ---------------------------------------------------------------------------
# P2：晕渲预览失败路径不留 .tmp.png
# ---------------------------------------------------------------------------


def test_hillshade_tmp_png_removed_when_replace_fails(tmp_path, monkeypatch):
    """os.replace 失败时 .tmp.png 不能留在任务产物目录里。

    旧行为：finally 只放 GDAL 句柄和 vsimem，tmp_path 完全没人管。每一次失败的
    预览请求攒一份 preview_hillshade.tmp.png，只能靠删任务清掉。
    （PNG 驱动不写 .aux.xml 边车，所以 .tmp.png 是唯一残留物。）
    """
    raster_dir = tmp_path / "raster"
    raster_dir.mkdir()
    _make_dem(raster_dir / "A_dem.tif", cols=40, rows=40)
    png_path = tmp_path / "preview_hillshade.png"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hillshade_preview.os, "replace", boom)

    with pytest.raises(OSError):
        hillshade_preview._render(raster_dir, png_path)

    leftovers = sorted(p.name for p in tmp_path.glob("*.tmp.png"))
    assert leftovers == [], f"失败路径留下了临时 PNG：{leftovers}"
    assert not png_path.exists()


def test_hillshade_tmp_png_removed_when_translate_reports_failure(tmp_path, monkeypatch):
    """gdal.Translate 返回 None（我们抛 RuntimeError）时同样不留 .tmp.png。"""
    raster_dir = tmp_path / "raster"
    raster_dir.mkdir()
    _make_dem(raster_dir / "A_dem.tif", cols=40, rows=40)
    png_path = tmp_path / "preview_hillshade.png"

    real_translate = gdal.Translate

    def half_written(dst, src, **kwargs):
        out = real_translate(dst, src, **kwargs)
        if kwargs.get("format") == "PNG":
            # 文件已经落盘了，但我们对外报失败 —— 正是残留物产生的时刻
            del out
            return None
        return out

    monkeypatch.setattr(gdal, "Translate", half_written)

    with pytest.raises(RuntimeError):
        hillshade_preview._render(raster_dir, png_path)

    leftovers = sorted(p.name for p in tmp_path.glob("*.tmp.png"))
    assert leftovers == [], f"失败路径留下了临时 PNG：{leftovers}"


def test_hillshade_success_path_still_produces_png(tmp_path):
    """成功路径不能被清理逻辑误删（replace 之后 tmp 已不存在，unlink 是 no-op）。"""
    raster_dir = tmp_path / "raster"
    raster_dir.mkdir()
    _make_dem(raster_dir / "A_dem.tif", cols=40, rows=40)
    png_path = tmp_path / "preview_hillshade.png"

    bounds = hillshade_preview._render(raster_dir, png_path)

    assert bounds is not None and png_path.is_file()
    assert list(tmp_path.glob("*.tmp.png")) == []


# ---------------------------------------------------------------------------
# 第 8 项（可选，已否决）：进程池启动方式保持平台默认
# ---------------------------------------------------------------------------


class _KwargRecordingPool:
    last_kwargs = None

    def __init__(self, **kwargs):
        _KwargRecordingPool.last_kwargs = kwargs
        self.initializer = kwargs.get("initializer")
        self.initargs = kwargs.get("initargs", ())

    def __enter__(self):
        if self.initializer is not None:
            self.initializer(*self.initargs)
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, iterable, chunksize=1):
        return map(fn, iterable)


def test_process_pool_uses_the_spawn_start_method(tmp_path, monkeypatch):
    """启动方式必须是显式 spawn，不能回落到平台默认。

    Linux 的默认是 fork，而父进程是多线程的 Flask —— CPython 3.12 起就为此打
    DeprecationWarning（子进程只继承调用线程，父进程里被别的线程持有的锁在子
    进程里永远解不开）。Windows/macOS 的打包产物本来就走 spawn，所以 worker 早
    已必须是 spawn-safe；Linux 走 fork 只是让三平台行为不一致，而 Python 3.14
    会把 Linux 默认改成 forkserver。contour_engine 的池也是 spawn，两处不分叉。

    这条断言的前一版钉的是**相反**的决定（「保持平台默认、不传 mp_context」），
    理由是替身 _InProcessPool 的签名里没有 mp_context 会 TypeError。那不是一条
    需要守住的不变量 —— 替身的签名要跟着真 API 走，改替身即可。
    """
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _KwargRecordingPool)
    monkeypatch.setattr(ct, "_worker_tile", lambda task: (0.0, 1.0, "martini"))
    _KwargRecordingPool.last_kwargs = None

    dem = tmp_path / "dem.tif"
    _make_dem(dem, cols=60, rows=60, deg_per_px=1.0, west=100.0, north=40.0)
    ct.build_terrain([str(dem)], str(tmp_path / "tiles"),
                     min_level=0, max_level=4, workers=2)

    assert _KwargRecordingPool.last_kwargs is not None, "并行分支没跑到，断言是空的"
    ctx = _KwargRecordingPool.last_kwargs.get("mp_context")
    assert ctx is not None, "没传 mp_context —— 会回落到平台默认（Linux = fork）"
    assert ctx.get_start_method() == "spawn", (
        f"启动方式是 {ctx.get_start_method()}，应为 spawn")
