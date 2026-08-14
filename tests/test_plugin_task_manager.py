"""插件任务管理器：创建/运行/完成/删除/孤儿恢复。"""

import json
import os
import sqlite3
import sys
import threading
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

#: 第二趟（`_gap_accepted` 那趟）停在原地等测试放行，用来把「上一轮还在收尾」
#: 与「下一轮已经登记」两件事重叠起来。
FAKE_GAP_PARKED_PLUGIN = '''
import time
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome
from src.contracts.outcome import TileOutcome
class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None
    def run(self, ctx):
        if ctx.params.get('_gap_accepted'):
            (ctx.output_dir / 'second_run_started').write_text('1')
            go = ctx.output_dir / 'go'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if ctx.stop_requested() or go.exists():
                    break
                time.sleep(0.01)
            return PluginOutcome.COMPLETED_WITH_GAPS
        ctx.record_tile_outcome(5, 2, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
        return PluginOutcome.PENDING_DECISION
def register():
    return PluginDefinition(pipeline=P())
'''


class _GateSocketIO:
    """把「终态已落库、工作线程还没走完 finally」那个窗口钉死。

    完成事件的 `emit` 挂在门上：放行之前那个线程一直停在 `_run_task` 返回之后的
    收尾段里（真实系统里这段隔着 `_emit` 与第三方钩子分发，可以任意慢）。
    不用 sleep 赌时序。
    """

    def __init__(self):
        self.hit = threading.Event()
        self.gate = threading.Event()

    def emit(self, event, payload=None):
        if event == 'plugin_task_completed' and not self.gate.is_set():
            self.hit.set()
            self.gate.wait(10)


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


def _setup(db, tmp_path, monkeypatch, source=FAKE_PLUGIN, socketio=None):
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
    return PluginTaskManager(socketio=socketio)


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


def test_previous_run_exit_keeps_next_run_stop_flag(db, tmp_path, monkeypatch):
    """上一轮退出**不许**摘掉下一轮的停止标志。

    真实路径：UI 收到 `plugin_task_completed`（发在收尾段**里面**）之后用户立刻
    点「接受缺块」，于是「上一轮还在走 finally」与「下一轮已经登记」重叠。
    无判据地 `stop_flags.pop` 会偷走新一轮的 Event：新线程读到 None 判成
    「删除请求先到」直接 return，行永久停在 running 谁也起不动；读在偷之后则
    这一轮再也停不下来（`delete_task` 置不了标志 → 后台 join 600 秒超时 →
    产物清理整支跳过）。
    """
    gate = _GateSocketIO()
    mgr = _setup(db, tmp_path, monkeypatch, source=FAKE_GAP_PARKED_PLUGIN,
                 socketio=gate)
    tid = mgr.create_task('fake', {'name': 't5',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'race')})
    task_dir = tmp_path / 'race' / f'plugin_task_{tid}'
    mgr.start_task(tid)
    # 门被撞上 = 终态已落库、第一轮的线程正停在收尾段里
    assert gate.hit.wait(10), '第一轮没走到完成事件'
    assert mgr.get_task(tid)['status'] == 'pending_decision'
    first = next(t for t in threading.enumerate()
                 if t.name == f'plugin-task-{tid}')

    mgr.accept_gaps(tid)                       # 第二轮登记进来
    flag = mgr.stop_flags[tid]
    deadline = time.monotonic() + 10
    while not (task_dir / 'second_run_started').exists():
        assert time.monotonic() < deadline, '第二轮没跑起来'
        time.sleep(0.01)

    gate.gate.set()                            # 放行第一轮，让它走完 finally
    first.join(10)
    assert not first.is_alive()

    # 回归点：第二轮的 Event 还在，而且真的还能叫停第二轮
    assert mgr.stop_flags.get(tid) is flag
    (task_dir / 'go').write_text('1')          # 正常收尾，别留悬着的线程
    row = _wait_status(mgr, tid, ('completed_with_gaps', 'failed'))
    assert row['status'] == 'completed_with_gaps', row.get('error_message')


def test_output_dir_failure_releases_reservation(db, tmp_path, monkeypatch):
    """准入之后、`run()` 之前抛异常也必须归还凭据。

    `output_path` 是用户填的：路径上有一段是**文件**时 `TaskContext.__init__` 的
    mkdir 抛 NotADirectoryError。凭据漏一张的后果不是「这次失败」而是这条任务
    永久锁死——owner `('plugin', id, 'run')` 是确定性的，留在调度器 `_owners`
    里之后每次 start 都撞 `owner ... already holds a reservation`。
    """
    mgr = _setup(db, tmp_path, monkeypatch)
    blocker = tmp_path / 'blocker'
    blocker.write_text('我是文件，不是目录', encoding='utf-8')
    tid = mgr.create_task('fake', {'name': 't6',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(blocker / 'sub')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('failed',))
    assert row['status'] == 'failed'
    assert 'NotADirectoryError' in row['error_message']

    from src.services.resource_scheduler import get_scheduler
    scheduler = get_scheduler(None)
    assert ('plugin', tid, 'run') not in scheduler._owners

    # 凭据真的回收了 → 同一条任务能再起一次，而不是撞 owner 冲突
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('failed',))
    assert 'already holds' not in (row['error_message'] or '')
    assert 'NotADirectoryError' in row['error_message']
