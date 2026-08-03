# GeoLibre 调研：可借鉴点

来源：2026-08-03 对 [opengeos/GeoLibre](https://github.com/opengeos/GeoLibre) 的浅克隆调研（commit `483c663`），每条建议均已对照本仓库代码核实。

GeoLibre 的定位是「浏览器里的查看/分析平台」（Tauri + React + MapLibre + DuckDB-WASM + Whitebox WASM，1000+ 浏览器端地理处理工具），与本项目「Flask + SocketIO 下载/生产管线」是不同物种——**借鉴点是具体机制，不是架构**。

## 已核实不采纳：Terrarium 免登录 DEM 源

GeoLibre 的 3D 地形用 Mapzen Terrarium 公开瓦片（`packages/map/src/map-controller.ts:88`，`s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`，maxzoom 15，无鉴权，解码公式 `R*256 + G + B/256 - 32768`）。

初判曾建议引入作 DEM 第二数据源，**核实本仓库后撤回**：`services/dem_task_manager.py:100` 的默认数据集已是 `COP-DEM-GLO-30`——AWS 公开桶 `copernicus-dem-30m.s3.amazonaws.com` 免签名（`services/dem_download_engine.py:59-67`），30m 分辨率、全球覆盖，质量优于 Terrarium（混合源、z15 封顶）。

残余价值仅一点：Terrarium 按 XYZ 金字塔取数，小区块不必整下载 1°×1° 的 COG 颗粒。记录于此，避免将来重复评估。

## 1. 共享缓存的安全清理（最有价值）

来源：GeoLibre `apps/geolibre-desktop/src/lib/offline-regions.ts`

对应问题：本项目瓦片 cache（`cache/<style>/<z>/<x>/<y>.png`）跨任务共享，删除任务时 `delete_files` 只敢删任务目录（`services/task_cleanup.py`），共享 cache 没有安全清理手段。

可借鉴机制：

- **确定性 region id**：`bbox(四位小数)@minZoom-maxZoom`，重复下载同一区域自动变成更新而非重复记录；
- **资源分类记录**：`tileUrls`（区域专属瓦片）与 `assetUrls`（共享资产）分开存，删除时共享资产永不清除；
- **只删独占瓦片**：`exclusiveTileUrls()` 计算「没有其他区域引用」的瓦片集合，重叠区域互不影响；
- **体积统计**：`measureRegionBytes()` 先读 Content-Length 头，缺失才读 body，避免为算体积反序列化大量瓦片。

落点：给共享 cache 加一份按任务记录瓦片坐标集合的清单；删除任务时可安全清理其独占瓦片，并能在 history 页展示区域占用体积。

→ 落地方案已展开：`docs/cache-exclusive-cleanup-plan.md`（核实后结论：本项目的任务表本身就是 manifest，无需另建清单，独占集可按 bbox+zoom 纯函数重建）。

## 2. MBTiles 导出

来源：GeoLibre `apps/geolibre-desktop/src/lib/mbtiles.ts`（消费端：MapLibre `addProtocol` 自定义协议直读 MBTiles）。

本项目现有产物是松散 PNG 瓦片 + GDAL 拼 GeoTIFF。MBTiles 可作为第三种产物形态：单文件、QGIS/MapLibre/tileserver 全认、便于分发。写入端就是一个 SQLite（`tiles` 表 + `metadata` 表），本项目已有 SQLite 依赖，成本低。注意 MBTiles 规范是 TMS（y 轴翻转），与 XYZ 落盘布局差一个 `y = 2^z - 1 - y`。

## 3. 工程化实践

来源：GeoLibre `CLAUDE.md` / `.github/workflows/ci.yml`

- **覆盖率 ratchet 地板**：CI 设覆盖率下限（其前端 78% 行/78% 分支/63% 函数、后端 `--cov-fail-under=55`），取值是「当前值下压几点」；覆盖率涨上去就把地板调高锁死。防回归比冲一次性高覆盖现实。本项目可给 pytest 加 `--cov-fail-under`。
- **镜像常量 + 漂移检测测试**：上游包未导出的常量（如 maplibre-gl-vector 内部 2 GiB 上限）在自己仓库镜像一份，配一个上游升级时会失败的测试。对本项目 vendored 的 CesiumLab tiler（`services/terrain_tiling/cesiumlab_terrain.py`）和 `static/vendor/cesium/1.143.0` 完全适用——vendor 升级时镜像常量不会静默漂移。

## 4. 跨反经线 bbox 拆分（对照检查项）

来源：GeoLibre `apps/geolibre-desktop/src/lib/offline-tiles.ts`——`west > east` 的 bbox 拆成 `[west, 180]` + `[-180, east]` 两段再枚举瓦片。

本项目 `services/dem_granules.py:42` 明确「bbox does not cross antimeridian (caller should split)」。可对照检查各调用方（瓦片下载、GeoTIFF 拼接）是否都做了拆分或显式拒绝，有缺口的补上。

## 5. 低优先级备查

- **tiles worker**（GeoLibre `workers/tiles/`）：Cloudflare Worker 做 CORS 加头 + 边缘缓存的瓦片代理，严格 allowlist 防开放代理；`reproject.ts` 把只提供 EPSG:4326 的 WMS 动态重投影成 XYZ 瓦片——将来要接此类服务可搬。
- **MapLibre 3D 地形细节**（`packages/map/src/terrain-control.ts`）：`setCenterClampedToGround(false)` 解决陡坡地形上的缩放抖动/黑闪；夸张系数实时更新。本项目用 Cesium 预览地形，暂用不上。
- **发布矩阵**：`scripts/render-*.sh` 模板渲染 winget/MSIX/AUR/COPR/homebrew 描述文件 + `release.yml` 管道——`scripts/push-release.sh` 扩展分发渠道时可参考其组织方式。

## 6. WASM 插件适用性评估（2026-08-03 补充调研）

GeoLibre 的 WASM 构成（`packages/processing/`）：

- **`geolibre-wasm` v1.4.2**（MIT，源码在 opengeos/geolibre-rust）：whitebox_next_gen 纯 Rust 引擎编译成 WASI 二进制 + 自研工具，解压约 29.7 MB，经 `@bjorn3/browser_wasi_shim` 在浏览器内存虚拟文件系统里运行。工具目录共 **1009 个**（地形、水文、LiDAR、遥感、矢量），含 `contours_from_raster`、`hillshade`、`multidirectional_hillshade`、`slope`、`aspect`、`geomorphons`、`fill_depressions`、`d8_flow_accum`、`watershed` 等。API 形态：`runTool(name, {args, input: {文件名: 字节}})` → `{exitCode, stdout, files}`。
- **`gdal3.js`**（GDAL→WASM，约 28 MB wasm + 12 MB data，运行时从 jsDelivr 拉取）：仅用于浏览器端 GeoTIFF/COG 导出。
- **DuckDB-WASM Spatial** / **onnxruntime-web**：矢量格式转换 / 浏览器端 ML 分割。

**结论：基本不适用于本项目。** 原因：

1. **定位不符**：WASM 的卖点是「无服务器、数据不出浏览器」；本项目自带 Flask 服务端，同样的分析用现有 GDAL/numpy/matplotlib 栈在服务端做更自然，且管线已成熟。
2. **产品形态不同**：`contours_from_raster` 输出**矢量**等值线（shp/geojson）；本项目 contour 管线产**样式化 PNG 瓦片**（间距/阴影可配；水体掩膜只对已停止创建的遗留下载型任务有效，上传驱动的新任务 water 恒为 0），不能直接替换。
3. **核心管线无对应物**：瓦片拼接 GeoTIFF、quantized-mesh 地形切片（vendored CesiumLab tiler）在 WASM 世界没有替代实现。
4. **WASM 自身限制**：约 4 GiB 内存上限、单线程——GeoLibre 自己在 `wasm-client.ts` 注释里写明大数据要走 sidecar（原生 Python）。下载+切片恰是重 IO/重 CPU 任务。
5. **集成成本**：本项目前端是无打包器的 vendored 静态 JS，引入 npm WASI 包要自带一整套 shim 胶水；30 MB wasm 还要 vendor 进 Nuitka 包，成本大于收益。

若未来真出现相关需求，对的路径是：

- 上传 GeoTIFF 的浏览器端速览（hillshade 等）：`geotiff.js`（纯 JS、无 WASM）足够，不必上 WASI；
- 水文分析类能力（填洼/流量累积/流域）：服务端装原生 `whitebox` pip 包（预编译 whitebox_tools 二进制，同一家算法），而不是 WASM。

## 不建议借鉴

store-driven React 单向数据流、DuckDB-WASM 客户端分析、Whitebox WASM 浏览器端工具箱——与 Flask + SocketIO 服务端管线是两种形态；等高线/地形分析在服务端用 GDAL 做对本项目更合适（WASM 部分的详细论证见上节）。
