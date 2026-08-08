import asyncio
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


def _reload_with_isolated_db(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in (
        "src.core.database",
        "src.services.task_manager",
        "src.services.dem_task_manager",
        "app",
    ):
        sys.modules.pop(mod, None)

    db = importlib.import_module("src.core.database")
    db.init_database()
    return db


def _seed_map_task(
    db,
    status="running",
    tile_statuses=("pending",),
    failed_tiles=0,
    output_format="tiles_only",
    output_path="/tmp",
    tile_zooms=None,
    zoom_min=0,
    zoom_max=0,
):
    """播种一个地图任务。

    task_tiles 已是稀疏失败表,播种按新语义造「完成态/失败态」:
    - 'completed' → 写磁盘 cache 文件(完成态由 cache 推导,不再有表里的行)
    - 'failed'    → 插一行 task_tiles 失败行
    - 'pending'   → 什么都不做(未下载 = 既无 cache 也无失败行)

    状态按 zoom 整层铺(拼接按 zoom 逐个跑,「部分 zoom 失败」需要多 zoom)。
    bbox 固定 (1,0,1,0),瓦片集合由 iter_tiles 枚举,与运行时同口径;
    zoom_min/zoom_max 决定枚举范围(默认 0-0,整个 bbox 只有 1 块瓦片)。
    """
    from src.services.download_engine import DownloadEngine

    zooms = list(tile_zooms) if tile_zooms is not None else [zoom_min] * len(tile_statuses)
    assert len(zooms) == len(tile_statuses)
    status_by_zoom = dict(zip(zooms, tile_statuses))

    engine = DownloadEngine()
    tiles = list(engine.iter_tiles(1, 0, 1, 0, zoom_min, zoom_max))

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles)
            VALUES ('map-task', ?, 1, 0, 1, 0, ?, ?, 'satellite', ?,
                    ?, ?, 0, ?)
            """,
            (status, zoom_min, zoom_max, output_format, output_path, len(tiles), failed_tiles),
        )
        task_id = cur.lastrowid
        for tile in tiles:
            tile_status = status_by_zoom.get(tile.zoom, "pending")
            if tile_status == "completed":
                # satellite → style code 's'(与 TaskManager.STYLE_MAP 一致)
                cache_path = tile.cache_path("s")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(b"not-really-a-png")
            elif tile_status == "failed":
                cur.execute(
                    """
                    INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count)
                    VALUES (?, ?, ?, ?, 'failed', 0)
                    """,
                    (task_id, tile.zoom, tile.x, tile.y),
                )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_dem_task(db, status="running", file_statuses=("pending",), failed_files=0):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-task', ?, 1, 0, 1, 0, 'ASTGTM.003', ?, ?, 0, ?)
            """,
            (status, tempfile.gettempdir(), len(file_statuses), failed_files),
        )
        task_id = cur.lastrowid
        for idx, file_status in enumerate(file_statuses):
            cur.execute(
                """
                INSERT INTO dem_files (task_id, granule_id, status, retry_count)
                VALUES (?, ?, ?, 0)
                """,
                (task_id, f"ASTGTMV003_N00E00{idx}_dem.tif", file_status),
            )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_contour_task(db, status="running", file_statuses=("pending",)):
    """一条正在下载 DEM 的等高线任务（water=0，避开 best-effort 的 ASTWBD 支线）。"""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks
              (name, status, north, south, east, west, dataset, contour_interval,
               water, zoom_min, zoom_max, output_path, total_files)
            VALUES ('contour-task', ?, 1, 0, 1, 0, 'ASTGTM.003', 10, 0, 10, 12, ?, ?)
            """,
            (status, tempfile.gettempdir(), len(file_statuses)),
        )
        task_id = cur.lastrowid
        for idx, file_status in enumerate(file_statuses):
            cur.execute(
                """
                INSERT INTO contour_files (task_id, granule_id, kind, status)
                VALUES (?, ?, 'dem', ?)
                """,
                (task_id, f"ASTGTMV003_N00E00{idx}_dem.tif", file_status),
            )
        conn.commit()
        return task_id
    finally:
        conn.close()


def _contour_task_row(db, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM contour_tasks WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _map_task_row(db, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _dem_task_row(db, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_tasks WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


def test_map_tile_failure_marks_parent_failed_not_completed(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        await progress_callback(tiles[0], "failed", "boom")
        return [{"tile": tiles[0], "status": "failed", "error": "boom"}]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["status"] == "failed"
    assert row["failed_tiles"] == 1
    assert not any(event == "task_completed" for event, _ in tm.socketio.events)


def test_dem_file_failure_marks_parent_failed_not_completed(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db)

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
        await progress_callback(granules[0], "failed", "boom", None)

    dtm.engine.download_files = fake_download_files

    try:
        asyncio.run(dtm._execute(task_id))
    except Exception:
        pass

    row = _dem_task_row(db, task_id)
    assert row["status"] == "failed"
    assert row["failed_files"] == 1
    assert not any(event == "task_completed" for event, _ in dtm.socketio.events)


def test_map_paused_task_is_not_overwritten_by_failure(monkeypatch, tmp_path):
    """暂停在收尾兜底之前落库，兜底的 UPDATE 不得把它抢成 failed。

    「暂停」是用户「等会儿接着下」的明确意图，被改写成 failed 就再也 resume
    不回来了 —— 这是失败兜底排除列表里 'paused' 那一项的唯一守卫。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        tm.pause_task(task_id)
        raise RuntimeError("network died after pause")

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["status"] == "paused"


