"""I20a/c/d 修复契约测试 —— 构建/发布脚本的形态断言。

覆盖：
- build.sh / build.bat 必须安装 requirements.txt 依赖（干净环境不再必失败）；
- build.sh 必须做 GDAL pin 与系统 gdal-config 版本一致性检查；
- push-release.sh / push-release.bat 不得硬编码 v0.0.1，版本须参数化
  （命令行参数或从 build.spec 的 APP_VERSION 单一来源读取）。
"""
import os

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

def test_push_release_sh_not_hardcoded_v001():
    content = _read('push-release.sh')
    assert 'v0.0.1' not in content, 'push-release.sh 仍硬编码 v0.0.1（当前版本 0.1.0）'


def test_push_release_sh_version_parameterized():
    content = _read('push-release.sh')
    # 接受命令行参数覆盖，或从 build.spec 的 APP_VERSION 单一来源读取
    assert '$1' in content or 'APP_VERSION' in content


def test_push_release_sh_strict_error_handling():
    assert 'set -euo pipefail' in _read('push-release.sh')


def test_push_release_bat_not_hardcoded_v001():
    content = _read('push-release.bat')
    assert 'v0.0.1' not in content, 'push-release.bat 仍硬编码 v0.0.1（当前版本 0.1.0）'


def test_push_release_bat_version_parameterized():
    content = _read('push-release.bat')
    assert '%~1' in content or 'APP_VERSION' in content
