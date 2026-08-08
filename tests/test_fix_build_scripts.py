"""I20a/c/d 修复契约测试 —— 构建/发布脚本的形态与行为断言。

覆盖：
- build.sh / build.bat 必须安装 requirements.txt 依赖（干净环境不再必失败）；
- build.sh 必须做 GDAL pin 与系统 gdal-config 版本一致性检查；
- push-release.sh / push-release.bat 不得硬编码 v0.0.1，版本须参数化
  （命令行参数覆盖，或从 src/core/config.py 的 Config.APP_VERSION 单一事实源解析；
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


def test_build_bat_checks_nuitka_build_exit_code():
    """M20：build.bat 必须查主构建调用的退出码，不能只看目录存在。

    nuitka_build.py 很早就把 dist/app.dist 重命名成 dist/terraforge，之后才依次
    跑产物自检 —— 那些自检一旦触发，目标目录已经存在了。只用 `if exist` 判定会
    把失败的构建报成 "Build successful!"，交付一个「能构建能启动但每次 GDAL
    调用都失败」的包（nuitka_build.py 的 raise 文案原话）。

    build.sh 靠 `set -euo pipefail` 天然有这个保护 —— 两个脚本的不对称此前从未
    被任何断言约束（本文件只钉了 build.sh 那半边）。
    """
    content = _read('build.bat')
    idx = content.find('uv run python nuitka_build.py')
    assert idx != -1, 'build.bat 里找不到主构建调用'
    tail = content[idx:idx + 600]
    assert 'errorlevel 1' in tail, (
        'build.bat 调用 nuitka_build.py 之后没有紧跟 errorlevel 检查 —— '
        '构建失败会被报成成功'
    )
    assert 'exit /b 1' in tail, 'errorlevel 命中后必须以非零码退出'


def test_build_bat_checks_nuitka_install_exit_code():
    """M20 顺带：nuitka 的补装步骤本身也要查退出码。

    锚点不再是 `uv pip install nuitka` 那串字面量 —— 2026-08-08 起 nuitka 从
    requirements.txt 带版本安装（裸装 latest 会在 tag 推出去之后打断 Windows
    构建，见 P1#13），命令变了而**不变量没变**：安装失败必须中止构建，否则
    后续步骤会以一个不完整的环境继续跑。所以按「补装 nuitka 的那个分支」定位。
    """
    content = _read('build.bat')
    idx = content.find('Installing Nuitka')
    assert idx != -1, 'build.bat 里找不到 nuitka 补装步骤'
    tail = content[idx:idx + 300]
    assert 'uv pip install' in tail, '补装分支里应当真的执行安装'
    assert 'uv pip install nuitka' not in tail, (
        '又在裸装 latest nuitka —— 版本必须来自 requirements.txt')
    assert 'errorlevel 1' in tail, (
        'nuitka 安装失败必须中止构建，否则后续步骤会以一个不完整的环境继续跑'
    )


# ---------- I20d: GDAL 闸门（scripts/check_gdal.py，两个脚本共用） ----------
#
# 旧断言钉的是 `gdal-config --version` 出现在 build.sh 里，以及 build.bat 含
# 'GDAL'+'requirements.txt' —— 两条都能被一个**恒定失败**的闸门满足，实际上
# build.sh 在 `set -euo pipefail` 下会在读 pin 那一行静默 exit 1。行为断言在
# tests/test_fix_l1_entry_build_misc.py（喂真 requirements.txt 的写法跑闸门）。

def test_build_sh_calls_the_shared_gdal_gate():
    content = _read('build.sh')
    assert 'scripts/check_gdal.py' in content, (
        'build.sh 必须调用共享的 GDAL 闸门 scripts/check_gdal.py'
    )


def test_build_bat_calls_the_shared_gdal_gate():
    content = _read('build.bat')
    # 找命令行本身，不是上面那段解释历史的 REM 注释
    idx = content.find('check_gdal.py', content.find('uv run python scripts'))
    assert idx != -1, 'build.bat 必须调用共享的 GDAL 闸门 scripts\\check_gdal.py'
    assert 'errorlevel 1' in content[idx:idx + 200], (
        'build.bat 里闸门失败必须中止构建 —— batch 不会自动传播退出码'
    )


# ---------- I20c: push-release 版本参数化 ----------

def _config_app_version():
    """从单一事实源 src/core/config.py 解析 Config.APP_VERSION。"""
    m = re.search(
        r"APP_VERSION\s*=\s*'([0-9.]+)'",
        _read(os.path.join('src', 'core', 'config.py')),
    )
    assert m, 'src/core/config.py 中未找到 Config.APP_VERSION'
    return m.group(1)


def test_app_version_matches_the_release_notes_heading():
    """APP_VERSION 与 RELEASE_NOTES.md 顶部标题必须一致。

    2026-08-08 评审抓到的漂移：`APP_VERSION='0.2.11'` 而发版说明写 v0.2.12。
    危险的不是无参发版（`push-release.sh` 从 APP_VERSION 取 tag，撞已存在的
    v0.2.11 会响亮中止），而是文档化的 `./push-release.sh 0.2.12` —— 它照样
    打 tag、发版，而产物启动横幅印的是 0.2.11。
    """
    heading = re.search(r'^##\s*v([0-9.]+)', _read('RELEASE_NOTES.md'), re.M)
    assert heading, 'RELEASE_NOTES.md 顶部找不到 `## vX.Y.Z` 标题'
    assert _config_app_version() == heading.group(1), (
        f"Config.APP_VERSION={_config_app_version()} 与 RELEASE_NOTES.md 的 "
        f"v{heading.group(1)} 不一致 —— 发版会印错版本号")


def _sh_run_version_resolution(*args, cwd=ROOT):
    """实际执行 push-release.sh 开头到 TAG= 之前的版本解析段。"""
    head = _read('push-release.sh').split('TAG=', 1)[0]
    return subprocess.run(
        [_BASH, '-c', head + '\necho "RESOLVED:$VERSION"', 'push-release.sh', *args],
        cwd=cwd, capture_output=True, text=True,
        # 脚本输出含 UTF-8 中文；Windows 上 text=True 默认 cp1252，
        # 解码失败会崩 reader 线程、stdout 变 None
        encoding='utf-8', errors='replace',
    )


def _find_real_bash():
    """找真正可用的 bash，找不到返回 None。

    Windows 的 System32\\bash.exe 是 WSL 安装占位 stub：which 找得到、能执行，
    但没有发行版时只打印「Windows Subsystem for Linux … to install」（UTF-16）
    并以非零退出——而且它对本该失败的调用的退出码与参数长度相关，功能验证
    用 `true` 挡不住。可靠的验明正身：`--version` 必须输出 GNU bash
    （WSL/git-bash 都是 GNU，stub 只会打印安装提示）。
    Windows 上 GitHub Actions 的 git-bash 常不在 pytest 的 PATH 里，
    额外探测 Git for Windows 的默认安装位置。
    """
    candidates = []
    found = shutil.which('bash')
    if found:
        candidates.append(found)
    if os.name == 'nt':
        candidates += [
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                         'Git', 'bin', 'bash.exe'),
            os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                         'Git', 'bin', 'bash.exe'),
        ]
    for exe in candidates:
        try:
            proc = subprocess.run([exe, '--version'], capture_output=True, timeout=10)
        except Exception:
            continue
        if proc.returncode == 0 and b'GNU bash' in proc.stdout:
            return exe
    return None


_BASH = _find_real_bash()
needs_bash = pytest.mark.skipif(
    _BASH is None,
    reason='需要可用的 GNU bash（Windows 的 WSL 占位 stub 不算）')


def test_push_release_sh_not_hardcoded_v001():
    assert 'v0.0.1' not in _read('push-release.sh')


def test_push_release_sh_reads_version_from_core_config():
    content = _read('push-release.sh')
    assert 'src/core/config.py' in content, (
        'push-release.sh 必须从 src/core/config.py 的 Config.APP_VERSION 读取版本'
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
    # 在没有 src/core/config.py 的目录下运行：应给出明确报错而非静默失败
    proc = _sh_run_version_resolution(cwd=str(tmp_path))
    assert proc.returncode != 0
    assert '无法确定版本号' in proc.stdout + proc.stderr


def test_push_release_sh_strict_error_handling():
    assert 'set -euo pipefail' in _read('push-release.sh')


def test_push_release_bat_not_hardcoded_v001():
    assert 'v0.0.1' not in _read('push-release.bat')


def test_push_release_bat_reads_version_from_core_config():
    content = _read('push-release.bat')
    assert 'src/core/config.py' in content, (
        'push-release.bat 必须从 src/core/config.py 的 Config.APP_VERSION 读取版本'
    )
    assert 'build.spec' not in content, (
        'build.spec 已随 Nuitka 迁移删除，push-release.bat 不得再引用'
    )
    assert '%~1' in content, 'push-release.bat 必须保留命令行参数覆盖'
    assert 'APP_VERSION' in content
