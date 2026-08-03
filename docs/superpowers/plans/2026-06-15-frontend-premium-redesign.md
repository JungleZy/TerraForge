# 前端高级化重设计 Implementation Plan

> **归档文档 · 非当前实现**
> **记录时间**：2026-06-15 ｜ **状态**：已实施，后被 2026-07 两轮 UI 改造整体取代
> 本计划确实执行过（青绿 `#2dd4bf` 深色版就是它的产物），但配色与主题机制已被整体替换：强调色现为 sky `#38bdf8`（令牌新旧映射见 `docs/superpowers/specs/2026-07-28-gis-workbench-ui-design.md:74-78`），并新增 dark/light/system 三态主题——当前唯一事实源是 `static/css/style.css` 的 `:root` 与 `[data-bs-theme="light"]` 块。**照本文改 CSS 会把界面改坏。**
> 仍有参考价值：Task 2 的「廉价特效清单」与全文的 `#detail*` DOM 契约只在这里有完整记录。
> ⚠️ 复选框状态无效（57 个全未勾 ≠ 未执行）；正文源码与行号为当日快照，禁止照抄或照行号定位。
> *正文保持原样未回改。*

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前工业深色风重做成克制的高级深色（青绿强调色），三页视觉 + 局部布局重构，功能零回归。

**Architecture:** 以 `static/css/style.css` 的 `:root` 令牌为单一事实源；旧颜色变量名保留为别名指向新值，让 JS 内联引用零改动。组件样式按钮/徽章/卡片/进度/表格统一重写。历史页统计卡片由新增后端聚合接口 `GET /api/history_stats` 供数。所有 JS 依赖的 DOM id 与状态类名（`status-*`、`badge bg-*`、`progress-bar bg-*`）保持不变。

**Tech Stack:** Flask + Jinja2 模板、原生 JS、Bootstrap 5.3、Leaflet、自定义 CSS。测试 pytest（`uv run pytest`）。开发服务器 `uv run python app.py`（:5000）。

**说明（关于 TDD）:** 后端接口走严格 TDD（先写失败测试）。CSS/模板/JS 的视觉改动无法用单元测试断言"好看"，其验证 = ①`uv run pytest tests/` 全绿（防回归）+ ②按每个任务末尾的"视觉验收清单"逐项肉眼确认。这是诚实做法，不为 CSS 编造假测试。

**分支:** 已在 `feat/frontend-premium-redesign`，设计文档已提交。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `static/css/style.css` | 令牌 + 全局 + 所有组件样式 | 大幅重写 |
| `templates/base.html` | 字体引入、导航栏、去网格背景 | 修改 |
| `templates/index.html` | 下载表单分组 | 修改 |
| `static/js/tasks.js` | 活动任务卡片模板、空状态 | 修改 |
| `templates/config.html` | 设置卡片、底部按钮 | 修改 |
| `templates/history.html` | 统计卡片标记、详情弹窗重排 | 修改 |
| `static/js/history.js` | 统计卡片取数填充、表格行模板 | 修改 |
| `routes/api.py` | 新增 `/api/history_stats` | 修改 |
| `tests/test_history_stats.py` | 后端接口测试 | 新建 |

---

## Task 1: 设计令牌 + 字体

**Files:**
- Modify: `static/css/style.css:1-36`（`@import` + `:root`）
- Modify: `templates/base.html:8-23`（字体引入）

- [ ] **Step 1: 重写字体 @import 与 :root 变量**

替换 `static/css/style.css` 第 1 行的 `@import`：
```css
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
```

