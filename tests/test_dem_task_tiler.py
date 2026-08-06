"""
Tests for DEM task tiler helpers.
"""

import json
import os
import sys
from pathlib import Path

import pytest


# Add parent directory to path for imports (match repo test style)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_list_dem_tifs_filters_num(tmp_path: Path):
    from src.services.terrain_tiling.vrt_builder import list_dem_tifs

    (tmp_path / "A_dem.tif").write_text("", encoding="utf-8")
    (tmp_path / "A_num.tif").write_text("", encoding="utf-8")

    assert list_dem_tifs(tmp_path) == [tmp_path / "A_dem.tif"]


def test_terrain_output_dir_for_task(tmp_path: Path):
    from src.services.terrain_tiling.dem_task_tiler import terrain_output_dir_for_task

    out = terrain_output_dir_for_task(str(tmp_path), 123)
    assert out == tmp_path / "dem_task_123" / "terrain_tiles"


def test_tile_dem_task_dir_calls_external_tools(tmp_path: Path):
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")

    out_dir = tmp_path / "out"

    captured = {}

    def fake_build_terrain(**kwargs):
        captured.update(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")

    params = TileParams(maxzoom=0, parent_url="https://example.com/parent.json")
    # Don't run the real GDAL/numpy pipeline in unit tests.
    tile_dem_task_dir(task_dir, out_dir, params, build_terrain_fn=fake_build_terrain)

    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert '"parentUrl": "https://example.com/parent.json"' in layer
    # 65x65 grid matches 30 m DEM resolution at the estimated maxzoom (z14).
    assert captured["tile_size"] == 65
    # 自适应三角化默认开启，且必须真的透传到 build_terrain —— 少了这两条，
    # 把 tile_dem_task_dir 里的两行 triangulator/max_error_k 删掉全量照样绿。
    # 65 = 2^6+1，满足自适应路径对 tile_size 的要求。
    # 'auto' = 逐瓦片择优（grid/martini 都编一遍，取 gzip 后更小的）：实测
    # 山地上 martini 的 gzip 字节反而 +17.6%，全局择优净省 27.6% 且每张瓦片
    # 字节严格 ≤ min(两者)。这里是这个字面量在全仓的唯一副本，
    # test_rtin.test_triangulation_defaults_agree_across_every_copy 负责把它
    # 传导到 DEFAULT_MAX_ERROR_K / build_terrain / CLI 三处。
    assert captured["triangulator"] == "auto"
    assert captured["max_error_k"] == 0.15


def test_tile_dem_task_dir_passes_through_the_backend_choice_counts(tmp_path: Path):
    """build_terrain 的 chose_martini / chose_grid 必须原样透传出来。

    这两个 key 写在 tile_dem_task_dir 的 docstring 里当契约,但在补这条之前
    **没有任何测试守着** —— 把它们从返回的 keys 元组里删掉,全量 1035 条一条不红。
    而"tiler 已经透传了"正是当初决定"两个 manager 不用改"的依据,
    依据本身无守卫就等于没依据。

    值刻意取互不相同、也不等于 total/rendered/failed 的数:key 之间串位
    (chose_martini 拿到 chose_grid 的值)照样会被抓住。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    def fake_build_terrain(**kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")
        return {"total": 40, "rendered": 37, "failed": 3,
                "chose_martini": 11, "chose_grid": 26}

    got = tile_dem_task_dir(task_dir, out_dir,
                            TileParams(maxzoom=0, parent_url="https://example.com/p.json"),
                            build_terrain_fn=fake_build_terrain)

    assert got == {"total": 40, "rendered": 37, "failed": 3,
                   "chose_martini": 11, "chose_grid": 26}


def test_tile_dem_task_dir_zero_fills_counts_for_legacy_stubs(tmp_path: Path):
    """老的测试替身返回 None 时,五个计数必须齐全地归零,而不是少几个 key。

    调用方按"无计数信息"处理,行为与加计数之前一致;少 key 会让调用方 KeyError。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    def fake_build_terrain(**kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")

    got = tile_dem_task_dir(task_dir, out_dir,
                            TileParams(maxzoom=0, parent_url="https://example.com/p.json"),
                            build_terrain_fn=fake_build_terrain)

    assert got == {"total": 0, "rendered": 0, "failed": 0,
                   "chose_martini": 0, "chose_grid": 0}


# ---------------------------------------------------------------------------
# 接入底图：解压 → 摘链 → 切片(z8+) → 植入 → 合成
# ---------------------------------------------------------------------------


def _make_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "a_dem.tif").write_bytes(b"x")
    return task_dir


def test_tiler_grafts_base_and_merges_layer_json(tmp_path: Path, monkeypatch):
    """底图可用时：切片走 min_level=8，切完植入并合成，不再写 parentUrl。"""
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    base = tmp_path / "base"
    (base / "0" / "0").mkdir(parents=True)
    (base / "0" / "0" / "0.terrain").write_bytes(b"\x1f\x8bbase")
    (base / "layer.json").write_text(
        json.dumps({"maxzoom": 7,
                    "available": [[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8}),
        encoding="utf-8")

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text(
            json.dumps({"maxzoom": 10, "available": [[]] * 10 + [
                [{"startX": 1, "startY": 1, "endX": 2, "endY": 2}]]}),
            encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0}

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)

    mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=10, parent_url="http://localhost:5000/terrain/base"),
        build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 8, "底图可用时任务只切 z8+"
    assert seen["max_level"] == 10
    assert (out_dir / "0" / "0" / "0.terrain").is_file(), "底图没被植入"

    data = json.loads((out_dir / "layer.json").read_text(encoding="utf-8"))
    # 底图可用时 parentUrl 必须消失：留着它 Cesium 会既加载本地 z0-7、又去级联
    # 请求那个多半不存在的父级服务。
    assert "parentUrl" not in data
    # 合成的两个方向都要钉住：只取底图（丢任务自己的 z10）和只取任务（底图那 8 层
    # 全空）都会让 Cesium 对着「声明没有、实际有」的层级不发请求。
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert data["available"][10] == [{"startX": 1, "startY": 1, "endX": 2, "endY": 2}]
    assert data["maxzoom"] == 10


def test_tiler_falls_back_to_parent_url_without_base(tmp_path: Path, monkeypatch):
    """底图不可用（分卷被删）→ 行为与 v0.2.8 完全一致：min_level=0 + 写 parentUrl。"""
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: None)

    mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=10, parent_url="https://example.com/parent"),
        build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 0
    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert '"parentUrl": "https://example.com/parent"' in layer


