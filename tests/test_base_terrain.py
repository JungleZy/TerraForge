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
