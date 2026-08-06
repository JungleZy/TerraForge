"""启动清扫（services/task_cleanup.sweep_startup_residue）行为测试。

覆盖三类 finally 盖不住（SIGKILL/关窗）的临时残留：stitch work_dir
（map_dl_stitch_*）、contour warp tmpdir（contour_warp_*）、cache 的
原子写临时件（*.part.*）。核心约束：匹配精确、非匹配项一律不碰
（宁可漏不可误删）、异常不向外抛。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services import task_cleanup
from src.services.task_cleanup import (
    _PROCESS_START_TIME,
    _sweep_cache_part_files,
    _sweep_tmp_dirs,
    sweep_startup_residue,
)


def _age(path: Path, seconds: int = 3600) -> None:
    """把 mtime 调到本进程启动之前，模拟上一次运行留下的残留。"""
    old = _PROCESS_START_TIME - seconds
    os.utime(path, (old, old))


def test_sweep_tmp_dirs_removes_only_matching_prefix_dirs(tmp_path):
    for name in ("map_dl_stitch_abc", "contour_warp_xyz"):
        d = tmp_path / name
        d.mkdir()
        (d / "work.tif").write_bytes(b"x")
    # 非匹配项:别的前缀、同名但是文件、前缀不完全匹配 —— 都必须留下
    keep_dir = tmp_path / "other_dir"
    keep_dir.mkdir()
    keep_file = tmp_path / "map_dl_stitch_notadir"
    keep_file.write_bytes(b"x")
    almost = tmp_path / "map_dl_stitch"  # 缺下划线后缀段,不算
    almost.mkdir()

    assert _sweep_tmp_dirs(tmp_path, "map_dl_stitch_") == 1
    assert _sweep_tmp_dirs(tmp_path, "contour_warp_") == 1

    assert not (tmp_path / "map_dl_stitch_abc").exists()
    assert not (tmp_path / "contour_warp_xyz").exists()
    assert keep_dir.exists() and keep_file.exists() and almost.exists()


def test_sweep_cache_part_files_respects_layout_and_depth(tmp_path, monkeypatch):
    # 写这些 .part 的进程都已经死了(pid 归属判据见下一条用例)。不打桩的话,
    # 用例结果会取决于本机上恰好有没有 pid=123 的进程 —— 那是 flaky。
    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: False)
    # dem cache:cache/dem/<granule>.part.*(浅层)
    dem = tmp_path / "dem"
    dem.mkdir()
    (dem / "N00E000.tif").write_bytes(b"real")
    (dem / "N00E001.tif.part.123.456").write_bytes(b"partial")
    # 瓦片 cache:cache/{style}/{z}/{x}/{y}.png 的 .part 在 x 目录(深度 4)
    xdir = tmp_path / "satellite" / "12" / "3456"
    xdir.mkdir(parents=True)
    (xdir / "789.png").write_bytes(b"real")
    (xdir / "790.png.part.123.789").write_bytes(b"partial")
    # 比已知布局更深的 .part:宁可漏不可误删 —— 不删
    deep = xdir / "unexpected" / "deeper"
    deep.mkdir(parents=True)
    (deep / "x.png.part.1.2").write_bytes(b"partial")

    removed = _sweep_cache_part_files(tmp_path)

    assert removed == 2
    assert (dem / "N00E000.tif").exists()
    assert not (dem / "N00E001.tif.part.123.456").exists()
    assert (xdir / "789.png").exists()
    assert not (xdir / "790.png.part.123.789").exists()
    assert (deep / "x.png.part.1.2").exists()


def test_sweep_cache_part_files_skips_files_owned_by_a_live_process(tmp_path, monkeypatch):
    """H3:.part 文件名里带写它的进程 pid —— 该进程还活着就不能删,否则会把
    另一个实例正在做的原子写打断(它的 part_path.replace() 抛 FileNotFoundError)。

    这是三类清扫对象里唯一带归属信息的一类,可以精确判定,不必退到 mtime 近似。
    """
    dem = tmp_path / "dem"
    dem.mkdir()
    live = dem / "A.tif.part.4242.1"
    dead = dem / "B.tif.part.4243.1"
    mine = dem / f"C.tif.part.{os.getpid()}.1"
    for f in (live, dead, mine):
        f.write_bytes(b"partial")

    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: pid == 4242)

    removed = _sweep_cache_part_files(tmp_path)

    assert live.exists(), "活进程正在写的 .part 必须留下"
    assert not dead.exists(), "已退出进程的 .part 应清掉"
    assert not mine.exists(), "本进程自己的残留(上一轮同 pid)应清掉"
    assert removed == 2


def test_sweep_startup_residue_end_to_end(monkeypatch, tmp_path):
    """gettempdir / CACHE_DIR / contour_warp_tmpdir 全部指到 tmp_path,
    验证三类残留一次清掉,且全程不抛异常。"""
    from src.core import config

    sys_tmp = tmp_path / "systmp"
    warp_root = tmp_path / "warp"
    cache = tmp_path / "cache"
    for d in (sys_tmp, warp_root, cache):
        d.mkdir()
    (sys_tmp / "map_dl_stitch_a").mkdir()
    (sys_tmp / "contour_warp_b").mkdir()
    (warp_root / "contour_warp_c").mkdir()
    (cache / "g.tif.part.1.2").write_bytes(b"partial")
    # H3:清扫只处理【早于本进程启动】的临时目录 —— 把三个探针的 mtime 调到
    # 过去,模拟「上次进程留下的残留」。不调的话它们比本进程新,会被(正确地)跳过。
    _age(sys_tmp / "map_dl_stitch_a")
    _age(sys_tmp / "contour_warp_b")
    _age(warp_root / "contour_warp_c")

    monkeypatch.setattr(task_cleanup.tempfile, "gettempdir", lambda: str(sys_tmp))
    monkeypatch.setattr(config.Config, "CACHE_DIR", cache)
    # 探针 .part 的 pid 是 1(Linux 上 init 恒存活),打桩成已退出。
    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "src.services.config_manager.ConfigManager.get",
        lambda self, k, d=None: str(warp_root) if k == "contour_warp_tmpdir" else d,
    )

    sweep_startup_residue()  # 不抛即过

    assert not (sys_tmp / "map_dl_stitch_a").exists()
    assert not (sys_tmp / "contour_warp_b").exists()
    assert not (warp_root / "contour_warp_c").exists()
    assert not (cache / "g.tif.part.1.2").exists()


def test_sweep_startup_residue_never_raises(monkeypatch, tmp_path):
    """配置库不可用 / cache 不存在 / 临时目录不可读,都只能跳过,不能影响启动。"""
    from src.core import config

    monkeypatch.setattr(task_cleanup.tempfile, "gettempdir", lambda: str(tmp_path / "nonexistent"))
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "no-such-cache")

    def boom(self, k, d=None):
        raise RuntimeError("config db unavailable")

    monkeypatch.setattr("src.services.config_manager.ConfigManager.get", boom)

    sweep_startup_residue()  # 任何内部失败都吞掉,不向外抛


# ---------------------------------------------------------------------------
# 第 5 类：多幅 DEM 物化的中间栅格（cesiumlab_terrain_<pid>_*）
# ---------------------------------------------------------------------------


def test_materialised_owner_pid_parses_and_rejects():
    """归属判定靠文件名里的 pid —— 解析规则必须精确。

    这一类不能靠 mtime：物化产物写完 mtime 就冻住，而切片可以再跑几小时，
    晚起的第二个实例会认为它「早于我启动」而放行删除。
    """
    from src.services.task_cleanup import _materialised_owner_pid

    assert _materialised_owner_pid("cesiumlab_terrain_12345_ab12cd.tif") == 12345
    # 老产物（2026-08-06 之前）名里没有 pid，解析不出来 → 退回 mtime 判据
    assert _materialised_owner_pid("cesiumlab_terrain_ab12cd.tif") is None
    # 别的东西一律不认
    assert _materialised_owner_pid("cesiumlab_terrain.py") is None
    assert _materialised_owner_pid("ASTGTMV003_N42E086_dem.tif") is None


def test_sweep_orphan_files_skips_live_writer(monkeypatch, tmp_path):
    """另一个活着的进程正在切片时，它的物化产物不得被删。

    删掉 = 那次切片当场炸，或者更糟：读到半截数据。pid 复用只会导致漏删
    （下次启动再清），方向是安全的。
    """
    from src.services.task_cleanup import _MATERIALISED_PREFIX, _sweep_orphan_files

    alive = tmp_path / "cesiumlab_terrain_4242_alive.tif"
    dead = tmp_path / "cesiumlab_terrain_4243_dead.tif"
    for f in (alive, dead):
        f.write_bytes(b"x" * 16)
        _age(f)

    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: pid == 4242)

    removed = _sweep_orphan_files(tmp_path, _MATERIALISED_PREFIX, _PROCESS_START_TIME)

    assert removed == 1
    assert alive.exists(), "活着的进程正在用的产物被删了"
    assert not dead.exists()


def test_sweep_orphan_files_leaves_everything_else_alone(monkeypatch, tmp_path):
    """只删自己的中间产物 —— DEM 源、成品瓦片、子目录一律不碰。

    宁可漏不可误删：这些目录里放的是用户几十分钟才下完的 DEM 和刚切好的瓦片。
    """
    from src.services.task_cleanup import _MATERIALISED_PREFIX, _sweep_orphan_files

    target = tmp_path / "cesiumlab_terrain_777_x.tif"
    target.write_bytes(b"x")
    _age(target)

    keep_files = [
        tmp_path / "ASTGTMV003_N42E086_dem.tif",      # DEM 源
        tmp_path / "cesiumlab_terrain.py",             # 源码同名前缀（无下划线段）
        tmp_path / "layer.json",
    ]
    for f in keep_files:
        f.write_bytes(b"keep")
        _age(f)
    keep_dir = tmp_path / "cesiumlab_terrain_dir"      # 同前缀但是目录
    keep_dir.mkdir()
    _age(keep_dir)

    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: False)
    removed = _sweep_orphan_files(tmp_path, _MATERIALISED_PREFIX, _PROCESS_START_TIME)

    assert removed == 1
    assert not target.exists()
    for f in keep_files:
        assert f.exists(), f"误删了 {f.name}"
    assert keep_dir.is_dir(), "误删了同前缀的目录"


def test_sweep_orphan_files_does_not_recurse(monkeypatch, tmp_path):
    """不递归：任务目录下是 terrain_tiles/{z}/{x}/{y}.terrain，可达百万级条目，
    递归会把启动拖到分钟级。产物恒在 work_dir 直下。"""
    from src.services.task_cleanup import _MATERIALISED_PREFIX, _sweep_orphan_files

    nested = tmp_path / "terrain_tiles" / "10" / "1511"
    nested.mkdir(parents=True)
    deep = nested / "cesiumlab_terrain_777_deep.tif"
    deep.write_bytes(b"x")
    _age(deep)

    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: False)
    assert _sweep_orphan_files(tmp_path, _MATERIALISED_PREFIX, _PROCESS_START_TIME) == 0
    assert deep.exists()


def test_materialised_sweep_roots_include_db_output_paths(monkeypatch, tmp_path):
    """扫描根必须从 DB 取 —— DEM 任务的 output_path 是用户自选的全盘路径。

    只扫 DOWNLOADS_DIR 会漏掉 GB 级残留的主场景（0.2.4 起保存路径放开全盘）。
    """
    from src.core import config
    from src.services.task_cleanup import _materialised_sweep_roots

    downloads = tmp_path / "downloads"
    (downloads / "terrain").mkdir(parents=True)
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", downloads)

    elsewhere = tmp_path / "some" / "other" / "disk" / "dem_task_7"
    elsewhere.mkdir(parents=True)

    class _FakeConn:
        def execute(self, sql):
            if "dem_terrain_jobs" in sql:
                return _FakeCursor([(str(elsewhere / "terrain_tiles"),)])
            return _FakeCursor([])

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    import contextlib

    @contextlib.contextmanager
    def fake_ctx():
        yield _FakeConn()

    monkeypatch.setattr("src.core.database.get_connection_context", fake_ctx)

    roots = [str(Path(r).resolve()) for r in _materialised_sweep_roots()]

    assert str((downloads / "terrain").resolve()) in roots, "漏了本地地形/base 的落点"
    assert str(elsewhere.resolve()) in roots, \
        "漏了 DB 里的 DEM 输出路径 —— 那是 GB 级残留的主场景"


def test_sweep_orphan_files_refuses_symlinks(monkeypatch, tmp_path):
    """同名的符号链接不能删。

    os.unlink 对目录会失败（IsADirectoryError 被 except OSError 吞掉），但对
    符号链接会**成功** —— 删掉的是链接本身。名字是攻击者/误操作可控的，而这个
    清扫跑在启动时、没有任何确认。项目其它清理路径（remove_task_dir_if_safe）
    同样明确拒绝带符号链接的路径。
    """
    from src.services.task_cleanup import _MATERIALISED_PREFIX, _sweep_orphan_files

    precious = tmp_path / "important_dem.tif"
    precious.write_bytes(b"user data")
    link = tmp_path / "cesiumlab_terrain_999_link.tif"
    try:
        link.symlink_to(precious)
    except (OSError, NotImplementedError):
        import pytest as _pytest
        _pytest.skip("此平台不支持符号链接")
    _age(link)

    monkeypatch.setattr("src.core.process_watchdog.pid_alive", lambda pid: False)
    removed = _sweep_orphan_files(tmp_path, _MATERIALISED_PREFIX, _PROCESS_START_TIME)

    assert removed == 0
    assert link.is_symlink(), "同名符号链接被删了"
    assert precious.exists()


# ---------------------------------------------------------------------------
# 第 6 类：随包底图的解压临时目录（.base_unpack_<pid>_*，位于 assets/terrain）
# ---------------------------------------------------------------------------


def test_base_unpack_tmp_dirs_are_swept_from_assets_terrain(monkeypatch, tmp_path):
    """assets/terrain 下的解压残留要清掉；分卷、成品底图、活进程的目录都不能碰。

    这个落点前五类的扫描根一条都覆盖不到（不在系统临时目录、不在 DOWNLOADS_DIR、
    不在 CACHE_DIR），而单次残留最多 167 MB / 4.3 万个文件；更要紧的是
    assets/terrain 是 Nuitka 的 --include-data-dir 源目录，清不掉就会被打进三个
    平台的发布产物，且没有任何其他回收入口。
    """
    from src.core import config
    from src.services.terrain_tiling import base_terrain

    assets = tmp_path / "assets" / "terrain"
    assets.mkdir(parents=True)
    monkeypatch.setattr(config.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(base_terrain, "bundle_dir", lambda: None)

    stale = assets / f"{base_terrain.UNPACK_TMP_PREFIX}999999_abc"
    stale.mkdir()
    (stale / "layer.json").write_text("{}", encoding="utf-8")
    _age(stale)

    # 宁可漏不可误删：另一个活进程正在写的目录（mtime 比本进程新）、随包分卷、
    # 已就位的成品底图 —— 三者一个都不能动。
    live = assets / f"{base_terrain.UNPACK_TMP_PREFIX}{os.getpid()}_live"
    live.mkdir()
    part = assets / "base_z8.tar.gz.partaa"
    part.write_bytes(b"x")
    ready = assets / "base_z8"
    ready.mkdir()
    _age(ready)      # 成品同样是「旧」的，只靠前缀不匹配保住

    # 其余五类的扫描根全部指到不存在的目录，免得这条用例碰到真实系统临时目录。
    monkeypatch.setattr(task_cleanup.tempfile, "gettempdir",
                        lambda: str(tmp_path / "nosys"))
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "nocache")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "nodl")
    monkeypatch.setattr("src.services.config_manager.ConfigManager.get",
                        lambda self, k, d=None: d)

    sweep_startup_residue()

    assert not stale.exists(), "上次进程留下的解压残留没被清掉"
    assert live.exists(), "另一个活进程正在写的临时目录被删了"
    assert part.exists(), "随包分卷被误删了"
    assert ready.exists(), "已就位的成品底图被误删了"
