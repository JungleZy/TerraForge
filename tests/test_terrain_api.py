import importlib
import json
import logging
import os
import pathlib
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    # Isolate DB and directory side effects before importing app.py (which runs init_database()).
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    for mod in ("app", "src.core.database", "src.services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")

    app = app_mod.app
    app.config["TESTING"] = True
    return app_mod, app.test_client()


def _insert_dem_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path, total_files)
            VALUES ('dem', 'completed', 1, 0, 1, 0, 'ASTGTM.003', ?, 1)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_terrain_api_wiring_does_not_return_500(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)

    get_response = client.get("/api/terrain/dem/1")
    assert get_response.status_code == 200
    assert get_response.get_json()["job"] is None

    start_response = client.post("/api/terrain/dem/1/start")
    assert start_response.status_code == 400
    assert "DEM task 1 not found" in start_response.get_json()["error"]


def test_terrain_api_start_creates_running_job(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, *args, **kwargs):
        return None

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job", fake_run_tiling_job)

    response = client.post(f"/api/terrain/dem/{task_id}/start")

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        job = cur.fetchone()
    finally:
        conn.close()

    assert job is not None
    assert job["status"] == "running"
    assert job["maxzoom"] == 14
    assert job["output_dir"].endswith(os.path.join(f"dem_task_{task_id}", "terrain_tiles"))


def test_terrain_api_get_existing_job(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_terrain_jobs
              (task_id, status, output_dir, maxzoom, parent_url, started_at)
            VALUES (?, 'completed', ?, 12, 'http://localhost:5000/terrain/base/layer.json', ?)
            """,
            (task_id, str(tmp_path / "tiles"), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/api/terrain/dem/{task_id}")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["job"]["task_id"] == task_id
    assert body["job"]["status"] == "completed"


def test_terrain_api_rejects_duplicate_running_start(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def fake_run_tiling_job(self, *args, **kwargs):
        return None

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job", fake_run_tiling_job)

    first = client.post(f"/api/terrain/dem/{task_id}/start")
    second = client.post(f"/api/terrain/dem/{task_id}/start")

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already running" in second.get_json()["error"]


def _job_row(db, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _finish_job(db, task_id):
    """把 job 行从 running 落到 completed —— 重切前的前置（running 会被拒）。"""
    conn = db.get_connection()
    try:
        conn.execute("UPDATE dem_terrain_jobs SET status='completed' WHERE task_id=?",
                     (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_terrain_start_persists_preset_defaults(monkeypatch, tmp_path):
    """不传档位时落库出厂默认：balanced + 法线关。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200

    job = _job_row(db, task_id)
    assert job["quality"] == "balanced"
    assert job["vertex_normals"] == 0


def test_terrain_start_persists_explicit_preset(monkeypatch, tmp_path):
    """显式档位与法线开关端到端落库：JSON body -> 路由 -> 管理器 -> 库。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"quality": "speed", "vertex_normals": True})

    assert resp.status_code == 200, resp.get_json()
    job = _job_row(db, task_id)
    assert job["quality"] == "speed"
    assert job["vertex_normals"] == 1


def test_terrain_start_accepts_form_encoded_preset(monkeypatch, tmp_path):
    """表单送上来的是字符串 'true' 而不是真布尔，同样要收下。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       data={"quality": "precision", "vertex_normals": "true"})

    assert resp.status_code == 200, resp.get_json()
    job = _job_row(db, task_id)
    assert job["quality"] == "precision"
    assert job["vertex_normals"] == 1


