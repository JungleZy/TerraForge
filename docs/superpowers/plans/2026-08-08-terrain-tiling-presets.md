# 高程切片三档预设 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给高程切片任务加「精度 / 均衡 / 速度」三档用户可选预设（默认均衡），外加一个独立的「地形光照法线」开关（默认关）。

**Architecture:** 档位不是新算法，而是**相对基准层级的偏移**（精度 +1 / 均衡 0 / 速度 −1），基准 = 用户填的 maxzoom。三档统一改用 `grid` 后端。偏移在 `build_terrain` 内部落地（那里是 max_level 唯一的解析点），法线经 `TileParams` 新字段透传。全部实测依据见 [`docs/reference/terrain/tiling-presets-measured.md`](../../reference/terrain/tiling-presets-measured.md)。

**Tech Stack:** Python 3.12 / Flask / SQLite / GDAL / 原生 JS（无框架）/ pytest

## Global Constraints

- **`tile_size` 恒为 65，一个字都不许动。** 随包底图是 65×65 且物理植入任务目录，改了会在 z7→z8 交界处密度跳变；`tests/test_build_scripts_contract.py:48-62` 与 `tests/test_dem_task_tiler.py:56` 钉着它。
- **CLI（`cesiumlab_terrain.main`）与全球底图构建脚本继续用 `auto`。** 底图覆盖海洋与大片平原，是 martini 收益最大的场景，且只构建一次。应用侧与 CLI 侧的默认值**有意分叉**。
- **`build_terrain` 的 kwarg 默认值一个都不改**（`triangulator='auto'`、`normals=True`、`max_error_k=DEFAULT_MAX_ERROR_K`）。改了会连锁掀翻 `tests/test_terrain_normals.py:358`、`tests/test_rtin.py:398-403`、`tests/test_fix_terrain_pool_robustness.py:81`。新增参数一律带「保持既有行为」的默认值。
- **新字段必须加进 `TileParams`，不能加成 `tile_dem_task_dir` 的新形参。** 多个契约测试用三参/四参替身钉住调用形态（`tests/test_local_terrain_api.py:108` 是三参、`tests/test_fix_dem_tiling_stoppable.py:60` 是四参），加形参会 TypeError。
- **新 DB 列必须带 `DEFAULT`。** `tests/test_terrain_api.py:97-99` 有不列新列的裸 INSERT。
- **新配置键必须登记进 `_VALUE_RULES` 或 `_UNCONSTRAINED_KEYS`，二选一不可兼得**（`tests/test_fix_config_path_validation.py:230-240` 双向集合相等）。
- **新 i18n key 必须「定义了就被引用、引用了就有定义」**（`tests/test_i18n.py:238/259` 双向闭合）。
- **新测试一律用 `tests/conftest.py:231-261` 的 `fresh_import` 或 `:294-315` 的 `isolated_app`**，禁止手写 `sys.modules.pop`（conftest L10-12 的规矩）。
- 测试目录扁平，无子目录。新特性测试**不要**用 `test_fix_*` 前缀（那是回归修复档专用）。

---

## Phase 0：事实基线（开工前必读，不要凭记忆写代码）

### 允许调用的 API（已逐行核实，带出处）

| 符号 | 出处 | 签名 / 事实 |
|---|---|---|
| `build_terrain` | `src/services/terrain_tiling/cesiumlab_terrain.py:1269-1284` | 关键字参数含 `min_level` / `max_level` / `tile_size=17` / `workers=0` / `progress_cb` / `stage_cb` / `stop_flag` / `triangulator='auto'` / `max_error_k=DEFAULT_MAX_ERROR_K` / `normals=True` |
| max_level 解析点 | 同上 `:1360-1361` | `if max_level is None: max_level = scheme.estimate_max_level(sampler.pixel_size_deg)`，**在 `build_input_raster` 物化之后**（`:1349`）、`DemSampler` 打开之后（`:1357`） |
| `build_terrain` 返回值 | 同上 `:1577-1578` | `{"total","rendered","failed","chose_martini","chose_grid"}` 五个 key，**不含 max_level** |
| `GeographicTilingScheme.estimate_max_level` | 同上 `:177-187` | 入参是像素度数；`<= 0` 时兜底返回 14（`tests/test_fix_terrain_estimate_max_level.py:35-36` 钉死） |
| `TileParams` | `src/services/terrain_tiling/dem_task_tiler.py:28-55` | `@dataclass(frozen=True)`；`maxzoom:int`、`parent_url:str`、`tile_size=65`、`workers=0`、`triangulator='auto'`、`max_error_k=0.15`、三个回调。**无 normals 字段** |
| `tile_dem_task_dir` | 同上 `:58-63` | `(task_dir, out_dir, params, build_terrain_fn=None)` |
| build_terrain 实参列表 | 同上 `:127-139` | **没传 `normals`** → 走 kwarg 默认 `True` |
| 五键白名单 | 同上 `:143-145` | `keys = ("total","rendered","failed","chose_martini","chose_grid")`，**任何新返回 key 会被静默丢弃** |
| `min_level` | 同上 `:125` | `min(8, int(params.maxzoom)) if base_dir is not None else 0` |
| `merge_base_availability` | `src/services/terrain_tiling/layer_json.py:87` | **只收两个 Path，无 maxzoom 形参**；maxzoom 从两份 JSON 取 max（`:139-141`）→ 自动跟随写盘值，不会错 |
| `validate_zoom` | `src/services/geo_validation.py:61-69` | 接受 `12` 与 `"12"`；拒绝 12.5 / −1 / 22 / NaN / bool；抛 `ValueError` |
| ALTER 迁移模板 | `src/core/database.py:786-811` | `(table, coldef)` 元组 for 循环 + 吞 `duplicate column name` |
| 配置键默认值 | 同上 `:90-91` | `('terrain_global_base_maxzoom','7')`, `('terrain_local_maxzoom','14')` |
| 布尔配置约定 | `src/services/config_manager.py:294-295` | 一律存字符串 `'true'`/`'false'`，读取侧 `== 'true'` 或 `!= 'false'` |
| `_UNCONSTRAINED_KEYS` | 同上 `:303-314` | 布尔开关与「取值表住在引擎里」的枚举放这里 |
| i18n 条目 | `src/i18n/catalog/tpl_index.py:243-246` | `MESSAGES` dict，key = `tpl.index.process.<短名>`，值 `{'zh':…, 'en':…}`。**只有 `js.` 前缀会下发浏览器** |
| 表单 select 范本 | `templates/index.html:248-251` | 纯静态 select，option 文案走 `{{ t(...) }}` |
| 表单 checkbox 范本 | `templates/index.html:299-302` | `form-check` + `checked` |
| 提交点 | `static/js/map.js:1874-1923`（FormData）/ `:1930-1962`（JSON body） | 取值一律 `document.getElementById(id)?.value` |

### 反模式（做了就是 bug，不要犯）

- **不要**改 `build_terrain` 任何现有 kwarg 的默认值。
- **不要**给 `tile_dem_task_dir` 加形参。
- **不要**在 `dem_task_tiler` 里自己算 `estimate_max_level`：像素尺寸只存在于 `DemSampler`，而 `DemSampler` 必须开在 `build_input_raster` 物化之后的产物上（`build_input_raster` docstring `:482-496` 明确禁止把多源 VRT 交给它——overview 选层漂移会让相邻瓦片高程差到 50.9 m）。在 tiler 里复现基准 = 重复一次物化（6 幅 ASTER 约 92 MB）。
- **不要**把档位取值表抄第二份。白名单只有一个家（Task 6 建立），config_manager 从那里 import。
- **不要**新建 `tests/test_fix_*.py`：这是新特性，不是修复。
- **不要**动 `static/js/map.js` 里的 `resetForm({ clearBounds: false, formId: 'processForm' })`（`tests/test_map_js_contract.py:67-72` 断言它至少出现 2 次）。

### 一处必须正面处置的设计冲突

`tests/test_rtin.py:755-811` 把 **三份 triangulator 默认值钉成同一个值**：
- `:788` `TileParams.triangulator == build_terrain 签名默认`
- `:806` `CLI --triangulator 默认 == build_terrain 签名默认`

