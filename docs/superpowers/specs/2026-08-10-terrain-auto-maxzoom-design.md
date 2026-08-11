# 切片档位锚回源分辨率：`maxzoom` 增加「自动」一态

> **状态**：设计已批准，待实施 ｜ **记录时间**：2026-08-10
> 前置版本：v0.2.13（`85b271b`）
> 相关实测：[`docs/reference/terrain/tiling-presets-measured.md`](../../reference/terrain/tiling-presets-measured.md)（尤其第四、六、九节）

## 结论先行

切片档位（精细 / 均衡 / 快速 = 层级偏移 +1 / 0 / −1）**不改**。改的是它锚在哪：把基准层级从「用户在表单里填的 `maxzoom`」换成「按源数据像素尺寸现算」，档位于是第一次真正等于一个**精度**——顶点间距 ≈ 0.3 / 0.6 / 1.2 × 源像素——而不是「比你填的那个数 ±1 级」。

这修的是 `tiling-presets-measured.md` 第六节记录、落地时**故意没修**的那条缺陷。

四条已定的取舍：

| 取舍 | 决定 | 理由 |
|---|---|---|
| 「自动」是不是出厂默认 | **是** | 固定 14 只对 30 m 源正确；粗源超建、细源欠建，两头都错 |
| 算出的层级过深怎么办 | **只预告，不拦** | 张数与体积在按下起切之前就摆在界面上；这类爆炸看得见（磁盘涨），不是本仓栽过的「completed + 200 + 零报错」那种静默错 |
| 「自动」怎么落库 | **哨兵 `maxzoom = -1`** | 两张表的该列是 `INTEGER NOT NULL`，去掉约束要 12 步重建表，违背本仓「幂等 ALTER ADD COLUMN」的迁移约定 |
| 存量库要不要跟着变 | **值恰好是 `'14'` 才改写** | 对 30 m 源产物零变化（`est` 本来就 = 14），只对非 30 m 源生效；显式设过别的数字的用户不动 |

## 背景

### 档位为什么是「多切一级 / 少切一级」

在 `grid` 后端 + `tile_size=65` 下，一张瓦片就是死板的 65×65 规则网格，几何精度由顶点间距唯一决定：

```
顶点间距 = 瓦片纬向跨度 / (tile_size - 1) = (180 / 2^z) / 64
```

`tile_size` 固定，层级就是精度本身。想在不动层级的前提下改精度，只有两个旋钮，都不通：

- **`tile_size`** —— 唯一真正的「每层精度」旋钮，但翻倍它和 `z+1` 在数学上是同一件事（间距都减半）。实测 `129@z13` 对 `65@z14`：RMS 完全相同（都是 0.082 m），体积只小 9%，耗时 19.5 s 对 6.5 s。且随包底图是 65×65 且被物理植入任务目录，改它会让 z7→z8 交界处顶点密度跳变。
- **`max_error_k` / martini** —— 只做减面，是从 `grid` 往下退，永远不可能比规则网格更准（`_max_error_for_level` 只在 martini/auto 分支被调用）。

所以「档位 = ±1 级」不是 UI 上的偷懒，它**就是**精度旋钮，只是记在了层级这个单位上。选型期两个旋钮做过同基线对比：层级旋钮的性价比是简化后端的 2.4~3.9 倍，而且它省时间、后端花时间（第四节）。

### 缺陷：基准锚错了地方

`build_terrain` 里 `max_level` 有两条来路（`cesium_terrain.py:1426-1428`）：

```python
if max_level is None:
    max_level = scheme.estimate_max_level(sampler.pixel_size_deg)
max_level = max(0, min(MAX_ZOOM, int(max_level) + int(level_offset)))
```

