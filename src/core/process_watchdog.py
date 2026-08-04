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

    Windows / 无 /proc 的平台恒为 None —— 那里改用进程创建时间做身份校验
    (见 _process_create_time)。
    """
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read()
    except OSError:
        return None


def _process_create_time(pid):
    """进程创建时间（Windows 专用身份指纹）；取不到返回 None。

    U12：PID 复用防护此前只在 Linux 生效（靠 /proc/<pid>/cmdline），Windows 上
    退化成纯 pid_alive 探活 —— 而 Windows 的 PID 回收比 Linux 激进得多，父进程
    死后其 pid 被无关进程复用时，看门狗会一直以为父进程还活着，孤儿子进程永远
    占着 5000 端口（下次启动报 Address already in use）。

    「创建时间 + PID」才唯一标识一个进程，这是 Windows 上的标准做法。
    GetProcessTimes 的创建时间是 100ns 精度的 FILETIME，进程存活期间恒定。

    触发面有限但真实：start_parent_watchdog 只在 reloader 子进程里跑，而打包
    exe 默认 DEBUG=0 不开 reloader —— 实际影响的是「Windows + 源码运行」的
    开发场景。
    """
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel_time), ctypes.byref(user_time))
            if not ok:
                return None
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
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
    # Windows 侧的等价指纹（U12）：进程创建时间。两者都取不到时才退回纯探活。
    parent_create_time = _process_create_time(parent_pid)

    def _identity_changed():
        if parent_cmdline is not None:
            return _read_proc_cmdline(parent_pid) != parent_cmdline
        if parent_create_time is not None:
            return _process_create_time(parent_pid) != parent_create_time
        return False

    def _watch():
        while True:
            if not pid_alive(parent_pid) or _identity_changed():
                logger.warning(
                    "reloader watcher 父进程已消失,孤儿子进程退出(避免残留占用端口)")
                os._exit(0)
            time.sleep(interval)

    threading.Thread(
        target=_watch, daemon=True, name='reloader-parent-watchdog'
    ).start()
