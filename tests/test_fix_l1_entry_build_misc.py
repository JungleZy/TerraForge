"""Code review LOW 级修复回归测试（L1：入口/核心/构建杂项）。

覆盖：
- app.py `-c` 作为 argv 最后元素时不再 IndexError，干净退出；
- create_app docstring 与实际返回的六元组一致；
- SECRET_KEY 未配置的警告移到 create_app（WSGI 部署也看得到）；
- database.py busy_timeout 先于 journal_mode=WAL 设置；
- config.py MAX_CONTENT_LENGTH 非法值回退默认并带变量名警告；
- process_watchdog 用 /proc/<pid>/cmdline 识别 PID 复用；
- nuitka_build pkg-config 调用加 win32 守卫、darwin gdal 候选去重、
  verify_no_missing_libs 把 exe 本体纳入 ldd 检查；
- build.sh / build.bat 的 GDAL 闸门收口到 scripts/check_gdal.py：接受
  requirements.txt 声明的范围、拒绝越界版本、拒绝缺 _gdal_array 的绑定。
"""
import logging
import os
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from conftest import fresh_import  # noqa: E402

ROOT = PROJECT_ROOT


def _read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


# ------------------------------------------------ app.py: `-c` 边界保护

def test_dash_c_without_program_exits_cleanly():
    """`exe -c`(程序段缺失)必须干净退出,而不是 IndexError  traceback。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'app.py'), '-c'],
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    assert proc.returncode == 1
    assert 'IndexError' not in proc.stderr
    assert "requires a program argument" in proc.stderr


def test_dash_c_with_program_still_executes():
    """回归保护:`-c 'prog'` 仍执行程序段而不是进入主程序。"""
    out = subprocess.check_output(
        [sys.executable, os.path.join(ROOT, 'app.py'), '-c', "print('EXEC_OK')"],
        text=True, timeout=300, cwd=ROOT,
    )
    assert 'EXEC_OK' in out
    assert 'TerraForge' not in out


# ------------------------- app.py: create_app docstring 与实际返回值一致

def test_create_app_docstring_matches_return(isolated_app):
    """docstring 不再声称只返回 (app, socketio) 二元组。"""
    doc = isolated_app.create_app.__doc__
    for name in ('app', 'socketio', 'task_manager', 'dem_task_manager',
                 'local_terrain_task_manager', 'contour_task_manager'):
        assert name in doc, f'docstring 缺少返回值成员 {name}'


# --------------------- app.py: SECRET_KEY 警告在 create_app 里统一打一次

def test_create_app_logs_secret_key_warning(monkeypatch, tmp_path, caplog):
    """SECRET_KEY 自动生成时,create_app 必须 logger.warning(WSGI 路径也经过这里)。"""
    from src.core import config

    monkeypatch.setattr(config.Config, 'SECRET_KEY_WAS_GENERATED', True)
    monkeypatch.setattr(config.Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(config.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'CACHE_DIR', tmp_path / 'cache')

    with caplog.at_level(logging.WARNING, logger='app'):
        fresh_import(monkeypatch, 'app', 'src.core.database')

    assert 'SECRET_KEY' in caplog.text


# --------------------- database.py: busy_timeout 先于 journal_mode=WAL

def test_busy_timeout_set_before_journal_mode():
    """journal_mode 切换本身也要拿库锁;busy_timeout 必须先生效,否则多实例
    同时启动时 journal_mode 这一步直接 database is locked。"""
    src = _read(os.path.join('src', 'core', 'database.py'))
    busy = src.index("conn.execute('PRAGMA busy_timeout")
    wal = src.index("conn.execute('PRAGMA journal_mode")
    assert busy < wal, 'busy_timeout 必须先于 journal_mode=WAL 设置'


# --------------------- config.py: MAX_CONTENT_LENGTH 非法值容错

# 注意:这里不用 fresh_import 重导入 src.core.config —— 同一会话内重导入同一子模块
# 两次以上时,monkeypatch 只恢复 sys.modules 条目,父包属性 src.core.config 仍指向
# 最后一次的新模块,后续测试 `from core import config` 与已缓存模块里的
# `from src.core.config import Config` 会拿到不同 Config(split-brain)。直接对
# 已导入模块调用解析函数即可覆盖同样的行为。

def test_max_content_length_invalid_falls_back_with_warning(monkeypatch, caplog):
    from src.core import config

    monkeypatch.setenv('MAX_CONTENT_LENGTH', 'not-a-number')
    with caplog.at_level(logging.WARNING, logger='src.core.config'):
        value = config._parse_max_content_length()
    assert value == config._DEFAULT_MAX_CONTENT_LENGTH
    assert 'MAX_CONTENT_LENGTH' in caplog.text, '报错必须带变量名'


def test_max_content_length_valid_env_still_honored(monkeypatch):
    from src.core import config

    monkeypatch.setenv('MAX_CONTENT_LENGTH', '12345')
    assert config._parse_max_content_length() == 12345


def test_max_content_length_config_uses_parser():
    """类属性必须经 _parse_max_content_length 求值(非法值容错才真的生效)。"""
    from src.core import config

    assert config.Config.MAX_CONTENT_LENGTH == config._parse_max_content_length()
    src = _read(os.path.join('src', 'core', 'config.py'))
    assert 'MAX_CONTENT_LENGTH = _parse_max_content_length()' in src


# --------------------- process_watchdog: PID 复用识别

import src.core.process_watchdog as pw  # noqa: E402

has_proc = pytest.mark.skipif(
    not os.path.exists('/proc/self/cmdline'), reason='需要 Linux /proc')


@has_proc
def test_read_proc_cmdline_reads_own_process():
    data = pw._read_proc_cmdline(os.getpid())
    assert data, '应能读到本进程 cmdline'


@has_proc
def test_read_proc_cmdline_missing_pid_returns_none():
    assert pw._read_proc_cmdline(2 ** 22) is None


@has_proc
def test_watchdog_exits_when_pid_reused(monkeypatch):
    """父进程 pid 被无关进程复用(cmdline 变化)时,看门狗必须视为父进程已死。"""
    monkeypatch.setenv(pw.PARENT_PID_ENV, str(os.getpid()))
    orig = pw._read_proc_cmdline
    calls = {'n': 0}

    def stub(pid):
        calls['n'] += 1
        # 第一次调用是 start_parent_watchdog 捕获父进程身份;之后模拟 pid 易主
        return orig(pid) if calls['n'] == 1 else orig(pid) + b'-reused'

    monkeypatch.setattr(pw, '_read_proc_cmdline', stub)
    exited = threading.Event()

    def fake_exit(code):
        exited.set()
        # 看门狗在 daemon 线程里跑;这里永不返回,线程卡在 fake_exit 内 ——
        # 既不杀测试进程,也不会在 monkeypatch teardown 后回到循环调真实 os._exit。
        threading.Event().wait()

    monkeypatch.setattr(os, '_exit', fake_exit)

    pw.start_parent_watchdog(interval=0.05)
    assert exited.wait(5), 'pid 易主(cmdline 变化)后看门狗未触发退出'


@has_proc
def test_watchdog_tolerates_unchanged_cmdline(monkeypatch):
    """cmdline 未变(父进程正常存活)时不得误触发退出。"""
    monkeypatch.setenv(pw.PARENT_PID_ENV, str(os.getpid()))
    orig = pw._read_proc_cmdline
    state = {'spoof': None}

    def stub(pid):
        # 默认透传真实 cmdline:既让本测试的看门狗认为一切正常,也保证
        # 其他测试残留的看门狗线程不会因 stub 误判而调用 fake_exit。
        return state['spoof'] if state['spoof'] is not None else orig(pid)

    monkeypatch.setattr(pw, '_read_proc_cmdline', stub)
    exited = threading.Event()

    def fake_exit(code):
        exited.set()
        threading.Event().wait()  # 见上个测试:卡住线程,避免 teardown 后误杀

    monkeypatch.setattr(os, '_exit', fake_exit)

    pw.start_parent_watchdog(interval=0.05)
    assert not exited.wait(0.5), 'cmdline 未变不应触发退出'

    # 收尾:让 cmdline 变化,看门狗调 fake_exit 卡在里边,不再循环。
    state['spoof'] = orig(os.getpid()) + b'-reused'
    assert exited.wait(5)


# --------------------- nuitka_build: pkg-config 平台守卫 / darwin 去重

import nuitka_build  # noqa: E402


class _OsShim:
    """属性代理:转发到真实 os 模块,允许覆盖单个属性(收窄 monkeypatch 范围)。"""

    def __getattr__(self, name):
        return getattr(os, name)


class _FakePopen:
    def __init__(self, output=''):
        self._output = output

    def read(self):
        return self._output


def test_pkg_config_guarded_on_win32(monkeypatch):
    """win32 下不得调用 pkg-config(不存在的 Unix 工具)。"""
    calls = []
    shim = _OsShim()
    shim.popen = lambda cmd: calls.append(cmd) or _FakePopen()
    monkeypatch.setattr(nuitka_build, 'os', shim)
    monkeypatch.setattr(sys, 'platform', 'win32')

    nuitka_build._proj_data_candidates()

    assert not any('pkg-config' in c for c in calls)


def test_pkg_config_still_used_on_linux(monkeypatch):
    """回归保护:非 win32 平台仍走 pkg-config 探测。"""
    calls = []
    shim = _OsShim()
    shim.popen = lambda cmd: calls.append(cmd) or _FakePopen()
    monkeypatch.setattr(nuitka_build, 'os', shim)
    monkeypatch.setattr(sys, 'platform', 'linux')

    nuitka_build._proj_data_candidates()

    assert any('pkg-config' in c for c in calls)


def test_gdal_data_candidates_darwin_no_duplicates(monkeypatch):
    """darwin 分支不得重复添加 '/usr/local/share/gdal'(通用列表已含)。"""
    for var in ('GDAL_DATA', 'CONDA_PREFIX'):
        monkeypatch.delenv(var, raising=False)
    shim = _OsShim()
    shim.popen = lambda cmd: _FakePopen()
    monkeypatch.setattr(nuitka_build, 'os', shim)
    monkeypatch.setattr(sys, 'platform', 'darwin')

    candidates = nuitka_build._gdal_data_candidates()

    assert candidates.count('/usr/local/share/gdal') == 1, candidates


# --------------------- nuitka_build: verify_no_missing_libs 查 exe 本体

linux_only = pytest.mark.skipif(sys.platform != 'linux', reason='ldd 自检仅 Linux')


@linux_only
def test_verify_no_missing_libs_checks_exe(monkeypatch, tmp_path):
    """主程序 exe(文件名不含 .so)缺库时也必须被自检抓住。"""
    exe = tmp_path / nuitka_build.APP_NAME
    exe.write_bytes(b'\x7fELF fake')

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            stdout=f"{cmd[1]}:\n\tlibgdal.so.34 => not found\n")

    monkeypatch.setattr(nuitka_build.subprocess, 'run', fake_run)

    with pytest.raises(RuntimeError, match=re.escape(str(exe))):
        nuitka_build.verify_no_missing_libs(str(tmp_path))


@linux_only
def test_verify_no_missing_libs_ok_when_resolved(monkeypatch, tmp_path):
    """回归保护:exe 与 .so 依赖齐全时自检通过。"""
    (tmp_path / nuitka_build.APP_NAME).write_bytes(b'\x7fELF fake')
    (tmp_path / 'libx.so').write_bytes(b'fake')
    monkeypatch.setattr(
        nuitka_build.subprocess, 'run',
        lambda cmd, **kw: SimpleNamespace(stdout='linux-vdso.so.1\n'))
    nuitka_build.verify_no_missing_libs(str(tmp_path))


# --------------------- build.sh / build.bat 的 GDAL 闸门(scripts/check_gdal.py)
#
# 2026-08-08 前这里钉的是「requirements.txt 缺少 GDAL== pin 时报错」——那条断言
# 把缺陷本身钉住了:requirements.txt 故意给的是范围(见该文件 GDAL 那行上方的
# 「⚠ 这里【不能】用精确钉」注释),所以 `^GDAL==`
# 恒不命中,build.sh 在 `set -euo pipefail` 下于赋值那一行静默 exit 1(连报错都打
# 不出来),build.bat 则每次都拒绝构建。旧测试只截取「读 pin」那一段跑,喂的又是
# 手写的 `GDAL==3.8.4`,所以永远看不到真 requirements.txt 会让脚本死掉。
# 现在钉的是闸门真正该判的两件事,并且用真 requirements.txt 的写法喂它。

CHECK_GDAL = os.path.join('scripts', 'check_gdal.py')


def _run_check_gdal(req_text, tmp_path, *, osgeo_version='3.11.4',
                    with_gdal_array=True):
    """子进程跑 scripts/check_gdal.py,返回 (returncode, 合并输出)。

    用 PYTHONPATH 前置一个 osgeo 桩包来控制「装的是哪个版本」「_gdal_array 在不
    在」:真 osgeo 已经 import 进本进程了,这两件事同进程内无法伪造,而它们恰恰是
    这道闸门唯一要判的东西。桩目录排在 site-packages 之前,所以能盖住真包。
    """
    stub = tmp_path / 'stub'
    (stub / 'osgeo').mkdir(parents=True)
    (stub / 'osgeo' / '__init__.py').write_text('', encoding='utf-8')
    (stub / 'osgeo' / 'gdal.py').write_text(
        f'__version__ = {osgeo_version!r}\n', encoding='utf-8')
    if with_gdal_array:
        (stub / 'osgeo' / 'gdal_array.py').write_text('', encoding='utf-8')

    req = tmp_path / 'requirements.txt'
    req.write_text(req_text, encoding='utf-8')

    env = dict(os.environ)
    env['PYTHONPATH'] = str(stub) + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, CHECK_GDAL), str(req)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=env, cwd=str(tmp_path),
    )
    return proc.returncode, proc.stdout + proc.stderr


# requirements.txt 里 GDAL 那一行的真实写法。写死在这里是有意的:政策变了就该
# 有用例红,而不是像旧版那样两个脚本悄悄失效。
_REAL_SPEC_LINE = 'aiohttp==3.9.1\nGDAL>=3.8,<4\nnumpy==1.26.4\n'


def test_gdal_gate_accepts_the_range_that_requirements_actually_declares(tmp_path):
    """核心回归:范围(而非 == pin)必须放行。旧实现在这条上 exit 1。"""
    rc, out = _run_check_gdal(_REAL_SPEC_LINE, tmp_path)
    assert rc == 0, out
    assert '3.11.4' in out


def test_gdal_gate_range_matches_the_real_requirements_file():
    """真 requirements.txt 必须能过闸门 —— 否则 ./build.sh 又不可用了。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, CHECK_GDAL)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_gdal_gate_rejects_version_outside_the_declared_range(tmp_path):
    """上限 `<4` 有实测依据(GDAL 4 默认开异常会让 _raise_on_gdal_error 空转)。"""
    rc, out = _run_check_gdal(_REAL_SPEC_LINE, tmp_path, osgeo_version='4.0.1')
    assert rc != 0
    assert '<4' in out


