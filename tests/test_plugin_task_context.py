"""TaskContext：outcome 攒批落库、缺块计数、产物登记、URL 闸、配额读取。"""

import os
import sqlite3
import sys
import threading

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.contracts.artifact import ArtifactKind
from src.contracts.outcome import TileOutcome
from src.contracts.region import RegionSpec
from src.contracts.reservation import ResourceKind
from src.plugins.task_context import TaskContext


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（它只有 autouse 的隔离夹具），所以按
    tests/test_plugin_db_schema.py:14-34 的既有写法在本文件里建一个；
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


def _ctx(db, tmp_path, granted=None):
    return TaskContext(
        task_id=1, plugin_id='demo',
        region=RegionSpec.from_bbox(40.0, 30.0, 117.0, 116.0),
        params={'k': 'v'}, output_dir=tmp_path / 'out', snapshot=None,
        stop_flag=threading.Event(), tlog=None, emit_progress=None,
        granted=granted or {}, config_manager=None)


def _seed_task(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO plugin_tasks (id, plugin_id, name, status)"
                 " VALUES (1, 'demo', 't', 'running')")
    conn.commit()
    conn.close()


def test_outcome_batch_flush_and_gap_count(db, tmp_path):
    _seed_task(db)
    ctx = _ctx(db, tmp_path)
    ctx.record_tile_outcome(3, 1, 1, TileOutcome.SUCCESS)
    ctx.record_tile_outcome(3, 1, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
    ctx.record_tile_outcome(3, 1, 3, TileOutcome.NO_DATA)
    ctx.flush_outcomes()
    conn = sqlite3.connect(db)
    rows = conn.execute(
        'SELECT status, y FROM plugin_task_tiles WHERE task_id = 1'
        ' ORDER BY y').fetchall()
    # success 不落行（稀疏表：有行即有洞）
    assert [(r[0], r[1]) for r in rows] == [
        (TileOutcome.RETRYABLE_FAILURE.value, 2), (TileOutcome.NO_DATA.value, 3)]
    gap = conn.execute(
        'SELECT gap_tiles FROM plugin_tasks WHERE id = 1').fetchone()[0]
    conn.close()
    assert gap == 2
    ctx.close()


def test_outcome_success_after_failure_removes_row(db, tmp_path):
    _seed_task(db)
    ctx = _ctx(db, tmp_path)
    ctx.record_tile_outcome(3, 5, 5, TileOutcome.RETRYABLE_FAILURE, 'x')
    ctx.flush_outcomes()
    ctx.record_tile_outcome(3, 5, 5, TileOutcome.SUCCESS)
    ctx.flush_outcomes()
    conn = sqlite3.connect(db)
    n = conn.execute(
        'SELECT COUNT(*) FROM plugin_task_tiles WHERE task_id = 1').fetchone()[0]
    conn.close()
    assert n == 0
    ctx.close()


def test_register_artifact_uses_plugin_pipeline(db, tmp_path):
    _seed_task(db)
    ctx = _ctx(db, tmp_path)
    art = tmp_path / 'out' / 'a.mbtiles'
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(b'x')
    ctx.register_artifact(art, ArtifactKind.MBTILES, has_gaps=True, fmt='pbf',
                          meta={'source': 'test'})
    conn = sqlite3.connect(db)
    row = conn.execute(
        'SELECT pipeline, kind, has_gaps, meta FROM artifacts'
        ' WHERE task_id = 1').fetchone()
    conn.close()
    assert row[0] == 'plugin' and row[1] == 'mbtiles' and row[2] == 1
    assert 'test' in row[3]
    ctx.close()


def test_check_url_blocks_link_local(db, tmp_path):
    from src.services.url_guard import UrlNotAllowed
    ctx = _ctx(db, tmp_path)
    with pytest.raises(UrlNotAllowed):
        ctx.check_url('http://169.254.169.254/latest/meta-data')
    ctx.close()


def test_granted_reads_reservation(db, tmp_path):
    ctx = _ctx(db, tmp_path, granted={ResourceKind.NETWORK: 8})
    assert ctx.granted(ResourceKind.NETWORK) == 8
    assert ctx.granted(ResourceKind.CPU_WORKER) == 0
    ctx.close()


def test_buffer_auto_flushes_at_batch_size(db, tmp_path):
    """攒批必须有上界：插件跑十万块时缓冲不能无限长。

    简报 5 个用例都显式 flush，量不到「到阈值自己落库」这条——而这正是
    `_FLUSH_BATCH_SIZE` 存在的唯一理由，丢了就是内存无界。
    """
    from src.plugins.task_context import _FLUSH_BATCH_SIZE

    _seed_task(db)
    ctx = _ctx(db, tmp_path)
    for i in range(_FLUSH_BATCH_SIZE + 50):
        ctx.record_tile_outcome(5, i, 0, TileOutcome.RETRYABLE_FAILURE, 'e')
    conn = sqlite3.connect(db)
    landed = conn.execute(
        'SELECT COUNT(*) FROM plugin_task_tiles WHERE task_id = 1').fetchone()[0]
    conn.close()
    assert landed == _FLUSH_BATCH_SIZE, '到阈值没有自动落库'
    ctx.flush_outcomes()
    conn = sqlite3.connect(db)
    total, gap = conn.execute(
        'SELECT (SELECT COUNT(*) FROM plugin_task_tiles WHERE task_id = 1),'
        ' (SELECT gap_tiles FROM plugin_tasks WHERE id = 1)').fetchone()
    conn.close()
    assert total == _FLUSH_BATCH_SIZE + 50 and gap == total
    ctx.close()
