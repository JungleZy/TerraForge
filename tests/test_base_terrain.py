"""随包底图的解压与植入 —— 纯文件操作，不碰 GDAL。

⚠️ 本文件所有用例必须写在 tmp_path 里。往仓库的 assets/terrain/ 写东西会被
CI 打进产物：流水线里测试跑在 Nuitka 打包之前。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _make_base(root, layers=((0, 2), (1, 4), (2, 8), (3, 16),
                             (4, 32), (5, 64), (6, 128), (7, 256)),
               dense=False):
    """造一个 base_z8 骨架。dense=False 时每层只建 x 目录不建瓦片（够就位判据用）。

    layers 里第二个数是**该层的 x 目录数，不是瓦片数**。EPSG:4326 是 2:1 地理
    网格：z 层有 2·2^z 个 x 目录，每个 x 目录下 2^z 个 y 文件，所以 z7 是 256 个
    目录 / 32768 个瓦片，全 8 层合计 510 个目录 / 43,690 个瓦片。

    照上面这串真实目录数建出来的骨架，恰好卡在 _PROBE_LEVELS 的边界上（z0/z4/z7
    要求 ≥2/32/256），少一个目录就该红 —— 这正是这些用例的护栏价值。若误用瓦片数
    当目录数，会建出多达 128 倍的目录，阈值被远远超过，边界断言全部失效（还会把
    单文件耗时从毫秒拖到 11 s）。
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")
    for z, nx in layers:
        for x in range(nx):
            d = root / str(z) / str(x)
            d.mkdir(parents=True, exist_ok=True)
            if dense:
                (d / "0.terrain").write_bytes(b"\x1f\x8bfake")
    return root


def _make_parts(parts_dir, base_root, part_size=1 << 16):
    """把 base_root 打成 tar.gz 再切成两卷，放进 parts_dir。

    tar 里的顶层目录必须叫 base_z8 —— 解压逻辑按这个名字核对。
    """
    import io
    import tarfile

    parts_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(base_root), arcname="base_z8")
    data = buf.getvalue()
    half = max(1, len(data) // 2)
    (parts_dir / "base_z8.tar.gz.partaa").write_bytes(data[:half])
    (parts_dir / "base_z8.tar.gz.partab").write_bytes(data[half:])
    return parts_dir


def test_is_base_ready_requires_layer_json_and_all_probe_levels(tmp_path):
    """就位判据：layer.json + z0/z4/z7 的 x 目录数都够。

    只看 layer.json 不够 —— 解压中途被打断也会留下它，而一个 layer.json 齐全
    但瓦片残缺的底图会让 Cesium 拿到 404 瓦片，进而塞假 heightmap 图层污染
    共享 builder（v0.2.8 修过这条链）。
    """
    from src.services.terrain_tiling.base_terrain import is_base_ready

    good = _make_base(tmp_path / "good")
    assert is_base_ready(good) is True

    # layer.json 缺失
    no_lj = _make_base(tmp_path / "no_lj")
    (no_lj / "layer.json").unlink()
    assert is_base_ready(no_lj) is False

    # z7 只解到一半
    half = _make_base(tmp_path / "half", layers=((0, 2), (4, 32), (7, 128)))
    assert is_base_ready(half) is False

    # z0 差一个目录，其余层齐全 —— 单独钉住 z0 这条探针
    z0_short = _make_base(tmp_path / "z0_short",
                          layers=((0, 1), (1, 4), (2, 8), (3, 16),
                                  (4, 32), (5, 64), (6, 128), (7, 256)))
    assert is_base_ready(z0_short) is False

    # z4 差一个目录，其余层齐全 —— 单独钉住 z4 这条探针
    z4_short = _make_base(tmp_path / "z4_short",
                          layers=((0, 2), (1, 4), (2, 8), (3, 16),
                                  (4, 31), (5, 64), (6, 128), (7, 256)))
    assert is_base_ready(z4_short) is False

    # 目录压根不存在
    assert is_base_ready(tmp_path / "nope") is False


def test_base_parts_dir_returns_none_without_parts(tmp_path, monkeypatch):
    """分卷不在（有人删了 assets/）时返回 None —— 调用方据此退回 parentUrl 兜底。"""
    from src.core import config as config_mod
    from src.services.terrain_tiling import base_terrain

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(base_terrain, "bundle_dir", lambda: None)
    assert base_terrain.base_parts_dir() is None

    parts = tmp_path / "assets" / "terrain"
    parts.mkdir(parents=True)
    (parts / "base_z8.tar.gz.partaa").write_bytes(b"x")
    assert base_terrain.base_parts_dir() == parts


def test_base_cache_dir_sits_next_to_the_parts(tmp_path, monkeypatch):
    """缓存与分卷同目录：assets/ 是随包数据，downloads/ 是用户产出。"""
    from src.core import config as config_mod
    from src.services.terrain_tiling import base_terrain

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(base_terrain, "bundle_dir", lambda: None)
    assert base_terrain.base_cache_dir() == tmp_path / "assets" / "terrain" / "base_z8"



def test_ensure_base_unpacked_extracts_and_is_idempotent(tmp_path, monkeypatch):
    """首次解压落地；第二次直接返回，不重复解压、也不去抢锁。"""
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    cache = base_terrain.ensure_base_unpacked()
    assert cache == parts / "base_z8"
    assert base_terrain.is_base_ready(cache)

    calls = []
    real_open = base_terrain.tarfile.open
    monkeypatch.setattr(base_terrain.tarfile, "open",
                        lambda *a, **k: (calls.append(1), real_open(*a, **k))[1])
    assert base_terrain.ensure_base_unpacked() == cache
    assert calls == [], "已就位时不该再解压一次"

    # 已就位的快路径必须在**加锁之前**短路：否则 N 个并发切片任务每次开工都要
    # 排队过同一把锁，而它们本来完全不需要互斥。锁文件是否被重建就是探针。
    lock_file = parts / ".base_unpack.lock"
    lock_file.unlink()
    assert base_terrain.ensure_base_unpacked() == cache
    assert not lock_file.exists(), "已就位时不该走加锁路径"


def test_ensure_base_unpacked_returns_none_without_parts(tmp_path, monkeypatch):
    """分卷缺失 → None，让调用方退回 parentUrl 兜底而不是报错。"""
    from src.services.terrain_tiling import base_terrain

    empty = tmp_path / "assets" / "terrain"
    empty.mkdir(parents=True)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: empty)
    assert base_terrain.ensure_base_unpacked() is None


