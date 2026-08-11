# 切片档位锚回源分辨率（`maxzoom` 自动挡）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给地形切片的 `maxzoom` 增加「自动」一态（按源数据像素尺寸现算基准层级），使切片档位（精细/均衡/快速 = 层级偏移 +1/0/−1）真正等于「顶点间距 ≈ 0.3 / 0.6 / 1.2 × 源像素」，而不是「比用户填的数字 ±1 级」。

**Architecture:** `build_terrain` 里 `max_level=None → estimate_max_level(sampler.pixel_size_deg)` 的分支**早已存在且有测试钉住**，只是应用侧恒传数字走不到它。本计划把「自动」这一态从表单一路打通到 `TileParams(maxzoom=None)`：新增唯一把关点 `geo_validation.coerce_maxzoom`（三态 `int` / `'auto'` / `None`），落库用哨兵 `-1`（两张表的该列是 `INTEGER NOT NULL`，去掉约束要重建表，违背本仓「幂等 ALTER ADD COLUMN」的迁移约定），出厂默认改成 `'auto'` 并对存量库做一次 `user_version 3 → 4` 的选择性改写。护栏是纯预告：`/api/raster/inspect` 增返逐层瓦片数，表单在起切前显示预计张数与体积。

**Tech Stack:** Python 3.12 / Flask / SQLite（`PRAGMA user_version` 迁移）/ 原生 JS（无框架）/ Jinja2 / pytest。虚拟环境是 `uv`，**所有命令走 `uv run`**。

**设计依据：** [`docs/superpowers/specs/2026-08-10-terrain-auto-maxzoom-design.md`](../specs/2026-08-10-terrain-auto-maxzoom-design.md)
**实测依据：** [`docs/reference/terrain/tiling-presets-measured.md`](../../reference/terrain/tiling-presets-measured.md)

## Global Constraints

- **所有命令走 `uv run`**（`.venv/` 已在项目根目录，不要手动 `source`）。
- **不改档位取值表**：`geo_validation.TILING_QUALITY_OFFSETS` 的 `{'precision': 1, 'balanced': 0, 'speed': -1}` 与 `DEFAULT_TILING_QUALITY = 'balanced'` 一个字都不动。
- **不改这四个旋钮**：`tile_size`（恒 65）、`max_error_k`（0.15）、`workers`、`triangulator`（应用侧恒 `'grid'`）。
- **不改 `src/services/terrain_tiling/cesium_terrain.py`**。估算 / 偏移 / 钳位三段已经是对的。
- **偏移表不许在 JS 里抄第二份**：前端要用偏移值时，由服务端渲染进 `<option data-offset>`（见 Task 8）。
- **i18n 双向闭合**：每个新 key 必须 `zh` / `en` 双份，且必须被源码以**完整字面量**引用（不许字符串拼接）——`tests/test_i18n.py` 两个方向都会红。
- **前端不许抄一份默认值**：提交时的兜底一律是空串（= 未传 = 走配置默认），由 `tests/test_map_js_contract.py::test_terrain_submit_lets_the_backend_supply_the_defaults` 钉住。
- **哨兵 `-1` 只在 `geo_validation` 的两个 helper 里出现**，调用方不许自己写 `-1`。
- 每个 Task 结束前跑该 Task 列出的测试命令并确认通过，再提交。**不要 `git push`。**

---

### Task 1: `coerce_maxzoom` 与哨兵翻译（校验层）

**Files:**
- Modify: `src/services/geo_validation.py`（在 `coerce_vertex_normals` 之后、`resolve_output_dir` 之前插入，即 `:135` 与 `:137` 之间）
- Test: `tests/test_terrain_auto_maxzoom.py`（新建）

**Interfaces:**
- Consumes: 现有 `validate_zoom(value, name) -> int`（值域 `MIN_ZOOM=0` .. `MAX_ZOOM=21`，非数字/布尔/越界一律 `ValueError`）
- Produces:
  - `AUTO_MAXZOOM: str = 'auto'`
  - `AUTO_MAXZOOM_SENTINEL: int = -1`
  - `coerce_maxzoom(value, name='maxzoom') -> int | 'auto' | None`
  - `maxzoom_to_db(maxzoom: int | 'auto') -> int`
  - `maxzoom_from_db(value: int) -> int | None`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_terrain_auto_maxzoom.py`：

```python
"""maxzoom 三态（int / 'auto' / 未传）的校验与落库表示。"""

import pytest

from src.services.geo_validation import (AUTO_MAXZOOM, AUTO_MAXZOOM_SENTINEL,
                                         coerce_maxzoom, maxzoom_from_db,
                                         maxzoom_to_db)


def test_auto_literal_passes_through():
    assert coerce_maxzoom('auto') == AUTO_MAXZOOM


def test_missing_means_unset():
    assert coerce_maxzoom(None) is None
    assert coerce_maxzoom('') is None


def test_numbers_still_go_through_validate_zoom():
    assert coerce_maxzoom('12') == 12
    assert coerce_maxzoom(12) == 12
    assert coerce_maxzoom(0) == 0
    assert coerce_maxzoom(21) == 21


@pytest.mark.parametrize('bad', [
    'AUTO',        # 不做大小写归一
    ' auto ',      # 不裁前后空白
    'Auto',
    'automatic',
    '12.5',
    22,
    -1,            # 哨兵不许从外部传进来
    True,          # 布尔不是层级
    [],            # JSON 送得进不可哈希的值，必须是 400 不是 500
])
def test_rejects(bad):
    with pytest.raises(ValueError):
        coerce_maxzoom(bad)


def test_db_roundtrip_for_auto():
    assert maxzoom_to_db(AUTO_MAXZOOM) == AUTO_MAXZOOM_SENTINEL
    assert maxzoom_from_db(AUTO_MAXZOOM_SENTINEL) is None


def test_db_roundtrip_for_a_number():
    assert maxzoom_to_db(12) == 12
    assert maxzoom_from_db(12) == 12
    # z0 是合法层级，不能被当成假值
    assert maxzoom_to_db(0) == 0
    assert maxzoom_from_db(0) == 0
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py -v`
Expected: FAIL —— `ImportError: cannot import name 'AUTO_MAXZOOM' from 'src.services.geo_validation'`

- [ ] **Step 3: 实现**

在 `src/services/geo_validation.py` 的 `coerce_vertex_normals` 函数结束（`:134` 的 `raise ValueError(...)` 之后）与 `def resolve_output_dir` 之前插入：

```python
# 「自动」层级的对外字面量与落库哨兵。
#
# 为什么落库要用哨兵而不是 NULL：dem_terrain_jobs.maxzoom 与
# local_terrain_tasks.maxzoom 都是 `INTEGER NOT NULL` 无默认，SQLite 去掉
# NOT NULL 要走 12 步重建表，而本仓的迁移约定是「幂等 ALTER ADD COLUMN」
# （见 CLAUDE.md「Database conventions」）。validate_zoom 的值域是 0..21，
# 用户输入永远到不了 -1，哨兵不存在撞车。
#
# ⚠️ 别和 effective_maxzoom 的 DEFAULT NULL 记混：那里的 NULL 是「还不知道
# 切到了第几级」，这里的 -1 是「基准不是一个数字」。两个列语义正交 ——
# 自动挡下 maxzoom = -1，effective_maxzoom 照常记录实际切到的层级。
AUTO_MAXZOOM = 'auto'
AUTO_MAXZOOM_SENTINEL = -1


