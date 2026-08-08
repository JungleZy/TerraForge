"""单实例互斥锁（H3 生产侧）。

**为什么需要**：`create_app()` 在模块导入期就无条件执行两件破坏性操作 ——
`sweep_startup_residue()` 按【纯文件名前缀】rmtree 掉系统临时目录下所有
`map_dl_stitch_*` / `contour_warp_*`（没有 PID 归属、没有 mtime 门槛，分不清
「上次进程的残留」和「另一个活着的进程正在写的工作目录」），以及四条管线各自
的孤儿恢复（把 DB 里 running 的任务改判 paused/failed）。这一切都发生在
`socketio.run()` 绑 5000 端口**之前** —— 即使第二个实例最终因端口占用崩溃，
破坏也已经完成：第一个实例正在拼接的 GB 级中间产物被删，该 zoom 拼接失败；
它正在跑的任务被改判暂停。

`app.py` 的 docstring 自己就描述过后半个危害，但当时只对 multiprocessing
worker 加了守卫，没覆盖「用户双击第二个 exe」这个更常见的路径。

**锁的粒度是「数据目录」而不是「可执行文件」**：锁文件放在
`Config.DATABASE_PATH` 的同级目录。同一份 DB / downloads / cache 只允许一个
实例，这正是上述破坏的作用域；同时测试套件把 DATABASE_PATH 指到 tmp_path，
天然不会和用户正在运行的实例抢锁。

逃生阀：环境变量 `TERRAFORGE_ALLOW_MULTI_INSTANCE=1` 跳过整个机制。
"""

import errno
import logging
import os
from pathlib import Path

from src.core.config import Config

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".terraforge-instance.lock"

# 锁靠文件句柄的存活维持（POSIX flock / Windows LockFileEx 都随 fd 关闭而释放），
# 所以句柄必须活到进程退出 —— 用模块级变量持有，别让它被 GC。
_lock_handle = None


def _allow_multi_instance() -> bool:
    return os.environ.get("TERRAFORGE_ALLOW_MULTI_INSTANCE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def lock_path() -> Path:
    return Path(Config.DATABASE_PATH).resolve().parent / LOCK_FILENAME


def acquire_instance_lock() -> bool:
    """尝试取得本数据目录的单实例锁。

    Returns:
        True  —— 本进程持有锁（或机制被跳过 / 建不出锁文件 / 该文件系统给不了
                 锁时的宽容回退），可以安全执行清扫与孤儿恢复；
        False —— 同一数据目录已有实例在运行，调用方必须跳过所有破坏性初始化。

    同一进程内重复调用直接返回 True（已持锁），不会自相冲突。
    """
    global _lock_handle

    if _allow_multi_instance():
        logger.info("TERRAFORGE_ALLOW_MULTI_INSTANCE 已设置，跳过单实例检查")
        return True
    if _lock_handle is not None:
        return True

    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 必须是 r+b 而不是 a+：
        #   1) Windows 的 msvcrt.locking 锁的是【当前文件指针处】开始的 N 个
        #      字节，不是整个文件。用 a+ 打开时指针在末尾，第一个实例写完 pid
        #      后文件长度变了，第二个实例打开时指针落在 pid 之后 —— 两个进程
        #      锁的是不同字节区间，互斥完全失效（Windows CI 实测抓到：第二个
        #      进程拿到锁并返回 OK）。所以下面锁定前必须显式 seek(0)。
        #   2) a+ 模式下所有写入都强制追加到末尾，seek/truncate 不起作用，
        #      pid 会越写越长。
        if not path.exists():
            path.touch()
        handle = open(path, "r+b")
    except OSError as e:
        # 建不出锁文件（只读介质、权限等）不该阻断启动 —— 退化成 0.2.4 的行为。
        logger.warning(f"无法创建单实例锁文件，跳过实例检查: {e}")
        return True

    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)                       # 见上：必须锁在固定偏移
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        # 只有「锁被别人拿着」这几个 errno 才算冲突。flock/msvcrt 在数据目录位于
        # NFS/CIFS/部分 FUSE 上时会抛 ENOLCK/EINVAL/ENOTSUP —— 同样是 OSError,
        # 但含义是「这个文件系统给不了锁」。都当成冲突的话,网络盘上的数据目录
        # 会被「另一个实例正在运行」直接拒绝启动,而实际上一个实例都没有。
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            handle.close()
            return False
        handle.close()
        logger.warning(f"单实例锁不可用（{e}），跳过实例检查")
        return True
    except Exception as e:  # 平台不支持锁语义时同样宽容回退
        handle.close()
        logger.warning(f"单实例锁不可用，跳过实例检查: {e}")
        return True

    try:
        # pid 只是排查用的附带信息，写在被锁字节【之后】，免得和锁区间纠缠。
        handle.seek(1)
        handle.truncate(1)
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
    except OSError:
        pass  # 写 pid 失败不影响锁本身

    _lock_handle = handle
    return True


def release_instance_lock() -> None:
    """显式释放锁（正常退出路径用；进程被杀时由 OS 回收）。"""
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _lock_handle.seek(0)   # 与加锁时同一偏移，否则解不掉
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _lock_handle.close()
    finally:
        _lock_handle = None
