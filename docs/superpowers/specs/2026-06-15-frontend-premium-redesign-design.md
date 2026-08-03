# 前端高级化重设计 — 设计文档

> **归档文档 · 非当前实现**
> **记录时间**：2026-06-15 ｜ **状态**：已实施，但设计令牌部分已被后续改版取代
> **⚠️ 不要把第 3 节当设计系统基准。** 青绿强调色 `--color-accent: #2dd4bf` 一族已被 GIS 蓝 sky 整套替换（`#38bdf8` / `#7dd3fc` / `#0ea5e9`），替换映射见 `2026-07-28-gis-workbench-ui-design.md:74-78`；本文只描述一套深色主题，而现在是 dark / light / system 三态（`<html data-bs-theme>` + localStorage `tf-theme`，见 `static/js/theme.js` 与 `static/css/style.css` 的 `[data-bs-theme="light"]` 块）。当前令牌事实源是 `static/css/style.css` 的 `:root`。
> 另有五处已作废：§3 的兼容别名 `--color-accent-amber/-warm/-copper` 已全部删除（全仓无引用）；`--font-sans` 现名 `--font-display`；字体不再走 Google Fonts，已本地 vendored 到 `static/vendor/fonts/`（Inter + JetBrains Mono woff2）；Leaflet 已下线，地图引擎改为 CesiumJS 1.143.0；§7 提到的 PyInstaller `build.spec` 已删除，打包改用 Nuitka（`nuitka_build.py`）。
> 仍成立：§6 的 `GET /api/history_stats` 已上线且响应形状与本文一致（`routes/api.py:679`，只是聚合表从三张扩到四张，含 `contour_tasks`）；§7 的 DOM 契约除 `#activeTasks`（已随 2026-08「单一时间流」改版移除）外仍守着——`#boundsInfo`、`#createTaskBtn`、`#historyTableBody`、`#pagination`、`#searchInput`、全部 `#detail*`（已迁至 `templates/base.html`）与 `status-*` / `badge bg-*` / `progress-bar bg-*` 类名均未变。
> *正文保持原样未回改。*

---

**日期**: 2026-06-15
**结论**: 把当前"用力过猛的工业深色风"重做成**克制的高级深色**（Linear/Vercel/Arc 路子），换青绿强调色，做视觉 + 局部布局重构，并为历史页新增统计卡片（含一个后端聚合接口）。功能零回归——所有 JS DOM 契约保持不变。

---

## 1. 目标与非目标

**目标**
- 整体气质：克制、现代、高级、人性化。
- 去掉廉价/游戏感来源：网格背景、发光、全大写、涟漪、微光、高饱和渐变药丸、列表 hover 平移。
- 换全新青绿强调色系（替代琥珀/古铜警示色）。
- 中文文本落到系统优质黑体（人性化细节）。
- 历史页加真实数据的统计卡片。

**非目标（YAGNI）**
- 不引入前端框架（保持 Bootstrap 5.3 + 原生 JS）。
- 不重写业务逻辑、不动下载/切片流程。
- 不做暗/亮主题切换（只做一套高级深色）。
- 不动 Leaflet 地图交互逻辑（仅按新配色微调控件样式）。

---

## 2. 设计原则

1. **减法优先**：高级感来自克制，不是堆装饰。能删的特效全删。
2. **层级靠对比，不靠特效**：用背景明度分层 + hairline 边框 + 留白建立层级，而非发光和粗边框。
3. **强调色稀缺**：青绿只用在主操作、关键状态、焦点环；不滥用。
4. **数字用等宽，文字用无衬线**：瓦片数、坐标、统计数字用 mono；标签/按钮/正文用 Inter。
5. **契约不变**：所有 JS 依赖的 DOM id、状态类名（`status-running` 等）、Bootstrap 类名保持，纯换皮 + 受控的结构重排。

---

## 3. 设计令牌（`:root`，重写 style.css 顶部变量）

### 配色
```css
/* 背景三层 */
--color-bg-primary:   #0c0d10;  /* 页面底 */
--color-bg-secondary: #15171c;  /* 卡片/面板 */
--color-bg-tertiary:  #1c2027;  /* 输入框/嵌套表面 */

/* 边框（hairline） */
--color-border:        rgba(255,255,255,0.07);
--color-border-strong: rgba(255,255,255,0.12);

/* 文字 */
--color-text-primary:   #e8eaed;
--color-text-secondary: #9aa0aa;
--color-text-muted:     #5f6670;

/* 强调色：青绿（方案 B） */
--color-accent:        #2dd4bf;  /* 主强调 */
--color-accent-hover:  #5eead4;  /* hover 提亮 */
--color-accent-strong: #14b8a6;  /* 实心按钮底 */
--color-accent-muted:  rgba(45,212,191,0.12);  /* 焦点环/tint 底 */
--color-on-accent:     #04201c;  /* 青绿实心上的深色文字（对比度足够） */

/* 状态色（与强调色区分：青绿=品牌，不当状态色用） */
--color-success: #34d399;   --color-success-bg: rgba(16,185,129,0.12);
--color-danger:  #f87171;   --color-danger-bg:  rgba(239,68,68,0.12);
--color-warning: #fbbf24;   --color-warning-bg: rgba(245,158,11,0.12);
--color-info:    #60a5fa;   --color-info-bg:    rgba(96,165,250,0.12); /* running 用蓝，区别于品牌青绿 */

/* 兼容别名：旧变量名指向新值，避免改 JS 内联引用（见 §9 风险2） */
--color-accent-amber: var(--color-accent);
--color-accent-warm:  var(--color-accent-hover);
--color-accent-copper: var(--color-accent-strong);
```