def coerce_maxzoom(value, name='maxzoom'):
    """把请求里的最大切片层级收成三态：int / 'auto' / None。

    - `'auto'` = 按源数据像素尺寸现算基准层级（`build_terrain` 收到
      `max_level=None` 时走 `GeographicTilingScheme.estimate_max_level`）；
    - `int` = 用户指定的基准层级，值域仍由 `validate_zoom` 把关；
    - `None`（`None` 与空串）= 未表态，调用方回落到配置 `terrain_local_maxzoom`。

    **这是 maxzoom 唯一的把关点。** 两个管理器过了这里就直接落库/构造
    TileParams，没有第二道网。

    刻意不做大小写归一、不裁前后空白（`validate_tiling_quality` 定的同一条
    规矩）：拼错的档位静默走 else 分支、作业照样 completed，是本仓栽过的坑。
    `'AUTO'` 当场 ValueError → 400，错误直指病因。

    `-1` 从外部传进来同样是 ValueError —— 它是内部落库表示，不是输入格式。
    这条由 validate_zoom 的下界天然保证，不需要额外分支。
    """
    if value is None or value == "":
        return None
    # 与 coerce_vertex_normals 同款：用 == 比较而不是 `in {...}`，JSON 送得进
    # 不可哈希的值（`{"maxzoom": []}`），集合成员判定会抛 TypeError → 500。
    if value == AUTO_MAXZOOM:
        return AUTO_MAXZOOM
    return validate_zoom(value, name)


def maxzoom_to_db(maxzoom):
    """归一后的 maxzoom → 落库整数。`'auto'` 存成哨兵，数字原样。

    只接受 `coerce_maxzoom` 的非 None 返回值；调用方不许自己写 -1。
    """
    return AUTO_MAXZOOM_SENTINEL if maxzoom == AUTO_MAXZOOM else int(maxzoom)


def maxzoom_from_db(value):
    """落库整数 → `TileParams.maxzoom` 的形态。哨兵还原成 None，其余是 int。

    `None` 正是 `build_terrain(max_level=None)` 触发按源分辨率估算的那一态，
    所以这个函数的返回值可以直接进 TileParams，不需要调用方再判一次。
    """
    v = int(value)
    return None if v == AUTO_MAXZOOM_SENTINEL else v
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py -v`
Expected: PASS（15 passed）

- [ ] **Step 5: 提交**

```bash
git add src/services/geo_validation.py tests/test_terrain_auto_maxzoom.py
git commit -m "feat(terrain): maxzoom 增加 auto 三态校验与落库哨兵"
```

---

### Task 2: `TileParams.maxzoom` 可为 None（切片器透传）

**Files:**
- Modify: `src/services/terrain_tiling/dem_task_tiler.py:30`（字段类型）、`:160`（`max_level=` 实参）
- Test: `tests/test_dem_task_tiler.py`（追加）

**Interfaces:**
- Consumes: Task 1 无（本任务独立）
- Produces: `TileParams.maxzoom: Optional[int]`；`tile_dem_task_dir` 在 `maxzoom is None` 时向 `build_terrain` 传 `max_level=None`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_dem_task_tiler.py` 末尾追加（沿用该文件已有的 `build_terrain_fn` 替身与临时目录写法；如果文件里已有构造 DEM 目录的 helper，用它，别新造）：

```python
def test_auto_maxzoom_reaches_build_terrain_as_none(tmp_path):
    """maxzoom=None 必须原样透传成 max_level=None —— 那是 build_terrain 里
    estimate_max_level 分支的唯一触发条件。传 0 或 -1 都会切出错误的层级。"""
    task_dir = tmp_path / "dem_task_1"
    task_dir.mkdir()
    (task_dir / "a_dem.tif").write_bytes(b"")
    out_dir = tmp_path / "terrain_tiles"

    seen = {}

    def fake_build_terrain(**kwargs):
        seen.update(kwargs)
        (out_dir / "layer.json").parent.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text("{}", encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0, "max_level": 16}

    params = TileParams(maxzoom=None, parent_url="")
    tile_dem_task_dir(task_dir, out_dir, params, build_terrain_fn=fake_build_terrain)

    assert seen["max_level"] is None
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_dem_task_tiler.py::test_auto_maxzoom_reaches_build_terrain_as_none -v`
Expected: FAIL —— `TypeError: int() argument must be ... not 'NoneType'`（`int(params.maxzoom)`）

- [ ] **Step 3: 实现**

`src/services/terrain_tiling/dem_task_tiler.py:30`，把

```python
    maxzoom: int
```

改成

```python
    # None = 自动：按源数据像素尺寸现算基准层级。build_terrain 收到
    # max_level=None 时走 GeographicTilingScheme.estimate_max_level，档位偏移
    # 再叠在估算值上（cesium_terrain 里 max_level 唯一的解析点）。
    # 落库表示是哨兵 -1，翻译住在 geo_validation.maxzoom_from_db / _to_db。
    maxzoom: Optional[int]
```

`:160`，把

```python
        max_level=int(params.maxzoom),
```

改成

```python
        max_level=None if params.maxzoom is None else int(params.maxzoom),
```

（`Optional` 已在该文件 `typing` 导入里，见 `:67` 的 `progress_cb: Optional[...]`。）

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_dem_task_tiler.py -v`
Expected: PASS（全文件绿，含新增那条）

- [ ] **Step 5: 提交**

```bash
git add src/services/terrain_tiling/dem_task_tiler.py tests/test_dem_task_tiler.py
git commit -m "feat(terrain): TileParams.maxzoom 支持 None 并透传给 build_terrain"
```

---

### Task 3: 两条路由改走 `coerce_maxzoom`

**Files:**
- Modify: `src/routes/local_terrain_api.py:39-41`、import 行
- Modify: `src/routes/terrain_api.py:37-42`、`:11` import 行
- Test: `tests/test_local_terrain_api.py`（追加）、`tests/test_terrain_api.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `coerce_maxzoom`
- Produces: 两条路由把 `'auto'` 原样交给管理器；非法值 400

- [ ] **Step 1: 写失败的测试**

在 `tests/test_local_terrain_api.py` 末尾追加（沿用该文件已有的 client fixture 与 multipart 上传写法）：

```python
def test_http_upload_accepts_the_auto_maxzoom(client, monkeypatch):
    """表单勾了「自动」送上来的是字面量 'auto'，必须原样到达管理器。"""
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(local_terrain_task_manager, "create_task_with_files",
                        lambda **kw: fake_create(**kw))
    resp = client.post("/api/terrain/local/tasks", data={
        "name": "t",
        "maxzoom": "auto",
        "files": (io.BytesIO(b"x"), "a.tif"),
    }, content_type="multipart/form-data")

    assert resp.status_code == 201
    assert seen["maxzoom"] == "auto"


def test_http_upload_rejects_a_misspelled_auto(client):
    resp = client.post("/api/terrain/local/tasks", data={
        "name": "t",
        "maxzoom": "AUTO",
        "files": (io.BytesIO(b"x"), "a.tif"),
    }, content_type="multipart/form-data")

    assert resp.status_code == 400
```

在 `tests/test_terrain_api.py` 末尾追加：

