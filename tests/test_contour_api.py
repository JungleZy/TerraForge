import importlib
import io
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


def _post_task(client, name="bj", files=None, **fields):
    """multipart 上传创建等高线任务。files 默认一个假 tif（内容不是真
    GeoTIFF —— 管理器只做扩展名/非空校验，范围并集读不出来就保持 0）。"""
    if files is None:
        files = [("dem1.tif", b"fake-tif-bytes")]
    data = dict(fields)
    data["name"] = name
    data["files"] = [(io.BytesIO(content), fname) for fname, content in files]
    return client.post("/api/contour/tasks", data=data,
                       content_type="multipart/form-data")


def test_create_contour_task_returns_201(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, contour_interval="50", zoom_min="12", zoom_max="14")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    assert isinstance(body["task_id"], int)


def test_create_contour_task_is_upload_driven(monkeypatch, tmp_path):
    """上传驱动：dataset='upload'、没有下载阶段（文件行直接 completed）、
    bbox 从上传文件算（假 tif 读不出范围则保持 0）。terrain_shade 透传。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(client, name="x", contour_interval="50",
                     zoom_min="12", zoom_max="12",
                     terrain_shade="0").get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["dataset"] == "upload"
    assert task["water"] == 0
    assert task["terrain_shade"] == 0
    assert task["total_files"] == 1
    assert task["downloaded_files"] == 1


def test_create_contour_task_no_files_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/contour/tasks", data={"name": "x"},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_create_contour_task_rejects_non_tif_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, files=[("dem.png", b"fake")])
    assert resp.status_code == 400


def test_create_contour_task_stores_style_overrides(monkeypatch, tmp_path):
    """按任务自定义配色：线色/分层断点/分层颜色入库，渲染时由 style_for_task 应用。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(
        client,
        line_color_intermediate="#111111",
        line_color_index="#222222",
        tint_breaks="0,1000",
        tint_colors="#111111,#222222,#333333",
    ).get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["line_color_intermediate"] == "#111111"
    assert task["line_color_index"] == "#222222"
    assert task["tint_breaks"] == "0.0,1000.0"
    assert task["tint_colors"] == "#111111,#222222,#333333"


def test_create_contour_task_rejects_bad_tint_400(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    # 2 个断点只给 2 个颜色（要 3 个）
    resp = _post_task(client, tint_breaks="0,1000", tint_colors="#111111,#222222")
    assert resp.status_code == 400


def test_style_preview_returns_png(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/contour/style_preview")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_style_preview_reflects_params(monkeypatch, tmp_path):
    """关掉地形着色 + 指定背景色：预览整幅就是该背景（无 tint 混合），
    取底边中点像素直接比对 —— 证明参数确实进了渲染管线。"""
    import io as _io

    from PIL import Image
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get(
        "/api/contour/style_preview?background=%23112233&terrain_shade=0&interval=100000")
    assert resp.status_code == 200
    im = Image.open(_io.BytesIO(resp.data)).convert("RGBA")
    r, g, b, a = im.getpixel((im.width // 2, im.height - 2))
    assert (r, g, b) == (0x11, 0x22, 0x33)


def test_list_and_get_contour_task(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    tid = _post_task(client, contour_interval="50",
                     zoom_min="12", zoom_max="14").get_json()["task_id"]

    lst = client.get("/api/contour/tasks")
    assert lst.status_code == 200
    assert lst.get_json()["count"] >= 1

    got = client.get(f"/api/contour/tasks/{tid}")
    assert got.status_code == 200
    assert got.get_json()["task"]["id"] == tid
