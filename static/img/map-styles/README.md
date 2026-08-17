# map-styles/ —— 地图样式的样例瓦片

> 五个文件名就是 Google vt 的 `lyrs` 码（`m` 标准 / `s` 卫星 / `y` 卫星+标注 / `h` 道路 / `t` 地形），与下载引擎用的码同一张表（`src/services/source_registry.py` 的 `STYLE_CODES`）。**改名要同步改代码**：`static/js/map.js:initMapStylePreview` 按 `/static/img/map-styles/${lyrs}.png` 取图。

引用点两处：界面「地图样式」下拉旁的预览图，和根 [`README.md`](../../../README.md) / [`README.en.md`](../../../README.en.md) 的样式表。

## 取景

| 项 | 值 |
|---|---|
| 位置 | 重庆一带，lat 29.56 / lon 106.55 |
| 层级 | z10 → 瓦片 `10/815/423`（由 `src/contracts/region_tiles.py:lat_lon_to_tile` 算出） |
| 服务器条目 | `mts0`（展开为 `mts0.googleapis.com`，与 `config.tile_servers` 出厂值同形） |

## 重抓

```bash
uv run python scripts/fetch_map_style_samples.py --dry-run   # 只抓不写，先看校验结果
uv run python scripts/fetch_map_style_samples.py             # 五张一起覆盖
```

脚本**五张一起换**：并排出现在同一个下拉框里的五张图必须是同一地点同一层级，否则读者会把图源差异误读成样式差异。落盘前两道闸 —— 图片魔数（挡运营商劫持的 `200` + HTML）与白底平均亮度（挡「劫持页/错误响应被存成瓦片」那类事故）。

需要能连通 `*.googleapis.com`（本仓开发机走 WSL 宿主上的代理：`--proxy http://<宿主IP>:<端口>`）；探不到代理时脚本会打印「直连」，随后逐张超时并整套拒收。

## `t.png` 是黑的，而且它没坏

白底合成后的平均亮度（2026-08-17 重抓复核）：

| 文件 | 平均亮度 (0–255) | 说明 |
|---|---|---|
| `m.png` | 219.7 | 标准道路图 |
| `h.png` | 219.2 | 道路叠加层（带透明，必须白底合成后再看） |
| `y.png` | 92.5 | 卫星 + 标注 |
| `s.png` | 90.6 | 纯卫星影像 |
| `t.png` | **7.4** | **地形阴影叠加层，暗是它的真实样貌** |

证据（2026-08-17 经宿主代理实测同一格 `10/815/423`）：重抓回来的 `lyrs=t` 与仓库里这张 **MD5 逐字节相同**（`e447b2fc78abb8cb801fe6abb0bfacf2`），而同一格的 `lyrs=p`（Google 那张浅色的完整地形底图）平均亮度 203.1、`lyrs=r`（标准道路图）219.7。也就是说 `lyrs=t` 只是**地形阴影层**，设计上叠在别的底图之上，单独看近乎全黑。

所以：界面选「地形图」时预览是个黑格子**不是 bug**，下载出来的瓦片也确实是这样。要拿它当图看，得自己叠到别的底图上。`fetch_map_style_samples.py` 的亮度闸因此把 `t` 列进 `DARK_BY_DESIGN` 直接放行，其余四个样式仍然一低于阈值就整套拒收。

> **已决定不换码（2026-08-17）**：`STYLE_CODES` 继续把「地形」映射到 `t`。换成 `p`（地形 + 底图 + 标注）要动缓存命名空间 `cache/<style_code>-<fingerprint>/` 与存量任务行，代价大于收益；`t` 到底是什么改由文档讲清 —— 本节加上两份 README 的样式表。

## 扩展名与实际格式不符，别去「修」

`m.png`、`h.png` 是真 PNG；`s.png`、`y.png`、`t.png` 的内容是 JPEG（Google 对影像类图层返回 JPEG）。浏览器按内容嗅探，显示正常；文件名统一 `.png` 是因为取图路径按样式码拼死了后缀。改后缀要同时改 `map.js` 与两份 README 的表格。
