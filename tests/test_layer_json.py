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
