"""插件任务管理器：创建/运行/完成/删除/孤儿恢复。"""

import json
import os
import sqlite3
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.plugins import registry
from src.plugins.task_manager import PluginTaskManager

FAKE_PLUGIN = '''
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome
from src.contracts.outcome import TileOutcome
class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None
    def run(self, ctx):
        ctx.record_tile_outcome(3, 1, 1, TileOutcome.SUCCESS)
        ctx.progress(1, 1, 'done')
        return PluginOutcome.COMPLETED
def register():
    return PluginDefinition(pipeline=P())
'''

#: §13-3 的缺块决策：第一趟留洞 → pending_decision；宿主把
#: `_gap_accepted` 写回 params 之后的那一趟才收尾产出。
FAKE_GAP_PLUGIN = '''
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome
from src.contracts.outcome import TileOutcome
class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None
    def run(self, ctx):
        if ctx.params.get('_gap_accepted'):
            ctx.log('用户接受缺块，收尾导出')
            return PluginOutcome.COMPLETED_WITH_GAPS
        ctx.record_tile_outcome(5, 2, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
        ctx.record_tile_outcome(5, 2, 3, TileOutcome.NO_DATA)
        return PluginOutcome.PENDING_DECISION
def register():
    return PluginDefinition(pipeline=P())
'''


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（它只有 autouse 的隔离夹具），所以按
    tests/test_plugin_task_context.py:21-41 的既有写法在本文件里建一个。
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


def _setup(db, tmp_path, monkeypatch, source=FAKE_PLUGIN):
    monkeypatch.setattr(registry, '_plugins_root',
                        lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / 'fake'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="fake"\nname="fake"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["pipeline"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(source, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    registry.set_enabled('fake', True)
    return PluginTaskManager(socketio=None)


def _wait_status(mgr, tid, want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = mgr.get_task(tid)
        if row and row['status'] in want:
            return row
        time.sleep(0.05)
    return mgr.get_task(tid)


def test_create_and_run_to_completed(db, tmp_path, monkeypatch):
    mgr = _setup(db, tmp_path, monkeypatch)
    tid = mgr.create_task('fake', {'name': 't1',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('completed', 'failed'))
    assert row['status'] == 'completed', row.get('error_message')


def test_orphan_running_recovered_to_failed(db, tmp_path, monkeypatch):
    _setup(db, tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO plugin_tasks (plugin_id, name, status)"
                 " VALUES ('fake', 'orphan', 'running')")
    conn.commit()
    conn.close()
    mgr2 = PluginTaskManager(socketio=None)
    row = [r for r in mgr2.list_tasks() if r['name'] == 'orphan'][0]
    assert row['status'] == 'failed' and row['error_message']


def test_delete_task_row(db, tmp_path, monkeypatch):
    mgr = _setup(db, tmp_path, monkeypatch)
    tid = mgr.create_task('fake', {'name': 't2',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'del')})
    outcome = mgr.delete_task(tid, delete_files=False)
    assert outcome.row_deleted is True
    assert mgr.get_task(tid) is None


def test_unknown_plugin_rejected(db, tmp_path, monkeypatch):
    mgr = _setup(db, tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        mgr.create_task('nope', {})


def test_pending_decision_then_accept_gaps_finishes(db, tmp_path, monkeypatch):
    """§13-3 全程：留洞 → pending_decision → accept_gaps 回写 → 收尾导出。"""
    mgr = _setup(db, tmp_path, monkeypatch, source=FAKE_GAP_PLUGIN)
    tid = mgr.create_task('fake', {'name': 't3',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'gap')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('pending_decision', 'failed', 'completed'))
    assert row['status'] == 'pending_decision', row.get('error_message')
    # 两个洞都进稀疏表；failed_items 只算「没被上游解释过」的那一个
    # （no_data 是上游明确说过没有，不是失败）。
    assert row['gap_tiles'] == 2
    assert row['failed_items'] == 1
    assert mgr.gap_summary(tid)['by_outcome'] == {'retryable_failure': 1,
                                                  'no_data': 1}

    mgr.accept_gaps(tid)
    row = _wait_status(mgr, tid, ('completed_with_gaps', 'failed'))
    assert row['status'] == 'completed_with_gaps', row.get('error_message')
    assert row['gap_decision'] == 'accept'
    assert json.loads(row['params_json'])['_gap_accepted'] is True