替换整个 `:root { ... }`（第 4-36 行）为：
```css
:root {
    /* 背景三层 */
    --color-bg-primary:   #0c0d10;
    --color-bg-secondary: #15171c;
    --color-bg-tertiary:  #1c2027;

    /* 边框 */
    --color-border:        rgba(255,255,255,0.07);
    --color-border-strong: rgba(255,255,255,0.12);

    /* 文字 */
    --color-text-primary:   #e8eaed;
    --color-text-secondary: #9aa0aa;
    --color-text-muted:     #5f6670;

    /* 强调色：青绿 */
    --color-accent:        #2dd4bf;
    --color-accent-hover:  #5eead4;
    --color-accent-strong: #14b8a6;
    --color-accent-muted:  rgba(45,212,191,0.12);
    --color-on-accent:     #04201c;

    /* 兼容别名：旧名指向新值，避免改 JS 内联引用 */
    --color-accent-amber:  var(--color-accent);
    --color-accent-warm:   var(--color-accent-hover);
    --color-accent-copper: var(--color-accent-strong);

    /* 状态色（青绿=品牌，不当状态色） */
    --color-success: #34d399;
    --color-danger:  #f87171;
    --color-warning: #fbbf24;
    --color-info:    #60a5fa;
    --color-success-bg: rgba(16,185,129,0.12);
    --color-danger-bg:  rgba(239,68,68,0.12);
    --color-warning-bg: rgba(245,158,11,0.12);
    --color-info-bg:    rgba(96,165,250,0.12);

    /* 字体 */
    --font-display: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', 'Hiragino Sans GB', sans-serif;
    --font-mono: 'JetBrains Mono', 'Space Mono', ui-monospace, SFMono-Regular, Menlo, monospace;

    /* 阴影（去发光，柔和深度） */
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.30);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.35);
    --shadow-lg: 0 12px 32px rgba(0,0,0,0.45);
    --shadow-glow: none;

    /* 圆角 */
    --radius-sm: 8px;
    --radius:    10px;
    --radius-lg: 14px;

    /* 字号系统（保留） */
    --font-size-xs: 0.75rem;
    --font-size-sm: 0.875rem;
    --font-size-base: 0.9375rem;
    --font-size-md: 1rem;
    --font-size-lg: 1.125rem;
    --font-size-xl: 1.25rem;
}
```
注意：`--shadow-glow: none;` 保留变量名（被多处引用）但置空，等于全局去发光。

- [ ] **Step 2: base.html 同步字体引入**

在 `templates/base.html` 的 `<head>` 内、Bootstrap CSS 之前加入（preconnect + Inter/JetBrains Mono）：
```html
    <!-- Fonts: Inter (UI) + JetBrains Mono (数字) -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
```
（style.css 的 `@import` 与此重复无害；保留 link 以便更早加载。）

- [ ] **Step 3: 把 body 字重从 300 提到 400**

`static/css/style.css` 中 `body { ... font-weight: 300; ... }`（约第 54 行）改为 `font-weight: 400;`。

- [ ] **Step 4: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS（CSS/模板改动不应影响后端测试）

- [ ] **Step 5: 视觉验收**

起服务 `uv run python app.py`，访问 `http://localhost:5000`：
- 背景为中性深色 `#0c0d10`（非蓝黑）。
- 中文不再发虚（字重 400 + 系统黑体回退）。
- 暂时配色可能不协调（组件还没改），只确认令牌生效、页面不崩。

- [ ] **Step 6: Commit**
```bash
git add static/css/style.css templates/base.html
git commit -m "feat(frontend): new design tokens (teal accent) + Inter font"
```

---

## Task 2: 全局减法（删廉价特效）

**Files:**
- Modify: `static/css/style.css`（多处选择器）

- [ ] **Step 1: 删网格背景层**

删除 `.main-content::before { ... }` 整块（约第 151-164 行，含琥珀网格 `background-image` 与 `background-size: 50px 50px`）。

- [ ] **Step 2: 删按钮涟漪**

删除 `.btn::before { ... }`（约第 633-644 行）与 `.btn:hover::before { ... }`（约第 646-649 行）两整块。

- [ ] **Step 3: 删进度条 shimmer**

删除 `.progress-bar::before { ... }`（约第 420-429 行）与 `@keyframes shimmer { ... }`（约第 431-438 行）。

- [ ] **Step 4: 删卡片顶部三色渐变条**

删除 `.index-left .card::before { ... }`（约第 194-202 行，copper→amber→warm 渐变）。

- [ ] **Step 5: 去 hover 平移**

- `.task-card:hover` 中删 `transform: translateX(4px);`（约第 282 行）。
- `.history-table tbody tr:hover` 中删 `transform: translateX(4px);`（约第 604 行）。
- `.task-card:hover::before` 中删 `box-shadow: var(--shadow-glow);`（约第 288 行）。
- `.stat-card:hover` 中删 `box-shadow: var(--shadow-glow), var(--shadow-lg);` 改为 `box-shadow: var(--shadow-lg);`（约第 863 行）。

- [ ] **Step 6: 去全大写 + 字距**

把以下选择器里的 `text-transform: uppercase;` 与 `letter-spacing: 0.05em;`（及类似字距）删除：
`.navbar-brand`(约103)、`.status-badge`(约354-355)、`.config-section .btn`(约560-561)、`.history-table th`(约586-588)、`.action-buttons .btn`(约619-621)、`.card-header`(约800-801)、`.stat-card p`(约880-881)。
保留 `.navbar-brand` 的存在，仅去 uppercase。

