"""删除任务时的进度回报。

勾了「同时删除磁盘产物」的删除是**同步**的：后端在请求线程里 rmtree 整个瓦片
金字塔。大任务几万到上百万个文件，Windows 上要几十秒到几分钟，而在此之前界面
上没有任何动静 —— 确认框一关就是一片死寂，用户以为没点上、再点一次。

改造后 services/task_cleanup 提供一条带回报的删除路径，services/task_deletion
把它接到 socket 的 `task_delete_progress` 事件上。本文件钉的是这两件事：
① 带回报的删除与 rmtree 语义等价（删干净、不跟符号链接、单个文件删不掉不抛）；
② 事件真的发出去了，且分母/终态/任务归属都对。
"""

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402

# 不碰 Config 的那几条用普通导入即可 —— fresh_import 需要 monkeypatch，且只在
# 「模块导入时把 Config 值绑进自己命名空间」的场景下才有意义。
import src.services.task_cleanup as task_cleanup_mod  # noqa: E402
import src.services.task_deletion as task_deletion_mod  # noqa: E402


class _RecordingSocketIO:
    """只记事件，不连网络。四个 manager 的 socketio 位就是这个形状。"""

    def __init__(self):
        self.events = []

    def emit(self, name, payload=None):
        self.events.append((name, payload))


class _FakeManager:
    """delete_task_row 按名字用到的四样东西（socketio 可选，与真 manager 一致）。"""

    def __init__(self, socketio=None):
        self._state_lock = threading.Lock()
        self.active_tasks = {}
        self.stop_flags = {}
        if socketio is not None:
            self.socketio = socketio


def _build_tree(root: Path, *, zooms=2, cols=3, rows=4) -> int:
    """造一棵 {z}/{x}/{y}.png 的小金字塔，返回**条目数**（文件 + 目录，含 root）。"""
    root.mkdir(parents=True)
    files = 0
    dirs = 1
    for z in range(zooms):
        (root / str(z)).mkdir()
        dirs += 1
        for x in range(cols):
            d = root / str(z) / str(x)
            d.mkdir()
            dirs += 1
            for y in range(rows):
                (d / f"{y}.png").write_bytes(b"x")
                files += 1
    return files + dirs


# ---------------------------------------------------------------- 删除语义

def test_reporting_removal_matches_rmtree_and_counts_every_entry(tmp_path):
    """带回报的删除要把整棵树删干净，且分子分母都按「文件 + 目录」计。

    口径必须两阶段一致：拿文件数当分母、却把 rmdir 也算进分子，百分比会在末尾
    冲过 100（瓦片金字塔的目录数是文件数的百分之几，肉眼可见）。
    """
    tc = task_cleanup_mod
    tree = tmp_path / "task_1"
    expected = _build_tree(tree)

    events = []
    tc._rmtree_reporting(tree, lambda phase, done, total: events.append((phase, done, total)))

    assert not tree.exists(), "带回报的删除必须真的把树删掉"
    scan = [e for e in events if e[0] == "scan"]
    delete = [e for e in events if e[0] == "delete"]
    assert scan and delete, "两个阶段各自至少要有一帧（末帧强制发）"
    assert scan[-1] == ("scan", expected, expected)
    assert delete[-1] == ("delete", expected, expected)


def test_reporting_removal_never_follows_a_symlink_out_of_the_tree(tmp_path):
    """树里的符号链接只删链接本身。

    判据必须是 `is_dir(follow_symlinks=False)`（与 shutil.rmtree 逐字相同）。
    跟进的后果是顺着链接把别处的目录删空 —— 而 remove_task_dir_if_safe 的护栏
    只查 task_dir 自身的各层，管不到树**内部**冒出来的链接。
    """
    tc = task_cleanup_mod
    tree = tmp_path / "task_1"
    tree.mkdir()
    (tree / "a.png").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.tif").write_bytes(b"keep me")
    os.symlink(outside, tree / "link")

    tc._rmtree_reporting(tree, lambda *a: None)

    assert not tree.exists()
    assert (outside / "precious.tif").exists(), "跟着链接删到树外面去了"