def test_interrupted_extraction_leaves_nothing_that_looks_ready(tmp_path, monkeypatch):
    """解压中途炸掉，不得在最终位置留下「看着像就位」的半成品。

    解到临时目录再原子改名就是为了这个：半个 base 比没有更糟 —— 缺的瓦片会让
    Cesium 拿到 404，进而整个 provider 降级成 heightmap。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def boom(parts_, dest, *a, **k):
        # 半成品必须「看着像就位」才有护栏价值：layer.json + 510 个 x 目录齐全、
        # 瓦片一个没有 —— 正是 is_base_ready 会放行、Cesium 会拿 404 的那种。
        # 若 boom 什么都不写，「解到临时目录再改名」被删掉这条用例也照样绿。
        _make_base(dest / "base_z8")
        raise OSError("simulated disk full")

    monkeypatch.setattr(base_terrain, "_extract_stream", boom)

    with pytest.raises(RuntimeError):
        base_terrain.ensure_base_unpacked()
    assert not base_terrain.is_base_ready(parts / "base_z8")


def test_stage_cb_is_reported_and_never_kills_the_unpack(tmp_path, monkeypatch):
    """进度回调要报，且回调自己抛异常不能把解压带崩。

    对齐 cesiumlab_terrain._gdal_stage_callback 的既有约定：一次 emit 故障
    （客户端断开等）不该让整个任务失败。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    seen = []

    def cb(phase, frac):
        seen.append((phase, frac))
        raise RuntimeError("emit failed")

    cache = base_terrain.ensure_base_unpacked(stage_cb=cb)
    assert base_terrain.is_base_ready(cache)
    assert seen and all(p == "base_unpack" for p, _ in seen)
    assert all(0.0 <= f <= 1.0 for _, f in seen)


