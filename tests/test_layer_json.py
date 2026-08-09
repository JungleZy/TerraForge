"""
Tests for layer json related DB schema bits.
"""

import os
import sys

import pytest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core import database
from src.core.config import Config


def test_dem_terrain_jobs_table_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    # Override database path for isolated test run
    monkeypatch.setattr(Config, "DATABASE_PATH", Path(str(db_path)))
    # Prevent init_database() from touching the real workspace
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")

    database.init_database()

    with database.get_connection_context() as conn:
        cur = conn.cursor()

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("dem_terrain_jobs",),
        )
        assert cur.fetchone() is not None

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_dem_terrain_jobs_status",),
        )
        assert cur.fetchone() is not None

        # Enforce spec-critical constraints: output_dir/maxzoom must be NOT NULL
        cur.execute("PRAGMA table_info(dem_terrain_jobs)")
        cols = {row[1]: row for row in cur.fetchall()}  # (cid,name,type,notnull,dflt_value,pk)
        assert cols["output_dir"][3] == 1
        assert cols["maxzoom"][3] == 1


def test_patch_layer_json_parent(tmp_path):
    import json

    from src.services.terrain_tiling.layer_json import patch_layer_json_parent

    layer_json_path = tmp_path / "layer.json"
    layer_json_path.write_text("{}", encoding="utf-8")

    patch_layer_json_parent(layer_json_path, "https://example.com/parent")

    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    assert data["parentUrl"] == "https://example.com/parent"


@pytest.mark.parametrize("given,expected", [
    # 存量 config 表里就是这个坏值（2026-08-05 之前的 DEFAULT_CONFIGS）
    ("http://localhost:5000/terrain/base/layer.json", "http://localhost:5000/terrain/base"),
    ("http://localhost:5000/terrain/base/layer.json/", "http://localhost:5000/terrain/base"),
    ("https://x.example/t/base/LAYER.JSON", "https://x.example/t/base"),
    # 已经是目录形式的原样保留（末尾斜杠也去掉，Cesium 自己会补）
    ("http://localhost:5000/terrain/base", "http://localhost:5000/terrain/base"),
    ("http://localhost:5000/terrain/base/", "http://localhost:5000/terrain/base"),
    # 空值不写 parentUrl 字段（无 parent 是合法形态）
    ("", None),
    (None, None),
])
def test_patch_layer_json_parent_strips_the_layer_json_suffix(tmp_path, given, expected):
    """parentUrl 必须是**目录**，带 /layer.json 会让 Cesium 整个 provider 降级。

    Cesium 对 parentUrl 做 appendForwardSlash() 后再拼 layer.json，所以传
    `.../base/layer.json` 会请求 `.../base/layer.json/layer.json` → 404。
    而它的 404 处理不抛错，是塞一个假的 heightmap-1.0 图层，并把
    heightmapStructure 写在共享 builder 上 —— 于是 requestTileGeometry 对
    **本任务自己的 quantized-mesh 瓦片**也走 heightmap 分支。

    实测后果（天山 N42E086，同一批瓦片只改 parentUrl，同源 localhost）：
      .../base/layer.json → 高程 -859 / -956 / -744
      .../base            → 高程 2656.6 / 1092.3 / 4154.2
    源 DEM 真值 2672 / 1086 / 4154 —— 4154 m 的山峰被解成海平面以下 744 m，
    而 hasVertexNormals 仍报 true、瓦片全 200、控制台无报错。

    剥离放在这里（唯一的写入点）而不是只改 DEFAULT_CONFIGS：改默认值只影响
    新建的库，存量 config 表里那一行还是坏的。
    """
    import json

    from src.services.terrain_tiling.layer_json import patch_layer_json_parent

    p = tmp_path / "layer.json"
    p.write_text("{}", encoding="utf-8")
    patch_layer_json_parent(p, given)

    data = json.loads(p.read_text(encoding="utf-8"))
    if expected is None:
        assert "parentUrl" not in data or data["parentUrl"] is None
    else:
        assert data["parentUrl"] == expected
        assert not data["parentUrl"].rstrip("/").lower().endswith("layer.json")


def test_default_parent_url_is_a_directory_everywhere_it_is_written():
    """三处默认值必须一致，且都不能以 layer.json 结尾。

    DEFAULT_CONFIGS 一份、两个 manager 各有一份兜底（config 表读不到时用）。
    改了一处漏掉另一处 = 部分部署仍然踩坑，而这类失败是静默的。
    """
    import re

    root = Path(__file__).resolve().parent.parent
    found = {}
    for rel in ("src/core/database.py",
                "src/services/dem_task_manager.py",
                "src/services/local_terrain_task_manager.py"):
        text = (root / rel).read_text(encoding="utf-8")
        urls = re.findall(r"['\"](https?://[^'\"]*?/terrain/base[^'\"]*)['\"]", text)
        assert urls, f"{rel} 里找不到 parentUrl 默认值"
        found[rel] = set(urls)
        for u in urls:
            assert not u.rstrip("/").lower().endswith("layer.json"), (
                f"{rel} 的默认 parentUrl 仍带 /layer.json：{u}")

    all_urls = set().union(*found.values())
    assert len(all_urls) == 1, f"三处默认值不一致：{found}"


