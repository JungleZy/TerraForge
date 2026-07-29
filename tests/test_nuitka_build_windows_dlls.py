"""nuitka_build.py Windows GDAL DLL 补拷逻辑测试。

CI 的 conda 布局下 GDAL DLL 在 conda 前缀内,Nuitka 会自动复制;
OSGeo4W 等前缀外布局会被 Nuitka 剥掉,需要 copy_extension_system_dlls_windows
用依赖闭包补拷。本测试用桩模拟 win32 平台、osgeo 包布局与 Nuitka 依赖扫描器,
不依赖真实 Windows 环境。
"""
import os
import sys
import types

import pytest

import nuitka_build


def test_is_windows_system_dll(monkeypatch):
    monkeypatch.setenv('SystemRoot', r'C:\Windows')
    # Windows 目录下 / API 集 / MSVC 运行库 → 系统 DLL,不拷
    assert nuitka_build._is_windows_system_dll(r'C:\Windows\System32\kernel32.dll')
    assert nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\api-ms-win-core-x.dll')
    assert nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\vcruntime140.dll')
    assert nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\msvcp140.dll')
    # GDAL 及其第三方依赖 → 必须拷
    assert not nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\gdal310.dll')
    assert not nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\proj.dll')
    assert not nuitka_build._is_windows_system_dll(r'D:\conda\Library\bin\geos_c.dll')


def test_find_bundled_gdal_dll(tmp_path):
    assert not nuitka_build._find_bundled_gdal_dll(str(tmp_path))
    (tmp_path / 'osgeo').mkdir()
    (tmp_path / 'osgeo' / 'gdal310.dll').write_bytes(b'x')
    assert nuitka_build._find_bundled_gdal_dll(str(tmp_path))


@pytest.fixture
def fake_windows(monkeypatch, tmp_path):
    """模拟 win32 平台 + 前缀外 GDAL 布局(conda Library/bin 风格的假目录)。"""
    monkeypatch.setattr(nuitka_build.sys, 'platform', 'win32')

    windir = tmp_path / 'Windows'
    (windir / 'System32').mkdir(parents=True)
    monkeypatch.setenv('SystemRoot', str(windir))
    system_dll = windir / 'System32' / 'kernel32.dll'
    system_dll.write_bytes(b'sys')

    bindir = tmp_path / 'Library' / 'bin'
    bindir.mkdir(parents=True)
    for name in ('gdal310.dll', 'proj.dll'):
        (bindir / name).write_bytes(b'dll')

    pkg_dir = tmp_path / 'site-packages' / 'osgeo'
    pkg_dir.mkdir(parents=True)
    (pkg_dir / '_gdal.pyd').write_bytes(b'pyd')
    osgeo_mod = types.ModuleType('osgeo')
    osgeo_mod.__file__ = str(pkg_dir / '__init__.py')
    monkeypatch.setitem(sys.modules, 'osgeo', osgeo_mod)

    detector = types.ModuleType('nuitka.freezer.DllDependenciesWin32')
    detector.detectBinaryPathDLLsWin32 = lambda **kw: [
        str(bindir / 'gdal310.dll'),
        str(bindir / 'proj.dll'),
        str(system_dll),
    ]
    monkeypatch.setitem(sys.modules, 'nuitka.freezer.DllDependenciesWin32', detector)

    dist = tmp_path / 'dist'
    dist.mkdir()
    return dist


def test_windows_copy_fills_missing_gdal_dlls(fake_windows):
    """前缀外布局 → 闭包中的非系统 DLL 补拷进 dist,系统 DLL 跳过。"""
    nuitka_build.copy_extension_system_dlls_windows(str(fake_windows))
    assert (fake_windows / 'gdal310.dll').exists()
    assert (fake_windows / 'proj.dll').exists()
    assert not (fake_windows / 'kernel32.dll').exists()


def test_windows_copy_noop_when_gdal_already_bundled(fake_windows):
    """conda 布局(Nuitka 已自动复制)→ no-op,不调用扫描器也不报错。"""
    (fake_windows / 'gdal310.dll').write_bytes(b'x')
    nuitka_build.copy_extension_system_dlls_windows(str(fake_windows))


def test_windows_copy_raises_when_closure_has_no_gdal(fake_windows, monkeypatch):
    """补拷后仍无 gdal*.dll → 构建必须报错,不得静默产出坏包。"""
    detector = sys.modules['nuitka.freezer.DllDependenciesWin32']
    monkeypatch.setattr(detector, 'detectBinaryPathDLLsWin32', lambda **kw: [])
    with pytest.raises(RuntimeError, match='GDAL DLL'):
        nuitka_build.copy_extension_system_dlls_windows(str(fake_windows))


def test_windows_copy_noop_on_non_windows(monkeypatch, tmp_path):
    """非 Windows 平台 → no-op。"""
    monkeypatch.setattr(nuitka_build.sys, 'platform', 'linux')
    nuitka_build.copy_extension_system_dlls_windows(str(tmp_path))
