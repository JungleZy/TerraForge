"""M2 / M6 / M19：三个「产物或状态被悄悄写坏」的回归。

- M2：拼接产物非原子写 —— 半截 GeoTIFF 被断点续跑当成已完成。
- M6：「恢复默认配置」写回相对保存路径 —— 之后地图/DEM 建任务全部 400。
- M19：发布包里混进冒烟测试生成的数据库 —— 默认保存路径写死成 CI 机器路径。
"""

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from conftest import fresh_import  # noqa: E402


# ---------------------------------------------------------------------------
# M6：reset_to_defaults 必须跟着做 default_save_path 归一化
# ---------------------------------------------------------------------------

def _fresh_db(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # 走 fresh_import 而不是裸 pop：裸 pop 不恢复，会给后续文件留下第二份模块
    # 对象（M23）。services.config_manager 被 test_config_manager.py /
    # test_tile_url_config.py 在**模块级** from-import，正是会踩到的形态。
    db, cfg = fresh_import(monkeypatch, "app", "core.database",
                           "services.config_manager")[1:]
    db.init_database()
    return db, cfg


def test_reset_to_defaults_keeps_save_path_absolute(monkeypatch, tmp_path):
    """reset 后 default_save_path 必须仍是绝对路径。

    DEFAULT_CONFIGS 里那一项是相对值 './downloads'，而 reset_to_defaults 绕过了
    set/set_many 的 validate_config —— 不归一的话会把一个 **validate_config 自己
    判非法** 的值写回库，之后 POST /api/tasks 一律 400「保存路径必须是绝对路径」。
    """
    db, cm_mod = _fresh_db(monkeypatch, tmp_path)
    mgr = cm_mod.ConfigManager()

    before = mgr.get("default_save_path")
    assert Path(before).is_absolute(), "init_database 之后本就该是绝对路径"

    assert mgr.reset_to_defaults() is True

    after = mgr.get("default_save_path")
    assert Path(after).is_absolute(), (
        f"reset 之后变回了相对路径 {after!r} —— 地图/DEM 建任务会全部 400")


def test_reset_to_defaults_result_passes_its_own_validation(monkeypatch, tmp_path):
    """更强的版本：reset 写回的值必须能通过 validate_config 自己那关。"""
    db, cm_mod = _fresh_db(monkeypatch, tmp_path)
    mgr = cm_mod.ConfigManager()
    mgr.reset_to_defaults()

    value = mgr.get("default_save_path")
    assert mgr.validate_config("default_save_path", value), (
        f"reset 写回的值通不过 ConfigManager 自己的 validate_config: {value!r}")


# ---------------------------------------------------------------------------
# M2：拼接产物原子写
# ---------------------------------------------------------------------------

def test_failed_stitch_leaves_no_partial_output(monkeypatch, tmp_path):
    """Translate 失败时绝不能在最终路径上留下【非空】半成品。

    断点判定是 `output_path.exists() and st_size > 0` 就跳过重拼并记成功。
    GDAL 写 GTiff 边写边落盘，被杀/抛异常留下的必然是非空半成品，恰好满足
    那个判据 —— 用户点一次「重试」就命中短路，warning 消失、任务转 completed，
    产物却是损坏状态不确定的 tif。
    """
    pytest.importorskip("osgeo.gdal")
    from core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "out")
    # 同 test_fix_cache_chain：DownloadEngine 构造即读配置库，不隔离
    # DATABASE_PATH 的话 CI 的干净 runner 上会「打不开数据库」。
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    # fresh_import 而非裸 pop：download_tile 内 `raise DownloadCancelled()` 解析的是
    # **它自己模块的全局**，裸 pop 不恢复会让后跑的 test_download_engine.py 在
    # `pytest.raises(DownloadCancelled)` 里 catch 到另一份类对象，异常穿透（M23，
    # 文件级逆序下实测两条 stop_flag 用例翻红）。
    _db, de = fresh_import(monkeypatch, "core.database", "services.download_engine")
    _db.init_database()

    from models.task import Tile
    from PIL import Image

    engine = de.DownloadEngine()
    zoom, x, y = 10, 843, 387
    tiles = [Tile(task_id=0, zoom=zoom, x=x, y=y), Tile(task_id=0, zoom=zoom, x=x + 1, y=y)]
    for t in tiles:
        p = t.cache_path("m")
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "mosaic.tif"

    real_translate = de.gdal.Translate

    def _translate_then_die(dest, src, **kwargs):
        # 先真写出文件（模拟 GDAL 边写边落盘），再抛错 —— 这正是断电/写满盘
        # 留下非空半成品的形态。
        ds = real_translate(dest, src, **kwargs)
        del ds
        raise RuntimeError("simulated disk full during translate")

    monkeypatch.setattr(de.gdal, "Translate", _translate_then_die)

    with pytest.raises(RuntimeError):
        engine.stitch_tiles_with_gdal(tiles, "m", str(out_path), zoom, target_epsg=3857)

    assert not out_path.exists(), (
        "失败的拼接在最终路径上留下了产物 —— 断点逻辑会把它当成已完成")
    leftovers = list(out_dir.glob("mosaic.tif.part.*"))
    assert leftovers == [], f"临时件未清理: {leftovers}"


