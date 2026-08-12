import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in (
        "app",
        "src.core.database",
        "src.services.task_manager",
        "src.services.dem_task_manager",
        "src.routes.api",
        "src.routes.dem_api",
        "src.routes.contour_api",
    ):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed_map_task(db, output_path, status="completed"):
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
                    'tiles_only', ?, 0, 0, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_dem_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dem_tasks
              (name, status, north, south, east, west, dataset, output_path,
               total_files, downloaded_files, failed_files)
            VALUES ('dem-task', ?, 1, 0, 1, 0, 'ASTGTM.003', ?, 0, 0, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _seed_contour_task(db, output_path, status="completed"):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks
              (name, status, north, south, east, west, dataset,
               contour_interval, zoom_min, zoom_max, output_path, total_files)
            VALUES ('contour-task', ?, 1, 0, 1, 0, 'ASTGTM.003',
                    50, 12, 14, ?, 0)
            """,
            (status, str(output_path)),
        )
        task_id = cur.lastrowid
        conn.commit()
        return task_id
    finally:
        conn.close()


def _make_artifact(dir_path):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "artifact.bin").write_bytes(b"payload")
    return dir_path


def _task_row(db, table, task_id):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE id=?", (task_id,))
        return cur.fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DELETE ...?delete_files=true：删除任务时可选清理磁盘产物
#
# 安全边界：只删 resolve 后严格位于 Config.DOWNLOADS_DIR 之内的任务目录；
# 产物落在 DOWNLOADS_DIR 之外（用户自定义 output_path）时跳过文件删除、
# 但 DB 记录照常删除。共享瓦片缓存 cache/ 绝不可删。
# ---------------------------------------------------------------------------


def test_delete_map_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    task_id = _seed_map_task(db, output_path=config.Config.DOWNLOADS_DIR)
    artifact = _make_artifact(Path(config.Config.DOWNLOADS_DIR) / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "tasks", task_id) is None


def test_delete_map_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    task_id = _seed_map_task(db, output_path=config.Config.DOWNLOADS_DIR)
    artifact = _make_artifact(Path(config.Config.DOWNLOADS_DIR) / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "tasks", task_id) is None


def test_delete_map_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files=true 也删
    (护栏:非符号链接、深度足够、非根目录/家目录/cache)"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("src.core.database")
    task_id = _seed_map_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"task_{task_id}")

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
    assert _task_row(db, "tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_dem_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_dem_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "dem_tasks", task_id) is None


def test_delete_dem_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_dem_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "dem_tasks", task_id) is None


def test_delete_dem_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files 也删"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("src.core.database")
    task_id = _seed_dem_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"dem_task_{task_id}")

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=1")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
    assert _task_row(db, "dem_tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_contour_task_with_delete_files_removes_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_contour_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=yes")

    assert resp.status_code == 200
    assert not artifact.exists(), "delete_files=true 应删除产物目录"
    assert _task_row(db, "contour_tasks", task_id) is None


def test_delete_contour_task_default_keeps_artifacts(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    dem_root = Path(config.Config.DOWNLOADS_DIR) / "dem"
    task_id = _seed_contour_task(db, output_path=dem_root)
    artifact = _make_artifact(dem_root / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}")

    assert resp.status_code == 200
    assert artifact.exists(), "缺省 delete_files=false 必须保留产物目录"
    assert _task_row(db, "contour_tasks", task_id) is None


def test_delete_contour_task_outside_downloads_dir_removes_files(monkeypatch, tmp_path):
    """0.2.4 护栏放开:DOWNLOADS_DIR 之外的注册任务目录,delete_files 也删"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"
    db = importlib.import_module("src.core.database")
    task_id = _seed_contour_task(db, output_path=outside)
    artifact = _make_artifact(outside / f"contour_task_{task_id}")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert not artifact.exists(), "注册任务目录即使在 DOWNLOADS_DIR 之外,delete_files=true 也应删除"
    assert _task_row(db, "contour_tasks", task_id) is None, "DB 记录仍应删除"


def test_delete_map_task_legacy_relative_output_path_removes_artifacts(monkeypatch, tmp_path):
    """存量行的相对 output_path:delete_files=true 时必须按【与读侧同一套】口径
    归一后删除产物。

    真实存量形态是 './downloads/map' —— commit 38e3e30fc 之前表单默认值硬编码
    成它并原样入库。M10 之前这个值有两套解释:写/删除侧一律拼到 DOWNLOADS_DIR
    下(→ <BASE>/downloads/downloads/map,不存在),读侧做前缀剥离
    (→ <BASE>/downloads/map,真实产物所在)。于是「删除并删文件」删了个寂寞却
    照回 200 success,恢复任务续下的瓦片还会写到第三个地方去。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    # 存量行:相对路径原始值(旧版本 create_task 入库的形态)
    task_id = _seed_map_task(db, output_path="./downloads/legacy_out")
    artifact = _make_artifact(
        Path(config.Config.DOWNLOADS_DIR) / "legacy_out" / f"task_{task_id}"
    )

    resp = client.delete(f"/api/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200
    assert resp.get_json().get("files_removed") is True
    assert not artifact.exists(), (
        "相对路径存量行必须按读侧同一套口径归一后再删产物"
    )
    assert _task_row(db, "tasks", task_id) is None


def test_stored_output_path_resolves_identically_on_read_and_write_sides(monkeypatch, tmp_path):
    """M10 的核心契约：同一个存量 output_path，读侧与写/删除侧必须解析到同一处。

    分叉的代价不是「路径不好看」，而是产物分裂成两处：删除删不到、
    /tiles/<id>/ 找不到新下的瓦片。
    """
    _load_app(monkeypatch, tmp_path)
    from src.services.task_cleanup import resolve_stored_output_dir
    from src.routes.terrain_static import _resolve_config_path

    for stored in ("./downloads/map", "downloads/map", "./downloads", "downloads",
                   "legacy_out", str(tmp_path / "abs" / "out")):
        write_side = resolve_stored_output_dir(stored).resolve()
        read_side = _resolve_config_path(stored)
        assert write_side == read_side, (
            f"{stored!r} 在两侧解析不一致: 写/删除侧 {write_side} vs 读侧 {read_side}")


# ---------------------------------------------------------------------------
# remove_task_dir_if_safe 护栏（0.2.4 全盘放开后重定的边界）
# ---------------------------------------------------------------------------


def _cleanup_mod(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    import src.services.task_cleanup as tc
    return tc


def test_cleanup_allows_registered_dir_outside_downloads(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    artifact = tmp_path / "elsewhere" / "task_1"
    artifact.mkdir(parents=True)
    (artifact / "f.txt").write_text("x")

    assert tc.remove_task_dir_if_safe(artifact) is True
    assert not artifact.exists()


def test_cleanup_refuses_symlink_component(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    real = tmp_path / "real" / "task_1"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        import pytest
        pytest.skip(f"无法创建符号链接: {e}")

    assert tc.remove_task_dir_if_safe(link / "task_1") is False
    assert real.exists(), "符号链接路径必须拒删,真实目录不能被动到"


def test_cleanup_refuses_shallow_and_home(monkeypatch, tmp_path):
    """H4/M22:守卫回归时本用例必须在【删除发生之前】翻红。

    旧写法直接把真实 `Path.home()` 喂给 remove_task_dir_if_safe —— 家目录只有
    task_cleanup.py 那一条守卫兜底,它一旦被改坏(合并条件、调整顺序都可能),
    控制流会一路走到 shutil.rmtree 把开发者或 CI runner 的家目录删掉,然后才
    因返回 True 让断言变红:捕获回归的手段是先造成灾难。CI 上三平台的
    checkout 都位于 HOME 之内,守卫一坏连工作区一起端掉。

    改为把 HOME/USERPROFILE 指到 tmp_path 下(Path.home() 跟随环境变量,断言
    效力不减),并把 rmtree 换成 spy 做第二道兜底 —— 风险归零。
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows 上 Path.home() 读它
    tc = _cleanup_mod(monkeypatch, tmp_path)

    rmtree_calls = []
    monkeypatch.setattr(tc.shutil, "rmtree", lambda *a, **k: rmtree_calls.append(a))

    assert tc.remove_task_dir_if_safe(Path(os.path.abspath(os.sep))) is False
    assert tc.remove_task_dir_if_safe(Path.home()) is False
    # M22:上面两条对【浅路径守卫】没有区分力 —— 把 task_cleanup 的 parts<3
    # 判断删掉后,'/' 会被紧随其后的「DOWNLOADS_DIR 祖先」那条等价兜住,用例
    # 照样绿。一级路径探针只有浅路径守卫能挡(它不存在,即使守卫失效也不会
    # 真删掉什么,但变异时会翻红)。
    probe = Path(os.path.abspath(os.sep)) / "tf_shallow_probe"
    assert tc.remove_task_dir_if_safe(probe) is False

    assert rmtree_calls == [], f"护栏命中时绝不该调用 rmtree,实际被调用: {rmtree_calls}"


def test_cleanup_refuses_cache_and_downloads_root(monkeypatch, tmp_path):
    tc = _cleanup_mod(monkeypatch, tmp_path)
    assert tc.remove_task_dir_if_safe(tmp_path / "downloads") is False
    assert tc.remove_task_dir_if_safe(tmp_path / "cache") is False
    assert tc.remove_task_dir_if_safe(tmp_path) is False, "包含 cache 的上级目录也拒删"


def test_init_database_normalizes_legacy_relative_output_paths(monkeypatch, tmp_path):
    """M10：存量相对 output_path 在启动时被一次性归一成绝对路径。

    收敛解析口径只解决「以后」；不把「以前」的行也拉齐的话，解析歧义会永久
    保留在数据里（受影响的是 38e3e30fc 之前建的任务行）。用 PRAGMA
    user_version 做幂等标记，重复启动不再全表扫描。
    """
    _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")

    task_id = _seed_map_task(db, output_path="./downloads/legacy_out")

    conn = db.get_connection()
    try:
        # 模拟旧库：把迁移标记退回，让归一化重新执行
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        cur = conn.cursor()
        changed = db.normalize_stored_output_paths(cur)
        conn.commit()
        row = conn.execute(
            "SELECT output_path FROM tasks WHERE id=?", (task_id,)).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert changed == 1
    expected = str(Path(config.Config.DOWNLOADS_DIR) / "legacy_out")
    assert row["output_path"] == expected, (
        f"存量相对路径未被归一: {row['output_path']!r} != {expected!r}")
    assert version == 2, "归一后必须打上 user_version 标记，否则每次启动都全表扫"


def test_output_path_normalization_is_idempotent(monkeypatch, tmp_path):
    """已经是绝对路径的行不得被再次改写（重复拼接会越走越深）。"""
    _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    absolute = str(tmp_path / "somewhere" / "out")
    task_id = _seed_map_task(db, output_path=absolute)

    conn = db.get_connection()
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        cur = conn.cursor()
        db.normalize_stored_output_paths(cur)
        conn.commit()
        row = conn.execute(
            "SELECT output_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    assert row["output_path"] == absolute


# ---------- M6：后台收尾用的快照可能放旧 600 秒 ----------
#
# `_background_cleanup` 先 join 工作线程(上限 _JOIN_TIMEOUT_SECONDS = 600 秒)
# 再清独占缓存,而快照是 DELETE 之前拍的。这段窗口里新建并启动的任务不在快照
# 里,于是它正在写的瓦片被算成「被删任务的独占集」而删掉,受害任务随后在拼接
# 阶段抛 FileNotFoundError。修复:动手之前重查存活任务行,与旧快照取**并集**。

def _probe_snapshot():
    from src.contracts.source import SourceSnapshot
    return SourceSnapshot(source_id="probe", url_template="https://x/{z}/{x}/{y}.png",
                          style="m", server_list=("https://x",))


def _region_task_row(task_id, north, south, east, west, zoom):
    return {"id": task_id, "north": north, "south": south, "east": east,
            "west": west, "zoom_min": zoom, "zoom_max": zoom, "style": "m",
            "region_spec": "", "source_snapshot": _probe_snapshot().to_json(),
            "status": "running"}


def _insert_region_task(db, row):
    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO tasks
              (id, name, status, north, south, east, west, zoom_min, zoom_max,
               style, output_format, output_path, total_tiles, downloaded_tiles,
               failed_tiles, source_snapshot)
            VALUES (?, 'probe', ?, ?, ?, ?, ?, ?, ?, 'm', 'tiles_only', '', 0, 0, 0, ?)
            """,
            (row["id"], row["status"], row["north"], row["south"], row["east"],
             row["west"], row["zoom_min"], row["zoom_max"], row["source_snapshot"]),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_shared_tile(cache_root, victim_row, zoom):
    """在受害任务确实覆盖的坐标上放一块瓦片,返回它的路径。"""
    from src.contracts.region import RegionSpec
    from src.contracts.region_tiles import iter_region_tile_spans
    y, x, _x1 = next(iter(iter_region_tile_spans(RegionSpec.from_row(victim_row), zoom)))
    d = (Path(cache_root) / _probe_snapshot().cache_namespace / str(zoom) / str(x))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{y}.png"
    p.write_bytes(b"live" * 8)
    return p


def _finished_thread():
    import threading
    th = threading.Thread(target=lambda: None)
    th.start()
    th.join()
    return th


def test_background_cleanup_spares_task_created_after_snapshot(monkeypatch, tmp_path):
    """快照拍完之后才出现的任务,它的瓦片必须活下来。

    这是 M6 的回归:旧代码把 600 秒前的 other_rows 原样拿去算独占集,新任务
    不在减数里 —— 它正在下载的瓦片被当成「只有被删任务在用」而 unlink,
    受害任务的 stitch 随后 FileNotFoundError,整层失败。
    """
    _load_app(monkeypatch, tmp_path)
    from src.core import config
    db = importlib.import_module("src.core.database")
    from src.services import task_deletion

    deleted = _region_task_row(1, 30.5, 30.0, 114.5, 114.0, 12)
    newcomer = _region_task_row(2, 30.4, 30.1, 114.4, 114.1, 12)

    # 删行时刻的快照:表里只有被删任务自己(它自己那一行也在快照里,
    # exclusive_tile_rects 按 id 排除)。
    stale_scope = (deleted, [deleted])
    # 窗口里冒出来的新任务:它已经在往 cache 里写了。
    _insert_region_task(db, newcomer)
    tile = _seed_shared_tile(config.Config.CACHE_DIR, newcomer, 12)

    task_deletion._background_cleanup(1, _finished_thread(), None, None, stale_scope)

    assert tile.exists(), "新任务正在用的瓦片被当成被删任务的独占集清掉了"


def test_background_cleanup_still_deletes_when_nobody_else_claims_the_tile(
        monkeypatch, tmp_path):
    """对照组:没有新任务时,同一块瓦片照删 —— 上一条用例不是靠「什么都不删」通过的。"""
    _load_app(monkeypatch, tmp_path)
    from src.core import config
    from src.services import task_deletion

    deleted = _region_task_row(1, 30.5, 30.0, 114.5, 114.0, 12)
    stale_scope = (deleted, [deleted])
    tile = _seed_shared_tile(config.Config.CACHE_DIR, deleted, 12)

    task_deletion._background_cleanup(1, _finished_thread(), None, None, stale_scope)

    assert not tile.exists()


def test_refresh_cache_scope_keeps_rows_that_vanished_from_the_table(monkeypatch, tmp_path):
    """取并集而不是替换:窗口里被删掉的那个任务仍留在保护集里。

    多保护几块瓦片的代价是「下次同区域下载命中缓存」;多删的代价是删掉别人
    正在用的文件。模块自己定的边界就是「宁可少删,不可多删」。
    """
    _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    from src.services import task_deletion

    deleted = _region_task_row(1, 30.5, 30.0, 114.5, 114.0, 12)
    gone = _region_task_row(9, 30.4, 30.2, 114.4, 114.2, 12)  # 只存在于旧快照
    newcomer = _region_task_row(2, 30.3, 30.1, 114.3, 114.1, 12)
    _insert_region_task(db, newcomer)

    _row, merged = task_deletion._refresh_cache_scope((deleted, [deleted, gone]))

    assert sorted(task_deletion._row_id(r) for r in merged) == [1, 2, 9]


def test_refresh_cache_scope_falls_back_to_snapshot_when_query_fails(monkeypatch, tmp_path):
    """重查失败不能把清理整个搞砸:退回旧快照(只会少删,不会多删)。"""
    _load_app(monkeypatch, tmp_path)
    from src.services import cache_exclusive, task_deletion

    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cache_exclusive, "surviving_task_rows", boom)
    scope = (_region_task_row(1, 1, 0, 1, 0, 5), [])
    assert task_deletion._refresh_cache_scope(scope) is scope
    assert task_deletion._refresh_cache_scope(None) is None
