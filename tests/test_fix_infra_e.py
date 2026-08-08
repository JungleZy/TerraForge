"""
Code review 修复回归测试（代理 E：基础设施与本地地形）。

覆盖：
- I1  database.py 开 WAL/busy_timeout；config_manager get/get_all 区分
      “无行（返回默认值）”与“出错（抛 sqlite3.Error）”，不再把锁异常静默吞成
      默认值（earthdata_username 读成 '' → 莫名 401）。
- I2  local_terrain_task_manager.start_tiling TOCTOU：锁内条件 UPDATE。
- I7  app.py dev reloader 的 watcher 父进程跳过 create_app（含 orphan recovery）。
- I15 local terrain maxzoom 0–21 校验（API + manager）。
- I16 local terrain 不信库存路径，从 Config.DOWNLOADS_DIR 重算（冻结 exe 搬迁后
      delete_task 守卫不失效、start_tiling 不写错目录）。
- Minor: config_manager 日志掩码 earthdata_username；proxy_url 的 user:pass@
      在 config_manager.set 与 system_proxy 日志中掩码。
"""
import importlib
import io
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


def _patch_config_paths(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")


def _fresh_db(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    sys.modules.pop("src.core.database", None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    return db


def _fresh_config_manager(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    cm_mod = importlib.import_module("src.services.config_manager")
    return cm_mod, cm_mod.ConfigManager()


def _fresh_local_mgr(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    for mod in ("src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    mgr_mod = importlib.import_module("src.services.local_terrain_task_manager")
    return db, mgr_mod


def _load_app(monkeypatch, tmp_path):
    _patch_config_paths(monkeypatch, tmp_path)
    for mod in ("app", "src.core.database", "src.services.local_terrain_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


# ---------------------------------------------------------------- I1a: WAL / busy_timeout


def test_wal_mode_and_busy_timeout_enabled(monkeypatch, tmp_path):
    """get_connection 拿到的连接必须开 WAL 且有非零 busy_timeout。"""
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy > 0
    finally:
        conn.close()


def test_concurrent_writer_waits_instead_of_locked(monkeypatch, tmp_path):
    """持写事务时另一个连接的写应等待（busy_timeout）而非立刻 database is locked。"""
    db = _fresh_db(monkeypatch, tmp_path)
    conn1 = db.get_connection()
    conn1.execute(
        "INSERT INTO tasks (name,status,north,south,east,west,zoom_min,zoom_max,"
        "style,output_format,output_path) "
        "VALUES ('a','pending',1,0,1,0,0,1,'m','png','/tmp/x')"
    )  # 不提交，持有写事务

    errors = []

    def writer():
        try:
            conn2 = db.get_connection()
            try:
                conn2.execute("INSERT INTO config (key, value) VALUES ('k2', 'v2')")
                conn2.commit()
            finally:
                conn2.close()
        except Exception as e:  # noqa: BLE001 - 收集后统一断言
            errors.append(e)

    th = threading.Thread(target=writer, daemon=True)
    th.start()
    time.sleep(0.5)  # 让 writer 先撞上写锁
    conn1.commit()
    conn1.close()
    th.join(timeout=30)

    assert not th.is_alive(), "writer 线程未被 busy_timeout 放行"
    assert errors == [], f"第二个写者应等待而非报错: {errors!r}"


# ------------------------------------------- I1b: config get/get_all 出错不静默吞


def test_config_get_distinguishes_missing_row_from_error(monkeypatch, tmp_path):
    cm_mod, mgr = _fresh_config_manager(monkeypatch, tmp_path)

    # 无行：返回默认值，不抛
    assert mgr.get("no_such_key", "dflt") == "dflt"
    assert mgr.get("another_missing") is None

    # 出错（如 database is locked）：必须抛，不能静默返回默认值
    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cm_mod, "get_connection_context", boom)
    with pytest.raises(sqlite3.Error):
        mgr.get("earthdata_username", "")


def test_config_get_all_raises_on_error(monkeypatch, tmp_path):
    cm_mod, mgr = _fresh_config_manager(monkeypatch, tmp_path)

    # 正常路径不受影响
    assert isinstance(mgr.get_all(), dict) and mgr.get_all()

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cm_mod, "get_connection_context", boom)
    with pytest.raises(sqlite3.Error):
        mgr.get_all()


# ------------------------------------------------- Minor: 日志掩码 username / proxy_url


def test_config_set_masks_username_and_password(monkeypatch, tmp_path, caplog):
    _cm_mod, mgr = _fresh_config_manager(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="src.services.config_manager"):
        mgr.set("earthdata_username", "alice@nasa.example")
        mgr.set("earthdata_password", "s3cret-token")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alice@nasa.example" not in text, "username 不应进日志"
    assert "s3cret-token" not in text, "password 不应进日志"
    # 值本身仍正常入库
    assert mgr.get("earthdata_username") == "alice@nasa.example"
    assert mgr.get("earthdata_password") == "s3cret-token"


def test_config_set_masks_proxy_url_userinfo(monkeypatch, tmp_path, caplog):
    _cm_mod, mgr = _fresh_config_manager(monkeypatch, tmp_path)

    url = "http://proxyuser:p%40ssw0rd@127.0.0.1:7890"
    with caplog.at_level(logging.INFO, logger="src.services.config_manager"):
        mgr.set("proxy_url", url)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "proxyuser" not in text, "proxy_url 的用户名不应进日志"
    assert "p%40ssw0rd" not in text, "proxy_url 的密码不应进日志"
    assert "127.0.0.1:7890" in text, "掩码后仍应保留 host 便于排查"
    # 原值仍完整入库
    assert mgr.get("proxy_url") == url


def test_system_proxy_log_masks_userinfo(monkeypatch, caplog):
    from src.services import system_proxy

    monkeypatch.setattr(
        system_proxy.urllib.request,
        "getproxies",
        lambda: {"http": "http://proxyuser:secretpass@127.0.0.1:7890"},
    )
    old_http = os.environ.get("HTTP_PROXY")
    os.environ.pop("HTTP_PROXY", None)
    try:
        with caplog.at_level(logging.INFO, logger="src.services.system_proxy"):
            applied = system_proxy.apply_system_proxy()
    finally:
        os.environ.pop("HTTP_PROXY", None)
        if old_http is not None:
            os.environ["HTTP_PROXY"] = old_http

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "proxyuser" not in text, "系统代理用户名不应进日志"
    assert "secretpass" not in text, "系统代理密码不应进日志"
    # 功能不受影响：原值仍然被应用
    assert applied.get("HTTP_PROXY") == "http://proxyuser:secretpass@127.0.0.1:7890"


# ------------------------------------------------------------- I2: start_tiling TOCTOU


def test_start_tiling_rejects_second_start(monkeypatch, tmp_path):
    """已 running 的任务再次 start_tiling 必须 ValueError（既有行为，回归保护）。"""
    db, mgr_mod = _fresh_local_mgr(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    orig_start = mgr_mod.LocalTerrainTaskManager.start_tiling
    monkeypatch.setattr(
        mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, tid: None
    )
    task_id = mgr.create_task_with_files(name="seq", files=[("a.tif", b"x")], maxzoom=12)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", orig_start)

    def fake_tile(task_dir, out_dir, params):
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=30)
    assert mgr.get_task(task_id)["status"] == "completed"

    # completed 后允许重新开始（重跑切片），先把状态置回 running 验证拒绝
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE local_terrain_tasks SET status='running' WHERE id=?", (task_id,)
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="already running"):
        mgr.start_tiling(task_id)


def test_start_tiling_concurrent_calls_start_exactly_one_thread(monkeypatch, tmp_path):
    """两个线程并发 start_tiling：恰好一个成功、一个 ValueError，且只起一个切片线程。"""
    db, mgr_mod = _fresh_local_mgr(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    orig_start = mgr_mod.LocalTerrainTaskManager.start_tiling
    monkeypatch.setattr(
        mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, tid: None
    )
    task_id = mgr.create_task_with_files(name="race", files=[("a.tif", b"x")], maxzoom=12)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", orig_start)

    run_count = []

    def fake_tile(task_dir, out_dir, params):
        run_count.append(1)
        time.sleep(0.5)  # 拉长窗口，放大竞态
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    results = {}
    barrier = threading.Barrier(2)

    def call(i):
        barrier.wait(timeout=10)
        try:
            mgr.start_tiling(task_id)
            results[i] = "ok"
        except ValueError as e:
            results[i] = f"ValueError: {e}"
        except Exception as e:  # noqa: BLE001 - 竞态下非 ValueError 也算失败
            results[i] = f"{type(e).__name__}: {e}"

    threads = [threading.Thread(target=call, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for t in list(mgr.active_tasks.values()):
        t.join(timeout=30)

    oks = [v for v in results.values() if v == "ok"]
    errs = [v for v in results.values() if v.startswith("ValueError")]
    assert len(oks) == 1 and len(errs) == 1, (
        f"并发 start_tiling 应恰好一个成功、一个 ValueError，实际: {results}"
    )
    assert "already running" in errs[0]
    assert len(run_count) == 1, "只允许一个切片线程真正运行"


# ---------------------------------------------------------------- I15: maxzoom 校验


def test_create_task_rejects_maxzoom_out_of_range(monkeypatch, tmp_path):
    _db, mgr_mod = _fresh_local_mgr(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    monkeypatch.setattr(
        mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, tid: None
    )

    for bad in (25, 22, -1):
        with pytest.raises(ValueError):
            mgr.create_task_with_files(name="z", files=[("a.tif", b"x")], maxzoom=bad)

    # 边界值合法
    tid = mgr.create_task_with_files(name="z21", files=[("a.tif", b"x")], maxzoom=21)
    assert mgr.get_task(tid)["maxzoom"] == 21


def test_http_upload_rejects_maxzoom_out_of_range(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_mod.local_terrain_task_manager.__class__,
        "start_tiling",
        lambda self, task_id: None,
    )

    for bad in ("22", "100", "-1", "abc"):
        resp = client.post(
            "/api/terrain/local/tasks",
            data={
                "name": "z",
                "maxzoom": bad,
                "files": [(io.BytesIO(b"x"), "a.tif")],
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400, f"maxzoom={bad!r} 应 400，实际 {resp.status_code}"

    resp = client.post(
        "/api/terrain/local/tasks",
        data={"name": "ok", "maxzoom": "14", "files": [(io.BytesIO(b"x"), "a.tif")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201


# ------------------------------------------- I16: 不信库存路径，从 DOWNLOADS_DIR 重算


def _make_task_with_stale_paths(db, mgr_mod, monkeypatch, tmp_path):
    """建一个任务，然后把库存路径改成‘冻结 exe 搬迁前’的旧绝对路径。"""
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)
    orig_start = mgr_mod.LocalTerrainTaskManager.start_tiling
    monkeypatch.setattr(
        mgr_mod.LocalTerrainTaskManager, "start_tiling", lambda self, tid: None
    )
    task_id = mgr.create_task_with_files(name="moved", files=[("a.tif", b"x")], maxzoom=12)
    monkeypatch.setattr(mgr_mod.LocalTerrainTaskManager, "start_tiling", orig_start)

    stale_root = f"/nonexistent_old_location/terrain/local_task_{task_id}"
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE local_terrain_tasks SET source_dir=?, output_dir=?, output_path=? WHERE id=?",
            (stale_root + "/source", stale_root + "/terrain_tiles", stale_root, task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return mgr, task_id


def test_start_tiling_recomputes_paths_from_downloads_dir(monkeypatch, tmp_path):
    db, mgr_mod = _fresh_local_mgr(monkeypatch, tmp_path)
    mgr, task_id = _make_task_with_stale_paths(db, mgr_mod, monkeypatch, tmp_path)

    captured = {}

    def fake_tile(task_dir, out_dir, params):
        captured["task_dir"] = str(task_dir)
        captured["out_dir"] = str(out_dir)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    mgr.start_tiling(task_id)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=30)

    expected_root = Path(tmp_path) / "downloads" / "terrain" / f"local_task_{task_id}"
    assert captured["task_dir"] == str(expected_root / "source"), (
        "start_tiling 应从当前 DOWNLOADS_DIR 重算 source_dir，而非信库存旧路径"
    )
    assert captured["out_dir"] == str(expected_root / "terrain_tiles")
    assert mgr.get_task(task_id)["status"] == "completed"


def test_delete_task_removes_dir_despite_stale_stored_path(monkeypatch, tmp_path):
    """库存 output_path 指向旧位置时，delete_task 仍应删掉当前 DOWNLOADS_DIR 下的目录。"""
    db, mgr_mod = _fresh_local_mgr(monkeypatch, tmp_path)
    mgr, task_id = _make_task_with_stale_paths(db, mgr_mod, monkeypatch, tmp_path)

    real_dir = Path(tmp_path) / "downloads" / "terrain" / f"local_task_{task_id}"
    assert real_dir.exists()

    mgr.delete_task(task_id)

    assert not real_dir.exists(), "delete_task 应按重算路径清理，库存旧路径不应让守卫失效"
    with pytest.raises(ValueError):
        mgr.get_task(task_id)


# ------------------------------------------------- I7: reloader 父进程跳过 create_app

# 在子进程里把 app.py 作为 __main__ 执行，模拟三种启动身份，观察它是否跑了
# create_app 的 orphan recovery（把 running 任务改写成 paused）。
_RUN_AS_MAIN = r"""
import os
import sys

project_root, debug, wrkm, tmp = sys.argv[1:5]
os.environ["DEBUG"] = debug
if wrkm:
    os.environ["WERKZEUG_RUN_MAIN"] = wrkm
else:
    os.environ.pop("WERKZEUG_RUN_MAIN", None)
os.environ.pop("TF_RELOADER_PARENT_PID", None)
sys.path.insert(0, project_root)

from src.core import config
from pathlib import Path
config.Config.BASE_DIR = Path(tmp)          # 运行日志落在 <BASE_DIR>/logs
config.Config.DATABASE_PATH = Path(tmp) / "test.db"
config.Config.DOWNLOADS_DIR = Path(tmp) / "downloads"
config.Config.CACHE_DIR = Path(tmp) / "cache"

from src.core import database
database.init_database()
conn = database.get_connection()
conn.execute(
    "INSERT INTO tasks (name,status,north,south,east,west,zoom_min,zoom_max,"
    "style,output_format,output_path) "
    "VALUES ('t','running',1,0,1,0,0,1,'m','png','/tmp/x')"
)
conn.commit()
conn.close()

# 防止 socketio.run 真的起服务器阻塞
import flask_socketio
flask_socketio.SocketIO.run = lambda self, app, **kw: None

import runpy
runpy.run_path(os.path.join(project_root, "app.py"), run_name="__main__")

conn = database.get_connection()
row = conn.execute("SELECT status FROM tasks WHERE name='t'").fetchone()
conn.close()
print("STATUS:" + row["status"], flush=True)
"""


def _run_app_as_main(tmp_path, debug, werkzeug_run_main):
    proc = subprocess.run(
        [sys.executable, "-c", _RUN_AS_MAIN,
         PROJECT_ROOT, debug, werkzeug_run_main, str(tmp_path)],
        capture_output=True, text=True, timeout=240,
    )
    lines = [l for l in proc.stdout.splitlines() if l.startswith("STATUS:")]
    assert lines, (
        f"runner 未产出 STATUS 行 (rc={proc.returncode})\n"
        f"stdout tail: {proc.stdout[-1500:]}\nstderr tail: {proc.stderr[-1500:]}"
    )
    return lines[0][len("STATUS:"):]


def test_reloader_parent_skips_create_app(tmp_path):
    """dev reloader 的 watcher 父进程（__main__ + DEBUG + 无 WERKZEUG_RUN_MAIN）
    只是文件监听器，绝不能跑 create_app 的 orphan recovery 去改写任务状态。"""
    status = _run_app_as_main(tmp_path, debug="1", werkzeug_run_main="")
    assert status == "running", (
        f"reloader 父进程跑了 create_app，running 任务被误改为 {status!r}"
    )


def test_reloader_child_still_initializes(tmp_path):
    """reloader 子进程（WERKZEUG_RUN_MAIN=true）必须正常初始化——回归保护。"""
    status = _run_app_as_main(tmp_path, debug="1", werkzeug_run_main="true")
    assert status == "paused", f"reloader 子进程未跑 orphan recovery: {status!r}"


def test_non_debug_single_process_still_initializes(tmp_path):
    """非 debug 单进程（冻结 exe 默认路径）必须正常初始化——回归保护。"""
    status = _run_app_as_main(tmp_path, debug="0", werkzeug_run_main="")
    assert status == "paused", f"非 debug 单进程未跑 orphan recovery: {status!r}"