- [ ] **Step 7: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 8: 视觉验收**

刷新三页：
- 首页地图区无琥珀网格。
- 按钮点击无圆形涟漪扩散；进度条无流光。
- 鼠标悬停任务卡/表格行不再左右平移。
- 导航品牌、表头、徽章、按钮文字不再全大写。

- [ ] **Step 9: Commit**
```bash
git add static/css/style.css
git commit -m "feat(frontend): remove grid bg, glow, ripple, shimmer, uppercase, hover-shift"
```

---

## Task 3: 组件重写 A — 按钮 / 徽章 / 表单

**Files:**
- Modify: `static/css/style.css`（按钮、徽章、表单相关块）

- [ ] **Step 1: 按钮——主按钮青绿实心，次按钮 ghost**

替换 `.btn-primary`/`.btn-primary:hover`（约第 651-661 行）为：
```css
.btn-primary {
    background: var(--color-accent-strong);
    color: var(--color-on-accent);
    font-weight: 600;
    box-shadow: none;
}
.btn-primary:hover {
    background: var(--color-accent);
    color: var(--color-on-accent);
    transform: none;
}
```
替换 `.btn-secondary`/`:hover`（约第 699-709 行）为 ghost 风格：
```css
.btn-secondary {
    background: transparent;
    color: var(--color-text-primary);
    border: 1px solid var(--color-border-strong);
    box-shadow: none;
}
.btn-secondary:hover {
    background: rgba(255,255,255,0.04);
    border-color: var(--color-text-secondary);
    transform: none;
}
```
把 `.btn-success`/`.btn-info`（约663-673、728-738）的彩色发光阴影改为 `box-shadow: none;` 并去 `:hover` 的 `transform: translateY(-2px);`、把 hover 渐变改为同色稍亮的纯色。`.btn-danger`/`.btn-warning` 同样：去发光、去位移，hover 仅微调底色。
统一在 `.btn`（约第 626-631 行）保证 `font-weight: 500;` 且无 `text-transform`。

- [ ] **Step 2: 徽章——tinted 风格**

替换状态徽章块（约第 359-392 行 `.status-badge.* , .badge.bg-*`）为：
```css
.status-badge { font-weight: 600; box-shadow: none; }

.status-badge.running, .badge.bg-primary, .badge.bg-info {
    background: var(--color-info-bg) !important; color: var(--color-info) !important;
    border: 1px solid rgba(96,165,250,0.30); box-shadow: none !important;
}
.status-badge.completed, .badge.bg-success {
    background: var(--color-success-bg) !important; color: var(--color-success) !important;
    border: 1px solid rgba(16,185,129,0.30); box-shadow: none !important;
}
.status-badge.failed, .badge.bg-danger {
    background: var(--color-danger-bg) !important; color: var(--color-danger) !important;
    border: 1px solid rgba(239,68,68,0.30); box-shadow: none !important;
}
.status-badge.paused, .badge.bg-warning {
    background: var(--color-warning-bg) !important; color: var(--color-warning) !important;
    border: 1px solid rgba(245,158,11,0.30); box-shadow: none !important;
}
.status-badge.pending, .badge.bg-secondary, .badge.bg-dark {
    background: rgba(255,255,255,0.06) !important; color: var(--color-text-secondary) !important;
    border: 1px solid var(--color-border-strong); box-shadow: none !important;
}
```
（`.badge.bg-dark` 加入是因为 history.js 用 `cancelled → dark`。）

- [ ] **Step 3: 表单——清爽输入 + 青绿焦点环**

确认 `.form-control, .form-select`（约第 898-908 行）`background: var(--color-bg-tertiary); border: 1px solid var(--color-border);`，`:focus` 改为：
```css
.form-control:focus, .form-select:focus,
.config-section .form-control:focus, .config-section .form-select:focus {
    border-color: var(--color-accent);
    box-shadow: 0 0 0 3px var(--color-accent-muted);
    background: var(--color-bg-secondary);
    outline: none;
}
```
`.form-check-input:checked`（约第 1098 行）改为 `background-color: var(--color-accent); border-color: var(--color-accent);`，`:focus` 的 box-shadow 改 `0 0 0 3px var(--color-accent-muted);`。

- [ ] **Step 4: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 视觉验收**
- 主按钮（创建任务/保存配置）为青绿实心深字；次按钮为透明描边。
- 任何按钮 hover 不上浮、无彩色光晕。
- 徽章为"淡色底 + 同色字 + 细边"，不再是高饱和药丸。
- 输入框聚焦为青绿细环。