def test_terrain_start_distinguishes_omitted_normals_from_explicit_false(
        monkeypatch, tmp_path):
    """「没传」走配置默认，「传了 false」是用户明确关掉 —— 两者不能混为一谈。

    把配置默认拨到开，再分别不传 / 显式传 false：混淆的实现（例如用真值判断
    收参）会让第一条也落 0，用户在设置里开的法线永远生效不了。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)
    app_mod.dem_task_manager.config.set("terrain_vertex_normals", "true")

    assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200
    assert _job_row(db, task_id)["vertex_normals"] == 1
    _finish_job(db, task_id)

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"vertex_normals": False})

    assert resp.status_code == 200, resp.get_json()
    assert _job_row(db, task_id)["vertex_normals"] == 0


def test_terrain_start_rejects_unknown_quality(monkeypatch, tmp_path):
    """拼错的档位是客户端错误：400，不是 500，也不静默退回 balanced。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    resp = client.post(f"/api/terrain/dem/{task_id}/start", json={"quality": "fast"})

    assert resp.status_code == 400, resp.get_json()
    assert "quality" in resp.get_json()["error"]
    assert _job_row(db, task_id) is None, "档位非法却还是建了 job 行"


def test_terrain_start_rejects_unhashable_quality(monkeypatch, tmp_path):
    """JSON 能送来 list 这种不可哈希值：仍是 400，不能漏成 500。

    validate_tiling_quality 用的是 `not isinstance(str)` 先判，所以不会在
    集合查找上抛 TypeError；这条钉的就是那个 isinstance 前置不被改掉。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    resp = client.post(f"/api/terrain/dem/{task_id}/start", json={"quality": []})

    assert resp.status_code == 400, resp.get_json()
    assert _job_row(db, task_id) is None


def test_terrain_start_rejects_unrecognized_vertex_normals(monkeypatch, tmp_path):
    """认不出来的法线开关当场 400，不静默折成 False。

    静默折成 False 的话，用户勾了法线、瓦片却没烘 —— 而法线是烘进瓦片的，
    事后想开只能重切（database.py:98）。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       data={"vertex_normals": "on"})

    assert resp.status_code == 400, resp.get_json()
    assert "vertex_normals" in resp.get_json()["error"]
    assert _job_row(db, task_id) is None


def test_terrain_restart_overwrites_previous_preset(monkeypatch, tmp_path):
    """重切换档位必须真的换掉。

    重切走的是 upsert 的 ON CONFLICT DO UPDATE 分支：那段漏写 quality /
    vertex_normals 的话，用户改了档位重切会沉默沿用上一轮的旧值 —— 产物没变、
    全程零报错。
    """
    app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    app_mod.dem_task_manager.start_tiling(task_id, quality="speed", vertex_normals=True)
    assert _job_row(db, task_id)["quality"] == "speed"
    _finish_job(db, task_id)

    app_mod.dem_task_manager.start_tiling(task_id, quality="precision", vertex_normals=False)

    job = _job_row(db, task_id)
    assert job["quality"] == "precision"
    assert job["vertex_normals"] == 0


def test_terrain_start_falls_back_when_configured_maxzoom_is_out_of_range(
        monkeypatch, tmp_path, caplog):
    """配置里的越界 maxzoom 退回出厂默认 14，不靠 build_terrain 那边封顶兜。

    退回必须留痕：静默吞掉用户写过的 99，会让「我明明配了 99」在整个系统里
    一处都查不到。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)
    app_mod.dem_task_manager.config.set("terrain_local_maxzoom", "99")

    with caplog.at_level(logging.WARNING, logger="src.services.dem_task_manager"):
        assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200
    assert _job_row(db, task_id)["maxzoom"] == 14

    dropped = [r.getMessage() for r in caplog.records
               if "terrain_local_maxzoom" in r.getMessage()]
    assert dropped, "越界配置被丢弃却没留下任何日志"
    assert "99" in dropped[0] and "14" in dropped[0]


def test_terrain_start_rejects_unhashable_vertex_normals(monkeypatch, tmp_path):
    """JSON 也能给法线送来 list：仍是 400，不能漏成 500。

    白名单写成 `in {'true','false'}` 的话，不可哈希入参在集合查找上抛
    TypeError —— 那是 except Exception 那条分支，用户拿到 500。改成 set
    全量测试仍会全绿，所以这条专门钉住元组这个选择。
    """
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"vertex_normals": []})

    assert resp.status_code == 400, resp.get_json()
    assert "vertex_normals" in resp.get_json()["error"]
    assert _job_row(db, task_id) is None


def test_terrain_start_treats_empty_strings_as_omitted(monkeypatch, tmp_path):
    """空串是「未传」，走配置默认，不是 400、也不是硬编码出厂值。

    前端收参的既有写法是 `el?.value || ''`（map.js 起切那处的 maxzoom 就这么
    发的），照抄到档位/法线上就会送来空串。当成非法值拒掉的话，用户什么都没
    改就切不动了。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)
    # 把配置拨到非出厂值：空串若被硬编码成 balanced/关，这条会红。
    app_mod.dem_task_manager.config.set("terrain_quality_preset", "speed")
    app_mod.dem_task_manager.config.set("terrain_vertex_normals", "true")

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"quality": "", "vertex_normals": ""})

    assert resp.status_code == 200, resp.get_json()
    job = _job_row(db, task_id)
    assert job["quality"] == "speed"
    assert job["vertex_normals"] == 1