估算分支**已经写好、已经有测试钉住**（`tests/test_fix_terrain_estimate_max_level.py:66` 钉的正是「未传 max_level 时基准 = `estimate_max_level`，偏移叠加在它上面」），但应用侧从来走不到它——`dem_task_tiler.tile_dem_task_dir` 恒传 `max_level=int(params.maxzoom)`。于是估算只有 CLI 省略 `--max-level` 时才生效。

后果双向（第六节实测）：

| 源分辨率 | `est` | 固定 14 切出来 | 按 `est` 切出来 |
|---|---|---|---|
| 3″（93 m） | 12 | 77.4 MB / 12071 张 | **6.9 MB / 1445 张** |
| 1″（30 m，Copernicus / ASTER） | 14 | — | 与固定值重合 |
| 5 m（用户自己的航测 DEM） | 16 | 被截断在 14，源数据的细节没进瓦片 | — |

粗源那 11 倍体积换来的只是对同一批 93 m 数据更平滑的插值，不含任何新地形。

### 界面上已经有一个算对了的数字

`raster_probe._estimate_maxzoom`（`raster_probe.py:301-328`）走的正是同一个 `GeographicTilingScheme.estimate_max_level`，结果以 `recommended_maxzoom` 出现在 TIF 信息卡的「建议最大层级」一行（`map.js:939` / `:980`）。但它**纯展示**——全仓没有任何 JS 会把它写进 `#localTerrainMaxzoom`（该 id 只被 `map.js:2405` 读取一次）。

也就是说：正确答案一直显示在用户眼前，只是没人把它接到切片上。

## 语义

`maxzoom` 从两态变三态：

| 送上来的值 | 含义 |
|---|---|
| `'auto'` | 按源数据像素尺寸现算基准层级 |
| `0..21` | 用户指定基准层级（现状行为） |
| 空串 / 缺省 | 未表态 → 走配置 `terrain_local_maxzoom`（出厂值改为 `'auto'`） |

**第三态不能复用空串。** 现有链路里空串已经被占用：路由把它归一成 `None`，管理器把 `None` 解释成「读配置默认」（`local_terrain_api.py:39-41`、`local_terrain_task_manager.py:157-158`）。「自动」必须是一个独立的字面量。

档位偏移仍叠加在基准上，`TILING_QUALITY_OFFSETS` 一个字都不动。

## 架构

```
表单 [☑ 自动] ──► maxzoom='auto'
                    │ coerce_maxzoom          geo_validation，唯一把关点
                    ▼
              manager 落库 maxzoom = -1        AUTO_MAXZOOM_SENTINEL
                    │
                    ▼
        TileParams(maxzoom=None)               Optional[int]
                    │ tile_dem_task_dir
                    ▼
        build_terrain(max_level=None)          ← 现成分支，:1427
                    │ estimate_max_level(sampler.pixel_size_deg)
                    │ + level_offset  → 钳位 [0,21]
                    ▼
        effective_maxzoom 回库                  产物事实，已有列
```

**估算发生在 `build_terrain` 内部，基于物化后的合并栅格**（`build_input_raster` 的产物），不是基于浏览器解析的文件头。表单里显示的是**预告**，多文件时取最细像素；两者绝大多数情况一致，但产物事实永远以 `effective_maxzoom` 为准。详情面板现有的「优先显示 `effective_maxzoom`」逻辑刚好承接这一点，不用改结构。

## 各层改动

