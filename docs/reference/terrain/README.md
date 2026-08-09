# terrain/ — 地形运维说明

四份文档：

| 文档 | 讲什么 |
|---|---|
| [`cesiumjs-loading.md`](cesiumjs-loading.md) | CesiumJS 端怎么加载本项目产出的 quantized-mesh 地形：base provider、单任务 DEM / 本地地形 provider、底图植入后单任务目录自包含（无 `parentUrl`）与底图不可用时的兜底级联，以及 `terrain_base_parent_url` 配置的生效时机与**必须是目录**这条坑 |
| [`global-base-build.md`](global-base-build.md) | 全球低层级基础地形（`assets/terrain/base_z8/`）：随包分卷的自动解压与手工预热入口，以及自建一份时的完整构建流程 —— 自备全球 DEM 的前置成本、可直接跑的命令与完整参数表、`--max-level` 省略后果的实算量级、输出目录与 `terrain_global_base_path` 的对齐要求 |
| [`triangulation-backends-measured.md`](triangulation-backends-measured.md) | 高程切片精度实测（3 个真实 granule、z8–14）：**`DemSampler` 半个源像素偏移的发现与修复**（1 弧秒 DEM 上 15.5 m 错位，端到端 RMS 8.07→1.03 m）、修完之后采样与三角化各占一半误差、为什么**暂不**再上更强的简化后端、`max_error_k` 在平缓地形上为何完全失灵、法线占 19%～38% 字节而光照默认关、以及对设计稿三处口径的修正。**其中「暂不再上更强简化后端」这条结论已被 `tiling-presets-measured.md` 取代**：应用侧现在连 `auto` 都不用了，三档统一走 `grid`，减面后端在用户任务这条路径上整个退场（`build_terrain` 与 CLI / 全球底图仍保留 `auto`）；该文第七节「还剩什么可做」的法线一条也已落地 |
| [`tiling-presets-measured.md`](tiling-presets-measured.md) | **高程切片三档预设（精度/均衡/速度）的实测选型与落地**（6 个真实 granule、102 组完整金字塔）：为什么三档都该用 `grid` 而落地前的默认 `auto` 在 6 个 DEM 上一个 Pareto 前沿都没进、为什么档位要靠 `max_level` 拉开而不是靠简化后端（兑换率差 2.4~3.9 倍）、法线为何该拆成独立开关（+35%~+100% 字节、1.5~2.2 倍时间、几何零收益）、`grid` 的三角形数暴涨为何不影响 Cesium 渲染（真实 WebGL 实测：同屏瓦片数不变、帧时在噪声内）、以及 `maxzoom` 不看源分辨率这条**至今仍在**的缺陷。**第九节是落地后的实现位置与真实切片复测**（含「张数比 ≠ 体积比、`speed` 档对小范围 DEM 不划算」那条反直觉），并说明档位的基准层级最终锚在**用户填的 `maxzoom`** 上，而不是本文测量时用的 `est` |

仓库里有构建脚本 [`scripts/build_global_base_terrain.ps1`](../../../scripts/build_global_base_terrain.ps1)。它**曾经漏传 `--tile-size`**（走 CLI 默认 17，而应用侧单任务用 65），照那时的脚本建出的 base 顶点网格比子层稀疏 4 倍/轴。**2026-08-05 的 a6da59e 已修**（早于三档预设，不是本次改的）：脚本现在显式传 `--tile-size $TileSize`，**该参数默认 65**（调用者 `-TileSize 129` 仍会覆盖它），且 `tests/test_build_scripts_contract.py` 钉住这个默认值与 `TileParams.tile_size` 相等。手上还留着旧脚本建出的 base 时，对齐办法见 `global-base-build.md`。

## 已知问题：无（parentUrl 在 z0–4 被遮蔽那条已修）

> **状态：两条路径都已消解。** 历史上每个任务的 `layer.json` 在 z0–4 声明**全球**可用，Cesium 取「第一个 availability 声明可用的层」、父层排在子层之后，于是 base 的 z0–4 永远不会被请求，被任务自己那 682 片垃圾瓦片整个遮蔽 —— **不抛错、不打日志、HTTP 全 200**，任务照样 completed。现在：
>
> - **默认路径**（随包底图可用）：任务只切 z8 起的层级（`tile_dem_task_dir` 恒传 `min_level = 8`，`build_terrain` 再钳到实际 `max_level` 以下），z0–z7 直接由植入任务目录的底图瓦片提供，`parentUrl` 已被 `merge_base_availability` 删除，没有级联可言。
> - **兜底路径**（底图不可用、退回 `parentUrl` 级联）：仍从 z0 出图，但 `available` 不再跟着出图范围走 —— `build_terrain` 用 `well_covered_tile_range` 把声明收窄到与 DEM 真正相交且覆盖率达标（`_MIN_TILE_AXIS_COVERAGE`）的范围，整层不够格就声明 `[]`，Cesium 于是会照常向 `parentUrl` 要 z0–4。
>
> 兜底路径上仍要付的代价：那 682 片瓦片的切片耗时与磁盘占用照付，`meta.json` 的 `minHeight` 也仍被它们的 0 值拉平（按所有出图瓦片聚合，与 `available` 无关）。历史分析见 [`../../reviews/2026-08-03-full-project-review.md`](../../reviews/2026-08-03-full-project-review.md) 的 **M12** 条目。

**动地形前真正要先看的一条**：`terrain_base_parent_url` 必须是**目录**形式。写成 `.../layer.json`，Cesium 会去请求 `.../layer.json/layer.json` 得 404，而它对这个 404 不报错 —— 塞一个假的 heightmap-1.0 图层并把 `heightmapStructure` 写在**共享的** builder 上，任务自己的 quantized-mesh 瓦片也按 heightmap 解析（实测 4154 m 山峰解成 −744 m，`hasVertexNormals` 仍报 true，瓦片全 200）。症状与上面那条旧缺陷几乎一样：拉远看地形明显不对、全程零报错。详见 [`cesiumjs-loading.md`](cesiumjs-loading.md) 的 §4。

## 两个没有界面的配置键

`terrain_global_base_path`（全球 base 目录，默认 `./assets/terrain/base_z8`）和 `terrain_base_parent_url`（**兜底**用的 `parentUrl`，默认 `http://localhost:5000/terrain/base` —— 必须是目录形式，带 `/layer.json` 会让 Cesium 整个 provider 降级成 heightmap）在配置页上**没有对应的输入框**，只能通过 `PUT /api/config` 改，或者直接改数据库 `config` 表。默认值在 `src/core/database.py` 的 `DEFAULT_CONFIGS` 里。

改 `terrain_base_parent_url` 的典型场景：服务不跑在 `localhost:5000`（换端口、部署到内网 IP 或域名），此时默认值指向的地址在客户端解析不到，级联会静默失败——Cesium 只是拿不到父层数据，同样不报错。
