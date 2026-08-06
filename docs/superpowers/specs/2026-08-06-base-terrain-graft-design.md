# 全球底图随任务植入：地形切片产出自包含目录

> **状态**：设计已批准，待实施 ｜ **记录时间**：2026-08-06
> 前置版本：v0.2.8（`716c45f0d`）

## 结论先行

地形切片的产出目录改成**自包含**：切片前把随包分发的全球底图解压到共享缓存，切片后把底图的 z0–z7 植入任务目录，合成一份完整的 `layer.json`。**整个目录可以拷走，在不装本程序的机器上直接当地形源用。**

三条已定的取舍：

| 取舍 | 决定 | 理由 |
|---|---|---|
| 重叠层级 | 底图独占 z0–z7，任务只切 z8+ | 分层无接缝；z7 顶点间距约 2.4 km，DEM 在这个尺度体现不出精度，只会在 DEM 边界留断崖 |
| 底图怎么进任务目录 | 共享缓存 + 硬链接，跨盘回退实体复制 | 目录内容与实体文件无区别（tar/zip/cp 都会展开成独立文件），但磁盘只占一份 |
| 底图缺失时 | 退回现有的 parentUrl 级联 | 分卷随包分发，缺失只在有人手动删了 `assets/` 时发生；保留兜底比报错友好 |

## 背景

### 现在不是融合，是级联

任务的 `layer.json` 写一个 `parentUrl` 指向 `/terrain/base`，Cesium 自己去父层取低层级瓦片（`src/services/terrain_tiling/layer_json.py`）。底图只有一份，所有任务共用，**任务目录本身不完整** —— 拷到别处就只剩 DEM 覆盖的那一小块，且 `parentUrl` 里写死的 `localhost:5000` 在别处必然 404。

Cesium 对这个 404 不报错，而是塞一个假的 heightmap-1.0 图层，并把 `heightmapStructure` 写在**共享的 builder** 上，于是任务自己的 quantized-mesh 瓦片也被按 heightmap 解析。实测 4154 m 山峰解成海平面以下 744 m，页面零报错（v0.2.8 修过这条，修法是「base 不可达就不写 parentUrl」）。

### 打包版的死角

v0.2.8 首次把底图分卷打进安装包（`assets/terrain/base_z8.tar.gz.part{aa,ab}`，90 + 77 MB），但还原脚本 `scripts/unpack_base_terrain.py` 没进 dist，而且 exe 用户也没有 Python 环境。结果是每个平台的下载包里躺着 167 MB 用不上的数据，发版说明只能给手工 `cat` / `copy /b` + `tar` 的命令顶着。

本设计把解压逻辑移进 `src/`，由切片流程自动触发，这个死角一并消掉。

### 底图规格（实测）

| 项 | 值 |
|---|---|
| 分卷 | `partaa` 90 MB + `partab` 77 MB |
| 解压后 | 224 MB / 43,690 个 `.terrain` |
| 层级 | z0–z7 全球（z0:2 / z1:8 / z2:32 / z3:128 / z4:512 / z5:2048 / z6:8192 / z7:32768） |
| 数据源 | GEBCO 2024，含海底地形，带逐顶点法线 |
| 切片方案 | EPSG:4326 TMS quantized-mesh，与任务瓦片同一套 `GeographicTilingScheme` |

同一套切片方案是文件级合并成立的前提 —— 两边的 `{z}/{x}/{y}` 坐标含义完全一致，不需要重投影。

## 架构

```
assets/terrain/*.part          分卷（随包）
        │ ensure_base_unpacked   幂等 · 跨进程锁 · 临时目录 + 原子改名
        ▼
downloads/terrain/base_z8      共享缓存 224 MB（也是 /terrain/base 的服务目录）
        │ graft_base_into       硬链接（跨盘回退 copy2）· skip-if-exists
        ▼
<任务输出>/terrain_tiles/       ← build_terrain(min_level=8) 写入 z8+
        │ merge_layer_json      available 并集 · 无 parentUrl
        ▼
        自包含地形源
```

执行顺序是**解压 → 切片 → 植入**。解压排在切片前有两个实打实的理由，不是随手排的：

1. 首次解压是分钟级（Windows 上 4.3 万个小文件尤其慢），必须独占 `stage_cb` 上报通道，否则和切片进度抢同一条通道，前端只能看到进度条来回跳。
2. `min_level` 的取值依赖「底图到底可不可用」，而可用性判据是本地磁盘上有没有 `layer.json` —— 解压必须先于这个判断。