def test_recheck_after_lock_skips_a_concurrently_finished_unpack(tmp_path, monkeypatch):
    """等锁期间别人解完了 —— 拿到锁后必须重查，不能再解一遍。

    两个任务同时开始切片时，后到的那个阻塞在 _CacheLock 上；等它拿到锁，底图
    已经就位。少了这次重查就是白解 4.3 万个文件，还要先 rmtree 掉别人刚放好的
    目录 —— 而那一刻第一个任务可能正在从里面读瓦片。

    这里用「进锁时底图变就位」来模拟并发赢家，而不是真起两个进程：真并发要靠
    sleep 卡时序，在 CI 上必然间歇性红。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    class _RivalWinsWhileWeWait(base_terrain._CacheLock):
        def __enter__(self):
            super().__enter__()
            _make_base(parts / "base_z8")   # 另一个进程刚放好的成品
            return self

    monkeypatch.setattr(base_terrain, "_CacheLock", _RivalWinsWhileWeWait)
    monkeypatch.setattr(base_terrain, "_extract_stream",
                        lambda *a, **k: pytest.fail("拿到锁后必须重查 is_base_ready，不该再解压"))

    assert base_terrain.ensure_base_unpacked() == parts / "base_z8"


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows 走 msvcrt.locking 分支，flock 探针不适用")
def test_cache_lock_is_exclusive_and_released(tmp_path):
    """锁必须是**独占**的，且出了 with 真的放开。

    POSIX 的 flock 绑在「打开文件描述」而不是进程上，所以同进程内另开一个 fd
    去抢锁就能确定性地验出互斥 —— 不起子进程、不靠 sleep，在 CI 上不会间歇性红。

    探针故意用 LOCK_SH 而不是 LOCK_EX：共享锁同样挡得住 LOCK_EX，拿 LOCK_EX
    当探针就分不出实现加的到底是独占还是共享（实测：把 LOCK_EX 改成 LOCK_SH
    这条用例照样绿）。只有 LOCK_SH 也抢不到，才说明实现拿的是独占锁。

    lock 必须用变量显式持有：写成 `with _CacheLock(...)` 时实例出了 with 就没
    引用了，CPython 会立刻 GC 掉它、顺带关 fd 把锁放了 —— 释放那半段就白测了
    （实测：删掉 __exit__ 里的 LOCK_UN + close 照样绿）。

    锁没释放比没上锁更糟：第二个切片任务会永久卡死在 ensure_base_unpacked 里。
    """
    import fcntl

    from src.services.terrain_tiling.base_terrain import _CacheLock

    lock_file = tmp_path / ".base_unpack.lock"
    lock = _CacheLock(lock_file)
    with lock:
        with open(lock_file, "a+b") as rival:
            with pytest.raises(BlockingIOError):
                fcntl.flock(rival.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)

    with open(lock_file, "a+b") as rival:
        fcntl.flock(rival.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(rival.fileno(), fcntl.LOCK_UN)


def test_cache_lock_closes_its_handle_when_acquire_fails(tmp_path, monkeypatch):
    """`__enter__` 里失败必须自己把句柄关掉。

    `__enter__` 抛异常时 with 语句还没成立，`__exit__` 不会被调用 —— 不在那里关
    就是每次失败漏一个 fd，进程不退不回收。
    """
    from src.services.terrain_tiling import base_terrain

    def denied(self):
        raise OSError("lock unavailable")

    monkeypatch.setattr(base_terrain._CacheLock, "_acquire", denied)
    lock = base_terrain._CacheLock(tmp_path / ".base_unpack.lock")

    with pytest.raises(OSError):
        lock.__enter__()
    assert lock._fh is None, "加锁失败后句柄没关，fd 泄漏"


# 子进程脚本：等锁，拿到就打印等了多久。必须是真子进程 —— 同一进程内
# msvcrt.locking 对自己已持有的字节区间可重入，测不出跨进程互斥。
_CHILD_WAIT_FOR_LOCK = '''\
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from src.services.terrain_tiling.base_terrain import _CacheLock

started = time.monotonic()
with _CacheLock(Path(sys.argv[2])):
    print(f"ACQUIRED {time.monotonic() - started:.2f}", flush=True)
'''


@pytest.mark.skipif(os.name != "nt",
                    reason="只在 Windows 上有意义：POSIX 分支由上面那条 flock 探针覆盖")
def test_cache_lock_makes_a_second_process_wait_on_windows(tmp_path):
    """Windows 上第二个进程必须**等**，而不是报错退出。

    这是本模块最核心的需求在主力平台上的唯一护栏。`msvcrt.LK_LOCK` 听起来像阻塞
    锁，实际是「每隔 1 秒重试、10 次之后抛 OSError」—— 最多等约 10 秒就崩。而解压
    4.3 万个小文件在 Windows 上要几分钟，所以用 LK_LOCK 的话，两个并发切片任务里
    后到的那个必然失败。

    父进程故意持锁 13 s（> LK_LOCK 的 ~10 s 上限）：持锁时间短于 10 s 的话
    LK_LOCK 也能在超时前拿到锁，这个 bug 就漏了。代价是这条用例在 Windows CI 上
    要跑 13 s —— 换本模块唯一的跨进程互斥证据，值。
    """
    import subprocess

    from src.services.terrain_tiling.base_terrain import _CacheLock

    hold = 13.0
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = tmp_path / "wait_for_lock.py"
    script.write_text(_CHILD_WAIT_FOR_LOCK, encoding="utf-8")
    lock_file = tmp_path / ".base_unpack.lock"

    lock = _CacheLock(lock_file)
    with lock:
        child = subprocess.Popen(
            [sys.executable, str(script), repo_root, str(lock_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            child.wait(timeout=hold)
        except subprocess.TimeoutExpired:
            pass   # 期望路径：它还在等
        assert child.poll() is None, (
            "子进程在父进程持锁期间就返回了 —— 要么没上锁，要么等待有上限崩了："
            + child.communicate()[1])

    out, err = child.communicate(timeout=60)
    assert child.returncode == 0, f"子进程应当等到锁而不是报错：{err}"
    waited = float(out.split("ACQUIRED", 1)[1].split()[0])
    assert waited >= hold - 1.0, f"子进程没有真的等（{waited:.2f}s < {hold}s）"


def test_tmp_dir_is_named_and_placed_for_the_startup_sweep(tmp_path, monkeypatch):
    """临时目录必须叫 `.base_unpack_<pid>_*`，且落在 assets/terrain。

    finally 盖不住 SIGKILL / 关窗 / 断电，残留只能靠下次启动清扫
    （task_cleanup.sweep_startup_residue 第 6 类）。前缀不对或落点不对，残留就
    永久留在 assets/terrain —— 那是 Nuitka 的 --include-data-dir 源目录，会被打进
    三个平台的发布产物，且没有任何其他路径会回收它。pid 是唯一可靠的归属判据
    （与 cesiumlab_terrain_<pid>_* 同一套约定）。
    """
    from src.services import task_cleanup
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    seen = []
    real = base_terrain._extract_stream
    monkeypatch.setattr(base_terrain, "_extract_stream",
                        lambda p, dest, t, cb: (seen.append(dest), real(p, dest, t, cb))[1])

    base_terrain.ensure_base_unpacked()

    assert len(seen) == 1
    tmp = seen[0]
    assert tmp.parent == parts, "临时目录必须落在启动清扫扫得到的 assets/terrain"
    assert tmp.name.startswith(f"{task_cleanup._BASE_UNPACK_PREFIX}{os.getpid()}_"), tmp.name


def test_keyboard_interrupt_is_not_disguised_as_a_failed_unpack(tmp_path, monkeypatch):
    """Ctrl-C 必须原样往上冒，不能被包成 RuntimeError。

    包成 RuntimeError 会让上层把「用户按了 Ctrl-C」误判成「随包底图坏了」。
    顺带钉住：临时目录的清理不依赖捕获 BaseException —— finally 无条件执行。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(base_terrain, "_extract_stream", interrupt)

    with pytest.raises(KeyboardInterrupt):
        base_terrain.ensure_base_unpacked()

    leftovers = [p.name for p in parts.iterdir()
                 if p.name.startswith(base_terrain.UNPACK_TMP_PREFIX)]
    assert leftovers == [], f"Ctrl-C 之后临时目录没清掉：{leftovers}"


def _stub_replace(monkeypatch, base_terrain, behaviour):
    """把 os.replace 换成打桩版并记录调用；顺带让退避不真的 sleep。

    返回 (calls, slept)：calls 是每次调用的 (src, dst)，slept 是每次退避的秒数。
    behaviour(n) 在第 n 次调用（从 1 起）时被调，返回 True 表示「这次放行、
    走真正的 os.replace」，抛异常表示「这次这样失败」。
    """
    real_replace = base_terrain.os.replace
    calls = []
    slept = []

    def stub(src, dst, **k):
        calls.append((src, dst))
        behaviour(len(calls))
        return real_replace(src, dst, **k)

    monkeypatch.setattr(base_terrain.os, "replace", stub)
    monkeypatch.setattr(base_terrain.time, "sleep", slept.append)
    return calls, slept


