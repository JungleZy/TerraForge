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
    # 应用侧默认后端是 grid，不是 auto —— 实测 auto 在 6 个真实 DEM 上一个
    # Pareto 前沿都没进（多花 2.6~5.9 倍时间，省下的体积用「降一级」买更便宜）。
    # 依据见 docs/reference/terrain/tiling-presets-measured.md 第三、四节。
    # CLI 与全球底图脚本仍用 auto，那是有意分叉，见同文档第八节。
    # 少了这两条断言，把 tile_dem_task_dir 里的 triangulator/max_error_k 两行
    # 删掉全量照样绿。
    assert captured["triangulator"] == "grid"
    assert captured["max_error_k"] == 0.15


def test_tile_dem_task_dir_threads_normals_and_offset(tmp_path: Path):
    """TileParams 的 normals / level_offset 必须真的进到 build_terrain 的 kwarg。

    normals 此前【根本没传】，走 build_terrain 的 kwarg 默认 True —— 应用侧
    因此拿不到关掉法线的能力，而实测法线吃 +35%~+100% 字节、约 2.1 倍时间，
    几何精度分毫不涨（tiling-presets-measured.md 第五节）。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    captured = {}

    def fake_build_terrain(**kwargs):
        captured.update(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text(
            '{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0, "max_level": 13,
                "chose_martini": 0, "chose_grid": 1}

    params = TileParams(maxzoom=14, parent_url="https://example.com/p.json",
                        normals=True, level_offset=-1)
    counts = tile_dem_task_dir(task_dir, out_dir, params,
                               build_terrain_fn=fake_build_terrain)

    assert captured["normals"] is True
    assert captured["level_offset"] == -1
    # 基准仍按用户的 maxzoom 传，偏移由 build_terrain 叠加 —— tiler 不自己算终值。
    assert captured["max_level"] == 14
    # build_terrain 回报的实际层级必须穿过白名单活着出来。
    assert counts["max_level"] == 13


def test_tile_dem_task_dir_defaults_normals_off():
    """应用侧默认不出法线：前端 enableLighting 默认关，白背 26%~50% 字节。"""
    from src.services.terrain_tiling.dem_task_tiler import TileParams

    assert TileParams(maxzoom=14, parent_url="").normals is False
    assert TileParams(maxzoom=14, parent_url="").level_offset == 0


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

    # max_level 不在替身的返回里 —— 归一成 None（「未知」）而不是 0：0 是合法
    # 层级（maxzoom<=1 配 speed 档就切到 0），拿它当未知会让管理器把
    # effective_maxzoom 落成假的 0。它自己的守卫在
    # test_tile_dem_task_dir_threads_normals_and_offset。
    assert got == {"total": 40, "rendered": 37, "failed": 3, "max_level": None,
                   "chose_martini": 11, "chose_grid": 26}


def test_tile_dem_task_dir_zero_fills_counts_for_legacy_stubs(tmp_path: Path):
    """老的测试替身返回 None 时,六个 key 必须齐全,而不是少几个。

    调用方按"无计数信息"处理,行为与加计数之前一致;少 key 会让调用方 KeyError。

    五个**计数**归零，但 max_level 归 None：它是层级不是计数，0 有它自己的含义
    （真的只切到 z0），与「切片器没回报」必须可区分 —— 两个管理器正是靠这个
    区分决定要不要把 effective_maxzoom 落库。
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

    assert got == {"total": 0, "rendered": 0, "failed": 0, "max_level": None,
                   "chose_martini": 0, "chose_grid": 0}