# ---------------------------------------------------------------------------
# HTTP → 管理器 → TileParams → build_terrain 全链路
#
# ⚠️ 下面三条**故意不打桩 `_run_tiling_job`**，这是它们存在的全部理由，别顺手
# 加回去。本文件其余用例都打了那个桩，而
# `TileParams(level_offset=TILING_QUALITY_OFFSETS[quality])` 恰恰就在
# `_run_tiling_job` 里面（src/services/dem_task_manager.py）—— 打上桩，被测的
# 那行就在桩后面，用例退化成空跑。删掉那行 `level_offset=...`，全量测试仍会
# 全绿，只有这三条会红。
#
# 桩打在最底层的 `cesiumlab_terrain.build_terrain` 上：`tile_dem_task_dir` 是
# 在调用点才 lazy import 它的，所以换模块属性拦得住，而中间的路由收参、管理器
# 校验/落库、TileParams 构造全都跑真的。
# ---------------------------------------------------------------------------


def _record_build_terrain(monkeypatch, captured):
    """把 build_terrain 换成录参替身：记下 kwargs，写一份最小 layer.json 交差。

    layer.json 是必须写的 —— tile_dem_task_dir 在 build_terrain 之后校验它存在，
    缺了会抛 FileNotFoundError，作业记成 failed。
    """
    from src.services.terrain_tiling import cesiumlab_terrain

    def fake_build_terrain(**kwargs):
        captured.append(kwargs)
        out = pathlib.Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text(
            json.dumps({"tilejson": "2.1.0", "extensions": [], "available": []}),
            encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0,
                "max_level": int(kwargs["max_level"]) + int(kwargs["level_offset"]),
                "chose_martini": 1, "chose_grid": 0}

    monkeypatch.setattr(cesiumlab_terrain, "build_terrain", fake_build_terrain)