def test_reporting_removal_does_not_raise_when_an_entry_cannot_be_removed(
        tmp_path, monkeypatch):
    """单个条目删不掉只是跳过 —— 与 `rmtree(ignore_errors=True)` 同一条约定。

    Windows 上任意一个瓦片被资源管理器预览/杀软占住就会走到这里。让它抛出去的
    后果是删除半途中断：任务行已经没了，产物目录半空，而 pending_deletions 的
    补删要等到下次启动。
    """
    tc = task_cleanup_mod
    tree = tmp_path / "task_1"
    tree.mkdir()
    (tree / "locked.png").write_bytes(b"x")
    (tree / "free.png").write_bytes(b"x")

    real_unlink = os.unlink

    def picky_unlink(path, *args, **kwargs):
        if str(path).endswith("locked.png"):
            raise PermissionError("file in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(tc.os, "unlink", picky_unlink)

    events = []
    tc._rmtree_reporting(tree, lambda *a: events.append(a))

    assert (tree / "locked.png").exists(), "删不掉的那个应该留在盘上"
    assert not (tree / "free.png").exists(), "一个删不掉不该让其余的也跳过"
    assert events[-1][0] == "delete", "中断了就没有终帧"


def test_guard_keeps_using_rmtree_when_nobody_asked_for_progress(tmp_path, monkeypatch):
    """没有 progress_cb 就不走带回报那条路。

    这条是**性能**契约，不是风格：带回报的删除要先扫一遍数分母，等于多遍历一次
    整棵树。启动补删、手动清理这些没人看进度的调用方不该付这份钱。
    """
    from src.core import config
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir()
    (tmp_path / "cache").mkdir()
    tc = fresh_import(monkeypatch, "src.services.task_cleanup")

    tree = tmp_path / "downloads" / "task_1"
    _build_tree(tree)

    calls = []
    real_rmtree = tc.shutil.rmtree
    monkeypatch.setattr(tc.shutil, "rmtree",
                        lambda p, **kw: (calls.append(p), real_rmtree(p, **kw))[1])
    monkeypatch.setattr(tc, "_rmtree_reporting",
                        lambda *a, **kw: pytest.fail("不该走带回报的删除"))

    assert tc.remove_task_dir_if_safe(tree) is True
    assert calls, "无回调时必须仍是 shutil.rmtree"
    assert not tree.exists()


def test_confirm_helper_forwards_the_callback(tmp_path, monkeypatch):
    """remove_task_dir_and_confirm 要把回调透下去，且返回值语义不变。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir()
    (tmp_path / "cache").mkdir()
    tc = fresh_import(monkeypatch, "src.services.task_cleanup")

    tree = tmp_path / "downloads" / "task_1"
    expected = _build_tree(tree)

    events = []
    outcome = tc.remove_task_dir_and_confirm(
        tree, progress_cb=lambda *a: events.append(a))

    assert outcome == tc.DirRemoval(True, True)
    assert events[-1] == ("delete", expected, expected)


# ---------------------------------------------------------------- 事件广播

def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    td = fresh_import(monkeypatch, "src.services.task_deletion")
    return db, td


def _seed(db):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (id, name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_format, output_path, total_tiles) "
            "VALUES (1, 't', 'paused', 1, 0, 1, 0, 1, 1, 'satellite', 'png', ?, 1)",
            ("/tmp/x",),
        )
        conn.commit()
    finally:
        conn.close()


def test_delete_broadcasts_progress_and_a_terminal_frame(monkeypatch, tmp_path):
    """快路径删产物时必须发 task_delete_progress，且末帧带 done=True。

    末帧是承重的：前端的进度框只认它把百分比推到 100，没有它就永远停在最后一次
    节流帧上（条目数不是节流步长整数倍时那正是 97% 卡住不动的形态）。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    art = tmp_path / "downloads" / "task_1"
    expected = _build_tree(art)
    sio = _RecordingSocketIO()

    out = td.delete_task_row(manager=_FakeManager(sio), task_id=1, table="tasks",
                             artifact_dir=art)

    assert out.files_removed is True
    frames = [p for name, p in sio.events if name == td.DELETE_PROGRESS_EVENT]
    assert frames, "删产物却一帧进度都没发"
    assert all(f["task_id"] == 1 and f["task_type"] == "map" for f in frames)
    assert {f["phase"] for f in frames} == {"scan", "delete"}
    last = frames[-1]
    assert last == {"task_id": 1, "task_type": "map", "phase": "delete",
                    "removed": expected, "total": expected, "done": True}
    assert not any(f["done"] for f in frames[:-1]), "done 只能出现在末帧"


def test_no_progress_frames_when_files_are_kept(monkeypatch, tmp_path):
    """没勾「同时删除磁盘产物」时一帧都不该发。

    那条路只有一条 DELETE + 一次 stat，毫秒级返回。发进度等于让前端为一个瞬间
    完成的请求弹一个闪一下的框。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    sio = _RecordingSocketIO()

    td.delete_task_row(manager=_FakeManager(sio), task_id=1, table="tasks",
                       artifact_dir=None)

    assert [e for e in sio.events if e[0] == td.DELETE_PROGRESS_EVENT] == []


def test_missing_row_deletes_nothing_and_reports_nothing(monkeypatch, tmp_path):
    """行本来就不存在时那道闸会把 artifact_dir 置空 —— 进度也要跟着消失。

    否则前端会为一个最终回 404 的请求显示一条走到 100% 的删除进度，而磁盘上
    （另一个生命周期留下的同名目录）一个字节都没动。
    """
    db, td = _setup(monkeypatch, tmp_path)  # 不 _seed，库里没有 id=1
    art = tmp_path / "downloads" / "task_1"
    _build_tree(art)
    sio = _RecordingSocketIO()

    out = td.delete_task_row(manager=_FakeManager(sio), task_id=1, table="tasks",
                             artifact_dir=art)

    assert out.row_deleted is False
    assert art.exists(), "行不存在时一片磁盘都不能碰"
    assert [e for e in sio.events if e[0] == td.DELETE_PROGRESS_EVENT] == []


@pytest.mark.parametrize("table,task_type", [
    ("tasks", "map"),
    ("dem_tasks", "dem"),
    ("contour_tasks", "contour"),
    ("local_terrain_tasks", "local_terrain"),
    ("plugin_tasks", "plugin"),
])
def test_task_type_matches_what_the_frontend_filters_on(table, task_type):
    """五张任务表 → 五个 task_type，与前端 deleteTask 的 taskType 逐字一致。

    对不上的后果是静默的：进度框收不到任何自己认得的帧，一直停在初始文案上，
    而事件在网络面板里明明在飞 —— 与「后端没实现」看起来一模一样。
    """
    td = task_deletion_mod
    assert td._PIPELINE_BY_TABLE[table] == task_type
    assert set(td._PIPELINE_BY_TABLE) == td._DELETABLE_TASK_TABLES


def test_emitter_is_absent_when_the_manager_has_no_socketio(monkeypatch, tmp_path):
    """manager 没有 socketio 时不建回调 —— 那样删除会退回 rmtree，不白扫一遍盘。

    给一个空回调也能「不报错」，但每一次无人收听的删除都要多遍历一遍整棵树。
    """
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    art = tmp_path / "downloads" / "task_1"
    _build_tree(art)

    assert td._make_progress_emitter(_FakeManager(), 1, "map") is None

    seen = []
    real = td.remove_task_dir_and_confirm
    monkeypatch.setattr(td, "remove_task_dir_and_confirm",
                        lambda d, **kw: (seen.append(kw.get("progress_cb")), real(d, **kw))[1])
    td.delete_task_row(manager=_FakeManager(), task_id=1, table="tasks",
                       artifact_dir=art)
    assert seen == [None]


def test_a_failing_emit_never_breaks_the_deletion(monkeypatch, tmp_path):
    """广播抛异常时删除照常走完 —— 进度是附带产物，不能反过来毁掉主动作。"""
    db, td = _setup(monkeypatch, tmp_path)
    _seed(db)
    art = tmp_path / "downloads" / "task_1"
    _build_tree(art)

    class _BrokenSocketIO:
        def emit(self, *a, **kw):
            raise RuntimeError("socket is gone")

    out = td.delete_task_row(manager=_FakeManager(_BrokenSocketIO()), task_id=1,
                             table="tasks", artifact_dir=art)

    assert out.row_deleted is True
    assert out.files_removed is True
    assert not art.exists()