def test_replace_retries_a_transient_permission_denied(tmp_path, monkeypatch, caplog):
    """最后那步改名撞上瞬时占用 → 退避重试，最终成功。

    真机验收实测：解压跑满 19 分钟之后 `os.replace` 抛 `[Errno 13] Permission
    denied`，19 分钟全部白费、临时目录被 finally 清掉、底图永远装不上。判定它
    是瞬时锁而不是文件系统限制靠两条探针：空目录 rename 成功；**同一个 4.3 万
    文件的真实底图目录**在空闲时做同样的跨目录 rename 也成功，耗时 0.14 秒。
    同源同目标同文件系统，空闲 0.14 s 就过、刚写完那一刻却失败 —— 只可能是刚
    落盘时目标仍被 Windows 侧的 Defender / 索引器持着句柄（现场是 WSL2 的
    /mnt/d，9p + drvfs）。

    三条断言各钉一件事，缺一条这个 bug 就能溜回来：
    ① 整体成功且底图真就位 —— 光「不抛异常」不算数，改名没落地也不抛；
    ② 退避间隔递增 —— 等间隔重试撑不到杀软扫完一批文件的窗口；
    ③ 每次重试都有 warning —— 卡住时这是唯一的诊断线索。
    """
    import logging

    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def flaky(n):
        if n <= 2:
            raise PermissionError(13, "Permission denied")

    calls, slept = _stub_replace(monkeypatch, base_terrain, flaky)

    with caplog.at_level(logging.WARNING, logger=base_terrain.__name__):
        cache = base_terrain.ensure_base_unpacked()

    assert cache == parts / "base_z8"
    assert base_terrain.is_base_ready(cache), "改名没真的落地"
    assert len(calls) == 3, f"重试次数不对：{len(calls)}"
    # ⚠️ 这里必须拿**字面量**比，不能写成 `slept == [FIRST_WAIT, FIRST_WAIT *
    # BACKOFF]`：那样断言会跟着常量一起变，把 BACKOFF 改成 1.0（退化成等间隔）
    # 也照样绿 —— 实测过，这条曾经就是那么写的假守卫。
    assert len(slept) == 2, f"重试了 {len(calls)} 次却退避了 {len(slept)} 次"
    assert slept[0] == pytest.approx(0.5), f"首次重试等太久，长尾都在等它：{slept}"
    assert slept[1] > slept[0], f"退避间隔没有递增：{slept}"

    warns = [r.getMessage() for r in caplog.records
             if r.levelno == logging.WARNING and r.name == base_terrain.__name__]
    assert len(warns) == 2, f"每次重试都该留一条 warning：{warns}"
    assert all("Permission denied" in m for m in warns), warns
    assert "1/6" in warns[0] and "2/6" in warns[1], warns


def test_replace_does_not_retry_a_permanent_error(tmp_path, monkeypatch):
    """磁盘满（ENOSPC）不是瞬时占用 → 立刻抛 RuntimeError，一次都不重试。

    重试只对「这一刻被别人占着」有意义。把 ENOSPC 也纳入重试，换来的是每次
    磁盘满都白等满一轮退避，而结果一模一样 —— 打桩计数就是这条边界的证据：
    `len(calls) == 1` 且完全没 sleep 过。
    """
    import errno

    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def disk_full(n):
        raise OSError(errno.ENOSPC, "No space left on device")

    calls, slept = _stub_replace(monkeypatch, base_terrain, disk_full)

    with pytest.raises(RuntimeError):
        base_terrain.ensure_base_unpacked()

    assert len(calls) == 1, f"不可重试的错误被重试了 {len(calls)} 次"
    assert slept == [], f"不可重试的错误还退避了：{slept}"
    assert not base_terrain.is_base_ready(parts / "base_z8")


@pytest.mark.parametrize("errno_name", ["EACCES", "EPERM", "EBUSY"])
def test_every_whitelisted_errno_is_actually_retried(tmp_path, monkeypatch, errno_name):
    """白名单里的**每一个** errno 都要真的走重试，一个都不许是摆设。

    上面那条只喂 EACCES=13（现场在 WSL2 抓到的那个），于是 `EPERM` 与 `EBUSY`
    从来没被任何测试碰过 —— 实测把它俩从 `_REPLACE_RETRYABLE_ERRNOS` 里删掉，
    整个文件 31 条全绿。而代码注释专门论证过「EBUSY 是给 Windows 留的余量：
    那边的『目标正被占用』不保证一律映射成 EACCES」，论证了却没人守，等于没有。

    参数化而不是循环：哪一个错误码退化了，测试名直接点出来。
    """
    import errno as errno_mod

    from src.services.terrain_tiling import base_terrain

    code = getattr(errno_mod, errno_name)
    assert code in base_terrain._REPLACE_RETRYABLE_ERRNOS, (
        f"{errno_name} 不在重试白名单里 —— 要么是被误删了，要么是本测试的名单过期了")

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def flaky(n):
        if n == 1:
            raise OSError(code, f"stubbed {errno_name}")

    calls, slept = _stub_replace(monkeypatch, base_terrain, flaky)

    cache = base_terrain.ensure_base_unpacked()

    assert cache == parts / "base_z8"
    assert base_terrain.is_base_ready(cache), f"{errno_name} 重试后改名没真的落地"
    assert len(calls) == 2, (
        f"{errno_name} 没有被重试（调用 {len(calls)} 次）—— 它在白名单里却不生效")
    assert len(slept) == 1, f"{errno_name} 重试了却没退避：{slept}"


