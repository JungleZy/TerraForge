"""删除必须如实汇报，且不许留下零引用的产物目录（2026-08-08 评审）。

两条旧行为，两个都不是「没写测试」而是「写了也测不出来」：

P1#6 —— `remove_task_dir_if_safe` 内部是 `shutil.rmtree(..., ignore_errors=True)`，
护栏放行就返回 True。删除的快路径（任务没在跑）把这个返回值直接当成
`DeleteOutcome.files_removed`，也不写 `pending_deletions`。Linux 上跑测试目录
永远删得掉，所以「删成功 → files_removed True」这条断言在新旧实现下都绿 ——
要暴露它必须**制造删不掉**（这里让 rmtree 变成空操作，等价于 Windows 上文件
被资源管理器预览/杀软扫描占住），再断言两件事：报的是 False，而且清单里留下
了行（那是启动清扫唯一的线索）。

DEM `delete_files=false` —— 路由不传 artifact_dir，任务行连同级联的
`dem_terrain_jobs` 一起消失，`<output_path>/dem_task_<id>/` 从此零 DB 引用。
「删完文件还在」这条断言同样在新旧实现下都绿（旧实现本来就不删），所以这里钉
的是**引用**：retained_outputs 里有行，且它真的进了清扫扫描根。
"""

import importlib
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


class _IdleManager:
    """delete_task_row 只用到这三样；active_tasks 空 = 走快路径。"""

    def __init__(self):
        import threading

        self._state_lock = threading.Lock()
        self.active_tasks = {}
        self.stop_flags = {}


def _setup(monkeypatch, tmp_path):
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    cleanup = importlib.import_module("src.services.task_cleanup")
    td = fresh_import(monkeypatch, "src.services.task_deletion")
    return db, td, cleanup