def test_gdal_gate_rejects_bindings_without_gdal_array(tmp_path):
    """带 build isolation 装出来的绑定缺 _gdal_array,而版本号照样读得出。

    旧闸门只比 major.minor,检不出这一类 —— exe 能构建、能启动、能服务首页,
    所有走 ReadAsArray/WriteArray 的 DEM/地形/等高线作业才在运行时炸。
    """
    rc, out = _run_check_gdal(_REAL_SPEC_LINE, tmp_path, with_gdal_array=False)
    assert rc != 0
    assert 'gdal_array' in out
    assert '--no-build-isolation' in out, '报错必须带上正确的装法'


def test_gdal_gate_reports_a_missing_dependency_line(tmp_path):
    """依赖行整行不见了要响亮报错 —— 且必须真的打出来(旧版这句是死代码)。"""
    rc, out = _run_check_gdal('flask\n', tmp_path)
    assert rc != 0
    assert 'GDAL' in out and 'requirements.txt' in out


def test_gdal_gate_ignores_the_install_hint_in_requirements_comments(tmp_path):
    """requirements.txt 的注释里就有 `GDAL==$(gdal-config --version)` 示例。

    依赖行匹配是行首锚定的,注释里的示例不能被当成声明的约束。
    """
    rc, out = _run_check_gdal(
        '# pip install --no-build-isolation "GDAL==2.4.0"\nGDAL>=3.8,<4\n', tmp_path)
    assert rc == 0, out