本方案要求应用侧 `grid`、CLI/底图侧 `auto` **有意分叉**，这条断言的前提不再成立。Task 3 专门处置，**不要在别的任务里顺手改它**。

---

## Phase 1：切片核心 —— 层级偏移与实际层级回报

### Task 1：`build_terrain` 支持 `level_offset`，并回报实际 max_level

**Files:**
- Modify: `src/services/terrain_tiling/cesiumlab_terrain.py:1269-1284`（签名）、`:1360-1361`（解析点）、`:1577-1578`（返回值）
- Test: `tests/test_fix_terrain_estimate_max_level.py`（追加，它已是层级估算的归属文件）

**Interfaces:**
- Produces: `build_terrain(..., level_offset: int = 0)`；返回 dict 新增 `"max_level": int`（实际切的最深层级）
- Consumes: 无

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_fix_terrain_estimate_max_level.py` 末尾：

```python
def _fake_sampler_cls(pixel_deg, bounds=(116.0, 39.0, 116.1, 39.1)):
    """最小可用的 DemSampler 替身：build_terrain 只用它的 pixel_size_deg 与 bounds。"""
    class _S:
        def __init__(self, path, nodata=None):
            self.pixel_size_deg = pixel_deg
            self.bounds = bounds
            self.ds = None
            self.band = None
    return _S


def _run_build(monkeypatch, tmp_path, **kw):
    """跑 build_terrain 但不真切瓦片：替掉 sampler 与 worker，只看层级决策。"""
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    monkeypatch.setattr(ct, "DemSampler", _fake_sampler_cls(180.0 / (64 * 2 ** 10)))
    monkeypatch.setattr(ct, "_worker_tile", lambda task: (0.0, 1.0, "grid"))
    return ct.build_terrain(["fake.tif"], str(tmp_path), tile_size=65, workers=1, **kw)


def test_level_offset_shifts_the_estimated_base(monkeypatch, tmp_path):
    """未传 max_level 时，基准 = estimate_max_level，偏移叠加在它上面。"""
    assert _run_build(monkeypatch, tmp_path / "a")["max_level"] == 10
    assert _run_build(monkeypatch, tmp_path / "b", level_offset=1)["max_level"] == 11
    assert _run_build(monkeypatch, tmp_path / "c", level_offset=-1)["max_level"] == 9


def test_level_offset_shifts_an_explicit_base(monkeypatch, tmp_path):
    """显式传了 max_level 时，它就是基准，偏移同样叠加。"""
    assert _run_build(monkeypatch, tmp_path / "d", max_level=14)["max_level"] == 14
    assert _run_build(monkeypatch, tmp_path / "e", max_level=14,
                      level_offset=-1)["max_level"] == 13


def test_level_offset_is_clamped_into_the_valid_range(monkeypatch, tmp_path):
    """负偏移不得把层级压到 0 以下，正偏移不得越过 MAX_ZOOM。"""
    assert _run_build(monkeypatch, tmp_path / "f", max_level=0,
                      level_offset=-1)["max_level"] == 0
    assert _run_build(monkeypatch, tmp_path / "g", max_level=21,
                      level_offset=1)["max_level"] == 21


def test_min_level_is_clamped_below_the_effective_max(monkeypatch, tmp_path):
    """min_level > max_level 会让 _tile_ranges 产出空区间 —— 切 0 张却报 completed。

    调用方按基准层级算 min_level（dem_task_tiler 恒传 8），下调偏移后基准可能
    低于 8，钳位必须在 build_terrain 里做，调用方不知道最终层级。
    """
    r = _run_build(monkeypatch, tmp_path / "h", min_level=8, max_level=5)
    assert r["max_level"] == 5
    assert r["total"] > 0, "min_level 未被钳到 max_level 以下，产出了空区间"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_fix_terrain_estimate_max_level.py -q`
Expected: FAIL —— `TypeError: build_terrain() got an unexpected keyword argument 'level_offset'`

- [ ] **Step 3: 改签名**

在 `cesiumlab_terrain.py:1283`（`normals: bool = True`）之后加一行：

```python
    normals: bool = True,
    level_offset: int = 0,
) -> dict:
```

- [ ] **Step 4: 改解析点**

把 `:1360-1361` 那两行替换成：

```python
        # 基准层级：显式传了就用它，否则按源像素尺寸估算。
        if max_level is None:
            max_level = scheme.estimate_max_level(sampler.pixel_size_deg)
        # 档位偏移叠加在基准上（精度 +1 / 均衡 0 / 速度 -1，见
        # docs/reference/terrain/tiling-presets-measured.md）。实测每加一级约
        # 3.3 倍体积换 2.8 倍精度，与源分辨率无关。
        # 钳位到 [0, MAX_ZOOM]：负层级会让 _tile_ranges 的 range() 直接空转，
        # 越界层级会让瓦片数按 4^n 爆炸。
        max_level = max(0, min(MAX_ZOOM, int(max_level) + int(level_offset)))
        # min_level 由调用方按【基准】算（dem_task_tiler 恒传 8），下调偏移后
        # 基准可能低于它。min_level > max_level 会让 _tile_ranges 产出空区间 ——
        # 切 0 张瓦片却报 completed，本仓栽过的同款静默成功。调用方拿不到最终
        # 层级，所以钳位只能在这里做。
        min_level = min(int(min_level), max_level)
```

在文件顶部的 import 区加（与既有 import 同段）：

```python
from src.services.geo_validation import MAX_ZOOM
```

- [ ] **Step 5: 改返回值**

把 `:1577-1578` 的返回 dict 加一个 key：

```python
        return {"total": total, "rendered": done, "failed": failed,
                # 实际切到的最深层级。档位偏移后它可能不等于调用方传进来的
                # max_level，调用方要据此落库/展示，不能拿请求值当产物事实。
                "max_level": max_level,
                "chose_martini": chose["martini"], "chose_grid": chose["grid"]}
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_fix_terrain_estimate_max_level.py -q`
Expected: PASS

- [ ] **Step 7: 跑受影响的既有测试**

Run: `.venv/bin/python -m pytest tests/test_rtin.py tests/test_terrain_normals.py tests/test_fix_terrain_pool_robustness.py tests/test_layer_json.py -q`
Expected: 全绿。**注意 `tests/test_fix_terrain_pool_robustness.py:84` 断言 `set(counts) == {"total","rendered","failed","chose_martini","chose_grid"}` —— 新增 `max_level` 会让它红。** 把那行改成：

```python
    assert set(counts) == {"total", "rendered", "failed", "max_level",
                           "chose_martini", "chose_grid"}
```

- [ ] **Step 8: 提交**

```bash
git add src/services/terrain_tiling/cesiumlab_terrain.py tests/test_fix_terrain_estimate_max_level.py tests/test_fix_terrain_pool_robustness.py
git commit -m "feat(terrain): build_terrain 支持 level_offset 并回报实际 max_level"
```

---

## Phase 2：参数体 —— 后端切换与法线透传

### Task 2：`TileParams` 改用 grid、新增 normals 与 level_offset

**Files:**
- Modify: `src/services/terrain_tiling/dem_task_tiler.py:28-55`（字段与注释）、`:125`（min_level）、`:127-139`（实参）、`:143`（白名单）
- Test: `tests/test_dem_task_tiler.py`（改 `:65`，追加两条）

**Interfaces:**
- Consumes: Task 1 的 `build_terrain(..., level_offset=)` 与返回值里的 `max_level`
- Produces: `TileParams.normals: bool = False`、`TileParams.level_offset: int = 0`、`TileParams.triangulator = 'grid'`；`tile_dem_task_dir` 返回 dict 新增 `max_level`

- [ ] **Step 1: 改既有断言（它会先红）**

`tests/test_dem_task_tiler.py:65` 改成：

```python
    assert captured["triangulator"] == "grid"