def test_replace_gives_up_after_the_budget_and_keeps_the_runtime_error_contract(
        tmp_path, monkeypatch):
    """一直被占着 → 用尽预算后仍抛 RuntimeError（契约不变），且预算是有限的。

    契约那一半是给调用方的：dem_task_tiler 与 base_terrain_warmup 都写的
    `except RuntimeError`，重试把原始 PermissionError 漏出去就整类接不住。
    预算那一半是给用户的：这一步真坏了的时候不能无限等 —— 总等待钉成常量算出
    来的值（0.5+1+2+4+8 = 15.5 s），改动常量时这条会红，逼人重新想清楚上下界。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def always_locked(n):
        raise PermissionError(13, "Permission denied")

    calls, slept = _stub_replace(monkeypatch, base_terrain, always_locked)

    with pytest.raises(RuntimeError):
        base_terrain.ensure_base_unpacked()

    assert len(calls) == base_terrain._REPLACE_RETRY_ATTEMPTS
    assert len(slept) == base_terrain._REPLACE_RETRY_ATTEMPTS - 1, \
        "最后一次失败之后不该再白等一轮"
    assert sum(slept) == pytest.approx(15.5), f"总等待预算变了：{sum(slept)} s"


def test_unwritable_assets_dir_raises_runtime_error_not_bare_oserror(tmp_path, monkeypatch):
    """加锁与建临时目录也在「失败抛 RuntimeError」这条契约里。

    assets/ 在打包安装场景下经常不可写（Program Files 权限、只读介质），
    `open(锁文件)` 和 `mkdtemp` 抛的都是裸 OSError。这两步留在 try 之外，调用方
    按 docstring 写的 `except RuntimeError` 就会整类漏掉，任务崩在没人接的
    PermissionError 上。
    """
    from src.services.terrain_tiling import base_terrain

    src = _make_base(tmp_path / "src", dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as m:
        m.setattr(base_terrain._CacheLock, "__enter__", denied)
        with pytest.raises(RuntimeError):
            base_terrain.ensure_base_unpacked()

    with monkeypatch.context() as m:
        m.setattr(base_terrain.tempfile, "mkdtemp", denied)
        with pytest.raises(RuntimeError):
            base_terrain.ensure_base_unpacked()


# ----------------------------------------------------------------- 植入任务目录


def _link_unsupported(*a, **k):
    """模拟跨盘 / 目标盘不支持硬链接。EXDEV 是 DEM 任务的常态：输出路径由用户
    自选，底图缓存在 assets/ 下，两者不同盘的概率比同盘还高。"""
    raise OSError(18, "Invalid cross-device link")


def test_graft_prefers_hardlinks_and_walks_in_a_reproducible_order(tmp_path, monkeypatch):
    """同盘走硬链接（磁盘只占一份），并且按名字排序遍历。

    顺序不是可有可无的细节：磁盘满时植入到一半，留下的前缀必须可复现，否则
    同一批数据每次留下不同的半成品，排障对不上，回滚测试也只能靠运气。
    夹具铺到 z3（16 个 x 目录）正是为了让「排序」与「创建顺序」分叉
    ——  sorted 是 0,1,10,11,…,2,3，创建是 0,1,2,…,15 —— readdir 恰好等于
    排序的巧合杀不掉这条断言。
    """
    from src.services.terrain_tiling import base_terrain

    layers = ((0, 2), (1, 4), (2, 8), (3, 16))
    base = _make_base(tmp_path / "base", layers=layers, dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    order = []
    real_link = base_terrain.os.link

    def spy(src, dst, **k):
        order.append(os.path.relpath(str(src), str(base)).replace(os.sep, "/"))
        return real_link(src, dst, **k)

    monkeypatch.setattr(base_terrain.os, "link", spy)

    got = base_terrain.graft_base_into(tiles, base)

    assert got == {"linked": 30, "copied": 0, "skipped": 0}
    src = base / "0" / "0" / "0.terrain"
    dst = tiles / "0" / "0" / "0.terrain"
    assert dst.is_file()
    assert os.stat(src).st_ino == os.stat(dst).st_ino, "同盘应当是硬链接"

    assert order[0] == "layer.json", "第一次 link 是探针，探针只探一次"
    assert order[1:] == [f"{z}/{x}/0.terrain"
                         for z, nx in layers
                         for x in sorted(str(i) for i in range(nx))]
    assert list(tiles.glob(".hardlink_probe*")) == [], "探针文件没清掉"


def test_graft_falls_back_to_copy_when_link_unsupported(tmp_path, monkeypatch):
    """跨盘 / 文件系统不支持硬链接时回退实体复制，内容必须逐字节一致。

    DEM 任务的输出路径是用户自选的全盘路径，跨盘是常态不是例外 —— 这条分支
    要当主路径测。

    `len(calls) == 1` 钉的是「策略一次性决定」：探针失败之后不许再对 4.3 万个
    瓦片逐个 try —— 同一个目标目录里策略不会中途改变，而逐个 try 的异常开销在
    Windows 上是可测量的。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    calls = []

    def no_link(src, dst, **k):
        calls.append(str(dst))
        _link_unsupported()

    monkeypatch.setattr(base_terrain.os, "link", no_link)

    got = base_terrain.graft_base_into(tiles, base)

    assert got == {"linked": 0, "copied": 6, "skipped": 0}
    src = base / "0" / "0" / "0.terrain"
    dst = tiles / "0" / "0" / "0.terrain"
    assert dst.read_bytes() == src.read_bytes()
    assert os.stat(src).st_ino != os.stat(dst).st_ino
    assert len(calls) == 1, f"探针之外还在逐文件试链：{calls}"