### 字体
```css
--font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI',
             'PingFang SC', 'Microsoft YaHei', 'Hiragino Sans GB', sans-serif;
--font-mono: 'JetBrains Mono', 'Space Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
```
- 基础字重 300 → **400**（300 在小字号发虚，是"不够高级"的隐藏元凶）。
- base.html 的 Google Fonts 引入改为 Inter（带 400/500/600）+ 可选 JetBrains Mono。
- 字号系统沿用现有 `--font-size-*` 变量（已统一，无需改）。

### 阴影 / 圆角
```css
--shadow-sm: 0 1px 2px rgba(0,0,0,0.30);
--shadow-md: 0 4px 12px rgba(0,0,0,0.35);
--shadow-lg: 0 12px 32px rgba(0,0,0,0.45);
/* 删除 --shadow-glow */

--radius-sm: 8px;   /* 控件 */
--radius:    10px;  /* 卡片 */
--radius-lg: 14px;  /* 大面板/模态框 */
```

---

## 4. 全局减法清单（style.css）

逐条删除/改写，这些是"廉价感"的直接来源：

| 现状 | 改为 |
|---|---|
| `.main-content::before` 琥珀网格背景 | 删除（或极淡径向渐变，默认删） |
| `--shadow-glow` 及所有 `box-shadow` 发光 | 删除，统一用新柔和阴影 |
| `.btn::before` 涟漪扩散动画 | 删除，hover 改为底色提亮 + 可选 1px translateY |
| `.progress-bar::before` shimmer 微光 | 删除 |
| 卡片/表格行 `:hover { transform: translateX(4px) }` | 删除，改边框/底色微变 |
| `.index-left .card::before` 古铜→琥珀三色渐变条 | 删除，或换单色 1px 顶边 |
| 大量 `text-transform: uppercase` + `letter-spacing`（按钮/卡头/徽章/导航品牌/表头/统计） | 改回正常大小写；仅保留极小微标签可选大写 |
| 高饱和渐变药丸徽章 + 发光 | 改 tinted 风格：`color: 状态色; background: 状态色-bg; border: 1px 状态色 25%` |
| 按钮高饱和渐变 + 彩色发光阴影 | 主按钮=青绿实心(深字)；次按钮=ghost(透明底+hairline)；危险/警告=tinted 或低饱和实心 |

---

## 5. 逐页改动

> 标注约定：**[CSS]** 仅样式 · **[模板]** 改 HTML 模板 · **[JS]** 必须连带改对应 JS

### 5.1 全局 base.html + 导航 — [模板][CSS]
- 引入 Inter 字体，更新字体栈变量。
- 导航栏：去 2px 琥珀下边框 + 发光 → hairline 下边框；品牌名去全大写、去 hover 发光；当前页 nav-link 用青绿 subtle 高亮（下划线细线或底色）。
- 删网格背景层。

### 5.2 首页 index.html — [模板][CSS][JS]
- **下载表单分组**：扁平字段 → 三组（`基础`：任务名/类型；`范围与层级`：样式/zoom/输出格式/本地切片层级；`输出`：保存路径），组间留白 + 细分隔。[模板][CSS]
- **上下文显隐**：`initDownloadTypeToggle` 已存在；接入"地图样式字段在 DEM/本地地形类型下隐藏"。[JS]
- **bounds 指示**：`#boundsInfo` 蓝 alert → 轻量内联指示（未选区域=弱提示 / 已选=青绿确认）。保持元素 id 不变，仅换样式与文案容器。[CSS]（map.js 仍写同一节点，必要时微调它写入的 class）
- **活动任务卡片重排**：标题/状态/进度三段更清晰，徽章换 tinted，进度条变细去 shimmer。**这块 HTML 由 tasks.js 生成 → 改 tasks.js 模板字符串**；保持 `task-card`、`status-*` 类名不变。[JS][CSS]
- **空状态**：`#activeTasks` 的"暂无活动任务"→ 图标 + 说明的正经空状态。[JS] 或 [模板]

