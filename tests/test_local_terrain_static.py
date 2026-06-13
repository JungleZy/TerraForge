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


def _insert_task(db, output_dir):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('lt', 'completed', ?, ?, ?, 14)
            """,
            (str(output_dir.parent), str(output_dir.parent / "source"), str(output_dir)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_serves_layer_json_from_output_dir(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    output_dir = tmp_path / "downloads" / "terrain" / "local_task_X" / "terrain_tiles"
    output_dir.mkdir(parents=True)
    (output_dir / "layer.json").write_text('{"ok":true}', encoding="utf-8")

    task_id = _insert_task(db, output_dir)

    resp = client.get(f"/terrain/local/{task_id}/layer.json")
    assert resp.status_code == 200
    assert b'"ok"' in resp.data


def test_blocks_path_traversal(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")

    output_dir = tmp_path / "downloads" / "terrain" / "local_task_Y" / "terrain_tiles"
    output_dir.mkdir(parents=True)
    task_id = _insert_task(db, output_dir)

    # _resolve_safe_file must reject the traversal with a hard 400 (the guard
    # fires — this is not a Flask routing 404).
    resp = client.get(f"/terrain/local/{task_id}/..%2f..%2f..%2fsecret")
    assert resp.status_code == 400


def test_missing_task_returns_404(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/terrain/local/99999/layer.json")
    assert resp.status_code == 404
