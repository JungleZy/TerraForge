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
- build.sh / build.bat 在 requirements.txt 缺少 GDAL== pin 时给出明确报错。
"""
import logging
import os
import re
import shutil
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
    from core import config

    monkeypatch.setattr(config.Config, 'SECRET_KEY_WAS_GENERATED', True)
    monkeypatch.setattr(config.Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(config.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'OUTPUT_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(config.Config, 'CACHE_DIR', tmp_path / 'cache')

    with caplog.at_level(logging.WARNING, logger='app'):
        fresh_import(monkeypatch, 'app', 'core.database')

    assert 'SECRET_KEY' in caplog.text


# --------------------- database.py: busy_timeout 先于 journal_mode=WAL

def test_busy_timeout_set_before_journal_mode():
    """journal_mode 切换本身也要拿库锁;busy_timeout 必须先生效,否则多实例
    同时启动时 journal_mode 这一步直接 database is locked。"""
    src = _read(os.path.join('core', 'database.py'))
    busy = src.index("conn.execute('PRAGMA busy_timeout")
    wal = src.index("conn.execute('PRAGMA journal_mode")
    assert busy < wal, 'busy_timeout 必须先于 journal_mode=WAL 设置'


# --------------------- config.py: MAX_CONTENT_LENGTH 非法值容错

# 注意:这里不用 fresh_import 重导入 core.config —— 同一会话内重导入同一子模块
# 两次以上时,monkeypatch 只恢复 sys.modules 条目,父包属性 core.config 仍指向
# 最后一次的新模块,后续测试 `from core import config` 与已缓存模块里的
# `from core.config import Config` 会拿到不同 Config(split-brain)。直接对
# 已导入模块调用解析函数即可覆盖同样的行为。

def test_max_content_length_invalid_falls_back_with_warning(monkeypatch, caplog):
    from core import config

    monkeypatch.setenv('MAX_CONTENT_LENGTH', 'not-a-number')
    with caplog.at_level(logging.WARNING, logger='core.config'):
        value = config._parse_max_content_length()
    assert value == config._DEFAULT_MAX_CONTENT_LENGTH
    assert 'MAX_CONTENT_LENGTH' in caplog.text, '报错必须带变量名'


def test_max_content_length_valid_env_still_honored(monkeypatch):
    from core import config

    monkeypatch.setenv('MAX_CONTENT_LENGTH', '12345')
    assert config._parse_max_content_length() == 12345


def test_max_content_length_config_uses_parser():
    """类属性必须经 _parse_max_content_length 求值(非法值容错才真的生效)。"""
    from core import config

    assert config.Config.MAX_CONTENT_LENGTH == config._parse_max_content_length()
    src = _read(os.path.join('core', 'config.py'))
    assert 'MAX_CONTENT_LENGTH = _parse_max_content_length()' in src


# --------------------- process_watchdog: PID 复用识别

import core.process_watchdog as pw  # noqa: E402

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


# --------------------- build.sh / build.bat: GDAL pin 缺失明确报错

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


def _build_sh_pin_check_segment():
    """截取 build.sh 中「读 pin + 缺失检查」一段单独执行(避开 uv install)。"""
    content = _read('build.sh')
    return 'REQUIRED_GDAL=' + content.split('REQUIRED_GDAL=', 1)[1].split(
        'SYSTEM_GDAL=', 1)[0]


@needs_bash
def test_build_sh_missing_gdal_pin_clear_error(tmp_path):
    (tmp_path / 'requirements.txt').write_text('flask\n', encoding='utf-8')
    proc = subprocess.run(
        ['bash', '-c', _build_sh_pin_check_segment()],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert 'requirements.txt 缺少 GDAL== pin' in proc.stdout + proc.stderr


@needs_bash
def test_build_sh_pin_present_passes_check(tmp_path):
    """回归保护:pin 存在时检查放行,且解析出版本号。"""
    (tmp_path / 'requirements.txt').write_text('GDAL==3.8.4\n', encoding='utf-8')
    proc = subprocess.run(
        ['bash', '-c', _build_sh_pin_check_segment() + '\necho "PIN:$REQUIRED_GDAL"'],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith('PIN:3.8.4')


def test_build_bat_missing_gdal_pin_clear_error():
    content = _read('build.bat')
    assert 'requirements.txt 缺少 GDAL== pin' in content, (
        'build.bat 必须在 GDAL== pin 缺失时给出明确报错'
    )
