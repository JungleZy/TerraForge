"""I20b 修复行为测试 —— build.spec 的 gdal-data/proj-data 收集失败必须让构建报错。

build.spec 由 PyInstaller 以 exec 方式执行（Analysis/PYZ/EXE/COLLECT 是
PyInstaller 注入的全局名）。本测试用 stub 注入这些名字与
PyInstaller.utils.hooks，再 monkeypatch os.popen / os.path 模拟
「gdal-config 不存在 / proj.db 找不到」的环境，断言 spec 直接 raise，
而不是静默产出一个能构建但跑起来就坏的 exe。
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, 'build.spec')


class _FakePopen:
    def __init__(self, output):
        self._output = output

    def read(self):
        return self._output


class _Stub:
    """吸收 Analysis/PYZ/EXE/COLLECT 调用；Analysis 记录 datas 供断言。

    spec 在 Analysis 之后还会访问 a.pure / a.scripts 等属性并继续构造
    PYZ/EXE/COLLECT，用 __getattr__ 返回通用 stub 让执行顺利走到底。
    """

    captured = {}

    def __init__(self, *args, **kwargs):
        _Stub.captured[type(self).__name__] = {'args': args, 'kwargs': kwargs}

    def __getattr__(self, name):
        return _Stub()


def _make_stub(name):
    return type(name, (_Stub,), {})


@pytest.fixture
def spec_env(monkeypatch):
    """准备 exec build.spec 的环境，返回 (globals_dict, 设置函数)。"""
    hooks = types.ModuleType('PyInstaller.utils.hooks')
    hooks.collect_data_files = lambda *a, **k: []
    hooks.collect_submodules = lambda *a, **k: []
    utils = types.ModuleType('PyInstaller.utils')
    utils.hooks = hooks
    pi = types.ModuleType('PyInstaller')
    pi.utils = utils
    monkeypatch.setitem(sys.modules, 'PyInstaller', pi)
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils', utils)
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils.hooks', hooks)

    for var in ('GDAL_DATA', 'PROJ_DATA', 'PROJ_LIB', 'CONDA_PREFIX'):
        monkeypatch.delenv(var, raising=False)

    glb = {
        '__name__': '__pyinstaller_spec__',
        '__file__': SPEC_PATH,
        'Analysis': _make_stub('Analysis'),
        'PYZ': _make_stub('PYZ'),
        'EXE': _make_stub('EXE'),
        'COLLECT': _make_stub('COLLECT'),
    }
    return glb


def _exec_spec(glb):
    with open(SPEC_PATH, encoding='utf-8') as f:
        source = f.read()
    exec(compile(source, SPEC_PATH, 'exec'), glb)


def test_spec_raises_when_gdal_and_proj_data_missing(spec_env, monkeypatch):
    """gdal-config 无输出 + 找不到 proj.db → 构建期直接报错。"""
    monkeypatch.setattr(os, 'popen', lambda cmd: _FakePopen(''))
    monkeypatch.setattr(os.path, 'isdir', lambda p: False)
    monkeypatch.setattr(os.path, 'exists', lambda p: False)
    with pytest.raises(RuntimeError, match='gdal-data'):
        _exec_spec(spec_env)


def test_spec_raises_when_only_proj_data_missing(spec_env, monkeypatch):
    """gdal-data 找得到但 proj.db 找不到 → 同样必须报错。"""
    monkeypatch.setattr(os, 'popen', lambda cmd: _FakePopen('/fake/gdal\n'))

    real_isdir = os.path.isdir

    def fake_isdir(p):
        if p == '/fake/gdal':
            return True
        return False

    monkeypatch.setattr(os.path, 'isdir', fake_isdir)
    monkeypatch.setattr(os.path, 'exists', lambda p: False)
    with pytest.raises(RuntimeError, match='proj-data'):
        _exec_spec(spec_env)


def test_spec_collects_data_dirs_when_available(spec_env, monkeypatch):
    """两个数据目录都可用 → 正常走到 Analysis，datas 含 gdal-data/proj-data。"""
    monkeypatch.setattr(os, 'popen', lambda cmd: _FakePopen('/fake/gdal\n'))
    monkeypatch.setattr(os.path, 'isdir', lambda p: True)
    monkeypatch.setattr(os.path, 'exists', lambda p: True)
    _exec_spec(spec_env)
    datas = _Stub.captured['Analysis']['kwargs']['datas']
    dests = {dest for _src, dest in datas}
    assert 'gdal-data' in dests
    assert 'proj-data' in dests