```

并把 `:57-64` 那段解释「auto 为何是默认」的注释整段替换成：

```python
    # 应用侧默认后端是 grid，不是 auto —— 实测 auto 在 6 个真实 DEM 上一个
    # Pareto 前沿都没进（多花 2.6~5.9 倍时间，省下的体积用「降一级」买更便宜）。
    # 依据见 docs/reference/terrain/tiling-presets-measured.md 第三、四节。
    # CLI 与全球底图脚本仍用 auto，那是有意分叉，见同文档第八节。
    # 少了这两条断言，把 tile_dem_task_dir 里的 triangulator/max_error_k 两行
    # 删掉全量照样绿。
```

- [ ] **Step 2: 追加新测试**

追加到 `tests/test_dem_task_tiler.py`（照抄该文件 `:33-51` 的替身形态）：

```python
def test_tile_dem_task_dir_threads_normals_and_offset(tmp_path: Path):
    """TileParams 的 normals / level_offset 必须真的进到 build_terrain 的 kwarg。

    normals 此前【根本没传】，走 build_terrain 的 kwarg 默认 True —— 应用侧
    因此拿不到关掉法线的能力，而实测法线吃 +35%~+100% 字节、约 2.1 倍时间，
    几何精度分毫不涨（tiling-presets-measured.md 第五节）。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "A_dem.tif").write_text("", encoding="utf-8")
    out_dir = tmp_path / "out"

    captured = {}

    def fake_build_terrain(**kwargs):
        captured.update(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text(
            '{"parentUrl":"OLD","available":[]}\n', encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0, "max_level": 13,
                "chose_martini": 0, "chose_grid": 1}

    params = TileParams(maxzoom=14, parent_url="https://example.com/p.json",
                        normals=True, level_offset=-1)
    counts = tile_dem_task_dir(task_dir, out_dir, params,
                               build_terrain_fn=fake_build_terrain)

    assert captured["normals"] is True
    assert captured["level_offset"] == -1
    # 基准仍按用户的 maxzoom 传，偏移由 build_terrain 叠加 —— tiler 不自己算终值。
    assert captured["max_level"] == 14
    # build_terrain 回报的实际层级必须穿过白名单活着出来。
    assert counts["max_level"] == 13


def test_tile_dem_task_dir_defaults_normals_off():
    """应用侧默认不出法线：前端 enableLighting 默认关，白背 26%~50% 字节。"""
    from src.services.terrain_tiling.dem_task_tiler import TileParams

    assert TileParams(maxzoom=14, parent_url="").normals is False
    assert TileParams(maxzoom=14, parent_url="").level_offset == 0
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_dem_task_tiler.py -q`
Expected: FAIL —— `TypeError: TileParams.__init__() got an unexpected keyword argument 'normals'`，以及 `assert 'auto' == 'grid'`

- [ ] **Step 4: 改 TileParams**

`dem_task_tiler.py:37-46` 那段（注释 + 两个字段）整体替换成：

```python
    # 三角化后端与误差系数。
    #
    # 后端默认 'grid'：实测 6 个真实 DEM、20 个配置的三轴（墙钟/字节/精度）
    # 支配判定里，'auto' 一个 Pareto 前沿都没进 —— 它多花 2.6~5.9 倍时间，而
    # 省下的体积用「降一级」买要便宜 2.4~3.9 倍。崎岖地形上它 98.8% 的瓦片
    # 本来就选 grid，纯属把同一个产物用 6 倍 CPU 重算一遍。
    # 依据：docs/reference/terrain/tiling-presets-measured.md 第三、四节。
    #
    # ⚠️ CLI（cesiumlab_terrain.main）与全球底图构建脚本仍用 'auto'，那是
    # **有意分叉**：底图覆盖海洋与大片平原（martini 收益最大），且只构建一次，
    # CPU 代价无所谓。不要"顺手统一"这两处，见同文档第八节末尾。
    #
    # max_error_k 在 grid 后端下不参与计算，保留是为了排障时切 'martini' 做对比。
    triangulator: str = "grid"
    max_error_k: float = 0.15
    # 逐顶点法线（oct 编码扩展段）。默认【关】：前端 enableLighting 默认关
    # （static/js/terrain_lighting.js:46-52），而实测法线吃 +35%~+100% 字节、
    # 约 2.1 倍切片时间，几何精度分毫不涨。此前这个开关根本没透传到
    # build_terrain，恒走它的 kwarg 默认 True。
    # ⚠️ 关掉后 layer.json 的 extensions 写成 []，Cesium 的 hasVertexNormals 是
    # provider 级单一标志 —— 光照按钮会静默退化成全球日夜渐变，且连植入的随包
    # 底图自带的法线也一起作废。UI 上必须写明这一点。
    normals: bool = False
    # 档位偏移：精度 +1 / 均衡 0 / 速度 -1，叠加在 maxzoom 上，由 build_terrain
    # 落地（那里是 max_level 唯一的解析点）。取值表住在
    # geo_validation.TILING_QUALITY_OFFSETS，不要在这里抄第二份。
    level_offset: int = 0
```

- [ ] **Step 5: 改 min_level 与实参列表**

`:121-125` 的 min_level 那段替换成：

```python
    # 底图独占 z0-z7，任务只出 z8+：两边零冲突，也没有「半张瓦片是真数据、
    # 半张是采到 DEM 外的外推值」那种接缝。
    # 恒传 8，不再在这里 min(8, maxzoom)：档位偏移后的最终层级只有
    # build_terrain 知道（它可能还要走 estimate），钳位因此挪进了那边
    # （见 cesiumlab_terrain 里 min_level = min(min_level, max_level) 那行）。
    min_level = 8 if base_dir is not None else 0
```

`:127-139` 的调用加两个实参：

```python
        triangulator=params.triangulator,
        max_error_k=params.max_error_k,
        normals=params.normals,
        level_offset=params.level_offset,
    )
```

- [ ] **Step 6: 改白名单**

`:143` 那行改成：

```python
    keys = ("total", "rendered", "failed", "max_level",
            "chose_martini", "chose_grid")
```

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_dem_task_tiler.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add src/services/terrain_tiling/dem_task_tiler.py tests/test_dem_task_tiler.py
git commit -m "feat(terrain): TileParams 默认改 grid，新增 normals/level_offset 透传"
```

### Task 3：处置 `test_rtin.py` 的默认值对账冲突

**Files:**
- Modify: `tests/test_rtin.py:755-811`
- Modify: `tests/test_build_scripts_contract.py`（追加一条，给 CLI 侧的 `auto` 一个家）

**Interfaces:**
- Consumes: Task 2 的 `TileParams.triangulator == 'grid'`
- Produces: 无（纯测试语义修订）

- [ ] **Step 1: 确认它现在是红的**

Run: `.venv/bin/python -m pytest tests/test_rtin.py::test_triangulation_defaults_agree_across_every_copy -q`
Expected: FAIL —— `TileParams.triangulator 'grid' 与 build_terrain 默认值 'auto' 不一致`

- [ ] **Step 2: 改 docstring 与断言**

把 `:756-770` 的 docstring 替换成：

```python
    """三角化默认值有多份副本。K 必须处处一致；后端**有意分叉**，只钉合法性。

    历史上三份 triangulator 默认值（TileParams / build_terrain 签名 / CLI
    argparse）被钉成同一个值。三档预设之后这个前提不再成立：
      - 应用侧 TileParams -> 'grid'（用户任务，实测 auto 不在 Pareto 前沿）
      - CLI / 全球底图    -> 'auto'（一次性构建，覆盖海洋与大片平原）
    依据见 docs/reference/terrain/tiling-presets-measured.md 第三节与第八节。

    分叉之后这里还能钉的是：两份默认值都必须是 build_terrain 白名单里的值。
    拼错会静默退回规则网格（build_terrain 入口校验的注释里记着这个坑），
    而白名单本身由 test_build_terrain_rejects_unknown_triangulator 守。
    两个具体字面量各有唯一的家，不在这里抄第三份：
      'grid' -> tests/test_dem_task_tiler.py（TileParams -> build_terrain 透传）
      'auto' -> tests/test_build_scripts_contract.py（全球底图脚本走 CLI 默认）

    K 这里刻意不写死 0.15：唯一的字面量归 test_dem_task_tiler。
    """
```

把 `:788-791` 替换成：

```python
    accepted = ("auto", "martini", "grid")
    assert params.triangulator in accepted, (
        f"TileParams.triangulator {params.triangulator!r} 不是 build_terrain "
        f"接受的值，会被入口校验拒掉")
```

把 `:806-808` 替换成：

```python
    assert captured["triangulator"] in accepted, (
        f"CLI --triangulator 默认 {captured['triangulator']!r} 不是 build_terrain "
        f"接受的值")
```

- [ ] **Step 3: 给 CLI 侧的 'auto' 一个家**

追加到 `tests/test_build_scripts_contract.py`：

```python
def test_global_base_build_keeps_the_adaptive_backend(tmp_path, monkeypatch):
    """全球底图必须走 auto，不能跟着应用侧改成 grid。

    底图覆盖海洋与大片平原 —— 正是 martini 减面收益最大、grid 字节代价最高的
    地方 —— 而它只构建一次，CPU 代价无所谓。应用侧用户任务的取舍条件正好相反
    （反复构建、用户指定 AOI），所以两边有意分叉。**这里是 'auto' 这个字面量
    在全仓的家**（'grid' 的家在 tests/test_dem_task_tiler.py）。
    依据：docs/reference/terrain/tiling-presets-measured.md 第八节末尾。
    """
    from src.services.terrain_tiling import cesiumlab_terrain as ct

    # 前提：脚本不传 --triangulator，所以它拿的是 CLI argparse 的默认值。
    assert "--triangulator" not in _ps1(), (
        "脚本显式传了 --triangulator，这条测试的前提（走 CLI 默认）不再成立")

    # 走真实 main()（只替掉 build_terrain），不去翻 parser 的内部结构 ——
    # 与 tests/test_rtin.py:796-804 用的是同一手法，顺带钉住 CLI 到
    # build_terrain 的透传。
    captured = {}

    def fake_build_terrain(inputs, output, **kw):
        captured.update(kw)

    src = tmp_path / "x.tif"
    src.write_bytes(b"")
    monkeypatch.setattr(ct, "build_terrain", fake_build_terrain)
    assert ct.main(["-i", str(src), "-o", str(tmp_path / "out")]) == 0

    assert captured["triangulator"] == "auto", (
        f"CLI --triangulator 默认变成了 {captured['triangulator']!r}；"
        f"全球底图会跟着变，而它正是 martini 收益最大的场景")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_rtin.py tests/test_build_scripts_contract.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_rtin.py tests/test_build_scripts_contract.py
git commit -m "test(terrain): 后端默认值有意分叉，对账改为合法性 + 各自的家"
```

---

## Phase 3：校验与配置

### Task 4：档位取值表与校验函数（唯一事实来源）

**Files:**
- Modify: `src/services/geo_validation.py`（在 `validate_zoom` 之后追加）
- Test: `tests/test_tiling_presets.py`（新建）

**Interfaces:**
- Produces: `TILING_QUALITY_OFFSETS: dict[str,int]`、`DEFAULT_TILING_QUALITY: str`、`validate_tiling_quality(value, name='quality') -> str`
- Consumes: 无

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tiling_presets.py`：

```python
"""高程切片三档预设：取值表、校验与层级偏移语义。