def _strip_script_comments(name, content):
    """去掉整行注释 —— 注释里提 `GDAL==` / requirements.txt 是解释历史，不是实现。"""
    prefix = 'REM' if name.endswith('.bat') else '#'
    return '\n'.join(
        line for line in content.splitlines()
        if not line.lstrip().upper().startswith(prefix.upper())
        or (prefix == '#' and line.startswith('#!')))


def test_build_scripts_do_not_reimplement_requirements_parsing():
    """两个脚本都只许调共享闸门,不许自己再解析一遍 requirements.txt。

    「一份规则两处实现」正是这个缺陷的成因:requirements.txt 的 pin/range 政策
    改了,两个脚本里的正则没跟上,而 CI 走 nuitka_build.py 绕开了它们。
    """
    for name in ('build.sh', 'build.bat'):
        raw = _read(name)
        assert 'check_gdal.py' in raw, f'{name} 必须调用 scripts/check_gdal.py'
        code = _strip_script_comments(name, raw)
        assert 'GDAL==' not in code, (
            f'{name} 又在自己解析 GDAL== pin —— requirements.txt 给的是范围')
        # requirements.txt 可以被【安装】任意多次(依赖安装 + nuitka 缺失时补装),
        # 也可以在 echo/注释里被提到 —— 但一次都不许被【读取解析】。原来这里断言
        # 出现次数 == 1,那是把「不许自己解析」误当成「只许提一次」,nuitka 改成从
        # requirements.txt 带版本安装之后就误报了。改成直接钉「没有解析构造」。
        assert not re.search(
            r'(grep|findstr|awk|sed|cat|type|for\s*/f)[^\n]*requirements\.txt', code), (
            f'{name} 又在自己解析 requirements.txt —— 判据只许住在 scripts/check_gdal.py')
        assert 'uv pip install -r requirements.txt' in code, (
            f'{name} 仍然必须按 requirements.txt 装依赖')
