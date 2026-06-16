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
    for mod in ("app", "database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def test_create_contour_task_returns_201(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/contour/tasks", json={
        "name": "bj", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 14,
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    assert isinstance(body["task_id"], int)


def test_create_contour_task_missing_field_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/contour/tasks", json={"name": "x"})
    assert resp.status_code == 400


def test_list_and_get_contour_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = client.post("/api/contour/tasks", json={
        "name": "bj", "north": 1.0, "south": 0.0, "east": 1.0, "west": 0.0,
        "contour_interval": 50, "zoom_min": 12, "zoom_max": 14,
    }).get_json()["task_id"]

    lst = client.get("/api/contour/tasks")
    assert lst.status_code == 200
    assert lst.get_json()["count"] >= 1

    got = client.get(f"/api/contour/tasks/{tid}")
    assert got.status_code == 200
    assert got.get_json()["task"]["id"] == tid
