"""启动身份真值表 —— src/core/runtime_mode.py。

这张表以前散在 app.py 的模块级布尔表达式里,只能靠子进程级用例
(test_app_mp_worker_guard.py / test_fix_infra_e.py 的 I7)间接验证:起一个真进程、
跑完整初始化、再回头查数据库有没有被改写。抽成纯函数后可以直接钉住每一格。

两条最贵的「否」是踩出来的事故(误改运行中任务的状态),它们必须由用例守住:
- dev reloader 的 watcher 父进程不能跑 create_app;
- spawn 平台 re-import 主模块(__mp_main__ / Nuitka 的 __parents_main__)不能跑。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.core.runtime_mode import (SERVER_HOST, SERVER_PORT, StartupRole,
                                   debug_enabled, detect_startup_role)


@pytest.fixture(autouse=True)
def _clear_werkzeug_env(monkeypatch):
    """默认按「非 reloader 子进程」判定,个别用例再自行设回。"""
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)


def _role(module_name, debug, werkzeug_child=False, monkeypatch=None):
    if werkzeug_child:
        monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    return detect_startup_role(module_name, debug=debug)


def test_single_process_prints_everything_and_initializes():
    """DEBUG=0 直接跑:横幅、启动提示、初始化全归它。"""
    role = detect_startup_role('__main__', debug=False)
    assert (role.print_banner, role.show_startup_output, role.should_create_app) == (
        True, True, True)
    assert role.reloader_parent is False


def test_reloader_parent_prints_banner_but_never_initializes(monkeypatch):
    """watcher 父进程只打横幅(它先启动,控制台不能空着),绝不能跑 create_app。

    父进程跑 create_app 会触发孤儿恢复写库 —— 同库另有存活实例时会把对方正在
    running 的任务改判成 paused。
    """
    role = _role('__main__', debug=True, monkeypatch=monkeypatch)
    assert role.reloader_parent is True
    assert role.print_banner is True
    assert role.show_startup_output is False
    assert role.should_create_app is False


def test_reloader_child_serves_and_prints_ready(monkeypatch):
    """真正起服务的子进程:不重复打横幅,负责打「组件加载完成」,做完整初始化。"""
    role = _role('__main__', debug=True, werkzeug_child=True, monkeypatch=monkeypatch)
    assert role.reloader_parent is False
    assert role.print_banner is False
    assert role.show_startup_output is True
    assert role.should_create_app is True


@pytest.mark.parametrize('debug', [True, False])
def test_wsgi_or_test_import_initializes_silently(debug):
    """gunicorn app:app 与测试 import app:要 app 实例,但不该往控制台打启动输出。"""
    role = detect_startup_role('app', debug=debug)
    assert role.should_create_app is True
    assert role.print_banner is False
    assert role.show_startup_output is False


@pytest.mark.parametrize('module_name', ['__mp_main__', '__parents_main__'])
@pytest.mark.parametrize('debug', [True, False])
def test_mp_rerun_never_initializes(module_name, debug):
    """spawn worker re-import 主模块时,parent_process() 还没设好,只能靠模块名拦。

    拦不住的后果:每个渲染 worker 都重跑 init_database、抢 SQLite 锁,并让孤儿恢复
    把正在 running 的任务误标成 paused(v0.1.1 Windows 打包 exe 地形切片的死因)。
    """
    role = detect_startup_role(module_name, debug=debug)
    assert role.should_create_app is False
    assert role.print_banner is False
    assert role.show_startup_output is False


def test_debug_flag_is_carried_on_the_role():
    """role.debug 就是 use_reloader / 横幅上那个运行模式的唯一来源。"""
    assert detect_startup_role('__main__', debug=True).debug is True
    assert detect_startup_role('__main__', debug=False).debug is False


def test_role_is_immutable():
    """身份一旦判定就不该被后续代码改写(否则「谁做初始化」会变成竞态)。"""
    role = detect_startup_role('app', debug=False)
    with pytest.raises(Exception):
        role.should_create_app = True
    assert isinstance(role, StartupRole)


def test_debug_env_var_overrides_default(monkeypatch):
    """DEBUG 环境变量始终优先;'0'/'false'/'False' 关,其余值开。"""
    for raw, expected in (('0', False), ('false', False), ('False', False),
                          ('1', True), ('yes', True)):
        monkeypatch.setenv('DEBUG', raw)
        assert debug_enabled() is expected, raw


def test_debug_defaults_off_when_frozen(monkeypatch):
    """打包 exe 默认关热重载:reloader 会重新执行 exe 本身,对最终用户毫无意义。"""
    monkeypatch.delenv('DEBUG', raising=False)
    from src.core import runtime_mode

    monkeypatch.setattr(runtime_mode, 'is_frozen', lambda: True)
    assert runtime_mode.debug_enabled() is False
    monkeypatch.setattr(runtime_mode, 'is_frozen', lambda: False)
    assert runtime_mode.debug_enabled() is True


def test_banner_and_server_share_one_address():
    """横幅印的地址和 socketio.run() 监听的必须是同一份常量,否则用户点开的是死链。"""
    from src.core import server_runner

    assert (SERVER_HOST, SERVER_PORT) == ('0.0.0.0', 5000)
    defaults = server_runner.run_server.__kwdefaults__
    assert defaults['host'] == SERVER_HOST
    assert defaults['port'] == SERVER_PORT
