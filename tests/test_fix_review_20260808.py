"""2026-08-08 全项目评审的 P0 回归测试（docs/reviews/2026-08-08-full-project-review.md）。

覆盖：
- B2 单实例冲突提示不得再引导用户删锁文件（删了不解锁，只会让两个实例同时在跑，
  第二个实例的启动清扫会删掉第一个实例正在写的中间产物）；
- T1 瓦片索引上界按半开区间算 —— DEM 四至落在瓦片边界上时不再多出一整行/列
  「与 DEM 零重叠却被声明 available」的假地形；
- T2 底图缓存迁移原子落地 —— 跨盘拷贝被中断时目标位置**不存在**，而不是留一棵
  只有 layer.json 的半树（那会被判为可用 → 高程全错且零报错，且 user_version
  已推到 3 永不重试）。

B1（build.sh 的 GDAL 闸门）在 tests/test_fix_l1_entry_build_misc.py 与
tests/test_fix_build_scripts.py 里，与既有的构建脚本用例放在一起。
"""
import os
import re
import shutil
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


# --------------------------------------------------------------------------
# B2: 冲突提示不得引导用户删锁文件
# --------------------------------------------------------------------------

def _conflict_message(monkeypatch):
    """让 acquire_instance_lock 返回 False，抓 _enforce_single_instance 打的那段话。"""
    from src import app_factory

    monkeypatch.setattr('src.core.single_instance.acquire_instance_lock',
                        lambda: False)
    captured = []
    monkeypatch.setattr(app_factory.logger, 'error', captured.append)
    monkeypatch.setattr('builtins.print', lambda *a, **k: None)

    with pytest.raises(SystemExit) as exc:
        app_factory._enforce_single_instance()
    assert exc.value.code == 1
    assert captured, '冲突时必须记一条 error'
    return captured[0]


def test_lock_conflict_message_never_tells_the_user_to_delete_it(monkeypatch):
    """旧提示是「若确认上一个实例已崩溃退出，删除 <lock> 后重试」。

    那条建议在 POSIX 上会直接制造「两个实例同时持锁」（机制由
    tests/test_single_instance_lock.py 的 test_deleting_the_lock_file_defeats_the_mutex
    钉住），而它的前提也不成立 —— 进程死了 OS 就释放了锁，不存在陈旧锁文件。
    """
    msg = _conflict_message(monkeypatch)

    assert not re.search(r'删除[^\n]*\.lock[^\n]*后重试', msg), (
        '提示语又在建议删锁文件后重试 —— 删了不解锁，只会让两个实例同时在跑')
    assert not re.search(r'(?<!不)(?<!不要)(?<!不需要、也不要)手动删除', msg), (
        '「手动删除」只能以否定形式出现')


def test_lock_conflict_message_explains_why_deleting_does_not_help(monkeypatch):
    """光删掉旧建议不够 —— 用户会自己想到去删，必须当场说清没用。"""
    msg = _conflict_message(monkeypatch)
    assert '不会解锁' in msg, '必须明说删掉锁文件不会解锁'
    assert 'TERRAFORGE_ALLOW_MULTI_INSTANCE' in msg, '真需要并行时的逃生口不能丢'


# --------------------------------------------------------------------------
# T1: 瓦片索引上界 = 半开区间
# --------------------------------------------------------------------------

from src.services.terrain_tiling.cesium_terrain import (  # noqa: E402
    GeographicTilingScheme, intersecting_tile_range,
)


def _overlaps(nx, ny, ix, iy, west, south, east, north):
    """瓦片 (ix, iy) 与给定 bbox 的重叠面积是否为正（相切不算相交）。"""
    step_x, step_y = 360.0 / nx, 180.0 / ny
    t_w, t_e = -180.0 + ix * step_x, -180.0 + (ix + 1) * step_x
    t_s, t_n = -90.0 + iy * step_y, -90.0 + (iy + 1) * step_y
    return (min(t_e, east) - max(t_w, west) > 0
            and min(t_n, north) - max(t_s, south) > 0)


