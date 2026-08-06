"""随包底图的解压与植入 —— 纯文件操作，不碰 GDAL。

⚠️ 本文件所有用例必须写在 tmp_path 里。往仓库的 assets/terrain/ 写东西会被
CI 打进产物：流水线里测试跑在 Nuitka 打包之前。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_base(root, layers=((0, 2), (1, 4), (2, 8), (3, 16),
                             (4, 32), (5, 64), (6, 128), (7, 256)),
               dense=False):
    """造一个 base_z8 骨架。dense=False 时每层只建 x 目录不建瓦片（够就位判据用）。

    layers 里第二个数是**该层的 x 目录数，不是瓦片数**。EPSG:4326 是 2:1 地理
    网格：z 层有 2·2^z 个 x 目录，每个 x 目录下 2^z 个 y 文件，所以 z7 是 256 个
    目录 / 32768 个瓦片，全 8 层合计 510 个目录 / 43,690 个瓦片。

    照上面这串真实目录数建出来的骨架，恰好卡在 _PROBE_LEVELS 的边界上（z0/z4/z7
    要求 ≥2/32/256），少一个目录就该红 —— 这正是这些用例的护栏价值。若误用瓦片数
    当目录数，会建出多达 128 倍的目录，阈值被远远超过，边界断言全部失效（还会把
    单文件耗时从毫秒拖到 11 s）。
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")
    for z, nx in layers:
        for x in range(nx):
            d = root / str(z) / str(x)
            d.mkdir(parents=True, exist_ok=True)
            if dense:
                (d / "0.terrain").write_bytes(b"\x1f\x8bfake")
    return root


def test_is_base_ready_requires_layer_json_and_all_probe_levels(tmp_path):
    """就位判据：layer.json + z0/z4/z7 的 x 目录数都够。

    只看 layer.json 不够 —— 解压中途被打断也会留下它，而一个 layer.json 齐全
    但瓦片残缺的底图会让 Cesium 拿到 404 瓦片，进而塞假 heightmap 图层污染
    共享 builder（v0.2.8 修过这条链）。
    """
    from src.services.terrain_tiling.base_terrain import is_base_ready

    good = _make_base(tmp_path / "good")
    assert is_base_ready(good) is True

    # layer.json 缺失
    no_lj = _make_base(tmp_path / "no_lj")
    (no_lj / "layer.json").unlink()
    assert is_base_ready(no_lj) is False

    # z7 只解到一半
    half = _make_base(tmp_path / "half", layers=((0, 2), (4, 32), (7, 128)))
    assert is_base_ready(half) is False

    # z0 差一个目录，其余层齐全 —— 单独钉住 z0 这条探针
    z0_short = _make_base(tmp_path / "z0_short",
                          layers=((0, 1), (1, 4), (2, 8), (3, 16),
                                  (4, 32), (5, 64), (6, 128), (7, 256)))
    assert is_base_ready(z0_short) is False

    # z4 差一个目录，其余层齐全 —— 单独钉住 z4 这条探针
    z4_short = _make_base(tmp_path / "z4_short",
                          layers=((0, 2), (1, 4), (2, 8), (3, 16),
                                  (4, 31), (5, 64), (6, 128), (7, 256)))
    assert is_base_ready(z4_short) is False

    # 目录压根不存在
    assert is_base_ready(tmp_path / "nope") is False


def test_base_parts_dir_returns_none_without_parts(tmp_path, monkeypatch):
    """分卷不在（有人删了 assets/）时返回 None —— 调用方据此退回 parentUrl 兜底。"""
    from src.core import config as config_mod
    from src.services.terrain_tiling import base_terrain

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(base_terrain, "bundle_dir", lambda: None)
    assert base_terrain.base_parts_dir() is None

    parts = tmp_path / "assets" / "terrain"
    parts.mkdir(parents=True)
    (parts / "base_z8.tar.gz.partaa").write_bytes(b"x")
    assert base_terrain.base_parts_dir() == parts


def test_base_cache_dir_sits_next_to_the_parts(tmp_path, monkeypatch):
    """缓存与分卷同目录：assets/ 是随包数据，downloads/ 是用户产出。"""
    from src.core import config as config_mod
    from src.services.terrain_tiling import base_terrain

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(base_terrain, "bundle_dir", lambda: None)
    assert base_terrain.base_cache_dir() == tmp_path / "assets" / "terrain" / "base_z8"