```python
def test_dem_start_rejects_a_misspelled_auto(client):
    """terrain_api 此前压根不校验 maxzoom，脏值一路走到 manager 才炸。"""
    resp = client.post("/api/terrain/dem/1/start", json={"maxzoom": "AUTO"})
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_local_terrain_api.py -k auto -v tests/test_terrain_api.py -k misspelled -v`
Expected: FAIL —— `'auto'` 被 `validate_zoom` 当成非数字 → 400（第一条），`'AUTO'` 在 DEM 侧一路放行（第三条拿到 200 或 500）

- [ ] **Step 3: 实现**

`src/routes/local_terrain_api.py`：import 行把 `validate_zoom` 换成 `coerce_maxzoom`（如果 `validate_zoom` 在该文件别处还有用就两个都留），然后把 `:39-41`

```python
        maxzoom_raw = request.form.get("maxzoom")
        # 0–21 校验（validate_zoom 抛带字段名的 ValueError -> 400）
        maxzoom = validate_zoom(maxzoom_raw, "maxzoom") if maxzoom_raw not in (None, "") else None
```

改成

```python
        # 三态：'auto'（按源分辨率现算）/ 0–21 / 未传（空串 → None → 配置默认）。
        # coerce_maxzoom 抛带字段名的 ValueError -> 400。
        maxzoom = coerce_maxzoom(request.form.get("maxzoom"), "maxzoom")
```

`src/routes/terrain_api.py`：`:11` 改成

```python
from src.services.geo_validation import coerce_maxzoom, coerce_vertex_normals
```

`:38-42`

```python
        maxzoom = payload.get("maxzoom")
        if maxzoom is None:
            maxzoom = request.form.get("maxzoom")
        if maxzoom == "":
            maxzoom = None
```

改成

```python
        maxzoom = payload.get("maxzoom")
        if maxzoom is None:
            maxzoom = request.form.get("maxzoom")
        # 此前这里不校验、原样交给 manager；'auto' 落地后必须在入口就分清
        # 「自动」与「脏值」，否则拼错的 'AUTO' 会被 manager 的 validate_zoom
        # 报成层级越界，把人指向一个不存在的数字问题。
        maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
```

同时把 `:33-35` 那段注释里「maxzoom / quality 的合法性交给 manager」改成「quality 的合法性交给 manager；maxzoom 在这里过 coerce_maxzoom」。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_local_terrain_api.py tests/test_terrain_api.py -v`
Expected: PASS（两文件全绿）

- [ ] **Step 5: 提交**

```bash
git add src/routes/local_terrain_api.py src/routes/terrain_api.py tests/test_local_terrain_api.py tests/test_terrain_api.py
git commit -m "feat(terrain): 两条切片路由收 auto 层级并收口 DEM 侧的 maxzoom 校验"
```

---

### Task 4: 管理器落库哨兵、读回还原

**Files:**
- Modify: `src/services/local_terrain_task_manager.py`：import 行、`_default_maxzoom`（`:116-133`）、`_normalize_tiling_params`（`:146-176`）、两处 INSERT 的 maxzoom 绑定参数（`:238-251` / `:379-390`）、起切读回（`:551`）
- Modify: `src/services/dem_task_manager.py`：import 行、`start_tiling` 的 maxzoom 归一（`:308-330`）、INSERT 绑定参数（`:364-394`）、`TileParams` 构造（`:583-588`）
- Test: `tests/test_terrain_auto_maxzoom.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `AUTO_MAXZOOM` / `coerce_maxzoom` / `maxzoom_to_db` / `maxzoom_from_db`；Task 2 的 `TileParams.maxzoom: Optional[int]`
- Produces: 自动挡任务在库里 `maxzoom = -1`，起切时构造 `TileParams(maxzoom=None)`

- [ ] **Step 1: 先读这几段的真实代码**

```bash
uv run python - <<'PY'
import subprocess
for f, a, b in [("src/services/local_terrain_task_manager.py", 230, 260),
                ("src/services/local_terrain_task_manager.py", 370, 395),
                ("src/services/local_terrain_task_manager.py", 540, 560),
                ("src/services/dem_task_manager.py", 300, 340),
                ("src/services/dem_task_manager.py", 360, 400),
                ("src/services/dem_task_manager.py", 575, 595)]:
    print(f"===== {f}:{a}-{b}")
    print("".join(open(f, encoding="utf-8").readlines()[a-1:b]))
PY
```

改动只涉及**绑定参数的表达式**，SQL 文本本身一个字不动。

- [ ] **Step 2: 写失败的测试**

在 `tests/test_terrain_auto_maxzoom.py` 追加：

```python
def test_local_manager_stores_the_sentinel_and_tiles_with_none(tmp_path, monkeypatch):
    """自动挡：库里存 -1，起切时 TileParams.maxzoom 必须是 None。

    存 -1 而 TileParams 传 -1 是最容易犯的错 —— build_terrain 会把 -1 当成
    显式层级，钳到 0，切出一张 z0 瓦片然后报 completed。
    """
    from src.services.geo_validation import AUTO_MAXZOOM_SENTINEL

    mgr = _make_local_manager(tmp_path)          # 见该文件已有的构造 helper
    task_id = mgr.create_task_with_files(
        name="t", files=[("a.tif", io.BytesIO(b"x"))], maxzoom="auto",
        quality=None, vertex_normals=None)

    row = _fetch_local_row(task_id)
    assert row["maxzoom"] == AUTO_MAXZOOM_SENTINEL

    seen = {}
    monkeypatch.setattr(
        "src.services.local_terrain_task_manager.tile_dem_task_dir",
        lambda task_dir, out_dir, params, **kw: seen.update(maxzoom=params.maxzoom) or {})
    mgr.start_tiling(task_id)
    _join_worker(mgr, task_id)

    assert seen["maxzoom"] is None
```

> 实施者注意：`_make_local_manager` / `_fetch_local_row` / `_join_worker` 三个 helper **以 `tests/test_local_terrain_api.py` 里已有的同类写法为准**——先读那个文件，复用它的 fixture，不要新造一套。若那里用的是 `isolated_app` fixture，本测试也用它。

- [ ] **Step 3: 运行测试确认它失败**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py -k sentinel -v`
Expected: FAIL —— `create_task_with_files` 里的 `validate_zoom("auto")` 抛 ValueError

- [ ] **Step 4: 实现 —— local 侧**

import 行加 `AUTO_MAXZOOM, coerce_maxzoom, maxzoom_from_db, maxzoom_to_db`。

`_default_maxzoom`（`:116-133`）整体替换为：

```python
    def _default_maxzoom(self):
        # 配置值过 coerce_maxzoom 而不是裸 int()：terrain_local_maxzoom 在
        # config_manager._UNCONSTRAINED_KEYS 里，写入侧没有取值规则，
        # PUT /api/config 收得下 99，也收得下任何拼错的字符串。
        # 校验失败不抛：配置是装机默认，一个坏值不该让所有任务都建不起来。
        # 但必须留痕，否则「我明明配了 25」在系统里一处都查不到。
        # （显式传参那条相反：调用方给了非法值必须当场报错，不能静默改写。）
        raw = self.config.get("terrain_local_maxzoom", AUTO_MAXZOOM)
        try:
            value = coerce_maxzoom(raw, "terrain_local_maxzoom")
        except Exception as e:
            logger.warning(
                f"配置 terrain_local_maxzoom={raw!r} 不可用({e})，"
                f"本次改用出厂默认 {AUTO_MAXZOOM!r}")
            return AUTO_MAXZOOM
        # 空值与缺键都算「没配过」→ 出厂默认（自动）。
        return AUTO_MAXZOOM if value is None else value
