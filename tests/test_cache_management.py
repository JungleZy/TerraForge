"""下载缓存分类统计与手动清理测试(services/task_cleanup + /api/cache/*)。

缓存不做任何自动清理(旧的 LRU 强清已移除):get_cache_stats 只读统计,
clear_cache_category 由前端二次确认后手动触发;非法/越界/不存在的
分类一律 ValueError,绝不可误删 cache 之外的任何东西。
"""

import pytest

from src.core.config import Config
from src.services.task_cleanup import clear_cache_category, get_cache_stats


def _make_cache(root):
    """构造 cache/m、cache/s、cache/dem 三个分类 + 一个顶层散落文件。"""
    cache = root / "cache"
    (cache / "m" / "10" / "1").mkdir(parents=True)
    (cache / "m" / "10" / "1" / "2.png").write_bytes(b"x" * 100)
    (cache / "m" / "10" / "1" / "3.png").write_bytes(b"x" * 50)
    (cache / "s" / "10" / "1").mkdir(parents=True)
    (cache / "s" / "10" / "1" / "2.png").write_bytes(b"x" * 200)
    (cache / "dem").mkdir(parents=True)
    (cache / "dem" / "N29E106.tif").write_bytes(b"x" * 1000)
    (cache / "stray.txt").write_bytes(b"x" * 7)
    return cache


# ---------- get_cache_stats ----------

def test_get_cache_stats_categorizes(tmp_path):
    cache = _make_cache(tmp_path)
    stats = get_cache_stats(cache)
    by_key = {c["key"]: c for c in stats["categories"]}
    assert by_key["m"]["size_bytes"] == 150
    assert by_key["m"]["file_count"] == 2
    assert by_key["m"]["label"] == "瓦片缓存（m）"
    assert by_key["s"]["size_bytes"] == 200
    assert by_key["dem"]["label"] == "DEM 缓存"
    assert by_key["dem"]["size_bytes"] == 1000
    assert by_key["_root"]["file_count"] == 1
    assert by_key["_root"]["label"] == "其他"
    assert stats["total_bytes"] == 150 + 200 + 1000 + 7


def test_get_cache_stats_missing_root(tmp_path):
    stats = get_cache_stats(tmp_path / "nope")
    assert stats == {"categories": [], "total_bytes": 0}


# ---------- clear_cache_category ----------

def test_clear_cache_category_removes_dir(tmp_path):
    cache = _make_cache(tmp_path)
    result = clear_cache_category("m", cache)
    assert result == {"removed_bytes": 150, "removed_files": 2}
    assert not (cache / "m").exists()
    assert (cache / "s").exists()  # 其他分类不受影响


def test_clear_cache_category_root_only_files(tmp_path):
    cache = _make_cache(tmp_path)
    result = clear_cache_category("_root", cache)
    assert result == {"removed_bytes": 7, "removed_files": 1}
    assert not (cache / "stray.txt").exists()
    assert (cache / "m").exists()  # 子目录不碰


@pytest.mark.parametrize("bad", ["..", "../m", "m/x", "/abs", "", None, "m\\x"])
def test_clear_cache_category_rejects_bad_names(tmp_path, bad):
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        clear_cache_category(bad, cache)
    assert (cache / "m").exists()  # 什么都没删


def test_clear_cache_category_rejects_missing(tmp_path):
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        clear_cache_category("q", cache)


# ---------- /api/cache/* ----------

def test_api_cache_stats(isolated_app):
    _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.get("/api/cache/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    keys = {c["key"] for c in data["categories"]}
    assert {"m", "s", "dem", "_root"} <= keys
    assert data["total_bytes"] == 150 + 200 + 1000 + 7


def test_api_cache_clear_one(isolated_app):
    cache = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json={"category": "m"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["total_removed_bytes"] == 150
    assert not (cache / "m").exists()
    assert (cache / "dem").exists()


def test_api_cache_clear_all(isolated_app):
    cache = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json={"category": "__all__"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_removed_bytes"] == 150 + 200 + 1000 + 7
    assert not (cache / "m").exists()
    assert not (cache / "s").exists()
    assert not (cache / "dem").exists()
    assert not (cache / "stray.txt").exists()


@pytest.mark.parametrize("body", [{}, {"category": ".."}, {"category": "q"},
                                 {"category": 123}])
def test_api_cache_clear_rejects_bad_input(isolated_app, body):
    cache = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json=body)
    assert resp.status_code == 400
    assert (cache / "m").exists()  # 什么都没删
