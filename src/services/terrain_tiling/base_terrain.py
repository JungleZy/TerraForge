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
import time
from pathlib import Path

from src.core.bundle import bundle_dir
from src.core.config import Config

logger = logging.getLogger(__name__)

PARTS_GLOB = "base_z8.tar.gz.part*"
BASE_DIR_NAME = "base_z8"

# 解压临时目录的前缀，名字里带 pid。**必须与 task_cleanup 的启动清扫登记保持
# 一致**（见 sweep_startup_residue 的第 6 类）：finally 盖不住 SIGKILL / 关窗 /
# 断电，而落点 assets/terrain 既不在系统临时目录、也不在 downloads 下，前五类
# 清扫一条都扫不到 —— 残留最多 167 MB / 4.3 万个文件，并且这个目录会被 Nuitka
# 打进三个平台的发布产物，没有任何其他路径会回收它。
UNPACK_TMP_PREFIX = ".base_unpack_"

# Windows 上等锁的轮询间隔与「等太久了」的告警门槛（见 _CacheLock._acquire）。
_LOCK_POLL_SECONDS = 0.5
_LOCK_SLOW_WARN_SECONDS = 30.0

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
        try:
            self._acquire()
        except BaseException:
            # __enter__ 里抛异常时 with 语句根本没成立，__exit__ 不会被调用 ——
            # 不在这里关掉句柄就是 fd 泄漏（每次失败漏一个，进程不退不回收）。
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            raise
        return self

    def _acquire(self) -> None:
        """一直等到拿到独占锁。两个平台的等待语义必须一致 —— 不引入平台差异。"""
        self._fh.seek(0)                     # 见类 docstring：必须锁在固定偏移
        if os.name != "nt":
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)   # 无限等待
            return

        # Windows 没有「无限等待」的原语。msvcrt.LK_LOCK 听起来像阻塞锁，实际是
        # 「每隔 1 秒重试、10 次之后抛 OSError」—— 也就是**最多等约 10 秒然后崩**。
        # 而解压 4.3 万个小文件在 Windows 上要几分钟，所以用 LK_LOCK 的话，两个
        # 并发切片任务里后到的那个必然在 10 秒后失败，本模块最核心的需求在主力
        # 平台上直接不成立。因此自己拿 LK_NBLCK 轮询，不设次数上限。
        import msvcrt

        started = time.monotonic()
        warned = False
        while True:
            try:
                self._fh.seek(0)             # 每轮都要：重试之间指针可能被动过
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                waited = time.monotonic() - started
                if not warned and waited >= _LOCK_SLOW_WARN_SECONDS:
                    # 不打这条日志的话，卡住时无从诊断：外面看起来就是任务不动。
                    warned = True
                    logger.info(
                        f"Terrain: 已等待另一个进程解压底图 {waited:.0f} s"
                        f"（锁文件 {self._path}），继续等")
                time.sleep(_LOCK_POLL_SECONDS)

    def __exit__(self, *exc):
        if self._fh is None:
            return False
        try:
            self._fh.seek(0)                 # 与加锁时同一偏移，否则解不掉
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

    「解压失败抛 RuntimeError」这条契约覆盖**加锁与建临时目录**在内的整段：
    assets/ 在打包安装场景下经常不可写（Program Files 权限、只读介质），锁文件
    open 和 mkdtemp 都会抛裸 OSError。这两步要是留在 try 之外，调用方按契约写的
    `except RuntimeError` 就会整类漏掉。
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

    tmp = None
    try:
        with _CacheLock(cache_dir.parent / ".base_unpack.lock"):
            # 拿到锁之后重查一次：等锁期间别的进程可能已经解完了。
            if is_base_ready(cache_dir):
                return cache_dir

            logger.info(f"Terrain: 解压随包底图 {total / 1048576:.0f} MB "
                        f"→ {cache_dir}（4.3 万个小文件，Windows 上可能要几分钟）")
            # 目录名里带 pid：启动清扫要靠它分清「上次进程的残留」和「另一个活着
            # 的进程正在写的目录」（与 cesiumlab_terrain_<pid>_* 同一套约定）。
            tmp = Path(tempfile.mkdtemp(
                prefix=f"{UNPACK_TMP_PREFIX}{os.getpid()}_", dir=str(cache_dir.parent)))
            _extract_stream(parts, tmp, total, stage_cb)
            extracted = tmp / BASE_DIR_NAME
            if not is_base_ready(extracted):
                raise RuntimeError(f"解压产物不完整：{extracted}")
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
            os.replace(extracted, cache_dir)
    except Exception as e:
        # 只捕 Exception，不捕 BaseException：Ctrl-C 必须原样往上冒，把
        # KeyboardInterrupt 伪装成「解压失败」会让上层误判成底图坏了。临时目录的
        # 清理不依赖这里 —— 下面的 finally 无条件执行。
        raise RuntimeError(f"随包底图解压失败：{e}") from e
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)

    _emit(stage_cb, 1.0)
    logger.info(f"Terrain: 底图就位 {cache_dir}")
    return cache_dir


def _probe_hardlink(base_dir: Path, tiles_dir: Path) -> bool:
    """在目标目录里试链一次，成败决定整批策略。

    **不做 4.3 万次逐个 try**：源和目标各自固定在一个文件系统上，能不能硬链接
    在同一个目标目录里不会中途改变，探一次就够了；而 Windows 上抛/接一次异常
    是微秒级开销，乘以 4.3 万个瓦片就是可测量的一截。

    探针源用 layer.json：它是底图里唯一被 is_base_ready 强制要求存在的文件。
    硬链接只要求对源可读，所以 assets/ 只读也照样能探。
    """
    probe_dst = tiles_dir / ".hardlink_probe"
    try:
        if probe_dst.exists():
            probe_dst.unlink()
        os.link(base_dir / "layer.json", probe_dst)
    except OSError:
        return False
    finally:
        # 探针文件必须收走：它会跟着任务产出一起打包/分发出去。
        try:
            probe_dst.unlink()
        except OSError:
            pass
    return True


