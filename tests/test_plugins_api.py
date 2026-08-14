"""插件 API：列表/启停/配置/任务生命周期/资产服务/history_all 合流。

`isolated_app` 返回的是 **app 模块**（`isolated_app.app` 才是 Flask 实例），
与 tests/test_cache_management.py 等既有用例同一写法。

注册表是进程全局的：`isolated_app` 构造 app 时已经 `load_all()` 过一次（那时
`_plugins_root()` 还指向仓库根的 plugins/）。所以 helper 在 monkeypatch 完
`_plugins_root` 之后重新 `reset_for_tests(); load_all()` —— 路由每次请求都实时
查注册表，不需要重造 app。
"""

import json
import time

import pytest

#: 带 schema（含一个 credential 参数）、缺块决策与导出器的假插件。
#: 刻意与 `fake` 分居两个插件根：`fake` 的用例断言 `sources == []` /
#: `supported_formats == []` / schema 为空，装在同一个根里会把它们全部弄红。
RICH_PLUGIN = '''
from pathlib import Path
from src.contracts.artifact import Artifact, ArtifactKind
from src.contracts.outcome import TileOutcome
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   PluginOutcome)


class P:
    def params_schema(self):
        return ParamSchema((
            ParamSpec(key='token', type='credential', label='密钥',
                      required=False),
            ParamSpec(key='depth', type='int', label='深度', default=3,
                      required=False, min=1, max=9),
        ))

    def estimate(self, params, region):
        return None

    def run(self, ctx):
        if ctx.params.get('_gap_accepted'):
            return PluginOutcome.COMPLETED_WITH_GAPS
        ctx.record_tile_outcome(5, 2, 2, TileOutcome.RETRYABLE_FAILURE, 'boom')
        ctx.record_tile_outcome(5, 2, 3, TileOutcome.NO_DATA)
        return PluginOutcome.PENDING_DECISION


class E:
    def format_id(self):
        return 'gpkg'

    def accepts(self, kind):
        return True

    def export(self, artifact, dest, ctx):
        Path(dest).write_bytes(b'gpkg')
        return Artifact(pipeline='plugin', task_id=artifact.task_id,
                        kind=ArtifactKind.MBTILES, path=str(dest), fmt='gpkg')


def register():
    return PluginDefinition(pipeline=P(), exporters=(E(),))
'''


