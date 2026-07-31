"""reloader 子进程看门狗 —— watcher 父进程被强杀时自动退出。

Werkzeug 的 reloader 用 subprocess 起子进程;父进程被 SIGKILL / 直接关终端等
强杀时,子进程变成孤儿继续占着监听端口(下次启动报 Address already in use),
werkzeug 自己不处理这种情况。这里在子进程里起守护线程轮询父进程是否存活,
父进程没了就退出。

父进程 pid 通过环境变量 TF_RELOADER_PARENT_PID 传递:父进程在 socketio.run
之前写入,werkzeug fork 子进程时自然继承。
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

PARENT_PID_ENV = 'TF_RELOADER_PARENT_PID'


def pid_alive(pid):
    """跨平台判断进程是否存在。

    注意 Windows 上不能用 os.kill(pid, 0) 探测 —— 那真的会终止进程。
    """
    if os.name == 'nt':
        import ctypes
        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属于别的用户
    return True


def _read_proc_cmdline(pid):
    """Linux 下读 /proc/<pid>/cmdline,用于识别 PID 复用;读不到返回 None。

    Windows / 无 /proc 的平台恒为 None —— 调用方据此跳过身份校验,退回纯
    pid_alive 探活(保持原行为)。
    """
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read()
    except OSError:
        return None


def start_parent_watchdog(interval=2.0):
    """启动看门狗线程:父进程(reloader watcher)死亡时本进程退出。

    仅在 reloader 子进程里调用;父进程 pid 从 PARENT_PID_ENV 读取,缺失或
    非法时不做任何事。
    """
    try:
        parent_pid = int(os.environ.get(PARENT_PID_ENV, '0'))
    except ValueError:
        return
    if parent_pid <= 0:
        return

    # PID 复用缓解(Linux):父进程死后其 pid 可能被无关进程复用,纯 pid_alive
    # 探活会误判父进程还活着、孤儿子进程永远占着端口。启动时(父进程必活)记下
    # 它的 cmdline,之后每轮比对 —— pid 易主后 cmdline 必然不同,视为父进程已死。
    # 父进程自己的 cmdline 运行中不会变,无误判风险。Windows 无 /proc,跳过该校验。
    parent_cmdline = _read_proc_cmdline(parent_pid)

    def _watch():
        while True:
            if not pid_alive(parent_pid) or (
                parent_cmdline is not None
                and _read_proc_cmdline(parent_pid) != parent_cmdline
            ):
                logger.warning(
                    "reloader watcher 父进程已消失,孤儿子进程退出(避免残留占用端口)")
                os._exit(0)
            time.sleep(interval)

    threading.Thread(
        target=_watch, daemon=True, name='reloader-parent-watchdog'
    ).start()
