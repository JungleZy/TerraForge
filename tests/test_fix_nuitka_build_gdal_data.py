"""I20b 修复行为测试 —— nuitka_build.py 的 gdal-data/proj-data 收集失败必须让构建报错。

nuitka_build.main() 在调用 Nuitka 之前先定位 GDAL/PROJ 数据目录,找不到就
直接 raise,而不是静默产出一个能构建但跑起来就坏的 exe。本测试 monkeypatch
os.popen / os.path 模拟「gdal-config 不存在 / proj.db 找不到」的环境做断言,
并用 stub 拦截 subprocess.check_call,不会真的触发 Nuitka 编译。
"""
import os

import pytest

import nuitka_build


class _FakePopen:
    def __init__(self, output):
        self._output = output

    def read(self):
        return self._output


@pytest.fixture
def build_env(monkeypatch, tmp_path):
    """隔离数据目录发现所依赖的环境变量与文件系统。"""
    for var in ('GDAL_DATA', 'PROJ_DATA', 'PROJ_LIB', 'CONDA_PREFIX'):
        monkeypatch.delenv(var, raising=False)
    # 防止真的执行 Nuitka / 共享库拷贝 / 产物重命名
    monkeypatch.setattr(nuitka_build.subprocess, 'check_call', lambda cmd: None)
    monkeypatch.setattr(nuitka_build, 'copy_extension_system_libs', lambda d: None)
    monkeypatch.setattr(nuitka_build, 'copy_extension_system_dlls_windows', lambda d: None)
    monkeypatch.setattr(nuitka_build, 'verify_no_missing_libs', lambda d: None)
    monkeypatch.setattr(nuitka_build.os.path, 'isfile', lambda p: True)
    monkeypatch.setattr(nuitka_build.os, 'rename', lambda s, d: None)
    monkeypatch.setattr(nuitka_build.shutil, 'rmtree', lambda p, **k: None)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def test_build_raises_when_gdal_and_proj_data_missing(build_env):
    """数据目录都找不到 → 构建期直接报错。"""
    build_env.setattr(nuitka_build, '_gdal_data_candidates', lambda: ['/fake/gdal'])
    build_env.setattr(nuitka_build, '_proj_data_candidates', lambda: ['/fake/proj'])
    build_env.setattr(os.path, 'isdir', lambda p: False)
    build_env.setattr(os.path, 'exists', lambda p: False)
    with pytest.raises(RuntimeError, match='gdal-data'):
        nuitka_build.main()


def test_build_raises_when_only_proj_data_missing(build_env):
    """gdal-data 找得到但 proj.db 找不到 → 同样必须报错。"""
    build_env.setattr(nuitka_build, '_gdal_data_candidates', lambda: ['/fake/gdal'])
    build_env.setattr(nuitka_build, '_proj_data_candidates', lambda: ['/fake/proj'])
    build_env.setattr(os.path, 'isdir', lambda p: True)
    build_env.setattr(os.path, 'exists', lambda p: p == os.path.join('/fake/gdal', 'epsg.wkt'))
    with pytest.raises(RuntimeError, match='proj-data'):
        nuitka_build.main()


def test_build_collects_data_dirs_when_available(build_env):
    """两个数据目录都可用 → Nuitka 命令行包含 gdal-data/proj-data 数据目录。"""
    captured = []
    build_env.setattr(
        nuitka_build.subprocess, 'check_call', lambda cmd: captured.append(cmd),
    )
    build_env.setattr(os, 'popen', lambda cmd: _FakePopen('/fake/gdal\n'))
    build_env.setattr(os.path, 'isdir', lambda p: True)
    build_env.setattr(os.path, 'exists', lambda p: True)

    nuitka_build.main()

    assert captured, 'Nuitka 未被调用'
    cmd = captured[0]
    assert any(arg.endswith('=gdal-data') for arg in cmd if arg.startswith('--include-data-dir='))
    assert any(arg.endswith('=proj-data') for arg in cmd if arg.startswith('--include-data-dir='))