### 5.3 配置 config.html — [模板][CSS]
- 6 个 `.config-section` 保留，重做为干净设置卡片：标题弱化（不再大号琥珀全大写）、字段网格对齐、`form-text` 提示统一样式。
- 底部按钮：保存=青绿实心主按钮，重置=ghost 次按钮；考虑做成吸底操作栏。
- 纯样式 + 模板微调，**不碰 config.js**（input id 全部不变）。

### 5.4 历史 history.html — [模板][CSS][JS][后端]
- **顶部统计卡片**（新增）：总任务 / 已完成 / 失败 / 累计下载量（瓦片+文件）。用克制版 `.stat-card`（重写：去琥珀渐变，改深色面板 + 大号 mono 数字 + 弱标签）。
  - 数据来源：**新增后端接口**（见第 6 节），history.js 加载时拉取并填充。[后端][JS][模板]
- **表格**：去 hover 平移；hairline 分隔 + 行 hover 底色微亮；状态列换 tinted 徽章；操作列图标按钮统一。表格行 HTML 由 history.js 生成 → 改其模板字符串中的内联样式与 class。[JS][CSS]
  - 注意：history.js 行内大量 `var(--color-accent-warm)` 引用（坐标箭头等）→ 改为新令牌。
- **任务详情弹窗**：现为一堆 `<strong>:` 段落 → 重排为键值网格/定义列表，更易扫读。**所有 `#detail*` id 保持不变**，只动外层布局 → history.js 填充逻辑不用改。[模板][CSS]
- 弹窗内 progress bar、徽章、terrain 区块跟随新样式。

---

## 6. 后端新增（唯一后端改动）

**`GET /api/history_stats`** — `routes/api.py`

跨三张表聚合，返回统计卡片所需数据：
```json
{
  "success": true,
  "stats": {
    "total_tasks": 0,
    "completed": 0,
    "failed": 0,
    "total_downloaded": 0   // 三表 downloaded_tiles + downloaded_files + uploaded_files 之和
  }
}
```
实现：对 `tasks` / `dem_tasks` / `local_terrain_tasks` 各跑 `COUNT(*)`、按 status 分组计数、`SUM(...)` 下载量，相加。沿用 `get_connection()` 模式与现有 `history_all` 一致。

**测试**：在 `tests/` 加 `test_history_stats.py`，建临时库插几条三类任务，断言聚合数字正确（遵循现有测试模式：先 monkey-patch `Config.*`，再 import）。

---

## 7. 不可破坏的契约（回归红线）

- **状态类名**：`status-running/completed/failed/paused/pending`、`badge bg-*`、`progress-bar bg-*` —— tasks.js/history.js 依赖，类名不变只改其 CSS。
- **DOM id**：history 弹窗全部 `#detail*`、`#activeTasks`、`#boundsInfo`、`#createTaskBtn`、config 所有 input id、`#historyTableBody`、`#pagination`、`#searchInput` —— 不变。
- **Bootstrap 行为类**：`modal`、`collapse`、`btn-close`、`page-link` 等交互类保持（bootstrap.bundle.js 依赖）。
- **PyInstaller**：所有改动是模板/CSS/JS/一个路由，`build.spec` 已收集 templates+static，无需改打包。

---

## 8. 测试与验收

- **后端**：`uv run pytest tests/` 全绿；新增 `test_history_stats.py` 通过。
- **手动视觉验收**（`uv run python app.py` 起服务，逐页看）：
  - 三页无琥珀网格/发光/涟漪/微光/全大写。
  - 青绿强调色只出现在主操作/焦点/关键状态。
  - 中文渲染为系统黑体、不发虚。
  - 创建任务、活动任务实时进度（SocketIO）、历史筛选/分页/详情弹窗/删除、配置保存/重置 —— 功能全部照旧可用。
  - 统计卡片显示真实数字。
- **响应式**：≤768px 下两栏堆叠、按钮全宽仍正常。

---

## 9. 风险与回归点

1. **改 tasks.js / history.js 的模板字符串**是最大回归源——任何 class/id 写错会让实时进度或表格渲染失效。对策：只改样式相关 class 与内联 style，保留结构 id/状态 class，改完逐项手动验收。
2. **新令牌名变更**：若沿用旧变量名（`--color-accent-warm` 等）则 JS 内联引用零改动最稳。**决定：保留旧变量名作为别名指向新值**，减少 JS 改动面（history.js 多处引用 `--color-accent-warm`）。
3. 后端新接口需与现有 `get_connection()` 生命周期一致，避免连接泄漏。

---

## 10. 实施顺序（供后续 plan 参考）

1. 令牌层：重写 style.css `:root` + 全局减法（base.html 字体/导航/网格）。
2. 组件层：按钮/徽章/输入/卡片/进度条/表格/弹窗的新样式。
3. 后端：`/api/history_stats` + 测试。
4. 逐页：index（表单分组 + tasks.js 卡片）、config（设置卡片）、history（统计卡片 + 表格 + 弹窗重排）。
5. 全量测试 + 逐页手动视觉验收。