```

`_normalize_tiling_params`（`:157-159`）把

```python
        if maxzoom is None:
            maxzoom = self._default_maxzoom()
        maxzoom = validate_zoom(maxzoom, "maxzoom")
```

改成

```python
        if maxzoom is None:
            maxzoom = self._default_maxzoom()
        # 返回值是三态里的两态：int 或 'auto'（None 在上一行已被替换掉）
        maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
```

并把该方法的返回类型注解 `-> Tuple[int, str, bool]` 改成 `-> Tuple[Union[int, str], str, bool]`（`Union` 加进该文件的 `typing` 导入）。

两处 INSERT（`:238-251` 与 `:379-390`）：把绑定元组里传 `maxzoom` 的那个位置改成 `maxzoom_to_db(maxzoom)`。**SQL 文本不动。**

起切读回（`:551`）把

```python
            maxzoom = int(row["maxzoom"])
```

改成

```python
            # 哨兵 -1 还原成 None = 自动，直接就是 TileParams.maxzoom 要的形态
            maxzoom = maxzoom_from_db(row["maxzoom"])
```

- [ ] **Step 5: 实现 —— DEM 侧**

import 行同样加四个名字。`start_tiling` 的 `:308-330` 整段替换为：

```python
        if maxzoom is not None:
            maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
        if maxzoom is None:
            # 配置值同样过 coerce_maxzoom：该键在 _UNCONSTRAINED_KEYS 里，
            # 写入侧无校验。此前是裸 int()，配置写 25 就能一路切出 z25。
            # 软退回而不是抛：配置是装机默认，一个坏值不该让所有切片都起不来；
            # 但必须留痕。local 侧 _default_maxzoom 是同一条规矩。
            maxzoom_raw = self.config.get("terrain_local_maxzoom", AUTO_MAXZOOM)
            try:
                maxzoom = coerce_maxzoom(maxzoom_raw, "terrain_local_maxzoom")
            except Exception as e:
                maxzoom = AUTO_MAXZOOM
                logger.warning(
                    f"配置 terrain_local_maxzoom={maxzoom_raw!r} 不可用({e})，"
                    f"本次切片改用出厂默认 {AUTO_MAXZOOM!r}")
            if maxzoom is None:
                # 配置被清空 = 没配过 → 出厂默认
                maxzoom = AUTO_MAXZOOM
        maxzoom = maxzoom_to_db(maxzoom)
```

—— 这样 `maxzoom` 从这里往下**一律是库形态的 int**（含哨兵），INSERT（`:377` 的 `maxzoom=excluded.maxzoom` 那条绑定）与线程参数（`:429-430`）都不用改。

`TileParams` 构造（`:583-588`）把 `maxzoom=maxzoom` 改成：

```python
                                      maxzoom=maxzoom_from_db(maxzoom),
```

- [ ] **Step 6: 修两条会变红的既有测试**

这两条钉的是「配置里的层级越界时软退回**出厂默认**」，而出厂默认这一步之后是 `'auto'`（Task 6 才改 `DEFAULT_CONFIGS`，但 `_default_maxzoom` / `start_tiling` 的兜底字面量在本任务就已经改成 `AUTO_MAXZOOM`），所以落库值从 `14` 变成哨兵 `-1`：

- `tests/test_local_terrain_api.py:835` `test_out_of_range_maxzoom_config_falls_back`：配置 `99` → 断言从「落库 14」改成「落库 `AUTO_MAXZOOM_SENTINEL`」。**warning 仍必须在** —— 软退回可以变，静默退回不行。
- `tests/test_terrain_api.py:313` `test_terrain_start_falls_back_when_configured_maxzoom_is_out_of_range`：同样改成哨兵，`:332-333` 的 warning 断言保持。

另外确认 `tests/test_local_terrain_api.py:948` `test_http_upload_treats_empty_strings_as_omitted`：空串仍然是「未传 → 配置默认」，只是配置默认现在解析成自动。如果它断言的是具体数字，一并改成哨兵；如果只断言「走了配置默认那条分支」，无需改动。

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py tests/test_local_terrain_api.py tests/test_terrain_api.py -v`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add src/services/local_terrain_task_manager.py src/services/dem_task_manager.py tests/test_terrain_auto_maxzoom.py tests/test_local_terrain_api.py tests/test_terrain_api.py
git commit -m "feat(terrain): 两个管理器落库 auto 哨兵并以 None 起切"
```

---

### Task 5: 表单三态（服务端初值 + 模板控件 + 提交）

**Files:**
- Modify: `src/routes/main.py:47-106`（`_terrain_form_defaults`）、`:138-155`（两处 `render_template`）
- Modify: `templates/index.html:275-290`
- Modify: `static/js/map.js:2411`（提交）、tif 信息卡绑定处附近（新增 change 监听）
- Modify: `src/i18n/catalog/tpl_index.py`（新增一个 key）
- Test: `tests/test_terrain_lighting_frontend.py`（改既有 + 追加）、`tests/test_map_js_contract.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `AUTO_MAXZOOM` / `coerce_maxzoom`
- Produces:
  - `_terrain_form_defaults(cfg) -> (maxzoom: int, maxzoom_auto: bool, preset: str)`
  - 模板变量 `terrain_local_maxzoom`（数字）、`terrain_local_maxzoom_auto`（bool）
  - DOM：`#localTerrainMaxzoomAuto`（checkbox）
  - i18n key `tpl.index.process.local_terrain_maxzoom_auto`

- [ ] **Step 1: 写失败的测试**

`tests/test_terrain_lighting_frontend.py` 追加（沿用该文件已有的渲染 helper）：

```python
def test_auto_maxzoom_renders_checked_and_disables_the_number_input():
    html = _render_index({'terrain_local_maxzoom': 'auto'})
    soup = _soup(html)
    box = soup.find(id='localTerrainMaxzoomAuto')
    num = soup.find(id='localTerrainMaxzoom')
    assert box.has_attr('checked')
    assert num.has_attr('disabled')
    # 禁用的数字框仍要渲染一个合法值：它是用户取消勾选后的起点。空 value 并不会
    # 让 #processForm :invalid（min/max 只管有值的控件，空值要 required 才拦），
    # 坏在静默：取消勾选后提交送空串，后端 coerce_maxzoom 当「未表态」回落到
    # 配置默认，也就是刚被取消掉的自动挡
    assert num['value'] == '14'


def test_manual_maxzoom_leaves_the_checkbox_clear():
    html = _render_index({'terrain_local_maxzoom': '16'})
    soup = _soup(html)
    assert not soup.find(id='localTerrainMaxzoomAuto').has_attr('checked')
    num = soup.find(id='localTerrainMaxzoom')
    assert not num.has_attr('disabled')
    assert num['value'] == '16'
```

`tests/test_map_js_contract.py` 追加：

