import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


def _reload_with_isolated_db(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in (
        "database",
        "models.task",
        "services.task_manager",
        "services.dem_task_manager",
        "app",
    ):
        sys.modules.pop(mod, None)

    db = importlib.import_module("database")
    db.init_database()
    return db


def _seed_map_task(db, status="running", tile_statuses=("pending",), failed_tiles=0):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles)
            VALUES ('map-task', ?, 1, 0, 1, 0, 0, 0, 'satellite', 'tiles_only',
                    ?, ?, 0, ?)
            """,
            (status, "/tmp", len(tile_statuses), failed_tiles),
        )
        task_id = cur.lastrowid
        for idx, tile_status in enumerate(tile_statuses):
            cur.execute(
                """
                INSERT INTO task_tiles (task_id, zoom, x, y, status, retry_count)
                VALUES (?, 0, ?, 0, ?, 0)
                """,
                (task_id, idx, tile_status),
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
            (status, "/tmp", len(file_statuses), failed_files),
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
    tm_mod = importlib.import_module("services.task_manager")
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
    dtm_mod = importlib.import_module("services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db)

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
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


def test_map_cancelled_task_is_not_overwritten_by_failure(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("services.task_manager")
    tm = tm_mod.TaskManager(socketio=FakeSocketIO())
    task_id = _seed_map_task(db)

    async def fake_download_tiles_batch(tiles, style, progress_callback, stop_flag=None):
        tm.cancel_task(task_id)
        raise RuntimeError("network died after cancel")

    tm.download_engine.download_tiles_batch = fake_download_tiles_batch

    asyncio.run(tm._execute_task(task_id))

    row = _map_task_row(db, task_id)
    assert row["status"] == "cancelled"


def test_dem_cancelled_task_is_not_overwritten_by_failure(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    dtm_mod = importlib.import_module("services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())
    task_id = _seed_dem_task(db)

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        dtm.cancel_task(task_id)
        raise RuntimeError("network died after cancel")

    dtm.engine.download_files = fake_download_files

    try:
        asyncio.run(dtm._execute(task_id))
    except RuntimeError:
        pass

    row = _dem_task_row(db, task_id)
    assert row["status"] == "cancelled"


def test_map_progress_counts_status_transitions(monkeypatch, tmp_path):
    db = _reload_with_isolated_db(monkeypatch, tmp_path)
    task_id = _seed_map_task(db, tile_statuses=("failed",), failed_tiles=1)
    tm_mod = importlib.import_module("services.task_manager")
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
    dtm_mod = importlib.import_module("services.dem_task_manager")
    dtm = dtm_mod.DemTaskManager(socketio=FakeSocketIO())

    async def fake_download_files(dataset, granules, output_dir, progress_callback, stop_flag):
        await progress_callback(granules[0], "completed", None, 10)
        await progress_callback(granules[0], "completed", None, 10)

    dtm.engine.download_files = fake_download_files

    asyncio.run(dtm._execute(task_id))

    row = _dem_task_row(db, task_id)
    assert row["downloaded_files"] == 1
    assert row["failed_files"] == 0
