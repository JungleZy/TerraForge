"""随包分发的全球底图：解压到共享缓存，再植入任务目录。

**刻意不 import numpy / osgeo。** 这里全是文件操作，不 import 才能在没有
GDAL 的环境里单测 —— 与同目录的 layer_json.py 一个定位。

共享缓存放在 assets/terrain/base_z8，与分卷同目录：assets/ 是随包分发的数据，
downloads/ 是用户产出，解压出来的底图属于前者。打包模式下 Config.BASE_DIR
（sys.executable 所在目录）恒等于 bundle_dir()，正是 --include-data-dir 把
assets/terrain 放进去的地方，所以两种运行模式路径都对得上。
"""
from __future__ import annotations

import errno
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

# 最后那一步 os.replace（临时目录 → 最终位置）的退避重试参数，见
# _replace_with_retry 的 docstring。总尝试 6 次、间隔 0.5/1/2/4/8 s，
# 累计最多等 15.5 s。
#
# 6 次这个数是两头夹出来的：
# ① 下限来自要覆盖的窗口 —— 杀软扫完刚落盘的一批文件是「秒」量级，0.5 s 的
#    首次重试能吃掉绝大多数，指数退避到 15.5 s 给最坏情况留足余量；
# ② 上限来自失败场景的体感 —— 这一步真的坏了（比如目标被别的程序常驻占用）
#    时，用户看到的是「解压完了又卡住」，15 s 还能忍，一分钟就该报错了。
# 指数退避而不是等间隔：常见情况在第一次 0.5 s 就过了，长尾才付长等待。
_REPLACE_RETRY_ATTEMPTS = 6
_REPLACE_RETRY_FIRST_WAIT = 0.5
_REPLACE_RETRY_BACKOFF = 2.0

# **只有这几个 errno 才重试**。它们的共同含义是「这一刻被别人占着」，等一等有意义。
# 其余错误一律立刻抛：磁盘满（ENOSPC）、目标非空（ENOTEMPTY）、跨设备（EXDEV）
# 重试多少次都是同一个结果，白白把失败拖慢 15 s。EBUSY 是给 Windows 留的余量 ——
# 那边的「目标正被占用」不保证一律映射成 EACCES，漏掉它就等于在主力平台上少一条
# 可重试路径（本次现场是 WSL2，抓到的是 EACCES=13）。
# ⚠️ 上面那句「目标非空重试也没用」**在 Windows 上不成立**，别拿它当全平台结论：
# 那边目标非空时 `os.replace` 报的常常是 EACCES 而不是 ENOTEMPTY，于是它会落进
# 重试白名单、白等满 15.5 s 再抛。触发路径是上游 `rmtree(ignore_errors=True)` 部分
# 失败留下了非空目标。代价只是失败慢一点（不影响正确性），所以没有为它单开分支 ——
# 但排查 Windows 上「改名失败前先卡 15 s」时，先想到这条。
_REPLACE_RETRYABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})


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


def _replace_with_retry(src: Path, dst: Path) -> None:
    """把解压产物原子改名到最终位置，遇到**瞬时**占用错误退避重试。

    为什么需要重试：v0.2.9 真机验收时，解压跑满 19 分钟之后，最后这一步抛

        [Errno 13] Permission denied:
          assets/terrain/.base_unpack_34971_oeg4o9m2/base_z8
          -> assets/terrain/base_z8

    19 分钟全部白费，临时目录被 finally 清掉，底图永远装不上。

    **判定它是瞬时锁而不是文件系统限制，靠的是两次探针：**
    ① 空目录在同样两个路径之间 rename：成功；
    ② **同一个 4.3 万文件的真实底图目录**，在空闲时（不是刚写完那一刻）做同样
       的跨目录 rename：成功，耗时 **0.14 秒**。
    也就是说，同样的源、同样的目标、同样的文件系统，空闲时 0.14 s 就过，刚写完
    那一刻却失败 —— 所以既不是「目录太大 rename 不动」，也不是「9p/drvfs 不支持
    目录 rename」，而是**刚落盘那一瞬间目标还被别人持着句柄**。现场环境是 WSL2
    的 /mnt/d（9p + drvfs 转发到 Windows），Windows 侧的 Defender 实时扫描与搜索
    索引器在文件刚写完时仍持有句柄，MoveFile 就返回 Permission denied。
    这类错误**可重试**，等杀软扫完这批文件就好了。

    ⚠️ 后来的人要改这里，先重跑上面两条探针：只要「空闲时同一个目录 rename 得动」
    还成立，问题就在时机而不在容量，重试就是对的解法；哪天探针本身失败了，才轮到
    考虑换策略（比如逐层移动或整树复制）。

    重试用尽后把**最后一次的原始异常**原样抛出，交给上层包装成 RuntimeError ——
    调用方（dem_task_tiler / base_terrain_warmup）按的是那条契约。
    """
    wait = _REPLACE_RETRY_FIRST_WAIT
    for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if (e.errno not in _REPLACE_RETRYABLE_ERRNOS
                    or attempt >= _REPLACE_RETRY_ATTEMPTS):
                raise
            # 这条日志是卡住时唯一的诊断线索：外面看起来只是「解压完了没反应」。
            logger.warning(
                f"Terrain: 底图改名到最终位置失败（第 {attempt}/"
                f"{_REPLACE_RETRY_ATTEMPTS} 次，errno={e.errno} {e.strerror}），"
                f"疑似杀软/索引器仍持有刚落盘的文件，{wait:.1f} s 后重试 → {dst}")
            time.sleep(wait)
            wait *= _REPLACE_RETRY_BACKOFF


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
            # 不是裸 os.replace：刚写完 4.3 万个文件那一刻目标可能还被杀软/索引器
            # 持着句柄，而失败一次就是白扔 19 分钟。见 _replace_with_retry。
            _replace_with_retry(extracted, cache_dir)
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