| 层 | 位置 | 落成什么 |
|---|---|---|
| **校验** | `src/services/geo_validation.py` 新增 `coerce_maxzoom`、`AUTO_MAXZOOM_SENTINEL = -1`，以及一对哨兵翻译 helper | 与 `validate_tiling_quality` / `coerce_vertex_normals` 并排，同一规格。**返回三态**：`'auto'`（严格字面量，**不做大小写归一、不裁空白**，`'AUTO'` / `' auto '` 一律 `ValueError`）、`int`（其余交给现有 `validate_zoom`）、`None`（`None` / `''` = 未表态）。**这是 maxzoom 唯一的把关点**。哨兵翻译也住这里：`maxzoom_to_db('auto') → -1`、`maxzoom_from_db(-1) → None`，两个方向各一处，调用方不许自己写 `-1` |
| **切片器** | `src/services/terrain_tiling/dem_task_tiler.py` | `TileParams.maxzoom: Optional[int]`；`tile_dem_task_dir` 传 `max_level=params.maxzoom`（`None` 直接透传，不再 `int()`） |
| **切片核** | `src/services/terrain_tiling/cesium_terrain.py` | **不改**。估算 + 偏移 + 钳位三段已经现成 |
| **管理器** | `src/services/local_terrain_task_manager.py`、`src/services/dem_task_manager.py` | 归一后 `'auto'` → `maxzoom_to_db` 落库 `-1`、构造 `TileParams(maxzoom=None)`；配置默认读到 `'auto'` 时同样走这条。起切读回处（`local_terrain_task_manager.py:551`）过 `maxzoom_from_db` |
| **路由** | `src/routes/local_terrain_api.py`、`src/routes/terrain_api.py` | 两条都改成过 `coerce_maxzoom`。⚠️ `terrain_api.py` 目前**压根不校验** maxzoom（`:37-42` 原样交给 manager），顺带收口 |
| **建表 / 迁移** | `src/core/database.py` | `DEFAULT_CONFIGS` 的 `terrain_local_maxzoom` 出厂值 `'14'` → `'auto'`；新增 `user_version` 3 → 4 的一次性迁移：`UPDATE config SET value='auto' WHERE key='terrain_local_maxzoom' AND value='14'`。**两张表的 `maxzoom` 列定义不动** |
| **配置校验** | `src/services/config_manager.py` | **不改**。该键在 `_UNCONSTRAINED_KEYS`（`:327`），`'auto'` 天然收得下 |
| **表单初值** | `src/routes/main.py` `_terrain_form_defaults` | 先认 `'auto'` 再走 `validate_zoom`——否则自动值会被当成非法输入 warning 回落 14。返回值从 `(maxzoom, preset)` 变成 `(maxzoom, maxzoom_auto, preset)`，其中自动态下 `maxzoom` 交付**出厂 14**（见下一行） |
| **表单** | `templates/index.html` | `#localTerrainMaxzoom` 旁加复选框 `#localTerrainMaxzoomAuto`；勾上时数字输入 `disabled` 变灰。自动态下数字输入的 `value` 仍渲染出厂 14——它是用户取消勾选后的起点。⚠️ 理由**不是**「空 `value` 会让表单 `:invalid`」：按 HTML 约束校验，`rangeUnderflow` / `rangeOverflow` 对空值不适用，空值要有 `required` 才触发 `valueMissing`，而这个控件没有 `required`（本仓的浏览器模拟器 `tests/test_config_form_submittable.py` 对空 `value` 同样是直接 `continue`）。真会让 `#processForm` 变 `:invalid` 的是**越界的非空值**（`value="99"`），那条另有护栏。渲染空值的真实坏处是**静默**：用户取消勾选后拿到一个空框，提交送空串，后端 `coerce_maxzoom` 判成「未表态」→ 回落到配置默认，也就是他刚取消掉的自动挡 |
| **提交** | `static/js/map.js` `submitLocalTerrain` | 勾上送 `'auto'`，否则送数字，控件缺席仍送空串——`test_map_js_contract.py:611` 那条「前端不许抄一份默认值」照旧成立 |
| **预告** | `src/services/raster_probe.py` + `static/js/map.js` | 见下节 |
| **详情面板** | `static/js/history.js` | 基准那一格遇到 `maxzoom == -1` 显示「自动（按源分辨率）」，不显示 `-1` |
| **i18n** | `src/i18n/catalog/tpl_index.py`、`js_map.py`、`js_history.py` | 自动开关标签、预告行、详情面板的「自动」字样，zh / en 各一份。**必须改**：`tpl.index.process.terrain_quality_hint`（`tpl_index.py:273`）现在写的是「基准层级就是上面填的最大切片层级」——自动挡下这句是错的 |