def _install_fake(tmp_path, monkeypatch):
    from src.plugins import registry

    monkeypatch.setattr(registry, '_plugins_root',
                        lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / 'fake'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="fake"\nname="假插件"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["pipeline"]\n[ui]\nassets=["panel.js"]\n',
        encoding='utf-8')
    (d / 'plugin.py').write_text(
        'from src.plugins.protocols import (ParamSchema, PluginDefinition,\n'
        '                                   PluginOutcome)\n'
        'class P:\n'
        '    def params_schema(self): return ParamSchema(())\n'
        '    def estimate(self, params, region): return None\n'
        '    def run(self, ctx): return PluginOutcome.COMPLETED\n'
        'def register(): return PluginDefinition(pipeline=P())\n',
        encoding='utf-8')
    (d / 'panel.js').write_text('window.x = 1;\n', encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    # 用例之间不留脏注册表：下一个用例的 app 构造会自己 load_all，但那之前
    # 任何直接查注册表的用例都不该看到这个 tmp_path 里的假插件。
    monkeypatch.setattr(registry, '_plugins_root',
                        lambda: tmp_path / 'plugins')
    return registry


@pytest.fixture
def fake_client(isolated_app, tmp_path, monkeypatch):
    """假插件 + test_client。返回 (client, registry)。"""
    registry = _install_fake(tmp_path, monkeypatch)
    yield isolated_app.app.test_client(), registry
    registry.reset_for_tests()


def _install_rich(tmp_path, monkeypatch):
    """RICH_PLUGIN 单独占一个插件根，返回 registry。"""
    from src.plugins import registry

    root = tmp_path / 'plugins_rich'
    monkeypatch.setattr(registry, '_plugins_root', lambda: root)
    d = root / 'rich'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="rich"\nname="富插件"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["pipeline","exporter"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(RICH_PLUGIN, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    return registry


@pytest.fixture
def rich_client(isolated_app, tmp_path, monkeypatch):
    registry = _install_rich(tmp_path, monkeypatch)
    client = isolated_app.app.test_client()
    client.post('/api/plugins/rich/enable')
    yield client, registry
    registry.reset_for_tests()


def _create(client, tmp_path, **extra):
    body = {'name': 'r', 'bbox': [40.0, 30.0, 117.0, 116.0],
            'output_path': str(tmp_path / 'out'), **extra}
    resp = client.post('/api/plugins/rich/tasks', json=body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _wait_status(client, tid, wanted, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f'/api/plugins/tasks/{tid}').get_json()['task']
        if task['status'] in wanted:
            return task
        time.sleep(0.02)
    raise AssertionError(f'任务 {tid} 没有到达 {wanted}，停在 {task["status"]}')


def test_plugins_list_and_enable(fake_client):
    client, registry = fake_client
    data = client.get('/api/plugins').get_json()
    assert data['success']
    fake = [p for p in data['plugins'] if p['id'] == 'fake'][0]
    assert fake['enabled'] is False and fake['name'] == '假插件'
    assert fake['has_ui'] is True
    assert fake['capabilities'] == ['pipeline']
    assert client.post('/api/plugins/fake/enable').get_json()['success']
    assert registry.get_record('fake').enabled is True
    assert client.post('/api/plugins/fake/disable').get_json()['success']
    assert registry.get_record('fake').enabled is False
    assert client.post('/api/plugins/nope/enable').status_code == 404


def test_config_roundtrip(fake_client):
    client, _registry = fake_client
    resp = client.put('/api/plugins/fake/config', json={'token': 'abc'})
    assert resp.get_json()['success']
    got = client.get('/api/plugins/fake/config').get_json()['config']
    assert got['token'] == 'abc'
    assert client.get('/api/plugins/nope/config').status_code == 404
    assert client.put('/api/plugins/fake/config',
                      data='not-json',
                      content_type='text/plain').status_code == 400


def test_create_and_get_task(fake_client, tmp_path):
    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    resp = client.post('/api/plugins/fake/tasks', json={
        'name': 't', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')})
    assert resp.status_code == 200, resp.get_json()
    tid = resp.get_json()['task_id']
    task = client.get(f'/api/plugins/tasks/{tid}').get_json()['task']
    assert task['plugin_id'] == 'fake' and task['status'] == 'pending'
    listed = client.get('/api/plugins/tasks').get_json()['tasks']
    assert [t['id'] for t in listed] == [tid]
    assert client.delete(f'/api/plugins/tasks/{tid}').get_json()['success']
    assert client.get(f'/api/plugins/tasks/{tid}').status_code == 404


def test_create_task_rejects_unknown_plugin_and_bad_bbox(fake_client, tmp_path):
    client, _registry = fake_client
    # 未启用的插件没有可用管线 → 404（KeyError 一档）。
    assert client.post('/api/plugins/fake/tasks',
                       json={'bbox': [1.0, 0.0, 1.0, 0.0]}).status_code == 404
    client.post('/api/plugins/fake/enable')
    assert client.post('/api/plugins/fake/tasks',
                       json={'bbox': 'nope'}).status_code == 400


def test_delete_without_files_registers_the_retained_dir(fake_client, tmp_path):
    """delete_files=false（默认）时产物目录必须留下一条 DB 引用。

    行一走，`<output_path>/plugin_task_<id>/` 就没有任何任务行指向它；启动清扫
    只认 pending_deletions 与任务表，扫不到它。不登记就是盘上一个谁都不知道的
    半成品目录（与四条核心管线的删除路由同一处理）。
    """
    from src.services import task_cleanup

    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    tid = client.post('/api/plugins/fake/tasks', json={
        'name': 'keep', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')}).get_json()['task_id']

    from src.plugins.task_manager import get_plugin_task_manager
    task_dir = get_plugin_task_manager().task_output_dir(tid)
    task_dir.mkdir(parents=True)
    (task_dir / 'leftover.bin').write_bytes(b'x')

    payload = client.delete(f'/api/plugins/tasks/{tid}').get_json()
    assert payload['success']
    assert payload['files_retained_path'] == str(task_dir)
    assert task_dir.exists(), 'delete_files=false 一个字节都不许动'
    assert task_dir in task_cleanup._retained_output_roots()


def test_delete_with_files_purges_registered_artifacts(fake_client, tmp_path):
    """delete_files=1 时任务目录与登记产物一起走，产物行不许留下来。

    只销任务行不销产物行的后果：产物列表与 /mbtiles 指向一批已经不存在的路径。
    与四条核心管线的删除路由同一处动作（`purge_registered_artifacts`）。
    """
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.plugins.task_manager import get_plugin_task_manager
    from src.services import artifact_store

    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    tid = client.post('/api/plugins/fake/tasks', json={
        'name': 'purge', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')}).get_json()['task_id']

    task_dir = get_plugin_task_manager().task_output_dir(tid)
    task_dir.mkdir(parents=True)
    inner = task_dir / 'result.mbtiles'
    inner.write_bytes(b'x')
    artifact_store.record_artifact(Artifact(
        pipeline='plugin', task_id=tid, kind=ArtifactKind.MBTILES,
        path=str(inner), fmt='pbf'))
    assert artifact_store.list_artifacts('plugin', tid)

    payload = client.delete(
        f'/api/plugins/tasks/{tid}?delete_files=1').get_json()
    assert payload['success']
    assert 'files_retained_path' not in payload
    assert not task_dir.exists()
    assert artifact_store.list_artifacts('plugin', tid) == []


def test_assets_served_and_traversal_blocked(fake_client):
    client, _registry = fake_client
    resp = client.get('/api/plugins/fake/assets/panel.js')
    assert resp.status_code == 200 and b'window.x' in resp.data
    resp = client.get('/api/plugins/fake/assets/..%2F..%2Fplugin.toml')
    assert resp.status_code in (400, 403, 404)
    # 目录里真实存在、但没在 manifest 声明的文件也不许出去。
    assert client.get('/api/plugins/fake/assets/plugin.toml').status_code == 403


def test_history_all_includes_plugin_rows(fake_client, tmp_path):
    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    client.post('/api/plugins/fake/tasks', json={
        'name': 'hist', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')})
    resp = client.get('/api/history_all')
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    types = {r['task_type'] for r in body['tasks']}
    assert 'plugin' in types
    row = [r for r in body['tasks'] if r['task_type'] == 'plugin'][0]
    # 列序对错了 SQL 不会报错，只会把值串到隔壁列上——所以逐个盯值。
    assert row['name'] == 'hist' and row['status'] == 'pending'
    assert row['style'] == 'fake' and row['output_format'] is None
    assert row['downloaded'] == 0 and row['total'] == 0
    assert body['pagination']['total_count'] == 1


def test_history_all_status_filter_covers_the_plugin_segment(fake_client,
                                                            tmp_path):
    """插件段吃同一个 status 筛选，且分页总数与列表长度一致。

    第五段不接 where 的后果：`?status=completed` 把全部插件任务原样带出来，而
    total_count 是按筛选算的 —— 列表和计数当场对不上。
    """
    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    client.post('/api/plugins/fake/tasks', json={
        'name': 'pend', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')})

    body = client.get('/api/history_all?status=completed').get_json()
    assert body['tasks'] == [] and body['pagination']['total_count'] == 0

    # pending 属于 active 组（ACTIVE_STATE_VALUES），组展开那条路也要走到。
    body = client.get('/api/history_all?status=active').get_json()
    assert len(body['tasks']) == 1 == body['pagination']['total_count']

    body = client.get('/api/history_all?status=pending').get_json()
    assert len(body['tasks']) == 1 == body['pagination']['total_count']
    assert body['tasks'][0]['task_type'] == 'plugin'


def test_history_stats_counts_plugin_tasks(fake_client, tmp_path):
    """统计卡片必须数上插件任务：它们就在 history_all 的时间流里，
    统计漏掉就是「总数 0、列表 1 条」两处互相矛盾。"""
    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    client.post('/api/plugins/fake/tasks', json={
        'name': 'stat', 'bbox': [40.0, 30.0, 117.0, 116.0],
        'output_path': str(tmp_path / 'out')})
    stats = client.get('/api/history_stats').get_json()['stats']
    assert stats['total_tasks'] == 1
    assert stats['completed'] == 0 and stats['failed'] == 0


def test_params_schema_endpoint(fake_client):
    client, _registry = fake_client
    assert client.get('/api/plugins/fake/schema').get_json()['params'] == []
    client.post('/api/plugins/fake/enable')
    assert client.get('/api/plugins/fake/schema').get_json()['params'] == []


def test_sources_and_export_formats(fake_client):
    client, _registry = fake_client
    client.post('/api/plugins/fake/enable')
    assert client.get('/api/plugins/sources').get_json()['sources'] == []
    resp = client.post('/api/plugins/export/1', json={'format': 'nope'})
    assert resp.status_code == 400
    assert resp.get_json()['supported_formats'] == []


def test_task_json_never_carries_credential_params(rich_client, tmp_path):
    """凭据参数不许出到浏览器，两个读端点都不许。

    `create_task` 把整份 params 原样落进 `params_json`（task_manager.py:141-150
    解释了为什么它不能在落库时剥——剥了插件重跑就读不到值），约束因此落在序列化
    这一层。`ParamSpec.type == 'credential'` 的键一律剔除，其余照给：前端本来就
    照 `/<pid>/schema` 渲染这些字段，回显是它的正当需求。

    T12（天地图源插件，`credential_key='token'`）落地那一刻这条就是真泄漏。
    """
    client, _registry = rich_client
    tid = _create(client, tmp_path, token='SECRET-TOKEN', depth=5)['task_id']

    task = client.get(f'/api/plugins/tasks/{tid}').get_json()['task']
    assert 'params_json' not in task
    assert task['params'] == {'depth': 5}, task['params']
    assert task['plugin_id'] == 'rich' and task['status'] == 'pending'
    # 整份响应体里都不许出现那个值（换个字段名藏着也不行）。
    raw = client.get(f'/api/plugins/tasks/{tid}').get_data(as_text=True)
    assert 'SECRET-TOKEN' not in raw

    listed = client.get('/api/plugins/tasks').get_data(as_text=True)
    assert 'SECRET-TOKEN' not in listed and 'params_json' not in listed
    assert json.loads(listed)['tasks'][0]['params'] == {'depth': 5}

    # 前端确实要的那些字段照给（不能因为剥字段把详情页剥空）。
    for key in ('id', 'name', 'north', 'south', 'east', 'west', 'output_path',
                'downloaded_items', 'total_items', 'gap_tiles', 'gap_decision',
                'created_at', 'error_message'):
        assert key in task, key


def test_task_params_withheld_when_schema_unavailable(rich_client, tmp_path):
    """schema 拿不到就整份 params 不给：不知道哪个键是凭据时不许猜着给。"""
    client, registry = rich_client
    tid = _create(client, tmp_path, token='SECRET-TOKEN', depth=5)['task_id']
    registry.reset_for_tests()          # 插件卸载了，任务行还在

    task = client.get(f'/api/plugins/tasks/{tid}').get_json()['task']
    assert 'params' not in task
    assert task['plugin_id'] == 'rich' and task['status'] == 'pending'


def test_auto_start_failure_keeps_the_created_task(rich_client, tmp_path,
                                                   monkeypatch):
    """auto_start 起不起来 ≠ 创建失败。

    真实路径：另一个标签页在 create 与 start 之间点了 /disable，`start_task`
    抛 ValueError。原来没包 try → 500，而任务行**已经建好了**：用户以为什么都
    没发生，库里多一条 pending 任务。这里用「第二次 get_pipeline 返回 None」
    把那个交错稳定复现（HTTP 层没法插进两步之间）。
    """
    from src.plugins import registry as registry_mod

    client, _registry = rich_client
    real = registry_mod.get_pipeline
    calls = {'n': 0}

    def flaky(plugin_id):
        calls['n'] += 1
        return real(plugin_id) if calls['n'] == 1 else None

    monkeypatch.setattr(registry_mod, 'get_pipeline', flaky)

    body = _create(client, tmp_path, auto_start=True)
    assert body['started'] is False
    assert '未启用' in body['start_error'] or '加载失败' in body['start_error']

    monkeypatch.setattr(registry_mod, 'get_pipeline', real)
    task = client.get(f"/api/plugins/tasks/{body['task_id']}").get_json()['task']
    assert task['status'] == 'pending', '行必须还在，且没被翻成 running'


def test_auto_start_success_reports_started(rich_client, tmp_path):
    client, _registry = rich_client
    body = _create(client, tmp_path, auto_start=True)
    assert body['started'] is True
    _wait_status(client, body['task_id'], ('pending_decision',))


def test_start_gaps_accept_gaps_over_http(rich_client, tmp_path):
    """§13-3 的决策流全程走 HTTP：start → gaps → accept-gaps。

    管理器层已有用例，这里盯的是路由：三条端点原来一条 HTTP 层覆盖都没有。
    """
    client, _registry = rich_client
    tid = _create(client, tmp_path)['task_id']

    assert client.post(f'/api/plugins/tasks/{tid}/start').get_json()['success']
    _wait_status(client, tid, ('pending_decision', 'failed'))
    gaps = client.get(f'/api/plugins/tasks/{tid}/gaps').get_json()
    assert gaps['success'] and gaps['task_id'] == tid
    # 总数从任务行来、分类计数从稀疏表来，两处必须对得上。
    assert gaps['gap_tiles'] == 2
    # no_data 是上游明确说过没有，不算失败——两者必须分得开。
    assert gaps['by_outcome'] == {'retryable_failure': 1, 'no_data': 1}
    assert gaps['gap_decision'] == ''

    assert client.post(
        f'/api/plugins/tasks/{tid}/accept-gaps').get_json()['success']
    task = _wait_status(client, tid, ('completed_with_gaps', 'failed'))
    assert task['status'] == 'completed_with_gaps', task['error_message']
    assert task['gap_decision'] == 'accept'

    # 不存在的任务：三条统一 404（与 GET /tasks/<tid> 同一档，不混进 400）。
    assert client.get('/api/plugins/tasks/999999/gaps').status_code == 404
    assert client.post('/api/plugins/tasks/999999/start').status_code == 404
    assert client.post(
        '/api/plugins/tasks/999999/accept-gaps').status_code == 404


def test_export_keeps_dotted_artifact_name(rich_client, tmp_path):
    """导出目标不许把产物名里的点当扩展名切掉。

    `Path('城区 2024.06').with_suffix('.gpkg')` → `城区 2024.gpkg`：月份没了，
    同一年两个月份的导出互相覆盖。产物名里的点未必是扩展名。
    """
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.services import artifact_store

    client, _registry = rich_client
    tid = _create(client, tmp_path)['task_id']
    src_dir = tmp_path / 'out' / f'plugin_task_{tid}'
    src_dir.mkdir(parents=True)
    source = src_dir / '城区 2024.06'
    source.mkdir()
    artifact_store.record_artifact(Artifact(
        pipeline='plugin', task_id=tid, kind=ArtifactKind.XYZ_DIR,
        path=str(source)))

    body = client.post(f'/api/plugins/export/{tid}',
                       json={'format': 'gpkg'}).get_json()
    assert body['success'], body
    assert body['path'] == str(src_dir / '城区 2024.06.gpkg')
    assert (src_dir / '城区 2024.06.gpkg').is_file()
    assert source.is_dir(), '源产物一个字节都不该被动'
    # 导出的成品也进登记，否则删任务时它是孤儿文件。
    assert str(src_dir / '城区 2024.06.gpkg') in {
        a.path for a in artifact_store.list_artifacts('plugin', tid)}


def test_export_does_not_overwrite_a_same_format_source(rich_client, tmp_path):
    """源已经是目标格式时，目标不许等于源——那是让导出器写在自己的输入上。"""
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.services import artifact_store

    client, _registry = rich_client
    tid = _create(client, tmp_path)['task_id']
    src_dir = tmp_path / 'out' / f'plugin_task_{tid}'
    src_dir.mkdir(parents=True)
    source = src_dir / 'result.gpkg'
    source.write_bytes(b'original')
    artifact_store.record_artifact(Artifact(
        pipeline='plugin', task_id=tid, kind=ArtifactKind.MBTILES,
        path=str(source), fmt='gpkg'))

    body = client.post(f'/api/plugins/export/{tid}',
                       json={'format': 'gpkg'}).get_json()
    assert body['success'], body
    assert body['path'] != str(source)
    assert source.read_bytes() == b'original'


def test_export_replaces_a_real_extension(rich_client, tmp_path):
    """后缀确实是生产者声明的那个格式时才替换：`a.tif` + tif → `a.gpkg`。"""
    from src.contracts.artifact import Artifact, ArtifactKind
    from src.services import artifact_store

    client, _registry = rich_client
    tid = _create(client, tmp_path)['task_id']
    src_dir = tmp_path / 'out' / f'plugin_task_{tid}'
    src_dir.mkdir(parents=True)
    source = src_dir / 'dem.tif'
    source.write_bytes(b'tif')
    artifact_store.record_artifact(Artifact(
        pipeline='plugin', task_id=tid, kind=ArtifactKind.GEOTIFF,
        path=str(source), fmt='tif'))

    body = client.post(f'/api/plugins/export/{tid}',
                       json={'format': 'gpkg'}).get_json()
    assert body['path'] == str(src_dir / 'dem.gpkg'), body
