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