```python
def test_terrain_submit_sends_the_auto_literal_when_the_box_is_checked():
    """勾了自动就送字面量 'auto'，不能送数字框里那个陈旧的数。"""
    src = _read('static/js/map.js')
    assert "localTerrainMaxzoomAuto" in src
    assert "'auto'" in src
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_terrain_lighting_frontend.py -k auto_maxzoom -v`
Expected: FAIL —— `AttributeError: 'NoneType' object has no attribute 'has_attr'`（控件还不存在）

- [ ] **Step 3: 服务端初值**

`src/routes/main.py:82-91` 那段替换为：

```python
    maxzoom = _FACTORY_LOCAL_MAXZOOM
    maxzoom_auto = True
    raw_zoom = cfg.get('terrain_local_maxzoom') or ''
    if raw_zoom != '':
        try:
            # 与两个管理器同一把尺（coerce_maxzoom），不在这里另抄一份 0/21 的
            # 上下界，也不在这里另认一次 'auto' 字面量。
            value = coerce_maxzoom(raw_zoom, 'terrain_local_maxzoom')
        except Exception as e:
            logger.warning(
                f"配置 terrain_local_maxzoom={raw_zoom!r} 不可用({e})，"
                f"处理表单初值改用出厂默认（自动）")
        else:
            # 数字框在自动挡下仍渲染出厂 14 —— 它是用户取消勾选后的起点。
            maxzoom_auto = value == AUTO_MAXZOOM
            if not maxzoom_auto:
                maxzoom = value
```

`:106` 的 `return maxzoom, preset` 改成 `return maxzoom, maxzoom_auto, preset`，docstring 的 Returns 段跟着改成三元组。import 行加 `AUTO_MAXZOOM, coerce_maxzoom`。

`:138` 与 `:149` 两处调用改成三元解包，两处 `render_template` 各加一个参数：

```python
                           terrain_local_maxzoom_auto=maxzoom_auto,
```

- [ ] **Step 4: 模板**

`templates/index.html:290` 那一行（`<input type="number" ... id="localTerrainMaxzoom" ...>`）替换为：

```html
                        {# 「自动」= 按源数据分辨率现算基准层级（后端送字面量 'auto'）。
                           勾上时禁用数字框：disabled 的输入不参与原生校验，也不会
                           让用户以为旁边那个数还算数。初值同样由服务端渲染 —— 写死
                           就又造出一个「改了没反应」的假旋钮。 #}
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" id="localTerrainMaxzoomAuto"
                                   {% if terrain_local_maxzoom_auto %}checked{% endif %}>
                            <label class="form-check-label" for="localTerrainMaxzoomAuto">
                                {{ t('tpl.index.process.local_terrain_maxzoom_auto') }}
                            </label>
                        </div>
                        <input type="number" class="form-control" id="localTerrainMaxzoom" min="0" max="21"
                               value="{{ terrain_local_maxzoom }}"
                               {% if terrain_local_maxzoom_auto %}disabled{% endif %}>
```

- [ ] **Step 5: i18n**

`src/i18n/catalog/tpl_index.py` 在 `tpl.index.process.local_terrain_maxzoom`（`:243-246`）之后插入：

```python
    'tpl.index.process.local_terrain_maxzoom_auto': {
        'zh': '自动（按源数据分辨率决定）',
        'en': 'Auto (from source resolution)',
    },
```

- [ ] **Step 6: 前端提交与禁用联动**

`static/js/map.js:2411` 那一行替换为：

```js
    // 勾了「自动」就送字面量 'auto'，不是数字框里那个陈旧的数 —— 数字框在自动挡
    // 下是 disabled 的，它的 value 只是用户取消勾选后的起点。
    const maxzoomAutoEl = document.getElementById('localTerrainMaxzoomAuto');
    fd.append('maxzoom', maxzoomAutoEl?.checked
        ? 'auto'
        : (document.getElementById('localTerrainMaxzoom')?.value || ''));
```

再在 `map.js` 里挂 change 监听。**先读 `static/js/map.js:1060-1090`**，找到给两个文件输入绑 `updateTifInfo` 的那个块（`'terrain'` / `'contour'` 两条），在它后面追加：

```js
    // 「自动」勾选状态与层级输入的禁用态联动。初值由服务端渲染，这里只管运行时。
    const maxzoomAutoToggle = document.getElementById('localTerrainMaxzoomAuto');
    if (maxzoomAutoToggle) {
        maxzoomAutoToggle.addEventListener('change', () => {
            const numEl = document.getElementById('localTerrainMaxzoom');
            if (numEl) numEl.disabled = maxzoomAutoToggle.checked;
        });
    }
```

- [ ] **Step 7: 修既有测试**

`tests/test_terrain_lighting_frontend.py:525` 的 `test_preset_controls_render_the_configured_defaults`（配置 16 → `value="16"`）与 `:633` 的 `test_out_of_range_maxzoom_is_clamped_out_loud`（99 → `value="14"`）现在还要额外断言 checkbox 未勾选（配置是数字）/ 已勾选（配置非法退回自动）。按新行为改断言，**不要改被测行为**：99 的那条现在应该退回**自动**并留 warning，把断言从 `value == '14'` 改成 `checkbox.has_attr('checked')` 且 warning 仍在。**名字也要跟着改**——`clamped` 说的是「钳成 14」这个旧行为，落地后它退回的是自动挡，已改名为 `test_out_of_range_maxzoom_falls_back_out_loud`。

- [ ] **Step 8: 运行测试确认通过**

Run: `uv run pytest tests/test_terrain_lighting_frontend.py tests/test_map_js_contract.py tests/test_config_form_submittable.py tests/test_i18n.py -v`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add src/routes/main.py templates/index.html static/js/map.js src/i18n/catalog/tpl_index.py tests/test_terrain_lighting_frontend.py tests/test_map_js_contract.py
git commit -m "feat(terrain): 处理表单增加「自动层级」开关"
```

---

### Task 6: 出厂默认改 `auto` + `user_version 3 → 4` 迁移

**Files:**
- Modify: `src/core/database.py:91`（`DEFAULT_CONFIGS`）、`:361` 之后（新函数）、`:883` 之后（调用）
- Modify: `src/routes/main.py:50`（`_FACTORY_LOCAL_MAXZOOM` 上方注释）
- Test: `tests/test_terrain_auto_maxzoom.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `AUTO_MAXZOOM`；Task 5 的 `_terrain_form_defaults` 已能认 `'auto'`（顺序不能颠倒，否则首页会 warning 并渲染成手动 14）
- Produces: `migrate_local_maxzoom_to_auto(cursor) -> bool`

- [ ] **Step 1: 写失败的测试**

```python
def test_fresh_db_ships_auto_as_the_factory_default(tmp_path, monkeypatch):
    db = _init_fresh_db(tmp_path)          # 复用 tests/test_tiling_presets.py 的写法
    assert _config_value(db, 'terrain_local_maxzoom') == 'auto'


def test_legacy_db_with_the_factory_14_is_migrated_to_auto(tmp_path):
    db = _legacy_db_with(tmp_path, {'terrain_local_maxzoom': '14'})
    _run_init_database(db)
    assert _config_value(db, 'terrain_local_maxzoom') == 'auto'
    assert _user_version(db) >= 4


def test_a_deliberately_configured_level_is_left_alone(tmp_path):
    """12 是用户自己设的，不许被改写 —— 只有恰好等于出厂 14 的才动。"""
    db = _legacy_db_with(tmp_path, {'terrain_local_maxzoom': '12'})
    _run_init_database(db)
    assert _config_value(db, 'terrain_local_maxzoom') == '12'


def test_migration_is_idempotent(tmp_path):
    db = _legacy_db_with(tmp_path, {'terrain_local_maxzoom': '14'})
    _run_init_database(db)
    # 用户迁移后又手动改回 14，第二次启动不许再改写他
    _set_config(db, 'terrain_local_maxzoom', '14')
    _run_init_database(db)
    assert _config_value(db, 'terrain_local_maxzoom') == '14'
```

