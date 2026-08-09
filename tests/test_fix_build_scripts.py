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
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from conftest import REAL_BASH, needs_bash  # noqa: E402


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


# 真 bash 的探测收口到 tests/conftest.py 的 find_real_bash()。这里原本有一份
# 逐字相同的实现，test_fix_l1_entry_build_misc.py 里还有第三份 —— 后者随那批
# 用例被删之后，新写的 CI 用例直接 `subprocess.run(['bash', ...])`，在 Windows
# runner 上撞了同一个 WSL 占位 stub，把 v0.2.12 的发版构建打断了一次。
# 一份规则三处实现，删掉任一处知识就跟着丢 —— 所以只留 conftest 那一份。
_BASH = REAL_BASH


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


# ---------- P2: CI 治理 —— 闸门要真挂在 CI 上，matrix 不许连坐 ----------
#
# 上面两条钉的是 build.sh / build.bat 调 scripts/check_gdal.py，而发版走的是
# workflow 里的打包步骤，从旁边绕过了闸门：缺 _gdal_array 的绑定打出来的 exe
# 能构建、能启动、冒烟请求 `/` 也 200，只有用户机器上的 DEM/地形/等高线作业
# 会炸。所以「谁调闸门」这件事必须对 CI 也有断言。
#
# 认的是真实的 `run:` 命令行而不是文件里任意一处提及：注释里写一句「闸门」不
# 该让这些断言变绿。

# workflow 里跑闸门的那一行（认命令，不认注释）
_GATE_RUN = re.compile(r'^\s*run: python scripts/check_gdal\.py\s*$', re.MULTILINE)

_WORKFLOW_DIR = os.path.join('.github', 'workflows')
_WORKFLOWS = ['build.yml', 'test-build.yml']


def _workflow(name):
    return _read(os.path.join(_WORKFLOW_DIR, name))


def _jobs(text):
    """按 job 把 workflow 文本切开（jobs: 下缩进两格的键）。

    项目不依赖 PyYAML（见 tests/test_fix_ci_workflows.py 的文本契约先例），而
    「每个 job 各自都要有闸门」这条判据必须按 job 看：整文件看的话，test-build.yml
    里 test job 有闸门就能替真正打包的 build job 顶包。
    """
    body = text[text.index('\njobs:') + 1:]
    heads = list(re.finditer(r'^  ([A-Za-z_][\w-]*):$', body, re.MULTILINE))
    assert heads, 'workflow 里一个 job 都没解析出来'
    return {m.group(1): body[m.start():(heads[i + 1].start() if i + 1 < len(heads) else len(body))]
            for i, m in enumerate(heads)}


@pytest.mark.parametrize('name', _WORKFLOWS)
def test_every_job_that_builds_runs_the_gdal_gate_first(name):
    """凡是跑 nuitka_build.py 的 job，都必须先跑 scripts/check_gdal.py。"""
    building = {jn: src for jn, src in _jobs(_workflow(name)).items()
                if 'python nuitka_build.py' in src}
    assert building, f'{name} 里找不到打包 job'
    for jn, src in building.items():
        gate = _GATE_RUN.search(src)
        assert gate, (
            f'{name} 的 {jn} job 直接打包，没跑 GDAL 闸门 scripts/check_gdal.py')
        assert gate.start() < src.index('python nuitka_build.py'), (
            f'{name} 的 {jn} job 把闸门排在 Nuitka 之后 —— 坏绑定已经打进产物了')


def test_gdal_gate_runs_after_the_conda_pin_that_it_polices():
    """Windows/macOS 那句 `conda install gdal=<x.y>` 是硬编码的构建输入，
    它凭什么可以硬编码：闸门排在它【之后】，拿 requirements.txt 的范围去量真装上
    的版本，钉值一旦漂出范围就是 CI 红。顺序反了，硬编码就重新变成没人管的
    第二处版本规则（评审 P2：一份规则两处实现，而 CI 那处不读另一处）。"""
    wf = _workflow('build.yml')
    pin = re.search(r'^\s*conda install .*\bgdal=', wf, re.MULTILINE)
    assert pin, 'conda 装 GDAL 的那行不见了 —— 本用例的前提变了，请重看闸门顺序'
    gate = _GATE_RUN.search(wf)
    assert gate and pin.start() < gate.start(), (
        'GDAL 闸门必须排在 conda 硬编码版本之后，否则那个钉值无人校验')


def test_build_matrix_does_not_fail_fast():
    """Create Release 跑在每个 matrix job 内部：默认的 fail-fast: true 会让一个
    平台失败连带取消另外两个，Release 就停在只挂了一两个平台产物的状态 —— 与文件
    顶部为同一件事设的 cancel-in-progress: false 直接冲突。"""
    wf = _workflow('build.yml')
    strategy = wf[wf.index('    strategy:'):wf.index('        os: [')]
    assert re.search(r'^\s*fail-fast:\s*false\s*$', strategy, re.MULTILINE), (
        'build.yml 的 matrix 缺 fail-fast: false —— 一个平台挂掉会连坐另外两个，'
        '发布出去的 Release 只带部分平台产物')