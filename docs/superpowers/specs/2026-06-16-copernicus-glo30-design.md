# Copernicus GLO-30 作为 DEM 数据源（默认）设计

> 新增 Copernicus DEM GLO-30 作为 DEM 数据源，设为默认；ASTER GDEM v3 保留为可选。
> 适用于等高线/着色管线与 DEM 管线。日期：2026-06-16。

## 背景 / 动机

ASTER GDEM v3（现默认）是光学立体测量，RMSE ~8m、多噪点，晕渲显脏/碎。
Copernicus GLO-30 是雷达（TanDEM-X）派生，同为 30m 但干净得多，且 AWS 公开桶
**免认证**下载。已实测**中国全境覆盖**（北京/重庆/新疆/西藏/台湾/海南/近边境 11 点全有）。

## 用户决策（已确认）

- 集成 Copernicus GLO-30，**默认切为 GLO-30**；ASTER 仍可在数据源下拉里选。
- 等高线/着色 + DEM 两条下载线都支持选源。

## 数据源规格（已核实）

| 数据集 id | 产品 | 认证 | base URL | 瓦片路径 |
|---|---|---|---|---|
| `COP-DEM-GLO-30`（默认）| Copernicus GLO-30 30m | **无**（AWS 公开桶）| `https://copernicus-dem-30m.s3.amazonaws.com/` | `Copernicus_DSM_COG_10_{N/S}LL_00_{E/W}LLL_00_DEM/<同名>.tif` |
| `ASTGTM.003` | ASTER GDEM v3 30m | Earthdata | `…/lp-prod-protected/ASTGTM.003/` | `ASTGTMV003_{tile}_dem.tif` |
| `ASTWBD.001`（水体，内部）| ASTER 水体 | Earthdata | `…/lp-prod-protected/ASTWBD.001/` | `ASTWBDV001_{tile}_att.tif` |

要点：
- GLO-30 瓦片是**嵌套路径**（目录/同名.tif）；本地存为 **basename 扁平文件**，便于 list_dem_tifs。
- GLO-30 文件名是大写 `_DEM.tif`；`list_dem_tifs` 要同时匹配 `_dem.tif` 和 `_DEM.tif`。
- **水体仍走 ASTWBD（Earthdata）**——即使 DEM 用免认证的 GLO-30，开水体仍需 Earthdata 账号。
- 海洋无瓦片（视为 0m）；极个别缺格按缺数据处理。

## 改动范围

| 层 | 文件 | 改动 |
|---|---|---|
| 数据 | `services/dem_granules.py` | `copernicus_glo30_granules_for_tile(tile)`（嵌套路径）；GLO-30 风格 tile_id |
| 下载 | `services/dem_download_engine.py` | `_dataset_base_url` 加 COP-DEM-GLO-30；**无认证下载分支**（跳过 Earthdata 签名）；本地存 basename |
| VRT | `services/terrain_tiling/vrt_builder.py` | `list_dem_tifs` 同时匹配 `_DEM.tif` |
| 等高线 | `services/contour_task_manager.py` | dataset 选源（默认 GLO-30），按源算 DEM 粒度；_execute 用任务 dataset 下载 |
| DEM | `services/dem_task_manager.py` | dataset 选源（默认 GLO-30）；GLO-30 时忽略 num/swb |
| API | `routes/contour_api.py` / `routes/dem_api.py` | 透传 dataset（已透传 data；DEM 端确认）|
| 前端 | `templates/index.html`,`static/js/map.js` | 数据源下拉（GLO-30 默认 / ASTER），等高线 + DEM 提交 dataset |
| 测试 | `tests/` | 粒度名/引擎 URL+无认证/list_dem_tifs/两个 manager 默认源与选源/前端语法 |

## 测试策略

- 纯函数（Copernicus 粒度名、引擎 base URL + requires_auth、list_dem_tifs 匹配大写）：直接单测。
- manager：create_task 默认 dataset=COP-DEM-GLO-30 且粒度为 Copernicus 路径；显式传 ASTGTM.003 时为 ASTER 粒度。
- 不做真实 AWS 下载单测（网络）；下载分支用 monkeypatch 验证“无认证数据集不调用 get_signed_url、直连 file_url”。
- 既有断言 ASTER 默认的测试相应更新为新默认或显式指定 dataset。
