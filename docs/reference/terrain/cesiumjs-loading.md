# CesiumJS 加载 Quantized-Mesh Terrain

本项目产出两类 quantized-mesh 地形：**全球低层级 base**（随包分发，自动解压）和**单任务 DEM 切片**（DEM 管线 / 本地地形管线产出）。本文讲客户端怎么加载它们，以及一个至今仍会咬人的坑：`parentUrl` 必须是**目录**（§4）。§3 记的是一个已经修掉的旧缺陷，留着是为了拦住照旧症状排查的人。

## 0) 前置条件

**只加载单任务切片不需要任何前置准备**，任务跑完即可用 §2 的 URL —— 而且从「底图随任务植入」这版起，§2 那个目录里**已经包含了 z0–z7 的全球底图**，可以整个拷到别的机器上当地形源用，不需要本程序、也不需要 §1。

§1 的全球 base 路由仍然保留（历史页的地形预览直接用它）。要用它：

1. **底图随仓库/安装包分发**（`assets/terrain/base_z8.tar.gz.part{aa,ab}`），首次地形切片时自动解压，不需要自己构建。想提前预热或强制重解见 [`global-base-build.md`](global-base-build.md)。自建一份全球 base（比如换数据源、换层级）也走那篇。
2. **解压落点必须与配置键 `terrain_global_base_path` 一致**（默认 `./assets/terrain/base_z8`）。`/terrain/base/...` 这条路由是拿这个配置值去磁盘找文件的（`src/routes/terrain_static.py`），目录对不上就是 404，没有任何自动发现。默认路径下两边由 `base_terrain.base_cache_dir()` 与 `DEFAULT_CONFIGS` 各自给出，一致性有测试钉住；自己改过配置就得自己保证。

   路径解析规则（`src/services/task_cleanup.py` 的 `resolve_stored_output_dir`）：绝对路径原样使用；`./downloads/...` 或 `downloads/...` 开头的相对路径挂到 `Config.DOWNLOADS_DIR` 下；其他相对路径（含默认的 `./assets/...`）挂到 `Config.BASE_DIR` 下。改了这个配置最多 5 秒生效（路由层有 5 秒 TTL 缓存），不用重启服务。

## 1) Base terrain provider

```js
const baseTerrain = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:5000/terrain/base/layer.json"
);
```

## 2) 单任务 DEM provider

两条管线的 URL 形态不同，产物格式一样：

```js
// DEM 管线（下载 Copernicus / ASTER 后切片），任务 id = 1
const demOverlay = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:5000/terrain/dem/1/layer.json"
);

// 本地地形管线（上传自己的 GeoTIFF 后切片），任务 id = 1
const localOverlay = await Cesium.CesiumTerrainProvider.fromUrl(
  "http://localhost:5000/terrain/local/1/layer.json"
);
```

路由定义在 `src/routes/terrain_static.py`：`terrain_dem_static`（`/dem/<int:task_id>/<path:subpath>`）与 `terrain_local_static`（`/local/<int:task_id>/<path:subpath>`）。

**默认路径下单任务目录是自包含的**：切完之后 `tile_dem_task_dir` 把随包底图**物理植入**任务目录（`graft_base_into` + `merge_base_availability`），`available` 是底图 z0–z7 与任务 z8+ 的并集，`parentUrl` 被**删掉** —— 目录里已经有底图了，再指一个 `localhost` 地址只会在目录被拷到别的机器上时 404。只有随包底图不可用（`assets/terrain/*.part` 被删）退回兜底路径时，才由 `patch_layer_json_parent` 写 `parentUrl` 指向全球 base。两种情况下都**只需要把单任务 provider 传给 Viewer**：

```js
new Cesium.Viewer("cesiumContainer", { terrainProvider: demOverlay });
```

## 3) z0–4 的级联：历史上被子层遮蔽，现已消解（两条路径都是）

> **状态：已修。本节保留下来，是为了让照着旧症状排查的人及时停下。** 历史上每个任务的 `layer.json` 在 z0–4 声明「我有全球地形」，而 Cesium 取「第一个 availability 声明可用的层」、父层排在子层之后 —— 于是 base 的 z0–4 永远不会被请求，低层级被任务自己那 682 片垃圾瓦片整个盖掉，**全程 HTTP 200、不抛错、不打日志**，任务照样 completed。完整分析见 [`../../reviews/2026-08-03-full-project-review.md`](../../reviews/2026-08-03-full-project-review.md) 的 **M12** 条目。

现在两条路径都不再有这个现象，机理不同：

