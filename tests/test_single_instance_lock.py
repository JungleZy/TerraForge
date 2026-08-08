"""单实例互斥锁（H3 生产侧）。

`create_app()` 在模块导入期就跑 sweep_startup_residue() 与四条管线的孤儿恢复，
两者都是破坏性的，且都发生在 socketio.run() 绑 5000 端口【之前】—— 第二个实例
即使最终因端口占用崩溃，也已经 rmtree 掉第一个实例正在写的 GB 级拼接/warp
工作目录，并把它正在 running 的任务改判成 paused。端口占用救不了，必须有真正
的进程级互斥。
"""

import errno
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.core import single_instance  # noqa: E402


@pytest.fixture
def lock_db(monkeypatch, tmp_path):
    """把锁指到 tmp_path 下的数据目录，并保证用例结束时释放。"""
    from src.core import config
    db = tmp_path / "data" / "map_downloader.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setattr(config.Config, "DATABASE_PATH", db)
    monkeypatch.delenv("TERRAFORGE_ALLOW_MULTI_INSTANCE", raising=False)
    # 本进程可能已在别处（conftest 的 isolated_app）持有过锁
    single_instance.release_instance_lock()
    yield db
    single_instance.release_instance_lock()


def _acquire_in_subprocess(db_path, env_extra=None):
    """在另一个真实进程里尝试取同一把锁，返回 'OK' / 'BLOCKED'。"""
    code = (
        "import sys; sys.path.insert(0, {root!r});"
        "from src.core.config import Config;"
        "Config.DATABASE_PATH = {db!r};"
        "from src.core.single_instance import acquire_instance_lock;"
        "print('OK' if acquire_instance_lock() else 'BLOCKED')"
    ).format(root=PROJECT_ROOT, db=str(db_path))
    env = dict(os.environ)
    env.pop("TERRAFORGE_ALLOW_MULTI_INSTANCE", None)
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_lock_path_follows_the_data_directory(lock_db):
    """锁的粒度是数据目录 —— 破坏的作用域正是 DB/downloads/cache 这一份数据。

    这也让测试套件天然不会和用户正在运行的实例抢锁（测试把 DATABASE_PATH
    指到 tmp_path）。
    """
    assert single_instance.lock_path().parent == lock_db.parent


def test_second_process_is_blocked_while_first_holds_the_lock(lock_db):
    assert single_instance.acquire_instance_lock() is True
    assert _acquire_in_subprocess(lock_db) == "BLOCKED"


def test_lock_is_available_again_after_release(lock_db):
    assert single_instance.acquire_instance_lock() is True
    single_instance.release_instance_lock()
    assert _acquire_in_subprocess(lock_db) == "OK"


def test_same_process_can_reacquire(lock_db):
    """同一进程内重复调用不能自锁（create_app 可能被再次调用）。"""
    assert single_instance.acquire_instance_lock() is True
    assert single_instance.acquire_instance_lock() is True


def test_escape_hatch_env_var_skips_the_check(lock_db):
    assert single_instance.acquire_instance_lock() is True
    assert _acquire_in_subprocess(
        lock_db, {"TERRAFORGE_ALLOW_MULTI_INSTANCE": "1"}) == "OK"


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows 的 open() 不带 FILE_SHARE_DELETE，unlink 会直接失败")
def test_deleting_the_lock_file_defeats_the_mutex(lock_db):
    """删锁文件**不会**解锁,只会让第二个实例也拿到锁 —— 两个实例同时在跑。

    锁锁的是「已打开句柄对应的 inode」而不是路径:unlink 之后下一次
    acquire_instance_lock() 走 `path.touch()` 建出【新 inode】再锁它,于是双方都
    认为自己持锁,而第二个实例的 sweep_startup_residue() 会 rmtree 掉第一个实例
    正在写的 GB 级拼接/warp 工作目录。

    本用例钉住的是这个平台事实,它是 `_enforce_single_instance()` 的提示语**不得**
    建议用户删锁文件的唯一理由(见 test_lock_conflict_message_never_tells_the_user_to_delete_it)。
    哪天这条断言变红(比如换成基于路径的锁),那句提示才可以放宽。
    """
    assert single_instance.acquire_instance_lock() is True
    assert _acquire_in_subprocess(lock_db) == "BLOCKED"

    single_instance.lock_path().unlink()

    assert _acquire_in_subprocess(lock_db) == "OK", (
        "如果这里变成 BLOCKED，说明锁改成了按路径判定 —— "
        "app_factory 的提示语可以重新讨论了")


def test_unwritable_lock_location_falls_back_to_allowing_startup(monkeypatch, tmp_path):
    """建不出锁文件（只读介质、权限）不该阻断启动 —— 退化成 0.2.4 的行为。"""
    from src.core import config
    single_instance.release_instance_lock()
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "d" / "x.db")
    monkeypatch.delenv("TERRAFORGE_ALLOW_MULTI_INSTANCE", raising=False)

    def _boom(*a, **k):
        raise PermissionError("read-only file system")

    monkeypatch.setattr("builtins.open", _boom)
    assert single_instance.acquire_instance_lock() is True
    assert single_instance._lock_handle is None, "回退路径不该留下半开的句柄"


# ---------------------------------------------------------------------------
# 网络盘上的数据目录：flock 抛的不是「有人持锁」
# ---------------------------------------------------------------------------
#
# 旧实现把 `except OSError` 整块当成「已有实例在运行」并 return False。
# NFS/CIFS/部分 FUSE 上 flock 会抛 ENOLCK/EINVAL/ENOTSUP —— 同一个异常类型，
# 含义却是「这个文件系统给不了锁」。于是数据目录放在网络盘上的用户，一个实例
# 都没有也起不来，提示还是「另一个实例正在运行」。
# 下面两条必须成对：只断言 ENOLCK 放行是空的（把 except 整块删掉也会绿），
# 必须同时钉住 EAGAIN 仍然 False，否则等于把互斥机制本身拆了。


@pytest.mark.skipif(os.name == "nt", reason="fcntl 分支仅 POSIX")
@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EINVAL, errno.ENOTSUP])
def test_filesystem_without_lock_support_falls_back_to_allowing_startup(
        lock_db, monkeypatch, caplog, code):
    """文件系统给不了锁 → 宽容放行（并留下日志），不是「已有实例」。"""
    import fcntl

    def _no_locks(fd, op):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fcntl, "flock", _no_locks)
    with caplog.at_level("WARNING"):
        assert single_instance.acquire_instance_lock() is True
    assert single_instance._lock_handle is None, "宽容回退不该留下半开的句柄"
    assert "跳过实例检查" in caplog.text, "静默放行等于让用户无从判断锁是否生效"


@pytest.mark.skipif(os.name == "nt", reason="fcntl 分支仅 POSIX")
@pytest.mark.parametrize("code", [errno.EAGAIN, errno.EACCES, errno.EDEADLK])
def test_contention_errnos_still_block(lock_db, monkeypatch, code):
    """对照：真·抢不到锁的 errno 必须仍然 return False。"""
    import fcntl

    def _held(fd, op):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fcntl, "flock", _held)
    assert single_instance.acquire_instance_lock() is False
    assert single_instance._lock_handle is None