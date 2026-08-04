# 全球基础地形（base terrain）离线构建

全球 base 是**可选**产物：只加载单任务 DEM 切片不需要它；它的作用是给单任务切片当父层，让镜头拉远到 DEM 范围之外时仍有地形（见 [`cesiumjs-loading.md`](cesiumjs-loading.md)）。

**先读这条再动手**：即使构建成功，级联在 **z0–4 也不生效** —— 单任务切片会把 base 的低层级整个遮蔽掉，且不报错。原因和规避见 [`cesiumjs-loading.md`](cesiumjs-loading.md) 的 §3。先了解这个限制再决定要不要花时间构建。

## 前置：自备一份全球 DEM

**本项目不提供全球 DEM 数据，也不会替你下载它** —— 这是整个构建里的主要成本（数据体积按数据源从几 GB 到上百 GB 不等，加上下载时间）。DEM 管线的下载器是按 bbox 抓 granule 的，不适合用来凑全球覆盖。

你需要准备的是一个目录，里面放全球 DEM 栅格文件。切片器接受的扩展名（`cesiumlab_terrain.py:616`）：`.tif` / `.tiff` / `.img` / `.hgt` / `.vrt`。多文件会自动拼成一个临时 VRT 再切，所以按 granule 切成很多小文件也可以。

层级选择决定了对源数据分辨率的要求：base 只服务低层级视角（惯例 maxzoom=8 —— 目录名 `base_z8` 与 ps1 脚本的默认值都是 8；切片器 CLI 本身没有这个默认，见下节）。z8 每片跨 360/512 = 0.703°，所以**用低分辨率的全球数据（如 30 弧秒级）就够了**，拿 30 m 数据切 z0–8 是纯粹浪费时间。

## 构建命令

切片器本身就是 CLI 入口（`src/services/terrain_tiling/cesiumlab_terrain.py` 的 `main()`）：

```bash
uv run python -m src.services.terrain_tiling.cesiumlab_terrain \
  -i /path/to/global_dem_dir \
  -o ./downloads/terrain/base_z8 \
  --max-level 8 \
  --tile-size 65
```

参数（全部来自 `cesiumlab_terrain.py:601-608` 的 argparse 定义）：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--input` / `-i` | 必填 | 输入。可以是**目录**（自动展开上面那些扩展名）、单个文件，或含 `*?[` 的 glob。**可重复传**（`action="append"`），多个输入自动合成 VRT |
| `--output` / `-o` | 必填 | 输出目录 |
| `--min-level` | `0` | 起始层级 |
| `--max-level` | `None` | 最高层级。**见下节：实践上必填** |
| `--tile-size` | `17` | 每瓦片顶点网格边长。**见下节：应用侧用的是 65** |
| `--nodata` | `None` | 覆盖源数据的 nodata 值；不传则用文件里声明的 |
| `--workers` / `-j` | `0` | 并行进程数。`0` 或负数 = `min(4, cpu_count())`（`:483-488`，刻意封顶 4，每个 worker 都要开 GDAL dataset，进程太多会 OOM 被 OS 杀掉） |

运行需要装了 numpy + GDAL Python 绑定的 Python 环境（见 CLAUDE.md 的 GDAL 章节）。

## `--max-level` 实践上必填

**省略 `--max-level` 不会给你一个保守的默认值，而是按源数据像素尺寸自动估算一个层级**（`:434-435` → `GeographicTilingScheme.estimate_max_level`，`:85-95`）。公式是 `ceil(log2((180/(tile_size-1)) / 源像素度数))` —— 也就是「一直细分到瓦片顶点间距追上源像素尺寸」。对全球数据这个数会大到没法接受：

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

**输出目录必须与配置键 `terrain_global_base_path` 一致**，默认 `./downloads/terrain/base_z8`（`src/core/database.py:68`）。路由 `/terrain/base/<path>` 是拿这个配置值去磁盘找文件的（`src/routes/terrain_static.py:122`、`:158-165`），目录对不上就是 404，没有自动发现。

相对路径的解析规则（`src/routes/terrain_static.py:63-87` 的 `_resolve_config_path`）：`./downloads/...` / `downloads/...` 开头挂到 `Config.DOWNLOADS_DIR`，其他相对路径挂到 `Config.BASE_DIR`，绝对路径原样使用。改配置最多 5 秒生效（路由层 5 秒 TTL 缓存），不用重启。

构建完的加载 URL：`http://localhost:5000/terrain/base/layer.json`

## 两个没有配置界面的键

`terrain_global_base_path` 和 `terrain_base_parent_url` 在配置页上**都没有输入框**（`templates/` 与 `static/` 里没有任何引用），只能通过 `PUT /api/config` 修改，或直接改数据库 `config` 表。默认值在 `src/core/database.py` 的 `DEFAULT_CONFIGS`（`:68`、`:71`）。

两者的生效方式**不一样**，别搞混：

- `terrain_global_base_path` —— 服务端路由每次请求读（带 5 秒缓存），**改完即时生效**。
- `terrain_base_parent_url` —— 在切片时被固化写进任务的 `layer.json`，**改完只影响之后新建的任务**；已有任务要么手改它的 `layer.json`，要么重新切片。详见 [`cesiumjs-loading.md`](cesiumjs-loading.md) 的 §4。

## 关于 `scripts/build_global_base_terrain.ps1`

仓库里有一个 Windows PowerShell 封装：[`../../../scripts/build_global_base_terrain.ps1`](../../../scripts/build_global_base_terrain.ps1)。参数 `-DemDir`（必填）、`-MaxZoom`（默认 8）、`-OutDir`（默认 `.\downloads\terrain\base_z8`），做的事就是建目录 + 调上面那条命令。

**它与应用侧参数有一处实际差异，用之前先知道**：脚本调用的是

```powershell
& python -m src.services.terrain_tiling.cesiumlab_terrain -i $DemDir -o $OutDir --max-level $MaxZoom
```

**没有传 `--tile-size`**，于是走 CLI 默认值 **17**；而应用侧切单任务 DEM 时用的是 **65**（`src/services/terrain_tiling/dem_task_tiler.py:24` 的 `TileParams.tile_size = 65`）。结果是 base 的瓦片顶点网格每轴比子层稀疏 4 倍（17×17 对 65×65），同一层级下两者的几何精度不一致，级联切换时可能看到明显的细节跳变。

想让 base 与应用侧一致，要么手工在脚本第 23 行补上 `--tile-size 65`，要么直接跑上面那条完整命令。

脚本还有两点要留意：它调的是裸 `python`（不是 `uv run python`），所以必须在已激活的、装好 numpy+GDAL 的环境里执行；它也不校验 `-OutDir` 是否与 `terrain_global_base_path` 配置一致 —— 改了 `-OutDir` 就得同步改配置。