## 组件

新增 `src/services/terrain_tiling/base_terrain.py`。**纯文件操作，不 import GDAL / numpy** —— 这样它能在没有 GDAL 的环境里单测，和 `layer_json.py` 的定位一致。

### `base_parts_dir() -> Path | None`

定位分卷目录：打包版走 `src/core/bundle.py:bundle_dir() / "assets" / "terrain"`，源码运行走仓库根的 `assets/terrain`。找不到分卷返回 `None`（= 底图不可用，走 parentUrl 兜底）。

### `ensure_base_unpacked(cache_dir: Path, stage_cb=None) -> Path | None`

幂等解压。

- **就位判据**沿用现有 `scripts/unpack_base_terrain.py:already_ok`：`layer.json` 在，且 z0/z4/z7 的顶层 x 目录数分别 ≥ 2/32/256。只看 `layer.json` 不够 —— 解压中途被打断也会留下它，而一个 `layer.json` 齐全但瓦片残缺的底图会让 Cesium 拿到 404 瓦片。
- **跨进程锁**：两个任务同时切片时，后到的**阻塞等待**而不是重复解压。锁粒度 = 缓存目录。⚠️ 现成的 `src/core/single_instance.py:acquire_instance_lock` 是**非阻塞**的（`LK_NBLCK` / `LOCK_NB`，抢不到返回 False），语义不对，不能直接复用 —— 要的是阻塞版。但它的**平台分支写法必须照抄**：Windows 的 `msvcrt.locking` 锁的是当前文件指针处的 N 个字节而非整个文件，加锁解锁都必须先 `seek(0)`，v0.2.5 就是在这上面栽的（两个实例锁到不同字节区间，互斥完全失效，Windows CI 实测抓到）。
- **原子性**：解到 `cache_dir.parent` 下的临时目录，校验通过后再 `rename` 到位。中断留下的临时目录由启动清扫回收。
- **进度**：`stage_cb('base_unpack', fraction)`，按已解压字节数 / 分卷总字节数估算。

### `graft_base_into(tiles_dir: Path, base_dir: Path) -> dict`

把底图的 z0–z7 植入任务目录，返回 `{'linked': n, 'copied': n, 'skipped': n}`。

- **策略一次性决定**：先在 `tiles_dir` 里对底图的一个探针文件试 `os.link`，成功则整批硬链接，失败（`EXDEV` 跨文件系统 / `EPERM` / 文件系统不支持）则整批 `shutil.copy2`。**不做 4.3 万次逐个 try** —— 逐个 try 的异常开销在 Windows 上是可测量的，而策略在同一个目标目录里不会中途改变。
- **skip-if-exists**：任务已经写过的瓦片不被覆盖。正常情况零冲突（任务 z8+，底图 z0–7），这条规则是为了兜住 `maxzoom < 8` 的退化任务 —— 那时任务和底图在同一层相撞，任务的 DEM 数据必须胜出。
- **失败即失败**：植入过程中任何 `OSError`（最可能是磁盘满）都向上抛，并回滚本次已植入的文件。半个底图比没有更糟：缺的那些瓦片会让 Cesium 拿到 404，正是上面说的那条静默降级路径。

### `merge_layer_json(task_layer: Path, base_layer: Path, out: Path) -> None`

- `available`：逐层取并集。底图给 z0–z7 的全球声明，任务给 z8..maxzoom 的相交声明。层数不等时以更深的为准，缺的层补空列表。
- `minzoom = 0`，`maxzoom = max(7, 任务 maxzoom)`。**不能直接取任务的** —— `maxzoom < 8` 的退化任务里底图的 z6/z7 比任务更深，写任务的 maxzoom 会把底图最深两层声明掉，Cesium 不再请求它们。
- **删掉 `parentUrl`**：自包含之后它是一次多余的请求，而且指向 `localhost` 在拷走后必然 404。
- 其余字段（`format` / `scheme` / `projection` / `tiles` / `bounds`）以任务的为准，两边本来就一致。

### `scripts/unpack_base_terrain.py`

改成 `base_terrain` 模块的薄 CLI 包装。逻辑只留一份，手工还原的escape hatch 保留。

## 切片流程的改动

### `TileParams` 新增 `min_level: int = 0`

默认 0 保持现有语义。两个 manager（`dem_task_manager.py` / `local_terrain_task_manager.py`）在底图可用时传 `min(8, maxzoom)`。

