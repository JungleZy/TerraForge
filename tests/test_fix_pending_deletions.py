"""待删产物清单：进程被强杀后，下次启动必须把没删完的任务产物补删掉。

删除任务时行立即消失、产物在后台收尾（见任务生命周期简化设计 D2）。后台线程
可能卡在一段分钟级的 GDAL 阻塞区上，这期间用户关掉程序，那个目录就没人管了。
pending_deletions 是这条承诺的兜底：删任务行的同一事务里先记一行，后台删成功
后清行，进程被强杀时残留的行由启动清扫补删。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import  # noqa: E402


def _setup(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)

    # conftest 的 isolate_startup_sweep 把沙箱 tempfile 替身打在【当前这个】
    # task_cleanup 模块对象上，重导入拿到的是新对象、补丁跟不过来。不把它接过
    # 来，下面两条调 sweep_startup_residue 的用例就会去 rmtree 本机 /tmp 下的
    # map_dl_stitch_* / contour_warp_* —— conftest 里记着那是复现过的真实 flaky。
    import src.services.task_cleanup as _current_cleanup
    sandbox_tempfile = _current_cleanup.tempfile

    # fresh_import 而非裸 sys.modules.pop：裸 pop 不恢复，会给后跑的
    # test_startup_residue_sweep.py / test_cache_management.py 留下第二份
    # task_cleanup（它们在模块级 from-import 它），正是 M23 双实例，
    # test_conftest_isolation_contract 的棘轮直接拦这个组合。
    db, cleanup = fresh_import(
        monkeypatch, "src.core.database", "src.services.task_cleanup")
    db.init_database()
    monkeypatch.setattr(cleanup, "tempfile", sandbox_tempfile)
    return db, cleanup


def _queue(db, path):
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO pending_deletions (path) VALUES (?)", (str(path),))
        conn.commit()
    finally:
        conn.close()


def _rows(db):
    conn = db.get_connection()
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM pending_deletions")]
    finally:
        conn.close()


def test_pending_deletion_is_swept_and_row_cleared(monkeypatch, tmp_path):
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_9"
    target.mkdir(parents=True)
    (target / "a.tif").write_bytes(b"x")
    _queue(db, target)

    removed = cleanup._sweep_pending_deletions()

    assert removed == 1
    assert not target.exists(), "清单里的目录必须被删掉"
    assert _rows(db) == [], "删成功后清单行必须清掉"


def test_out_of_bounds_path_is_dropped_not_retried_forever(monkeypatch, tmp_path):
    """越界路径永远删不掉，留在清单里只会每次启动重试一遍并刷 warning。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    # DOWNLOADS_DIR 本身 —— remove_task_dir_if_safe 明确拒绝
    # （"Refusing to delete downloads root or its ancestor" 那条守卫）
    downloads_root = tmp_path / "downloads"
    _queue(db, downloads_root)

    removed = cleanup._sweep_pending_deletions()

    assert removed == 0, "越界拒删不算删除数"
    assert downloads_root.exists(), "护栏必须挡住 DOWNLOADS_DIR 本身"
    assert _rows(db) == [], "越界路径要从清单里丢弃，不能无限重试"


def test_row_survives_when_directory_could_not_be_removed(monkeypatch, tmp_path):
    """rmtree 用的是 ignore_errors=True —— 删不掉也返回 True。

    Windows 上文件被占用就是这种情况：只看返回值会把没删干净的目录从清单里
    抹掉，那正是这张表要防的事。目录还在就必须留着行，下次启动再试。
    """
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_8"
    target.mkdir(parents=True)
    _queue(db, target)

    # 模拟「符合删除条件、但实际没删掉」。顺带记下调用：这条用例的两个断言
    # （没计数、行还在）在「补删压根没跑」时同样成立，不钉住调用就是假绿。
    probed = []
    monkeypatch.setattr(
        cleanup, "remove_task_dir_if_safe", lambda p: probed.append(p) or True)

    removed = cleanup._sweep_pending_deletions()

    assert probed == [target], "补删必须真的处理过这一行"
    assert removed == 0
    assert _rows(db) == [str(target)], "目录还在时清单行必须保留"