@pytest.mark.parametrize("strategy", ["link", "copy"])
def test_graft_never_overwrites_task_tiles(tmp_path, monkeypatch, strategy):
    """任务已经写过的瓦片胜出，两条策略分支都要守住。

    正常情况零冲突（任务 z8+、底图 z0-7），这条是为了兜住 maxzoom < 8 的退化
    任务 —— 那时两边在同一层相撞，任务的 DEM 数据必须赢。复制分支单独测是因为
    它的失效方式和链接分支不同：os.link 撞名会抛 FileExistsError（吵闹地坏），
    shutil.copy2 则**静默覆盖**（安静地坏），少了这一半就漏掉更危险的那一半。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2),), dense=True)
    tiles = tmp_path / "tiles"
    (tiles / "0" / "0").mkdir(parents=True)
    (tiles / "0" / "0" / "0.terrain").write_bytes(b"TASK-OWN")

    if strategy == "copy":
        monkeypatch.setattr(base_terrain.os, "link", _link_unsupported)

    got = base_terrain.graft_base_into(tiles, base)

    assert got["skipped"] == 1
    assert got["linked"] + got["copied"] == 1, "另一个 x 目录该照常植入"
    assert (tiles / "0" / "0" / "0.terrain").read_bytes() == b"TASK-OWN"


def test_graft_never_carries_the_base_layer_json_over(tmp_path):
    """底图的 layer.json 不许搬 —— 它由 Task 4 的合成函数处理。

    直接搬过去会盖掉任务自己的 layer.json（available 范围、maxzoom 全是任务
    的），Cesium 拿着底图的元数据去请求任务瓦片，请求的层级根本不存在。
    第一段（目标目录里本来没有 layer.json）才是真正的守卫：如果实现只是靠
    skip-if-exists 兜着，第二段照样绿，第一段会红。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into

    base = _make_base(tmp_path / "base", layers=((0, 2),), dense=True)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    graft_base_into(fresh, base)
    assert not (fresh / "layer.json").exists(), "底图的 layer.json 被搬过来了"

    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "layer.json").write_bytes(b'{"maxzoom": 12}')
    graft_base_into(owned, base)
    assert (owned / "layer.json").read_bytes() == b'{"maxzoom": 12}'


def test_graft_rolls_back_only_what_it_wrote_on_failure(tmp_path, monkeypatch):
    """植入中途失败 → 抛出，且**只**清掉本次写进去的文件。

    半个底图比没有更糟：缺的瓦片让 Cesium 拿 404，它不报错而是塞一个假的
    heightmap-1.0 图层并污染共享 builder，连任务自己的 quantized-mesh 瓦片都
    被按 heightmap 解析（v0.2.8 实测：4154 m 山峰解成 -744 m，控制台零报错）。

    任务自己的瓦片有两类，回滚一类都不能带走：
    ① `0/0/0.terrain` 与底图撞名，被 skip —— 它压根不属于本次植入；
    ② `9/1/1.terrain` 不撞名 —— 回滚不许整目录清场。
    ① 一定排在失败点之前，靠的是实现按名字排序遍历（见上面那条顺序用例）。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    (tiles / "0" / "0").mkdir(parents=True)
    (tiles / "0" / "0" / "0.terrain").write_bytes(b"TASK-OWN")
    (tiles / "9" / "1").mkdir(parents=True)
    (tiles / "9" / "1" / "1.terrain").write_bytes(b"TASK-OWN")

    n = {"i": 0}
    real_link = base_terrain.os.link

    def flaky(src, dst, **k):
        # 只数瓦片，不数探针 —— 免得这条用例依赖探针有没有走 os.link
        if str(dst).endswith(".terrain"):
            n["i"] += 1
            if n["i"] > 2:
                raise OSError(28, "No space left on device")
        return real_link(src, dst, **k)

    monkeypatch.setattr(base_terrain.os, "link", flaky)

    with pytest.raises(OSError):
        base_terrain.graft_base_into(tiles, base)

    leftover = [p for p in tiles.rglob("*.terrain")
                if p.read_bytes() != b"TASK-OWN"]
    assert leftover == [], f"回滚不干净：{leftover}"
    assert (tiles / "9" / "1" / "1.terrain").read_bytes() == b"TASK-OWN"
    assert (tiles / "0" / "0" / "0.terrain").read_bytes() == b"TASK-OWN", \
        "被 skip 的瓦片是任务自己的，不属于本次植入"


def test_graft_rolls_back_a_half_written_copy(tmp_path, monkeypatch):
    """复制分支写到一半炸掉，那个截断的半成品也必须被回滚带走。

    `os.link` 是原子的，`shutil.copy2` 不是：copyfile 先 `open(dst,'wb')` 建好并
    截断目标再灌数据，ENOSPC 打在写的中途就会在目标位置留下一个长度不对的
    `.terrain`。而复制是这份代码自己认定的**常态分支**（跨盘），ENOSPC 又正是它
    最可能的死法。

    截断比缺失更毒：缺失是 404（已知的 heightmap 污染链），截断是 200 + 解析炸。
    更要命的是它会**永久粘住** —— 回滚不删空目录，重试时 `dst_dir.is_dir()` 与
    `dst.exists()` 双双为真，这个残片会被当成「任务自己的瓦片」skip 掉，此后
    每次重试都绕过它。所以末尾那次重试断言不是锦上添花，它才是「粘住」的证据。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()
    monkeypatch.setattr(base_terrain.os, "link", _link_unsupported)

    n = {"i": 0}
    real_copy = base_terrain.shutil.copy2

    def half_written(src, dst, **k):
        n["i"] += 1
        if n["i"] <= 2:
            return real_copy(src, dst, **k)
        # 复刻 shutil.copyfile 的现场：目标已经建好并写进去一截，然后才抛
        with open(dst, "wb") as fh:
            fh.write(b"\x1f")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(base_terrain.shutil, "copy2", half_written)

    with pytest.raises(OSError):
        base_terrain.graft_base_into(tiles, base)

    leftover = sorted(str(p.relative_to(tiles)) for p in tiles.rglob("*.terrain"))
    assert leftover == [], f"截断的半成品粘在目标目录里：{leftover}"

    monkeypatch.setattr(base_terrain.shutil, "copy2", real_copy)
    assert base_terrain.graft_base_into(tiles, base) == \
        {"linked": 0, "copied": 6, "skipped": 0}, "重试把残片当成任务自己的瓦片跳过了"