- [ ] **Step 6: Commit**
```bash
git add static/css/style.css
git commit -m "feat(frontend): restyle buttons (solid/ghost), tinted badges, teal focus"
```

---

## Task 4: 组件重写 B — 卡片 / 进度 / 表格 / 弹窗 / 杂项

**Files:**
- Modify: `static/css/style.css`

- [ ] **Step 1: 卡片与卡头**

`.card`（约第 779-786 行）确保 `border: 1px solid var(--color-border); box-shadow: var(--shadow-sm);`，`.card:hover` 改为 `border-color: var(--color-border-strong); box-shadow: var(--shadow-md);`（不要位移）。
`.card-header`（约第 792-803 行）去渐变与 2px 琥珀下边框，改：
```css
.card-header {
    background: transparent;
    color: var(--color-text-primary);
    font-weight: 600;
    border-bottom: 1px solid var(--color-border);
    border-radius: var(--radius) var(--radius) 0 0 !important;
    padding: 0.85rem 1rem;
}
```

- [ ] **Step 2: 进度条**

`.progress`（约第 399-406 行）改 `height: 8px; border-radius: 999px; background: var(--color-bg-tertiary); border: none; box-shadow: none;`（细长胶囊）。
弹窗内联进度条（history.js 写死 `height: 28px`）保留可读高度——无需改 JS，CSS 用 `.modal .progress { height: 22px; }` 覆盖即可，文字居中显示百分比。
`.progress-bar.bg-info/.bg-success/.bg-warning/.bg-danger`（约第 440-454 行）改为对应**纯状态色**（非渐变）：`bg-success→var(--color-success)`，`bg-info→var(--color-info)`，`bg-warning→var(--color-warning)`，`bg-danger→var(--color-danger)`。

- [ ] **Step 3: 表格**

`.history-table th`（约第 579-589 行）去渐变与 2px 琥珀边、去大写，改：
```css
.history-table th {
    background: var(--color-bg-tertiary);
    color: var(--color-text-secondary);
    font-weight: 600;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--color-border);
}
```
`.history-table tbody tr:hover`（约第 602-605 行）已在 Task2 去位移，确认底色 `background: rgba(255,255,255,0.03);`。

- [ ] **Step 4: 弹窗 / 分页 / 滚动条 / Leaflet**

`.modal-header`（约第 954-959 行）去 2px 琥珀边改 `border-bottom: 1px solid var(--color-border);`，`.modal-title` 颜色由琥珀改 `var(--color-text-primary)`。
分页 `.page-item.active .page-link`（约第 1016-1021 行）改 `background: var(--color-accent-strong); border-color: var(--color-accent-strong); color: var(--color-on-accent);`。
滚动条 thumb（`*::-webkit-scrollbar-thumb` 约第 1253 行 及 `.index-right` 同款）由古铜改 `background: var(--color-border-strong);`，hover `background: var(--color-text-muted);`。
Leaflet 控件 hover 边框由琥珀改 `var(--color-accent)`（约第 1296 行）——保留即可（已走别名，自动变青绿）。

- [ ] **Step 5: 重写 .stat-card 为克制版**

替换 `.stat-card`/`:hover`/`h3`/`p`（约第 850-882 行）为：
```css
.stat-card {
    text-align: left;
    padding: 1.25rem 1.5rem;
    border-radius: var(--radius);
    background: var(--color-bg-secondary);
    border: 1px solid var(--color-border);
    box-shadow: var(--shadow-sm);
    color: var(--color-text-primary);
}
.stat-card:hover { border-color: var(--color-border-strong); }
.stat-card h3 {
    font-size: 2rem;
    font-weight: 600;
    font-family: var(--font-mono);
    color: var(--color-text-primary) !important;
    margin: 0 0 0.25rem 0;
    text-shadow: none;
}
.stat-card p {
    font-size: var(--font-size-sm);
    color: var(--color-text-secondary);
    margin: 0;
    font-weight: 500;
}
.stat-card.accent h3 { color: var(--color-accent) !important; }
.stat-card.danger h3 { color: var(--color-danger) !important; }
```

- [ ] **Step 6: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 7: 视觉验收**
- 卡片为平整深色面板 + 发丝边，hover 仅边框变亮。
- 进度条细长胶囊；弹窗内进度条仍可读。
- 历史表头低调（灰字、无琥珀）。
- 弹窗/分页/滚动条/地图控件强调色统一为青绿。