### 为什么用哨兵而不是让 `maxzoom` 可 NULL

两张表的该列都是 `INTEGER NOT NULL` 无默认（`database.py:589`、`:675`），`tests/test_local_terrain_schema.py:49` 显式钉住 notnull。SQLite 去掉 NOT NULL 要走 12 步重建表，而本仓的迁移约定明确是「幂等 `ALTER TABLE ... ADD COLUMN`」（CLAUDE.md「Database conventions」）。

`validate_zoom` 的值域是 `0..21`，用户输入永远到不了 `-1`，哨兵不存在撞车。常量住在 `geo_validation.py` 与取值表并列，单一事实源。

两个被否掉的替代：

- **新增 `maxzoom_mode TEXT DEFAULT 'manual'` 列** —— 符合 ALTER 约定，但两列要保持同步，且自动态下 `maxzoom` 存什么都是假值。
- **重建表让 `maxzoom` 可 NULL** —— 语义最干净，但违背迁移约定，且要同时改一条现有测试。

⚠️ 与 `effective_maxzoom` 的 `DEFAULT NULL` 不要记混：那里的 `NULL` 是「还不知道切到了第几级」，这里的 `-1` 是「基准不是一个数字」。两个列语义正交，自动挡下 `maxzoom = -1` 且 `effective_maxzoom` 照常记录实际层级。

### 迁移为什么敢改存量值

本工具默认下载的是 30 m 源（Copernicus GLO-30 / ASTER），`estimate_max_level` 对它算出来就是 14。所以 `'14' → 'auto'` 对最常见的情形是**产物零变化**——同一份 DEM、同一个档位、切出同样的层级。行为只对非 30 m 源改变，而那正是要修的缺陷。

显式设过 12 或 16 的用户不受影响（`WHERE value='14'` 不匹配）。无法区分「出厂没动」与「特意设成 14」是这条迁移唯一的模糊处，但由于上一段，两者的产物在常见情形下相同，代价可接受。

## 预告行

`/api/raster/inspect` 的响应汇总节点已有 `bounds_wgs84` 与 `recommended_maxzoom`（`raster_probe.py:496-503`，单文件时汇总同样存在）。

**新增 `summary["tile_counts"]`：长度 22 的整数数组，`tile_counts[z]` = 该层与并集 bounds 相交的张数（逐层，不累加）**。服务端用 `intersecting_tile_range` 的同一套几何算，不做任何 `min_level` 假设——累加区间留给消费方，因为底图可用与否会改变起点。

这样 JS 不需要移植任何几何代码——单一事实源留在 Python，档位下拉与自动开关切换时纯查表，零额外请求。信息卡里追加一行：

> 自动 → 基准 z16 · 精细档实际 z17 · 约 71 万张 · 约 6.0 GB

- **张数**：前端按 `sum(tile_counts[8..实际层级])` 累加（随包底图可用时 `dem_task_tiler` 恒传 `min_level=8`）。模型自检：1°×1° 源在 z8–z14 累加得约 1.15 万张，与第二节实测的 12071 张（其中含 z0–z4 无条件全球的 682 张，见 9.3 节）扣除底座后相差 <1%
- **体积**：单张均值取 **8.4 KB**（第 9.3 节最深层级的实测值，偏保守）；勾了法线再乘 1.4（第五节 +35%~+100% 的下沿）
- **手动挡也显示** —— 填 18 一样会爆，这一行不该只在自动挡出现

参考量级（1°×1°，z8 起）：

| 基准 | 实际张数 | 估算体积 |
|---|---|---|
| z14（30 m 源，均衡） | 约 1.1 万 | 约 96 MB |
| z16（5 m 源，均衡） | 约 18 万 | 约 1.5 GB |
| z17（5 m 源，精细） | 约 71 万 | 约 6.0 GB |

