"""TaskContext：outcome 攒批落库、缺块计数、产物登记、URL 闸、配额读取。"""

import os
import sqlite3
import sys
import threading
from pathlib import Path

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


# --------------------------------------------------------------- 修复轮 1 覆盖


@pytest.fixture
def tlog(db):
    """一个真的 TaskLogger（带文件 handler）。用完必须 close：logger 对象是
    进程级缓存的，留一个指向已删 tmp_path 的 handler 会污染后续测试。"""
    from src.services.task_logging import open_task_log

    handle = open_task_log('plugin', 1)
    assert handle.enabled, '前置条件：这个 tlog 必须真的挂上了文件 handler'
    try:
        yield handle
    finally:
        handle.close()


def _ctx_with(db, tmp_path, **over):
    kwargs = dict(
        task_id=1, plugin_id='demo',
        region=RegionSpec.from_bbox(40.0, 30.0, 117.0, 116.0),
        params={'k': 'v'}, output_dir=tmp_path / 'out', snapshot=None,
        stop_flag=threading.Event(), tlog=None, emit_progress=None,
        granted={}, config_manager=None)
    kwargs.update(over)
    return TaskContext(**kwargs)


def test_log_level_cannot_hijack_task_logger(db, tmp_path, tlog):
    """插件传的 level 字符串不许穿透到 TaskLogger 的属性面。

    裸 `getattr(self._tlog, level)` 时 `level='__init__'` 会**静默**重建
    TaskLogger：enabled 变 False、`_logger` 换成 `task.%s.<消息>`，而文件
    handler 还挂在旧 logger 上且 `_handler` 已置 None——close() 从此直接
    return，句柄永久泄漏。`level='close'` 则抛 TypeError，捅穿 TaskLogger
    「所有方法都不抛」的不变量，把异常丢回插件自己的 run()。
    """
    ctx = _ctx_with(db, tmp_path, tlog=tlog)
    name_before, path_before = tlog._logger.name, tlog.path
    for bad in ('__init__', 'close', '_log', 'bogus', ''):
        ctx.log('m', bad)          # 不抛
    assert tlog.enabled, '日志句柄被 level 参数摘掉了'
    assert tlog._logger.name == name_before, 'logger 被 level 参数换掉了'
    assert tlog.path == path_before


def test_log_level_error_is_not_downgraded(db, tmp_path, caplog):
    """允许清单内的 level 必须按原级别落——降级到 info 会让插件的错误行淹没。"""
    import logging as _logging

    ctx = _ctx_with(db, tmp_path)      # tlog=None → 回落到模块 logger
    with caplog.at_level(_logging.DEBUG, logger='src.plugins.task_context'):
        ctx.log('boom', 'error')
    levels = [r.levelno for r in caplog.records if 'boom' in r.getMessage()]
    assert levels == [_logging.ERROR]


def test_flush_failure_retains_batch_for_retry(db, tmp_path, monkeypatch):
    """落库失败要把批次退回缓冲，交给后续 flush 重试。

    宿主对同一问题的既有做法就是退回（`task_manager._restore_progress_batch`）。
    丢批的代价不是少几条日志：`gap_tiles` 从行数重算，丢一批 → 计数偏小 →
    任务被判 COMPLETED 而非带缺块，用户拿到带洞却标「干净」的成果（§13-3）。
    """
    _seed_task(db)
    ctx = _ctx_with(db, tmp_path)
    ctx.record_tile_outcome(3, 1, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
    ctx.record_tile_outcome(3, 1, 3, TileOutcome.SUCCESS)

    # 只让第一次落库失败，不用 monkeypatch.undo()：monkeypatch 是函数级夹具，
    # 与 `db` 夹具共用同一个实例，undo() 会把 Config.DATABASE_PATH 一起还原成
    # 真库——那样这个测试就往项目自己的数据库里写行了（第一版真写了一行）。
    from src.core.config import Config
    from src.plugins import task_context as mod

    real, attempts = mod.get_connection, []

    def _flaky(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError('database is locked')
        # 绊线：这一批要是落到真库上，这里当场红，而不是静默污染用户的数据库。
        assert Path(Config.DATABASE_PATH) == Path(db), 'DATABASE_PATH 漂到真库了'
        return real(*a, **k)

    monkeypatch.setattr(mod, 'get_connection', _flaky)
    ctx.flush_outcomes()              # 失败，不抛
    ctx.flush_outcomes()              # 重试：这一批必须还在
    assert len(attempts) == 2
    conn = sqlite3.connect(db)
    rows = conn.execute(
        'SELECT zoom, x, y, status FROM plugin_task_tiles'
        ' WHERE task_id = 1').fetchall()
    gap = conn.execute(
        'SELECT gap_tiles FROM plugin_tasks WHERE id = 1').fetchone()[0]
    conn.close()
    assert rows == [(3, 1, 2, TileOutcome.RETRYABLE_FAILURE.value)]
    assert gap == 1


def test_flush_failure_discards_beyond_retention_cap(db, tmp_path, monkeypatch):
    """回填必须有界：持续故障下缓冲不能把内存吃光。"""
    from src.plugins.task_context import _MAX_RETAINED

    _seed_task(db)
    ctx = _ctx_with(db, tmp_path)
    monkeypatch.setattr(
        'src.plugins.task_context.get_connection',
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('x')))
    for i in range(_MAX_RETAINED + 500):
        ctx.record_tile_outcome(3, i, 0, TileOutcome.NO_DATA)
    assert (len(ctx._outcome_buffer) + len(ctx._success_buffer)
            <= _MAX_RETAINED), '回填无上界'


def test_register_artifact_rejects_path_outside_output_dir(db, tmp_path):
    """插件不许把 output_dir 之外的路径登记成自己的产物。

    `task_cleanup.purge_registered_artifacts` 对登记行做 `unlink()`，四条删除
    路由都在调它——不校验就等于把「删任意文件」的原语交给第三方代码。
    """
    _seed_task(db)
    ctx = _ctx_with(db, tmp_path)
    outsider = tmp_path / 'secret.key'
    outsider.write_bytes(b'k')
    with pytest.raises(ValueError):
        ctx.register_artifact(outsider, ArtifactKind.MBTILES)
    # 符号链接不能绕过：字面路径在 output_dir 内，指向的却在外面
    link = ctx.output_dir / 'a.mbtiles'
    link.symlink_to(outsider)
    with pytest.raises(ValueError):
        ctx.register_artifact(link, ArtifactKind.MBTILES)
    conn = sqlite3.connect(db)
    n = conn.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]
    conn.close()
    assert n == 0, '被拒的产物不该留下登记行'


