# 全球基础地形（base terrain）

**2026-08-05 起 base 随仓库和安装包分发，不需要你自己构建。** 本文分两部分：先讲怎么用自带的那份，再讲什么情况下需要自己重建（本文原本只有后半部分）。

base 的作用是给单任务 DEM 切片当父层，让镜头拉远到 DEM 范围之外时仍有地形（见 [`cesiumjs-loading.md`](cesiumjs-loading.md)）。

## 一、用自带的那份

仓库里的 `assets/terrain/base_z8.tar.gz.part{aa,ab}` 是打包好的分卷。**正常情况下什么都不用做** —— 每次地形切片开头都会调 `ensure_base_unpacked()`（`src/services/terrain_tiling/base_terrain.py`），第一次跑时自动还原到 `assets/terrain/base_z8/`，之后幂等跳过；两个任务同时切片时后到的那个阻塞等待，不会重复解压。

想在第一次切片前先把那几分钟花掉，或者怀疑缓存坏了要强制重解，用手工入口：

```bash
uv run python scripts/unpack_base_terrain.py            # 预热
uv run python scripts/unpack_base_terrain.py --force    # 强制重解
```

脚本本身不含解压逻辑，全部委托给 `base_terrain`，默认目标也取自 `base_cache_dir()` —— 与配置键 `terrain_global_base_path` 的默认值 `./assets/terrain/base_z8` 解析到同一个目录（`tests/test_build_scripts_contract.py::test_base_cache_dir_matches_the_configured_path` 钉住这条一致性）。

从旧版本升级上来的库里那一行还是 `./downloads/terrain/base_z8`，由 `migrate_base_path_to_assets`（`PRAGMA user_version` 2 → 3）在启动时改写；旧位置已有完整底图时直接搬过去，不重解压。

自带这份的规格：

| 项 | 值 |
|---|---|
| 数据源 | GEBCO 2024（15 弧秒、含海底地形、全球无洞） |
| 层级 | **z0–7**（不是 z8 —— 目录名是历史命名，见下） |
| 顶点网格 | 65×65（与应用侧 `TileParams.tile_size` 一致） |
| 三角化 | `auto`（逐瓦片择优） |
| 法线 | 有（oct-encoded，开光照必需） |
| 瓦片数 / 体积 | 43690 张 / 226 MB（分卷 167 MB） |

**为什么是 z0–7 而目录仍叫 `base_z8`**：z8 一层就占 76% 的体积（131072 张 / 532 MB），而它的顶点间距 1.2 km 只在「贴近看 DEM 外围」时才用得上 —— 那种场景本就该切正式 DEM。z7 是 2.4 km，远景与中距离补充足够。目录名保持 `base_z8` 是因为它已经是配置默认值、文档与脚本里的既定名称，改名的波及面大于收益。

**为什么是分卷**：打包后 167 MB，而 GitHub 的单文件硬限制是 100 MB。分卷按字母序拼接即可还原（`cat part* > x.tar.gz` 的等价物），不需要专用工具。不直接提交 43690 个瓦片文件是因为 git 存全量历史且二进制不增量，那么多小文件会让 clone / status / checkout 明显变慢。

## 二、先读这三条（自己重建时同样适用）

1. **不建 base 是安全的**（2026-08-05 起）。切片时会检查 base 目录里有没有 `layer.json`，没有就**干脆不写 `parentUrl`**（`layer_json.parent_url_if_base_available`）。在此之前，没建 base 的装机会写出一个指向 404 的 `parentUrl`，而 Cesium 拿不到它时不报错、改塞一个假的 heightmap-1.0 图层并污染共享 builder —— 结果是**本任务自己的 quantized-mesh 瓦片也按 heightmap 解析**，实测 4154 m 的山峰解成海平面以下 744 m，且瓦片全 200、控制台无报错。所以「没还原 base」从来不是问题，**还原了一半或路径配错才是**。