def test_graft_refuses_a_base_dir_that_has_no_layer_json(tmp_path):
    """底图目录不在 / layer.json 缺失 → 抛，不许返回一个全零的「植入成功」。

    静默成功是这里最坏的结果：Task 5 认为底图已就位，Task 4 接着合成一份宣告
    z0-7 available 的 layer.json，Cesium 对着根本不存在的瓦片拿 404 —— 正是这个
    Task 存在的理由本身。

    顺带堵掉探针的降级洞：探针源就是 layer.json，它缺失时 `os.link` 抛
    FileNotFoundError（⊂ OSError）→ 探针返回 False → 整批 167 MB 静默走实体复制，
    慢一个量级还占双份磁盘，没有任何断言兜得住。入口校验一次两个洞一起堵。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into

    tiles = tmp_path / "tiles"

    with pytest.raises(OSError):
        graft_base_into(tiles, tmp_path / "nope")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(OSError):
        graft_base_into(tiles, empty)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="靠 chmod 造读取失败：Windows 权限语义不同，root 无视权限位")
def test_graft_fails_loudly_when_a_subtree_cannot_be_read(tmp_path):
    """遍历中途读不了某棵子树 → 抛并回滚，不许静默跳过后报「植入完成」。

    `os.walk` 的 `onerror` 默认是 None：scandir 失败时它直接 continue，整棵子树
    被无声吞掉。那样就得到「半个底图 + 成功返回」，而「宁可整批撤掉」这条设计
    前提直接失效 —— 缺的那半正好落进 Cesium 的 404 → 假 heightmap 图层链。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    blocked = base / "1" / "0"
    blocked.chmod(0o000)
    try:
        with pytest.raises(OSError):
            base_terrain.graft_base_into(tiles, base)
    finally:
        blocked.chmod(0o755)

    leftover = sorted(str(p.relative_to(tiles)) for p in tiles.rglob("*.terrain"))
    assert leftover == [], f"子树读不了还留下了半个底图：{leftover}"



# ---------------------------------------------------------------------------
# 摘链：切片写穿共享缓存的防线
# ---------------------------------------------------------------------------


def test_ungraft_unlinks_shared_inodes_and_keeps_task_own_files(tmp_path):
    """只摘与底图共享 inode 的那些，任务自己的文件一个都不许带走。

    摘掉的判据必须是 (st_dev, st_ino) 而不是「底图里有同名文件」：maxzoom < 8 的
    退化任务重跑时，上一轮任务自己在 z0-7 写的瓦片与底图**同名**，按名字摘就会
    把任务数据删掉，而 skip-if-exists 随后用底图瓦片把那个位置填上 —— 任务的
    DEM 数据被底图静默顶替。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into, ungraft_base_from

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    stats = graft_base_into(tiles, base)
    assert stats["linked"] > 0, "本用例要求硬链接生效，否则测不出共享 inode"

    # 与底图同名、但是任务自己写的（独立 inode）：不能被摘
    same_name = tiles / "1" / "0" / "0.terrain"
    same_name.unlink()
    same_name.write_bytes(b"task-own-same-name")
    # 底图里没有的名字：不能被摘
    (tiles / "8" / "1").mkdir(parents=True)
    (tiles / "8" / "1" / "2.terrain").write_bytes(b"task-z8")
    (tiles / "layer.json").write_text('{"maxzoom": 5}', encoding="utf-8")

    removed = ungraft_base_from(tiles, base)

    assert removed == stats["linked"] - 1, "摘掉的个数不等于剩下的硬链接数"
    assert not (tiles / "0" / "0" / "0.terrain").exists(), "共享 inode 的硬链接没摘掉"
    assert same_name.read_bytes() == b"task-own-same-name", "按名字摘掉了任务自己的瓦片"
    assert (tiles / "8" / "1" / "2.terrain").read_bytes() == b"task-z8"
    assert (tiles / "layer.json").is_file(), "顶层 layer.json 是任务的，不许碰"


def test_ungraft_makes_tiling_writes_stop_hitting_the_shared_cache(tmp_path):
    """摘链之后再落盘，共享缓存里那份必须原样不动。

    cesiumlab_terrain._worker_tile 落盘用 `(out/f"{y}.terrain").write_bytes(blob)`，
    而 `Path.write_bytes` = `open(path,'wb')` + write —— **就地截断同一个 inode**，
    不是先删再建。硬链接下这一笔直接改写共享缓存 assets/terrain/base_z8 里的那份
    文件：全局底图被静默污染，影响之后所有任务，且没有任何信号。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into, ungraft_base_from

    base = _make_base(tmp_path / "base", layers=((5, 2),), dense=True)
    cached = base / "5" / "0" / "0.terrain"
    tiles = tmp_path / "tiles"
    assert graft_base_into(tiles, base)["linked"] > 0

    ungraft_base_from(tiles, base)
    (tiles / "5" / "0" / "0.terrain").write_bytes(b"TASK")

    assert cached.read_bytes() == b"\x1f\x8bfake", "任务瓦片写穿到了共享底图缓存"