def _seed_map_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_format, output_path, total_tiles) "
            "VALUES ('t', 'completed', 1, 0, 1, 0, 1, 1, 'satellite', 'png', ?, 1)",
            (str(output_path),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _pending(db):
    conn = db.get_connection()
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM pending_deletions")]
    finally:
        conn.close()


def _retained(db):
    conn = db.get_connection()
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM retained_outputs")]
    finally:
        conn.close()


def _break_rmtree(monkeypatch, cleanup):
    """让 rmtree 变成空操作 —— 复刻 Windows 上「文件被占，ignore_errors 吞掉」。"""
    monkeypatch.setattr(
        cleanup, "shutil", types.SimpleNamespace(rmtree=lambda *a, **k: None))


# ---------------------------------------------------------------------------
# P1#6：快路径必须报「真删了」，删不掉就得留下清单行
# ---------------------------------------------------------------------------


def test_fast_path_reports_false_and_queues_when_dir_survives(monkeypatch, tmp_path):
    """删不掉时必须报 False，并且把目录留在 pending_deletions 里。

    旧实现两条都不满足：`files_removed` 抄的是护栏的「可删」返回值（恒 True），
    而入队那一句挂在 `if artifact_dir is not None and running:` 下面，快路径压根
    不写清单 —— 接口回 200 {files_removed: true}，整个瓦片金字塔留在盘上，
    启动清扫也永远收不回。
    """
    db, td, cleanup = _setup(monkeypatch, tmp_path)
    task_id = _seed_map_task(db, tmp_path / "downloads")
    art = tmp_path / "downloads" / f"task_{task_id}"
    art.mkdir(parents=True)
    (art / "0" / "0").mkdir(parents=True)
    (art / "0" / "0" / "0.png").write_bytes(b"x")

    _break_rmtree(monkeypatch, cleanup)

    out = td.delete_task_row(manager=_IdleManager(), task_id=task_id,
                             table="tasks", artifact_dir=art)

    assert out.row_deleted is True
    assert out.files_removed is False, "目录还在，不许报成删掉了"
    assert art.exists(), "前提没成立：rmtree 应该被打断，否则本用例没有区分力"
    assert _pending(db) == [str(art)], "没删掉就必须留下清单行，否则启动清扫收不回"


def test_fast_path_reports_true_and_leaves_no_queue_row(monkeypatch, tmp_path):
    """能删掉时报 True，并且当场销账 —— 正常删除不许在清单里留痕。

    入队提前到 DELETE 同一事务之后，如果忘了销账，每次启动都会重扫一个早就
    不存在的目录并刷 warning。
    """
    db, td, cleanup = _setup(monkeypatch, tmp_path)
    task_id = _seed_map_task(db, tmp_path / "downloads")
    art = tmp_path / "downloads" / f"task_{task_id}"
    art.mkdir(parents=True)
    (art / "a.png").write_bytes(b"x")

    out = td.delete_task_row(manager=_IdleManager(), task_id=task_id,
                             table="tasks", artifact_dir=art)

    assert out.files_removed is True
    assert not art.exists()
    assert _pending(db) == [], "删干净了就要销账"


def test_out_of_bounds_dir_reports_false_and_leaves_no_queue_row(monkeypatch, tmp_path):
    """越界路径报 False，但**不**留清单行 —— 它永远删不掉，留着只会每次启动重试。

    这一条把 files_removed=False 的两种成因分开：占用中（留行，下次补删）与
    越界（清行）。只测其中一种，反向实现（一律留行 / 一律清行）也能绿。
    """
    db, td, cleanup = _setup(monkeypatch, tmp_path)
    task_id = _seed_map_task(db, tmp_path / "downloads")
    # DOWNLOADS_DIR 本身 —— 护栏明确拒绝
    art = tmp_path / "downloads"

    out = td.delete_task_row(manager=_IdleManager(), task_id=task_id,
                             table="tasks", artifact_dir=art)

    assert out.row_deleted is True
    assert out.files_removed is False
    assert art.exists(), "护栏必须挡住 DOWNLOADS_DIR"
    assert _pending(db) == [], "永远删不掉的路径不该赖在清单里"


def test_row_missing_touches_neither_disk_nor_queue(monkeypatch, tmp_path):
    """行不存在时一片磁盘都不能碰，也不能入队。

    入队提前到快路径之后，这道「404 不许静默真删」的闸门必须仍然在入队【之前】
    生效，否则删一个不存在的 task_id 会把同名残留目录排进启动清扫。
    """
    db, td, cleanup = _setup(monkeypatch, tmp_path)
    art = tmp_path / "downloads" / "task_999"
    art.mkdir(parents=True)

    out = td.delete_task_row(manager=_IdleManager(), task_id=999,
                             table="tasks", artifact_dir=art)

    assert out.row_deleted is False
    assert out.files_removed is None
    assert art.exists(), "404 的同时静默真删"
    assert _pending(db) == []


def test_unknown_table_name_is_rejected(monkeypatch, tmp_path):
    """表名直接拼进 SQL —— 白名单之外一律抛，不留「文档约定」这种口头契约。"""
    import pytest

    db, td, cleanup = _setup(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        td.delete_task_row(manager=_IdleManager(), task_id=1,
                           table="tasks; DROP TABLE tasks", artifact_dir=None)
    # 五条管线（四条内建 + 插件共用的 plugin_tasks）用的表名必须都在白名单里，
    # 少一张就是整条管线删不掉
    assert td._DELETABLE_TASK_TABLES == {
        "tasks", "dem_tasks", "contour_tasks", "local_terrain_tasks",
        "plugin_tasks"}


# ---------------------------------------------------------------------------
# DEM delete_files=false：不许留下零引用的产物目录
# ---------------------------------------------------------------------------


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


def _seed_dem_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dem_tasks (name, status, north, south, east, west, "
            "dataset, output_path, total_files, downloaded_files, failed_files) "
            "VALUES ('dem', 'paused', 1, 0, 1, 0, 'ASTGTM.003', ?, 0, 0, 0)",
            (str(output_path),),
        )
        task_id = cur.lastrowid
        # 切片作业行是这个目录**唯一**的 DB 引用（output_dir 的父级就是它），
        # 而它挂着 ON DELETE CASCADE —— 删任务行时会一起消失。
        cur.execute(
            "INSERT INTO dem_terrain_jobs (task_id, status, output_dir, maxzoom) "
            "VALUES (?, 'paused', ?, 12)",
            (task_id, str(Path(output_path) / f"dem_task_{task_id}" / "terrain_tiles")),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_dem_delete_keeping_files_leaves_a_reference(monkeypatch, tmp_path):
    """delete_files=false 删完，产物目录必须仍然被 DB 引用得到。

    「文件还在」不是有效断言 —— 旧实现本来就一个字节都不删，那条在两边都绿。
    有区分力的是引用：旧实现里任务行一走，级联掉的 dem_terrain_jobs 是这个目录
    唯一的引用，从此 _materialised_sweep_roots 推不出它，目录直下那个与源数据
    同量级的物化中间栅格也永远回收不了。
    """
    out_root = tmp_path / "downloads"
    out_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    cleanup = importlib.import_module("src.services.task_cleanup")

    task_id = _seed_dem_task(db, out_root)
    art = out_root / f"dem_task_{task_id}"
    (art / "terrain_tiles").mkdir(parents=True)
    (art / "terrain_tiles" / "layer.json").write_text("{}")
    # 物化中间栅格：它就落在产物目录直下，是「引用断了」最贵的那部分代价
    materialised = art / f"cesium_terrain_{os.getpid()}_x.tif"
    materialised.write_bytes(b"x")

    resp = client.delete(f"/api/dem/tasks/{task_id}")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "files_removed" not in body, "没要求删文件就不该给出删除结论"
    assert body.get("files_retained_path") == str(art), \
        "保留的产物目录必须在响应里说清楚，否则用户无从找回"

    assert materialised.exists() and (art / "terrain_tiles" / "layer.json").exists(), \
        "用户选了保留，一个字节都不许动"
    assert _retained(db) == [str(art)], "任务行没了，登记表是这个目录唯一的引用"
    assert _pending(db) == [], "登记 != 待删；绝不能把它排进启动清扫的删除队列"

    roots = [str(p) for p in cleanup._materialised_sweep_roots()]
    assert str(art) in roots, \
        "登记表必须真的接回扫描根，否则它就只是一张没人读的表"


def test_dem_delete_with_files_does_not_record_a_retained_row(monkeypatch, tmp_path):
    """delete_files=true 走的是删除，不是保留 —— 不许往登记表里写。"""
    out_root = tmp_path / "downloads"
    out_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    task_id = _seed_dem_task(db, out_root)
    art = out_root / f"dem_task_{task_id}"
    (art / "terrain_tiles").mkdir(parents=True)

    resp = client.delete(f"/api/dem/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("files_removed") is True
    assert not art.exists()
    assert _retained(db) == []
    assert _pending(db) == []


def test_map_delete_keeping_files_leaves_a_reference(monkeypatch, tmp_path):
    """map 的 delete_files=false 同样不许留下零引用目录。

    「文件还在」在新旧实现下都绿（旧实现本来就不删），钉的是 retained_outputs
    里那条引用。
    """
    out_root = tmp_path / "downloads"
    out_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    task_id = _seed_map_task(db, out_root)
    art = out_root / f"task_{task_id}"
    art.mkdir(parents=True)
    (art / "0.png").write_bytes(b"x")

    resp = client.delete(f"/api/tasks/{task_id}")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("files_retained_path") == str(art)
    assert (art / "0.png").exists(), "用户选了保留，一个字节都不许动"
    assert _retained(db) == [str(art)]
    assert _pending(db) == [], "登记 != 待删"


def test_local_terrain_delete_keeping_files_leaves_a_reference(monkeypatch, tmp_path):
    """本地地形的 delete_files=false（它的默认是 true，前端会显式传 false）同理。

    这条管线的产物路径不读库存 output_path，而是按固定布局从当前
    DOWNLOADS_DIR 重算 —— 登记的必须是同一个口径的路径，否则引用指向的目录
    根本不存在。
    """
    (tmp_path / "downloads" / "terrain").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_terrain_tasks (name, status, output_path, "
            "source_dir, output_dir, total_files, maxzoom) "
            "VALUES ('lt', 'completed', ?, ?, ?, 1, 12)",
            (str(tmp_path / "downloads" / "terrain"),
             str(tmp_path / "downloads" / "terrain" / "local_task_1" / "source"),
             str(tmp_path / "downloads" / "terrain" / "local_task_1" / "terrain_tiles")),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    art = tmp_path / "downloads" / "terrain" / f"local_task_{task_id}"
    (art / "terrain_tiles").mkdir(parents=True)
    (art / "terrain_tiles" / "layer.json").write_text("{}")

    resp = client.delete(f"/api/terrain/local/tasks/{task_id}?delete_files=false")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("files_retained_path") == str(art)
    assert (art / "terrain_tiles" / "layer.json").exists()
    assert _retained(db) == [str(art)]
    assert _pending(db) == []


def test_retained_row_retires_when_the_directory_is_gone(monkeypatch, tmp_path):
    """用户手工删掉目录之后，登记行要自己退休 —— 否则这张表无界增长。"""
    db, td, cleanup = _setup(monkeypatch, tmp_path)
    alive = tmp_path / "downloads" / "dem_task_1"
    alive.mkdir(parents=True)
    gone = tmp_path / "downloads" / "dem_task_2"

    assert cleanup.record_retained_output(alive) is True
    assert cleanup.record_retained_output(gone) is True
    assert cleanup.record_retained_output("relative/dir") is False, \
        "相对路径按进程 cwd 解释，登记它等于给扫描根埋一个随机目录"

    roots = [str(p) for p in cleanup._retained_output_roots()]

    assert roots == [str(alive)]
    assert _retained(db) == [str(alive)], "已经不存在的目录，引用没有意义了"


# ---------------------------------------------------------------------------
# `.part.` 归属判定：线程 id 不是 pid
# ---------------------------------------------------------------------------


def test_part_owner_pid_rejects_thread_ident_shaped_values():
    """`.part.<thread_ident>` 不许被当成 pid。

    task_manager._stream_copy_tile 现在写的是 `.part.<pid>.<thread_ident>`（本次
    评审改的），但改名之前它写的是 `.part.<threading.get_ident()>`，形态与
    `.part.<pid>` 一模一样，而用户盘上那批存量文件不会自己消失。CPython 在
    Linux/macOS 上的 ident 是 pthread_t 指针（量级 1e14），拿它去和活进程表比对
    得出的是反向结论。
    """
    import threading

    from src.services.task_cleanup import _part_owner_pid

    assert _part_owner_pid("y.png.part.4321.99") == 4321
    assert _part_owner_pid("out.tif.part.4321") == 4321, "拼接产物只有 pid 一段"
    assert _part_owner_pid(f"y.png.part.{threading.get_ident()}") is None or \
        threading.get_ident() < 2 ** 31, \
        "Linux/macOS 的线程 id 必须被判成「归属未知」"
    assert _part_owner_pid("y.png.part.abc") is None
    assert _part_owner_pid("y.png") is None


def _seed_contour_task(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contour_tasks (name, status, north, south, east, west, "
            "dataset, contour_interval, zoom_min, zoom_max, output_path) "
            "VALUES ('c', 'paused', 1, 0, 1, 0, 'upload', 50, 10, 12, ?)",
            (str(output_path),),
        )
        task_id = cur.lastrowid
        # contour_files 挂 ON DELETE CASCADE —— 与 dem_terrain_jobs 同理，
        # 删任务行时这个目录的最后一个 DB 引用就没了。
        cur.execute(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, local_path) "
            "VALUES (?, 'upload_1_dem.tif', 'dem', 'completed', ?)",
            (task_id, str(Path(output_path) / f"contour_task_{task_id}" / "upload_1_dem.tif")),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_contour_delete_keeping_files_leaves_a_reference(monkeypatch, tmp_path):
    """第四条管线补齐：contour 的 delete_files=false 也不许留下零引用目录。

    「文件还在」在新旧实现下都绿（旧实现本来就不删），钉的是 retained_outputs
    里那条引用。contour 是四条里最后接上这套机制的一条 —— 它的删除路由当时
    正在被另一处改动占用，所以单独补。
    """
    out_root = tmp_path / "downloads" / "dem"
    out_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    task_id = _seed_contour_task(db, out_root)
    art = out_root / f"contour_task_{task_id}"
    (art / "10" / "1").mkdir(parents=True)
    (art / "10" / "1" / "2.png").write_bytes(b"x")

    resp = client.delete(f"/api/contour/tasks/{task_id}")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("files_retained_path") == str(art)
    assert (art / "10" / "1" / "2.png").exists(), "用户选了保留，一个字节都不许动"
    assert _retained(db) == [str(art)]
    assert _pending(db) == [], "登记 != 待删"


def test_contour_delete_with_files_does_not_record_a_retained_row(monkeypatch, tmp_path):
    """delete_files=true 走的是删除，不是保留 —— 不许往登记表里写。"""
    out_root = tmp_path / "downloads" / "dem"
    out_root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    _, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    task_id = _seed_contour_task(db, out_root)
    art = out_root / f"contour_task_{task_id}"
    art.mkdir(parents=True)
    (art / "meta.json").write_text("{}", encoding="utf-8")

    resp = client.delete(f"/api/contour/tasks/{task_id}?delete_files=true")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("files_removed") is True
    assert not art.exists()
    assert _retained(db) == []
    assert _pending(db) == []