## 明确不做的

- **不改档位取值表。** `TILING_QUALITY_OFFSETS` 的 +1 / 0 / −1 与 `DEFAULT_TILING_QUALITY` 原样。
- **不改 `tile_size`、`max_error_k`、`workers`、`triangulator`。** 第十节「明确不要动的旋钮」全部继续照办。
- **不动 `build_terrain`。** 它那三段（估算 / 偏移 / 钳位）已经是对的，本设计只是让应用侧走得到。
- **不给配置页加控件。** `terrain_local_maxzoom` 全仓只有 `templates/index.html:290` 一个控件，配置页从来没有这一项，本次不新增。
- **不动 DEM 侧的前端。** `POST /api/terrain/dem/<id>/start` 当前没有前端调用方（`history.js:474-475` 记录了起切按钮的移除），只被测试驱动；它的路由与管理器照改，前端不碰。

## 测试

### 预判会红

| 文件 | 测试 | 为什么 |
|---|---|---|
| `tests/test_tiling_presets.py` | `test_config_defaults_are_shipped` (:146) | 出厂值 `'14'` → `'auto'` |
| `tests/test_terrain_lighting_frontend.py` | `test_preset_controls_render_the_configured_defaults` (:525)、`test_out_of_range_maxzoom_is_clamped_out_loud` (:633，落地时随行为改名为 `test_out_of_range_maxzoom_falls_back_out_loud`——越界配置不再被 clamp 成 14，而是退回自动挡) | 控件形态变了（多一个复选框），初值断言 `value="16"` / `value="14"` 需要跟着走 |
| `tests/test_local_terrain_api.py` | `test_out_of_range_maxzoom_config_falls_back` (:835) | 配置非法时的回落目标从 `14` 变成自动 |
| `tests/test_config_form_submittable.py` | `test_unvalidated_config_cannot_make_the_page_unsubmittable` (:118) | 直接查 `id == 'localTerrainMaxzoom'` 的 tag |

### 新增覆盖

- `coerce_maxzoom` 三态单测，含 `'AUTO'` / `' auto '` / `'12.5'` / `-1` 必须抛（`-1` 是内部哨兵，**不许从外部传进来**）。
- HTTP → 管理器 → `build_terrain` 全链：自动挡下替身收到的 kwarg 必须是 `max_level=None`，**不是 `-1` 也不是 `14`**。
- 哨兵落库与读回往返：`maxzoom='auto'` → 库里 `-1` → 起切时 `TileParams.maxzoom is None`。
- 迁移：`'14'` → `'auto'`、`'12'` 原样不动、跑两遍幂等、`user_version` 落到 4。
- `tile_counts` 与 `intersecting_tile_range` 对账（同一份 bounds 下两者逐级相等），以及它出现在单文件汇总里。
- 自动挡下 `effective_maxzoom` 仍被正确回写（不能因为基准是 `-1` 就漏掉产物事实）。

## 风险

| 风险 | 处置 |
|---|---|
| 高分辨率源自动算出 z17，切出 6 GB | 预告行在起切前给出张数与体积（本次采纳的护栏）；用户可切回手动或降档 |
| 预告（文件头解析）与实际（物化栅格）算出的层级不一致 | 详情面板已经优先显示 `effective_maxzoom`；预告行文案写明是估计值 |
| 存量用户的 `'14'` 被改写成 `'auto'` | 仅在值恰好等于出厂默认时改写；对 30 m 源产物零变化 |
| 哨兵 `-1` 泄漏到界面 | 详情面板与表单各有一处翻译点，测试钉住；`coerce_maxzoom` 拒绝外部传入 `-1` |
| 自动挡下 `maxzoom ≤ 8` 配负偏移切 0 张 | `build_terrain:1453` 的 `min_level = min(min_level, max_level)` 已经兜住，不需要新代码 |
