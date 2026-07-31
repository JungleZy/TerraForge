"""I20a/c/d 修复契约测试 —— 构建/发布脚本的形态与行为断言。

覆盖：
- build.sh / build.bat 必须安装 requirements.txt 依赖（干净环境不再必失败）；
- build.sh 必须做 GDAL pin 与系统 gdal-config 版本一致性检查；
- push-release.sh / push-release.bat 不得硬编码 v0.0.1，版本须参数化
  （命令行参数覆盖，或从 core/config.py 的 Config.APP_VERSION 单一事实源解析；
  build.spec 已随 PyInstaller→Nuitka 迁移删除，脚本不得再引用）。
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    # 发版脚本已移至 scripts/，构建脚本仍在根目录
    if name.startswith('push-release.'):
        name = os.path.join('scripts', name)
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


# ---------- I20a: 构建脚本必须装依赖 ----------

def test_build_sh_installs_requirements():
    assert 'uv pip install -r requirements.txt' in _read('build.sh')


def test_build_bat_installs_requirements():
    assert 'uv pip install -r requirements.txt' in _read('build.bat')


def test_build_sh_strict_error_handling():
    assert 'set -euo pipefail' in _read('build.sh')


# ---------- I20d: GDAL 版本一致性检查 ----------

def test_build_sh_checks_gdal_version_against_pin():
    content = _read('build.sh')
    assert 'gdal-config --version' in content, (
        'build.sh 必须读取系统 GDAL 版本与 requirements.txt 的 pin 做一致性检查'
    )


def test_build_bat_checks_gdal_version():
    content = _read('build.bat')
    assert 'GDAL' in content and 'requirements.txt' in content, (
        'build.bat 必须做 GDAL pin 版本一致性检查'
    )


# ---------- I20c: push-release 版本参数化 ----------

def _config_app_version():
    """从单一事实源 core/config.py 解析 Config.APP_VERSION。"""
    m = re.search(
        r"APP_VERSION\s*=\s*'([0-9.]+)'",
        _read(os.path.join('core', 'config.py')),
    )
    assert m, 'core/config.py 中未找到 Config.APP_VERSION'
    return m.group(1)


def _sh_run_version_resolution(*args, cwd=ROOT):
    """实际执行 push-release.sh 开头到 TAG= 之前的版本解析段。"""
    head = _read('push-release.sh').split('TAG=', 1)[0]
    return subprocess.run(
        ['bash', '-c', head + '\necho "RESOLVED:$VERSION"', 'push-release.sh', *args],
        cwd=cwd, capture_output=True, text=True,
    )


def _bash_is_usable():
    """Windows 的 System32\\bash.exe 是 WSL 安装占位 stub：which 找得到、
    能执行，但只打印「Windows Subsystem for Linux … to install」（UTF-16）
    并以非零退出。路径探测挡不住它，必须功能验证——真跑一句 `true`。"""
    exe = shutil.which('bash')
    if not exe:
        return False
    try:
        return subprocess.run(
            [exe, '-c', 'true'], capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


needs_bash = pytest.mark.skipif(
    not _bash_is_usable(),
    reason='需要可用的 bash（Windows 的 WSL 占位 stub 不算）')


def test_push_release_sh_not_hardcoded_v001():
    assert 'v0.0.1' not in _read('push-release.sh')


def test_push_release_sh_reads_version_from_core_config():
    content = _read('push-release.sh')
    assert 'core/config.py' in content, (
        'push-release.sh 必须从 core/config.py 的 Config.APP_VERSION 读取版本'
    )
    assert 'build.spec' not in content, (
        'build.spec 已随 Nuitka 迁移删除，push-release.sh 不得再引用'
    )


@needs_bash
def test_push_release_sh_resolves_version_from_config():
    proc = _sh_run_version_resolution()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith('RESOLVED:' + _config_app_version())


@needs_bash
def test_push_release_sh_cli_arg_overrides_config():
    proc = _sh_run_version_resolution('9.9.9')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith('RESOLVED:9.9.9')


@needs_bash
def test_push_release_sh_clear_error_when_version_unresolvable(tmp_path):
    # 在没有 core/config.py 的目录下运行：应给出明确报错而非静默失败
    proc = _sh_run_version_resolution(cwd=str(tmp_path))
    assert proc.returncode != 0
    assert '无法确定版本号' in proc.stdout + proc.stderr


def test_push_release_sh_strict_error_handling():
    assert 'set -euo pipefail' in _read('push-release.sh')


def test_push_release_bat_not_hardcoded_v001():
    assert 'v0.0.1' not in _read('push-release.bat')


def test_push_release_bat_reads_version_from_core_config():
    content = _read('push-release.bat')
    assert 'core/config.py' in content, (
        'push-release.bat 必须从 core/config.py 的 Config.APP_VERSION 读取版本'
    )
    assert 'build.spec' not in content, (
        'build.spec 已随 Nuitka 迁移删除，push-release.bat 不得再引用'
    )
    assert '%~1' in content, 'push-release.bat 必须保留命令行参数覆盖'
    assert 'APP_VERSION' in content