2. **重建时记得带法线**。CLI 默认就写（`normals=True`），但 2026-08-05 之前切出来的 base 没有。而 Cesium 的 `hasVertexNormals` 是 **provider 级单一标志**、不是逐瓦片 —— 只要子层声明了法线，整个地球用同一个着色器分支，没有法线属性的 base 瓦片会读到缺失属性的默认值，**开光照后是近乎纯黑的楔形**（不是「平淡」）。

3. **级联在 z0–4 不生效**：即使构建成功，单任务切片会把 base 的低层级整个遮蔽掉，且不报错。原因和规避见 [`cesiumjs-loading.md`](cesiumjs-loading.md) 的 §3。

## 三、自己重建（换数据源 / 换层级 / 换覆盖范围时）

**本项目不提供全球 DEM 数据，也不会替你下载它** —— 这是整个构建里的主要成本（数据体积按数据源从几 GB 到上百 GB 不等，加上下载时间）。DEM 管线的下载器是按 bbox 抓 granule 的，不适合用来凑全球覆盖。

自带那份用的是 GEBCO 2024（CEDA 镜像，4 GB）。选它是因为其余候选当时都不可用：NOAA ETOPO 的所有已知路径 404、GMTED2010 的 USGS 域名连不上、OpenTopography 的 SRTM 只覆盖 60°N–56°S、AWS 上的 Copernicus 是 30 m（切 z0–8 过剩几百倍）。

⚠️ **下载提速**：CEDA 是**单连接限速**（实测 0.24 MB/s，4 GB 要 6.7 小时），8 路分段并行能到 1.38 MB/s（58 分钟）—— 瓶颈是每连接限速而非总带宽。

⚠️ **切片提速**：直接切 8 块 VRT 并输出到 WSL2 的 `/mnt/` 挂载，实测 3.3 张/秒后完全卡死。三处改动后到 1146 张/秒：① 先 `gdal.Warp` 把源降到 30 弧秒**单文件**（消除 VRT 跨块读取，且 z8 顶点间距 39.5″、15″ 本就过剩）；② 输出到本地文件系统而非 Windows 挂载；③ 层级不高时用 `--triangulator grid`（但**若要随包分发则用 `auto`** —— 见下）。

⚠️ **后端选 `auto` 不选 `grid`**：直觉上 base 大半是海洋、平坦瓦片「grid 恒胜」，但那是**法线关闭时**的结论。加法线后每顶点多 2 字节，grid 恒 4225 顶点（8450 B）而 martini 平坦瓦片只有 589 顶点（1178 B），结论反转 —— 实测同一份数据 grid 2.1 GB、auto 942 MB，**省 55% 且零质量损失**。

你需要准备的是一个目录，里面放全球 DEM 栅格文件。切片器接受的扩展名（`cesiumlab_terrain.main` 里展开目录用的那张后缀表）：`.tif` / `.tiff` / `.img` / `.hgt` / `.vrt`。

⚠️ **多文件输入会先被物化成一整份单文件副本**（`build_input_raster`，2026-08-05 起）。多源 VRT 上 GDAL 的 overview 选层会随读窗口漂移，实测开出 50.9 m 的瓦片接缝，所以多幅输入一律先合并成单个 GeoTIFF 再补一套 2 的幂 overview。

对全球数据这笔开销不小：GEBCO 2024 的 15 弧秒是 86400×43200 Int16（裸数据 7.5 GB），DEFLATE 压缩加金字塔后仍是数 GB，全部落在**输出目录的上一级**，切完才删。所以**别把全球 DEM 拆成很多小 granule 丢进来**——上面「切片提速」那条建议的做法（先 `gdal.Warp` 降到 30 弧秒**单文件**）同时也绕开了这笔开销：单文件输入直接走直通路径，一个字节都不复制。

