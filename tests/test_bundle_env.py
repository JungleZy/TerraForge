"""core.bundle 打包环境测试 —— 缺失 gdal-data/proj-data 时不得静默跳过。

打包产物若缺 gdal-data/proj-data,必须在启动时就大声报错,
而不是让 exe 跑起来后在 GDAL 调用处莫名失败(原 I20b hook 行为的 Nuitka 版)。
"""
import os
import sys

import pytest

from core import bundle

_ENV_VARS = ('GDAL_DATA', 'PROJ_LIB', 'PROJ_DATA')


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delattr(bundle, '__compiled__', raising=False)
    frozen_sentinel = getattr(sys, 'frozen', None)
    yield monkeypatch
    # setup_bundle_env() 直接写 os.environ,monkeypatch 追踪不到,必须手动清理,
    # 否则 GDAL_DATA 泄漏到后续测试(指向已删除的 tmp 目录,GDAL 重投影类测试全挂)。
    for var in _ENV_VARS:
        os.environ.pop(var, None)
    # bundle_dir() 在伪造编译环境下会补设 sys.frozen,同样必须还原——
    # 否则 Windows 上后续创建 ProcessPoolExecutor 的测试会走 frozen 命令行
    # (python.exe --multiprocessing-fork),worker 无法启动,进程池挂死。
    if frozen_sentinel is None:
        if hasattr(sys, 'frozen'):
            del sys.frozen
    else:
        sys.frozen = frozen_sentinel


def _fake_compiled(monkeypatch, exe_path):
    """模拟 Nuitka 打包环境:模块命名空间注入 __compiled__,sys.executable 指向 exe。"""
    monkeypatch.setattr(bundle, '__compiled__', object(), raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe_path))


def test_sets_env_when_data_dirs_present(clean_env, tmp_path):
    exe_dir = tmp_path / 'terraforge'
    (exe_dir / 'gdal-data').mkdir(parents=True)
    (exe_dir / 'proj-data').mkdir()
    _fake_compiled(clean_env, exe_dir / 'terraforge')

    bundle.setup_bundle_env()

    assert bundle.bundle_dir() == str(exe_dir)
    assert os.environ['GDAL_DATA'] == str(exe_dir / 'gdal-data')
    assert os.environ['PROJ_LIB'] == str(exe_dir / 'proj-data')
    assert os.environ['PROJ_DATA'] == str(exe_dir / 'proj-data')


def test_raises_when_data_dirs_missing(clean_env, tmp_path):
    """打包产物缺数据目录 → 启动即报错,不得静默。"""
    _fake_compiled(clean_env, tmp_path / 'terraforge')
    with pytest.raises(RuntimeError, match='gdal-data'):
        bundle.setup_bundle_env()


def test_noop_outside_bundle(clean_env):
    """非打包环境(无 __compiled__)→ 不做任何事,也不报错。"""
    assert bundle.bundle_dir() is None
    bundle.setup_bundle_env()
    for var in _ENV_VARS:
        assert var not in os.environ


def test_windows_path_and_dll_directory_setup(clean_env, tmp_path, monkeypatch):
    """Windows 打包环境 → dist 根目录加入 PATH 并注册 AddDllDirectory。

    Nuitka 用 LOAD_WITH_ALTERED_SEARCH_PATH 加载 .pyd,dist 根目录的 GDAL DLL
    只能通过 PATH 搜到(真实 Windows 上探针验证过)。
    """
    exe_dir = tmp_path / 'terraforge'
    (exe_dir / 'gdal-data').mkdir(parents=True)
    (exe_dir / 'proj-data').mkdir()
    _fake_compiled(clean_env, exe_dir / 'terraforge.exe')
    monkeypatch.setattr(bundle.sys, 'platform', 'win32')
    added = []
    monkeypatch.setattr(bundle.os, 'add_dll_directory', added.append, raising=False)
    monkeypatch.setenv('PATH', '/usr/bin')

    bundle.setup_bundle_env()

    assert os.environ['PATH'].startswith(str(exe_dir) + os.pathsep)
    assert added == [str(exe_dir)]


def test_sets_sys_frozen_when_compiled():
    """编译环境下调用 bundle_dir() 必须补设 sys.frozen。

    multiprocessing.spawn.get_command_line 靠 sys.frozen 识别冻结环境;
    且必须在调用时设置——Python 3.9 的 Nuitka 在模块 import 时
    __compiled__ 尚未进入 globals(),import 时设置会漏判。
    """
    sentinel = getattr(sys, 'frozen', None)
    sentinel_exe = sys.executable
    try:
        with open(bundle.__file__, encoding='utf-8') as f:
            source = f.read()
        glb = {'__name__': 'bundle_frozen_test', '__compiled__': object()}
        exec(compile(source, bundle.__file__, 'exec'), glb)
        glb['bundle_dir']()
        assert sys.frozen is True
    finally:
        sys.executable = sentinel_exe
        if sentinel is None:
            del sys.frozen
        else:
            sys.frozen = sentinel
