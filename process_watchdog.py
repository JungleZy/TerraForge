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

    def _watch():
        while True:
            if not pid_alive(parent_pid):
                logger.warning(
                    "reloader watcher 父进程已消失,孤儿子进程退出(避免残留占用端口)")
                os._exit(0)
            time.sleep(interval)

    threading.Thread(
        target=_watch, daemon=True, name='reloader-parent-watchdog'
    ).start()
