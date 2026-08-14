"""元数据钩子：task_completed 事件 → sidecar JSON；异常不炸分发。

覆盖两件容易写错的事：sidecar 名是**追加**后缀（目录型产物没有后缀，替换式
的 with_suffix 会拼出错名字），以及钩子自己那层旁路守卫 —— 宿主
`dispatch_event` 的 try 是兜底，不是本插件可以少一层的理由。
"""

import json
import logging
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.contracts.artifact import Artifact, ArtifactKind  # noqa: E402
# src.core.database 只在函数体内 import：模块级 from-import 它会踩
# tests/test_conftest_isolation_contract.py 的双实例棘轮（别处裸 pop 它）。
from src.plugins import registry                           # noqa: E402
from src.plugins.builtin.artifact_meta import ArtifactMetaHook  # noqa: E402
from src.plugins.protocols import TaskEvent                # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张真库：DATABASE_PATH 指到 tmp_path 后 init_database() 建全。

    conftest.py 没有 `db` fixture（只有 autouse 的隔离夹具），按
    tests/test_plugin_mvt.py:41-60 的既有写法在本文件里建一个。BASE_DIR 一起
    指过来：`registry._plugins_root()` 由它派生，否则 load_all() 会连带扫
    开发机上真实的 plugins/ 目录。
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


@pytest.fixture
def loaded(db):
    """真注册表：load_all() 后启用 artifact_meta，收尾复位（注册表是进程全局）。"""
    registry.reset_for_tests()
    registry.load_all()
    registry.set_enabled('artifact_meta', True)
    yield registry
    registry.reset_for_tests()


def _record(art, **over):
    from src.core.database import utc_now_iso
    from src.services import artifact_store
    fields = dict(pipeline='plugin', task_id=7, kind=ArtifactKind.MBTILES,
                  path=str(art), fmt='pbf', has_gaps=True,
                  created_at=utc_now_iso())
    fields.update(over)
    artifact_store.record_artifact(Artifact(**fields))


def _seed_artifact(db, tmp_path):
    art = tmp_path / 'a.mbtiles'
    art.write_bytes(b'x')
    _record(art)
    return art


def _completed(task_id=7):
    return TaskEvent(kind='task_completed', pipeline='plugin',
                     task_id=task_id, plugin_id='mvt')


def test_sidecar_written_on_completed(db, tmp_path):
    art = _seed_artifact(db, tmp_path)
    ArtifactMetaHook().on_event(_completed())
    sidecar = art.with_suffix(art.suffix + '.tfmeta.json')
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding='utf-8'))
    assert data['has_gaps'] is True and data['kind'] == 'mbtiles'
    assert data['pipeline'] == 'plugin' and data['task_id'] == 7
    assert data['format'] == 'pbf' and data['generated_at']


def test_other_events_ignored(db, tmp_path):
    art = _seed_artifact(db, tmp_path)
    ArtifactMetaHook().on_event(TaskEvent(
        kind='task_failed', pipeline='plugin', task_id=7, plugin_id='mvt'))
    assert not art.with_suffix(art.suffix + '.tfmeta.json').exists()


def test_suffixless_artifact_keeps_its_whole_name(db, tmp_path):
    """目录型产物没有后缀：sidecar 是 `tiles.tfmeta.json`，名字一个字都不许丢。

    钉死「追加」而不是「替换」：`Path('城区 2024.06').with_suffix('.tfmeta.json')`
    会把 `.06` 当后缀吃掉，落出 `城区 2024.tfmeta.json`。
    """
    plain = tmp_path / 'tiles'
    plain.mkdir()
    dotted = tmp_path / '城区 2024.06'
    dotted.mkdir()
    _record(plain, kind=ArtifactKind.XYZ_DIR, fmt='png', tile_count=12,
            minzoom=3, maxzoom=5, has_gaps=False)
    _record(dotted, kind=ArtifactKind.TERRAIN_DIR, fmt='terrain')

    ArtifactMetaHook().on_event(_completed())

    assert (tmp_path / 'tiles.tfmeta.json').is_file()
    assert (tmp_path / '城区 2024.06.tfmeta.json').is_file()
    assert not (tmp_path / '城区 2024.tfmeta.json').exists()
    data = json.loads((tmp_path / 'tiles.tfmeta.json').read_text(encoding='utf-8'))
    assert data['kind'] == 'xyz_dir' and data['tile_count'] == 12
    assert data['minzoom'] == 3 and data['maxzoom'] == 5


def test_write_failure_only_logs(db, tmp_path, caplog):
    """写不出来只落 warning：钩子是旁路，绝不向上抛。

    构造法：sidecar 那个名字先被一个**目录**占住 —— write_text 必然
    IsADirectoryError（Windows 上 PermissionError，都是 OSError）。比 chmod
    可靠：root 跑测试时 0o444 照样写得进去。
    """
    art = _seed_artifact(db, tmp_path)
    blocker = tmp_path / 'a.mbtiles.tfmeta.json'
    blocker.mkdir()

    with caplog.at_level(logging.WARNING,
                        logger='src.plugins.builtin.artifact_meta'):
        ArtifactMetaHook().on_event(_completed())        # 不抛

    assert blocker.is_dir() and not any(blocker.iterdir())
    assert any('sidecar' in r.message for r in caplog.records)


