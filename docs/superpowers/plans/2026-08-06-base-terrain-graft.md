# 全球底图随任务植入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 地形切片的产出目录变成自包含 —— 切片前把随包底图解压到 `assets/terrain/base_z8`，切片后把 z0–z7 植入任务目录并合成 `layer.json`，整个目录拷走即可在别的机器上当地形源用。

**Architecture:** 新增一个不依赖 GDAL 的纯文件模块 `src/services/terrain_tiling/base_terrain.py`，提供解压（幂等 + 跨进程阻塞锁 + 原子改名）、植入（硬链接，跨盘回退复制）、`layer.json` 合成三件事。`tile_dem_task_dir` 按「解压 → 切片(min_level=8) → 植入 → 合成」串起来；底图不可用时退回现有的 parentUrl 级联，一行不动。

**Tech Stack:** Python 3.12 / 标准库（`tarfile` `gzip` `os.link` `shutil` `msvcrt`/`fcntl`）/ pytest / SQLite `PRAGMA user_version` 迁移 / Nuitka 打包参数。

**设计稿:** `docs/superpowers/specs/2026-08-06-base-terrain-graft-design.md`（先读它，尤其「排除的方案」一节 —— 有两条看着更自然的做法已经被否决，别重新发明）

## Global Constraints

- 语言：注释、docstring、提交信息一律中文；代码标识符英文。
- `base_terrain.py` **不得 import numpy / osgeo / GDAL**。它要能在没有 GDAL 的环境里单测，与 `layer_json.py` 定位一致。
- 单测**绝不能**往仓库的 `assets/terrain/` 里解压或写入，一律用 `tmp_path`。CI 流水线里测试跑在打包**之前**，一条测试污染仓库就会让 224 MB 被打进产物。
- 共享缓存路径：`assets/terrain/base_z8`（相对形态 `./assets/terrain/base_z8`，经 `resolve_stored_output_dir` 落到 `Config.BASE_DIR` 下）。旧值 `./downloads/terrain/base_z8` 只在迁移代码里出现。
- 底图规格（用于测试断言）。⚠️ **x 目录数与瓦片数是两回事，别混用** —— EPSG:4326 是 2:1 地理网格，z 层有 `2·2^z` 个 x 目录、每个 x 目录下 `2^z` 个 y 文件：

  | | z0 | z1 | z2 | z3 | z4 | z5 | z6 | z7 | 合计 |
  |---|---|---|---|---|---|---|---|---|---|
  | x 目录数（`iterdir` 数到的） | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 510 |
  | `.terrain` 瓦片数 | 2 | 8 | 32 | 128 | 512 | 2048 | 8192 | 32768 | 43,690 |

  `is_base_ready` 的探针 `_PROBE_LEVELS = ((0,2),(4,32),(7,256))` 用的是**上排**（与既有的 `scripts/unpack_base_terrain.py:55` 逐值一致），造测试夹具时也必须用上排。用下排会把 z7 建成 32,768 个目录 —— 慢 128 倍，而且阈值 256 被远远超过，`good` 用例就再也钉不住边界了。
- 每个 Task 结束时**只跑该 Task 涉及的测试文件**，不跑全量。全量在最后一个 Task 统一跑。
- 不要跑 formatter / linter。

---

### Task 1: `base_terrain` 模块骨架 —— 分卷定位与就位判据

**Files:**
- Create: `src/services/terrain_tiling/base_terrain.py`
- Create: `tests/test_base_terrain.py`

**Interfaces:**
- Consumes: `src/core/bundle.py:bundle_dir() -> str | None`、`src/core/config.py:Config.BASE_DIR`
- Produces:
  - `PARTS_GLOB = "base_z8.tar.gz.part*"`
  - `BASE_DIR_NAME = "base_z8"`
  - `base_parts_dir() -> Path | None` —— 分卷所在目录，找不到分卷返回 `None`
  - `base_cache_dir() -> Path` —— `Config.BASE_DIR / "assets" / "terrain" / "base_z8"`
  - `is_base_ready(cache_dir: Path) -> bool`

- [ ] **Step 1: 写失败的测试**

```python
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
    """造一个 base_z8 骨架。第二个数是 **x 目录数**，不是瓦片数。

    真实底图 z0-z7 共 510 个 x 目录（2·2^z），照这个建 `is_base_ready` 的三条
    探针恰好卡在边界上 —— 少一个目录就该红。误用瓦片数（32768）会建出 128 倍
    的目录，阈值被远远超过，边界断言就失效了。

    dense=False 时只建 x 目录不建瓦片，够就位判据用。
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
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.services.terrain_tiling.base_terrain'`

- [ ] **Step 3: 写最小实现**

```python
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
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
git add src/services/terrain_tiling/base_terrain.py tests/test_base_terrain.py
git commit -m "feat(terrain): 底图模块骨架 —— 分卷定位、缓存路径、就位判据"
```

---

### Task 2: 幂等解压 + 跨进程阻塞锁 + 原子改名

**Files:**
- Modify: `src/services/terrain_tiling/base_terrain.py`
- Modify: `tests/test_base_terrain.py`
- Read first: `src/core/single_instance.py:60-100`（平台分支写法照抄，语义要改成阻塞）

**Interfaces:**
- Consumes: Task 1 的 `base_parts_dir()` / `base_cache_dir()` / `is_base_ready()`
- Produces: `ensure_base_unpacked(cache_dir: Path | None = None, stage_cb=None) -> Path | None`
  —— 返回就位的缓存目录；分卷不存在返回 `None`；解压失败抛 `RuntimeError`

- [ ] **Step 1: 写失败的测试**

```python
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


def test_ensure_base_unpacked_extracts_and_is_idempotent(tmp_path, monkeypatch):
    """首次解压落地；第二次直接返回，不重复解压。"""
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

    def boom(*a, **k):
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
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'ensure_base_unpacked'`

- [ ] **Step 3: 写实现（追加到 `base_terrain.py`）**