def test_ungraft_prunes_whole_levels_that_were_never_grafted(tmp_path, monkeypatch):
    """首次切片（out_dir 里还没有 z0-7）不许抛，也不许去遍历底图那 43k 个文件。

    这条路径每次切片开头都会走，而底图整棵树有 510 个目录 / 43,690 个文件。
    目标里整个层级目录都不存在时，它下面的每一个文件都必然不存在，必须在
    进 os.walk 之前就整层剪掉。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 4)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    def no_walk(*a, **k):
        raise AssertionError("目标层级一个都不存在，还是遍历了整棵底图")

    monkeypatch.setattr(base_terrain.os, "walk", no_walk)
    assert base_terrain.ungraft_base_from(tiles, base) == 0


def test_ungraft_tolerates_a_missing_tiles_dir(tmp_path):
    """out_dir 还没建出来（全新任务）→ 返回 0，不抛。"""
    from src.services.terrain_tiling.base_terrain import ungraft_base_from

    base = _make_base(tmp_path / "base", layers=((0, 2),), dense=True)
    assert ungraft_base_from(tmp_path / "nope", base) == 0


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="靠 chmod 造删除失败：Windows 权限语义不同，root 无视权限位")
def test_ungraft_fails_loudly_when_a_link_cannot_be_removed(tmp_path):
    """摘不掉就必须抛，不许吞掉继续。

    吞掉 = 那个硬链接留在原地，紧接着的 build_terrain 一笔 write_bytes 就把共享
    缓存改写了 —— 正是这个函数存在的全部理由。宁可让任务当场失败。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into, ungraft_base_from

    base = _make_base(tmp_path / "base", layers=((0, 2),), dense=True)
    tiles = tmp_path / "tiles"
    assert graft_base_into(tiles, base)["linked"] > 0

    locked = tiles / "0" / "0"
    locked.chmod(0o555)  # 目录不可写 → 里面的文件删不掉
    try:
        with pytest.raises(OSError):
            ungraft_base_from(tiles, base)
    finally:
        locked.chmod(0o755)


# ---------------------------------------------------------------------------
# 端到端：解压 → 植入 → 合成之后，任务目录能脱离本程序独立当地形源用
# ---------------------------------------------------------------------------


def test_grafted_dir_is_self_contained(tmp_path, monkeypatch):
    """「拷走还能不能用」的三个必要条件一次钉住。

    上面每条用例只覆盖链条的一环，绿的组合仍可能拼不出一个能用的目录 —— 比如
    植入成功但 layer.json 仍写着 parentUrl（拷到别的机器上那个 localhost 必然
    404，而 Cesium 的 404 处理会把整个 provider 降级成 heightmap），或者
    available 没并到 z0（文件在目录里，Cesium 却根本不去请求根瓦片）。

    只造 z0-z2 三层并把 _PROBE_LEVELS 一起缩到同样三层：这条用例验的是链条串起来
    对不对，不是就位判据的边界（那条边界由
    test_is_base_ready_requires_layer_json_and_all_probe_levels 用真实的 510 个
    目录钉着）。层数与 x 目录数仍取真实网格值（z0:2 / z1:4 / z2:8），免得读的人
    以为可以随便填。
    """
    import json

    from src.services.terrain_tiling import base_terrain
    from src.services.terrain_tiling.layer_json import merge_base_availability

    levels = ((0, 2), (1, 4), (2, 8))
    src = _make_base(tmp_path / "src", layers=levels, dense=True)
    # 真实底图的 layer.json 带逐层的全球 available；_make_base 的默认骨架写的是
    # 空数组（那些用例只关心就位判据，不关心声明）。合成要验的正是这份声明被并进
    # 任务的 layer.json，所以这里必须摆上真数据。每层一个覆盖全球的矩形：
    # EPSG:4326 下 z 层的 x 是 0..2·2^z-1、y 是 0..2^z-1。
    (src / "layer.json").write_text(json.dumps({
        "minzoom": 0, "maxzoom": 2,
        "available": [[{"startX": 0, "startY": 0,
                        "endX": 2 * 2 ** z - 1, "endY": 2 ** z - 1}]
                      for z, _ in levels],
    }), encoding="utf-8")
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)
    monkeypatch.setattr(base_terrain, "_PROBE_LEVELS", levels)

    base = base_terrain.ensure_base_unpacked()
    assert base is not None

    tiles = tmp_path / "task" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text(
        json.dumps({"minzoom": 0, "maxzoom": 9, "available": [[]] * 9 + [
            [{"startX": 1, "startY": 1, "endX": 1, "endY": 1}]],
            "parentUrl": "http://localhost:5000/terrain/base"}),
        encoding="utf-8")
    (tiles / "9" / "1").mkdir(parents=True)
    (tiles / "9" / "1" / "1.terrain").write_bytes(b"\x1f\x8btask")

    base_terrain.graft_base_into(tiles, base)
    merge_base_availability(tiles / "layer.json", base / "layer.json")

    for z, nx in levels:
        for x in range(nx):
            assert (tiles / str(z) / str(x) / "0.terrain").is_file(), \
                f"z{z}/{x} 没植入 —— 拷走后这一块就是空的"
    assert (tiles / "9" / "1" / "1.terrain").read_bytes() == b"\x1f\x8btask", \
        "任务自己的瓦片被底图顶替了"

    data = json.loads((tiles / "layer.json").read_text(encoding="utf-8"))
    assert "parentUrl" not in data, "自包含之后 parentUrl 是一次 404 的邀请"
    assert data["minzoom"] == 0
    assert data["maxzoom"] == 9
    # available 是按绝对层号对齐的密集数组：下标 i 就是 z(minzoom + i)
    assert len(data["available"]) == 10
    assert data["available"][0], "z0 没被声明，Cesium 不会去请求根瓦片"
    assert data["available"][9] == [{"startX": 1, "startY": 1, "endX": 1, "endY": 1}]