def ungraft_base_from(tiles_dir: Path, base_dir: Path) -> int:
    """摘掉 tiles_dir 里与底图**共享 inode** 的文件，返回摘掉的个数。

    **必须在每次切片之前调用**，与 graft_base_into 成对。理由是硬链接 + 落盘
    方式的组合：cesiumlab_terrain._worker_tile 写瓦片用
    `(out_path / f"{y}.terrain").write_bytes(blob)`，而 `Path.write_bytes` 是
    `open(path, 'wb')` + write —— **就地截断同一个 inode**，不是先删再建。上一轮
    植入留下的硬链接指向共享缓存 assets/terrain/base_z8，于是这一笔直接改写了
    全局底图里的那份文件：之后所有任务都拿到被污染的 z0-7，且没有任何信号。

    可达路径不是理论上的：maxzoom <= 7 的任务实际起切层级就是 maxzoom（tiler 恒传
    min_level=8，由 build_terrain 钳到有效 max_level 以下），自己就要写 z0-7；
    重跑一次就正好打在上一轮植入的硬链接上。

    判据用 (st_dev, st_ino) 而不是「底图里有同名文件」：同一位置也可能躺着任务
    自己上一轮写的瓦片（退化任务与底图同层相撞），按名字摘会删掉任务数据，随后
    skip-if-exists 用底图瓦片把坑填上 —— 任务的 DEM 被底图静默顶替。跨盘回退的
    实体复制天然是独立 inode，摘不到也不需要摘。

    只扫底图占据的那几层（z0-z7 共 510 个目录）：任务的 z8+ 在底图里没有对应
    位置，永远不可能共享 inode，而那才是瓦片数量的大头。目标里整层目录都不存在
    时（首次切片的常态）连 os.walk 都不进，8 次 is_dir() 就返回。

    unlink 失败不吞：摘不掉就意味着接下来那一笔写穿共享缓存，宁可让任务当场失败。
    """
    tiles_dir = Path(tiles_dir)
    base_dir = Path(base_dir)
    if not tiles_dir.is_dir():
        return 0

    removed = 0
    base_str = str(base_dir)
    try:
        level_names = sorted(os.listdir(base_str))
    except FileNotFoundError:
        return 0

    for level in level_names:
        # 只走层号目录：底图根下的 layer.json 从来不参与植入（由
        # merge_base_availability 合成），任务那份更是不能碰。
        src_level = os.path.join(base_str, level)
        if not os.path.isdir(src_level):
            continue
        if not (tiles_dir / level).is_dir():
            continue
        # 层级里面不再逐目录剪枝：目标子目录不存在时下面每个 dst.stat() 都直接
        # FileNotFoundError，与先探一次目录同样是一次 stat，省不下来。也不排序：
        # 摘到一半失败会让整个任务失败，重试时从头再摘一遍，没有「留下的前缀要
        # 对得上」这回事（graft 排序是因为它的半成品会留在盘上）。
        for dirpath, _dirnames, filenames in os.walk(src_level, onerror=_walk_error):
            dst_dir = tiles_dir / os.path.relpath(dirpath, base_str)
            for name in filenames:
                dst = dst_dir / name
                try:
                    dst_st = dst.stat()
                except OSError:
                    # 目标没有这个瓦片是常态（首次切片、或上一轮只植入了一部分）。
                    continue
                src_st = os.stat(os.path.join(dirpath, name))
                if (dst_st.st_dev, dst_st.st_ino) != (src_st.st_dev, src_st.st_ino):
                    continue
                dst.unlink()
                removed += 1

    if removed:
        logger.info(f"Terrain: 切片前摘掉上一轮植入的底图硬链接 {removed} 个 → {tiles_dir}")
    return removed
