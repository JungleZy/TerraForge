# terrain/ — 地形运维说明

三份文档：

| 文档 | 讲什么 |
|---|---|
| [`cesiumjs-loading.md`](cesiumjs-loading.md) | CesiumJS 端怎么加载本项目产出的 quantized-mesh 地形：base provider、单任务 DEM / 本地地形 provider、`parentUrl` 级联的**实际行为**（见下方已知问题），以及 `terrain_base_parent_url` 配置的生效时机 |
| [`global-base-build.md`](global-base-build.md) | 全球低层级基础地形（`assets/terrain/base_z8/`）：随包分卷的自动解压与手工预热入口，以及自建一份时的完整构建流程 —— 自备全球 DEM 的前置成本、可直接跑的命令与完整参数表、`--max-level` 省略后果的实算量级、输出目录与 `terrain_global_base_path` 的对齐要求 |
| [`triangulation-backends-measured.md`](triangulation-backends-measured.md) | 高程切片精度实测（3 个真实 granule、z8–14）：**`DemSampler` 半个源像素偏移的发现与修复**（1 弧秒 DEM 上 15.5 m 错位，端到端 RMS 8.07→1.03 m）、修完之后采样与三角化各占一半误差、为什么**暂不**再上更强的简化后端、`max_error_k` 在平缓地形上为何完全失灵、法线占 19%～38% 字节而光照默认关、以及对设计稿三处口径的修正 |

仓库里有构建脚本 [`scripts/build_global_base_terrain.ps1`](../../../scripts/build_global_base_terrain.ps1)，但它**漏传 `--tile-size`**（走 CLI 默认 17，而应用侧单任务用 65），照它建出的 base 顶点网格比子层稀疏 4 倍/轴。详见 `global-base-build.md` 的说明与两种对齐办法。

## ⚠️ 已知问题：parentUrl 级联在 z0–4 不生效，且全程不报错

> **状态（底图随任务植入之后）：默认路径上已消解，兜底路径上依旧。** 底图可用时任务只切 `min_level = min(8, maxzoom)` 起的层级，z0–z4 那 682 片垃圾瓦片根本不再生成，低层级直接由植入进任务目录的底图瓦片提供，也不再有 `parentUrl`、没有级联可言。下面描述的整套现象只在**底图不可用**（有人删了 `assets/terrain/*.part`）退回 parentUrl 级联时才成立 —— 那条路径一行未动，所以原文保留。

**先看这条再排查。** 每个 DEM / 本地地形任务的 `layer.json` 在 z0–4 声明了**全球**可用（`available[0..4]` 是整个世界的矩形，而不是 DEM 实际范围），Cesium 取「第一个 availability 声明可用的层」、父层排在子层之后 —— 于是 base 地形的 z0–4 **永远不会被请求**，被任务自己那 682 片垃圾瓦片整个遮蔽。

这些瓦片的高程要么是被兜成 hmin=0/hmax=1 的平面（与 DEM 完全不相交时），要么是把 DEM 边缘一行/列的高程沿法向拉伸出去（部分相交时）—— 后者会让一块 4000 m 的高原在 z0 视角下糊成横跨半个半球的阶梯台地。

**症状与误诊**：低层级（拉远看全球）地形明显不对——该是平的地方鼓起来、该有起伏的地方是平板。此时**不要**去查 base 有没有构建成功、`/terrain/base/layer.json` 的 URL 对不对、CORS 通不通 —— 全都是正常的。根因就在上面：**整条链路不抛任何错、不打任何日志、HTTP 全 200**，任务照样标 completed。

**触发条件**：仅限退回 parentUrl 级联的兜底路径（见本节开头的状态说明），且全球 base 目录真实存在时才看得出来。没构建 / `parent_url` 为空时是单层 provider，没有「被遮蔽」这回事——但那 682 片无用瓦片的切片耗时、磁盘占用，以及 `meta.json` 里 minHeight 被 0 值污染恒为 0，是始终存在的代价。

完整分析、代码位置（`src/services/terrain_tiling/cesiumlab_terrain.py` 的 `_tile_ranges` z≤4 全球分支、`DemSampler.sample` 的越界钳位）和改法见 [`../../reviews/2026-08-03-full-project-review.md`](../../reviews/2026-08-03-full-project-review.md) 的 **M12** 条目。

## 两个没有界面的配置键

`terrain_global_base_path`（全球 base 目录，默认 `./assets/terrain/base_z8`）和 `terrain_base_parent_url`（**兜底**用的 `parentUrl`，默认 `http://localhost:5000/terrain/base` —— 必须是目录形式，带 `/layer.json` 会让 Cesium 整个 provider 降级成 heightmap）在配置页上**没有对应的输入框**，只能通过 `PUT /api/config` 改，或者直接改数据库 `config` 表。默认值在 `src/core/database.py` 的 `DEFAULT_CONFIGS` 里。

改 `terrain_base_parent_url` 的典型场景：服务不跑在 `localhost:5000`（换端口、部署到内网 IP 或域名），此时默认值指向的地址在客户端解析不到，级联会静默失败——Cesium 只是拿不到父层数据，同样不报错。
