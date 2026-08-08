import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import geotiff_bytes

# 创建路径用 GDAL 校验上传的是不是可读栅格（2026-08-08 评审 contour P2）。
pytest.importorskip("osgeo.gdal")

_TIF = geotiff_bytes()
_TIF2 = geotiff_bytes(lon0=117.0)


class _FakeUpload:
    """FileStorage 兼容替身：filename 属性 + save() 落盘，供不经过 HTTP
    直接测 manager 的用例使用（manager 不再接受 (filename, bytes) 元组）。"""

    def __init__(self, filename, content):
        self.filename = filename
        self._content = content

    def save(self, dst):
        with open(dst, "wb") as f:
            f.write(self._content)


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    db = importlib.import_module("src.core.database")
    db.init_database()
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    return db, ctm_mod


def _task_dir(task_id):
    from src.core import config
    return Path(config.Config.DOWNLOADS_DIR) / "dem" / f"contour_task_{task_id}"


def test_create_task_with_files_streams_uploads_to_disk(monkeypatch, tmp_path):
    """流式落盘：manager 吃 FileStorage 式对象（filename + save），
    文件内容写进任务目录，contour_files 行记 completed + 实际大小。

    内容必须是真 GeoTIFF：创建路径会用 GDAL 打开求范围并集，非栅格现在直接
    ValueError（以前只记一条 warning、bbox 保持 0、任务照常建成）。"""
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)

    task_id = mgr.create_task_with_files(
        name="up", files=[_FakeUpload("a.tif", _TIF), _FakeUpload("b.tiff", _TIF2)],
        contour_interval=50, zoom_min=12, zoom_max=12)

    task_dir = _task_dir(task_id)
    assert (task_dir / "upload_1_dem.tif").read_bytes() == _TIF
    assert (task_dir / "upload_2_dem.tif").read_bytes() == _TIF2
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT granule_id, status, size_bytes FROM contour_files"
            " WHERE task_id=? ORDER BY granule_id", (task_id,)).fetchall()
    finally:
        conn.close()
    assert [(r["granule_id"], r["status"], r["size_bytes"]) for r in rows] == [
        ("upload_1_dem.tif", "completed", len(_TIF)),
        ("upload_2_dem.tif", "completed", len(_TIF2)),
    ]


def test_create_task_with_files_failure_cleans_up_disk_and_db(monkeypatch, tmp_path):
    """创建中途失败（第二个文件为空）：已落盘的文件和任务目录要清掉，
    DB 行回滚 —— rowid 复用后残留 tif 不能被下个同 id 任务扫进渲染。"""
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.create_task_with_files(
            name="bad", files=[_FakeUpload("a.tif", _TIF), _FakeUpload("b.tif", b"")],
            contour_interval=50, zoom_min=12, zoom_max=12)

    assert not _task_dir(1).exists()
    conn = db.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM contour_tasks").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM contour_files").fetchone()["c"] == 0
    finally:
        conn.close()

    # rowid 复用：下一个任务拿到同一个 id，任务目录里只有它自己的文件
    task_id = mgr.create_task_with_files(
        name="ok", files=[_FakeUpload("c.tif", _TIF)],
        contour_interval=50, zoom_min=12, zoom_max=12)
    assert task_id == 1
    assert sorted(p.name for p in _task_dir(task_id).iterdir()) == ["upload_1_dem.tif"]


def test_list_tasks_limit_clamped_to_at_least_one(monkeypatch, tmp_path):
    """limit=-1 在 SQLite 里是不限行数：钳到 >=1，不能绕过上限拉全表。"""
    db, ctm_mod = _setup(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    for i in range(2):
        mgr.create_task_with_files(
            name=f"t{i}", files=[_FakeUpload("a.tif", _TIF)],
            contour_interval=50, zoom_min=12, zoom_max=12)
    assert len(mgr.list_tasks(limit=-1)) == 1
    assert len(mgr.list_tasks(limit=0)) == 2  # 0/None 走默认值 100
    assert len(mgr.list_tasks(limit=None)) == 2