def test_successful_stitch_still_produces_the_output(monkeypatch, tmp_path):
    """对照：正常路径不受原子写改动影响。"""
    pytest.importorskip("osgeo.gdal")
    from core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "out")
    # 同 test_fix_cache_chain：DownloadEngine 构造即读配置库，不隔离
    # DATABASE_PATH 的话 CI 的干净 runner 上会「打不开数据库」。
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    # fresh_import 而非裸 pop：download_tile 内 `raise DownloadCancelled()` 解析的是
    # **它自己模块的全局**，裸 pop 不恢复会让后跑的 test_download_engine.py 在
    # `pytest.raises(DownloadCancelled)` 里 catch 到另一份类对象，异常穿透（M23，
    # 文件级逆序下实测两条 stop_flag 用例翻红）。
    _db, de = fresh_import(monkeypatch, "core.database", "services.download_engine")
    _db.init_database()

    from models.task import Tile
    from PIL import Image

    engine = de.DownloadEngine()
    zoom, x, y = 10, 843, 387
    tiles = [Tile(task_id=0, zoom=zoom, x=x, y=y), Tile(task_id=0, zoom=zoom, x=x + 1, y=y)]
    for t in tiles:
        p = t.cache_path("m")
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p)

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "mosaic.tif"

    engine.stitch_tiles_with_gdal(tiles, "m", str(out_path), zoom, target_epsg=3857)

    assert out_path.exists() and out_path.stat().st_size > 0
    assert list(out_dir.glob("mosaic.tif.part.*")) == [], "成功路径不得留下临时件"


# ---------------------------------------------------------------------------
# M19：发布包不得混入冒烟测试的运行期数据
# ---------------------------------------------------------------------------

def test_build_workflow_strips_runtime_data_before_packaging():
    """打包前必须清掉 dist/terraforge 下的 data/downloads/cache。

    frozen 模式下 Config.BASE_DIR = exe 所在目录，冒烟测试那一次启动会在 dist
    里建出 data/map_downloader.db —— 而它不是空壳：init_database() 会把
    default_save_path 归一成【当时 CI 机器上的绝对路径】，用户端首启不会再动它。
    结果就是用户解压运行后，表单被预填成 /home/runner/... 或 D:\\a\\map-download\\...
    """
    wf = Path(PROJECT_ROOT, ".github", "workflows", "build.yml").read_text(encoding="utf-8")

    smoke_at = wf.find("Smoke test executable")
    package_at = wf.find("Package application")
    assert smoke_at != -1 and package_at != -1
    between = wf[smoke_at:package_at]

    assert re.search(r"rm\s+-rf[^\n]*dist/terraforge/data", between), (
        "冒烟测试与打包之间没有清理 dist/terraforge/data —— 发布包会带上 CI 机器的 DB")
    for leftover in ("downloads", "cache"):
        assert f"dist/terraforge/{leftover}" in between, (
            f"清理步骤漏了 dist/terraforge/{leftover}")