# ---------------------------------------------------------------------------
# base 不可达时必须【不写】parentUrl
#
# 2026-08-05 的第一版修复只处理了「URL 带 /layer.json」这一条触发路径，
# 但根因是「parentUrl 指向一个不可达的资源」—— base_z8 从来没建过的装机上，
# 目录形式的 URL 照样 404、照样降级。实测（base_z8 不存在，parentUrl 已是
# 正确的 .../terrain/base）：末层 isHeightmap=true、heightmapStructure 有值、
# 高程 -859 / -956 / -744（真值 2672/1086/4154）、瓦片类型 HeightmapTerrainData、
# 法线无。也就是说第一版修复对**默认装机**毫无帮助。
# ---------------------------------------------------------------------------

def test_parent_url_is_dropped_when_the_base_terrain_is_missing(tmp_path):
    """base 目录不存在 / 没有 layer.json 时必须返回 None（= 不写 parentUrl）。

    写一个指向 404 的 parentUrl 比不写更糟：Cesium 拿不到它时**不报错**，
    而是塞一个假的 heightmap-1.0 图层，并把 heightmapStructure 写在共享
    builder 上 —— 于是本任务自己的 quantized-mesh 瓦片也按 heightmap 解析。
    不写 parentUrl 则一切正常（实测高程 2656.6/1092.3/4154.2，法线可用）。
    """
    from src.services.terrain_tiling.layer_json import parent_url_if_base_available

    url = "http://localhost:5000/terrain/base"

    # 目录压根不存在
    assert parent_url_if_base_available(url, tmp_path / "nope") is None

    # 目录在但没有 layer.json（切了一半 / 手工建了空目录）
    empty = tmp_path / "base_empty"
    empty.mkdir()
    assert parent_url_if_base_available(url, empty) is None

    # layer.json 是目录而不是文件
    weird = tmp_path / "base_weird"
    (weird / "layer.json").mkdir(parents=True)
    assert parent_url_if_base_available(url, weird) is None

    # base_dir 传 None（配置缺失）
    assert parent_url_if_base_available(url, None) is None


def test_parent_url_is_kept_and_normalized_when_the_base_exists(tmp_path):
    """base 可用时才写，且仍然要做目录规整。"""
    from src.services.terrain_tiling.layer_json import parent_url_if_base_available

    base = tmp_path / "base_z8"
    base.mkdir()
    (base / "layer.json").write_text('{"tilejson":"1.0"}', encoding="utf-8")

    assert parent_url_if_base_available(
        "http://localhost:5000/terrain/base", base) == "http://localhost:5000/terrain/base"
    # 存量 config 表里的坏值仍然要被剥掉
    assert parent_url_if_base_available(
        "http://localhost:5000/terrain/base/layer.json", base) == "http://localhost:5000/terrain/base"
    # 空 URL 仍然是不写
    assert parent_url_if_base_available("", base) is None
    assert parent_url_if_base_available(None, base) is None


def test_both_managers_gate_parent_url_on_base_availability():
    """两个 manager 都必须走这个闸门，而不是各自直接读配置值。

    DEM 与 local terrain 是两条独立管线，只修一条 = 另一条仍然产出高程全错的
    地形，而且失败是静默的（作业 completed、瓦片 200、控制台无报错）。
    """
    root = Path(__file__).resolve().parent.parent
    for rel in ("src/services/dem_task_manager.py",
                "src/services/local_terrain_task_manager.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "parent_url_if_base_available" in text, (
            f"{rel} 没有走 base 可用性闸门 —— base 不存在时会写出一个 404 的 "
            f"parentUrl，导致整个 provider 降级成 heightmap")
        assert "terrain_global_base_path" in text, (
            f"{rel} 没有读 terrain_global_base_path，无从判断 base 是否存在")


# ---------------------------------------------------------------------------
# layer.json 合成（底图植入之后的收尾）
#
# 植入把底图的 z0-z7 瓦片放进了任务目录，但 Cesium 只看 layer.json：声明里没
# 有的层它根本不请求，文件在磁盘上也等于不存在。而声明错、maxzoom 声明浅、
# parentUrl 留着指向 localhost，三者任何一个都会把整个 provider 拖进
# heightmap 降级链（详见 normalize_parent_url 的实测表）。
# ---------------------------------------------------------------------------

