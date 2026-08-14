"""A1..A4 与 O1/O3/O4：插件契约、资源配额、配置 schema、注册表运维。

最终全分支评审的 Critical / Important 各条。每条用例在修复之前都跑红过；
断言的是可观察后果（登记行落在哪条管线上、插件真的拿到多少配额、广播载荷里
有没有那个键、`plugins` 表里还剩哪些行），不是实现细节。
"""

import json
import shutil
import sqlite3
import time

import pytest

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


# ---------------------------------------------------------------- A1 归属

def test_out_of_tree_artifact_path_is_refused():
    """产物路径越出任务目录 → ValueError。两扇门共用这一道校验。

    `task_cleanup.purge_registered_artifacts` 对每条登记行做 `unlink()`，
    任务目录之外的普通文件无条件删——插件登记 `~/.ssh/id_rsa`，用户删任务时
    宿主替它删掉。
    """
    import tempfile
    from pathlib import Path

    from src.services.artifact_store import ensure_owned_artifact_path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'plugin_task_1'
        root.mkdir()
        inside = root / 'a.gpkg'
        inside.write_bytes(b'x')
        assert ensure_owned_artifact_path(inside, root) == inside.resolve()
        with pytest.raises(ValueError):
            ensure_owned_artifact_path(Path(tmp) / 'outside.gpkg', root)
        # 软链穿越：字面检查会放行，resolve 之后才拦得住。
        link = root / 'link.gpkg'
        try:
            link.symlink_to(Path(tmp) / 'outside.gpkg')
        except (OSError, NotImplementedError):
            pytest.skip('本平台不允许创建符号链接')
        with pytest.raises(ValueError):
            ensure_owned_artifact_path(link, root)


def test_export_forces_host_owned_pipeline_and_task_id(db, tmp_path):
    """导出器谎报 `pipeline`/`task_id` 时，登记行按宿主的取值落库。"""
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.services import artifact_store

    root = tmp_path / 'plugin_task_9'
    root.mkdir(parents=True)
    target = root / 'out.gpkg'
    target.write_bytes(b'gpkg')
    artifact_store.record_plugin_artifact(
        Artifact(pipeline='map', task_id=12345, kind=ArtifactKind.GEOTIFF,
                 path=str(target), fmt='gpkg'),
        pipeline='plugin', task_id=9, output_root=root)

    assert artifact_store.list_artifacts('map', 12345) == []
    rows = artifact_store.list_artifacts('plugin', 9)
    assert [a.path for a in rows] == [str(target.resolve())]


# ---------------------------------------------------------------- A2 配额

_NETWORK_PLUGIN = '''
from src.contracts.reservation import ResourceKind
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome


class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None

    def run(self, ctx):
        (ctx.output_dir / 'granted.txt').write_text(
            str(ctx.granted(ResourceKind.NETWORK)), encoding='utf-8')
        return PluginOutcome.COMPLETED


def register(): return PluginDefinition(pipeline=P())
'''


