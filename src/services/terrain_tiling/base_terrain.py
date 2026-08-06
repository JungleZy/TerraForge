"""随包分发的全球底图：解压到共享缓存，再植入任务目录。

**刻意不 import numpy / osgeo。** 这里全是文件操作，不 import 才能在没有
GDAL 的环境里单测 —— 与同目录的 layer_json.py 一个定位。

共享缓存放在 assets/terrain/base_z8，与分卷同目录：assets/ 是随包分发的数据，
downloads/ 是用户产出，解压出来的底图属于前者。打包模式下 Config.BASE_DIR
（sys.executable 所在目录）恒等于 bundle_dir()，正是 --include-data-dir 把
assets/terrain 放进去的地方，所以两种运行模式路径都对得上。
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.core.bundle import bundle_dir
from src.core.config import Config

logger = logging.getLogger(__name__)

PARTS_GLOB = "base_z8.tar.gz.part*"
BASE_DIR_NAME = "base_z8"

# 就位判据抽查这三层的顶层 x 目录数，而不是全量 walk：44k 个文件在 Windows 上
# 数一遍要几秒，而这个判据在每次切片开头都会跑。
_PROBE_LEVELS = ((0, 2), (4, 32), (7, 256))


def _assets_terrain_dir() -> Path:
    base = bundle_dir()
    root = Path(base) if base else Path(Config.BASE_DIR)
    return root / "assets" / "terrain"


def base_parts_dir() -> Path | None:
    """分卷所在目录；没有分卷返回 None（= 底图不可用，调用方退回 parentUrl）。"""
    d = _assets_terrain_dir()
    try:
        if any(d.glob(PARTS_GLOB)):
            return d
    except OSError:
        return None
    return None


def base_cache_dir() -> Path:
    """解压目标 = 分卷同目录下的 base_z8。"""
    return _assets_terrain_dir() / BASE_DIR_NAME


def is_base_ready(cache_dir: Path) -> bool:
    """已解压且看起来完整。

    只看 layer.json 存在是不够的：解压中途被打断也会留下它，而一个 layer.json
    齐全、瓦片残缺的底图会让 Cesium 拿到 404 瓦片 —— 它不报错，而是塞一个假的
    heightmap-1.0 图层并污染共享 builder，连任务自己的 quantized-mesh 瓦片都
    被按 heightmap 解析（v0.2.8 实测：4154 m 山峰解成 -744 m，零报错）。
    """
    cache_dir = Path(cache_dir)
    if not (cache_dir / "layer.json").is_file():
        return False
    for z, expect_x in _PROBE_LEVELS:
        d = cache_dir / str(z)
        try:
            if not d.is_dir() or sum(1 for _ in d.iterdir()) < expect_x:
                return False
        except OSError:
            return False
    return True