def test_non_absolute_path_never_reaches_the_guard(monkeypatch, tmp_path):
    """空串 / '.' / 相对路径绝不能进护栏 —— 它会按【进程 cwd】解释掉。

    remove_task_dir_if_safe 内部是 expanduser().absolute().resolve()，非绝对
    路径的基准就是 cwd。而冻结 exe 的 cwd 是用户双击时所在的那个目录（桌面、
    下载夹、任意数据盘）。实测 '' 和 '.' 会让护栏把整个 cwd rmtree 掉，而且
    它还返回 True。补删跑在启动路径、无人值守、全体用户上，这类值必须在进护栏
    之前就丢弃。
    """
    db, cleanup = _setup(monkeypatch, tmp_path)
    victim = tmp_path / "cwd" / "payload"
    victim.mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "cwd")
    for raw in ("", ".", "sub/deeper"):
        _queue(db, raw)

    # 进了护栏就算漏 —— 别只靠「cwd 恰好没被删掉」来判定，那取决于运气
    # （默认部署下 cwd 等于 BASE_DIR，会被「downloads 的祖先」那条顺手挡掉）。
    monkeypatch.setattr(
        cleanup, "remove_task_dir_if_safe",
        lambda p: pytest.fail(f"非绝对路径进了护栏: {str(p)!r}"))

    removed = cleanup._sweep_pending_deletions()

    assert removed == 0, "丢弃不算删除数"
    assert victim.exists(), "cwd 下的无关目录被删了"
    assert _rows(db) == [], "非绝对路径要从清单里丢弃，不能无限重试"


def test_tilde_path_row_survives_when_directory_could_not_be_removed(
        monkeypatch, tmp_path):
    """`~` 路径也得走「删不干净就保留行」那一支。

    护栏内部会 expanduser 后再 rmtree，所以判「删干净了没」必须拿**展开后**的
    路径去 exists()：`Path('~/x').exists()` 恒为 False（实测），用没展开的路径
    探，整类 ~ 路径都会被误判成「已删掉」而清行 —— 正是这张表要防的事。
    """
    db, cleanup = _setup(monkeypatch, tmp_path)
    home = tmp_path / "home"
    (home / "dem" / "dem_task_5").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows 上 expanduser 认这个
    _queue(db, "~/dem/dem_task_5")

    # 模拟「符合删除条件、但实际没删掉」（占用中）。同 dem_task_8 那条：两个
    # 断言在「补删压根没跑」时同样成立，记下调用才不是假绿。护栏收到的必须是
    # **展开后**的路径 —— 它就是接下来拿去 exists() 的那一个。
    probed = []
    monkeypatch.setattr(
        cleanup, "remove_task_dir_if_safe", lambda p: probed.append(p) or True)

    removed = cleanup._sweep_pending_deletions()

    assert probed == [home / "dem" / "dem_task_5"], "护栏必须收到展开后的路径"
    assert removed == 0
    assert _rows(db) == ["~/dem/dem_task_5"], "目录还在时清单行必须保留"


def test_missing_directory_clears_the_row(monkeypatch, tmp_path):
    """目录早就没了（用户手工删过）也要清行，别让清单无限增长。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_7"
    _queue(db, target)

    removed = cleanup._sweep_pending_deletions()

    assert _rows(db) == []
    assert removed == 1


def test_sweep_startup_residue_runs_pending_deletions(monkeypatch, tmp_path):
    """补删必须真的挂在启动清扫上，不是一个没人调的函数。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    target = tmp_path / "downloads" / "dem" / "dem_task_6"
    target.mkdir(parents=True)
    _queue(db, target)

    cleanup.sweep_startup_residue()

    assert not target.exists()
    assert _rows(db) == []


def test_sweep_never_raises_when_table_is_missing(monkeypatch, tmp_path):
    """老库没有这张表 —— 清扫是 best-effort，绝不能因此让启动失败。"""
    db, cleanup = _setup(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        conn.execute("DROP TABLE pending_deletions")
        conn.commit()
    finally:
        conn.close()

    assert cleanup._sweep_pending_deletions() == 0
    cleanup.sweep_startup_residue()   # 不得抛
