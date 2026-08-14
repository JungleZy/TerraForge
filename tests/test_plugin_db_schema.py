"""插件系统三张表的建表与迁移契约。"""

import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（它只有 autouse 的隔离夹具），所以按
    tests/test_fix_stranded_tiling_jobs.py:32-47 的既有写法在本文件里建一个；
    返回值是 Path，`sqlite3.connect(Path)` 直接可用。
    """
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core.database import init_database

    init_database()
    return path


def _columns(db, table):
    conn = sqlite3.connect(db)
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    finally:
        conn.close()


def test_plugin_tables_exist_with_expected_columns(db):
    for table in ('plugin_tasks', 'plugin_task_tiles', 'plugins'):
        assert _columns(db, table), f'{table} 未建'
    assert {'id', 'plugin_id', 'name', 'status', 'params_json', 'region_json',
            'output_path', 'total_items', 'downloaded_items', 'failed_items',
            'gap_tiles', 'gap_decision', 'total_running_seconds',
            'north', 'south', 'east', 'west', 'zoom_min', 'zoom_max',
            'created_at', 'started_at', 'completed_at', 'error_message'
            } <= _columns(db, 'plugin_tasks')
    assert {'task_id', 'zoom', 'x', 'y', 'status', 'retry_count',
            'error_message'} <= _columns(db, 'plugin_task_tiles')
    assert {'id', 'enabled', 'version', 'origin', 'config_json',
            'load_error', 'installed_at'} <= _columns(db, 'plugins')


def test_user_version_is_7(db):
    conn = sqlite3.connect(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] >= 7
    finally:
        conn.close()


def test_plugin_pipeline_registered_in_contracts(db):
    from src.contracts.artifact import PIPELINES, _PIPELINE_TABLES
    from src.contracts.artifact import Artifact, ArtifactKind
    assert 'plugin' in PIPELINES
    assert _PIPELINE_TABLES['plugin'] == 'plugin_tasks'
    a = Artifact(pipeline='plugin', task_id=1,
                 kind=ArtifactKind.MBTILES, path='/tmp/x.mbtiles')
    assert a.task_table == 'plugin_tasks'


def test_plugin_tasks_deletable(db):
    from src.services.task_deletion import _DELETABLE_TASK_TABLES
    assert 'plugin_tasks' in _DELETABLE_TASK_TABLES