def _start_and_settle(monkeypatch, tmp_path, payload):
    """POST /start 后等作业线程落到终态，返回 (job 行, build_terrain 收到的 kwargs)。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    out_root = tmp_path / "downloads"
    task_id = _insert_dem_task(db, out_root)
    # 真 tiler 要求任务目录里有 *_dem.tif（内容无所谓，build_terrain 已被替身接管）。
    task_dir = out_root / f"dem_task_{task_id}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "a_dem.tif").write_bytes(b"stub-dem")

    captured = []
    _record_build_terrain(monkeypatch, captured)

    resp = client.post(f"/api/terrain/dem/{task_id}/start", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    deadline = time.time() + 60
    row = None
    while time.time() < deadline:
        row = _job_row(db, task_id)
        if row is not None and row["status"] in ("completed", "failed"):
            break
        time.sleep(0.02)

    assert row is not None, "作业行没建出来"
    assert len(captured) == 1, f"build_terrain 调用次数不对：{len(captured)}"
    return row, captured[0]


def test_http_speed_reaches_build_terrain_as_level_offset_minus_one(monkeypatch, tmp_path):
    """`{"quality":"speed"}` 一路走到 build_terrain 必须变成 level_offset=-1。

    基准层级原样传（max_level=12），偏移单独传 —— 两者在 build_terrain 里才合并，
    这是全链路唯一一次能验证「路由收到的档位字符串真的变成了层级偏移」的地方。
    """
    row, kw = _start_and_settle(monkeypatch, tmp_path, {"quality": "speed", "maxzoom": 12})

    assert row["quality"] == "speed"
    assert row["maxzoom"] == 12
    assert row["vertex_normals"] == 0
    assert kw["max_level"] == 12, kw["max_level"]
    assert kw["level_offset"] == -1, kw["level_offset"]
    assert kw["normals"] is False, kw["normals"]
    assert row["status"] == "completed", row["error_message"]


def test_http_precision_with_normals_reaches_build_terrain(monkeypatch, tmp_path):
    """精度档 + 开法线：offset=+1 且 normals=True 同时抵达，落库 vertex_normals=1。

    法线这一路要过三次类型转换（HTTP 的 bool/'true' → DB 的 INTEGER 0/1 →
    TileParams 的 bool），任何一处折错都只在真实链路上显形。
    """
    row, kw = _start_and_settle(
        monkeypatch, tmp_path,
        {"quality": "precision", "maxzoom": 12, "vertex_normals": True})

    assert row["quality"] == "precision"
    assert row["vertex_normals"] == 1
    assert kw["level_offset"] == 1, kw["level_offset"]
    assert kw["normals"] is True, kw["normals"]
    assert row["status"] == "completed", row["error_message"]


def test_http_default_preset_reaches_build_terrain_as_zero_offset(monkeypatch, tmp_path):
    """不传档位：落库 balanced，且 build_terrain 收到的偏移是 0 而不是 None。

    默认档必须是货真价实的 0 —— 传 None 会在 build_terrain 里的 int() 上炸，
    而 `or 0` 之类的兜底又会把 precision 的 +1 之外的一切都抹平。
    """
    row, kw = _start_and_settle(monkeypatch, tmp_path, {"maxzoom": 12})

    assert row["quality"] == "balanced"
    assert kw["level_offset"] == 0, kw["level_offset"]
    assert kw["normals"] is False
    assert row["status"] == "completed", row["error_message"]


# ---------------------------------------------------------------------------
# I1：实际切到的层级必须落库，且与 layer.json 里的那个数字是同一个
# ---------------------------------------------------------------------------


def _run_real_build_terrain(monkeypatch):
    """让 build_terrain **真跑**，只替掉真正需要 GDAL 的两处：采样器与逐瓦片编码。

    层级决策（偏移 + [0,21] 钳位 + min_level 收口）和 layer.json 都是
    build_terrain 自己写的。这是本文件里唯一一条不 stub 掉它的链路，而 I1 要
    证明的正是「落库的层级 == layer.json 里的层级」—— 拿替身自己回报的数字去
    对它自己写的 layer.json 是循环论证，证不了任何东西。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    class _FakeSampler:
        def __init__(self, path, nodata=None):
            # build_terrain 只用 pixel_size_deg（估算分支用，本用例显式传了
            # maxzoom 所以走不到）与 bounds（决定枚举多少张瓦片，取小选区）。
            self.pixel_size_deg = 180.0 / (64 * 2 ** 10)
            self.bounds = (116.0, 39.0, 116.1, 39.1)
            self.ds = None
            self.band = None

    monkeypatch.setattr(ct, "DemSampler", _FakeSampler)
    monkeypatch.setattr(ct, "_worker_tile", lambda task: (0.0, 1.0, "grid"))
    # 必须走串行分支：并行分支在 spawn 出来的子进程里跑**真的** _worker_tile，
    # 上面两个替身进不去，桩 tif 会让每张瓦片都失败。workers<=0 时
    # build_terrain 取 min(4, os.cpu_count())，把它压成 1 就落到串行。
    monkeypatch.setattr(ct.os, "cpu_count", lambda: 1)