def test_tile_dem_task_dir_keeps_level_zero_distinct_from_unknown(tmp_path: Path):
    """build_terrain 回报 max_level=0 时必须原样出来，不能被当成「未知」。

    0 是合法层级：maxzoom=0（或 maxzoom=1 配 speed 档）真的只切 z0。若白名单
    把它和「替身没回报」压成同一个值，两个管理器要么把 effective_maxzoom 落成
    假 0、要么把真 0 当成 NULL 回落到基准值 —— 两种都是错数字。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    def fake_build_terrain(**kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0, "max_level": 0,
                "chose_martini": 0, "chose_grid": 1}

    got = tile_dem_task_dir(task_dir, out_dir,
                            TileParams(maxzoom=0, parent_url="https://example.com/p.json"),
                            build_terrain_fn=fake_build_terrain)

    assert got["max_level"] == 0, "真的切到 z0 被归成了别的值"
    assert got["max_level"] is not None


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


def test_unpack_failure_falls_back_instead_of_killing_the_job(tmp_path: Path, monkeypatch):
    """解压失败 → 退回 parentUrl 兜底，**不能**让整个切片任务失败。

    ensure_base_unpacked 把「assets/ 不可写」也包装成 RuntimeError（打包安装到
    Program Files、从只读介质运行都会命中，它自己的 docstring 就是这么写的）。
    不接住的话，v0.2.8 能正常切片的场景在这版变成整个地形任务失败 —— 功能回归，
    而且报错文案是「随包底图解压失败」，用户不会知道这本来是可以忽略的。

    这里退回兜底是干净的：解压阶段失败时任务目录一个字节都还没被碰过，语义与
    「分卷缺失返回 None」完全一致。graft 阶段失败则必须让任务失败 —— 那时目录里
    已经躺着半个底图，缺的瓦片会让 Cesium 拿 404 并把整个 provider 降级成
    heightmap（另有用例钉住，别把两者混为一谈）。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text("{}", encoding="utf-8")

    def boom(**k):
        raise RuntimeError("随包底图解压失败：[Errno 30] Read-only file system")

    monkeypatch.setattr(mod, "ensure_base_unpacked", boom)

    mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=10, parent_url="https://example.com/parent"),
        build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 0, "退回兜底后必须从 z0 开始切，否则低层级整个空缺"
    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert '"parentUrl": "https://example.com/parent"' in layer


def test_tiler_hands_the_raw_maxzoom_and_a_constant_min_level(tmp_path: Path, monkeypatch):
    """底图可用时 tiler 恒传 min_level=8，并原样交出用户请求的 maxzoom。

    这两个值决定下游那道钳位的两个输入。maxzoom=5 是最能说明问题的取值：
    交出去的是 8 和 5，min_level > max_level —— 若没人钳，_tile_ranges() 产出
    空区间，任务 rendered=0 却报 completed，又一款静默成功。

    钳位**不在这里做**：档位偏移后的最终层级只有 build_terrain 知道，所以由它的
    `min_level = min(min_level, max_level)` 收口。「真的没切成零张」由
    tests/test_fix_terrain_estimate_max_level.py::test_min_level_is_clamped_below_the_effective_max
    钉（同样的 8/5 组合，跑真实 build_terrain，断言 total > 0）；本条只钉 tiler
    这一侧交出的两个入参，替身根本不切瓦片。
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

    assert seen["min_level"] == 8, "min_level 恒传 8，钳位由 build_terrain 负责"
    assert seen["max_level"] == 5, (
        "tiler 必须原样交出用户请求的 maxzoom，不能自己叠偏移 —— 否则下游会拿"
        "偏移过的值当基准再叠一次。这里 level_offset 恒为 0，钉住『不自己算终值』"
        "的是 test_tile_dem_task_dir_threads_normals_and_offset（offset=-1、"
        "maxzoom=14，断言交出去的仍是 14）")


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

    可达路径：maxzoom=5 → 实际起切层级被钳成 5，任务自己就要写 z5。上一轮植入
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
        # 与 cesium_terrain._worker_tile 的落盘方式逐字一致
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


# ---------------------------------------------------------------------------
# 自动档 maxzoom：None 透传
# ---------------------------------------------------------------------------


def test_auto_maxzoom_reaches_build_terrain_as_none(tmp_path: Path, monkeypatch):
    """maxzoom=None（自动档）必须原样透传成 max_level=None。

    那是 build_terrain 里 estimate_max_level 分支的**唯一**触发条件
    （cesium_terrain 的 `if max_level is None:`）—— 换句话说，自动档能不能
    按源数据像素尺寸现算基准层级，全押在这一个 kwarg 上。

    断言写死 `is None`，不许松成「假值」：0 和 -1 都是数字，会被当成用户显式
    要求的层级。传 0 的后果尤其阴 —— 切出一张 z0 瓦片、layer.json 照常写、
    任务报 completed，正是本项目栽过好几次的那款静默产出错数据。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = _make_task_dir(tmp_path)
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 16, "available": []}', encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0, "max_level": 16}

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: None)

    counts = mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=None, parent_url="https://example.com/p.json"),
        build_terrain_fn=fake_build_terrain)

    assert seen["max_level"] is None, (
        f"自动档没透传成 None，到达 build_terrain 的是 {seen['max_level']!r} —— "
        f"任何数字都会被当成显式层级，estimate_max_level 那条分支就永远走不到")
    # 自动档下请求值是空的，估算出来的实际层级是 effective_maxzoom 的唯一来源，
    # 回报通路不能跟着一起断。
    assert counts["max_level"] == 16