def _assert_range_is_exactly_the_overlapping_tiles(z, bounds):
    """返回的闭区间必须【恰好】是与 DEM 真正相交的那些瓦片：不多也不少。"""
    nx, ny = GeographicTilingScheme(tile_size=65).tile_count(z)
    west, south, east, north = bounds
    ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, west, south, east, north)

    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            assert _overlaps(nx, ny, ix, iy, *bounds), (
                f'z={z} tile ({ix},{iy}) 与 DEM 零重叠却被纳入范围 —— '
                '它会出一张假地形并被声明 available')
    # 边界外一圈不能有漏掉的真相交瓦片
    for ix, iy in ((ix0 - 1, iy0), (ix1 + 1, iy0), (ix0, iy0 - 1), (ix0, iy1 + 1)):
        if 0 <= ix < nx and 0 <= iy < ny and _overlaps(nx, ny, ix, iy, *bounds):
            pytest.fail(f'z={z} tile ({ix},{iy}) 真的相交却被排除在范围外')


# 北界 45.0 是 1°x1° granule 的常见值（ASTGTM/SRTM 在 45N 处切分），而
# (45+90)/180*2^z = 0.75*2^z 对 z>=2 恒为整数 —— 每一级都会踩到边界。
@pytest.mark.parametrize('z', [2, 5, 8, 11])
def test_dem_edge_on_a_tile_boundary_adds_no_phantom_row(z):
    _assert_range_is_exactly_the_overlapping_tiles(z, (85.0, 44.0, 86.0, 45.0))


@pytest.mark.parametrize('z', [2, 5, 8, 11])
def test_dem_edge_on_a_tile_boundary_adds_no_phantom_column(z):
    # 东界 45.0：(45+180)/360*2^(z+1) = 0.625*2^(z+1)，z>=2 时同样是整数
    _assert_range_is_exactly_the_overlapping_tiles(z, (44.0, 10.0, 45.0, 11.0))


@pytest.mark.parametrize('z', [3, 6, 9])
def test_interior_bounds_are_unchanged_by_the_half_open_rule(z):
    """回归保护：四至不在边界上时，新算法必须与 floor 逐字等价（不能砍掉真覆盖）。"""
    nx, ny = GeographicTilingScheme(tile_size=65).tile_count(z)
    import math
    for west, south, east, north in [(85.3, 44.2, 86.7, 44.9),
                                     (-120.4, -33.7, -119.1, -32.2),
                                     (0.3, 0.4, 1.7, 1.9)]:
        got = intersecting_tile_range(nx, ny, west, south, east, north)
        legacy = (
            max(0, int(math.floor((west + 180.0) / 360.0 * nx))),
            min(nx - 1, int(math.floor((east + 180.0) / 360.0 * nx))),
            max(0, int(math.floor((south + 90.0) / 180.0 * ny))),
            min(ny - 1, int(math.floor((north + 90.0) / 180.0 * ny))),
        )
        assert got == legacy, f'z={z} 内部区间不该变: {got} != {legacy}'


def test_dem_thinner_than_one_tile_still_yields_one_tile():
    """退化情形：DEM 窄于一格且整体贴在边界上，上界不能掉到下界之下。"""
    nx, ny = GeographicTilingScheme(tile_size=65).tile_count(2)
    ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, 45.0, 45.0, 45.0, 45.0)
    assert ix0 <= ix1 and iy0 <= iy1
    assert (ix1 - ix0, iy1 - iy0) == (0, 0), '零面积的 DEM 只该落在一格里'


def test_global_bounds_still_cover_every_tile():
    nx, ny = GeographicTilingScheme(tile_size=65).tile_count(4)
    assert intersecting_tile_range(nx, ny, -180.0, -90.0, 180.0, 90.0) == (
        0, nx - 1, 0, ny - 1)


# --------------------------------------------------------------------------
# T2: 底图缓存迁移必须原子落地
# --------------------------------------------------------------------------