选型依据见 docs/reference/terrain/tiling-presets-measured.md。
"""
import pytest


def test_quality_offsets_are_a_one_step_ladder():
    """三档必须是「基准 ±1」的等距梯子 —— 实测每级 3.3 倍体积换 2.8 倍精度。"""
    from src.services.geo_validation import TILING_QUALITY_OFFSETS

    assert TILING_QUALITY_OFFSETS == {
        "precision": 1, "balanced": 0, "speed": -1}


def test_default_quality_is_balanced():
    from src.services.geo_validation import (DEFAULT_TILING_QUALITY,
                                             TILING_QUALITY_OFFSETS)

    assert DEFAULT_TILING_QUALITY == "balanced"
    assert TILING_QUALITY_OFFSETS[DEFAULT_TILING_QUALITY] == 0


@pytest.mark.parametrize("value", ["precision", "balanced", "speed"])
def test_validate_accepts_every_preset(value):
    from src.services.geo_validation import validate_tiling_quality

    assert validate_tiling_quality(value) == value


@pytest.mark.parametrize("value", ["", "fast", "BALANCED", None, 0, True])
def test_validate_rejects_anything_else(value):
    """拼错必须当场报错，不能静默退回默认档 —— 那是本仓栽过三次的失效形态。"""
    from src.services.geo_validation import validate_tiling_quality

    with pytest.raises(ValueError):
        validate_tiling_quality(value)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tiling_presets.py -q`
Expected: FAIL —— `ImportError: cannot import name 'TILING_QUALITY_OFFSETS'`

- [ ] **Step 3: 实现**

追加到 `src/services/geo_validation.py`（`validate_zoom` 之后）：

```python
# 切片档位 -> 相对基准层级的偏移。**这是全项目唯一的取值表**：config_manager
# 的校验规则、管理器的缺省、路由的收参都从这里取，不要抄第二份。
# 三档为什么是「基准 ±1」而不是换三角化后端：实测层级旋钮的性价比是简化后端
# 的 2.4~3.9 倍，且它省时间、后端花时间。
# 依据：docs/reference/terrain/tiling-presets-measured.md 第四节。
TILING_QUALITY_OFFSETS = {
    'precision': 1,   # 基准 +1：约 3.3 倍体积换 2.8 倍精度
    'balanced': 0,    # 基准，默认
    'speed': -1,      # 基准 -1：约 1/3.3 体积、1/2.5 耗时
}
DEFAULT_TILING_QUALITY = 'balanced'


def validate_tiling_quality(value, name='quality'):
    """校验切片档位,返回规范化后的 str。只接受取值表里的三个字面量。

    刻意不做大小写归一、不做前后空白裁剪、不静默退回默认档:
    build_terrain 早年有过「triangulator 拼错静默走 else 分支、作业照样
    completed」的坑,这里当场报错、错误直指病因。
    """
    if not isinstance(value, str) or value not in TILING_QUALITY_OFFSETS:
        allowed = ', '.join(sorted(TILING_QUALITY_OFFSETS))
        raise ValueError(f"{name} ({value!r}) must be one of: {allowed}")
    return value
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tiling_presets.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/services/geo_validation.py tests/test_tiling_presets.py
git commit -m "feat(terrain): 切片档位取值表与校验（唯一事实来源）"
```

### Task 5：两个配置键 + 四个 DB 列

**Files:**
- Modify: `src/core/database.py:91`（DEFAULT_CONFIGS）、`:786-803`（迁移元组）、`:575-592` 与 `:637-655`（建表语句）
- Modify: `src/services/config_manager.py:262-291`（`_VALUE_RULES`）、`:303-314`（`_UNCONSTRAINED_KEYS`）
- Test: `tests/test_tiling_presets.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `TILING_QUALITY_OFFSETS`、`DEFAULT_TILING_QUALITY`
- Produces: 配置键 `terrain_quality_preset`（默认 `'balanced'`）、`terrain_vertex_normals`（默认 `'false'`）；`dem_terrain_jobs` 与 `local_terrain_tasks` 各新增 `quality TEXT DEFAULT 'balanced'`、`vertex_normals INTEGER DEFAULT 0`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tiling_presets.py`。**注意导入形态**：全仓惯例是 `from conftest import fresh_import`（不是 `tests.conftest`，`tests/` 没有 `__init__.py`），且它单名返回模块本身、多名才返回列表（`conftest.py:239-240`）：

```python
from conftest import fresh_import  # noqa: E402  —— 放模块顶部，与全仓一致


def _fresh_db(monkeypatch, tmp_path):
    """新库 + 建表。禁止手写 sys.modules.pop（conftest.py:10-12 的规矩）。"""
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "t.db")
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    return db


def test_config_defaults_are_shipped(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    defaults = dict(db.DEFAULT_CONFIGS)

    assert defaults["terrain_quality_preset"] == "balanced"
    # 布尔配置在本仓一律存字符串 'true'/'false'（config_manager:294-295）
    assert defaults["terrain_vertex_normals"] == "false"


@pytest.mark.parametrize("table", ["dem_terrain_jobs", "local_terrain_tasks"])
def test_preset_columns_exist_with_defaults(monkeypatch, tmp_path, table):
    """新列必须带 DEFAULT —— tests/test_terrain_api.py 有不列新列的裸 INSERT。"""
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = {r[1]: r for r in cur.fetchall()}  # (cid,name,type,notnull,dflt,pk)
    finally:
        conn.close()

    assert "quality" in cols, f"{table} 缺 quality 列"
    assert "vertex_normals" in cols, f"{table} 缺 vertex_normals 列"
    assert cols["quality"][4] == "'balanced'"
    assert cols["vertex_normals"][4] == "0"
```
校验规则的测试**不放在新文件里**，追加到 `tests/test_fix_config_path_validation.py`（配置键校验的归属文件，那里有现成的 `cm` fixture，`:49-53`，它是 `ConfigManager()` 实例 —— `validate_config` 是**实例方法**，不能当静态方法调）。照抄该文件 `:210-223` 的 `parametrize(value, ok)` 形态：

```python
@pytest.mark.parametrize('value,ok', [
    ('precision', True),
    ('balanced', True),
    ('speed', True),
    ('fast', False),        # 不存在的档位
    ('', False),            # 空值不等于"用默认"
    ('Balanced', False),    # 刻意不做大小写归一
])
def test_tiling_quality_shape(cm, value, ok):
    """档位是枚举，脏值必须被配置接口拒掉。

    白名单从 geo_validation.TILING_QUALITY_OFFSETS 取，不在 config_manager
    里抄第二份 —— 见 _UNCONSTRAINED_KEYS 注释里关于"第二处事实来源"的说明。
    """
    assert cm.validate_config('terrain_quality_preset', value) is ok
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tiling_presets.py -q`
Expected: FAIL —— `KeyError: 'terrain_quality_preset'`

- [ ] **Step 3: 加配置键**

`src/core/database.py:91`（`('terrain_local_maxzoom', '14'),`）之后插入：

```python
    # 切片档位：precision / balanced / speed，语义是相对 maxzoom 的层级偏移
    # （+1 / 0 / -1）。取值表在 src/services/geo_validation.TILING_QUALITY_OFFSETS，
    # 这里只放默认值。选型实测见 docs/reference/terrain/tiling-presets-measured.md。
    ('terrain_quality_preset', 'balanced'),
    # 地形光照法线（oct 编码扩展段）。默认关：前端 enableLighting 默认也是关的，
    # 而法线吃 +35%~+100% 字节、约 2.1 倍切片时间，几何精度分毫不涨。
    # ⚠️ 关着切出来的瓦片，事后想开只能重切 —— 法线是烘焙进瓦片的。
    ('terrain_vertex_normals', 'false'),
```

- [ ] **Step 4: 加 DB 列**

`src/core/database.py:802`（`("dem_terrain_jobs", "total_tiles INTEGER DEFAULT 0"),`）之后插入：

```python
            # 三档预设（precision/balanced/speed）与法线开关。两张地形任务表
            # 都要：DEM 切片走 dem_terrain_jobs，本地地形走 local_terrain_tasks。
            # 必须带 DEFAULT —— tests/test_terrain_api.py 有不列新列的裸 INSERT。
            ("dem_terrain_jobs", "quality TEXT DEFAULT 'balanced'"),
            ("dem_terrain_jobs", "vertex_normals INTEGER DEFAULT 0"),
            ("local_terrain_tasks", "quality TEXT DEFAULT 'balanced'"),
            ("local_terrain_tasks", "vertex_normals INTEGER DEFAULT 0"),
```

同时在两处建表语句里补上同名同默认的列，让新库与迁移库结构一致：
- `dem_terrain_jobs` 建表（`:575-592`）
- `local_terrain_tasks` 建表（`:637-655`）

两处都在 `maxzoom` 那一列之后加：

```sql
                quality TEXT DEFAULT 'balanced',
                vertex_normals INTEGER DEFAULT 0,
```

- [ ] **Step 5: 登记校验规则**

`src/services/config_manager.py` 顶部 import 区加：

```python
from src.services.geo_validation import TILING_QUALITY_OFFSETS
```

`_VALUE_RULES`（`:262-291`）加一条：

```python
    # 档位是小而稳定的枚举，白名单直接从 geo_validation 的取值表取 ——
    # 不在这里抄第二份（那正是 _UNCONSTRAINED_KEYS 注释里说的第二处事实来源）。
    'terrain_quality_preset': lambda v: v in TILING_QUALITY_OFFSETS,
```

`_UNCONSTRAINED_KEYS`（`:303-314`）的布尔那一行加上新键：

```python
    'proxy_auto_detect', 'cache_enabled', 'dem_cache_enabled',
    'terrain_vertex_normals',
```

并把 `:302` 的注释补一句：

```python
#   - terrain_*_maxzoom / terrain_local_maxzoom：纯数值上限，同上不在本条范围。
#   - terrain_vertex_normals：布尔开关，按本表第一条理由；
#     terrain_quality_preset 反过来登记在 _VALUE_RULES —— 它的取值表只有三个
#     值且住在 geo_validation 里，直接 import 白名单不构成第二处事实来源。
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tiling_presets.py tests/test_fix_config_path_validation.py tests/test_config_manager.py tests/test_layer_json.py tests/test_local_terrain_schema.py -q`
Expected: PASS（`test_fix_config_path_validation.py:230` 的双向集合相等是这一步的主闸门）

- [ ] **Step 7: 提交**

```bash
git add src/core/database.py src/services/config_manager.py tests/test_tiling_presets.py
git commit -m "feat(terrain): 档位与法线的配置键 + 两张任务表的落库列"
```

---

## Phase 4：管理器

### Task 6：DEM 切片管理器接收并透传档位与法线

**Files:**
- Modify: `src/services/dem_task_manager.py:265`（`start_tiling` 签名）、`:305-314`（校验/缺省）、`:325-346`（upsert）、`:362-371`（线程 args）、`:407-409`（`_run_tiling_job` 签名）、`:517-521`（TileParams 构造）
- Test: `tests/test_terrain_api.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `validate_tiling_quality` / `TILING_QUALITY_OFFSETS` / `DEFAULT_TILING_QUALITY`；Task 2 的 `TileParams(normals=, level_offset=)`
- Produces: `DemTaskManager.start_tiling(task_id, maxzoom=None, quality=None, vertex_normals=None)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_terrain_api.py`（照抄该文件 `:58-84` 的形态）：

```python
def test_terrain_start_persists_preset_defaults(monkeypatch, tmp_path):
    """不传档位时落库出厂默认：balanced + 法线关。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    assert client.post(f"/api/terrain/dem/{task_id}/start").status_code == 200

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        job = cur.fetchone()
    finally:
        conn.close()

    assert job["quality"] == "balanced"
    assert job["vertex_normals"] == 0