def test_degenerate_maxzoom_still_tiles_something(tmp_path: Path, monkeypatch):
    """maxzoom < 8 的任务不能因为 min_level=8 切出零张瓦片却报成功。

    min_level 死写 8 时 max_level(5) < min_level(8)，_tile_ranges() 产出空区间，
    任务 rendered=0 却 completed —— 又一款静默成功。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)

    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 5, "available": []}', encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)

    mod.tile_dem_task_dir(task_dir, tmp_path / "out",
                          mod.TileParams(maxzoom=5, parent_url=""),
                          build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 5, "min_level 必须是 min(8, maxzoom)"
    assert seen["min_level"] <= seen["max_level"], "min_level > max_level = 一张瓦片都不切"


def test_base_pipeline_runs_in_the_right_order(tmp_path: Path, monkeypatch):
    """解压 → 摘链 → 切片 → 植入 → 合成，顺序错一步就有真实后果。

    - 解压排在切片**前**：首次解压是分钟级，要独占 stage_cb 上报通道，否则和
      切片进度抢同一条通道，前端只看到进度条来回跳。stage_cb 必须真的传进去，
      不传等于这几分钟整段黑屏。
    - 摘链排在切片**前**：晚一步，切片就已经就地截断了共享缓存的 inode。
    - 植入排在切片**后**：graft_base_into 的冲突判断是遍历时读一次的目录级快照，
      与切片并发跑会绕过 skip-if-exists（它 docstring 里写死的前提）。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    calls = []
    marker = lambda phase, frac: None  # noqa: E731

    def fake_unpack(**kwargs):
        calls.append("unpack")
        assert kwargs.get("stage_cb") is marker, "解压阶段没拿到 stage_cb，几分钟黑屏"
        return base

    def fake_ungraft(tiles_dir, base_dir):
        calls.append("ungraft")
        assert Path(tiles_dir) == out_dir and Path(base_dir) == base
        return 0

    def fake_graft(tiles_dir, base_dir):
        calls.append("graft")
        assert Path(tiles_dir) == out_dir and Path(base_dir) == base
        return {"linked": 0, "copied": 0, "skipped": 0}

    def fake_merge(task_layer_path, base_layer_path):
        calls.append("merge")
        assert Path(base_layer_path) == base / "layer.json"

    def fake_build_terrain(**kwargs):
        calls.append("tile")
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 9, "available": []}', encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", fake_unpack)
    monkeypatch.setattr(mod, "ungraft_base_from", fake_ungraft)
    monkeypatch.setattr(mod, "graft_base_into", fake_graft)
    monkeypatch.setattr(mod, "merge_base_availability", fake_merge)

    mod.tile_dem_task_dir(task_dir, out_dir,
                          mod.TileParams(maxzoom=9, parent_url="", stage_cb=marker),
                          build_terrain_fn=fake_build_terrain)

    assert calls == ["unpack", "ungraft", "tile", "graft", "merge"]