@pytest.fixture
def migration_env(monkeypatch, tmp_path):
    """一个 user_version=2 且 config 行仍是旧路径的库 + 旧位置的底图目录。"""
    from src.core import config as config_mod
    from src.core import database

    monkeypatch.setattr(config_mod.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(config_mod.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(database.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(database.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')

    old_dir = tmp_path / 'downloads' / 'terrain' / 'base_z8'
    (old_dir / '8' / '0').mkdir(parents=True)
    (old_dir / 'layer.json').write_text('{"available": []}', encoding='utf-8')
    (old_dir / '8' / '0' / '0.terrain').write_bytes(b'\x00' * 16)

    conn = sqlite3.connect(tmp_path / 'm.db')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute("INSERT INTO config VALUES ('terrain_global_base_path', ?)",
                 (database._OLD_BASE_PATH,))
    conn.execute('PRAGMA user_version = 2')
    conn.commit()

    new_dir = tmp_path / 'assets' / 'terrain' / 'base_z8'
    yield database, conn, old_dir, new_dir
    conn.close()


def _stored_path(conn):
    return conn.execute(
        "SELECT value FROM config WHERE key = 'terrain_global_base_path'"
    ).fetchone()['value']


def test_same_filesystem_migration_moves_the_tree(migration_env):
    database, conn, old_dir, new_dir = migration_env

    assert database.migrate_base_path_to_assets(conn.cursor()) is True

    assert (new_dir / 'layer.json').is_file()
    assert (new_dir / '8' / '0' / '0.terrain').is_file()
    assert not old_dir.exists()
    assert _stored_path(conn) == database._NEW_BASE_PATH
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 3


def test_cross_device_migration_lands_atomically(migration_env, monkeypatch):
    """跨盘时 os.replace(目录) 会失败，退化成 copytree —— 也必须原子落地。"""
    database, conn, old_dir, new_dir = migration_env
    real_replace = os.replace

    def _replace(src, dst, *a, **k):
        if os.fspath(src) == os.fspath(old_dir):
            raise OSError(18, 'Invalid cross-device link')
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(database.os, 'replace', _replace)

    assert database.migrate_base_path_to_assets(conn.cursor()) is True

    assert (new_dir / '8' / '0' / '0.terrain').is_file()
    assert not new_dir.with_name(new_dir.name + '.part').exists(), '暂存目录必须清掉'
    assert not old_dir.exists()


def test_interrupted_cross_device_copy_leaves_no_half_tree(migration_env, monkeypatch):
    """核心不变量：拷贝中断时目标位置**不存在**。

    旧实现直接 shutil.move 到最终位置，跨盘退化成 copytree 后被中断就留下一棵
    只有 layer.json 的半树 —— layer_json.parent_url_if_base_available 只看
    layer.json 判可用，于是底图被当成完整的，高程全错且零报错；而
    user_version 已经推到 3，永不重试。
    """
    database, conn, old_dir, new_dir = migration_env

    def _boom_after_root_files(src, dst, *a, **k):
        # 模拟 copytree 已经拷完根级文件（layer.json）才断
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(os.path.join(src, 'layer.json'), os.path.join(dst, 'layer.json'))
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(database.os, 'replace',
                        lambda src, dst, *a, **k: (_ for _ in ()).throw(
                            OSError(18, 'Invalid cross-device link')))
    monkeypatch.setattr(database.shutil, 'copytree', _boom_after_root_files)

    assert database.migrate_base_path_to_assets(conn.cursor()) is True

    assert not new_dir.exists(), (
        '目标位置出现了半棵树 —— 它会被判为可用底图，高程全错且零报错')
    assert not new_dir.with_name(new_dir.name + '.part').exists(), (
        '拷贝失败留下了 .part 暂存目录 —— 224 MB 无主残留，五类启动清扫都不认它')
    assert (old_dir / '8' / '0' / '0.terrain').is_file(), '搬不动时旧目录必须保留'
    # 迁移失败也不能阻断启动、不能每次重试（这条是原设计，保持不变）
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
    assert _stored_path(conn) == database._NEW_BASE_PATH


def test_custom_path_is_never_touched(migration_env):
    """用户自定义过的路径不动 —— 只改仍等于旧默认值的那种。"""
    database, conn, old_dir, new_dir = migration_env
    conn.execute("UPDATE config SET value = '/srv/terrain/base' "
                 "WHERE key = 'terrain_global_base_path'")

    assert database.migrate_base_path_to_assets(conn.cursor()) is False

    assert _stored_path(conn) == '/srv/terrain/base'
    assert not new_dir.exists()
    assert old_dir.is_dir()