def _walk_error(err: OSError) -> None:
    """`os.walk` 的错误回调 —— 唯一的作用是把错误重新抛出来。

    默认 `onerror=None` 时 scandir 失败会被直接 continue 掉，整棵子树静默消失，
    植入照样返回一个「成功」的计数。那正是最坏的结果：调用方以为底图就位，
    合成出来的 layer.json 宣告 z0-7 都 available，Cesium 对着不存在的瓦片拿 404。
    宁可整批抛出去回滚。
    """
    raise err


def graft_base_into(tiles_dir: Path, base_dir: Path) -> dict[str, int]:
    """把底图植入任务瓦片目录，返回 {linked, copied, skipped}。

    skip-if-exists：任务已经写过的瓦片不被覆盖。正常情况零冲突（任务 z8+、
    底图 z0-7），这条规则兜的是 maxzoom < 8 的退化任务 —— 那时两边在同一层
    相撞，任务自己的 DEM 数据必须赢。

    失败即失败并回滚：留半个底图比没有更糟 —— 缺的瓦片让 Cesium 拿 404，它不
    报错，而是塞一个假的 heightmap-1.0 图层并污染共享 builder，连任务自己的
    quantized-mesh 瓦片都被按 heightmap 解析（v0.2.8 实测：4154 m 山峰解成
    -744 m，控制台零报错）。所以宁可整批撤掉、退回 parentUrl 级联。

    **前提：植入必须发生在任务自己切片完成之后。** 冲突判断（`dst_dir.is_dir()`
    的目录级快照 + 逐文件 `exists()`）是遍历时读一次的，与切片并发跑的话，先被
    判成「目录不存在、必然无冲突」的那一批就绕过了 skip-if-exists，随后任务写下
    的瓦片可能撞上已植入的底图瓦片。串行调用就没有这个问题。
    """
    tiles_dir = Path(tiles_dir)
    base_dir = Path(base_dir)
    # 底图目录不对就当场抛，别返回一个全零的「成功」：调用方会据此认为底图已
    # 就位。layer.json 同时也是探针源，它缺失时探针会静默降级成整批实体复制
    # （167 MB、慢一个量级、占双份磁盘），这一次校验把两个洞一起堵上。
    if not (base_dir / "layer.json").is_file():
        raise FileNotFoundError(2, "底图目录缺少 layer.json", str(base_dir))
    tiles_dir.mkdir(parents=True, exist_ok=True)

    use_link = _probe_hardlink(base_dir, tiles_dir)
    stats = {"linked": 0, "copied": 0, "skipped": 0}
    written: list[Path] = []
    base_str = str(base_dir)

    try:
        # 用 os.walk 而不是 rglob：一次拿一整个目录的名字，不为 4.3 万个瓦片各
        # 造一个 Path，目录级的准备工作（mkdir / 冲突判断）也只做 518 次。
        for dirpath, dirnames, filenames in os.walk(base_str, onerror=_walk_error):
            # 排序遍历：磁盘满时植入到一半，留下的前缀每次都一样，排障能对得上。
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, base_str)
            at_root = rel_dir == os.curdir
            dst_dir = tiles_dir if at_root else tiles_dir / rel_dir
            # 目标目录整个不存在 → 里面每个文件都必然是新的，逐文件 exists() 可以
            # 全省掉。正常情况零冲突，这一下把 4.3 万次 stat 压到 518 次 is_dir()。
            may_collide = dst_dir.is_dir()
            dst_ready = may_collide
            for name in filenames:
                # 顶层 layer.json 由 merge_layer_json 合成；直接搬会拿底图的
                # maxzoom / available 盖掉任务自己的，Cesium 会去请求不存在的层级。
                if at_root and name == "layer.json":
                    continue
                dst = dst_dir / name
                if may_collide and dst.exists():
                    stats["skipped"] += 1
                    continue
                if not dst_ready:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    dst_ready = True
                src = os.path.join(dirpath, name)
                # 先登记再动手：copy2 不是原子的（copyfile 先建好并截断目标再灌
                # 数据），写到一半炸掉时目标位置已经躺着一个截断的瓦片。操作后
                # 才 append 会让它漏出回滚网，然后被下一次重试当成「任务自己的
                # 瓦片」永久 skip 掉。link 分支多出的那次 unlink 抛
                # FileNotFoundError，被回滚里的 except OSError 吃掉，零副作用。
                written.append(dst)
                if use_link:
                    os.link(src, dst)
                    stats["linked"] += 1
                else:
                    shutil.copy2(src, dst)
                    stats["copied"] += 1
    except BaseException:
        # 捕到 BaseException 才连 Ctrl-C 一起兜住 —— 半成品底图的危害与是不是
        # 用户按的键无关。回滚只删 written 里的：被 skip 的、以及任务自己写的
        # 瓦片都不在里面，一个都不能带走。空目录留着无害（重试时 mkdir 直接复用），
        # 而 Cesium 只看文件在不在。
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        raise

    logger.info(f"Terrain: 底图植入完成 linked={stats['linked']} "
                f"copied={stats['copied']} skipped={stats['skipped']} → {tiles_dir}")
    return stats
