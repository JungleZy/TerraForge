import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _reload(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图缓存自 user_version=3 起落在 BASE_DIR/assets/terrain/base_z8；
    # 不 patch BASE_DIR 的话可用性闸门会去看真实仓库根，本机有没有解压过底图
    # 就成了测试结果的一部分。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    for mod in ("src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    mgr_mod = importlib.import_module("src.services.local_terrain_task_manager")
    return db, mgr_mod


def _make_base_terrain(tmp_path):
    """造一份最小 base_z8 —— 闸门要求 base 真的存在才写 parentUrl。

    2026-08-05：base 不可达时 manager 会返回 None 而不是写一个 404 的 URL
    （见 layer_json.parent_url_if_base_available）。这些用例要测的是「配置值
    被正确使用」，所以先把前提摆上；「base 不存在」那条另有专门用例。
    """
    base = tmp_path / "assets" / "terrain" / "base_z8"
    base.mkdir(parents=True, exist_ok=True)
    (base / "layer.json").write_text('{"tilejson":"1.0"}', encoding="utf-8")
    return base


def test_create_task_saves_files_and_rows(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    # Don't actually tile in this task's tests.
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    files = [
        ("a.tif", b"fake-tif-bytes-a"),
        ("b.tiff", b"fake-tif-bytes-b"),
    ]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=12)

    task = mgr.get_task(task_id)
    assert task["name"] == "local-1"
    assert task["total_files"] == 2
    assert task["uploaded_files"] == 2
    assert task["failed_files"] == 0
    assert task["maxzoom"] == 12

    # Files saved under source/ as *_dem.tif
    from pathlib import Path
    source_dir = Path(task["source_dir"])
    saved = sorted(p.name for p in source_dir.glob("*_dem.tif"))
    assert saved == ["upload_1_dem.tif", "upload_2_dem.tif"]
    assert (source_dir / "upload_1_dem.tif").read_bytes() == b"fake-tif-bytes-a"

    rows = mgr.list_files(task_id)
    assert len(rows) == 2
    assert {r["original_filename"] for r in rows} == {"a.tif", "b.tiff"}
    assert all(r["status"] == "uploaded" for r in rows)


def test_create_task_rejects_non_tif(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="bad", files=[("x.png", b"data")], maxzoom=12)


def test_create_task_rejects_empty_file_list(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="empty", files=[], maxzoom=12)


def test_create_task_rejects_zero_byte_file(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="zero", files=[("a.tif", b"")], maxzoom=12)


def test_start_tiling_invokes_build_terrain(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    # Capture the tiler call by patching tile_dem_task_dir in the manager module.
    def fake_tile(task_dir, out_dir, params):
        calls["task_dir"] = task_dir
        calls["out_dir"] = out_dir
        calls["maxzoom"] = params.maxzoom
        # Simulate tiler output so completion logic can proceed.
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    files = [("a.tif", b"fake-tif-bytes-a")]
    task_id = mgr.create_task_with_files(name="local-1", files=files, maxzoom=11)

    # start_tiling spawns a thread; wait for it.
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "completed"
    assert calls["maxzoom"] == 11
    assert str(calls["task_dir"]).endswith(os.path.join(f"local_task_{task_id}", "source"))
    assert str(calls["out_dir"]).endswith(os.path.join(f"local_task_{task_id}", "terrain_tiles"))


def test_start_tiling_marks_failed_on_error(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    def boom(task_dir, out_dir, params):
        raise RuntimeError("tiler exploded")

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", boom)

    task_id = mgr.create_task_with_files(name="local-1", files=[("a.tif", b"x")], maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    task = mgr.get_task(task_id)
    assert task["status"] == "failed"
    assert "tiler exploded" in (task["error_message"] or "")


import io


def _load_app(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_http_wiring_list_does_not_500(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/terrain/local/tasks")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_http_upload_creates_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)

    # Don't run real tiler.
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    data = {
        "name": "http-local",
        "maxzoom": "10",
        "files": [
            (io.BytesIO(b"fake-a"), "a.tif"),
            (io.BytesIO(b"fake-b"), "b.tiff"),
        ],
    }
    resp = client.post(
        "/api/terrain/local/tasks",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    task_id = body["task_id"]

    detail = client.get(f"/api/terrain/local/tasks/{task_id}")
    assert detail.status_code == 200
    dbody = detail.get_json()
    assert dbody["task"]["total_files"] == 2
    assert dbody["layer_url"].endswith(f"/terrain/local/{task_id}/layer.json")
    assert len(dbody["files"]) == 2


def test_http_upload_no_valid_files_returns_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "bad", "files": [(io.BytesIO(b"x"), "x.png")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_history_all_includes_local_terrain(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    # Seed one completed local terrain task directly.
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, total_files, uploaded_files, maxzoom)
            VALUES ('hist-local', 'completed', '/tmp/x', '/tmp/x/source', '/tmp/x/terrain_tiles', 2, 2, 12)
            """
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/api/history_all")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    local = [t for t in body["tasks"] if t["task_type"] == "local_terrain"]
    assert len(local) == 1
    t = local[0]
    assert t["name"] == "hist-local"
    # bbox columns are NULL for local terrain
    assert t["north"] is None and t["south"] is None
    assert t["total"] == 2 and t["downloaded"] == 2


def test_upload_rejects_too_many_files(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )
    files = [(io.BytesIO(b"x"), f"f{i}.tif") for i in range(101)]
    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "many", "files": files},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "Too many files" in resp.get_json()["error"]


def test_upload_rejects_bad_ext_before_read(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )
    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "bad", "files": [(io.BytesIO(b"x"), "a.tif"), (io.BytesIO(b"y"), "b.png")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.get_json()["error"]


def test_max_content_length_configured(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    assert app_mod.app.config.get("MAX_CONTENT_LENGTH") is not None
    assert app_mod.app.config["MAX_CONTENT_LENGTH"] > 0


def test_delete_task_removes_row_and_dir(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="del", files=[("a.tif", b"data")], maxzoom=12)
    from pathlib import Path
    task_root = Path(mgr.get_task(task_id)["output_path"])
    assert task_root.exists()

    mgr.delete_task(task_id)

    assert not task_root.exists()
    import pytest
    with pytest.raises(ValueError):
        mgr.get_task(task_id)


def test_delete_running_task_still_deletes(monkeypatch, tmp_path):
    """DB 状态是 running 但没有活线程（进程重启后的孤儿行）—— 同步删掉。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="del2", files=[("a.tif", b"data")], maxzoom=12)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE local_terrain_tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    outcome = mgr.delete_task(task_id)

    assert outcome.row_deleted is True
    import pytest
    with pytest.raises(ValueError):
        mgr.get_task(task_id)


def test_delete_with_active_thread_stops_it_and_drops_row(monkeypatch, tmp_path):
    """active_tasks 里有存活的切片线程 —— 行当场消失，且停止标志被置上。

    停止标志是「运行中删除」区别于快路径的唯一同步可观察点：产物清理挪到了
    后台线程，行删除两条路径都做。
    """
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="del3", files=[("a.tif", b"data")], maxzoom=12)
    assert mgr.get_task(task_id)["status"] == "pending"

    import threading
    gate = threading.Event()
    th = threading.Thread(target=lambda: gate.wait(timeout=30), daemon=True)
    th.start()
    stop_flag = threading.Event()
    mgr.active_tasks[task_id] = th
    mgr.stop_flags[task_id] = stop_flag
    try:
        outcome = mgr.delete_task(task_id)

        assert outcome.row_deleted is True
        assert stop_flag.is_set(), "运行中删除必须让切片线程停下来"
        import pytest
        with pytest.raises(ValueError):
            mgr.get_task(task_id)
    finally:
        gate.set()
        th.join(timeout=5)


def test_delete_task_delete_files_false_keeps_dir(monkeypatch, tmp_path):
    """delete_files=False must remove the DB row but keep the on-disk dir."""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="keep", files=[("a.tif", b"data")], maxzoom=12)
    from pathlib import Path
    task_root = Path(mgr.get_task(task_id)["output_path"])
    assert task_root.exists()

    mgr.delete_task(task_id, delete_files=False)

    assert task_root.exists()
    import pytest
    with pytest.raises(ValueError):
        mgr.get_task(task_id)


def test_http_delete_delete_files_param(monkeypatch, tmp_path):
    """DELETE .../tasks/<id>?delete_files=false keeps files, =true removes them."""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    from pathlib import Path

    created = []
    for i in range(2):
        resp = client.post(
            "/api/terrain/local/tasks",
            data={"name": f"df-{i}", "maxzoom": "10",
                  "files": [(io.BytesIO(b"fake"), "a.tif")]},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 201
        created.append(resp.get_json()["task_id"])

    dirs = {
        tid: Path(app_mod.local_terrain_task_manager.get_task(tid)["output_path"])
        for tid in created
    }
    assert all(d.exists() for d in dirs.values())

    r1 = client.delete(f"/api/terrain/local/tasks/{created[0]}?delete_files=false")
    assert r1.status_code == 200
    assert dirs[created[0]].exists()

    r2 = client.delete(f"/api/terrain/local/tasks/{created[1]}?delete_files=true")
    assert r2.status_code == 200
    assert not dirs[created[1]].exists()


def test_http_delete_missing_task_returns_404(monkeypatch, tmp_path):
    """行不存在 → 404。此前 manager 抛 ValueError、路由一律回 400；共享助手不为
    「行不存在」抛异常，路由这里主动对齐另外三条管线的 404。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)

    resp = client.delete("/api/terrain/local/tasks/99999")

    assert resp.status_code == 404, resp.get_json()


def test_http_delete_missing_task_keeps_same_named_dir(monkeypatch, tmp_path):
    """删不存在的 id 必须一片磁盘都不碰 —— 否则就是「404 + 静默真删」。

    只有本地地形的 artifact_dir 在 manager 内部按 task_id 硬算（delete_files 缺省
    就是 true），另外三条在路由层算、算之前先查过任务行。所以助手若不以
    row_deleted 为前提删产物，这条管线会在返回 404 的同时把同名目录 rmtree 掉。
    残留同名目录的真实来路：先 delete_files=false 删了行、目录留着，客户端重试
    再带 delete_files=true。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from pathlib import Path

    stale = Path(tmp_path) / "downloads" / "terrain" / "local_task_99999"
    stale.mkdir(parents=True)
    leftover = stale / "terrain_tiles" / "layer.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text('{"stale":true}', encoding="utf-8")

    resp = client.delete("/api/terrain/local/tasks/99999?delete_files=true")

    assert resp.status_code == 404, resp.get_json()
    assert leftover.exists(), "行不存在却把同名目录删了 —— 404 掩盖了一次真删"


# ---------------------------------------------------------------------------
# M4 修复的行为测试：流式上传(M5)、创建失败清目录(M12)、limit 钳制(M13)、
# parent_url 配置键(M20)
# ---------------------------------------------------------------------------


def test_create_task_accepts_file_streams(monkeypatch, tmp_path):
    """M5: manager 接受文件流（路由直传 FileStorage），分块写盘；
    落盘内容与 size_bytes 与 bytes 路径一致。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    payload_b = b"fake-tif-bytes-b" * 500  # 大于 copyfileobj 单块也无所谓，流式复制
    files = [
        ("a.tif", io.BytesIO(b"fake-tif-bytes-a")),
        ("b.tiff", io.BytesIO(payload_b)),
    ]
    task_id = mgr.create_task_with_files(name="stream", files=files, maxzoom=12)

    from pathlib import Path
    source_dir = Path(mgr.get_task(task_id)["source_dir"])
    assert (source_dir / "upload_1_dem.tif").read_bytes() == b"fake-tif-bytes-a"
    assert (source_dir / "upload_2_dem.tif").read_bytes() == payload_b

    rows = {r["original_filename"]: r for r in mgr.list_files(task_id)}
    assert rows["a.tif"]["size_bytes"] == len(b"fake-tif-bytes-a")
    assert rows["b.tiff"]["size_bytes"] == len(payload_b)
    assert all(r["status"] == "uploaded" for r in rows.values())


def test_create_task_all_uploads_fail_cleans_dir(monkeypatch, tmp_path):
    """M12: 全部写盘失败时，任务行标记 failed，且任务目录被清理，不留磁盘残留。"""
    import pytest

    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(mgr_mod.shutil, "copyfileobj", boom)

    with pytest.raises(ValueError):
        mgr.create_task_with_files(name="f", files=[("a.tif", io.BytesIO(b"x"))], maxzoom=12)

    tasks = mgr.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"

    from pathlib import Path
    task_root = Path(tasks[0]["output_path"])
    assert not task_root.exists()


def test_create_task_rollback_cleans_dir_rowid_reuse_safe(monkeypatch, tmp_path):
    """M12: 文件落盘后、commit 前失败（回滚路径）也要清任务目录；
    SQLite rowid 复用后，下一个同 id 任务的 source 目录不含残留 tif。"""
    import sqlite3

    import pytest

    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    real_get_connection = mgr_mod.get_connection

    class _CommitBoom:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(mgr_mod, "get_connection", lambda: _CommitBoom(real_get_connection()))

    # 两个文件都落盘后 commit 失败 -> 回滚；残留 upload_1/upload_2 必须被清掉
    with pytest.raises(Exception):
        mgr.create_task_with_files(
            name="boom",
            files=[("a.tif", b"stale-a"), ("a2.tif", b"stale-a2")],
            maxzoom=12,
        )

    monkeypatch.setattr(mgr_mod, "get_connection", real_get_connection)

    # 回滚后表为空，rowid 复用：新任务拿到同一个 id、同一个任务目录
    task_id = mgr.create_task_with_files(name="ok", files=[("b.tif", b"fresh")], maxzoom=12)

    from pathlib import Path
    source_dir = Path(mgr.get_task(task_id)["source_dir"])
    saved = sorted(p.name for p in source_dir.glob("*_dem.tif"))
    assert saved == ["upload_1_dem.tif"]
    assert (source_dir / "upload_1_dem.tif").read_bytes() == b"fresh"
    assert len(mgr.list_files(task_id)) == 1


def test_list_tasks_limit_clamped(monkeypatch, tmp_path):
    """M13: limit<=0 或 >100 回退默认窗口 100；SQLite LIMIT -1 不能返回全表。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, total_files, uploaded_files, maxzoom)
            VALUES ('t', 'completed', '/tmp/x', '/tmp/x/source', '/tmp/x/terrain_tiles', 1, 1, 12)
            """,
            [() for _ in range(105)],
        )
        conn.commit()
    finally:
        conn.close()

    assert len(mgr.list_tasks(limit=-1)) == 100
    assert len(mgr.list_tasks(limit=0)) == 100
    assert len(mgr.list_tasks(limit=5)) == 5
    assert len(mgr.list_tasks(limit=500)) == 100
    assert len(mgr.list_tasks(limit=None)) == 100


def test_parent_url_defaults_to_localhost(monkeypatch, tmp_path):
    """M20: 未配置时 parent_url 保持 localhost:5000 既有行为。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    _make_base_terrain(tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="p", files=[("a.tif", b"x")], maxzoom=12)
    # 目录形式（2026-08-05 改）—— 带 /layer.json 会让 Cesium 降级成 heightmap，
    # 高程全错且不报错。见 layer_json.normalize_parent_url。
    assert mgr.get_task(task_id)["parent_url"] == "http://localhost:5000/terrain/base"


def test_parent_url_from_config_key(monkeypatch, tmp_path):
    """M20: 配置键 terrain_base_parent_url（与 DEM 管线同一键）覆盖默认值。"""
    from src.services.config_manager import ConfigManager

    db, mgr_mod = _reload(monkeypatch, tmp_path)
    _make_base_terrain(tmp_path)
    ConfigManager().set(
        "terrain_base_parent_url", "https://tiles.example.com:8443/terrain/base/layer.json"
    )
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="p", files=[("a.tif", b"x")], maxzoom=12)
    # 配置值被采用，尾部 /layer.json 在写入前被剥掉
    assert mgr.get_task(task_id)["parent_url"] == "https://tiles.example.com:8443/terrain/base"


def test_start_tiling_parent_url_fallback_uses_config(monkeypatch, tmp_path):
    """M20: 存量行 parent_url 为 NULL 时，start_tiling 的回退值也走配置键。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    _make_base_terrain(tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    captured = {}

    def fake_tile(task_dir, out_dir, params):
        captured["parent_url"] = params.parent_url
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(name="p", files=[("a.tif", b"x")], maxzoom=12)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)
    # 正常创建的行已写入配置值
    assert captured["parent_url"] == "http://localhost:5000/terrain/base"

    # 模拟存量脏行：parent_url 置 NULL，重新 start，回退值同样走配置
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE local_terrain_tasks SET parent_url=NULL WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)
    assert captured["parent_url"] == "http://localhost:5000/terrain/base"


def test_parent_url_is_none_when_base_terrain_was_never_built(monkeypatch, tmp_path):
    """没建 base_z8（默认装机的常态）时必须不写 parentUrl。

    与 DEM 管线同一条闸门。两条管线独立，只修一条 = 另一条仍然产出高程全错
    的地形，而且失败是静默的（作业 completed、瓦片 200、控制台无报错）。
    """
    db, mgr_mod = _reload(monkeypatch, tmp_path)     # 刻意不建 base
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, task_id: None)

    task_id = mgr.create_task_with_files(name="p", files=[("a.tif", b"x")], maxzoom=12)

    assert mgr.get_task(task_id)["parent_url"] is None



def test_preset_reaches_tile_params(monkeypatch, tmp_path):
    """档位 -> level_offset、法线 -> normals，必须原样进到 TileParams。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["level_offset"] = params.level_offset
        calls["normals"] = params.normals
        calls["triangulator"] = params.triangulator
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(
        name="local-preset", files=[("a.tif", b"fake")], maxzoom=11,
        quality="speed", vertex_normals=True)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["level_offset"] == -1
    assert calls["normals"] is True
    # 应用侧后端恒为 grid，档位不改后端。
    assert calls["triangulator"] == "grid"


def test_preset_defaults_when_omitted(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["level_offset"] = params.level_offset
        calls["normals"] = params.normals
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(
        name="local-default", files=[("a.tif", b"fake")], maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["level_offset"] == 0
    assert calls["normals"] is False


def test_preset_survives_restart(monkeypatch, tmp_path):
    """重跑必须从库读回档位/法线，不能静默退回默认档。

    start_tiling 不带参（唯一入口就是 `start_tiling(task_id)`），档位只能来自
    建任务时落库的那两列。漏读回的话「建任务选 speed、重跑变 balanced」——
    产物变了、状态仍是 completed、全程零报错，用户只会以为档位没生效。
    """
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["level_offset"] = params.level_offset
        calls["normals"] = params.normals
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(
        name="local-restart", files=[("a.tif", b"fake")], maxzoom=11,
        quality="speed", vertex_normals=True)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)
    assert calls["level_offset"] == -1

    calls.clear()
    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["level_offset"] == -1
    assert calls["normals"] is True


def test_preset_persisted_on_task_row(monkeypatch, tmp_path):
    """两列真的落库 —— 重跑读回的前提。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)

    task_id = mgr.create_task_with_files(
        name="local-row", files=[("a.tif", b"fake")], maxzoom=12,
        quality="precision", vertex_normals=True)

    row = mgr.get_task(task_id)
    assert row["quality"] == "precision"
    assert row["vertex_normals"] == 1


def test_invalid_quality_rejected(monkeypatch, tmp_path):
    """拼错的档位当场 ValueError（路由转 400），不静默退回 balanced。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling",
                        lambda self, task_id: None)

    import pytest
    with pytest.raises(ValueError):
        mgr.create_task_with_files(
            name="bad-quality", files=[("a.tif", b"fake")], maxzoom=12,
            quality="turbo")


# --------------------------------------------------------------------------
# 路由层收参（Task 8）：档位/法线从 multipart 表单进来，一路落到任务行。
# 上面那批直调 manager 的用例钉的是管理器契约；下面这批钉的是「表单字段真的
# 被读了并转下去」—— 路由漏读一个字段，上面全绿、用户选的档位却永远不生效。

def _http_upload(client, **form):
    form.setdefault("name", "http-preset")
    form["files"] = [(io.BytesIO(b"fake"), "a.tif")]
    return client.post("/api/terrain/local/tasks", data=form,
                       content_type="multipart/form-data")


def _no_tiling(monkeypatch, app_mod):
    monkeypatch.setattr(app_mod.local_terrain_task_manager.__class__,
                        "start_tiling", lambda self, task_id: None)


def test_http_upload_persists_preset(monkeypatch, tmp_path):
    """表单里的 quality / vertex_normals 落到任务行。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _no_tiling(monkeypatch, app_mod)

    resp = _http_upload(client, maxzoom="10", quality="speed",
                        vertex_normals="true")

    assert resp.status_code == 201, resp.get_json()
    row = app_mod.local_terrain_task_manager.get_task(resp.get_json()["task_id"])
    assert row["quality"] == "speed"
    assert row["vertex_normals"] == 1


def test_http_upload_preset_defaults_when_omitted(monkeypatch, tmp_path):
    """两个字段都不传时落出厂默认：balanced + 法线关。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _no_tiling(monkeypatch, app_mod)

    resp = _http_upload(client, maxzoom="10")

    assert resp.status_code == 201, resp.get_json()
    row = app_mod.local_terrain_task_manager.get_task(resp.get_json()["task_id"])
    assert row["quality"] == "balanced"
    assert row["vertex_normals"] == 0


def test_http_upload_distinguishes_omitted_normals_from_explicit_false(
        monkeypatch, tmp_path):
    """「没传」走配置默认，「传了 false」是用户明确关掉。

    把配置拨到开再各来一次：混淆两者的实现会让用户在设置里开的法线永远
    生效不了 —— 而法线烘进瓦片，事后想开只能重切。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _no_tiling(monkeypatch, app_mod)
    app_mod.local_terrain_task_manager.config.set("terrain_vertex_normals", "true")

    omitted = _http_upload(client, maxzoom="10")
    assert omitted.status_code == 201, omitted.get_json()
    row = app_mod.local_terrain_task_manager.get_task(omitted.get_json()["task_id"])
    assert row["vertex_normals"] == 1

    explicit = _http_upload(client, maxzoom="10", vertex_normals="false")
    assert explicit.status_code == 201, explicit.get_json()
    row = app_mod.local_terrain_task_manager.get_task(explicit.get_json()["task_id"])
    assert row["vertex_normals"] == 0


def test_http_upload_rejects_unknown_quality(monkeypatch, tmp_path):
    """拼错的档位是 400，不是 500，也不建任务、不落盘。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _no_tiling(monkeypatch, app_mod)

    resp = _http_upload(client, maxzoom="10", quality="turbo")

    assert resp.status_code == 400, resp.get_json()
    assert "quality" in resp.get_json()["error"]
    assert client.get("/api/terrain/local/tasks").get_json()["count"] == 0


def test_http_upload_rejects_unrecognized_vertex_normals(monkeypatch, tmp_path):
    """认不出来的法线开关当场 400，不静默折成 False。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    _no_tiling(monkeypatch, app_mod)

    resp = _http_upload(client, maxzoom="10", vertex_normals="on")

    assert resp.status_code == 400, resp.get_json()
    assert "vertex_normals" in resp.get_json()["error"]
    assert client.get("/api/terrain/local/tasks").get_json()["count"] == 0