层级选择决定了对源数据分辨率的要求：base 只服务低层级视角（惯例 maxzoom=8 —— 目录名 `base_z8` 与 ps1 脚本的默认值都是 8；切片器 CLI 本身没有这个默认，见下节）。z8 每片跨 360/512 = 0.703°，所以**用低分辨率的全球数据（如 30 弧秒级）就够了**，拿 30 m 数据切 z0–8 是纯粹浪费时间。

## 构建命令

切片器本身就是 CLI 入口（`src/services/terrain_tiling/cesiumlab_terrain.py` 的 `main()`）：

```bash
uv run python -m src.services.terrain_tiling.cesiumlab_terrain \
  -i /path/to/global_dem_dir \
  -o ./assets/terrain/base_z8 \
  --max-level 8 \
  --tile-size 65
```

参数（全部来自 `cesiumlab_terrain.main` 的 argparse 定义）：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--input` / `-i` | 必填 | 输入。可以是**目录**（自动展开上面那些扩展名）、单个文件，或含 `*?[` 的 glob。**可重复传**（`action="append"`）。多个输入会被物化成一份单文件副本（见上方 ⚠️），单个输入直通 |
| `--output` / `-o` | 必填 | 输出目录 |
| `--min-level` | `0` | 起始层级 |
| `--max-level` | `None` | 最高层级。**见下节：实践上必填** |
| `--tile-size` | `17` | 每瓦片顶点网格边长。**见下节：应用侧用的是 65** |
| `--nodata` | `None` | 覆盖源数据的 nodata 值；不传则用文件里声明的 |
| `--workers` / `-j` | `0` | 并行进程数。`0` 或负数 = `min(4, cpu_count())`（`build_terrain` 里定的上限，刻意封顶 4，每个 worker 都要开 GDAL dataset，进程太多会 OOM 被 OS 杀掉） |

运行需要装了 numpy + GDAL Python 绑定的 Python 环境（见 CLAUDE.md 的 GDAL 章节）。

## `--max-level` 实践上必填

**省略 `--max-level` 不会给你一个保守的默认值，而是按源数据像素尺寸自动估算一个层级**（`build_terrain` 在 `max_level is None` 时调 `GeographicTilingScheme.estimate_max_level`）。公式是 `ceil(log2((180/(tile_size-1)) / 源像素度数))` —— 也就是「一直细分到瓦片顶点间距追上源像素尺寸」。对全球数据这个数会大到没法接受：

| 源数据分辨率 | `--tile-size 65` 时估出的 max-level | z0..max 全球瓦片总数 |
|---|---|---|
| 30 弧秒（~1 km） | 9 | 约 70 万 |
| 15 弧秒（GEBCO） | 10 | 约 280 万 |
| 3 弧秒（SRTM） | 12 | 约 4470 万 |
| 1 弧秒（GLO-30 / ASTER） | 14 | 约 7.16 亿 |

（`--tile-size 17` 时每档还要再加 2 级，1 弧秒会估到 16 级、约 114 亿片。）

`estimate_max_level` 是给「切一小块 DEM」设计的，切全球时它没有任何全球感知。所以：**构建全球 base 一定要显式写 `--max-level`**，通常就是 8。

## 输出与配置

命令跑完，输出目录里是：

```
layer.json          # 含 bounds / valid_bounds / minzoom / maxzoom / available
meta.json           # minLevel / maxLevel / minHeight / maxHeight / bounds / tileSize
{z}/{x}/{y}.terrain # quantized-mesh-1.0
```

base 自己的 `layer.json` **不带** `parentUrl`（`patch_layer_json_parent` 只在单任务切片路径上调用），这是对的 —— base 就是链条的顶端。

**输出目录必须与配置键 `terrain_global_base_path` 一致**，默认 `./assets/terrain/base_z8`（`src/core/database.py` 的 `DEFAULT_CONFIGS`）。路由 `/terrain/base/<path>` 是拿这个配置值去磁盘找文件的（`src/routes/terrain_static.py`），目录对不上就是 404，没有自动发现。切片侧的 `base_terrain.base_cache_dir()` 是同一个落点，两处对不上的后果不止 404 —— 底图判为不可用后会退回 parentUrl 级联，而那个 URL 正指向服务空目录的 `/terrain/base`，Cesium 对这个 404 不报错，会塞一个假 heightmap 图层污染共享 builder，任务自己的瓦片高程也跟着全错。

相对路径的解析规则（`src/routes/terrain_static.py` 的 `_resolve_config_path`）：`./downloads/...` / `downloads/...` 开头挂到 `Config.DOWNLOADS_DIR`，其他相对路径挂到 `Config.BASE_DIR`，绝对路径原样使用。改配置最多 5 秒生效（路由层 5 秒 TTL 缓存），不用重启。

构建完的加载 URL：`http://localhost:5000/terrain/base/layer.json`

## 两个没有配置界面的键

`terrain_global_base_path` 和 `terrain_base_parent_url` 在配置页上**都没有输入框**（`templates/` 与 `static/` 里没有任何引用），只能通过 `PUT /api/config` 修改，或直接改数据库 `config` 表。默认值在 `src/core/database.py` 的 `DEFAULT_CONFIGS` 里这两个键上。

两者的生效方式**不一样**，别搞混：

- `terrain_global_base_path` —— 服务端路由每次请求读（带 5 秒缓存），**改完即时生效**。
- `terrain_base_parent_url` —— 在切片时被固化写进任务的 `layer.json`，**改完只影响之后新建的任务**；已有任务要么手改它的 `layer.json`，要么重新切片。默认值是应用内相对路径 `/terrain/base`，由浏览器继承提供 `layer.json` 的 origin，换端口/反代/远程访问都不用改它；只有指向另一套地形服务时才需要配完整 http(s) 地址。详见 [`cesiumjs-loading.md`](cesiumjs-loading.md) 的 §4。

## 关于 `scripts/build_global_base_terrain.ps1`

仓库里有一个 Windows PowerShell 封装：[`../../../scripts/build_global_base_terrain.ps1`](../../../scripts/build_global_base_terrain.ps1)。参数 `-DemDir`（必填）、`-MaxZoom`（默认 8）、`-OutDir`（默认 `.\downloads\terrain\base_z8`），做的事就是建目录 + 调上面那条命令。

参数 `-DemDir`（必填）、`-MaxZoom`（默认 8）、`-OutDir`（默认 `.\downloads\terrain\base_z8`）、`-TileSize`（默认 65）。做的事就是建目录 + 调上面那条命令：

```powershell
& uv run python -m src.services.terrain_tiling.cesiumlab_terrain `
    -i $DemDir -o $OutDir --max-level $MaxZoom --tile-size $TileSize
```

**2026-08-05 修了两处**（此前这一节描述的是修复前的形态）：

- **模块路径缺 `src.` 前缀**，src-layout 迁移后一直没跟着改，脚本跑起来直接 `ModuleNotFoundError` —— 也就是说它有一段时间是**完全不能用**的。更麻烦的是本文档当时引用的却是带 `src.` 的正确形态，拿文档去核对脚本只会得出「一切正常」。现已一致，并由 `tests/test_build_scripts_contract.py` 钉住（含「文档引用与脚本实际内容一致」这条）。
- **没传 `--tile-size`**，于是走 CLI 默认值 **17**，而应用侧是 **65**（`dem_task_tiler.py` 的 `TileParams.tile_size`）。base 的顶点网格每轴比子层稀疏 4 倍，级联切换时几何精度跳变。现在默认 65，且测试钉住它与 `TileParams.tile_size` 相等。

另外改用了 `uv run python`（原先是裸 `python`，要求调用者自己先激活装好 numpy+GDAL 的环境）。仍需留意：脚本**不校验** `-OutDir` 是否与 `terrain_global_base_path` 配置一致 —— 改了 `-OutDir` 就得同步改配置。