def _write_layer(path, *, maxzoom, available, parent=None, minzoom=0):
    import json
    data = {"tilejson": "1.0", "format": "quantized-mesh-1.0", "scheme": "tms",
            "projection": "EPSG:4326", "tiles": ["{z}/{x}/{y}.terrain"],
            "minzoom": minzoom, "maxzoom": maxzoom, "available": available}
    if parent:
        data["parentUrl"] = parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_merge_base_availability_unions_levels_and_drops_parent_url(tmp_path):
    """available 逐层并集，parentUrl 必须被删掉。

    自包含之后 parentUrl 是一次多余请求，而且它指向 localhost —— 目录拷到别的
    机器上必然 404，而 Cesium 的 404 处理会把整个 provider 降级成 heightmap。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=10,
                        available=[[]] * 8 + [
                            [{"startX": 5, "startY": 5, "endX": 6, "endY": 6}],
                            [{"startX": 10, "startY": 10, "endX": 12, "endY": 12}],
                            [{"startX": 20, "startY": 20, "endX": 24, "endY": 24}]],
                        parent="http://localhost:5000/terrain/base")

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert "parentUrl" not in data
    assert data["minzoom"] == 0
    assert data["maxzoom"] == 10
    assert len(data["available"]) == 11
    # z0-z7 来自底图
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert data["available"][7] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    # z8+ 来自任务；底图在这几层没有声明，并集结果就只有任务的
    assert data["available"][8] == [{"startX": 5, "startY": 5, "endX": 6, "endY": 6}]
    assert data["available"][10] == [{"startX": 20, "startY": 20, "endX": 24, "endY": 24}]
    # 其余字段原样保留：合成只碰 available / minzoom / maxzoom / parentUrl
    assert data["format"] == "quantized-mesh-1.0"
    assert data["tiles"] == ["{z}/{x}/{y}.terrain"]


def test_merge_keeps_base_levels_deeper_than_the_task(tmp_path):
    """maxzoom < 8 的退化任务：maxzoom 必须取 max(7, 任务的)。

    直接取任务的会把底图的 z6/z7 声明掉，Cesium 从此不请求它们 —— 明明文件
    就在目录里，却看不到。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=5,
                        available=[[]] * 5 + [[{"startX": 3, "startY": 3, "endX": 3, "endY": 3}]])

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert data["maxzoom"] == 7
    assert len(data["available"]) == 8
    # 同层相撞时两边的声明都保留（任务瓦片在磁盘上胜出，声明是并集）
    assert {"startX": 3, "startY": 3, "endX": 3, "endY": 3} in data["available"][5]
    assert {"startX": 0, "startY": 0, "endX": 1, "endY": 0} in data["available"][5]
    assert len(data["available"][5]) == 2
    assert data["available"][7] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]


def test_merge_aligns_by_absolute_level_not_by_index(tmp_path):
    """两侧 available 的原点不同时必须按**绝对层号**对齐，输出归一到 z0。

    `available[i]` 的绝对层号是 `minzoom + i` —— 见 `cesiumlab_terrain.build_terrain`
    里 `for z in range(min_level, max_level + 1)` 逐层 append 出的
    `available_per_level`，以及它写进 layer.json 的 `"minzoom": min_level`。
    今天任务侧下标 0 恰好就是 z0，只是因为随包底图不可用时 `tile_dem_task_dir`
    的 `min_level` 取 0；底图可用时任务只需要切 z8+，任务侧的原点就变成 8 了。

    按下标对齐会把任务的 z8 声明并到底图的 z0 上：文件全在，声明全错，Cesium
    拿着错声明去请求不存在的瓦片 → 404 → 假 heightmap 图层 → 共享 builder 污染
    → 高程全错且零报错。正是这个函数要防的那条链。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    # 真实形状：minzoom=8/maxzoom=10 的任务产出的是 **3 层** available，下标 0 是 z8
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=10, minzoom=8,
                        available=[[{"startX": 5, "startY": 5, "endX": 6, "endY": 6}],
                                   [{"startX": 10, "startY": 10, "endX": 12, "endY": 12}],
                                   [{"startX": 20, "startY": 20, "endX": 24, "endY": 24}]])

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert len(data["available"]) == 11
    # 底图的 z0 声明落在下标 0
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert data["available"][7] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    # 任务的 z8 声明落在下标 8，而不是被并到下标 0 上
    assert data["available"][8] == [{"startX": 5, "startY": 5, "endX": 6, "endY": 6}]
    assert data["available"][10] == [{"startX": 20, "startY": 20, "endX": 24, "endY": 24}]
    assert data["maxzoom"] == 10


def test_merge_forces_minzoom_to_zero_even_when_the_task_starts_deeper(tmp_path):
    """minzoom 必须硬置 0，不能沿用任务原值。

    底图可用时任务只切 z8+，它自己的 layer.json 写的就是 minzoom=8。植入之后
    z0 起的底图瓦片躺在同一个目录里，但 Cesium 用 minzoom 决定从哪一层开始请求
    —— 留着 8 就等于把刚植入的 8 层全部作废，屏幕上依旧只有那一小块。

    任务书给的两条用例里任务侧 minzoom 本来就是 0，杀不掉「删掉 minzoom=0 这
    一行」的变异体，所以补这条。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=10, minzoom=8,
                        available=[[{"startX": 5, "startY": 5, "endX": 6, "endY": 6}]] * 3)

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert data["minzoom"] == 0