`min(8, maxzoom)` 而不是死写 8：`maxzoom < 8` 的任务如果 `min_level=8 > max_level`，`_tile_ranges()` 会产出空区间，任务切出零张瓦片却报 completed —— 又一款静默成功。

### `tile_dem_task_dir` 的新顺序

```
ensure_base_unpacked()            → base_dir | None
build_terrain(min_level=…)        → 任务瓦片 + 任务 layer.json
base_dir 有值:  graft_base_into() + merge_layer_json()
base_dir 为 None: patch_layer_json_parent()   ← 现有兜底路径不动
```

### 保留不动

`/terrain/base` 路由与共享缓存**保留** —— `static/js/history.js:727` 直接用 `${location.origin}/terrain/base/layer.json` 做历史页的地形预览。`parentUrl` 相关代码（`normalize_parent_url` / `parent_url_if_base_available` / `patch_layer_json_parent`、配置项 `terrain_base_parent_url`）在底图可用路径上停用，但底图缺失时它们是唯一兜底，不删。

## 磁盘账

| 项 | 硬链接可用 | 跨盘回退 |
|---|---|---|
| 共享缓存 | 224 MB（一次性） | 224 MB（一次性） |
| 每个任务目录 | 4.3 万个目录项，数据 0 字节 | +224 MB |
| 10 个任务 | 224 MB | 2.4 GB |

DEM 任务的输出路径是用户自选的全盘路径，跨盘是常态而不是例外，所以回退分支必须当作主路径来测。

## 测试清单

护栏而不是覆盖率。每条都要能在改坏时变红：

| 用例 | 钉住什么 |
|---|---|
| `os.link` 抛 `OSError(EXDEV)` → 必须回退复制，且内容与源逐字节一致 | 跨盘分支不是死代码 |
| 已就位时不重复解压（打桩计数解压调用） | 幂等 |
| 半个底图（删掉 z7 的一半 x 目录）不被判为就位 | 中断的解压不会被误认 |
| 植入中途 `OSError` → 抛出且已植入文件被回滚 | 不留半个底图 |
| 任务已有的瓦片不被底图覆盖 | skip-if-exists |
| 合成后的 `layer.json`：available 是并集、maxzoom 正确、**不含 parentUrl** | 合成契约 |
| `maxzoom=5` 的任务：`min_level` 取 5 而不是 8，切出的瓦片数 > 0 | 退化任务不静默切零张 |
| 底图不可用（`base_parts_dir()` 返回 None）→ 走 parentUrl 兜底，行为与 v0.2.8 一致 | 兜底路径没被改坏 |
| 植入后目录自包含：z0–z7 全在，layer.json 声明覆盖全球 | 端到端契约 |

## 排除的方案

### 真融合（把底图解码回高程栅格，与 DEM 合成一张再重切）

唯一能做到「低层级也带 DEM 细节且完全无缝」的做法。排除理由：现在只有 quantized-mesh 编码器，要新写解码器；且每个任务都要重切全球 z0–z7 共 4.3 万张，切片时间从分钟级升到小时级。换来的是 z≤7 上肉眼看不出的差别（z7 顶点间距 2.4 km）。

### 每个任务直接从分卷解压一遍

用户最初的提法。产出与硬链接方案完全相同，但每个任务多付 224 MB 磁盘 + Windows 上分钟级的 4.3 万小文件写入。硬链接在「拷走后自包含」这个目标上没有任何损失 —— tar 会把它们存成归档内的链接（解出来仍是完整文件），zip 和 `cp -r` 直接展开成独立文件。

### 复刻现有级联的取舍规则（任务在 available 范围内胜出）

渲染结果与升级前一模一样。排除理由：z5–z7 与 DEM 相交的那几张瓦片仍然是「少半真数据 + 大半外推」—— 一张 z5 瓦片跨 5.6°×5.6°，典型 1° DEM 只占 3%，其余是采到 DEM 外的钳位值，在 DEM 边界处留一道断崖。既然要重做，就把这个陈年问题一起解决。

## 已知代价

- 首次切片多等一次解压（分钟级，有进度上报）。
- 任务目录的文件数从「DEM 覆盖范围的瓦片数」涨到「+43,690」。删除任务时要删的文件数同步上涨，`remove_task_dir_if_safe` 的耗时会变长（但它本来就是递归删目录，量级问题不是逻辑问题）。
- 硬链接让「任务目录占多少磁盘」这个问题变得不直观：`du` 对同一个 inode 只算一次，用户看到的数字会随统计顺序变化。这是硬链接的固有语义，不打算掩盖。