- [ ] **Step 8: Commit**
```bash
git add static/css/style.css
git commit -m "feat(frontend): restyle cards, progress, tables, modal, stat-card"
```

---

## Task 5: 后端 `/api/history_stats`（TDD）

**Files:**
- Test: `tests/test_history_stats.py`（新建）
- Modify: `routes/api.py`（在 `get_history_all` 之后新增路由）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_history_stats.py`：
```python
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "database", "services.dem_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _seed(db):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        # 2 map tasks: 1 completed (10 tiles), 1 failed (3 tiles)
        cur.execute("INSERT INTO tasks (name, status, north, south, east, west, "
                    "zoom_min, zoom_max, style, total_tiles, downloaded_tiles, output_path) "
                    "VALUES ('m1','completed',1,0,1,0,1,2,'m',10,10,'/x')")
        cur.execute("INSERT INTO tasks (name, status, north, south, east, west, "
                    "zoom_min, zoom_max, style, total_tiles, downloaded_tiles, output_path) "
                    "VALUES ('m2','failed',1,0,1,0,1,2,'m',10,3,'/x')")
        # 1 dem task completed (2 files)
        cur.execute("INSERT INTO dem_tasks (name, status, north, south, east, west, "
                    "dataset, total_files, downloaded_files, output_path) "
                    "VALUES ('d1','completed',1,0,1,0,'ASTGTM.003',2,2,'/x')")
        # 1 local_terrain task completed (5 files)
        cur.execute("INSERT INTO local_terrain_tasks (name, status, maxzoom, "
                    "total_files, uploaded_files, output_path) "
                    "VALUES ('l1','completed',14,5,5,'/x')")
        conn.commit()
    finally:
        conn.close()


def test_history_stats_aggregates_three_tables(monkeypatch, tmp_path):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("database")
    _seed(db)

    resp = client.get("/api/history_stats")
    assert resp.status_code == 200
    stats = resp.get_json()["stats"]
    assert stats["total_tasks"] == 4
    assert stats["completed"] == 3
    assert stats["failed"] == 1
    assert stats["total_downloaded"] == 10 + 3 + 2 + 5  # 20


def test_history_stats_empty_db(monkeypatch, tmp_path):
    _app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/api/history_stats")
    assert resp.status_code == 200
    stats = resp.get_json()["stats"]
    assert stats == {"total_tasks": 0, "completed": 0, "failed": 0, "total_downloaded": 0}
```
> 注意：`local_terrain_tasks` 的列以 `database.py` 实际定义为准。运行前先 `grep -n "CREATE TABLE local_terrain_tasks" -A20 database.py` 核对 `maxzoom/total_files/uploaded_files/output_path` 列名，若不同则调整 INSERT。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_history_stats.py -q`
Expected: FAIL（404 或 KeyError——路由还不存在）

- [ ] **Step 3: 实现路由**

