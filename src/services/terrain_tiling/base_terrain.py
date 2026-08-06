"""随包分发的全球底图：解压到共享缓存，再植入任务目录。

**刻意不 import numpy / osgeo。** 这里全是文件操作，不 import 才能在没有
GDAL 的环境里单测 —— 与同目录的 layer_json.py 一个定位。

共享缓存放在 assets/terrain/base_z8，与分卷同目录：assets/ 是随包分发的数据，
downloads/ 是用户产出，解压出来的底图属于前者。打包模式下 Config.BASE_DIR
（sys.executable 所在目录）恒等于 bundle_dir()，正是 --include-data-dir 把
assets/terrain 放进去的地方，所以两种运行模式路径都对得上。
"""
from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from src.core.bundle import bundle_dir
from src.core.config import Config

logger = logging.getLogger(__name__)

PARTS_GLOB = "base_z8.tar.gz.part*"
BASE_DIR_NAME = "base_z8"

# 就位判据抽查这三层的顶层 x 目录数，而不是全量 walk：44k 个文件在 Windows 上
# 数一遍要几秒，而这个判据在每次切片开头都会跑。
_PROBE_LEVELS = ((0, 2), (4, 32), (7, 256))


def _assets_terrain_dir() -> Path:
    base = bundle_dir()
    root = Path(base) if base else Path(Config.BASE_DIR)
    return root / "assets" / "terrain"


def base_parts_dir() -> Path | None:
    """分卷所在目录；没有分卷返回 None（= 底图不可用，调用方退回 parentUrl）。"""
    d = _assets_terrain_dir()
    try:
        if any(d.glob(PARTS_GLOB)):
            return d
    except OSError:
        return None
    return None


def base_cache_dir() -> Path:
    """解压目标 = 分卷同目录下的 base_z8。"""
    return _assets_terrain_dir() / BASE_DIR_NAME


def is_base_ready(cache_dir: Path) -> bool:
    """已解压且看起来完整。

    只看 layer.json 存在是不够的：解压中途被打断也会留下它，而一个 layer.json
    齐全、瓦片残缺的底图会让 Cesium 拿到 404 瓦片 —— 它不报错，而是塞一个假的
    heightmap-1.0 图层并污染共享 builder，连任务自己的 quantized-mesh 瓦片都
    被按 heightmap 解析（v0.2.8 实测：4154 m 山峰解成 -744 m，零报错）。
    """
    cache_dir = Path(cache_dir)
    if not (cache_dir / "layer.json").is_file():
        return False
    for z, expect_x in _PROBE_LEVELS:
        d = cache_dir / str(z)
        try:
            if not d.is_dir() or sum(1 for _ in d.iterdir()) < expect_x:
                return False
        except OSError:
            return False
    return True


class _CacheLock:
    """缓存目录的跨进程**阻塞**锁。

    ⚠️ 不能直接用 src/core/single_instance.py:acquire_instance_lock —— 那个是
    非阻塞的（LK_NBLCK / LOCK_NB，抢不到返回 False），语义是「已经有人在跑就
    退出」。这里要的正相反：两个任务同时切片时，后到的**等**第一个解压完，
    而不是判定底图不可用。

    但平台分支的写法要照抄它：Windows 的 msvcrt.locking 锁的是**当前文件指针
    处**开始的 N 个字节而不是整个文件，加锁解锁都必须先 seek(0)。v0.2.5 就是
    在这上面栽的 —— 两个实例锁到不同字节区间，互斥完全失效，Windows CI 实测
    抓到。
    """

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+b")
        self._fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        return False


def _emit(stage_cb, fraction: float) -> None:
    """进度上报，吞掉回调自己的异常。

    一次 emit 故障（客户端断开等）不该让解压失败 —— 与
    cesiumlab_terrain._gdal_stage_callback 同一条约定。
    """
    if stage_cb is None:
        return
    try:
        stage_cb("base_unpack", max(0.0, min(1.0, float(fraction))))
    except Exception:
        pass


def _extract_stream(parts: list[Path], dest: Path, total_bytes: int, stage_cb) -> None:
    """按字母序流式拼接分卷并解压到 dest。

    不先拼成完整的 tar.gz 再解：那要额外落一份 167 MB。流式读的代价是分卷缺失
    要解到一半才发现，但分卷是随包分发的、要么齐要么整个目录被删（那种情况
    base_parts_dir 已经返回 None 了）。
    """
    import gzip
    import io

    read = 0

    class _Cat(io.RawIOBase):
        def __init__(self):
            self._it = iter(parts)
            self._fh = None

        def readable(self):
            return True

        def readinto(self, b):
            nonlocal read
            while True:
                if self._fh is None:
                    try:
                        self._fh = open(next(self._it), "rb")
                    except StopIteration:
                        return 0
                n = self._fh.readinto(b)
                if n:
                    read += n
                    if total_bytes:
                        _emit(stage_cb, read / total_bytes)
                    return n
                self._fh.close()
                self._fh = None

    with gzip.open(_Cat(), "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tf:
        # filter="data" 是显式的：3.12 起不传就是 DeprecationWarning，3.14 起默认
        # 就是它。底图是我们自己打的包，没有绝对路径 / 软链 / 设备节点，"data"
        # 的限制全都满足。
        tf.extractall(dest, filter="data")


def ensure_base_unpacked(cache_dir: Path | None = None, stage_cb=None) -> Path | None:
    """幂等解压随包底图。已就位直接返回；分卷缺失返回 None；解压失败抛。

    解到临时目录再原子改名：中途被打断时，最终位置上不会出现一个「layer.json
    齐全但瓦片残缺」的目录 —— 那种半成品会被 is_base_ready 之外的任何粗判据
    认成就位，然后让 Cesium 拿 404。
    """
    cache_dir = Path(cache_dir) if cache_dir is not None else base_cache_dir()
    if is_base_ready(cache_dir):
        return cache_dir

    parts_dir = base_parts_dir()
    if parts_dir is None:
        logger.info("Terrain: 未找到随包底图分卷，跳过底图植入（退回 parentUrl 级联）")
        return None

    parts = sorted(parts_dir.glob(PARTS_GLOB))
    total = sum(p.stat().st_size for p in parts)

    with _CacheLock(cache_dir.parent / ".base_unpack.lock"):
        # 拿到锁之后重查一次：等锁期间别的进程可能已经解完了。
        if is_base_ready(cache_dir):
            return cache_dir

        logger.info(f"Terrain: 解压随包底图 {total / 1048576:.0f} MB "
                    f"→ {cache_dir}（4.3 万个小文件，Windows 上可能要几分钟）")
        tmp = Path(tempfile.mkdtemp(prefix=".base_unpack_", dir=str(cache_dir.parent)))
        try:
            _extract_stream(parts, tmp, total, stage_cb)
            extracted = tmp / BASE_DIR_NAME
            if not is_base_ready(extracted):
                raise RuntimeError(f"解压产物不完整：{extracted}")
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
            os.replace(extracted, cache_dir)
        except BaseException as e:
            raise RuntimeError(f"随包底图解压失败：{e}") from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    _emit(stage_cb, 1.0)
    logger.info(f"Terrain: 底图就位 {cache_dir}")
    return cache_dir