> 实施者注意：四个 helper 以 `tests/test_tiling_presets.py` 里 `test_legacy_db_gets_the_effective_level_column_from_the_migration`（`:406`）的建库写法为准，先读那一段再抄形态。

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py -k "factory or migrat or deliberately" -v`
Expected: FAIL —— 出厂值还是 `'14'`

- [ ] **Step 3: 实现**

`src/core/database.py:91` 把

```python
    ('terrain_local_maxzoom', '14'),
```

改成

```python
    # 'auto' = 按源数据像素尺寸现算基准层级（geo_validation.AUTO_MAXZOOM）。
    # 固定 14 只对 30 m 源正确：3″ 源会超建（77.4 MB vs 6.9 MB，多出来的只是对
    # 同一批数据更平滑的插值），5 m 源会欠建（est≈16 被截断在 14）。
    # ⚠️ 改这个默认值的同时**必须**跑 migrate_local_maxzoom_to_auto ——
    # INSERT OR IGNORE 只对新建的库生效，存量行还是 '14'。
    ('terrain_local_maxzoom', 'auto'),
```

在 `migrate_cancelled_tasks_to_failed` 之后（`:361` 之后）新增：

```python
def migrate_local_maxzoom_to_auto(cursor) -> bool:
    """terrain_local_maxzoom 的出厂 '14' 迁成 'auto'（user_version 3 → 4）。

    **只改 DEFAULT_CONFIGS 不够**：它走 INSERT OR IGNORE，只对新建的库生效。

    **为什么敢改存量值**：本工具默认下载的是 30 m 源（Copernicus GLO-30 /
    ASTER），estimate_max_level 对它算出来就是 14 —— 这条迁移对最常见的情形
    是**产物零变化**（同一份 DEM、同一个档位、切出同样的层级），只对非 30 m
    源生效，而那正是要修的缺陷。

    `WHERE value='14'` 把「显式设过 12 / 16」的用户挡在外面。无法区分「出厂
    没动」与「特意设成 14」是这条迁移唯一的模糊处，代价由上一段兜住。

    整段包在 try 里且 `PRAGMA user_version = 4` 无条件执行（migrate_base_path_
    to_assets 定的同一条规矩）：迁移出问题不能阻断启动，更不能每次启动重试。
    """
    if cursor.execute('PRAGMA user_version').fetchone()[0] >= 4:
        return False

    changed = False
    try:
        cursor.execute(
            "UPDATE config SET value = 'auto' "
            "WHERE key = 'terrain_local_maxzoom' AND value = '14'")
        changed = cursor.rowcount > 0
    except Exception as e:
        logger.warning(f'terrain_local_maxzoom 迁移跳过（{e!r}）')

    cursor.execute('PRAGMA user_version = 4')
    if changed:
        logger.info("terrain_local_maxzoom '14' → 'auto' (user_version=4)")
    return changed
```

`:883` 之后加一行：

```python
        migrate_local_maxzoom_to_auto(cursor)
```

`src/routes/main.py:47-50` 的注释改成说明 `_FACTORY_LOCAL_MAXZOOM = 14` 现在是**数字框的起点值**（自动挡下用户取消勾选后从这里开始），不再是配置的出厂默认。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_terrain_auto_maxzoom.py tests/test_tiling_presets.py -v`
Expected: PASS（`test_config_defaults_are_shipped` 里 `'14'` 的断言要同步改成 `'auto'`）

- [ ] **Step 5: 提交**

```bash
git add src/core/database.py src/routes/main.py tests/test_terrain_auto_maxzoom.py tests/test_tiling_presets.py
git commit -m "feat(terrain): 层级出厂默认改为 auto 并迁移存量的出厂 14"
```

---

### Task 7: `/api/raster/inspect` 增返逐层瓦片数

**Files:**
- Modify: `src/services/raster_probe.py`（`_estimate_maxzoom` 之后新增函数；`describe_headers` 的 summary 组装处 `:500-506`）
- Test: `tests/test_raster_inspect.py`（追加）

**Interfaces:**
- Consumes: `cesium_terrain.GeographicTilingScheme` / `intersecting_tile_range`（惰性 import，缺 GDAL 时降级）
- Produces: `summary["tile_counts"]: list[int]`，长度 `MAX_ZOOM + 1 = 22`，`tile_counts[z]` = **该层**与并集 bounds 相交的张数（逐层，不累加）

- [ ] **Step 1: 写失败的测试**

`tests/test_raster_inspect.py` 追加：

```python
def test_summary_reports_per_level_tile_counts():
    """逐层、不累加 —— 累加区间留给消费方，因为随包底图可用与否会改变起点。"""
    out = describe_headers([_geographic_entry()], mode='terrain')
    counts = out['summary']['tile_counts']

    assert len(counts) == MAX_ZOOM + 1
    # z0 的瓦片方案是 2x1 全球，任何 bbox 至少落在一张里
    assert counts[0] >= 1
    # 每加一级，x/y 各翻倍 → 张数趋近 4 倍
    assert counts[14] > counts[13] > counts[12]


def test_tile_counts_match_the_tiler_geometry():
    """与切片器用的是同一套 intersecting_tile_range，不许各算各的。"""
    from src.services.terrain_tiling.cesium_terrain import (
        GeographicTilingScheme, intersecting_tile_range)

    out = describe_headers([_geographic_entry()], mode='terrain')
    w, s, e, n = out['summary']['bounds_wgs84']
    scheme = GeographicTilingScheme(tile_size=65)
    for z in (8, 12, 14):
        nx, ny = scheme.tile_count(z)
        ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, w, s, e, n)
        assert out['summary']['tile_counts'][z] == (ix1 - ix0 + 1) * (iy1 - iy0 + 1)


def test_contour_mode_does_not_report_tile_counts():
    """等高线走 Web Mercator，这张表对它没有意义，给了只会被误用。"""
    out = describe_headers([_geographic_entry()], mode='contour')
    assert 'tile_counts' not in out['summary']
```

