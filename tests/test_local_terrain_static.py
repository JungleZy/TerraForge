import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "database", "services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _insert_task(db, downloads_dir):
    """Insert a local terrain task and return (task_id, canonical terrain_tiles dir).

    The static route recomputes the served path from DOWNLOADS_DIR/terrain/
    local_task_<id>/terrain_tiles (it does NOT trust the stored output_dir), so
    the on-disk dir must live at that canonical location.
    """
    from pathlib import Path
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt', 'completed', '', '', '', 14)
            """
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    task_root = Path(downloads_dir) / "terrain" / f"local_task_{task_id}"
    out_dir = task_root / "terrain_tiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    return task_id, out_dir


def test_serves_layer_json_from_output_dir(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    task_id, out_dir = _insert_task(db, tmp_path / "downloads")
    (out_dir / "layer.json").write_text('{"ok":true}', encoding="utf-8")

    resp = client.get(f"/terrain/local/{task_id}/layer.json")
    assert resp.status_code == 200
    assert b'"ok"' in resp.data


def test_blocks_path_traversal(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    task_id, _out_dir = _insert_task(db, tmp_path / "downloads")

    # _resolve_safe_file must reject the traversal with a hard 400 (the guard
    # fires — this is not a Flask routing 404).
    resp = client.get(f"/terrain/local/{task_id}/..%2f..%2f..%2fsecret")
    assert resp.status_code == 400


def test_missing_task_returns_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/terrain/local/99999/layer.json")
    assert resp.status_code == 404


def test_serves_from_recomputed_path_when_stored_output_dir_is_stale(monkeypatch, tmp_path):
    """#4: the route must recompute the served dir from the current DOWNLOADS_DIR,
    not trust the absolute output_dir stored at creation time (which breaks when a
    frozen executable is relocated)."""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    # Real tiles live at the canonical DOWNLOADS_DIR location...
    real_dir = tmp_path / "downloads" / "terrain" / "local_task_1" / "terrain_tiles"
    real_dir.mkdir(parents=True)
    (real_dir / "layer.json").write_text('{"recomputed":true}', encoding="utf-8")

    # ...but the DB row stores a STALE absolute path (as if the exe moved).
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (id, name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES (1, 'lt', 'completed', '/old/moved/local_task_1',
                    '/old/moved/local_task_1/source',
                    '/old/moved/local_task_1/terrain_tiles', 14)
            """
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/terrain/local/1/layer.json")
    assert resp.status_code == 200
    assert b"recomputed" in resp.data