- **默认路径（随包底图可用）**：任务只切 z8 起的层级（`tile_dem_task_dir` 里的 `min_level = 8 if base_dir is not None else 0`，`build_terrain` 再把它钳到实际 `max_level` 以下），z0–z7 由植入进任务目录的底图瓦片提供，`available` 是两者的并集，`parentUrl` 已被删除 —— 那 682 片垃圾瓦片根本不生成，也没有级联可言。
- **兜底路径（底图不可用）**：任务仍从 z0 起出图（根瓦片缺失会让 Cesium 的单层 provider 路径直接 404），但**声明与出图已经分家**：`build_terrain` 用 `well_covered_tile_range` 把 `available` 收窄到与 DEM 真正相交、且首尾行列覆盖率不低于 `_MIN_TILE_AXIS_COVERAGE`（0.25）的范围，整层都不够格就声明 `[]`。子层不再声称覆盖全球，Cesium 于是会照常向 `parentUrl` 要 z0–4。

**兜底路径上仍要付的代价**（只是不再表现成错误地形）：那 682 片瓦片的切片耗时与磁盘占用照付；`meta.json` 的 `minHeight` 也仍被它们拉平 —— 它按**所有出图瓦片**聚合，与 `available` 声明与否无关（`DemSampler.sample` 在采样窗口与 DEM 完全不相交时返回全 0，部分相交时用 `np.clip` 把坐标钳到边缘像素，把边缘那一行/列沿法向拉出去）。

**症状与误诊**：拉远看全球时地形仍然明显不对（该平的地方鼓起来、该有起伏的地方是平板），**不要**再去查 base 有没有构建成功、`/terrain/base/layer.json` 通不通、CORS 通不通 —— 先看 §4 的「必须是目录」那一条，`parentUrl` 写成 `.../layer.json` 是现在最常见的成因，症状与本节的旧缺陷几乎一模一样。

## 4) 配置键 `terrain_base_parent_url`：改了要重新切片

`parentUrl` 写的是哪个地址，由配置键 `terrain_base_parent_url` 决定，默认 `http://localhost:5000/terrain/base`（`src/core/database.py` 的 `DEFAULT_CONFIGS`）。

⚠️ **这个值必须是目录，绝不能带 `/layer.json`。** Cesium 会先 `appendForwardSlash()` 再拼 `layer.json`，写成 `.../base/layer.json` 就变成请求 `.../base/layer.json/layer.json` → 404；而 Cesium 对这个 404 **不报错**，它塞一个假的 heightmap-1.0 图层，并把 `heightmapStructure` 写在**共享的** builder 上 —— 于是任务自己的 quantized-mesh 瓦片也按 heightmap 解析。实测（天山 N42E086，同一批瓦片只改这一个值）：4154 m 的山峰解成 **−744 m**，而 `hasVertexNormals` 仍报 true、瓦片全 200、控制台一条错都没有。写入侧由 `layer_json.normalize_parent_url` 剥掉这个后缀，但**手工改任务 `layer.json` 绕不过这一关** —— 那是直接改产物文件，没有任何东西替你规整。

**这个键在配置页上没有输入框**，只能通过 `PUT /api/config` 改，或者直接改数据库 `config` 表。

**改完必须重新切片才对已有任务生效**（且只对兜底路径有意义 —— 默认路径根本不写 `parentUrl`，见 §2）。`parentUrl` 是在切片收尾时被**固化写进** `layer.json` 的文件字段，不是运行时读配置：`tile_dem_task_dir` 在 `build_terrain(...)` 返回后、底图不可用的分支里调用 `patch_layer_json_parent(layer_json_path, params.parent_url)`，而 `patch_layer_json_parent`（`src/services/terrain_tiling/layer_json.py`）只做一件事 —— 把规整后的 `parentUrl` 写回文件（值为 None 时改为删除该字段）。配置值是任务**启动切片那一刻**从 `ConfigManager` 读的，两条管线各有一处：`DemTaskManager.start_tiling` 与 `local_terrain_task_manager._parent_layer_url`，两处都过 `parent_url_if_base_available`（底图目录里没有 `layer.json` 就返回 None，宁可不写也不写一个 404 的）。之后再改配置不会回头动已经写好的文件。

所以三种做法，按代价从低到高：

1. 手工改已有任务 `terrain_tiles/layer.json` 里的 `parentUrl` 字段（一行 JSON，立即生效）—— 记住上面那条：写目录，不写 `/layer.json`；
2. 改配置，只对之后新建的任务生效；
3. 改配置后重新切片旧任务。

**什么时候必须改**：服务不跑在 `localhost:5000`（换端口、部署到内网 IP 或域名）。默认值在客户端解析不到时，级联会**静默失败** —— Cesium 只是拿不到父层数据，同样不报错。