def test_speed_preset_persists_the_level_it_actually_tiled(monkeypatch, tmp_path):
    """speed 档切完：库里的 effective_maxzoom = 基准−1，且 == layer.json 的 maxzoom。

    改动前 build_terrain 回报的实际层级在两个管理器里被就地丢弃，详情面板显示
    的是 maxzoom 那一列（用户填的**基准**值）—— precision/speed 两档下它和产物
    差一级，面板写 12 而 layer.json 写 13，是具体的错数字而不是「可以推导」。

    三个断言缺一不可：基准列没被改写、实际列是偏移后的值、实际列与产物里那份
    layer.json 逐字一致。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    out_root = tmp_path / "downloads"
    task_id = _insert_dem_task(db, out_root)
    task_dir = out_root / f"dem_task_{task_id}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "a_dem.tif").write_bytes(b"stub-dem")

    _run_real_build_terrain(monkeypatch)

    resp = client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"quality": "speed", "maxzoom": 12})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    deadline = time.time() + 60
    row = None
    while time.time() < deadline:
        row = _job_row(db, task_id)
        if row is not None and row["status"] in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert row is not None and row["status"] == "completed", (
        row["error_message"] if row is not None else "作业行没建出来")

    assert row["maxzoom"] == 12, "基准层级那一列被改写了 —— 它必须是用户填的值"
    assert row["effective_maxzoom"] == 11, (
        f"speed 档实际切到的层级应是 12-1=11，库里是 {row['effective_maxzoom']!r}")

    layer = json.loads(
        (pathlib.Path(row["output_dir"]) / "layer.json").read_text(encoding="utf-8"))
    assert layer["maxzoom"] == row["effective_maxzoom"], (
        f"落库的实际层级 {row['effective_maxzoom']} 与 layer.json 声明的 "
        f"{layer['maxzoom']} 对不上 —— 面板显示的和 Cesium 拿到的不是同一件事")


def test_running_job_reports_no_effective_level_yet(monkeypatch, tmp_path):
    """还没切完的作业 effective_maxzoom 必须是 NULL，不能预填基准值。

    NULL 是「未知」，界面据此回落到基准值并标明那是基准值。预填的话面板会拿
    一个尚不成立的数字冒充产物事实（而且 speed/precision 档下它还是错的）。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")
    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **kw: None)

    assert client.post(f"/api/terrain/dem/{task_id}/start",
                       json={"quality": "speed", "maxzoom": 12}).status_code == 200

    row = _job_row(db, task_id)
    assert row["status"] == "running"
    assert row["effective_maxzoom"] is None


def test_restart_clears_the_previous_effective_level(monkeypatch, tmp_path):
    """重切先把上一轮的实际层级清空 —— 新档位切完之前显示旧值就是撒谎。"""
    row, _kw = _start_and_settle(monkeypatch, tmp_path, {"quality": "speed", "maxzoom": 12})
    assert row["effective_maxzoom"] == 11

    db = importlib.import_module("src.core.database")
    task_id = row["task_id"]
    conn = db.get_connection()
    try:
        conn.execute("UPDATE dem_terrain_jobs SET status='completed' WHERE task_id=?",
                     (task_id,))
        conn.commit()
    finally:
        conn.close()

    from src.services import dem_task_manager as dtm
    mgr = dtm.DemTaskManager.__new__(dtm.DemTaskManager)
    mgr.config = {}
    mgr._state_lock = __import__("threading").RLock()
    mgr.active_tasks = {}
    mgr.stop_flags = {}
    monkeypatch.setattr(dtm.DemTaskManager, "_run_tiling_job",
                        lambda self, *a, **kw: None)
    mgr.start_tiling(task_id, maxzoom=12, quality="precision")

    assert _job_row(db, task_id)["effective_maxzoom"] is None, (
        "重切没把上一轮的实际层级清空 —— 面板会拿上一档的产物事实冒充这一档的")


