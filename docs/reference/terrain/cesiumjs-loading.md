# CesiumJS 加载 Quantized-Mesh Terrain

本项目产出两类 quantized-mesh 地形：**全球低层级 base**（离线手工构建，可选）和**单任务 DEM 切片**（DEM 管线 / 本地地形管线产出）。本文讲客户端怎么加载它们，以及一个必须先知道的坑（§3）。

## 0) 前置条件

**只加载单任务切片不需要任何前置准备**，任务跑完即可用 §2 的 URL。

要用 §1 的全球 base，必须先满足两条：

1. **base 需要你自己离线构建** —— 仓库里不带地形数据。构建流程见 [`global-base-build.md`](global-base-build.md)。
2. **构建输出目录必须与配置键 `terrain_global_base_path` 一致**（默认 `./downloads/terrain/base_z8`）。`/terrain/base/...` 这条路由是拿这个配置值去磁盘找文件的（`routes/terrain_static.py:122`、`:158-165`），目录对不上就是 404，没有任何自动发现。

   路径解析规则（`routes/terrain_static.py:63-87` 的 `_resolve_config_path`）：绝对路径原样使用；`./downloads/...` 或 `downloads/...` 开头的相对路径挂到 `Config.DOWNLOADS_DIR` 下；其他相对路径挂到 `Config.BASE_DIR` 下。改了这个配置最多 5 秒生效（路由层有 5 秒 TTL 缓存），不用重启服务。

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

路由定义在 `routes/terrain_static.py`：`/dem/<int:task_id>/<path:subpath>`（:215）与 `/local/<int:task_id>/<path:subpath>`（:228）。

单任务切片的 `layer.json` 里带 `parentUrl` 指向全球 base，所以**只需要把单任务 provider 传给 Viewer**，不用手动加载两个 provider：

```js
new Cesium.Viewer("cesiumContainer", { terrainProvider: demOverlay });
```

## 3) ⚠️ 级联只在 z>4 生效，z0–4 被子层遮蔽且不报错

> 本节描述的是**待修的代码缺陷**，不是设计。代码修复后应删除本节。

**结论先说**：`parentUrl` 的级联只在 **zoom > 4** 时真正生效。**z0–4 永远不会请求 base**，Cesium 会用任务自己产出的垃圾瓦片渲染全球，表现为 bbox 之外一片被填成 0 的平坦假地形（或从 DEM 边缘拉伸出去的阶梯台地），**全程不抛错、不打日志、HTTP 全 200**，任务照样标 completed。

**机理**（`services/terrain_tiling/cesiumlab_terrain.py`）：

- 两条管线都以 `min_level=0` 调切片器（`services/terrain_tiling/dem_task_tiler.py:59`），所以每个任务都会产出 z0 起的瓦片 —— 哪怕它的 DEM 只有一小块。
- 切片器的 `_tile_ranges` 在 `z <= 4` 时**无条件取全球瓦片范围**，不与 DEM 实际 bbox 求交（`:448-449`）：

  ```python
  if z <= 4:
      x0, x1, y0, y1 = 0, nx - 1, 0, ny - 1
  else:
      x0, x1, y0, y1 = ix0, ix1, iy0, iy1
  ```

- `layer.json` 的 `available` 数组是用**同一个** `_tile_ranges` 生成的（`:462`），于是 `available[0..4]` 声明的是整个世界（2×1 / 4×2 / 8×4 / 16×8 / 32×16），`available[5]` 才收缩到 DEM 真实范围。
- Cesium 取「第一个 availability 声明可用的层」，而父层是排在子层之后才追加的 —— 子层在 z0–4 声称自己覆盖全球，**Cesium 就没有理由去请求 parentUrl**，base 的 z0–4 永远不会被取到。
- 那些全球瓦片的高程从哪来：`DemSampler.sample` 在采样窗口与 DEM 完全不相交时直接返回全 0（`:164`），部分相交时把采样坐标钳到 DEM 边缘像素（`:208-209` 的两处 `np.clip`），也就是把边缘那一行/列的高程沿法向拉出去。所以一块 4000 m 的高原在 z0 视角下会糊成横跨半个半球的阶梯台地。

**触发条件**：只在 `terrain_global_base_path` 指向的 base 目录**真实存在**时才看得出来。没构建 base 时是单层 provider，不存在「被遮蔽」这回事 —— 但那 682 片（z0–4 全球瓦片总数）无用瓦片的切片耗时、磁盘占用，以及 `meta.json` 里 `minHeight` 被 0 值污染恒为 0，是始终存在的代价。

**症状与误诊**：拉远看全球时地形明显不对 —— 该平的地方鼓起来、该有起伏的地方是平板。**此时不要去查 base 有没有构建成功、`/terrain/base/layer.json` 的 URL 对不对、CORS 通不通** —— 这些全都是正常的，根因就是上面这条。

完整分析与改法见 [`../../reviews/2026-08-03-full-project-review.md`](../../reviews/2026-08-03-full-project-review.md) 的 **M12** 条目。

**临时规避**：如果你只关心大范围视角的正确性，可以手工编辑任务的 `terrain_tiles/layer.json`，把 `available[0..4]` 五个元素改成 `[]`（空数组 = 该层无可用瓦片），Cesium 就会转而向 `parentUrl` 要 z0–4。这是绕过，不是修复 —— 重新切片会把它覆盖回去。

## 4) 配置键 `terrain_base_parent_url`：改了要重新切片

`parentUrl` 写的是哪个地址，由配置键 `terrain_base_parent_url` 决定，默认 `http://localhost:5000/terrain/base/layer.json`（`core/database.py:71`）。

**这个键在配置页上没有输入框**，只能通过 `PUT /api/config` 改，或者直接改数据库 `config` 表。

**改完必须重新切片才对已有任务生效。** `parentUrl` 是在切片收尾时被**固化写进** `layer.json` 的文件字段，不是运行时读配置：`services/terrain_tiling/dem_task_tiler.py` 在 `build_terrain(...)` 返回后调用 `patch_layer_json_parent(layer_json_path, params.parent_url)`，而 `patch_layer_json_parent`（`services/terrain_tiling/layer_json.py`）只做一件事 —— 把 `data["parentUrl"] = parent_url` 写回文件。配置值是任务**启动切片那一刻**从 `ConfigManager` 读的（`services/dem_task_manager.py:298`、`services/local_terrain_task_manager.py:40`），之后再改配置不会回头动已经写好的文件。

所以三种做法，按代价从低到高：

1. 手工改已有任务 `terrain_tiles/layer.json` 里的 `parentUrl` 字段（一行 JSON，立即生效）；
2. 改配置，只对之后新建的任务生效；
3. 改配置后重新切片旧任务。

**什么时候必须改**：服务不跑在 `localhost:5000`（换端口、部署到内网 IP 或域名）。默认值在客户端解析不到时，级联会**静默失败** —— Cesium 只是拿不到父层数据，同样不报错。
