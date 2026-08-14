"""插件 API：列表/启停/配置/任务生命周期/资产服务/history_all 合流。

`isolated_app` 返回的是 **app 模块**（`isolated_app.app` 才是 Flask 实例），
与 tests/test_cache_management.py 等既有用例同一写法。

注册表是进程全局的：`isolated_app` 构造 app 时已经 `load_all()` 过一次（那时
`_plugins_root()` 还指向仓库根的 plugins/）。所以 helper 在 monkeypatch 完
`_plugins_root` 之后重新 `reset_for_tests(); load_all()` —— 路由每次请求都实时
查注册表，不需要重造 app。
"""

import pytest


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
