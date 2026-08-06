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