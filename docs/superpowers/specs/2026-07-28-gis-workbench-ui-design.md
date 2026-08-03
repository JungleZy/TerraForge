# GIS 工作台界面改版设计

> **归档文档 · 非当前实现**
> **记录时间**：2026-07-28 ｜ **状态**：**2026-07-28 时点设计快照 · 部分作废**（下方正文「状态：已获用户方向性确认」是当日语气，不代表今天）
> **已作废**：① 顶部工具栏 `.workbench-toolbar` —— 已删，`templates/base.html:53-55` 的注释专门反驳它（现在没有顶部工具栏，历史/配置入口是首页地图上的浮动按钮）；② 380px 右侧 dock `.workbench-dock` / `.index-right` 与展开把手 `.dock-reopen-handle` —— 2026-07-30 `c854e12fe` 整体删除，全仓已 grep 不到 `workbenchDock` / `dockReopen`；③ **Leaflet 相关的一切**（Leaflet.draw 工具栏、`L.control.scale`、`invalidateSize()`）—— 地图引擎已于 2026-07-29 `38e3e30fc` 换成 CesiumJS；④「不做浅色主题」—— 已被 2026-08-01 `5c4cbefe7`（明暗/跟随系统主题切换）反转。
> **仍有效**：① 第 68 行起的「配色与视觉令牌」表 —— 五个 accent 令牌与 `static/css/style.css:31-35` **逐值一致**（`#38bdf8` / `#7dd3fc` / `#0ea5e9` / `rgba(56,189,248,0.12)` / `#041e2b`）；② 第 114 行起的「追加决策（2026-07-29）：历史/配置改为覆盖面板」—— 该架构今天仍在跑：`.workbench-panel` 480px、`.workbench-panel--wide` 920px（`static/css/style.css:765-784`）、z-index 阶梯（backdrop 1400 < 面板 1401 < modal-backdrop 1450 < modal 1500 < toast 11000）、`static/js/panels.js` 的面板懒初始化。
> 当前事实源：`static/css/style.css`、`templates/index.html`、`templates/base.html`、`static/js/panels.js`。
> 📌 本文件被 `../plans/2026-07-28-gis-workbench-ui.md` 的「设计文档」行按路径引用，**请勿移动或改名**。
> *正文保持原样未回改。*

---

日期：2026-07-28
状态：已获用户方向性确认（布局 A / 蓝色系 / 固定 dock / 三页统一 / 实现方案 1）

## 目标

当前界面是「深色网页表单」观感，用户认为小气、不像专业 GIS 处理系统。本次改版把首页重构为**全屏地图工作台**（QGIS / ArcGIS Pro / Google Earth Engine 形态），并把历史记录、配置两页统一进同一套工作台外壳，强调色由青绿换为 GIS 蓝色系。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 整体布局 | 全屏地图工作台：顶部工具栏 + 地图主画布 + 右侧 dock + 底部状态栏 |
| 配色 | 保留深色背景，强调色换为 GIS 蓝/青蓝（sky 系） |
| 右侧面板 | 固定停靠 dock，约 380px，可一键收起/展开，不可拖拽调宽 |
| 页面范围 | 首页、历史记录、配置三页统一外壳 |
| 实现路线 | 方案 1：保留 Bootstrap 5.3 基础组件，重构外壳模板 + CSS 令牌 |

## 布局结构

> ⚠️ **以下布局图及其后各小节（顶部工具栏 / 地图主区 / 右侧 dock / 底部状态栏 / 历史配置页）已作废**：工具栏与 dock 均已删除，Leaflet 已换 CesiumJS。底部状态栏是唯一幸存者（`.workbench-statusbar` 仍在 `static/css/style.css:463`）。当前形态见第 114 行起的 2026-07-29 追加决策。

```
┌──────────────────────────────────────────────────────────────┐
│ 顶部工具栏 (48px)：品牌 │ 导航(首页/历史/配置) │ 连接状态指示  │
├────────────────────────────────────┬─────────────────────────┤
│  地图全屏画布（Leaflet 铺满）       │ 右侧 dock 380px          │
│  框选后右上角浮层显示范围信息       │ · 下载参数（独立滚动）    │
│                                    │ · 活动任务              │
│                                    │ 头部有收起按钮           │
├────────────────────────────────────┴─────────────────────────┤
│ 状态栏 (28px)：鼠标经纬度 │ 缩放级别 │ 比例尺 │ 已选区域估算  │
└──────────────────────────────────────────────────────────────┘
```

