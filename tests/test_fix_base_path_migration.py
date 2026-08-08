"""底图缓存位置迁移：downloads/terrain/base_z8 → assets/terrain/base_z8。

只改 DEFAULT_CONFIGS 是不够的 —— 那走的是 INSERT OR IGNORE，只对新建的库生效。
存量库那行还是旧路径，于是：解压去新位置 → 旧位置空 → /terrain/base 和可用性
判定都按旧路径 → 底图判为不可用 → 走 parentUrl 兜底 → 那个 URL 指向服务旧空
路径的 /terrain/base → 404 → Cesium 塞假 heightmap 图层污染共享 builder →
任务自己的瓦片高程全错且零报错。正是 v0.2.8 刚修过的那条链。

⚠️ **每条用例都必须把 Config.BASE_DIR / DOWNLOADS_DIR 打到 tmp_path**，哪怕
用例本身不关心搬迁。迁移会在旧位置有底图时搬过去，而开发机上
`downloads/terrain/base_z8` 是真实存在的 224 MB —— 不 patch 就会把它搬进仓库的
`assets/terrain/`，既毁了本机缓存，又违反「测试不得往仓库 assets/ 写」的约束
（CI 里测试跑在 Nuitka 打包之前，解出来的东西会被打进三个平台的产物）。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _sandbox_config(tmp_path, monkeypatch):
    """把迁移会碰的两个目录锁进 tmp_path（理由见模块 docstring）。"""
    from src.core import config as config_mod

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")


def _legacy_db(path, base_path_value, user_version=2):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('terrain_global_base_path', ?)",
                 (base_path_value,))
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    return conn


def _read(conn):
    row = conn.execute(
        "SELECT value FROM config WHERE key = 'terrain_global_base_path'").fetchone()
    return row["value"]


def test_migration_rewrites_the_stale_default(tmp_path):
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "a.db", "./downloads/terrain/base_z8")
    assert migrate_base_path_to_assets(conn.cursor()) is True
    conn.commit()

    assert _read(conn) == "./assets/terrain/base_z8"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migration_leaves_a_user_customised_path_alone(tmp_path):
    """用户自己改过的路径不动 —— 迁移只认旧默认值。"""
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "b.db", "/mnt/big-disk/my-base")
    migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert _read(conn) == "/mnt/big-disk/my-base"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migration_is_reentrant(tmp_path):
    """user_version 已是 3 → 不再改写。"""
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "c.db", "./downloads/terrain/base_z8", user_version=3)
    assert migrate_base_path_to_assets(conn.cursor()) is False
    conn.commit()

    assert _read(conn) == "./downloads/terrain/base_z8"


def test_migration_moves_an_existing_unpacked_base(tmp_path):
    """旧位置有完整底图 → rename 过去，不重解压 224 MB。"""
    from src.core.database import migrate_base_path_to_assets

    old = tmp_path / "downloads" / "terrain" / "base_z8"
    old.mkdir(parents=True)
    (old / "layer.json").write_text("{}", encoding="utf-8")

    conn = _legacy_db(tmp_path / "d.db", "./downloads/terrain/base_z8")
    migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert (tmp_path / "assets" / "terrain" / "base_z8" / "layer.json").is_file()
    assert not old.exists()


def test_migration_survives_a_failing_move(tmp_path, monkeypatch):
    """两条搬迁路径全失败（跨盘 + 拷贝也不成）不能阻断启动。

    旧目录留着、新位置留待重新解压、user_version 照样推到 3（不重试）。
    注入点是 os.replace 与 shutil.copytree —— 2026-08-08 起实现不再用
    shutil.move 直连最终位置（那会在跨盘中断时留一棵只有 layer.json 的半树，
    见 tests/test_fix_review_20260808.py 的 T2 一节）。
    """
    from src.core import database as db_mod

    old = tmp_path / "downloads" / "terrain" / "base_z8"
    old.mkdir(parents=True)
    (old / "layer.json").write_text("{}", encoding="utf-8")

    def boom(*a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(db_mod.os, "replace", boom)
    monkeypatch.setattr(db_mod.shutil, "copytree", boom)

    conn = _legacy_db(tmp_path / "e.db", "./downloads/terrain/base_z8")
    db_mod.migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert old.is_dir(), "搬不动时旧目录必须保留"
    assert not (tmp_path / "assets" / "terrain" / "base_z8").exists(), (
        "搬不动却在目标位置留下了东西 —— 半成品底图会被判为可用")
    assert _read(conn) == "./assets/terrain/base_z8"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