在 `routes/api.py` 的 `get_history_all` 函数结束后新增：
```python
@api_bp.route('/history_stats', methods=['GET'])
def get_history_stats():
    """Aggregate task counts and download totals across all three task tables."""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()

            def _counts(table):
                cursor.execute(
                    f"SELECT COUNT(*) AS total, "
                    f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
                    f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                    f"FROM {table}"
                )
                row = cursor.fetchone()
                return (int(row['total'] or 0), int(row['completed'] or 0), int(row['failed'] or 0))

            def _sum(table, col):
                cursor.execute(f"SELECT COALESCE(SUM({col}), 0) AS s FROM {table}")
                return int(cursor.fetchone()['s'] or 0)

            m_total, m_done, m_fail = _counts('tasks')
            d_total, d_done, d_fail = _counts('dem_tasks')
            l_total, l_done, l_fail = _counts('local_terrain_tasks')

            total_downloaded = (
                _sum('tasks', 'downloaded_tiles')
                + _sum('dem_tasks', 'downloaded_files')
                + _sum('local_terrain_tasks', 'uploaded_files')
            )

            return jsonify({
                'success': True,
                'stats': {
                    'total_tasks': m_total + d_total + l_total,
                    'completed': m_done + d_done + l_done,
                    'failed': m_fail + d_fail + l_fail,
                    'total_downloaded': total_downloaded,
                }
            })
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error getting history stats: {e}")
        return jsonify({'error': 'Failed to get history stats'}), 500
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_history_stats.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**
```bash
git add tests/test_history_stats.py routes/api.py
git commit -m "feat(api): add /api/history_stats aggregating three task tables"
```

---

## Task 6: 首页 — 表单分组 + 活动任务卡片

**Files:**
- Modify: `templates/index.html`
- Modify: `static/js/tasks.js`

- [ ] **Step 1: 表单视觉分组**

在 `templates/index.html` 下载表单（`<form id="downloadForm">` 内）用小标题分隔三组，不改任何 input id。在第一个 `mb-3`（任务名称）前插入分组小标题，并在"范围与层级""输出"前各插：
```html
<div class="form-group-label">基础</div>
```
对应在 `static/css/style.css` 末尾加：
```css
.form-group-label {
    font-size: var(--font-size-xs);
    font-weight: 600;
    color: var(--color-text-muted);
    text-transform: none;
    margin: 1.25rem 0 0.5rem;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid var(--color-border);
}
.form-group-label:first-child { margin-top: 0; }
```
分组建议：`基础`=任务名/下载类型；`范围与层级`=地图样式/zoom min-max/输出格式/(dem/local 选项)/本地最大层级；`输出`=保存路径。仅移动现有 `<div class="mb-3">` 块顺序与插入小标题，**不改 id**。

- [ ] **Step 2: 地图样式字段按类型显隐**

查看 `static/js/tasks.js` 中 `initDownloadTypeToggle`，在切换逻辑里：当 `downloadType` 为 `dem` 或 `local_terrain` 时隐藏 `#mapStyle` 所在的 `.mb-3`（给该 div 加 id `mapStyleField`），为 `map` 时显示。
模板改：给地图样式的 `<div class="mb-3">`（约第 61 行）加 `id="mapStyleField"`。
JS 改（在现有 toggle 函数体内，参照它对 `#demOptions`/`#localTerrainOptions` 的显隐写法）：
```javascript
const mapStyleField = document.getElementById('mapStyleField');
if (mapStyleField) {
    mapStyleField.style.display = (type === 'map') ? '' : 'none';
}
```
> 若 `initDownloadTypeToggle` 实际在 `map.js` 而非 `tasks.js`，改对应文件。先 `grep -rn "initDownloadTypeToggle" static/js/`。

- [ ] **Step 3: 活动任务卡片模板 + 空状态**

在 `static/js/tasks.js` 找到生成活动任务卡片 HTML 的模板字符串（含 `task-card`、`progress-bar` 等）。保持 `task-card`、`status-${status}`、`badge bg-*`、`progress-bar bg-*` 类名与所有数据节点不变，仅调整内部布局：标题行（名称 + 状态徽章）一行、进度详情一行、进度条一行，去除多余内联琥珀色。
空状态：找到写入 `#activeTasks` 的"暂无活动任务"分支，替换为：
```javascript
container.innerHTML = `
  <div style="text-align:center; padding:2rem 1rem; color:var(--color-text-muted);">
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.4; margin-bottom:0.75rem;">
      <line x1="22" y1="12" x2="18" y2="12"></line><line x1="6" y1="12" x2="2" y2="12"></line>
      <line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line>
    </svg>
    <p style="margin:0;">暂无活动任务</p>
  </div>`;
```
> 变量名 `container` 以现有代码为准（可能是 `document.getElementById('activeTasks')`）。

- [ ] **Step 4: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 视觉与功能验收**
- 表单分三组、留白清晰；切到"GDEM V3"/"本地高程切片"时地图样式字段消失，DEM/本地选项出现。
- 创建一个 map 任务，活动任务卡片实时刷新进度（SocketIO 正常）、状态色条正确、进度条无流光。
- 无任务时显示新空状态。

- [ ] **Step 6: Commit**
```bash
git add templates/index.html static/js/tasks.js static/css/style.css
git commit -m "feat(frontend): group download form, contextual map-style field, restyle active task cards"
```

---

## Task 7: 配置页 — 设置卡片 + 底部按钮

**Files:**
- Modify: `templates/config.html`
- Modify: `static/css/style.css`（`.config-section` 相关）

- [ ] **Step 1: 设置卡片样式收敛**

`static/css/style.css` 中 `.config-section h3`（约第 514-523 行）弱化：去琥珀色与大号，改：
```css
.config-section h3 {
    color: var(--color-text-primary);
    font-size: var(--font-size-md);
    font-weight: 600;
    margin-bottom: 1.25rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--color-border);
    display: flex; align-items: center;
}
```
`.config-section`（约第 500-508 行）确认 `border: 1px solid var(--color-border); box-shadow: var(--shadow-sm);`，`:hover` 改 `border-color: var(--color-border-strong);`（已在别名下自动近似，显式写更稳）。
`.config-section h3 svg`（图标）颜色随文字即可；如需弱化加 `opacity:0.7`。

