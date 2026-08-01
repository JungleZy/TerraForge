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

from services import task_cleanup
from services.task_cleanup import (
    _sweep_cache_part_files,
    _sweep_tmp_dirs,
    sweep_startup_residue,
)


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


def test_sweep_cache_part_files_respects_layout_and_depth(tmp_path):
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


def test_sweep_startup_residue_end_to_end(monkeypatch, tmp_path):
    """gettempdir / CACHE_DIR / contour_warp_tmpdir 全部指到 tmp_path,
    验证三类残留一次清掉,且全程不抛异常。"""
    from core import config

    sys_tmp = tmp_path / "systmp"
    warp_root = tmp_path / "warp"
    cache = tmp_path / "cache"
    for d in (sys_tmp, warp_root, cache):
        d.mkdir()
    (sys_tmp / "map_dl_stitch_a").mkdir()
    (sys_tmp / "contour_warp_b").mkdir()
    (warp_root / "contour_warp_c").mkdir()
    (cache / "g.tif.part.1.2").write_bytes(b"partial")

    monkeypatch.setattr(task_cleanup.tempfile, "gettempdir", lambda: str(sys_tmp))
    monkeypatch.setattr(config.Config, "CACHE_DIR", cache)
    monkeypatch.setattr(
        "services.config_manager.ConfigManager.get",
        lambda self, k, d=None: str(warp_root) if k == "contour_warp_tmpdir" else d,
    )

    sweep_startup_residue()  # 不抛即过

    assert not (sys_tmp / "map_dl_stitch_a").exists()
    assert not (sys_tmp / "contour_warp_b").exists()
    assert not (warp_root / "contour_warp_c").exists()
    assert not (cache / "g.tif.part.1.2").exists()


def test_sweep_startup_residue_never_raises(monkeypatch, tmp_path):
    """配置库不可用 / cache 不存在 / 临时目录不可读,都只能跳过,不能影响启动。"""
    from core import config

    monkeypatch.setattr(task_cleanup.tempfile, "gettempdir", lambda: str(tmp_path / "nonexistent"))
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "no-such-cache")

    def boom(self, k, d=None):
        raise RuntimeError("config db unavailable")

    monkeypatch.setattr("services.config_manager.ConfigManager.get", boom)

    sweep_startup_residue()  # 任何内部失败都吞掉,不向外抛
