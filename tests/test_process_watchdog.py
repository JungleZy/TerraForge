"""process_watchdog 测试 —— pid_alive 探测与看门狗的容错行为。"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.core import process_watchdog as pw
from src.core.process_watchdog import PARENT_PID_ENV, pid_alive, start_parent_watchdog


def test_pid_alive_current_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_dead_process():
    # 起个子进程再杀掉,用它的 pid 验证"不存在"分支
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    assert pid_alive(proc.pid) is True
    proc.kill()
    proc.wait()
    # 等待系统回收,避免僵尸态导致误判(Windows 上句柄可能仍查询得到)
    deadline = time.time() + 5
    while pid_alive(proc.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert pid_alive(proc.pid) is False


def test_watchdog_noop_without_env(monkeypatch):
    monkeypatch.delenv(PARENT_PID_ENV, raising=False)
    start_parent_watchdog()  # 不抛异常、不起线程


def test_watchdog_noop_with_invalid_env(monkeypatch):
    monkeypatch.setenv(PARENT_PID_ENV, 'not-a-number')
    start_parent_watchdog()
    monkeypatch.setenv(PARENT_PID_ENV, '-1')
    start_parent_watchdog()


def test_watchdog_starts_daemon_thread(monkeypatch):
    """起了线程就要收尾：用例结束前必须把它停进假的 os._exit 里。

    看门狗线程是 daemon + 无限循环，循环体里调的是 `os._exit(0)`。它一旦活过
    本用例，monkeypatch 就把**真的** `os._exit` 还了回去，此后任何让探活或身份
    校验读不到东西的用例（打桩 open、fd 耗尽、/proc 读失败）都会让这条遗留线程
    把整个 pytest 进程以**退出码 0** 杀掉：输出停在半行、没有汇总行、CI 判绿。

    这不是假想:v0.3.3 首次发版构建的 test-build 就是这样 —— 套件跑到 77% 静默
    中止,「Run test suite」一步却是绿的,而同一个 commit 在 build.yml 里跑完全程
    并报出了一条真实的失败。一条能把红套件变绿的用例比它测的那个功能危险得多。
    """
    alive = {'value': True}
    monkeypatch.setattr(pw, 'pid_alive', lambda pid: alive['value'])

    parked = threading.Event()

    def fake_exit(code):
        parked.set()
        threading.Event().wait()  # 线程停在这里,永不回到循环去调真的 os._exit

    monkeypatch.setattr(os, '_exit', fake_exit)
    monkeypatch.setenv(PARENT_PID_ENV, str(os.getpid()))

    start_parent_watchdog(interval=0.01)
    assert 'reloader-parent-watchdog' in [t.name for t in threading.enumerate()]

    # 探活转假,把线程赶进 fake_exit 停住。改 pid_alive 而不是改 cmdline:
    # macOS / Windows 没有 /proc,身份校验那条路在那两个平台上根本不走。
    alive['value'] = False
    assert parked.wait(5), '看门狗线程没停下来,它会带着真的 os._exit 活到会话结束'