- [ ] **Step 2: 底部按钮主次分明**

`templates/config.html` 底部按钮区（约第 217-231 行）：重置按钮已是 `btn-secondary`（Task3 已成 ghost），保存按钮 `btn-primary`（青绿实心）。给容器加上边距与分隔：
```html
<div class="d-flex justify-content-between" style="margin: 2rem 0; padding-top: 1.5rem; border-top: 1px solid var(--color-border);">
```

- [ ] **Step 3: form-text 提示统一**

`config.html` 中 `<small class="form-text" style="color: var(--color-text-muted);">`（约第 90 行）保留，确认 CSS 有通用 `.form-text { color: var(--color-text-muted); font-size: var(--font-size-xs); }`，没有则在 style.css 末尾补上。

- [ ] **Step 4: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: 视觉与功能验收**
- 六个设置区块为干净深色卡片，标题低调（非琥珀大字）。
- 底部：保存=青绿实心、重置=描边；保存/重置功能照旧（改几个值点保存，刷新仍在；点重置回默认）。

- [ ] **Step 6: Commit**
```bash
git add templates/config.html static/css/style.css
git commit -m "feat(frontend): refine config settings cards and footer actions"
```

---

## Task 8: 历史页 — 统计卡片 + 表格 + 详情弹窗

**Files:**
- Modify: `templates/history.html`
- Modify: `static/js/history.js`

- [ ] **Step 1: 统计卡片标记**

在 `templates/history.html` 顶部（`<div class="row">` 内、历史地图卡片之前）插入统计卡片行：
```html
<div class="col-12">
    <div class="row" id="statsRow" style="margin-bottom: 1rem;">
        <div class="col-6 col-md-3"><div class="stat-card"><h3 id="statTotal">-</h3><p>总任务</p></div></div>
        <div class="col-6 col-md-3"><div class="stat-card accent"><h3 id="statCompleted">-</h3><p>已完成</p></div></div>
        <div class="col-6 col-md-3"><div class="stat-card danger"><h3 id="statFailed">-</h3><p>失败</p></div></div>
        <div class="col-6 col-md-3"><div class="stat-card"><h3 id="statDownloaded">-</h3><p>累计下载量</p></div></div>
    </div>
</div>
```

- [ ] **Step 2: 取数填充**

在 `static/js/history.js` 的 `initHistory()` 里调用一个新函数 `loadStats()`；新增：
```javascript
async function loadStats() {
    try {
        const r = await fetch('/api/history_stats');
        const j = await r.json();
        if (!j.success) return;
        const s = j.stats;
        document.getElementById('statTotal').textContent = s.total_tasks;
        document.getElementById('statCompleted').textContent = s.completed;
        document.getElementById('statFailed').textContent = s.failed;
        document.getElementById('statDownloaded').textContent = s.total_downloaded.toLocaleString();
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}
```
在 `initHistory()` 中、`loadHistory(1)` 旁加 `loadStats();`。删除任务后刷新统计：在 `deleteTask` 成功分支 `loadHistory(currentPage)` 后加 `loadStats();`。

- [ ] **Step 3: 表格行内联色更新**

`static/js/history.js` 的 `renderHistoryTable` 模板字符串里把坐标箭头等 `var(--color-accent-warm)` 引用保留即可（已走别名→青绿）。确认状态徽章用 `badge bg-${getStatusColor(...)}`（Task3 已 tinted 化，`cancelled→dark` 已覆盖）。操作按钮 `btn-info`/`btn-danger` 保持。无需大改，仅在视觉验收时确认协调。

- [ ] **Step 4: 详情弹窗重排为键值网格**