def test_tiler_unlinks_grafted_base_before_tiling(tmp_path: Path, monkeypatch):
    """重跑 maxzoom<8 的任务不许把瓦片写穿进共享底图缓存。

    可达路径：maxzoom=5 → min_level=min(8,5)=5，任务自己就要写 z5。上一轮植入
    在 out_dir 里留下的是**指向共享缓存的硬链接**，而落盘走 Path.write_bytes
    （就地截断同一 inode），于是第二轮的 z5 瓦片直接改写 assets/terrain/base_z8
    里那份文件 —— 全局底图被污染，影响之后所有任务，零信号。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod
    from src.services.terrain_tiling.base_terrain import graft_base_into

    base = tmp_path / "base"
    (base / "5" / "1").mkdir(parents=True)
    cached = base / "5" / "1" / "2.terrain"
    cached.write_bytes(b"\x1f\x8bBASE")
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    # 上一轮跑完的现场
    assert graft_base_into(out_dir, base)["linked"] > 0, \
        "本用例要求硬链接生效，否则测不出写穿"

    def fake_build_terrain(**kwargs):
        out = Path(kwargs["output_dir"])
        d = out / "5" / "1"
        d.mkdir(parents=True, exist_ok=True)
        # 与 cesiumlab_terrain._worker_tile 的落盘方式逐字一致
        (d / "2.terrain").write_bytes(b"TASK-Z5")
        (out / "layer.json").write_text('{"maxzoom": 5, "available": []}', encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)

    mod.tile_dem_task_dir(task_dir, out_dir,
                          mod.TileParams(maxzoom=5, parent_url=""),
                          build_terrain_fn=fake_build_terrain)

    assert cached.read_bytes() == b"\x1f\x8bBASE", "任务瓦片写穿到了共享底图缓存"
    assert (out_dir / "5" / "1" / "2.terrain").read_bytes() == b"TASK-Z5", \
        "植入把任务自己的瓦片盖掉了（skip-if-exists 失效）"


def test_graft_failure_fails_the_whole_task(tmp_path: Path, monkeypatch):
    """植入失败必须上抛，不许吞掉后退回 parentUrl 兜底。

    半个底图比没有底图更糟：缺的瓦片让 Cesium 拿 404 → 它不报错，而是塞一个假的
    heightmap-1.0 图层并污染共享 builder，连任务自己的 quantized-mesh 瓦片都被按
    heightmap 解析（v0.2.8 实测 4154 m 山峰解成 -744 m，控制台零报错）。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    def fake_build_terrain(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 9, "available": []}', encoding="utf-8")

    def boom(tiles_dir, base_dir):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)
    monkeypatch.setattr(mod, "graft_base_into", boom)

    with pytest.raises(OSError):
        mod.tile_dem_task_dir(task_dir, out_dir,
                              mod.TileParams(maxzoom=9,
                                             parent_url="https://example.com/parent"),
                              build_terrain_fn=fake_build_terrain)

    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert "parentUrl" not in layer, "植入失败后偷偷退回了 parentUrl 兜底"
