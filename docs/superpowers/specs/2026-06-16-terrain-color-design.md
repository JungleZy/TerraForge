# 等高线地图：山体分层设色 + 晕渲 + 水体 设计

> 扩展现有「等高线瓦片」管线，在画线之前叠加地形着色。复用已下载的 ASTER DEM，
> 新增 ASTWBD 水体掩膜下载。日期：2026-06-16。

## 目标

等高线瓦片当前只有线 + 纯色背景。本特性把**山（海拔高低）**和**水（江河湖海）**也用
颜色表示：分层设色（hypsometric）+ 晕渲（hillshade）+ 真实水体（ASTWBD att 掩膜）。

## 用户决策（已确认）

1. 山体：**分层设色 + 晕渲**（彩色 + 阳光阴影，最接近专业地形图）。
2. 水体：**ASTWBD V1 真实水体掩膜**（非 elevation 阈值）。
3. 配色：默认 GB/军标暖色分层（绿→黄褐→棕→白，水蓝），全部 config 可调。
4. 两个 per-task 开关「分层设色+晕渲」「水体」，**默认开**；可关掉得到纯线（透明叠加用）。

## 数据源

- 高程：ASTGTM.003 `ASTGTMV003_{tile}_dem.tif`（管线已下）。
- 水体：**ASTWBD.001** `ASTWBDV001_{tile}_att.tif`，值 0陆地/1海洋/2河流/3湖泊。
  - 同在 `https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/ASTWBD.001/`。
  - ⚠ 云端是单 COG 还是 zip 需用户 Earthdata 实测确认；数据集 URL + 文件名隔离成独立
    helper，便于一行修正。

## 渲染分层（每瓦片，下→上）

1. **分层设色 + 晕渲**：`matplotlib.colors.LightSource(az, alt).shade(arr, cmap, norm,
   vert_exag, dx=pxW, dy=|pxH|, blend_mode)` 一步生成彩色阴影 RGB → `imshow(extent=tile)`。
   - 全局固定分层（BoundaryNorm + ListedColormap）→ 相邻瓦片颜色一致、无缝。
   - nodata（nan）像素 alpha=0，露出背景。
2. **水体**：att VRT 独立 warp 到 3857，按瓦片地理窗读取；att∈{1,2,3} 涂蓝
   （海/湖+河 可不同蓝），盖在设色上、压在线下。
3. **等高线**首/计曲线 + 高程标注（现有逻辑，zorder 提到最上）。
- 背景色（米白/透明）退化为只在无数据区域生效。
- 每瓦片有效等高距 `eff = interval_for_zoom(...)` 逻辑保持不变。

## 一致性 & 性能

- 设色用全局固定高程→颜色映射（不是每瓦片 min/max），晕渲用固定光照参数 +
  现有 1px 窗口 padding，跨瓦片对齐。
- 设色+晕渲 + 水体读取使每瓦片更慢；叠加「整块 DEM + 高 zoom」瓦片量，大范围高层级耗时明显。

## 改动范围

| 层 | 文件 | 改动 |
|---|---|---|
| DB | `database.py` | contour_tasks +`terrain_shade`,`water`(默认1)；contour_files +`kind`('dem'/'water')；config 新增分层/光照/水色键；幂等 ALTER |
| 数据 | `services/dem_granules.py` | `astwbd_v1_att_granules_for_tile(tile)` |
| 下载 | `services/dem_download_engine.py` | `_dataset_base_url` 加 ASTWBD.001 分支 |
| VRT | `services/terrain_tiling/vrt_builder.py` | `list_att_tifs(task_dir)` |
| 引擎 | `services/contour_engine.py` | ContourStyle 加分层/光照/水色字段+from_config；`build_contour_tiles` 加 `shade`/`water` + att 输入，铺色/水体图层 |
| 编排 | `services/contour_task_tiler.py` | ContourParams 加 shade/water；列 att tifs 传入 |
| 管理 | `services/contour_task_manager.py` | create_task 存开关+水体时加 ASTWBD 粒度(kind='water')；_execute 下载 DEM + (水体时)att，fail-gate 计两类，渲染传开关 |
| API | `routes/contour_api.py` | POST 接收 terrain_shade/water |
| 前端 | `templates/index.html`,`static/js/map.js` | 两个勾选(默认勾)+提交 |
| 测试 | `tests/` | 粒度名/URL/list_att/schema/config/engine 渲染(合成 att)/manager/api |

## 测试策略

- 纯函数（粒度名、URL、list_att、coverage 已有）：直接单测。
- 引擎渲染：合成 DEM + 合成 att tif（真实 GDAL/matplotlib，env 已具备，参照
  test_contour_engine_render.py），断言瓦片生成且非全透明。
- manager：monkeypatch download_files / tile_contour_task_dir，断言开关入库、水体加 att 粒度
  (kind='water')、_execute 两类下载 + 开关透传渲染。
- DB/API：schema 列、config 种子、POST 字段。