def test_register_artifact_fills_size_columns(db, tmp_path):
    """规模列要填：四条宿主管线都填，插件不填就在缓存管理页永远显示 0 B。"""
    _seed_task(db)
    ctx = _ctx_with(db, tmp_path)
    art = ctx.output_dir / 'a.mbtiles'
    art.write_bytes(b'x' * 1234)
    ctx.register_artifact(art, ArtifactKind.MBTILES, fmt='pbf')
    conn = sqlite3.connect(db)
    row = conn.execute(
        'SELECT bytes_total, path FROM artifacts WHERE task_id = 1').fetchone()
    conn.close()
    assert row[0] == 1234 and row[1] == str(art.resolve())


def test_params_is_a_read_only_snapshot(db, tmp_path):
    """插件改不动 params，也看不见宿主之后对原 dict 的改动。

    直接存引用时 `ctx.params is params`，插件改一个键就渗回宿主——T7 拿同一份
    params 落库、缺块决策重跑时还要再用一次。
    """
    original = {'k': 'v'}
    ctx = _ctx_with(db, tmp_path, params=original)
    with pytest.raises(TypeError):
        ctx.params['k'] = 'hacked'
    original['k'] = 'host-changed'
    assert ctx.params['k'] == 'v'


def test_granted_is_a_copy(db, tmp_path):
    """配额表拷一份：那个 dict 是调度器账本，插件改一个数字就是永久配额泄漏。"""
    ledger = {ResourceKind.NETWORK: 8}
    ctx = _ctx_with(db, tmp_path, granted=ledger)
    ctx._granted[ResourceKind.NETWORK] = 999
    assert ledger[ResourceKind.NETWORK] == 8


def test_close_does_not_close_task_logger(db, tmp_path, tlog):
    """`close()` 只 flush，不许摘 tlog 的文件 handler。

    `_outcome_buffer` 的生命周期是插件这一次运行，`tlog` 的是整个任务——管理器
    要在 `run()` 返回之后才写终态那几行（`tlog.event('terminal', ...)`）。谁在
    这里关 tlog，任务日志就缺最后一行（§4.5）。而 `close()` 挂在插件的公开面
    上，插件自己调一次就够。
    """
    _seed_task(db)
    ctx = _ctx_with(db, tmp_path, tlog=tlog)
    ctx.record_tile_outcome(3, 1, 2, TileOutcome.NO_DATA)
    ctx.close()

    assert tlog.enabled, 'ctx.close() 摘掉了宿主的日志句柄'
    tlog.event('terminal', status='completed')   # 宿主收尾：必须还写得进文件
    for handler in tlog._logger.handlers:
        handler.flush()
    assert 'EVENT terminal' in tlog.path.read_text(encoding='utf-8')

    conn = sqlite3.connect(db)
    n = conn.execute(
        'SELECT COUNT(*) FROM plugin_task_tiles WHERE task_id = 1').fetchone()[0]
    conn.close()
    assert n == 1, 'close() 必须先把缓冲落库'


def test_record_after_close_is_discarded_without_raising(db, tmp_path, caplog):
    """收尾后再记的 outcome 丢弃 + 记 warning，**不抛**（记账路径没有抛的权力）。"""
    import logging as _logging

    _seed_task(db)
    ctx = _ctx_with(db, tmp_path)
    ctx.close()
    with caplog.at_level(_logging.WARNING, logger='src.plugins.task_context'):
        ctx.record_tile_outcome(3, 9, 9, TileOutcome.NO_DATA)
    ctx.flush_outcomes()
    conn = sqlite3.connect(db)
    n = conn.execute(
        'SELECT COUNT(*) FROM plugin_task_tiles WHERE task_id = 1').fetchone()[0]
    conn.close()
    assert n == 0
    assert any('已收尾' in r.getMessage() for r in caplog.records)


def test_proxy_url_resolved_once(db, tmp_path, monkeypatch):
    """代理在一次运行内不会变，而 resolve_from_config 会阻塞到探测超时——
    插件在下载循环里每块瓦片问一次是很自然的写法，不能每次都吃那段等待。"""
    calls = []

    class _CM:
        def get(self, key, default=None):
            calls.append(key)
            return 'http://p:8080' if key == 'proxy_url' else default

    monkeypatch.setattr('src.services.proxy_autodetect.auto_detect_enabled',
                        lambda cm: False)
    ctx = _ctx_with(db, tmp_path, config_manager=_CM())
    assert ctx.proxy_url() == 'http://p:8080'
    assert ctx.proxy_url() == 'http://p:8080'
    assert calls.count('proxy_url') == 1