```python
import os
import shutil
import tarfile
import tempfile


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
        tf.extractall(dest)


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
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: 提交**

```bash
git add src/services/terrain_tiling/base_terrain.py tests/test_base_terrain.py
git commit -m "feat(terrain): 底图幂等解压 —— 跨进程阻塞锁 + 临时目录原子改名"
```

---

### Task 3: 植入任务目录（硬链接，跨盘回退复制）

**Files:**
- Modify: `src/services/terrain_tiling/base_terrain.py`
- Modify: `tests/test_base_terrain.py`

**Interfaces:**
- Consumes: Task 2 的 `ensure_base_unpacked`
- Produces: `graft_base_into(tiles_dir: Path, base_dir: Path) -> dict`
  —— 返回 `{"linked": int, "copied": int, "skipped": int}`；失败抛 `OSError` 并回滚本次已植入的文件

- [ ] **Step 1: 写失败的测试**

```python
def test_graft_prefers_hardlinks(tmp_path):
    """同盘时走硬链接：磁盘只占一份。"""
    from src.services.terrain_tiling.base_terrain import graft_base_into

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 2)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    got = graft_base_into(tiles, base)

    assert got["linked"] > 0 and got["copied"] == 0
    src = base / "0" / "0" / "0.terrain"
    dst = tiles / "0" / "0" / "0.terrain"
    assert dst.is_file()
    assert os.stat(src).st_ino == os.stat(dst).st_ino, "同盘应当是硬链接"


def test_graft_falls_back_to_copy_when_link_unsupported(tmp_path, monkeypatch):
    """跨盘 / 文件系统不支持硬链接时回退实体复制，内容必须逐字节一致。

    DEM 任务的输出路径是用户自选的全盘路径，跨盘是常态不是例外 —— 这条分支
    要当主路径测。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 2)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    def no_link(*a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(base_terrain.os, "link", no_link)

    got = base_terrain.graft_base_into(tiles, base)

    assert got["copied"] > 0 and got["linked"] == 0
    src = base / "0" / "0" / "0.terrain"
    dst = tiles / "0" / "0" / "0.terrain"
    assert dst.read_bytes() == src.read_bytes()
    assert os.stat(src).st_ino != os.stat(dst).st_ino


def test_graft_never_overwrites_task_tiles(tmp_path):
    """任务已经写过的瓦片胜出。

    正常情况零冲突（任务 z8+、底图 z0-7），这条是为了兜住 maxzoom < 8 的退化
    任务 —— 那时两边在同一层相撞，任务的 DEM 数据必须赢。
    """
    from src.services.terrain_tiling.base_terrain import graft_base_into

    base = _make_base(tmp_path / "base", layers=((0, 2),), dense=True)
    tiles = tmp_path / "tiles"
    (tiles / "0" / "0").mkdir(parents=True)
    (tiles / "0" / "0" / "0.terrain").write_bytes(b"TASK-OWN")

    got = graft_base_into(tiles, base)

    assert got["skipped"] >= 1
    assert (tiles / "0" / "0" / "0.terrain").read_bytes() == b"TASK-OWN"


def test_graft_rolls_back_what_it_wrote_on_failure(tmp_path, monkeypatch):
    """植入中途失败 → 抛出，且本次写进去的文件被清掉。

    半个底图比没有更糟：缺的瓦片让 Cesium 拿 404，整个 provider 降级成
    heightmap，任务自己的瓦片高程也跟着错（v0.2.8 实测）。
    """
    from src.services.terrain_tiling import base_terrain

    base = _make_base(tmp_path / "base", layers=((0, 2), (1, 8)), dense=True)
    tiles = tmp_path / "tiles"
    tiles.mkdir()

    n = {"i": 0}
    real_link = base_terrain.os.link

    def flaky(src, dst, **k):
        n["i"] += 1
        if n["i"] > 3:
            raise OSError(28, "No space left on device")
        return real_link(src, dst, **k)

    monkeypatch.setattr(base_terrain.os, "link", flaky)
    # 任务自己的瓦片不属于本次植入，不得被回滚带走
    (tiles / "9" / "1").mkdir(parents=True)
    (tiles / "9" / "1" / "1.terrain").write_bytes(b"TASK-OWN")

    with pytest.raises(OSError):
        base_terrain.graft_base_into(tiles, base)

    leftover = [p for p in tiles.rglob("*.terrain")
                if p.read_bytes() != b"TASK-OWN"]
    assert leftover == [], f"回滚不干净：{leftover}"
    assert (tiles / "9" / "1" / "1.terrain").read_bytes() == b"TASK-OWN"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'graft_base_into'`

- [ ] **Step 3: 写实现（追加到 `base_terrain.py`）**

```python
def _probe_hardlink(base_dir: Path, tiles_dir: Path) -> bool:
    """在目标目录里试链一次，成败决定整批策略。

    **不做 4.3 万次逐个 try**：策略在同一个目标目录里不会中途改变，而逐个 try
    的异常开销在 Windows 上是可测量的。
    """
    probe_src = base_dir / "layer.json"
    probe_dst = tiles_dir / ".hardlink_probe"
    try:
        if probe_dst.exists():
            probe_dst.unlink()
        os.link(probe_src, probe_dst)
    except OSError:
        return False
    finally:
        try:
            probe_dst.unlink()
        except OSError:
            pass
    return True


def graft_base_into(tiles_dir: Path, base_dir: Path) -> dict:
    """把底图植入任务瓦片目录，返回 {linked, copied, skipped}。

    skip-if-exists：任务已经写过的瓦片不被覆盖。正常情况零冲突（任务 z8+、
    底图 z0-7），这条规则兜的是 maxzoom < 8 的退化任务。

    失败即失败并回滚：留半个底图会让 Cesium 拿 404 → 塞假 heightmap 图层 →
    污染共享 builder → 任务自己的 quantized-mesh 瓦片也按 heightmap 解析。
    """
    tiles_dir = Path(tiles_dir)
    base_dir = Path(base_dir)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    use_link = _probe_hardlink(base_dir, tiles_dir)
    stats = {"linked": 0, "copied": 0, "skipped": 0}
    written: list[Path] = []

    try:
        for src in base_dir.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(base_dir)
            # layer.json 由 merge_layer_json 合成，不能直接搬过来盖掉任务的
            if rel.name == "layer.json" and len(rel.parts) == 1:
                continue
            dst = tiles_dir / rel
            if dst.exists():
                stats["skipped"] += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if use_link:
                os.link(src, dst)
                stats["linked"] += 1
            else:
                shutil.copy2(src, dst)
                stats["copied"] += 1
            written.append(dst)
    except BaseException:
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        raise

    logger.info(f"Terrain: 底图植入完成 linked={stats['linked']} "
                f"copied={stats['copied']} skipped={stats['skipped']}")
    return stats
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: 提交**

```bash
git add src/services/terrain_tiling/base_terrain.py tests/test_base_terrain.py
git commit -m "feat(terrain): 底图植入任务目录 —— 硬链接优先，跨盘回退复制"
```

---

### Task 4: `layer.json` 合成

**Files:**
- Modify: `src/services/terrain_tiling/layer_json.py`
- Modify: `tests/test_layer_json.py`

**Interfaces:**
- Produces: `merge_base_availability(task_layer_path: Path, base_layer_path: Path) -> None`
  —— 就地改写 `task_layer_path`

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_layer_json.py`）**

```python
def _write_layer(path, *, maxzoom, available, parent=None):
    import json
    data = {"tilejson": "1.0", "format": "quantized-mesh-1.0", "scheme": "tms",
            "projection": "EPSG:4326", "tiles": ["{z}/{x}/{y}.terrain"],
            "minzoom": 0, "maxzoom": maxzoom, "available": available}
    if parent:
        data["parentUrl"] = parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_merge_base_availability_unions_levels_and_drops_parent_url(tmp_path):
    """available 逐层并集，parentUrl 必须被删掉。

    自包含之后 parentUrl 是一次多余请求，而且它指向 localhost —— 目录拷到别的
    机器上必然 404，而 Cesium 的 404 处理会把整个 provider 降级成 heightmap。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=10,
                        available=[[]] * 8 + [
                            [{"startX": 5, "startY": 5, "endX": 6, "endY": 6}],
                            [{"startX": 10, "startY": 10, "endX": 12, "endY": 12}],
                            [{"startX": 20, "startY": 20, "endX": 24, "endY": 24}]],
                        parent="http://localhost:5000/terrain/base")

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert "parentUrl" not in data
    assert data["minzoom"] == 0
    assert data["maxzoom"] == 10
    assert len(data["available"]) == 11
    # z0-z7 来自底图
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
    # z8+ 来自任务
    assert data["available"][8] == [{"startX": 5, "startY": 5, "endX": 6, "endY": 6}]