> `_geographic_entry()` 用该文件 `test_geographic_dem_reports_crs_bounds_resolution_and_zoom`（`:129`）里已有的那个 entry 构造，先读再抄。

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_raster_inspect.py -k tile_counts -v`
Expected: FAIL —— `KeyError: 'tile_counts'`

- [ ] **Step 3: 实现**

在 `_estimate_maxzoom`（`:301-328`）之后插入：

```python
def _tile_counts_per_level(bounds_wgs84):
    """每个层级与 bounds 相交的瓦片数（逐层，**不累加**），长度 MAX_ZOOM+1。

    用途是起切前的规模预告。**刻意不做 min_level 假设**：随包底图可用时
    dem_task_tiler 恒传 min_level=8、只切 z8+，底图缺失时从 z0 起 —— 累加
    区间是消费方的事，这里只给原料。

    几何与切片器共用 intersecting_tile_range，不另算一套：上界用 ceil-1 而不是
    floor，DEM 四至恰好落在瓦片边界上时 floor 会多算一整行（该函数的 docstring
    有整段论证）。预告的数与实际切出来的数对不上，比没有预告更糟。

    cesium_terrain 模块级 import osgeo，缺 GDAL 时导入即失败 —— 那就不给
    这张表（此时本来也切不了片），与 _estimate_maxzoom 同款降级。
    """
    try:
        from src.services.terrain_tiling.cesium_terrain import (
            GeographicTilingScheme, intersecting_tile_range)
    except Exception:
        return None

    west, south, east, north = bounds_wgs84
    scheme = GeographicTilingScheme(tile_size=_TERRAIN_TILE_SIZE)
    counts = []
    for z in range(MAX_ZOOM + 1):
        nx, ny = scheme.tile_count(z)
        ix0, ix1, iy0, iy1 = intersecting_tile_range(nx, ny, west, south, east, north)
        counts.append((ix1 - ix0 + 1) * (iy1 - iy0 + 1))
    return counts
```

在 `describe_headers` 里，`summary["bounds_wgs84"] = [...]`（`:496-499`）那个 `if bounds:` 块的末尾追加：

```python
        # 只给高程管线：等高线走 Web Mercator，这张表对它没有意义。
        if mode == "terrain":
            counts = _tile_counts_per_level(summary["bounds_wgs84"])
            if counts is not None:
                summary["tile_counts"] = counts
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_raster_inspect.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/services/raster_probe.py tests/test_raster_inspect.py
git commit -m "feat(terrain): raster inspect 增返逐层瓦片数用于规模预告"
```

---

### Task 8: 起切前的规模预告行

**Files:**
- Modify: `templates/index.html`（档位下拉三个 `<option>` 加 `data-offset`；档位提示之后加 `#localTerrainEstimate` 容器）
- Modify: `src/routes/main.py`（两处 `render_template` 传 `terrain_quality_offsets`）
- Modify: `static/js/map.js`（缓存 inspect 汇总、渲染预告、三处监听）
- Modify: `src/i18n/catalog/js_map.py`（两个新 key）
- Test: `tests/test_tif_info_frontend.py`（追加）

**Interfaces:**
- Consumes: Task 7 的 `summary.tile_counts` 与既有 `summary.recommended_maxzoom`；Task 5 的 `#localTerrainMaxzoomAuto`
- Produces: DOM `#localTerrainEstimate`；`renderTerrainTileEstimate()`；i18n `js.map.terrain.estimate` / `js.map.terrain.estimate_auto_base`

- [ ] **Step 1: 写失败的测试**

`tests/test_tif_info_frontend.py` 追加：

```python
def test_preset_options_carry_the_offset_from_the_single_source_of_truth():
    """偏移表不许在 JS 里抄第二份 —— 由服务端渲染进 option 的 data-offset。"""
    from src.services.geo_validation import TILING_QUALITY_OFFSETS

    soup = _soup(_render_index({}))
    for preset, offset in TILING_QUALITY_OFFSETS.items():
        opt = soup.find('option', {'value': preset})
        assert opt['data-offset'] == str(offset)


def test_index_renders_the_estimate_container():
    soup = _soup(_render_index({}))
    assert soup.find(id='localTerrainEstimate') is not None


def test_map_js_does_not_hardcode_the_offset_table():
    src = _read('static/js/map.js')
    assert 'precision: 1' not in src
    assert "dataset.offset" in src
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_tif_info_frontend.py -k "offset or estimate" -v`
Expected: FAIL —— `option` 上没有 `data-offset`

- [ ] **Step 3: 模板与路由**

`templates/index.html` 档位下拉的三个 option（`:304-311`）各加一个属性，例如 precision 那条：

```html
                            <option value="precision" data-offset="{{ terrain_quality_offsets['precision'] }}"
                                    {% if terrain_quality_preset == 'precision' %}selected{% endif %}>
                                {{ t('tpl.index.process.terrain_quality_precision') }}
                            </option>
```

（balanced / speed 两条同形，把 `'precision'` 换成各自的键。）

档位提示（`terrain_quality_hint`）之后加：

```html
                        {# 起切前的规模预告：层级由「自动/手动」与档位共同决定。
                           数字是估算 —— 真实基准层级由 build_terrain 用**物化后**的
                           合并栅格现算，产物事实以任务详情的 effective_maxzoom 为准。 #}
                        <div class="tif-info" id="localTerrainEstimate" hidden></div>
```

`src/routes/main.py` 两处 `render_template` 各加：

```python
                           terrain_quality_offsets=TILING_QUALITY_OFFSETS,
```

（`TILING_QUALITY_OFFSETS` 已在该文件 import。）

- [ ] **Step 4: i18n**

`src/i18n/catalog/js_map.py` 在 `js.map.tifinfo.recommended_maxzoom`（`:369-372`）之后插入：

```python
    'js.map.terrain.estimate': {
        'zh': '预计切片：基准 z{base} → 实际 z{level} · 约 {tiles} 张 · 约 {size}',
        'en': 'Estimated: base z{base} → actual z{level} · ~{tiles} tiles · ~{size}',
    },
    'js.map.terrain.estimate_hint': {
        'zh': '估算值。实际基准层级在切片时按合并后的源栅格现算，产物层级见任务详情。',
        'en': 'An estimate. The real base level is computed from the merged source '
              'raster at tiling time; see the task detail for what was produced.',
    },
```

- [ ] **Step 5: 前端**

`static/js/map.js`：在 `_tifInfoSummaryBlock`（`:957`）之前插入模块级常量与渲染函数：

```js
// 起切前的规模预告。三个数都是估算：
//   体积 —— 单张均值 8.4 KB，取自 docs/reference/terrain/tiling-presets-measured.md
//           9.3 节最深层级的实测值（10.4 / 8.8 / 8.4 KB 里最保守的那个）；
//   法线 —— 开启后 +35%~+100% 字节（第五节），取下沿 1.4；
//   起点 —— 8，随包底图可用时 dem_task_tiler 恒传的 min_level。
// ⚠️ 档位偏移**不在这里抄第二份**：它由服务端渲染进 <option data-offset>，
//    取值表只有 geo_validation.TILING_QUALITY_OFFSETS 一份。
const TERRAIN_TILE_BYTES = 8.4 * 1024;
const TERRAIN_NORMALS_FACTOR = 1.4;
const TERRAIN_MIN_LEVEL = 8;

// 最近一次 /api/raster/inspect 的汇总。档位/自动开关变化时要重算预告，而那
// 两个事件不该再打一次服务端 —— 层级与 bounds 都已经在手上。
let _terrainInspectSummary = null;

function renderTerrainTileEstimate() {
    const box = document.getElementById('localTerrainEstimate');
    if (!box) return;

    const summary = _terrainInspectSummary;
    const autoEl = document.getElementById('localTerrainMaxzoomAuto');
    const numEl = document.getElementById('localTerrainMaxzoom');
    const qualityEl = document.getElementById('localTerrainQuality');

    let base;
    if (autoEl?.checked) {
        base = summary?.recommended_maxzoom;
    } else {
        base = numEl && numEl.value !== '' ? Number(numEl.value) : undefined;
    }
    const counts = summary?.tile_counts;
    if (base === undefined || base === null || !Array.isArray(counts)) {
        box.hidden = true;
        box.textContent = '';
        return;
    }

    const opt = qualityEl?.selectedOptions?.[0];
    const offset = Number(opt?.dataset?.offset ?? 0);
    // 与 build_terrain 同一道钳位：max(0, min(21, base + offset))
    const level = Math.max(0, Math.min(counts.length - 1, base + offset));

    // 起点也要跟着钳下来，与 build_terrain 的 min_level = min(min_level, max_level)
    // 同步：基准 8 配快速档实际切到 z7，起点还死守 8 的话预告会算成「约 0 张」。
    let tiles = 0;
    for (let z = Math.min(TERRAIN_MIN_LEVEL, level); z <= level; z++) tiles += counts[z] || 0;

    const normalsEl = document.getElementById('localTerrainNormals');
    const bytes = tiles * TERRAIN_TILE_BYTES * (normalsEl?.checked ? TERRAIN_NORMALS_FACTOR : 1);

    box.hidden = false;
    box.textContent = t('js.map.terrain.estimate', {
        base: String(base),
        level: String(level),
        tiles: tiles.toLocaleString(),
        size: _fmtBytes(bytes),
    });
    box.title = t('js.map.terrain.estimate_hint');
}
```