### 顶部工具栏（`.workbench-toolbar`）

- 替代现有 Bootstrap navbar，高 48px，固定在视口顶部（外壳整体用 flex 列布局占满 100vh，不用 fixed 定位 + padding 补偿）。
- 左：品牌（现有定位图标 SVG + "Maps Downloader"）。
- 中/右：导航链接 首页 / 历史记录 / 配置，active 逻辑沿用现有 `location.pathname` 比对脚本。
- 最右：Socket.IO 连接状态指示（绿点「已连接」/红点「已断开」）。任务进度依赖 WebSocket，连接状态本就该可见；断开时配合现有 toast 机制不重复告警。
- 窄屏（<768px）：导航折叠为汉堡菜单，沿用 Bootstrap collapse。

### 地图主区（首页）

- `#map` 从卡片中解放，直接铺满主区剩余空间；删除「选择下载区域」卡片外壳。
- 框选完成后，地图右上角显示范围信息浮层（`.bounds-overlay`）：南北东西经纬度 + 瓦片数估算，替代现表单底部的 `#boundsInfo` alert；未框选时浮层不显示。
- Leaflet 的 scale 控件添加到地图（位置左下），zoom 控件保持左上；Leaflet.draw 工具栏位置不变。
- dock 收起/展开动画结束后调用 `map.invalidateSize()`。

### 右侧 dock（`.workbench-dock`）

- 宽 380px，flex 不收缩；内部纵向分两段：「下载参数」（表单，独立滚动）与「活动任务」。
- dock 头部右侧放收起按钮（chevron 图标）；收起时 dock 平移出视口，地图占满整行，地图右缘浮出一个 32px 宽的展开把手（`.dock-reopen-handle`）。
- 收起状态只存内存（`classList` 切换），不做 localStorage 持久化（YAGNI）。
- 表单分组标签（基础 / 范围与层级 / 输出）与字段结构不变——本次不改交互逻辑，只改外壳与质感。

### 底部状态栏（`.workbench-statusbar`）

- 高 28px，等宽字体（JetBrains Mono），字号 xs。
- 首页内容：`经度 xx.xxxx° 纬度 xx.xxxx°`（`mousemove` 实时更新，节流 ~50ms）、`缩放 zN`（`zoomend` 更新）、已选区域摘要（框选后显示矩形尺寸与瓦片数估算，未框选显示「未选择区域」）。
- 比例尺直接用 Leaflet `L.control.scale`（定位左下，视觉上与状态栏相邻，不强求塞进状态栏 DOM）。
- 历史记录/配置页的状态栏只显示左侧全局信息（如当前任务总数/连接状态复用工具栏指示则留空该区），保持外壳一致。

### 历史记录 / 配置页

- 复用同一 `base.html` 外壳（工具栏 + 状态栏）；内容区居中、最大宽度约 1100px，卡片样式与 dock 面板同一质感（同一 CSS 变量）。

## 配色与视觉令牌

背景三层、文字、状态色、密度令牌（`--ctl-h` 等）**全部保留不动**，只替换强调色一族：

| 令牌 | 现值（青绿） | 新值（GIS 蓝） |
|---|---|---|
| `--color-accent` | `#2dd4bf` (teal-400) | `#38bdf8` (sky-400) |
| `--color-accent-hover` | `#5eead4` (teal-300) | `#7dd3fc` (sky-300) |
| `--color-accent-strong` | `#14b8a6` (teal-500) | `#0ea5e9` (sky-500) |
| `--color-accent-muted` | `rgba(45,212,191,0.12)` | `rgba(56,189,248,0.12)` |
| `--color-on-accent` | `#04201c` | `#041e2b`（accent 上的墨色；初稿 #082f49 实测 active 态对比度不足，加深至此） |

- 状态色不动；`--color-info: #60a5fa` 与 accent 同色系但角色不同（信息展示 vs 可交互），可接受。
- 沿用现有「同档位移」规则（Tailwind 400→300 hover），与 `test_button_hover_is_a_real_change` 的相对亮度约束兼容。
- Leaflet.draw 工具栏、Leaflet 控件在深色主题下的既有覆盖样式保留，新增对蓝色 accent 的适配（hover/激活态引用变量即可，不写死颜色）。

