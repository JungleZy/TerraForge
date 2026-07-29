"""I20b 修复行为测试 —— hook-gdal.py 运行时钩子不得静默跳过缺失的数据目录。

打包产物若缺 gdal-data/proj-data，hook 必须在启动时就大声报错，
而不是让 exe 跑起来后在 GDAL 调用处莫名失败。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_PATH = os.path.join(ROOT, 'hook-gdal.py')

_ENV_VARS = ('GDAL_DATA', 'PROJ_LIB', 'PROJ_DATA')


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delattr(sys, '_MEIPASS', raising=False)
    return monkeypatch


def _exec_hook():
    with open(HOOK_PATH, encoding='utf-8') as f:
        source = f.read()
    exec(compile(source, HOOK_PATH, 'exec'), {'__name__': '__pyinstaller_hook__'})


def test_hook_sets_env_when_data_dirs_present(clean_env, tmp_path):
    gdal_dir = tmp_path / 'gdal-data'
    proj_dir = tmp_path / 'proj-data'
    gdal_dir.mkdir()
    proj_dir.mkdir()
    clean_env.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    _exec_hook()
    assert os.environ['GDAL_DATA'] == str(gdal_dir)
    assert os.environ['PROJ_LIB'] == str(proj_dir)
    assert os.environ['PROJ_DATA'] == str(proj_dir)


def test_hook_raises_when_data_dirs_missing(clean_env, tmp_path):
    """冻结产物缺数据目录 → 启动即报错，不得静默。"""
    clean_env.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    with pytest.raises(RuntimeError, match='gdal-data'):
        _exec_hook()


def test_hook_noop_outside_pyinstaller(clean_env):
    """非冻结环境（无 _MEIPASS）→ 不做任何事，也不报错。"""
    _exec_hook()
    for var in _ENV_VARS:
        assert var not in os.environ
