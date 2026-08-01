"""下载性能与瓦片 cache 上限回归测试。

两个问题的钉死口径:
- connector 的 limit_per_host 过去恒为 TILE_SERVER_COUNT=4 —— 4 台服务器
  最多 16 条连接,把 concurrent_downloads=20+ 的配置悄悄压死(实测吞吐
  5 块/秒 vs 放开后 11+ 块/秒)。per-host 上限必须随并发配置缩放。
- cache_max_size_mb 配置过去没有任何消费方,瓦片 cache 无限增长(实际
  环境 6.1GB vs 配置的 1000MB)。enforce_cache_size_limit 按 LRU 清最久
  未用的瓦片;dem 目录(Earthdata 登录才能重下)不在清理范围。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把 Config 落盘路径 + 数据库全部指向 tmp_path 并建库(项目测试规约)。"""
    from core.config import Config
    from core import database

    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'config.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    database.init_database()
    (tmp_path / 'cache').mkdir(exist_ok=True)
    return tmp_path


def _make_file(path: Path, size: int, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'0' * size)
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


# ---------- connector 连接上限 ----------

def test_connector_per_host_limit_scales_with_concurrency(isolated_config, monkeypatch):
    """limit_per_host 不得成为并发瓶颈:并发 50 时 per-host 上限必须 ≥ 50。

    空瓦片列表也会建 session/connector,全程无网络。
    """
    import services.download_engine as de_mod
    from services.config_manager import ConfigManager

    ConfigManager().set('concurrent_downloads', '50')

    captured = {}
    real_connector = de_mod.aiohttp.TCPConnector

    def spy_connector(*args, **kwargs):
        captured.update(kwargs)
        return real_connector(*args, **kwargs)

    monkeypatch.setattr(de_mod.aiohttp, 'TCPConnector', spy_connector)

    engine = de_mod.DownloadEngine()
    asyncio.run(engine.download_tiles_batch([], 's'))

    assert captured.get('limit') == 50
    assert captured.get('limit_per_host', 0) >= 50, (
        f"limit_per_host={captured.get('limit_per_host')} 会把 50 的并发"
        f"压死在每 host 几条连接上(旧 bug: 恒为 4)"
    )


# ---------- cache 上限 LRU 清理 ----------

def test_cache_limit_evicts_oldest_tiles_first(isolated_config):
    """超限后按最久未用先清,直到回到上限内;最新瓦片保留。"""
    from core.config import Config
    from services.config_manager import ConfigManager
    from services.task_cleanup import enforce_cache_size_limit

    cache = Path(Config.CACHE_DIR)
    kb700 = 700 * 1024
    # 三块瓦片共 2.1MB,上限 1MB → 最旧的两块清掉,最新的留下(0.7MB ≤ 1MB)
    old1 = _make_file(cache / 's' / '10' / '1' / '1.png', kb700, age_days=30)
    old2 = _make_file(cache / 's' / '10' / '1' / '2.png', kb700, age_days=20)
    new1 = _make_file(cache / 's' / '10' / '1' / '3.png', kb700, age_days=2)

    ConfigManager().set('cache_max_size_mb', '1')
    stats = enforce_cache_size_limit()

    assert not old1.exists(), "最旧的瓦片应第一个被清"
    assert not old2.exists(), "次旧的瓦片应第二个被清"
    assert new1.exists(), "最新的瓦片必须保留"
    assert stats['removed_files'] == 2
    assert stats['removed_bytes'] == 2 * kb700


def test_cache_limit_spares_dem_and_part_files(isolated_config):
    """dem 目录与原子写临时件(*.part.*)不参与清理。"""
    from core.config import Config
    from services.config_manager import ConfigManager
    from services.task_cleanup import enforce_cache_size_limit

    cache = Path(Config.CACHE_DIR)
    kb700 = 700 * 1024
    old1 = _make_file(cache / 's' / '10' / '1' / '1.png', kb700, age_days=30)
    old2 = _make_file(cache / 's' / '10' / '1' / '2.png', kb700, age_days=20)
    dem = _make_file(cache / 'dem' / 'ASTGTM_003' / 'granule.zip', 5 * 1024 * 1024, age_days=60)
    part = _make_file(cache / 's' / '10' / '1' / '3.png.part.123.456', 100, age_days=60)

    # 瓦片共 1.4MB > 上限 1MB → 只清最旧的 old1;dem 的 5MB 不计入账面
    ConfigManager().set('cache_max_size_mb', '1')
    stats = enforce_cache_size_limit()

    assert not old1.exists(), "超限后最旧的瓦片要清"
    assert old2.exists(), "回到上限内就停手"
    assert dem.exists(), "dem granule 不属于瓦片 cache 清理范围"
    assert part.exists(), "进行中的原子写临时件绝不能在 LRU 里被删"
    assert stats['removed_files'] == 1


def test_cache_limit_spares_recently_written_tiles(isolated_config):
    """一小时内写入的瓦片不清 —— 保护下载完还没拼接的在途任务。"""
    from core.config import Config
    from services.config_manager import ConfigManager
    from services.task_cleanup import enforce_cache_size_limit

    cache = Path(Config.CACHE_DIR)
    kb700 = 700 * 1024
    fresh = _make_file(cache / 's' / '10' / '1' / '1.png', kb700, age_days=0)
    old1 = _make_file(cache / 's' / '10' / '1' / '2.png', kb700, age_days=30)
    old2 = _make_file(cache / 's' / '10' / '1' / '3.png', kb700, age_days=20)

    # 上限 1MB:两块旧的清完仍 0.7MB ≤ 1MB;若 fresh 也被当候选就会被误删
    ConfigManager().set('cache_max_size_mb', '1')
    stats = enforce_cache_size_limit()

    assert not old1.exists() and not old2.exists()
    assert fresh.exists(), "刚下载的瓦片(在途任务待拼接)不能被 LRU 清掉"
    assert stats['removed_files'] == 2


def test_cache_limit_under_limit_is_noop(isolated_config):
    """未超限什么都不删。"""
    from core.config import Config
    from services.task_cleanup import enforce_cache_size_limit

    cache = Path(Config.CACHE_DIR)
    tile = _make_file(cache / 's' / '10' / '1' / '1.png', 1024, age_days=30)

    # 默认 1000MB,远超 1KB
    stats = enforce_cache_size_limit()

    assert tile.exists()
    assert stats['removed_files'] == 0


def test_cache_limit_zero_disables(isolated_config):
    """上限 0 = 不限制(不是清空 cache)。"""
    from core.config import Config
    from services.config_manager import ConfigManager
    from services.task_cleanup import enforce_cache_size_limit

    cache = Path(Config.CACHE_DIR)
    tile = _make_file(cache / 's' / '10' / '1' / '1.png', 1024, age_days=30)

    ConfigManager().set('cache_max_size_mb', '0')
    stats = enforce_cache_size_limit()

    assert tile.exists(), "0 必须按「不限制」处理,不能理解成清空"
    assert stats['removed_files'] == 0