## 组件与文件改动清单

- `templates/base.html`：重构为工作台外壳（toolbar / main flex / statusbar），加连接状态指示元素；保留 vendor 资源加载顺序与三条硬规矩注释（版本号字面量、不加 integrity、style.css 最后）。
- `templates/index.html`：去掉地图卡片与 boundsInfo alert，改为地图 + bounds 浮层 + dock 结构。
- `templates/history.html`、`templates/config.html`：仅接入新外壳（内容区容器类），内部结构不动。
- `static/css/style.css`：替换 accent 令牌值；新增工作台组件样式（toolbar、statusbar、dock、dock 收起态、bounds-overlay、连接状态点）；删除被替代的旧 navbar / index-layout 样式；新增样式一律用既有变量，不引入未定义 var()（`test_no_dangling_custom_property_references` 兜住）。
- `static/js/map.js`：新增状态栏数据源（mouseenter/mousemove/zoomend 监听）、dock 开合 + `invalidateSize()`、bounds 浮层更新；`initMap` 签名不变。
- `static/js/tasks.js`：框选后的瓦片数估算同步写到状态栏与浮层（复用现有估算逻辑，若现有逻辑只在 alert 上则搬移而非复制）。
- `static/js/ui.js`：新增 Socket.IO connect/disconnect 全局监听更新工具栏状态点（若 socket 由 tasks.js 建立，则在其建立处挂监听，以实际代码为准）。
- `tests/test_css_contract.py`：同步 accent 色值断言；新增「工作台外壳关键选择器存在」类断言时只测契约（令牌值、选择器存在性、对比度），不测像素。

## 错误处理

- Socket.IO 断开：工具栏状态点变红显示「已断开」，重连恢复；不新增弹窗（现有 toast 机制已覆盖任务级错误）。
- 地图容器尺寸为 0（dock 动画中）：`invalidateSize()` 在 `transitionend` 后调用并带 300ms 兜底定时器。
- 窄屏：dock 改为覆盖式抽屉（position absolute 全高覆盖在地图上），不挤压地图；<576px 时状态栏只保留经纬度 + 缩放。

## 测试

- 运行既有全套 pytest；重点维护 `tests/test_css_contract.py`（色值、密度自洽、var() 无悬空、对比度）。
- 模板相关既有测试（如 `test_css_contract.py` 中从 base.html 抠版本号的三条断言）必须保持通过——vendor 版本号字面量不动。
- 若存在 UI 截图基线流程（`docs/ui-baseline/`），实现后重新出图人工验收，不把截图比对做成自动测试。

## 明确不做（YAGNI）

> ⚠️ **本段已作废**：dock 本身已删除（前两条无对象）；「不做悬浮面板」被次日的覆盖面板决策推翻（见第 114 行）；「不做浅色主题」被 2026-08-01 `5c4cbefe7` 反转，明暗/跟随系统三态主题现已实现。

- 不做 dock 拖拽调宽、不做面板布局持久化、不做悬浮面板。
- 不改任何后端 API、下载逻辑、表单字段与校验逻辑。
- 不引入新前端依赖（无新 vendor 库、无 CDN）。
- 不做浅色主题。

## 追加决策（2026-07-29）：历史/配置改为覆盖面板

用户反馈独立页与首页割裂感强。改为单窗口形态（ArcGIS Online / Felt 模式）：

- 历史记录、配置做成首页地图上的**右侧滑出覆盖面板**（`.workbench-panel`，
  历史 920px / 配置 480px，位于工具栏与状态栏之间），地图始终为背景。
  导航点击在首页被 `static/js/panels.js` 拦截改为打开面板；ESC / 点 backdrop /
  关闭按钮收起；支持 `#history` `#config` hash 直达与前进后退（hashchange）。
- 内容抽成 `templates/_history_content.html` / `_config_content.html` 两个
  Jinja partial，首页面板与独立页共享；独立页 /history /config 保留可用。
- 面板懒初始化：首次打开才 initHistory/initConfig（hidden 容器建 Leaflet 地图
  会得到 0 尺寸）。z-index：Leaflet ≤1000 < backdrop 1400 < 面板 1401
  < modal-backdrop 1450 < modal 1500（历史详情弹窗在面板之上）< toast 11000。
- index 路由改为传扁平化全量 config（配置面板 partial 需要）。