在 `updateTifInfo` 拿到响应、渲染完信息卡之后（`map.js:1041-1046` 那次 fetch 的成功分支里），当 `mode === 'terrain'` 时缓存汇总并渲染：

```js
        if (mode === 'terrain') {
            _terrainInspectSummary = data.summary || null;
            renderTerrainTileEstimate();
        }
```

在 Task 5 加的那个 change 监听块里，把 `renderTerrainTileEstimate()` 补进回调，并给另外三个控件各挂一个：

```js
    ['localTerrainMaxzoom', 'localTerrainQuality', 'localTerrainNormals'].forEach((id) => {
        document.getElementById(id)?.addEventListener('change', renderTerrainTileEstimate);
    });
```

（Task 5 里 `maxzoomAutoToggle` 的回调末尾补一行 `renderTerrainTileEstimate();`。）

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/test_tif_info_frontend.py tests/test_i18n.py tests/test_map_js_contract.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add templates/index.html src/routes/main.py static/js/map.js src/i18n/catalog/js_map.py tests/test_tif_info_frontend.py
git commit -m "feat(terrain): 起切前显示预计层级、张数与体积"
```

---

### Task 9: 详情面板回显「自动」+ 档位提示改口径

**Files:**
- Modify: `static/js/history.js:399-403`
- Modify: `src/i18n/catalog/js_history.py`（新增一个 key）
- Modify: `src/i18n/catalog/tpl_index.py:273-279`（改 `terrain_quality_hint` 的 zh/en）
- Test: `tests/test_tasks_js_contract.py` 或 `tests/test_i18n.py`（追加）

**Interfaces:**
- Consumes: Task 4 落库的哨兵 `-1`；Task 1 的 `AUTO_MAXZOOM_SENTINEL`（Python 侧断言用）
- Produces: i18n `js.history.terrain.maxzoom_auto`

- [ ] **Step 1: 写失败的测试**

```python
def test_history_js_translates_the_auto_sentinel():
    """哨兵 -1 不许泄漏到界面上。"""
    src = _read('static/js/history.js')
    assert 'js.history.terrain.maxzoom_auto' in src
    assert '-1' in src          # 与哨兵比较的那一处


def test_quality_hint_no_longer_claims_the_base_is_the_typed_number():
    """自动挡下「基准层级就是上面填的最大切片层级」是错的。"""
    from src.i18n.catalog.tpl_index import MESSAGES

    zh = MESSAGES['tpl.index.process.terrain_quality_hint']['zh']
    assert '自动' in zh
    assert '基准层级就是上面填的最大切片层级' not in zh
```

- [ ] **Step 2: 运行测试确认它失败**

Run: `uv run pytest tests/test_i18n.py -k "auto_sentinel or quality_hint" -v`
Expected: FAIL

- [ ] **Step 3: 实现 —— history.js**

`static/js/history.js:399-403` 替换为：

```js
            const localTerrainActualMaxzoom = task.effective_maxzoom;
            // 基准那一格的三态：切完了显示产物事实；没切完且基准是自动挡，
            // 显示「自动」而不是哨兵 -1（geo_validation.AUTO_MAXZOOM_SENTINEL，
            // 库里存的就是这个数）；否则回落到用户填的基准值并标明它是基准。
            const localTerrainBase = task.maxzoom === -1
                ? t('js.history.terrain.maxzoom_auto')
                : `${task.maxzoom} (${t('js.history.terrain.maxzoom_base_label')})`;
            document.getElementById('detailZoom').textContent =
                localTerrainActualMaxzoom != null
                    ? `0 - ${localTerrainActualMaxzoom}`
                    : `0 - ${localTerrainBase}`;
```

- [ ] **Step 4: 实现 —— i18n**

`src/i18n/catalog/js_history.py` 在 `js.history.terrain.maxzoom_base_label` 旁边插入：

```python
    'js.history.terrain.maxzoom_auto': {
        'zh': '自动（按源分辨率）',
        'en': 'auto (from source resolution)',
    },
```

`src/i18n/catalog/tpl_index.py:273-279` 的 `terrain_quality_hint` 替换为：

```python
    'tpl.index.process.terrain_quality_hint': {
        'zh': '基准层级：勾了「自动」就按源数据分辨率现算，否则就是上面填的最大'
              '切片层级。每差一级约 3.3 倍体积换 2.8 倍精度；层级已在 0 或 21 '
              '上限时不再偏移。',
        'en': 'The base level is derived from the source resolution when "Auto" is '
              'checked, otherwise it is the max tiling zoom above. Each step is one '
              'zoom level: ~3.3x size for ~2.8x accuracy. At the 0 / 21 limits the '
              'offset is clamped and the preset changes nothing.',
    },
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_i18n.py tests/test_tasks_js_contract.py -v`
Expected: PASS

- [ ] **Step 6: 全量回归**

Run: `uv run pytest tests/ -q`
Expected: 全绿。若有红项，逐条判断是「行为确实变了、断言该改」还是「改坏了」——**不许为了变绿而改被测行为**。

- [ ] **Step 7: 提交**

```bash
git add static/js/history.js src/i18n/catalog/js_history.py src/i18n/catalog/tpl_index.py tests/
git commit -m "feat(terrain): 详情面板回显自动层级并改正档位提示的口径"
```

---

## 收尾（不是 Task，但别忘）

- [ ] `CLAUDE.md` 的「DEM / terrain specifics」里，`effective_maxzoom` 那一条与「单一事实源 `TILING_QUALITY_OFFSETS`」那一条要补上自动挡：基准层级现在有两个来源。
- [ ] `docs/reference/terrain/tiling-presets-measured.md` 第六节末尾那句「**这条缺陷没有随三档一起修掉**」要改成已修，并指向本次的 spec。
- [ ] `CHANGELOG.md` / `RELEASE_NOTES.md` 按仓库既有口径写一条：默认行为变了（层级不再固定 14），存量库的出厂 14 会被迁成自动。