def test_merge_keeps_base_levels_deeper_than_the_task(tmp_path):
    """maxzoom < 8 的退化任务：maxzoom 必须取 max(7, 任务的)。

    直接取任务的会把底图的 z6/z7 声明掉，Cesium 从此不请求它们 —— 明明文件
    就在目录里，却看不到。
    """
    import json

    from src.services.terrain_tiling.layer_json import merge_base_availability

    base = _write_layer(tmp_path / "base" / "layer.json", maxzoom=7,
                        available=[[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8)
    task = _write_layer(tmp_path / "task" / "layer.json", maxzoom=5,
                        available=[[]] * 5 + [[{"startX": 3, "startY": 3, "endX": 3, "endY": 3}]])

    merge_base_availability(task, base)
    data = json.loads(task.read_text(encoding="utf-8"))

    assert data["maxzoom"] == 7
    assert len(data["available"]) == 8
    # 同层相撞时两边的声明都保留（任务瓦片在磁盘上胜出，声明是并集）
    assert {"startX": 3, "startY": 3, "endX": 3, "endY": 3} in data["available"][5]
    assert data["available"][7] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_layer_json.py -q`
Expected: FAIL — `ImportError: cannot import name 'merge_base_availability'`

- [ ] **Step 3: 写实现（追加到 `layer_json.py`）**

```python
def merge_base_availability(task_layer_path: Path, base_layer_path: Path) -> None:
    """把底图的 available 并进任务的 layer.json，并删掉 parentUrl。

    植入之后任务目录是自包含的，parentUrl 就成了多余的一次请求 —— 更糟的是它
    指向 localhost:5000，目录拷到别的机器上必然 404，而 Cesium 对这个 404 不
    报错，它塞一个假的 heightmap-1.0 图层并把 heightmapStructure 写在共享的
    builder 上，于是任务自己的 quantized-mesh 瓦片也按 heightmap 解析
    （v0.2.8 实测：4154 m 山峰解成 -744 m，控制台零报错）。

    maxzoom 取 max(底图, 任务)：maxzoom < 8 的退化任务里底图的 z6/z7 比任务更
    深，写任务的值会把这两层声明掉，Cesium 从此不请求它们 —— 文件在目录里却
    看不到。
    """
    task = json.loads(task_layer_path.read_text(encoding="utf-8"))
    base = json.loads(base_layer_path.read_text(encoding="utf-8"))

    task_av = task.get("available") or []
    base_av = base.get("available") or []
    merged = []
    for z in range(max(len(task_av), len(base_av))):
        ranges = list(base_av[z]) if z < len(base_av) else []
        for r in (task_av[z] if z < len(task_av) else []):
            if r not in ranges:
                ranges.append(r)
        merged.append(ranges)

    task["available"] = merged
    task["minzoom"] = 0
    task["maxzoom"] = max(int(base.get("maxzoom", 0) or 0),
                          int(task.get("maxzoom", 0) or 0))
    task.pop("parentUrl", None)

    task_layer_path.write_text(
        json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_layer_json.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/services/terrain_tiling/layer_json.py tests/test_layer_json.py
git commit -m "feat(terrain): layer.json 合成 —— available 并集、maxzoom 取深、删 parentUrl"
```

---

### Task 5: 接进切片流程（解压 → 切片 z8+ → 植入 → 合成）

**Files:**
- Modify: `src/services/terrain_tiling/dem_task_tiler.py:9-10`（import）
- Modify: `src/services/terrain_tiling/dem_task_tiler.py:84-102`（调用顺序）
- Modify: `tests/test_dem_task_tiler.py`

**Interfaces:**
- Consumes: `ensure_base_unpacked(cache_dir=None, stage_cb=None)`、`graft_base_into(tiles_dir, base_dir)`、`merge_base_availability(task_layer_path, base_layer_path)`
- Produces: 无新公开符号。`TileParams` **不动** —— 起始层级由底图可用性决定，不需要新字段（理由见 Step 3）。两个 manager 一行都不用改，它们对 tiler 的 `(task_dir, out_dir, params)` 调用形态不变。

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_dem_task_tiler.py`）**

```python
def test_tiler_grafts_base_and_merges_layer_json(tmp_path, monkeypatch):
    """底图可用时：切片走 min_level=8，切完植入并合成，不再写 parentUrl。"""
    import json

    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "a_dem.tif").write_bytes(b"x")
    out_dir = tmp_path / "out"

    base = tmp_path / "base"
    (base / "0" / "0").mkdir(parents=True)
    (base / "0" / "0" / "0.terrain").write_bytes(b"\x1f\x8bbase")
    (base / "layer.json").write_text(
        json.dumps({"maxzoom": 7,
                    "available": [[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]] * 8}),
        encoding="utf-8")

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text(
            json.dumps({"maxzoom": 10, "available": [[]] * 10 + [
                [{"startX": 1, "startY": 1, "endX": 2, "endY": 2}]]}),
            encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0}

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)

    mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=10, parent_url="http://localhost:5000/terrain/base"),
        build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 8, "底图可用时任务只切 z8+"
    assert (out_dir / "0" / "0" / "0.terrain").is_file(), "底图没被植入"
    data = json.loads((out_dir / "layer.json").read_text(encoding="utf-8"))
    assert "parentUrl" not in data
    assert data["available"][0] == [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]


def test_tiler_falls_back_to_parent_url_without_base(tmp_path, monkeypatch):
    """底图不可用（分卷被删）→ 行为与 v0.2.8 完全一致：min_level=0 + 写 parentUrl。"""
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "a_dem.tif").write_bytes(b"x")
    out_dir = tmp_path / "out"

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: None)

    mod.tile_dem_task_dir(
        task_dir, out_dir,
        mod.TileParams(maxzoom=10, parent_url="https://example.com/parent"),
        build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 0
    layer = (out_dir / "layer.json").read_text(encoding="utf-8")
    assert '"parentUrl": "https://example.com/parent"' in layer


def test_degenerate_maxzoom_still_tiles_something(tmp_path, monkeypatch):
    """maxzoom < 8 的任务不能因为 min_level=8 切出零张瓦片却报成功。

    min_level 死写 8 时 max_level(5) < min_level(8)，_tile_ranges() 产出空区间，
    任务 rendered=0 却 completed —— 又一款静默成功。
    """
    from src.services.terrain_tiling import dem_task_tiler as mod

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "a_dem.tif").write_bytes(b"x")

    base = tmp_path / "base"
    base.mkdir()
    (base / "layer.json").write_text('{"maxzoom": 7, "available": []}', encoding="utf-8")

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "layer.json").write_text('{"maxzoom": 5, "available": []}', encoding="utf-8")

    monkeypatch.setattr(mod, "ensure_base_unpacked", lambda **k: base)

    mod.tile_dem_task_dir(task_dir, tmp_path / "out",
                          mod.TileParams(maxzoom=5, parent_url=""),
                          build_terrain_fn=fake_build_terrain)

    assert seen["min_level"] == 5, "min_level 必须是 min(8, maxzoom)"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_dem_task_tiler.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'ensure_base_unpacked'`

- [ ] **Step 3: 改 `dem_task_tiler.py`**

顶部 import 加：

```python
from src.services.terrain_tiling.base_terrain import ensure_base_unpacked, graft_base_into
from src.services.terrain_tiling.layer_json import merge_base_availability, patch_layer_json_parent
```

`TileParams` **不加新字段** —— 起始层级完全由「底图可不可用」决定，没有第二种取值需要调用方传。加一个默认 0、又总是被覆盖的 `min_level` 就是个假旋钮，这个仓库已经有 `terrain_global_base_maxzoom` 那个「全项目零消费」的教训。

`tile_dem_task_dir` 里，把 `build_terrain_fn(...)` 之前和 `patch_layer_json_parent` 那段换成：

```python
    # 解压排在切片前：首次解压是分钟级，要独占 stage_cb 上报通道，否则和切片
    # 进度抢同一条通道，前端只能看到进度条来回跳。
    base_dir = ensure_base_unpacked(stage_cb=params.stage_cb)

    # 底图独占 z0-z7，任务只出 z8+：两边零冲突，也没有「半张瓦片是真数据、
    # 半张是采到 DEM 外的外推值」那种接缝。
    # min(8, maxzoom) 而不是死写 8 —— maxzoom < 8 时 min_level > max_level 会让
    # _tile_ranges() 产出空区间，任务切零张瓦片却报 completed，又一款静默成功。
    min_level = min(8, int(params.maxzoom)) if base_dir is not None else 0

    counts = build_terrain_fn(
        inputs=[str(p) for p in dem_tifs],
        output_dir=str(out_dir),
        min_level=min_level,
        max_level=int(params.maxzoom),
        tile_size=int(params.tile_size),
        workers=int(params.workers),
        progress_cb=params.progress_cb,
        stage_cb=params.stage_cb,
        stop_flag=params.stop_flag,
        triangulator=params.triangulator,
        max_error_k=params.max_error_k,
    )

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.is_file():
        raise FileNotFoundError(f"Missing layer.json at {layer_json_path}")

    if base_dir is not None:
        # 植入失败即任务失败：半个底图会让 Cesium 拿 404 并把整个 provider
        # 降级成 heightmap，比根本没有底图更糟。
        graft_base_into(out_dir, base_dir)
        merge_base_availability(layer_json_path, base_dir / "layer.json")
    else:
        patch_layer_json_parent(layer_json_path, params.parent_url)
```

- [ ] **Step 4: 加全局护栏，防止测试往仓库里解压 224 MB**

接入之后，任何调用**真** `tile_dem_task_dir` 的测试都会走到 `ensure_base_unpacked()`。本机仓库里 `assets/terrain/*.part` 是真实存在的 —— 于是这些测试会真的解压 224 MB / 4.3 万个文件进仓库，违反 Global Constraints，而且现象是「测试跑了几分钟」不是报错，极难归因。已知会踩到的有 4 条：`test_dem_task_tiler.py` 的三条、`test_terrain_stage_progress.py` 里的穿透那条。

靠「记得给每条测试加 mock」挡不住以后新写的测试。在 `tests/conftest.py` 加 autouse fixture，形制照抄同文件里既有的 `isolate_startup_sweep`（它挡的是启动清扫打到真实 /tmp，同一类问题）：

```python
@pytest.fixture(scope="session")
def _base_terrain_sandbox(tmp_path_factory):
    return tmp_path_factory.mktemp("base_terrain_sandbox")


@pytest.fixture(autouse=True)
def isolate_base_terrain(monkeypatch, _base_terrain_sandbox):
    """测试侧防护：不让任何测试把随包底图解压进仓库。

    `tile_dem_task_dir` 现在开头就调 `ensure_base_unpacked()`，而仓库里
    `assets/terrain/*.part` 是真实存在的 —— 任何调用真 tiler 的测试都会解出
    224 MB / 4.3 万个文件到 `assets/terrain/base_z8`。两个后果：跑一次测试多
    等几分钟（现象不是报错，是「怎么这么慢」，极难归因），以及 CI 里测试跑在
    Nuitka 打包**之前**，解出来的东西会被打进三个平台的产物。

    做法是把分卷目录指到一个空沙箱：`base_parts_dir()` 找不到分卷就返回 None，
    `ensure_base_unpacked()` 随之返回 None，调用方走 parentUrl 兜底 —— 正是
    这些既有测试原本断言的路径，所以它们一行都不用改。

    要测真解压的用例（`test_base_terrain.py`）在用例内自己 monkeypatch
    `_assets_terrain_dir`，setattr 打在后面，覆盖本 fixture。
    """
    try:
        from src.services.terrain_tiling import base_terrain as bt
    except Exception:  # 环境缺依赖时不阻断收集
        return
    monkeypatch.setattr(bt, "_assets_terrain_dir",
                        lambda: Path(_base_terrain_sandbox))
```

`tests/conftest.py` 顶部若无 `from pathlib import Path` 则加上。

- [ ] **Step 5: 跑测试确认它绿**

Run: `uv run pytest tests/test_dem_task_tiler.py tests/test_terrain_stage_progress.py tests/test_base_terrain.py tests/test_fix_terrain_gdal_import.py -q`
Expected: PASS。四条既有用例**不改一行**就该继续绿（沙箱让它们走兜底路径，正是它们原本断言的）。若哪条红了，说明护栏没生效或接入顺序不对，别改断言迁就。

计时核对：这几个文件加起来应当是**秒级**。若跑了几分钟，说明护栏漏了，有测试在真解压。

- [ ] **Step 6: 提交**

```bash
git add src/services/terrain_tiling/dem_task_tiler.py tests/test_dem_task_tiler.py tests/conftest.py
git commit -m "feat(terrain): 切片流程接入底图 —— 解压→切片(z8+)→植入→合成"
```

---

### Task 6: 存量库的 config 迁移（`user_version` 2 → 3）

**Files:**
- Modify: `src/core/database.py:66-72`（默认值 + 挪错位的注释）
- Modify: `src/core/database.py:641-643`（挂上新迁移）
- Create: 迁移函数 `migrate_base_path_to_assets(cursor)`，放在 `normalize_stored_output_paths` 之后
- Create: `tests/test_fix_base_path_migration.py`

**Interfaces:**
- Produces: `migrate_base_path_to_assets(cursor) -> bool`（改写了返回 True）
- 常量：`_OLD_BASE_PATH = './downloads/terrain/base_z8'`、`_NEW_BASE_PATH = './assets/terrain/base_z8'`

- [ ] **Step 1: 写失败的测试**

```python
"""底图缓存位置迁移：downloads/terrain/base_z8 → assets/terrain/base_z8。

只改 DEFAULT_CONFIGS 是不够的 —— 那走的是 INSERT OR IGNORE，只对新建的库生效。
存量库那行还是旧路径，于是：解压去新位置 → 旧位置空 → /terrain/base 和可用性
判定都按旧路径 → 底图判为不可用 → 走 parentUrl 兜底 → 那个 URL 指向服务旧空
路径的 /terrain/base → 404 → Cesium 塞假 heightmap 图层污染共享 builder →
任务自己的瓦片高程全错且零报错。正是 v0.2.8 刚修过的那条链。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _legacy_db(path, base_path_value, user_version=2):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('terrain_global_base_path', ?)",
                 (base_path_value,))
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    return conn


def _read(conn):
    row = conn.execute(
        "SELECT value FROM config WHERE key = 'terrain_global_base_path'").fetchone()
    return row["value"]


def test_migration_rewrites_the_stale_default(tmp_path):
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "a.db", "./downloads/terrain/base_z8")
    assert migrate_base_path_to_assets(conn.cursor()) is True
    conn.commit()

    assert _read(conn) == "./assets/terrain/base_z8"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migration_leaves_a_user_customised_path_alone(tmp_path):
    """用户自己改过的路径不动 —— 迁移只认旧默认值。"""
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "b.db", "/mnt/big-disk/my-base")
    migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert _read(conn) == "/mnt/big-disk/my-base"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migration_is_reentrant(tmp_path):
    """user_version 已是 3 → 不再改写。"""
    from src.core.database import migrate_base_path_to_assets

    conn = _legacy_db(tmp_path / "c.db", "./downloads/terrain/base_z8", user_version=3)
    assert migrate_base_path_to_assets(conn.cursor()) is False
    conn.commit()

    assert _read(conn) == "./downloads/terrain/base_z8"


def test_migration_moves_an_existing_unpacked_base(tmp_path, monkeypatch):
    """旧位置有完整底图 → rename 过去，不重解压 224 MB。"""
    from src.core import config as config_mod
    from src.core.database import migrate_base_path_to_assets

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")

    old = tmp_path / "downloads" / "terrain" / "base_z8"
    old.mkdir(parents=True)
    (old / "layer.json").write_text("{}", encoding="utf-8")

    conn = _legacy_db(tmp_path / "d.db", "./downloads/terrain/base_z8")
    migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert (tmp_path / "assets" / "terrain" / "base_z8" / "layer.json").is_file()
    assert not old.exists()


def test_migration_survives_a_failing_rename(tmp_path, monkeypatch):
    """rename 失败（跨盘等）不能阻断启动：旧目录留着，新位置留待重新解压。"""
    from src.core import config as config_mod
    from src.core import database as db_mod

    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")

    old = tmp_path / "downloads" / "terrain" / "base_z8"
    old.mkdir(parents=True)
    (old / "layer.json").write_text("{}", encoding="utf-8")

    def boom(*a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(db_mod.shutil, "move", boom)

    conn = _legacy_db(tmp_path / "e.db", "./downloads/terrain/base_z8")
    db_mod.migrate_base_path_to_assets(conn.cursor())
    conn.commit()

    assert old.is_dir(), "rename 失败时旧目录必须保留"
    assert _read(conn) == "./assets/terrain/base_z8"
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_fix_base_path_migration.py -q`
Expected: FAIL — `ImportError: cannot import name 'migrate_base_path_to_assets'`

- [ ] **Step 3: 写实现**

`src/core/database.py` 顶部加 `import shutil`。`DEFAULT_CONFIGS` 里改一行并把错位的注释挪到它该在的键下面：

```python
    # Terrain defaults
    # 解压后的全球底图与分卷同目录（assets/ 是随包分发的数据，downloads/ 是
    # 用户产出）。改这个默认值的同时必须跑 migrate_base_path_to_assets ——
    # INSERT OR IGNORE 只对新建的库生效，存量行还是旧路径。
    ('terrain_global_base_path', './assets/terrain/base_z8'),
    # ⚠️ 这个键**全项目零消费** —— 没有任何代码读 terrain_global_base_maxzoom，
    # base 的层级由 layer.json 的 available 决定。保留是为了兼容存量 config 行；
    # 真要用它之前先确认有没有第二处事实来源，否则又是一个「改了没反应」的假旋钮。
    # （注意 terrain_global_base_path 不是零消费：terrain_static、dem_task_manager、
    # local_terrain_task_manager 三处都读它。）
    ('terrain_global_base_maxzoom', '7'),
```

在 `normalize_stored_output_paths` 之后加：

```python
_OLD_BASE_PATH = './downloads/terrain/base_z8'
_NEW_BASE_PATH = './assets/terrain/base_z8'


def migrate_base_path_to_assets(cursor) -> bool:
    """底图缓存位置 downloads/ → assets/ 的一次性迁移（user_version 2 → 3）。

    **只改 DEFAULT_CONFIGS 不够**：它走 INSERT OR IGNORE，只对新建的库生效。
    存量库那行还是旧路径，于是解压去新位置、服务与可用性判定按旧位置 → 底图
    判为不可用 → 走 parentUrl 兜底 → 那个 URL 指向服务旧空路径的 /terrain/base
    → 404 → Cesium 塞假 heightmap 图层污染共享 builder → 任务自己的
    quantized-mesh 瓦片也按 heightmap 解析，高程全错且零报错。

    只在该行**仍等于旧默认值**时改写：用户自定义过的路径不动。
    旧位置已有底图时直接搬过去，不删掉重解压 224 MB；搬不动（跨盘）就留着，
    新位置重新解压 —— 多占一份磁盘，但不会坏。
    """
    if cursor.execute('PRAGMA user_version').fetchone()[0] >= 3:
        return False

    changed = False
    try:
        row = cursor.execute(
            "SELECT value FROM config WHERE key = 'terrain_global_base_path'"
        ).fetchone()
        current = (row['value'] if row is not None and hasattr(row, 'keys')
                   else (row[0] if row else None))
        if current is not None and str(current).strip() == _OLD_BASE_PATH:
            cursor.execute(
                "UPDATE config SET value = ? WHERE key = 'terrain_global_base_path'",
                (_NEW_BASE_PATH,))
            changed = True

            old_dir = Path(Config.DOWNLOADS_DIR) / 'terrain' / 'base_z8'
            new_dir = Path(Config.BASE_DIR) / 'assets' / 'terrain' / 'base_z8'
            if old_dir.is_dir() and not new_dir.exists():
                try:
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_dir), str(new_dir))
                    logger.info(f'底图缓存已搬到 {new_dir}')
                except OSError as e:
                    logger.warning(
                        f'底图缓存搬迁失败（{e!r}），旧目录保留在 {old_dir}，'
                        f'新位置会重新解压')
    except Exception as e:
        logger.warning(f'terrain_global_base_path 迁移跳过（{e!r}）')

    cursor.execute('PRAGMA user_version = 3')
    if changed:
        logger.info('terrain_global_base_path 迁移到 assets/ (user_version=3)')
    return changed
```

顶部若无 `from pathlib import Path` 则加上。最后在 `init_database()` 的 `normalize_stored_output_paths(cursor)` 之后加一行：

```python
        migrate_base_path_to_assets(cursor)
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_fix_base_path_migration.py tests/test_database.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/core/database.py tests/test_fix_base_path_migration.py
git commit -m "fix(db): 底图缓存位置迁移到 assets/，存量 config 行一次性改写"
```

---

### Task 7: 打包排除 + gitignore + CLI 瘦身 + 文档订正

**Files:**
- Modify: `nuitka_build.py:366`（加 `--noinclude-data-files`）
- Modify: `.gitignore:14,130`（去重 `downloads/`，加 `assets/terrain/base_z8/`）
- Modify: `scripts/unpack_base_terrain.py`（改成薄 CLI 包装）
- Modify: `CLAUDE.md:134,146`（订正底图路径与过期的穿越校验说法）
- Modify: `tests/test_fix_build_scripts.py`（加打包参数契约）
- Create: `tests/test_no_repo_pollution.py`

**Interfaces:**
- Consumes: Task 2 的 `ensure_base_unpacked`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_fix_build_scripts.py`：

```python
def test_nuitka_excludes_the_unpacked_base():
    """打包必须排除解压后的底图目录。

    assets/terrain 是整目录收（--include-data-dir），缓存挪进来之后，任何在
    本机跑过一次切片的人再构建，dist 会平白多 224 MB / 4.3 万个文件 —— 正是
    那行注释里说「让 Nuitka 逐个收集会把构建拖垮」而刻意避开的事。
    """
    content = _read('nuitka_build.py')
    assert '--include-data-dir=assets/terrain=assets/terrain' in content
    assert '--noinclude-data-files=assets/terrain/base_z8/**' in content, (
        '缺了这条排除，dist 会把解压后的 4.3 万个底图瓦片一起打进去')
```

新建 `tests/test_no_repo_pollution.py`：

```python
"""测试不得往仓库的 assets/terrain/ 里解压。

CI 流水线里测试跑在 Nuitka 打包**之前**：有一条测试解压到仓库里，224 MB /
4.3 万个文件就会被打进三个平台的产物。这条用例是那个约束的守卫。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_repo_assets_terrain_has_no_unpacked_base():
    unpacked = os.path.join(_REPO, "assets", "terrain", "base_z8")
    assert not os.path.exists(unpacked), (
        f"仓库里出现了解压后的底图：{unpacked}\n"
        f"要么是某条测试把它解到了仓库（必须改成 tmp_path），"
        f"要么是本机手工解压过（删掉它，或确认 .gitignore 挡住了）")


def test_gitignore_blocks_the_unpacked_base():
    with open(os.path.join(_REPO, ".gitignore"), encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    assert "assets/terrain/base_z8/" in lines, (
        ".gitignore 要挡住解压后的 4.3 万个文件，否则 git status 会被淹掉")
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run pytest tests/test_no_repo_pollution.py tests/test_fix_build_scripts.py -q`
Expected: FAIL — `test_gitignore_blocks_the_unpacked_base` 与 `test_nuitka_excludes_the_unpacked_base` 红
（⚠️ 本机 `downloads/terrain/base_z8` 存在但 `assets/terrain/base_z8` 不存在，第一条应当是绿的；若红说明本机已手工解压过，删掉即可）

- [ ] **Step 3: 改四个文件**

`nuitka_build.py`，在 `'--include-data-dir=assets/terrain=assets/terrain',` 之后加：

```python
        # 排除解压后的目录：assets/terrain 是整目录收，而运行期会把分卷解压成
        # 同目录下的 base_z8（4.3 万个文件 / 224 MB）。不排除的话，任何在本机
        # 跑过一次切片的人再构建，产物就平白多这一份 —— 正是上面那段注释里说
        # 「让 Nuitka 逐个收集会把构建拖垮」而刻意避开的事。
        '--noinclude-data-files=assets/terrain/base_z8/**',
```

`.gitignore`：删掉第 130 行重复的 `downloads/`，在第 14 行 `downloads/` 之后加：

```
# 运行期从 assets/terrain/*.part 解压出来的全球底图（4.3 万个文件 / 224 MB）
assets/terrain/base_z8/
```

`scripts/unpack_base_terrain.py` 改成薄包装（保留 `-o` / `--force`，逻辑全部委托）：

```python
#!/usr/bin/env python3
"""把 assets/terrain/ 里的分卷还原成全球 base 地形（手工入口）。

⚠️ 正常情况下**不需要跑它** —— 地形切片会自动解压（见
`src/services/terrain_tiling/base_terrain.py:ensure_base_unpacked`）。
这个脚本留作排障与预热用：想在第一次切片之前先把那几分钟花掉，或者怀疑
缓存坏了要强制重解。

用法：
    uv run python scripts/unpack_base_terrain.py            # 解到默认位置
    uv run python scripts/unpack_base_terrain.py --force    # 已存在也重解
    uv run python scripts/unpack_base_terrain.py -o <dir>   # 解到指定目录
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.terrain_tiling.base_terrain import (  # noqa: E402
    base_cache_dir, ensure_base_unpacked, is_base_ready,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="还原全球 base 地形（分卷 → 目录）")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help=f"解压目标（默认 {base_cache_dir()}）")
    ap.add_argument("--force", action="store_true", help="已存在也重新解压")
    args = ap.parse_args()

    out = args.output or base_cache_dir()
    if args.force and out.exists():
        print(f"清理旧目录 {out} …")
        shutil.rmtree(out)

    def progress(_phase, frac):
        print(f"\r  解压中 {frac * 100:5.1f}%", end="", flush=True)

    result = ensure_base_unpacked(cache_dir=out, stage_cb=progress)
    print()
    if result is None:
        print("❌ 找不到分卷：仓库里应当自带 assets/terrain/base_z8.tar.gz.part*",
              file=sys.stderr)
        return 1
    if not is_base_ready(result):
        print(f"❌ 解压后校验不通过：{result}", file=sys.stderr)
        return 1
    n = sum(1 for _ in (result / "7").rglob("*.terrain")) if (result / "7").is_dir() else 0
    print(f"✅ 完成：{result}")
    print(f"   z7 瓦片 {n} 张（应为 32768）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`CLAUDE.md` 三处订正，给出逐字替换内容：

**第 134 行**，把开头到 `restore once with ...` 那一句换成（`Built from GEBCO 2024 at **z0–7** ...` 起的后半句原样保留）：

```markdown
  - Global base (low-zoom planet coverage): unpacked to `assets/terrain/base_z8/`, served at `/terrain/base/...`, and **grafted into every task's `terrain_tiles/`** (hardlinks; full copies across filesystems) so a task directory is self-contained and can be copied to another machine. **Ships with the repo** as split archives in `assets/terrain/base_z8.tar.gz.part{aa,ab}` (167 MB total — split because GitHub's single-file hard limit is 100 MB); `ensure_base_unpacked` in `terrain_tiling/base_terrain.py` unpacks it on the first tiling run, `scripts/unpack_base_terrain.py` is only a manual pre-warm / force-redo entry point. ⚠️ `nuitka_build.py` must keep `--noinclude-data-files=assets/terrain/base_z8/**` — `assets/terrain` is collected whole, so without it every build made on a machine that has tiled once ships an extra 224 MB / 43,690 files. Built from GEBCO 2024 at **z0–7**
```

**第 135 行**整行替换（parentUrl 现在只是兜底路径）：

```markdown
  - A task's `layer.json` is rewritten by `merge_base_availability`: `available` is the union of the base's z0–z7 and the task's z8+, `maxzoom = max(7, task maxzoom)`, and `parentUrl` is **removed** — the directory carries the base itself, and a `parentUrl` pointing at `localhost` 404s once the directory is copied elsewhere. `patch_layer_json_parent` / `parent_url_if_base_available` remain as the fallback for when the split archives are missing (see `docs/reference/terrain/cesiumjs-loading.md`).
```

**第 146 行**整行替换（原文那条从 0.2.4 起已不成立，见 `terrain_static.py:78-80`）：

```markdown
- `src/routes/terrain_static.py` enforces path-traversal safety through `_resolve_safe_file`: it locks the request-controlled `subpath` inside `base_dir`. `base_dir` itself comes from a DB task row or config and is **not** required to sit under `Config.DOWNLOADS_DIR` (that requirement was dropped in 0.2.4 — the global base now lives under `assets/`). Don't bypass `_resolve_safe_file`.
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run pytest tests/test_no_repo_pollution.py tests/test_fix_build_scripts.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add nuitka_build.py .gitignore scripts/unpack_base_terrain.py CLAUDE.md \
        tests/test_no_repo_pollution.py tests/test_fix_build_scripts.py
git commit -m "chore(build): 打包排除解压后的底图 + gitignore + CLI 瘦身 + 文档订正"
```

---

### Task 8: 端到端护栏 + 全量回归

**Files:**
- Modify: `tests/test_base_terrain.py`
- Test: 全量

- [ ] **Step 1: 写端到端护栏**

```python
def test_grafted_dir_is_self_contained(tmp_path, monkeypatch):
    """端到端：解压 → 植入 → 合成之后，目录能脱离本程序独立用。

    判据就是「拷走还能不能用」的三个必要条件：底图 z0-z7 的文件都在、
    layer.json 声明覆盖到 z0、且不含任何指向 localhost 的 parentUrl。
    """
    import json

    from src.services.terrain_tiling import base_terrain
    from src.services.terrain_tiling.layer_json import merge_base_availability

    src = _make_base(tmp_path / "src", layers=((0, 2), (1, 8), (2, 32)), dense=True)
    parts = _make_parts(tmp_path / "assets" / "terrain", src)
    monkeypatch.setattr(base_terrain, "_assets_terrain_dir", lambda: parts)
    monkeypatch.setattr(base_terrain, "_PROBE_LEVELS", ((0, 2), (1, 8), (2, 32)))

    base = base_terrain.ensure_base_unpacked()
    assert base is not None

    tiles = tmp_path / "task" / "terrain_tiles"
    tiles.mkdir(parents=True)
    (tiles / "layer.json").write_text(
        json.dumps({"maxzoom": 9, "available": [[]] * 9 + [
            [{"startX": 1, "startY": 1, "endX": 1, "endY": 1}]]}),
        encoding="utf-8")
    (tiles / "9" / "1").mkdir(parents=True)
    (tiles / "9" / "1" / "1.terrain").write_bytes(b"\x1f\x8btask")

    base_terrain.graft_base_into(tiles, base)
    merge_base_availability(tiles / "layer.json", base / "layer.json")

    for z, nx in ((0, 2), (1, 8), (2, 32)):
        for x in range(nx):
            assert (tiles / str(z) / str(x) / "0.terrain").is_file(), \
                f"z{z}/{x} 没植入 —— 拷走后这一块就是空的"
    assert (tiles / "9" / "1" / "1.terrain").read_bytes() == b"\x1f\x8btask"

    data = json.loads((tiles / "layer.json").read_text(encoding="utf-8"))
    assert "parentUrl" not in data
    assert data["available"][0], "z0 必须被声明，否则 Cesium 不去请求根瓦片"
    assert data["maxzoom"] == 9
```

- [ ] **Step 2: 跑它**

Run: `uv run pytest tests/test_base_terrain.py -q`
Expected: PASS

- [ ] **Step 3: 跑全量**

Run: `uv run pytest tests/ -q`
Expected: 全绿。基线是 v0.2.8 的 1192 项；新增用例后应为 1192 + 本次新增数。

**若有既有用例变红**，逐个核对而不是改断言迁就：
- `tests/test_layer_json.py::test_both_managers_gate_parent_url_on_base_availability` —— 源码级断言（两个 manager 里必须出现 `parent_url_if_base_available`）。兜底路径没删，应当仍绿；若红说明 Task 5 误删了兜底。
- `tests/test_dem_task_tiler.py` 的三条、`tests/test_terrain_stage_progress.py` 的穿透那条 —— 它们调用真 tiler，靠 Task 5 加的 `isolate_base_terrain` autouse fixture 走兜底路径。若红，先查那个 fixture 是不是没生效（现象往往是「这几个文件跑了几分钟」而不是断言失败），而不是去改它们的断言。
- **计时是判据之一**：全量应当仍在 6 分钟量级。若明显变长，多半是有测试在真解压 224 MB —— 用 `pytest --durations=10` 定位。

- [ ] **Step 4: 确认仓库没被污染**

Run: `uv run pytest tests/test_no_repo_pollution.py -q && git status --short`
Expected: PASS，且 `git status` 里不出现 `assets/terrain/base_z8`

- [ ] **Step 5: 提交**

```bash
git add tests/test_base_terrain.py
git commit -m "test(terrain): 底图植入的端到端自包含护栏"
```

---

## 完成后

改动是用户可见的行为变化（任务目录体积、首次切片多一段解压、产出可脱离程序使用），需要写进 `RELEASE_NOTES.md` 并 bump 版本。**这一步不在本计划内**，等实施完成后单独确认版本号与发版说明。