def test_contour_paused_task_is_not_overwritten_by_failure(monkeypatch, tmp_path):
    """等高线侧同款：见 test_map_paused_task_is_not_overwritten_by_failure。"""
    from conftest import fresh_import

    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    # contour manager 走 fresh_import 而不是本文件的裸 pop 表：它在别的测试文件
    # 里是模块级 import，裸 pop 不还原会留下双实例（见 conftest 的说明与
    # test_conftest_isolation_contract）。
    ctm_mod = fresh_import(monkeypatch, "src.services.contour_task_manager")
    ctm = ctm_mod.ContourTaskManager(socketio=FakeSocketIO())
    task_id = _seed_contour_task(db)

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
        ctm.pause_task(task_id)
        raise RuntimeError("network died after pause")

    ctm.engine.download_files = fake_download_files

    try:
        asyncio.run(ctm._execute(task_id))
    except RuntimeError:
        pass

    row = _contour_task_row(db, task_id)
    assert row["status"] == "paused"


def test_dem_paused_task_is_not_overwritten_by_failure(monkeypatch, tmp_path):
    """DEM 侧同款：见 test_map_paused_task_is_not_overwritten_by_failure。"""
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db)

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
        dtm.pause_task(task_id)
        raise RuntimeError("network died after pause")

    dtm.engine.download_files = fake_download_files

    try:
        asyncio.run(dtm._execute(task_id))
    except RuntimeError:
        pass

    row = _dem_task_row(db, task_id)
    assert row["status"] == "paused"