`templates/history.html` 弹窗 `.modal-body`（约第 97-163 行）把成对的 `<p><strong>标签:</strong> <span id="...">` 重排为定义网格。**所有 `id` 必须原样保留**（`detailId/detailName/detailStatus/detailStyle/detailFormat/detailZoom/detailTotal/detailDownloaded/detailFailed/detailProgress/detailNorth/detailSouth/detailEast/detailWest/detailPath/detailCreated/detailStarted/detailCompleted/detailErrorRow/detailError/detailTerrainRow/...`）。
做法：用 CSS grid 包裹，例如把基本信息两列段落换成：
```html
<div class="detail-grid">
  <div class="detail-item"><span class="detail-k">任务ID</span><span class="detail-v" id="detailId"></span></div>
  <div class="detail-item"><span class="detail-k">任务名称</span><span class="detail-v" id="detailName"></span></div>
  <div class="detail-item"><span class="detail-k">状态</span><span class="detail-v" id="detailStatus"></span></div>
  <div class="detail-item"><span class="detail-k">地图样式</span><span class="detail-v" id="detailStyle"></span></div>
  <div class="detail-item"><span class="detail-k">输出格式</span><span class="detail-v" id="detailFormat"></span></div>
  <div class="detail-item"><span class="detail-k">缩放级别</span><span class="detail-v" id="detailZoom"></span></div>
  <div class="detail-item"><span class="detail-k">总数量</span><span class="detail-v" id="detailTotal"></span></div>
  <div class="detail-item"><span class="detail-k">已下载</span><span class="detail-v" id="detailDownloaded"></span></div>
  <div class="detail-item"><span class="detail-k">失败</span><span class="detail-v" id="detailFailed"></span></div>
</div>
<div style="margin-top:1rem;"><span class="detail-k">进度</span> <span id="detailProgress"></span></div>
```
区域范围/路径/时间/错误/terrain 各块同理保留 id，外层换 `detail-grid`。
在 `static/css/style.css` 末尾加：
```css
.detail-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem 1.5rem; }
.detail-item { display: flex; flex-direction: column; gap: 0.15rem; padding: 0.4rem 0; border-bottom: 1px solid var(--color-border); }
.detail-k { font-size: var(--font-size-xs); color: var(--color-text-muted); }
.detail-v { font-size: var(--font-size-sm); color: var(--color-text-primary); }
@media (max-width: 576px) { .detail-grid { grid-template-columns: 1fr; } }
```

- [ ] **Step 5: 防回归测试**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: 视觉与功能验收**
- 顶部四张统计卡显示真实数字（已完成=青绿、失败=红）。
- 表格状态徽章 tinted、行 hover 仅底色变化；搜索、分页正常。
- 点"详情"弹窗打开、键值网格整齐、各字段有值；DEM 任务的地形切片区块仍可启动/刷新；删除任务后统计与列表都刷新。

- [ ] **Step 7: Commit**
```bash
git add templates/history.html static/js/history.js static/css/style.css
git commit -m "feat(frontend): history stats cards + table/modal restyle"
```

---

## Task 9: 全量验证与收尾

**Files:** 无新增，仅验证

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全 PASS（含新增 `test_history_stats.py`）

- [ ] **Step 2: 三页完整手动验收**

`uv run python app.py`，逐项确认（对照 spec §8）：
- 无琥珀网格/发光/涟漪/微光/全大写/hover 平移。
- 青绿强调色只在主操作/焦点/关键状态/激活分页。
- 中文系统黑体、不发虚。
- 功能全链路：创建 map 任务→实时进度→历史出现；切 DEM/本地类型表单联动；配置保存/重置；历史搜索/分页/详情/删除；统计卡数字正确；DEM 详情地形切片启动/刷新。
- 缩放浏览器到 ≤768px：两栏堆叠、按钮全宽正常。

- [ ] **Step 3: 检查 tasks.js 行尾**

记忆中 `tasks.js` 有混合 CRLF/LF。若本次改动它，统一为 LF：
Run: `file static/js/tasks.js` 或编辑器确认；如需修复 `sed -i 's/\r$//' static/js/tasks.js` 后重测。

- [ ] **Step 4: 收尾**

REQUIRED SUB-SKILL: 使用 superpowers:finishing-a-development-branch 决定合并/PR/清理方式。

---

## Self-Review 记录

- **Spec 覆盖**：§3 令牌→T1；§4 减法→T2；§5.1 base/导航→T1/T2；组件→T3/T4；§5.2 首页→T6；§5.3 配置→T7；§5.4 历史→T8；§6 后端接口→T5；§8 验收→T9。无遗漏。
- **占位符**：无 TBD/TODO；CSS 删除项给了选择器+约行号，代码项给了完整片段。
- **契约一致**：状态类名 `status-*`/`badge bg-*`/`progress-bar bg-*`、全部 `#detail*` id、config input id 在各任务中均要求保留；新增 id（`mapStyleField`、`statTotal` 等）前后一致。
- **行号提醒**：所有"约第 N 行"为参考，实施时以实际匹配的选择器为准（CSS 在前序任务中已被编辑，行号会漂移）。
