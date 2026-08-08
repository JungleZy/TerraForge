"""C 组修复回归测试 —— 共享 API(src/routes/api.py / src/routes/dem_api.py)。

覆盖:
- C5 : POST /api/tasks 的 output_path 越界 → 400
- I3 : DELETE 绕开 manager 锁的竞态 —— 有活跃线程的任务拒绝删除(400),记录保留
- I4 : pause/cancel 的 ValueError 不再吞成 500(映射 400,与 start/resume 一致)
- I15: 预计瓦片数超阈值 → 创建直接 400(不是 warning)
- minors: PUT /api/config 非法 JSON 不 500 + 键白名单;limit/page 负数钳制;
          style/output_format 为 None → 400(不是 AttributeError → 500)
"""
import importlib
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # "routes" 包本身也必须 pop:app.py 里是 `from routes import api_bp`,
    # 若 routes/__init__ 仍在 sys.modules,注册的是旧 src.routes.api 模块的旧
    # blueprint(其视图闭包旧 task_manager),而 `from src.routes.api import
    # init_task_manager` 又会新建一个 src.routes.api 模块 —— 测试 patch 新模块、
    # 请求却打到旧模块,I3/I15 的断言就对不上。
    for mod in (
        "app", "src.core.database", "src.services.task_manager", "src.services.dem_task_manager",
        "src.routes", "src.routes.api", "src.routes.dem_api", "src.routes.contour_api",
    ):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _map_payload(**overrides):
    from src.core import config

    payload = {
        "name": "t", "north": 40.0, "south": 39.0, "east": 117.0, "west": 116.0,
        "zoom_min": 10, "zoom_max": 11, "style": "roadmap",
        # 保存路径新口径:一律绝对路径(相对值 → 400),默认 DOWNLOADS_DIR 下子目录
        "output_format": "tiles_only",
        "output_path": str(config.Config.DOWNLOADS_DIR / "downloads"),
    }
    payload.update(overrides)
    return payload