def test_terrain_start_persists_explicit_preset(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    monkeypatch.setattr(app_mod.dem_task_manager.__class__, "_run_tiling_job",
                        lambda self, *a, **k: None)

    r = client.post(f"/api/terrain/dem/{task_id}/start",
                    json={"quality": "speed", "vertex_normals": True})
    assert r.status_code == 200

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dem_terrain_jobs WHERE task_id=?", (task_id,))
        job = cur.fetchone()
    finally:
        conn.close()

    assert job["quality"] == "speed"
    assert job["vertex_normals"] == 1


def test_terrain_start_rejects_unknown_quality(monkeypatch, tmp_path):
    """拼错的档位当场 400，不静默退回 balanced。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    task_id = _insert_dem_task(db, tmp_path / "downloads")

    r = client.post(f"/api/terrain/dem/{task_id}/start", json={"quality": "fast"})
    assert r.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_terrain_api.py -q`
Expected: FAIL —— `sqlite3.Row` 里没有 `quality`（Task 5 已加列则改为断言值不符 / 400 未返回）

- [ ] **Step 3: 改 `start_tiling` 签名与缺省解析**

`dem_task_manager.py:265` 改成：

```python
    def start_tiling(self, task_id, maxzoom=None, quality=None,
                     vertex_normals=None):
```

在 `:305-314` 现有 maxzoom 缺省逻辑之后，照同一形态追加：

```python
        # 档位与法线：请求未给就取配置默认，与 maxzoom 完全同形。
        # 校验放在这里而不是路由层：DEM 这条路径的 maxzoom 校验就在管理器里
        # （local 那条在路由层），两个新参数跟着各自路径的既有位置走。
        if quality is None:
            quality = (self.config.get('terrain_quality_preset', DEFAULT_TILING_QUALITY)
                       or DEFAULT_TILING_QUALITY)
        quality = validate_tiling_quality(quality)
        if vertex_normals is None:
            vertex_normals = (
                self.config.get('terrain_vertex_normals', 'false') or 'false') == 'true'
        vertex_normals = bool(vertex_normals)
```

在文件顶部 import 区加：

```python
from src.services.geo_validation import (DEFAULT_TILING_QUALITY,
                                         validate_tiling_quality)
```

> 注意：读配置用的是**该管理器既有的 `self.config`**（见 `:298`、`:300`、`:310` 的现状写法），不要新建 `ConfigManager` 实例。缺省值逐字与 `DEFAULT_CONFIGS` 一致 —— 这是 `:296-298` 注释里点名过的规矩（兜底值和出厂默认不一致会造出「改了没反应」的假旋钮）。

- [ ] **Step 4: 落库与线程透传**

`:325-346` 的 upsert 语句列名与占位符各加两项 `quality, vertex_normals`，值传 `(quality, 1 if vertex_normals else 0)`。

`:362-371` 的线程 args 追加 `quality, vertex_normals`，并同步改 `:407-409` 的 `_run_tiling_job` 签名。

- [ ] **Step 5: 构造 TileParams**

`:517-521` 改成：

```python
            params = TileParams(
                maxzoom=maxzoom,
                parent_url=parent_url,
                normals=vertex_normals,
                level_offset=TILING_QUALITY_OFFSETS[quality],
                progress_cb=progress_cb,
                stage_cb=stage_cb,
                stop_flag=stop_flag,
            )
```

import 补上 `TILING_QUALITY_OFFSETS`。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_terrain_api.py tests/test_fix_dem_tiling_stoppable.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add src/services/dem_task_manager.py tests/test_terrain_api.py
git commit -m "feat(terrain): DEM 切片作业接收档位与法线开关"
```

### Task 7：本地地形管理器接收并透传档位与法线

**Files:**
- Modify: `src/services/local_terrain_task_manager.py:121-126`（`create_task_with_files` 签名）、`:147-149`（校验）、`:180-188`（INSERT）、`:331-382`（`start_tiling` 从库读回）、`:468-475`（TileParams 构造）
- Test: `tests/test_local_terrain_api.py`（追加）

**Interfaces:**
- Consumes: 同 Task 6
- Produces: `create_task_with_files(name, files, maxzoom=None, quality=None, vertex_normals=None)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_local_terrain_api.py`（照抄该文件 `:101-130` 的三参替身形态）：

```python
def test_preset_reaches_tile_params(monkeypatch, tmp_path):
    """档位 -> level_offset、法线 -> normals，必须原样进到 TileParams。"""
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["level_offset"] = params.level_offset
        calls["normals"] = params.normals
        calls["triangulator"] = params.triangulator
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(
        name="local-preset", files=[("a.tif", b"fake")], maxzoom=11,
        quality="speed", vertex_normals=True)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["level_offset"] == -1
    assert calls["normals"] is True
    # 应用侧后端恒为 grid，档位不改后端。
    assert calls["triangulator"] == "grid"


def test_preset_defaults_when_omitted(monkeypatch, tmp_path):
    db, mgr_mod = _reload(monkeypatch, tmp_path)
    mgr = mgr_mod.LocalTerrainTaskManager(socketio=None)

    calls = {}

    def fake_tile(task_dir, out_dir, params):
        calls["level_offset"] = params.level_offset
        calls["normals"] = params.normals
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mgr_mod, "tile_dem_task_dir", fake_tile)

    task_id = mgr.create_task_with_files(
        name="local-default", files=[("a.tif", b"fake")], maxzoom=11)
    th = mgr.active_tasks.get(task_id)
    if th:
        th.join(timeout=5)

    assert calls["level_offset"] == 0
    assert calls["normals"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_local_terrain_api.py -q`
Expected: FAIL —— `TypeError: create_task_with_files() got an unexpected keyword argument 'quality'`

- [ ] **Step 3: 实现**

按 Task 6 完全相同的形态改这五处；差异只有：本地这条路径的校验按既有惯例留在**路由层**（`:147-149` 现有的 `validate_zoom` 就在管理器里，跟着它放即可），且 `start_tiling`（`:331-382`）要从库把 `quality` / `vertex_normals` 读回来（重跑走这条路）。

读回处照 `int(row['maxzoom'])` 的形态写：

```python
        quality = validate_tiling_quality(row['quality'] or DEFAULT_TILING_QUALITY)
        vertex_normals = bool(row['vertex_normals'])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_local_terrain_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/services/local_terrain_task_manager.py tests/test_local_terrain_api.py
git commit -m "feat(terrain): 本地地形任务接收档位与法线开关"
```

---

## Phase 5：路由

### Task 8：两个入口收参

**Files:**
- Modify: `src/routes/terrain_api.py:24-44`
- Modify: `src/routes/local_terrain_api.py:32-70`
- Test: 已由 Task 6 / Task 7 的 HTTP 层测试覆盖

**Interfaces:**
- Consumes: Task 6 / Task 7 的管理器签名
- Produces: 两个端点接受 `quality`（字符串）与 `vertex_normals`（布尔 / `'true'`）

- [ ] **Step 1: 确认 Task 6 的 HTTP 测试仍红**

Run: `.venv/bin/python -m pytest tests/test_terrain_api.py -k preset -q`
Expected: FAIL（路由还没把参数转给管理器）

- [ ] **Step 2: 改 DEM 入口**

`src/routes/terrain_api.py` 在现有 maxzoom 收参之后，照同一形态加：

```python
    quality = payload.get('quality') or request.form.get('quality')
    if quality == '':
        quality = None
    raw_normals = payload.get('vertex_normals')
    if raw_normals is None:
        raw_normals = request.form.get('vertex_normals')
    # 表单传的是字符串 'true'/'false'，JSON 传的是真布尔。两种都收。
    vertex_normals = (None if raw_normals is None or raw_normals == ''
                      else (raw_normals is True or str(raw_normals).lower() == 'true'))
```

并把它们传给 `start_tiling(...)`。`ValueError → 400` 的既有映射会自动覆盖非法档位。

- [ ] **Step 3: 改本地地形入口**

`src/routes/local_terrain_api.py` 同形加两个 form 字段，传给 `create_task_with_files(...)`，并在路由层调 `validate_tiling_quality`（与该文件 `:38-40` 现有的 `validate_zoom` 位置一致）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_terrain_api.py tests/test_local_terrain_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/routes/terrain_api.py src/routes/local_terrain_api.py
git commit -m "feat(terrain): 两个切片入口收档位与法线参数"
```

---

## Phase 6：前端

### Task 9：任务表单控件 + i18n

**Files:**
- Modify: `templates/index.html:268-275`（`#localTerrainOptions` 内）
- Modify: `src/i18n/catalog/tpl_index.py`（`:243-246` 附近）
- Test: `tests/test_terrain_lighting_frontend.py`（追加控件 id 契约）

**Interfaces:**
- Produces: DOM id `localTerrainQuality`（select）、`localTerrainNormals`（checkbox）
- Consumes: 无

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_terrain_lighting_frontend.py`：

```python
def test_tiling_preset_controls_exist_in_the_process_form():
    """档位下拉与法线复选框的 id 是接线契约 —— 改名会让提交静默丢参数。

    map.js 用 getElementById 取值，id 对不上时 `?.value` 返回 undefined，
    FormData 里就没有这个字段，后端取配置默认 —— 全程零报错，用户选的档位
    悄悄不生效。本仓栽过同形态的坑（见本文件头部清单）。
    """
    # 该文件已有 INDEX_HTML 常量与 _read() 助手（:48、:54-56），照用，
    # 不要另起 pathlib —— 文件里没有 import Path。re 已在 :34 导入。
    html = _read(INDEX_HTML)
    assert 'id="localTerrainQuality"' in html
    for value in ("precision", "balanced", "speed"):
        assert f'value="{value}"' in html, f"档位下拉缺 {value} 选项"
    # 不能只断言 'type="checkbox"' 存在 —— index.html:299-302 本来就有复选框，
    # 那样写恒真、什么都保不住。必须锁定这一个控件本身。
    tag = re.search(r'<input[^>]*id="localTerrainNormals"[^>]*>', html)
    assert tag, "找不到 id=localTerrainNormals 的输入控件"
    assert 'type="checkbox"' in tag.group(0), (
        f"法线控件不是复选框：{tag.group(0)}")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_terrain_lighting_frontend.py -q`
Expected: FAIL

- [ ] **Step 3: 加控件**

`templates/index.html` 的 `#localTerrainOptions` 内、`localTerrainMaxzoom` 之后插入（select 抄 `:248-251` 的静态形态，checkbox 抄 `:299-302`）：

```html
        <div class="mb-2">
          <label class="form-label" for="localTerrainQuality">{{ t('tpl.index.process.terrain_quality') }}</label>
          <select class="form-select" id="localTerrainQuality">
            <option value="precision">{{ t('tpl.index.process.terrain_quality_precision') }}</option>
            <option value="balanced" selected>{{ t('tpl.index.process.terrain_quality_balanced') }}</option>
            <option value="speed">{{ t('tpl.index.process.terrain_quality_speed') }}</option>
          </select>
          <div class="form-text">{{ t('tpl.index.process.terrain_quality_hint') }}</div>
        </div>
        <div class="form-check mb-2">
          <input class="form-check-input" type="checkbox" id="localTerrainNormals">
          <label class="form-check-label" for="localTerrainNormals">{{ t('tpl.index.process.terrain_normals') }}</label>
          <div class="form-text">{{ t('tpl.index.process.terrain_normals_hint') }}</div>
        </div>
```

- [ ] **Step 4: 加 i18n 条目**

`src/i18n/catalog/tpl_index.py` 的 `MESSAGES` 里，`local_terrain_maxzoom` 条目旁加：

```python
    'tpl.index.process.terrain_quality': {
        'zh': '切片档位', 'en': 'Tiling quality'},
    'tpl.index.process.terrain_quality_precision': {
        'zh': '精度（比所选层级多一级，体积约 3.3 倍）',
        'en': 'Precision (one level deeper, ~3.3x size)'},
    'tpl.index.process.terrain_quality_balanced': {
        'zh': '均衡（按所选层级，推荐）',
        'en': 'Balanced (as selected, recommended)'},
    'tpl.index.process.terrain_quality_speed': {
        'zh': '速度（比所选层级少一级，体积约 1/3.3）',
        'en': 'Speed (one level shallower, ~1/3.3 size)'},
    'tpl.index.process.terrain_quality_hint': {
        'zh': '每档相差一个层级：约 3.3 倍体积换 2.8 倍精度。',
        'en': 'Each step is one zoom level: ~3.3x size for ~2.8x accuracy.'},
    'tpl.index.process.terrain_normals': {
        'zh': '生成地形光照法线', 'en': 'Generate terrain lighting normals'},
    'tpl.index.process.terrain_normals_hint': {
        'zh': '不勾选则地图上的地形光照按钮无效。体积多 35%~100%，切片慢约一倍，'
              '地形精度不变。切完想改只能重新切片。',
        'en': 'Unchecked disables the terrain lighting button. Costs 35-100% more '
              'size and about 2x tiling time, with no accuracy gain. Changing it '
              'later requires re-tiling.'},
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_terrain_lighting_frontend.py tests/test_i18n.py tests/test_css_contract.py tests/test_config_form_submittable.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add templates/index.html src/i18n/catalog/tpl_index.py tests/test_terrain_lighting_frontend.py
git commit -m "feat(terrain): 任务表单加切片档位与法线开关"
```

### Task 10：提交路径接线

**Files:**
- Modify: `static/js/map.js:1874-1923`（`submitLocalTerrain`，FormData）、`:1930-1962`（`startDemTaskTerrainTiling`，JSON body）
- Test: `tests/test_map_js_contract.py`（追加）

**Interfaces:**
- Consumes: Task 9 的 DOM id；Task 8 的端点参数名
- Produces: 无

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_map_js_contract.py`：

```python
def test_terrain_submit_sends_the_preset_fields():
    """两个提交点都必须带上档位与法线，否则用户的选择静默丢失。"""
    # 该文件已有 _map_js() 助手（:21-24）。文件里没有 import Path，别另起 pathlib。
    code = _map_js()

    assert "localTerrainQuality" in code
    assert "localTerrainNormals" in code
    # 上传分支走 FormData，DEM 分支走 JSON body，两处都要有。
    assert code.count("localTerrainQuality") >= 2, (
        "档位只接线了一个提交入口")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_map_js_contract.py -q`
Expected: FAIL

- [ ] **Step 3: 接线 FormData 分支**

`submitLocalTerrain` 里，紧跟现有 `formData.append('maxzoom', ...)` 之后加：

```javascript
        formData.append('quality', document.getElementById('localTerrainQuality')?.value || 'balanced');
        formData.append('vertex_normals', document.getElementById('localTerrainNormals')?.checked ? 'true' : 'false');
```

- [ ] **Step 4: 接线 JSON 分支**

`startDemTaskTerrainTiling` 的 body 对象加两个字段：

```javascript
                quality: document.getElementById('localTerrainQuality')?.value || 'balanced',
                vertex_normals: !!document.getElementById('localTerrainNormals')?.checked,
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_map_js_contract.py tests/test_tasks_js_contract.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add static/js/map.js tests/test_map_js_contract.py
git commit -m "feat(terrain): 两个提交入口带上档位与法线"
```

### Task 11：详情面板显示已用档位

**Files:**
- Modify: `static/js/history.js:492-549`（`refreshTerrainDetail`，现在显示 `job.maxzoom`）
- Modify: `src/i18n/catalog/js_map.py`（或该文件所属的 `js.*` 目录模块）
- Test: `tests/test_tasks_js_contract.py`（追加）

**Interfaces:**
- Consumes: Task 6 落库的 `quality` / `vertex_normals` 字段（需确认作业详情接口把它们吐出来）
- Produces: 无

> **前置核对**：`history.js:469` 那个起切入口**不带 body**，且 `base.html:157-176` 的详情面板里没有任何输入控件。本任务**不给它加控件** —— 那条路径走配置默认值即可。但它必须**显示**实际用的档位，否则用户在那里起切完全不知道用了什么。

- [ ] **Step 1: 写失败测试**

```python
def test_terrain_detail_shows_the_preset():
    """详情面板必须显示实际用的档位 —— 那个起切入口没有档位控件，走配置默认，
    不显示的话用户无从知道切出来的是哪一档。"""
    # 该文件已有 _js(name) 助手（:38-40）。文件里没有 import Path。
    code = _js("history.js")

    # 只断言 "quality" 太弱：history.js 里可能有别的同名词。锁定字段读取形态。
    assert re.search(r'\bjob\.quality\b', code), (
        "详情面板没有读作业的 quality 字段")
    assert re.search(r'\bjob\.vertex_normals\b', code), (
        "详情面板没有读作业的 vertex_normals 字段")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tasks_js_contract.py -q`
Expected: FAIL

- [ ] **Step 3: 确认接口吐出字段**

先跑一遍确认作业详情接口返回了新列（`SELECT *` 则自动带上）：

Run: `.venv/bin/python -m pytest tests/test_terrain_api.py -q`

**已核实：不需要改后端。** `DemTaskManager.get_tiling_job`（`src/services/dem_task_manager.py:619-628`）用的是 `SELECT * FROM dem_terrain_jobs` + `dict(row)`，Task 5 加的两列会自动出现在响应里。这一步只做确认，不改代码。

- [ ] **Step 4: 改显示**

`refreshTerrainDetail` 里现有显示 `job.maxzoom` 的那行之后追加档位与法线状态（文案走 `js.` 前缀的 i18n key，并同步加进目录）。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tasks_js_contract.py tests/test_i18n.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add static/js/history.js src/i18n/catalog/ tests/test_tasks_js_contract.py
git commit -m "feat(terrain): 任务详情显示实际使用的切片档位"
```

---

## Phase 7：文档

### Task 12：更新地形文档

**Files:**
- Modify: `docs/reference/terrain/tiling-presets-measured.md`（第九节「落地要动哪里」改成「已落地」并指向实际实现）
- Modify: `docs/reference/terrain/README.md:9`（`triangulation-backends-measured.md` 那行提到「暂不再上更强简化后端」，需补一句指向本次改动）

- [ ] **Step 1: 改实测文档的落地节**

把第九节的表格从「要做的事」改成「实现位置」，逐行填上最终落点。第十节「明确不要动的旋钮」保持原样（`tile_size` / `max_error_k` / `workers` 的结论未变）。

- [ ] **Step 2: 在索引里补一句**

`docs/reference/terrain/README.md:9` 的 `triangulation-backends-measured.md` 描述末尾追加：

```
（其中「暂不再上更强简化后端」的结论已被 tiling-presets-measured.md 取代：
应用侧现在连 auto 都不用了，统一走 grid）
```

- [ ] **Step 3: 提交**

```bash
git add docs/reference/terrain/
git commit -m "docs(terrain): 三档预设落地后更新实测文档与索引"
```

---

## Phase 8：验证

### Task 13：全量与真实冒烟

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest tests -q`
Expected: 0 failed。**基线是本计划开工前的通过数**（当时是 1589 passed / 3 skipped，但仓库在动，以开工前实测为准）。

- [ ] **Step 2: 真实切片冒烟 —— 三档产物必须真的不同**

这一步是本计划的**交付证明**，不能用测试替代：

```bash
DEM=~/.cache/mapdl-probe/dem/huabei_N36_E115.tif
for q in precision balanced speed; do
  .venv/bin/python - <<PY
from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir
from src.services.geo_validation import TILING_QUALITY_OFFSETS
import pathlib, shutil, time
out = pathlib.Path("/tmp/preset_smoke/$q"); shutil.rmtree(out, ignore_errors=True)
src = pathlib.Path("/tmp/preset_smoke/src_$q"); src.mkdir(parents=True, exist_ok=True)
shutil.copy("$DEM", src / "a_dem.tif")
p = TileParams(maxzoom=12, parent_url="", level_offset=TILING_QUALITY_OFFSETS["$q"])
t = time.time(); c = tile_dem_task_dir(src, out, p)
n = sum(1 for _ in out.rglob("*.terrain"))
b = sum(f.stat().st_size for f in out.rglob("*.terrain"))
print(f"$q: 实际层级 {c['max_level']} 张数 {n} 体积 {b/1e6:.1f}MB 耗时 {time.time()-t:.1f}s")
PY
done
```

Expected（huabei，基准 z12）：
- `precision`: 实际层级 **13**，张数约 3607，体积约 24.6 MB
- `balanced`: 实际层级 **12**，张数约 1445，体积约 6.7 MB
- `speed`: 实际层级 **11**，张数约 580，体积约 2 MB

**判定标准**：三档的实际层级必须是 13 / 12 / 11，体积逐档约 1/3.3。**若三档产物相同，说明 `level_offset` 没接通** —— 这正是本计划最可能的静默失败点。

- [ ] **Step 3: 法线开关冒烟**

```bash
.venv/bin/python - <<'PY'
import json, pathlib
for q in ("balanced",):
    lj = json.loads((pathlib.Path(f"/tmp/preset_smoke/{q}") / "layer.json").read_text())
    print(q, "extensions =", lj["extensions"])
PY
```

Expected: `extensions = []`（默认关法线）。传 `normals=True` 重跑一次应得 `["octvertexnormals"]`。

- [ ] **Step 4: 浏览器冒烟**

启动应用，创建一个本地地形任务，分别用三档各切一次，在地图上加载并确认：
1. 三档都能正常渲染（不是黑屏、不是塌成平面）；
2. 速度档明显更粗糙但无裂缝；
3. 不勾法线时，点地形光照按钮没有地形明暗（这是**预期行为**，UI 文案已写明）。

- [ ] **Step 5: 清理临时产物**

```bash
rm -rf /tmp/preset_smoke
```

- [ ] **Step 6: 提交**

```bash
git commit --allow-empty -m "chore(terrain): 三档预设端到端验证通过"
```

---

## Phase 9（可选，可整段砍掉）：自动基准层级

> **这一段与前八个阶段完全解耦，砍掉不影响三档可用。** 单独列出来是因为它的代价明显更高，值得单独拍板。

**要解决的问题**：`maxzoom` 恒传固定值（配置默认 14），不看源分辨率。93 m 的 DEM 建到 z14 是 77.4 MB，按 `estimate_max_level`(=12) 只要 6.9 MB —— **11 倍体积**换不到任何新地形；反过来 5 m 的 DEM 被 14 截断，细节根本没进瓦片。

**为什么不放进主线**：`maxzoom` 列现在是 **NOT NULL**，且被 `tests/test_layer_json.py:48` 与 `tests/test_local_terrain_schema.py:40-54` 钉死；`local_terrain_task_manager.py:343-366` 回读处是裸 `int(row['maxzoom'])`。要表达「自动」就得让它可空（改 schema + 两条测试 + 回读容错），或者另加一个 `auto_maxzoom INTEGER DEFAULT 0` 标志列。前者动既有约束，后者再加两列。

**若要做，最小路径**（不改 NOT NULL）：
1. 两张表各加 `auto_maxzoom INTEGER DEFAULT 0`。
2. 表单的 maxzoom 输入旁加一个「自动（按源分辨率）」复选框，勾上时禁用数字框。
3. 管理器在 `auto_maxzoom=1` 时给 `TileParams.maxzoom` 传 `None`；`tile_dem_task_dir` 把 `max_level=params.maxzoom` 原样传（`None` 会让 `build_terrain` 走 `estimate_max_level`）—— Task 1 已经让这条路径可用，**不需要再改切片核心**。
4. 详情面板显示 `counts["max_level"]`（Task 1 已回报）——自动模式下这是用户唯一能知道实际层级的地方，**必须显示**。
5. 新增测试：用 `tests/conftest.py:264-291` 的 `geotiff_bytes(pixel_deg=...)` 造不同分辨率的源，断言自动模式下实际层级随源分辨率变化，且 `pixel_deg=0` 时走 `estimate_max_level` 的兜底 14。

---

## 自检记录

**规格覆盖**：三档预设（Task 1/2/4/5/6/7/8/9/10）、法线独立开关（Task 2/5/6/7/8/9/10）、CLI 与底图不动（Global Constraints + Task 3）、`tile_size` 不动（Global Constraints）、文档（Task 12）、验证（Task 13）。自动基准层级刻意留在 Phase 9。

**已知会红并已安排处置**：`tests/test_dem_task_tiler.py:65`（Task 2 Step 1）、`tests/test_rtin.py:788/806`（Task 3）、`tests/test_fix_terrain_pool_robustness.py:84`（Task 1 Step 7）。

**注释与代码矛盾、必须同步改**：`dem_task_tiler.py:37-44`（Task 2 Step 4）、`tests/test_dem_task_tiler.py:57-64`（Task 2 Step 1）、`tests/test_terrain_lighting_frontend.py:121`「瓦片里的 oct 法线段是无条件落盘的」在默认关法线后不再成立（Task 9 顺带改）。

**类型一致性**：`level_offset` 全链路是 `int`；`quality` 全链路是 `str`，只在 `TILING_QUALITY_OFFSETS[quality]` 处转成 offset；`vertex_normals` 在 HTTP 层是 `bool`/`'true'`、在 DB 是 `INTEGER 0/1`、在 `TileParams` 是 `bool`，三处转换点分别在路由（Task 8）、管理器落库（Task 6/7）、管理器构造 TileParams（Task 6/7）。