def test_thread_construction_failure_does_not_strand_the_dem_job_in_running(
        monkeypatch, tmp_path):
    """线程构造抛出去时 job 行不能停在 running。

    job 行在 `conn.commit()` 那一刻就已经是 running,而 `threading.Thread(...)`
    构造与 active_tasks / stop_flags 登记此前排在包住 `th.start()` 的 try
    **外面** —— 构造抛出去时 L2 回补块够不着,job 行永久停在 running:再次
    start_tiling 被 `WHERE status != 'running'` 判为已在运行,delete_task 也被
    挡,只能重启进程靠孤儿恢复解开。
    """
    import pytest
    import threading as _threading

    app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    mgr_mod = importlib.import_module("src.services.dem_task_manager")
    task_id = _insert_dem_task(db, tmp_path / "downloads")
    mgr = app_mod.dem_task_manager

    class _NoThreads:
        """只让 Thread 构造抛（模拟 can't start new thread），其余转发给真模块。

        换掉的是模块名字而不是全局 threading.Thread：后者会在用例期间影响进程里
        任何别的线程创建。
        """

        def __getattr__(self, name):
            return getattr(_threading, name)

        def Thread(self, *args, **kwargs):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(mgr_mod, "threading", _NoThreads())

    with pytest.raises(RuntimeError):
        mgr.start_tiling(task_id)

    job = _job_row(db, task_id)
    assert job is not None
    assert job["status"] == "failed", "线程构造失败，job 行卡死在 running"
    assert "failed to start" in (job["error_message"] or "")
    assert task_id not in mgr.active_tasks
    assert task_id not in mgr.stop_flags


def test_dem_terminal_updates_never_overwrite_an_already_terminal_job(
        monkeypatch, tmp_path):
    """_run_tiling_job 的两条终态 UPDATE 都不能改写一条已落终态的 job 行。

    两条语句此前只有 `WHERE task_id=?`。仓库约定（见 _mark_failed / 本地地形侧）
    是终态 UPDATE 必须带 `AND status='running'`：否则一条迟到的收尾会把别的路径
    刚写下的终态盖掉，界面上的成败结论会莫名其妙地反转。
    """
    from src.services import dem_task_manager as dtm

    _app_mod, _client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    def _seed_job(status):
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
            conn.execute(
                """
                INSERT INTO dem_terrain_jobs
                  (task_id, status, output_dir, maxzoom, parent_url, started_at)
                VALUES (?, ?, ?, 12, '', ?)
                """,
                (task_id, status, str(tmp_path / "tiles"),
                 datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    # __new__ 构造：本方法对 socketio / _state_lock 都用 getattr 兜底，直调即可。
    mgr = dtm.DemTaskManager.__new__(dtm.DemTaskManager)
    task_dir = tmp_path / "downloads" / f"dem_task_{task_id}"
    out_dir = task_dir / "terrain_tiles"

    # 失败兜底那条：行已是 completed，不能被改写成 failed。
    _seed_job("completed")

    def boom_tiler(**kwargs):
        raise RuntimeError("tiler exploded")

    monkeypatch.setattr(dtm, "tile_dem_task_dir", boom_tiler)
    mgr._run_tiling_job(task_id, task_dir, out_dir, 12, "")

    row = _job_row(db, task_id)
    assert row["status"] == "completed", "失败兜底盖掉了一条已经是 completed 的行"
    assert row["error_message"] is None

    # 成功收尾那条：行已是 failed，不能被改写成 completed。
    _seed_job("failed")
    monkeypatch.setattr(
        dtm, "tile_dem_task_dir",
        lambda **kwargs: {"rendered": 1, "total": 1, "failed": 0, "max_level": 12})
    mgr._run_tiling_job(task_id, task_dir, out_dir, 12, "")

    row = _job_row(db, task_id)
    assert row["status"] == "failed", "成功收尾盖掉了一条已经是 failed 的行"