def test_map_progress_counts_status_transitions(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    task_id = _seed_map_task(db, tile_statuses=("failed",), failed_tiles=1)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        await progress_callback(tiles[0], "completed", None)
        await progress_callback(tiles[0], "completed", None)
        return [{"tile": tiles[0], "status": "completed"}]

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["downloaded_tiles"] == 1
    assert row["failed_tiles"] == 0


def test_dem_progress_counts_status_transitions(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    task_id = _seed_dem_task(db, file_statuses=("failed",), failed_files=1)
    dtm_mod = importlib.import_module("src.services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag, bytes_callback=None):
        await progress_callback(granules[0], "completed", None, 10)
        await progress_callback(granules[0], "completed", None, 10)

    dtm.engine.download_files = fake_download_files

    asyncio.run(dtm._execute(task_id))

    row = _dem_task_row(db, task_id)
    assert row["downloaded_files"] == 1
    assert row["failed_files"] == 0


# ---------------------------------------------------------------------------
# 拼接结果必须影响任务最终状态
#
# 缺陷：拼接的 except 只打一条 log 就继续,而任务最终状态只看失败瓦片数
# （现在是 task_tiles 稀疏失败表,重构前是全量行的 failed/pending 计数）——
# 与拼接成功与否毫无关系。于是一整个 zoom 的拼接图一张
# 都没产出,前端仍然收到 task_completed,用户只能自己去翻输出目录才知道。
#
# 本分支往这条路径新加了 gdal.Warp(PROJ 查表 / 内存 / 磁盘满 / 并发删中间文件
# 都会让它失败),失败恰好落在这个唯一静默吞异常的 except 里。
# ---------------------------------------------------------------------------


def test_map_all_stitch_failures_mark_task_failed(monkeypatch, tmp_path):
    """所有 zoom 的拼接都失败 → 任务必须报 failed,不能报 completed。

    output_format=image_only 时拼接图是选这个格式的意义所在(瓦片对所有格式
    都会复制),一张拼接图都没有还说"完成"就是纯粹的谎报。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(10, 11),
        zoom_min=10,
        zoom_max=11,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    def exploding_stitch(tiles, style, output_path, zoom_level, **_):
        raise RuntimeError("gdal.Warp failed: PROJ database missing")

    tm.download_engine.stitch_tiles_with_gdal = exploding_stitch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["status"] == "failed", "拼接全失败的任务不能报完成"
    assert "拼接全部失败" in row["error_message"]
    # 根因必须落到 error_message 里,否则用户看到的只是"失败"两个字
    assert "PROJ database missing" in row["error_message"]

    events = tm.socketio.events
    assert not any(name == "task_completed" for name, _ in events)
    assert [p["zoom_level"] for name, p in events if name == "task_stitch_failed"] == [10, 11], (
        "每个失败的 zoom 都应该实时 emit 一次 task_stitch_failed"
    )


def test_map_partial_stitch_failure_completes_with_warning(monkeypatch, tmp_path):
    """部分 zoom 拼接失败 → 任务仍然完成(保住成功的那几个),但必须带明确警告。

    刻意**不**把部分失败判成整个任务失败:成功的 zoom 是用户真正要的产出。
    代价是"completed"这个状态本身不再自证清白,所以警告必须同时落在
    tasks.error_message(持久,能在任务列表/历史里看到)和 task_completed 事件上。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(10, 11),
        zoom_min=10,
        zoom_max=11,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    def half_broken_stitch(tiles, style, output_path, zoom_level, **_):
        if zoom_level == 11:
            raise RuntimeError("boom at zoom 11")
        return output_path

    tm.download_engine.stitch_tiles_with_gdal = half_broken_stitch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["status"] == "completed", "成功的 zoom 应该保留,不该把整个任务判死"
    assert "部分缩放级别拼接失败" in row["error_message"]
    assert "1/2" in row["error_message"], "警告里要写清几个 zoom 里坏了几个"
    assert "boom at zoom 11" in row["error_message"]

    completed = [p for name, p in tm.socketio.events if name == "task_completed"]
    assert completed, "部分失败仍然要 emit task_completed"
    assert completed[0]["warning"] == row["error_message"], (
        "task_completed 必须带上警告,否则前端只能靠再拉一次接口才知道"
    )


def test_map_clean_stitch_leaves_no_error_message(monkeypatch, tmp_path):
    """回归护栏:拼接全成功时 error_message 必须仍是空的。

    上面两条测试往 completed 分支引入了 error_message 写入,这条确保正常
    任务不会被顺带打上一条假警告。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(10, 11),
        zoom_min=10,
        zoom_max=11,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    stitched = []

    def ok_stitch(tiles, style, output_path, zoom_level, **_):
        stitched.append(zoom_level)
        return output_path

    tm.download_engine.stitch_tiles_with_gdal = ok_stitch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert stitched == [10, 11]
    assert row["status"] == "completed"
    assert row["error_message"] is None
    completed = [p for name, p in tm.socketio.events if name == "task_completed"]
    assert completed and completed[0]["warning"] is None


def test_map_stitch_emits_start_event_before_each_zoom(monkeypatch, tmp_path):
    """拼接开始必须发 task_stitch_progress(phase='start'),且在拼接函数之前。

    旧的完成事件只在拼完才发:单个大 zoom 一拼几十分钟起步,期间界面零
    反馈,任务行停在「已下载 N/N」——「卡 100%」的直接成因。断言顺序:
    每个 zoom 的 start 事件必须出现在该 zoom 的拼接调用之前。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(10, 11),
        zoom_min=10,
        zoom_max=11,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    stitched_order_ok = []

    def tracking_stitch(tiles, style, output_path, zoom_level, **_):
        # 拼接函数被调用的瞬间,该 zoom 的 phase='start' 事件必须已经发出
        already = any(
            name == "task_stitch_progress"
            and p.get("phase") == "start"
            and p.get("zoom_level") == zoom_level
            for name, p in tm.socketio.events
        )
        stitched_order_ok.append((zoom_level, already))
        return output_path

    tm.download_engine.stitch_tiles_with_gdal = tracking_stitch

    asyncio.run(tm._execute_task(task_id))

    starts = [
        p["zoom_level"] for name, p in tm.socketio.events
        if name == "task_stitch_progress" and p.get("phase") == "start"
    ]
    assert starts == [10, 11], "每个 zoom 拼接前都要发一次 phase='start'"
    assert stitched_order_ok == [(10, True), (11, True)], (
        "phase='start' 必须在该 zoom 的拼接调用之前发出,否则大单层拼接期间"
        "界面仍然零反馈"
    )


# ---------------------------------------------------------------------------
# 边下边复制:下载回调即时复制 + cache 命中补拷线程,结尾阶段退化为对账
# ---------------------------------------------------------------------------


def test_stream_copy_writes_output_before_stitch(monkeypatch, tmp_path):
    """下载成功的瓦片必须在拼接开始前就已出现在产物目录(不等结尾复制阶段)。

    拼接在流程上先于结尾复制阶段,拼接函数里能看到的产物 = 下载阶段写入的。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("pending",),
        tile_zooms=(10,),
        zoom_min=10,
        zoom_max=10,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    async def fake_batch(tiles, style, progress_callback, stop_flag):
        for tile in tiles:
            p = tile.cache_path("s")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"png-bytes")
            await progress_callback(tile, "completed", None)

    tm.download_engine.download_tiles_batch = fake_batch

    from src.services.download_engine import DownloadEngine
    engine = DownloadEngine()
    expected = list(engine.iter_tiles(1, 0, 1, 0, 10, 10))
    assert expected, "测试前提:z10 1°x1° 至少 1 块瓦片"

    def checking_stitch(tiles, style, output_path, zoom_level, **_):
        out_dir = tmp_path / "out" / f"task_{task_id}"
        for tile in expected:
            dest = out_dir / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
            assert dest.exists() and dest.stat().st_size > 0, (
                f"拼接开始前产物目录缺 {dest} —— 下载回调没做即时复制"
            )
        return output_path

    tm.download_engine.stitch_tiles_with_gdal = checking_stitch

    asyncio.run(tm._execute_task(task_id))
    assert _map_task_row(db, task_id)["status"] == "completed"


def test_cache_hit_backfill_writes_output_before_stitch(monkeypatch, tmp_path):
    """cache 命中(本次零下载)的瓦片也由补拷线程在拼接开始前复制到产物目录。

    全部命中时 tiles 待下载清单为空,不经下载回调 —— 没有补拷线程的话
    这些瓦片只能等结尾复制阶段,续跑任务仍要在 100% 后等一次全量复制。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(10, 11),
        zoom_min=10,
        zoom_max=11,
        output_format="image_only",
        output_path=str(tmp_path / "out"),
    )

    from src.services.download_engine import DownloadEngine
    engine = DownloadEngine()
    expected = list(engine.iter_tiles(1, 0, 1, 0, 10, 11))
    assert expected

    def checking_stitch(tiles, style, output_path, zoom_level, **_):
        out_dir = tmp_path / "out" / f"task_{task_id}"
        for tile in expected:
            dest = out_dir / str(tile.zoom) / str(tile.x) / f"{tile.y}.png"
            assert dest.exists() and dest.stat().st_size > 0, (
                f"拼接开始前产物目录缺 {dest} —— cache 命中瓦片没有开案补拷"
            )
        return output_path

    tm.download_engine.stitch_tiles_with_gdal = checking_stitch

    asyncio.run(tm._execute_task(task_id))
    assert _map_task_row(db, task_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# 复制瓦片阶段（所有格式都会复制）必须响应停止标志,并给出进度
# ---------------------------------------------------------------------------


def test_map_tile_copy_stage_honours_stop_flag(monkeypatch, tmp_path):
    """复制循环里必须检查 stop_flag。

    both 是下拉框的默认项,升级后它多跑一整个复制阶段。10 万瓦片下没有这个
    检查,用户点暂停/删除要等整个复制跑完才生效。
    """
    import threading

    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())

    # zoom 10 的 1°x1° bbox 有 12 块瓦片(cache 文件由播种写妥)——
    # 多于 2 块才能验证「取消后不再复制下一块」。
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed",),
        tile_zooms=(10,),
        zoom_min=10,
        zoom_max=10,
        output_format="tiles_only",
        output_path=str(tmp_path / "out"),
    )

    # 复制循环查的是登记表里那一份 flag（见 _is_stop_requested）
    tm.stop_flags[task_id] = threading.Event()

    copied = []
    real_copy2 = tm_mod.shutil.copy2

    def stopping_copy2(src, dst):
        real_copy2(src, dst)
        copied.append(str(src))
        # 用户在复制过程中按了暂停/删除 —— 两条路都只是置这个标志
        tm.stop_flags[task_id].set()

    monkeypatch.setattr(tm_mod.shutil, "copy2", stopping_copy2)

    asyncio.run(tm._execute_task(task_id))

    assert len(copied) == 1, (
        f"置位停止标志后又复制了 {len(copied)} 个瓦片 —— 复制循环没检查 "
        "stop_flag,停止要等整个复制跑完才生效"
    )


def test_map_tile_copy_stage_emits_progress(monkeypatch, tmp_path):
    """复制阶段必须有进度事件,否则下载走完 100% 之后 UI 静止若干分钟像卡死。

    间隔取 COPY_PROGRESS_INTERVAL,末尾必须补一发(否则不满一个间隔的任务
    一个事件都没有)。
    """
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())

    # zoom 12-13 的 1°x1° bbox 有几百块瓦片(足够跨过一个 COPY_PROGRESS_INTERVAL
    # 批次);cache 文件由播种按枚举结果写妥。
    task_id = _seed_map_task(
        db,
        tile_statuses=("completed", "completed"),
        tile_zooms=(12, 13),
        zoom_min=12,
        zoom_max=13,
        output_format="tiles_only",
        output_path=str(tmp_path / "out"),
    )
    total = _map_task_row(db, task_id)["total_tiles"]
    assert total > tm_mod.COPY_PROGRESS_INTERVAL, (
        "瓦片数必须超过一个进度批次,才能验证「每间隔报一次」"
    )

    asyncio.run(tm._execute_task(task_id))

    # 每 COPY_PROGRESS_INTERVAL 块报一次,最后不满一批也必须补一发
    expected = list(range(tm_mod.COPY_PROGRESS_INTERVAL, total + 1, tm_mod.COPY_PROGRESS_INTERVAL))
    if total % tm_mod.COPY_PROGRESS_INTERVAL:
        expected.append(total)

    progress = [p for name, p in tm.socketio.events if name == "task_copy_progress"]
    assert [p["processed_tiles"] for p in progress] == expected, (
        "应在每 COPY_PROGRESS_INTERVAL 个瓦片以及最后一个瓦片时各报一次"
    )
    assert progress[-1]["copied_tiles"] == total
    assert progress[-1]["total_tiles"] == total