def test_one_bad_artifact_does_not_stop_the_rest(db, tmp_path, caplog):
    """一件产物写失败，后面的照样写：钩子按件兜，不是整批放弃。"""
    bad = tmp_path / 'bad.mbtiles'
    bad.write_bytes(b'x')
    (tmp_path / 'bad.mbtiles.tfmeta.json').mkdir()
    good = tmp_path / 'good.mbtiles'
    good.write_bytes(b'x')
    _record(bad)
    _record(good)

    with caplog.at_level(logging.WARNING,
                        logger='src.plugins.builtin.artifact_meta'):
        ArtifactMetaHook().on_event(_completed())

    assert (tmp_path / 'good.mbtiles.tfmeta.json').is_file()


def test_hook_exception_does_not_break_dispatch(db, tmp_path, monkeypatch):
    calls = []

    class BadHook:
        def on_event(self, event):
            raise RuntimeError('boom')

    class GoodHook:
        def on_event(self, event):
            calls.append(event.task_id)

    monkeypatch.setattr(registry, 'iter_hooks',
                        lambda: iter([('bad', BadHook()), ('good', GoodHook())]))
    registry.dispatch_event(TaskEvent(
        kind='task_completed', pipeline='plugin', task_id=1, plugin_id='x'))
    assert calls == [1]


def test_throwing_hook_does_not_starve_the_meta_hook(db, tmp_path, monkeypatch):
    """排在抛异常的钩子后面，本插件仍然收到事件并落 sidecar。"""
    art = _seed_artifact(db, tmp_path)

    class BadHook:
        def on_event(self, event):
            raise RuntimeError('boom')

    monkeypatch.setattr(registry, 'iter_hooks', lambda: iter(
        [('bad', BadHook()), ('artifact_meta', ArtifactMetaHook())]))
    registry.dispatch_event(_completed())

    assert (tmp_path / 'a.mbtiles.tfmeta.json').is_file()


def test_plugin_loads_and_is_enumerable(loaded, tmp_path):
    """真实链路：load_all 无 load_error，启用后 iter_hooks 枚举得到本插件。"""
    rec = loaded.get_record('artifact_meta')
    assert rec is not None and rec.load_error == ''
    assert rec.origin == 'builtin' and 'hook' in rec.manifest.capabilities
    hooks = dict(loaded.iter_hooks())
    assert isinstance(hooks.get('artifact_meta'), ArtifactMetaHook)


def test_disabled_plugin_is_not_dispatched_to(loaded, db, tmp_path):
    """缺省关闭的语义：禁用后 dispatch_event 不再落 sidecar。"""
    art = _seed_artifact(db, tmp_path)
    loaded.set_enabled('artifact_meta', False)
    loaded.dispatch_event(_completed())
    assert not (tmp_path / 'a.mbtiles.tfmeta.json').exists()

    loaded.set_enabled('artifact_meta', True)
    loaded.dispatch_event(_completed())
    assert (tmp_path / 'a.mbtiles.tfmeta.json').is_file()


#: 一个最小的真管线插件：跑一趟、登记一件产物、成功收尾。用来走通「任务终态
#: → task_manager 发事件 → 本钩子落 sidecar」这条真实链路（宿主发事件的唯一
#: 位置是 `task_manager._run_task:398-401`）。
_FAKE_PIPELINE = '''
from src.contracts.artifact import ArtifactKind
from src.plugins.protocols import ParamSchema, PluginDefinition, PluginOutcome
class P:
    def params_schema(self): return ParamSchema(())
    def estimate(self, params, region): return None
    def run(self, ctx):
        out = ctx.output_dir / 'tiles.mbtiles'
        out.write_bytes(b'x')
        ctx.register_artifact(out, ArtifactKind.MBTILES, fmt='pbf')
        return PluginOutcome.COMPLETED
def register():
    return PluginDefinition(pipeline=P())
'''


def test_real_task_completion_writes_sidecar(db, tmp_path, monkeypatch):
    """端到端：插件任务跑完 → 宿主发 task_completed → 产物旁出现 sidecar。

    这是本插件在生产里唯一的触发路径，所以不 monkeypatch 任何一环：真注册表、
    真 PluginTaskManager、真产物登记。
    """
    from src.plugins.task_manager import PluginTaskManager

    monkeypatch.setattr(registry, '_plugins_root', lambda: tmp_path / 'plugins')
    d = tmp_path / 'plugins' / 'fake'
    d.mkdir(parents=True)
    (d / 'plugin.toml').write_text(
        'id="fake"\nname="fake"\nversion="0.1"\napi_version="1"\n'
        'capabilities=["pipeline"]\n', encoding='utf-8')
    (d / 'plugin.py').write_text(_FAKE_PIPELINE, encoding='utf-8')
    registry.reset_for_tests()
    registry.load_all()
    registry.set_enabled('fake', True)
    registry.set_enabled('artifact_meta', True)
    try:
        mgr = PluginTaskManager(socketio=None)
        out = tmp_path / 'e2e'
        tid = mgr.create_task('fake', {'name': 'e2e',
                                       'bbox': [40.0, 30.0, 117.0, 116.0],
                                       'output_path': str(out)})
        mgr.start_task(tid)
        # ctx.output_dir 是 `<output_path>/plugin_task_<tid>`（宿主给每个任务
        # 开的子目录），sidecar 落在产物旁边而不是 output_path 根下。
        sidecar = out / f'plugin_task_{tid}' / 'tiles.mbtiles.tfmeta.json'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not sidecar.exists():
            time.sleep(0.05)
        row = mgr.get_task(tid)
        assert row['status'] == 'completed', row['error_message']
        assert sidecar.is_file(), '任务成功了但 sidecar 没落地'
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        assert data['task_id'] == tid and data['kind'] == 'mbtiles'
        assert data['bytes_total'] == 1 and data['meta']['plugin_id'] == 'fake'
    finally:
        registry.reset_for_tests()
