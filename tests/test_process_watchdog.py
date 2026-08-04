"""process_watchdog 测试 —— pid_alive 探测与看门狗的容错行为。"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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
    import threading
    monkeypatch.setenv(PARENT_PID_ENV, str(os.getpid()))  # 指向自己,永不触发退出
    start_parent_watchdog()
    names = [t.name for t in threading.enumerate()]
    assert 'reloader-parent-watchdog' in names