def test_host_grants_network_quota_to_a_network_plugin(db, tmp_path, monkeypatch):
    """**走宿主真实路径**：声明了 network 权限的插件必须真的拿到 NETWORK 配额。

    不手搓 `TaskContext(granted={NETWORK: n})` —— 那测的是一个宿主从不产生的
    输入。修复之前 `_run_task` 只请求 TASK_SLOT（+DISK_BYTES），
    `ctx.granted(NETWORK)` 在生产里恒为 0。
    """
    from src.services.config_manager import ConfigManager
    from src.services.resource_scheduler import reset_scheduler
    from src.plugins.task_manager import PluginTaskManager

    cfg = ConfigManager()
    cfg.set('concurrent_downloads', '6')
    cfg.set('max_network_connections', '16')
    reset_scheduler()

    _write_plugin(tmp_path, monkeypatch, 'netplug', _NETWORK_PLUGIN)
    mgr = PluginTaskManager(socketio=None, config_manager=cfg)
    tid = mgr.create_task('netplug', {'name': 'net',
                                      'bbox': [40.0, 30.0, 117.0, 116.0],
                                      'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('completed', 'failed'))
    assert row['status'] == 'completed', row['error_message']

    granted = int((tmp_path / 'out' / f'plugin_task_{tid}'
                   / 'granted.txt').read_text(encoding='utf-8'))
    assert granted > 0, '宿主从来没请求过 NETWORK'
    assert granted == 6, '配额要与 concurrent_downloads 对得上'
    reset_scheduler()
    registry.reset_for_tests()


def test_a_plugin_without_the_network_permission_gets_no_quota(
        db, tmp_path, monkeypatch):
    """没声明 network 的插件不预留连接——配额是稀缺的，不该白占。"""
    from src.services.config_manager import ConfigManager
    from src.services.resource_scheduler import reset_scheduler
    from src.plugins.task_manager import PluginTaskManager

    reset_scheduler()
    _write_plugin(tmp_path, monkeypatch, 'quiet', _NETWORK_PLUGIN,
                  perms='["filesystem"]')
    mgr = PluginTaskManager(socketio=None, config_manager=ConfigManager())
    tid = mgr.create_task('quiet', {'name': 'q',
                                    'bbox': [40.0, 30.0, 117.0, 116.0],
                                    'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    _wait_status(mgr, tid, ('completed', 'failed'))
    granted = int((tmp_path / 'out' / f'plugin_task_{tid}'
                   / 'granted.txt').read_text(encoding='utf-8'))
    assert granted == 0
    reset_scheduler()
    registry.reset_for_tests()


# ---------------------------------------------------------------- A3 广播

_FAILING_PLUGIN = '''
from src.plugins.protocols import ParamSchema, PluginDefinition


class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None
    def run(self, ctx):
        raise RuntimeError("上游 403: https://h/a?tk=%s")


def register(): return PluginDefinition(pipeline=P())
''' % TOKEN


def test_task_failed_event_carries_the_error_message(db, tmp_path, monkeypatch):
    """`plugin_task_failed` 必须带 `error_message`，且它已经脱敏。

    不带的话前端 `handleTaskFailed(..., data.error_message)` 拿到 undefined，
    常驻失败 toast 与任务行都写「未知错误」，而真原因就躺在库里。
    """
    from src.plugins.task_manager import PluginTaskManager

    events = []

    class _Recorder:
        def emit(self, event, payload=None):
            events.append((event, payload or {}))

    _write_plugin(tmp_path, monkeypatch, 'boom', _FAILING_PLUGIN)
    mgr = PluginTaskManager(socketio=_Recorder())
    tid = mgr.create_task('boom', {'name': 'b',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    row = _wait_status(mgr, tid, ('failed',))
    assert row['status'] == 'failed'

    failed = [p for e, p in events if e == 'plugin_task_failed']
    assert failed, events
    payload = failed[-1]
    assert 'error_message' in payload
    assert payload['error_message'], '空串等于界面上的「未知错误」'
    assert TOKEN not in payload['error_message']
    registry.reset_for_tests()


# ---------------------------------------------------------------- O1 配置 UI

def test_config_endpoint_gives_schema_and_never_echoes_the_credential(
        isolated_app, tmp_path, monkeypatch):
    """配置端点要够前端渲染一个表单，且凭据真值不出服务端。"""
    from src.services.config_manager import SECRET_UNCHANGED

    _write_plugin(tmp_path, monkeypatch, 'cred', '''
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   SourceDescriptor)


def register():
    return PluginDefinition(
        sources=(SourceDescriptor(source_id='a', name='A',
                                  url_template='https://h/{z}/{x}/{y}',
                                  max_zoom=18, credential_key='token'),),
        config_schema=ParamSchema((
            ParamSpec(key='token', type='credential', label='key',
                      required=True),
        )))
''', caps='["sources"]')
    client = isolated_app.app.test_client()

    saved = client.put('/api/plugins/cred/config', json={'token': TOKEN})
    assert saved.status_code == 200, saved.get_data(as_text=True)

    body = client.get('/api/plugins/cred/config').get_json()
    assert [s['key'] for s in body['schema']] == ['token']
    assert body['schema'][0]['type'] == 'credential'
    assert body['config']['token'] == SECRET_UNCHANGED
    assert TOKEN not in json.dumps(body)

    # 哨兵原样回传 = 不改。库里仍是真值。
    assert client.put('/api/plugins/cred/config',
                      json={'token': SECRET_UNCHANGED}).status_code == 200
    assert registry.get_config('cred') == {'token': TOKEN}

    # 拼错的键名要被指名道姓地拦下，而不是静默落库。
    bad = client.put('/api/plugins/cred/config', json={'tokn': 'x'})
    assert bad.status_code == 400
    assert 'tokn' in bad.get_json()['errors']
    assert registry.get_config('cred') == {'token': TOKEN}
    registry.reset_for_tests()


def test_tianditu_declares_its_token_in_the_config_schema():
    """天地图必须声明 schema——它是「没有 pipeline 就不校验」的原始受害者。"""
    from src.plugins.builtin import tianditu_source

    definition = tianditu_source.register()
    assert definition.pipeline is None
    assert definition.config_schema is not None
    spec = definition.config_schema.specs[0]
    assert spec.key == 'token' and spec.type == 'credential' and spec.required


# ---------------------------------------------------------------- O3/O4 运维

def test_startup_summary_is_a_warning_when_a_plugin_failed(
        db, tmp_path, monkeypatch, caplog):
    """加载失败的插件不算「就绪」，整条汇总降级成 warning。

    运维扫 INFO 会把「4 个插件（启用 0 个）」判成成功，而实际可用数是 0。
    """
    import logging

    monkeypatch.setattr(registry, '_plugins_root', lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / 'broken'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="broken"\nname="b"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["pipeline"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text('raise RuntimeError("nope")\n', encoding='utf-8')
    registry.reset_for_tests()
    with caplog.at_level(logging.INFO, logger='src.plugins.registry'):
        registry.load_all()

    summary = [r for r in caplog.records if '插件注册表就绪' in r.getMessage()]
    assert summary, caplog.text
    assert summary[-1].levelno == logging.WARNING
    assert '失败 1' in summary[-1].getMessage()
    # I3：每个插件开始加载时都有一行，卡死时看得出卡在谁身上。
    assert any('加载插件' in r.getMessage() for r in caplog.records)
    registry.reset_for_tests()


def test_stale_plugin_rows_are_pruned_so_a_new_plugin_cannot_inherit(
        db, tmp_path, monkeypatch):
    """本轮没出现过的插件行必须清掉。

    不清的话：用户删掉插件 A、再装一个恰好同 id 的插件 B，B 直接继承 A 的
    `enabled=1` 与 `config_json`（含 token）—— `_upsert_row` 对已存在的行只更新
    版本与错误，启停与配置「是用户的决定」不覆盖。
    """
    body = ('from src.plugins.protocols import ParamSchema, PluginDefinition\n'
            'class P:\n'
            '    def params_schema(self): return ParamSchema(())\n'
            '    def estimate(self, params, region): return None\n'
            '    def run(self, ctx): return None\n'
            'def register(): return PluginDefinition(pipeline=P())\n')
    d = _write_plugin(tmp_path, monkeypatch, 'twin', body)
    registry.set_config('twin', {'token': TOKEN})
    assert registry.get_record('twin').enabled is True

    conn = sqlite3.connect(db)
    try:
        # 顺带覆盖回落 id 的孤儿行：它永远不会被覆盖，面板上也看不见
        # （`GET /api/plugins` 只读内存 `_RECORDS`）。
        conn.execute("INSERT INTO plugins (id, enabled, version, origin,"
                     " load_error) VALUES ('tianditu_source', 0, '', "
                     "'builtin', 'ModuleNotFoundError')")
        conn.commit()
    finally:
        conn.close()

    # 用户把插件目录删了，然后装了一个同 id 的新插件。
    (d / 'plugin.py').unlink()
    (d / 'plugin.toml').unlink()
    shutil.rmtree(d)
    registry.reset_for_tests()
    registry.load_all()

    conn = sqlite3.connect(db)
    try:
        ids = {r[0] for r in conn.execute('SELECT id FROM plugins')}
    finally:
        conn.close()
    assert 'twin' not in ids, '前任的 enabled 与 token 还留着'
    assert 'tianditu_source' not in ids, '回落 id 的孤儿行没被清掉'

    _write_plugin(tmp_path, monkeypatch, 'twin', body, enable=False)
    assert registry.get_record('twin').enabled is False, '新插件继承了旧开关'
    assert registry.get_config('twin') == {}, '新插件继承了旧 token'
    registry.reset_for_tests()


def test_a_failed_directory_scan_does_not_wipe_the_table(
        db, tmp_path, monkeypatch):
    """扫不动插件目录时**不许**清行——那会把用户所有外部插件的配置一次清空。"""
    body = ('from src.plugins.protocols import ParamSchema, PluginDefinition\n'
            'class P:\n'
            '    def params_schema(self): return ParamSchema(())\n'
            '    def estimate(self, params, region): return None\n'
            '    def run(self, ctx): return None\n'
            'def register(): return PluginDefinition(pipeline=P())\n')
    _write_plugin(tmp_path, monkeypatch, 'keeper', body)
    registry.set_config('keeper', {'token': TOKEN})

    monkeypatch.setattr(registry, '_external_dirs', lambda: ([], False))
    registry.reset_for_tests()
    registry.load_all()

    conn = sqlite3.connect(db)
    try:
        ids = {r[0] for r in conn.execute('SELECT id FROM plugins')}
    finally:
        conn.close()
    assert 'keeper' in ids
    registry.reset_for_tests()


_GAP_PLUGIN = """
from src.contracts.outcome import TileOutcome
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome


class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None

    def run(self, ctx):
        ctx.record_tile_outcome(5, 2, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
        return PluginOutcome.COMPLETED_WITH_GAPS


def register(): return PluginDefinition(pipeline=P())
"""


def test_deleting_a_plugin_task_clears_its_tile_rows(db, tmp_path, monkeypatch):
    """删任务时缺块行跟着走，不等下次启动的孤儿扫描。"""
    from src.core.database import get_connection
    from src.plugins.task_manager import PluginTaskManager

    _write_plugin(tmp_path, monkeypatch, 'gaps', _GAP_PLUGIN)
    mgr = PluginTaskManager(socketio=None)
    tid = mgr.create_task('gaps', {'name': 'g',
                                   'bbox': [40.0, 30.0, 117.0, 116.0],
                                   'output_path': str(tmp_path / 'out')})
    mgr.start_task(tid)
    _wait_status(mgr, tid, ('completed_with_gaps', 'completed', 'failed'))

    conn = get_connection()
    try:
        before = conn.execute(
            'SELECT COUNT(*) AS n FROM plugin_task_tiles WHERE task_id = ?',
            (tid,)).fetchone()['n']
    finally:
        conn.close()
    assert before > 0, '前提：这条任务确实留下了缺块行'

    assert mgr.delete_task(tid).row_deleted is True
    conn = get_connection()
    try:
        after = conn.execute(
            'SELECT COUNT(*) AS n FROM plugin_task_tiles WHERE task_id = ?',
            (tid,)).fetchone()['n']
    finally:
        conn.close()
    assert after == 0
    registry.reset_for_tests()


def test_load_all_takes_no_socketio():
    """§5：插件拿不到 socketio。那个形参体内零引用，两边都删了。"""
    import inspect

    assert list(inspect.signature(registry.load_all).parameters) == []


_EVIL_EXPORTER = '''
from pathlib import Path
from src.contracts.artifact import Artifact, ArtifactKind
from src.plugins.protocols import PluginDefinition


class E:
    def format_id(self): return 'evil'
    def accepts(self, kind): return True

    def export(self, artifact, dest, ctx):
        # 写在宿主给的目标上（导出本身是成功的），但**登记**一条越界路径。
        Path(dest).write_bytes(b'x')
        victim = Path(dest).parent.parent / 'victim.txt'
        victim.write_text('important', encoding='utf-8')
        return Artifact(pipeline='map', task_id=999,
                        kind=ArtifactKind.MBTILES, path=str(victim),
                        fmt='evil')


def register(): return PluginDefinition(exporters=(E(),))
'''


def test_export_route_refuses_an_out_of_tree_registration(
        isolated_app, tmp_path, monkeypatch):
    """导出端点是归属校验的第二扇门，必须和 `register_artifact` 过同一道。

    修复之前它把 `exporter.export()` 返回的 Artifact 原样 `record_artifact`，
    `path`/`pipeline`/`task_id` 三个字段零校验 —— 登记一条任务目录之外的路径，
    用户删任务时 `purge_registered_artifacts` 会替它 unlink 掉。
    """
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.core.database import get_connection
    from src.services import artifact_store

    _write_plugin(tmp_path, monkeypatch, 'evil', _EVIL_EXPORTER,
                  caps='["exporter"]', perms='["filesystem"]')
    client = isolated_app.app.test_client()

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (id, name, north, south, east, west,"
            " zoom_min, zoom_max, style, output_format, output_path, status)"
            " VALUES (77, 't', 1, 0, 1, 0, 5, 5, 's', 'png', ?, 'completed')",
            (str(tmp_path / 'out'),))
        conn.commit()
    finally:
        conn.close()
    src_dir = tmp_path / 'out' / 'task_77'
    src_dir.mkdir(parents=True)
    source = src_dir / 'dem.tif'
    source.write_bytes(b'tif')
    artifact_store.record_artifact(Artifact(
        pipeline='map', task_id=77, kind=ArtifactKind.GEOTIFF,
        path=str(source), fmt='tif'))

    resp = client.post('/api/export/map/77', json={'format': 'evil'})
    assert resp.status_code == 500, resp.get_data(as_text=True)
    assert artifact_store.list_artifacts('plugin', 999) == []
    assert str(tmp_path / 'out' / 'victim.txt') not in {
        a.path for a in artifact_store.list_artifacts('map', 77)}
    registry.reset_for_tests()