def test_merge_maxzoom_never_shallower_than_the_declared_levels(tmp_path):
    """底图漏写 maxzoom 时，maxzoom 仍不能浅于 available 的最深层。

    底图有 8 层 available 却没写 maxzoom（用户自备的 layer.json 常见），任务又
    是 maxzoom=5 的退化任务 —— 只取两边 maxzoom 的话结果是 8 层声明配 maxzoom=5，
    底图 z6/z7 的文件躺在目录里但永远不会被请求，一字不差就是本函数要防的那条。

    往深了报是安全边：两个方向的代价不对称 —— 报浅是硬闸门，整层直接消失；报深
    由 available 逐层兜住（空层 isTileAvailable 返回 false，Cesium 不去请求），
    代价是零。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = tmp_path / "base" / "layer.json"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps({
        "tilejson": "1.0", "minzoom": 0,
        "available": [[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8,
    }), encoding="utf-8")

    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=5,
                        available=[[]] * 5 + [[{"startX": 3, "startY": 3,
                                                "endX": 3, "endY": 3}]])

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert len(data["available"]) == 8
    assert data["maxzoom"] == 7


def test_merge_declares_base_levels_when_the_task_produced_no_tiles(tmp_path):
    """任务侧一张瓦片都没切出来时，结果必须仍然声明底图的 8 层。

    切片失败/范围落空会产出 available 全空的 layer.json。此时目录里唯一有内容
    的就是植入的底图，合成如果跟着塌成空声明，用户看到的是一个纯黑的球，而不是
    「底图有、细节没有」—— 后者才对得上磁盘上的事实。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=10,
                        available=[[]] * 11)

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert data["maxzoom"] == 10
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert data["available"][7] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    assert data["available"][8] == []


def test_merge_tolerates_missing_and_null_fields(tmp_path):
    """字段缺失 / 为 null 不能把合成打断在半路。

    layer.json 不是只有 ctb-tile 会产出（底图允许用户自备，见
    docs/reference/terrain/global-base-build.md），少写 available 或 maxzoom 都
    见得到。这里的取舍是：**结构性缺失容忍，内容损坏不容忍** —— JSON 本身解不开
    就让异常抛出去，由调用方回滚，静默吞掉只会产出一个没人知道是坏的 layer.json。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = tmp_path / "base" / "layer.json"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps({"tilejson": "1.0"}), encoding="utf-8")

    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=9,
                        available=[[]] * 9 + [[{"startX": 1, "startY": 1,
                                                "endX": 1, "endY": 1}]])

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert data["maxzoom"] == 9
    assert data["available"][9] == [{"startX": 1, "startY": 1, "endX": 1, "endY": 1}]

    # available / maxzoom 显式为 null 也一样（json.dumps(None) 会走到这一支）
    base.write_text(json.dumps({"available": None, "maxzoom": None}), encoding="utf-8")
    task2 = _write_layer(tmp_path / "task2" / "layer.json", maxzoom=None,
                         available=None)

    merge_base_availability(task2, base)
    data2 = json.loads(task2.read_text(encoding="utf-8"))

    assert data2["available"] == []
    assert data2["maxzoom"] == 0


def test_merge_is_idempotent(tmp_path):
    """重跑一次不能把声明翻倍。

    植入失败重试、或者用户对同一个任务目录再点一次「合成」，都会让这个函数在
    已合成过的 layer.json 上再跑一遍。重复的 range 不会让 Cesium 报错，但声明
    会随重试次数线性膨胀，而 layer.json 是每次加载都要下载解析的。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=8,
                        available=[[]] * 8 + [[{"startX": 5, "startY": 5,
                                                "endX": 6, "endY": 6}]])

    merge_base_availability(task, base)
    first = json.loads(task.read_text(encoding="utf-8"))
    merge_base_availability(task, base)
    second = json.loads(task.read_text(encoding="utf-8"))

    assert second == first
    assert len(second["available"][0]) == 1