def _seed_map_task(db, status="pending"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks
              (name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles,
               downloaded_tiles, failed_tiles)
            VALUES ('map-task', ?, 1, 0, 1, 0, 0, 0, 'satellite',
                    'tiles_only', 'downloads', 0, 0, 0)
            """,
            (status,),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_dem_task(db, status="pending"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-task', ?, 1, 0, 1, 0, 'ASTGTM.003', 'downloads', 0, 0, 0)
            """,
            (status,),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _task_row(db, table, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


class _FakeActiveThread:
    """模拟 manager.active_tasks 里一条还活着的执行线程。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._stop.wait, daemon=True)
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        # task_deletion 的后台收尾会 join 工作线程。缺了这个方法它只会记一条
        # warning 就放弃，墓碑也摘不掉 —— 假线程必须在这一点上像真线程。
        self._thread.join(timeout)

    def release(self):
        self._stop.set()
        self._thread.join(timeout=5)


# ---------- C5: output_path 入口校验 ----------

@pytest.mark.parametrize('case', ['relative', 'absolute_outside', 'shallow'])
def test_create_map_task_output_path_validation(monkeypatch, tmp_path, case):
    _, client = _load_app(monkeypatch, tmp_path)
    if case == 'relative':
        bad_path = '../../outside'
    elif case == 'absolute_outside':
        # 0.2.4 全盘化:DOWNLOADS_DIR 之外的深路径(兄弟目录,深度足够)接受
        bad_path = str(tmp_path / 'outside_downloads')
    else:
        # 浅层(根目录)拒绝:产物 <path>/task_<id> 会落在根级
        bad_path = os.path.abspath(os.sep)
    resp = client.post("/api/tasks", json=_map_payload(output_path=bad_path))
    if case == 'absolute_outside':
        assert resp.status_code == 201, resp.get_json()
        return
    assert resp.status_code == 400, resp.get_json()
    error = resp.get_json()['error']
    if case == 'relative':
        # 相对路径新口径:不再代为解析,直接要求绝对路径(文案指路「浏览」)
        assert '绝对路径' in error
    else:
        assert '两级目录' in error


def test_create_map_task_output_path_inside_downloads_201(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=_map_payload(
        output_path=str(tmp_path / 'downloads' / 'sub')))
    assert resp.status_code == 201, resp.get_json()


# ---------- I3: DELETE 与活跃线程 ----------

def test_delete_map_task_with_active_thread_succeeds(monkeypatch, tmp_path):
    """语义翻面：活跃的 map 任务现在可以直接删。

    原断言是「拒删，返回 400」。砍掉「取消」之后删除是唯一的销毁动作，再拒下去
    用户就没有任何办法销毁一个正在跑的任务了。删除自己置停止标志、当场删行，
    产物清理交给后台（见 services/task_deletion）。
    """
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    api_mod = importlib.import_module("src.routes.api")
    task_id = _seed_map_task(db, status="paused")

    fake = _FakeActiveThread()
    api_mod.task_manager.active_tasks[task_id] = fake
    try:
        resp = client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 200, resp.get_json()
        assert _task_row(db, "tasks", task_id) is None, "活跃任务没被删掉"
    finally:
        fake.release()
        api_mod.task_manager.active_tasks.pop(task_id, None)


def test_delete_dem_task_with_active_thread_rejected(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    dem_api_mod = importlib.import_module("src.routes.dem_api")
    task_id = _seed_dem_task(db, status="paused")

    fake = _FakeActiveThread()
    dem_api_mod.dem_task_manager.active_tasks[task_id] = fake
    try:
        resp = client.delete(f"/api/dem/tasks/{task_id}")
        assert resp.status_code == 400, resp.get_json()
        assert _task_row(db, "dem_tasks", task_id) is not None, "活跃 DEM 任务被删掉了"
    finally:
        fake.release()
        dem_api_mod.dem_task_manager.active_tasks.pop(task_id, None)


def test_delete_map_task_without_active_thread_still_works(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_map_task(db, status="paused")
    resp = client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.get_json()
    assert _task_row(db, "tasks", task_id) is None


# ---------- I4: pause/cancel 的 ValueError → 400(不吞成 500) ----------

def test_pause_cancel_nonexistent_map_task_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks/9999/pause")
    assert resp.status_code == 400, resp.get_json()
    resp = client.post("/api/tasks/9999/cancel")
    assert resp.status_code == 400, resp.get_json()


def test_pause_map_task_with_wrong_status_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _seed_map_task(db, status="pending")
    resp = client.post(f"/api/tasks/{task_id}/pause")
    assert resp.status_code == 400, resp.get_json()


def test_pause_cancel_nonexistent_dem_task_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/dem/tasks/9999/pause")
    assert resp.status_code == 400, resp.get_json()
    resp = client.post("/api/dem/tasks/9999/cancel")
    assert resp.status_code == 400, resp.get_json()


# ---------- I15: 瓦片数超软阈值 → 0.1.4 起照常创建 ----------

def test_create_map_task_over_tile_soft_cap_still_created(monkeypatch, tmp_path):
    """0.1.4 放开硬上限：超阈值不再 400（前端大任务确认框替用户把关）。"""
    _, client = _load_app(monkeypatch, tmp_path)
    tm_mod = importlib.import_module("src.services.task_manager")
    monkeypatch.setattr(tm_mod, "WARN_TILES_THRESHOLD", 5)
    resp = client.post("/api/tasks", json=_map_payload())
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()['success'] is True


# ---------- minor: PUT /api/config ----------

def test_update_config_invalid_json_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.put("/api/config", data='{not valid json',
                      content_type='application/json')
    assert resp.status_code == 400, resp.data
    resp = client.put("/api/config", json=[1, 2, 3])
    assert resp.status_code == 400, resp.get_json()


def test_update_config_unknown_key_rejected(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.put("/api/config", json={"definitely_not_a_config_key": "1"})
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["success"] is False

    # 未知键不得落库
    db = importlib.import_module("src.core.database")
    conn = db.get_connection()
    try:
        row = conn.cursor().execute(
            "SELECT value FROM config WHERE key = 'definitely_not_a_config_key'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "未知配置键被写入数据库了"


def test_update_config_known_key_still_works(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.put("/api/config", json={"gdal_resampling": "nearest"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["success"] is True


# ---------- minor: limit/page 负数钳制 ----------

def test_get_tasks_limit_zero_clamped_to_default(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    for _ in range(2):
        _seed_map_task(db)
    resp = client.get("/api/tasks?limit=0")
    assert resp.status_code == 200
    # limit<=0 应钳制回默认值(100),而不是把 SQLite 的 LIMIT 0 透传成空列表
    assert resp.get_json()["count"] == 2


def test_history_negative_page_clamped(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/history?page=-3")
    assert resp.status_code == 200
    assert resp.get_json()["pagination"]["page"] == 1


# ---------- minor: style/output_format 为 None → 400(不是 500) ----------

def test_create_map_task_style_none_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=_map_payload(style=None))
    assert resp.status_code == 400, resp.get_json()
    assert 'style' in resp.get_json()['error']


def test_create_map_task_output_format_none_400(monkeypatch, tmp_path):
    _, client = _load_app(monkeypatch, tmp_path)
    resp = client.post("/api/tasks", json=_map_payload(output_format=None))
    assert resp.status_code == 400, resp.get_json()
    assert 'output_format' in resp.get_json()['error']


def test_map_style_from_shorthand_none_raises_valueerror():
    from src.models.task import MapStyle, OutputFormat
    with pytest.raises(ValueError):
        MapStyle.from_shorthand(None)
    with pytest.raises(ValueError):
        OutputFormat.from_shorthand(None)
