import importlib
import sys


def _load_client(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 全球底图缓存自 user_version=3 起落在 BASE_DIR/assets/terrain/base_z8；
    # 不 patch BASE_DIR 的话 /terrain/base 会去服务真实仓库根。
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)

    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    return app.test_client()


def test_terrain_base_serves_existing_layer_json(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)

    # Default terrain_global_base_path is ./assets/terrain/base_z8, rebased
    # onto Config.BASE_DIR (assets/ ships with the package; downloads/ is user output).
    base_dir = tmp_path / "assets" / "terrain" / "base_z8"
    base_dir.mkdir(parents=True)
    (base_dir / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    r = client.get("/terrain/base/layer.json")
    assert r.status_code == 200
    assert b'"tilejson"' in r.data


def test_terrain_base_missing_file_returns_404(monkeypatch, tmp_path):
    client = _load_client(monkeypatch, tmp_path)

    r = client.get("/terrain/base/layer.json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /terrain/dem/<id>/ —— 必须按 dem_tasks.output_path 解析(与切片写入侧
# dem_task_manager._resolve_task_output_dir 同口径),而不是硬编码
# downloads/dem:用户自定义保存路径(仍在 DOWNLOADS_DIR 内,合法)时,
# 切片写在 <output_path>/dem_task_<id>/,硬编码路由读不到,地形预览 404
# 退化成「仅定位到区域」。
# ---------------------------------------------------------------------------


def _insert_dem_task(output_path: str) -> int:
    db = importlib.import_module("src.core.database")
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks (name, status, north, south, east, west, dataset, output_path)
            VALUES ('dem', 'completed', 1, 0, 1, 0, 'COP-DEM-GLO-30', ?)
            """,
            (output_path,),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_terrain_dem_serves_from_task_output_path(monkeypatch, tmp_path):
    """自定义 output_path 的 dem 任务,切片也必须能被服务到"""
    client = _load_client(monkeypatch, tmp_path)
    custom = tmp_path / "downloads" / "my_dem"
    task_id = _insert_dem_task(str(custom))

    tiles = custom / f"dem_task_{task_id}" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    r = client.get(f"/terrain/dem/{task_id}/layer.json")
    assert r.status_code == 200
    assert b'"tilejson"' in r.data


def test_terrain_dem_default_path_still_served(monkeypatch, tmp_path):
    """回归护栏:默认 <downloads>/dem 路径的任务不受影响"""
    client = _load_client(monkeypatch, tmp_path)
    default = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(default))

    tiles = default / f"dem_task_{task_id}" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    r = client.get(f"/terrain/dem/{task_id}/layer.json")
    assert r.status_code == 200


def test_terrain_dem_unknown_task_returns_404_even_if_files_exist(monkeypatch, tmp_path):
    """任务行不存在 → 404,即使磁盘上恰好有同 id 的目录(与 tiles/contour/local 三路一致)"""
    client = _load_client(monkeypatch, tmp_path)

    orphan = tmp_path / "downloads" / "dem" / "dem_task_999" / "terrain_tiles"
    orphan.mkdir(parents=True)
    (orphan / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    r = client.get("/terrain/dem/999/layer.json")
    assert r.status_code == 404


def test_terrain_dem_deleted_task_stops_serving(monkeypatch, tmp_path):
    """delete_files=false(磁盘保留)删除任务后,瓦片 URL 必须立即 404 ——
    路由层删除时要清 output_path 缓存,否则已删任务仍可访问。"""
    client = _load_client(monkeypatch, tmp_path)
    default = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(default))

    tiles = default / f"dem_task_{task_id}" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text('{"tilejson":"2.1.0"}', encoding="utf-8")

    assert client.get(f"/terrain/dem/{task_id}/layer.json").status_code == 200

    r = client.delete(f"/api/dem/tasks/{task_id}?delete_files=false")
    assert r.status_code == 200
    assert (tiles / "layer.json").exists(), "delete_files=false 不应动磁盘"

    assert client.get(f"/terrain/dem/{task_id}/layer.json").status_code == 404


# ---------------------------------------------------------------------------
# 存量 layer.json 的 parentUrl 按【响应期】归一
#
# 磁盘上已经切好的任务里固化着 `http://localhost:5000/terrain/...`：瓦片现在可能
# 由 5001 专用 origin 提供，那个地址会把父级请求绕回主连接池；远程访问时
# `localhost` 更是指向客户端本机 —— 两种情况都是 404，而 Cesium 对这个 404 不
# 报错，它塞一个假 heightmap-1.0 图层并把 heightmapStructure 写在共享 builder 上，
# 于是任务自己的 quantized-mesh 瓦片也按 heightmap 解析（实测 4154 m 山峰解成
# -744 m，控制台零报错）。
#
# 改在响应里而不是回写磁盘：切片产物是用户数据，服务端不该在 GET 上改它；而且
# 目录可能只读、也可能被拷到别处，回写既不可靠也没必要。
# ---------------------------------------------------------------------------


def _seed_layer_json(tmp_path, parent_url: str) -> int:
    import json

    default = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(default))
    tiles = default / f"dem_task_{task_id}" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text(json.dumps({
        "tilejson": "2.1.0",
        "format": "quantized-mesh-1.0",
        "parentUrl": parent_url,
    }), encoding="utf-8")
    return task_id


def test_legacy_localhost_parent_url_is_relative_in_the_response(monkeypatch, tmp_path):
    """存量的 http://localhost:5000/terrain/base 在响应里变成 /terrain/base。"""
    import json

    client = _load_client(monkeypatch, tmp_path)
    task_id = _seed_layer_json(tmp_path, "http://localhost:5000/terrain/base")

    r = client.get(f"/terrain/dem/{task_id}/layer.json")
    assert r.status_code == 200
    assert json.loads(r.data)["parentUrl"] == "/terrain/base"
    # 其余字段原样保留
    assert json.loads(r.data)["format"] == "quantized-mesh-1.0"

    # 磁盘文件不能被改动 —— 归一只发生在响应期
    on_disk = json.loads(
        (tmp_path / "downloads" / "dem" / f"dem_task_{task_id}" / "terrain_tiles"
         / "layer.json").read_text(encoding="utf-8"))
    assert on_disk["parentUrl"] == "http://localhost:5000/terrain/base"


def test_external_parent_url_is_served_unchanged(monkeypatch, tmp_path):
    """部署者配置的外部地形服务不能被改写。"""
    import json

    client = _load_client(monkeypatch, tmp_path)
    task_id = _seed_layer_json(tmp_path, "https://terrain.example.com/base")

    r = client.get(f"/terrain/dem/{task_id}/layer.json")
    assert r.status_code == 200
    assert json.loads(r.data)["parentUrl"] == "https://terrain.example.com/base"


def test_malformed_layer_json_is_served_as_is(monkeypatch, tmp_path):
    """非 JSON / 解不开的 layer.json 保持原行为（原样发出，不 500）。"""
    client = _load_client(monkeypatch, tmp_path)
    default = tmp_path / "downloads" / "dem"
    task_id = _insert_dem_task(str(default))
    tiles = default / f"dem_task_{task_id}" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text("{not json", encoding="utf-8")

    r = client.get(f"/terrain/dem/{task_id}/layer.json")
    assert r.status_code == 200
    assert r.data == b"{not json"
