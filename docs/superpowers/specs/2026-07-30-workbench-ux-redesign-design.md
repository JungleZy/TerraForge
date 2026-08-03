# 工作台 UX 重设计：加载动画 / 状态栏 / 框选调整 / 记录中心

> **归档文档 · 非当前实现**
> **记录时间**：2026-07-30 ｜ **状态**：**已实现**（`c854e12fe` 落地，0.2.x 沿用至今）
> 下方正文「状态：已批准」是动工前的语气，**不是待办** —— splash 加载动画、底部状态栏、框选角点可调、dock 移除并入记录中心，全部已落地并在跑。
> 三点与今日实现的出入：① 第 4 节末「左列工具条按钮文案：历史 → 记录」—— 实际文案是**「任务」**（`templates/index.html:67`，`data-panel="records"`）；② 第 4 节的记录面板结构（顶部「进行中」区 + 下方「历史」区）**已被 2026-08-01 的单一时间流重构取代**（`2d2e2c24f` / `8392bf942`，活动任务并入同一条时间流 + 状态筛选 chips，三分区已废）；③ 第 6 节要求断言的 `#recordsPanel` **并不存在** —— 实现沿用 `#historyPanel` id，`records` 只是 `static/js/panels.js` 里的逻辑名（`PANELS = { history: 'historyPanel', records: 'historyPanel', config: 'configPanel' }`）。
> 当前事实源：`templates/index.html`、`templates/_history_content.html`、`static/js/panels.js`、`static/js/history.js`。
> *正文保持原样未回改。*

---

日期：2026-07-30
状态：已批准（用户授权"根据这个项目设计一套优雅的交互和界面"）

## 背景

TerraForge 是 Flask + CesiumJS 的离线地图下载工作台。当前交互问题：

- 页面无首屏加载动画，Cesium 同步初始化期间白屏突兀。
- 底部状态栏仅有连接点 + 3 项读数，信息量低。
- 框选（自绘 rectangle，map.js）落定后不可调整，只能清除重画。
- 「下载参数」dock 常驻右侧占 380px，与「历史」面板功能割裂；活动任务卡片藏在 dock 里，和历史记录分离。

## 方案对比（已选 A）

- **A. 框选即入口（推荐）**：地图是唯一主界面，选区是下载的触发点。下载/处理改为按需弹窗，dock 整体移除；活动任务并入历史面板，统一为「记录」滑出面板。符合用户"不用常驻、框上出现下载按钮"的设想，屏幕空间全给地图。
- B. 保留 dock 但默认收起：改动小，但没有解决"下载/历史重复"的核心问题。
- C. 全面 SPA 化重写：成本高、风险大，超出本次需求。

## 设计

### 1. 首屏加载动画（Splash）

- 全屏覆盖层 `#splashScreen`（仅首页 index.html），深色背景 + 动态经纬网格扫描线 + TerraForge 标识脉冲 + 进度条与阶段文案（"正在初始化地图引擎… / 加载影像服务… / 就绪"）。
- 进度 = 模拟缓动进度（封顶 90%）+ 真实事件补完：Cesium Viewer 创建成功且首帧渲染后推到 100%，淡出（opacity transition 400ms）后 `display:none`，不阻塞交互。
- 加载失败（JS 异常）时 splash 显示错误提示而非永久停留。
- 纯 CSS/SVG 动画，无新依赖；遵守 base.html 的 vendor 版本号与 style.css 最后加载的硬规矩。

### 2. 底部状态栏

布局（左 → 右），全部首页可用：

- 连接状态点（现有 `#connStatus`）。
- 活动任务：数量徽标 + 迷你进度条 + 总百分比（由 socketio `task_progress` 聚合驱动，无任务时显示"无活动任务"）。
- 鼠标经纬度（现有，节流刷新）。
- 近似缩放级（现有）。
- 选区尺寸 W°×H°（现有 `#statusSelection`）。
- 最近事件：单行滚动消息（任务完成/失败/拼接进度等，消费目前仅 console.log 的 `task_stitch_progress` / `task_completed` / `task_failed` 事件）。
- 最右：当前时间（时钟，1s 刷新）。

状态栏内容块仍在 `{% block statusbar %}`（index.html），socket 驱动逻辑放 tasks.js / map.js 既有初始化路径，不新增 JS 文件。

### 3. 框选结果可调整

选区落定（LEFT_UP）后进入"可调"状态：

- 4 个角点拖拽手柄：Cesium point entity（高亮色描边圆点），`ScreenSpaceEventHandler` pick + drag，拖动时实时更新 `_rectDegrees`、矩形 entity、bounds overlay 与状态栏。拖动期间禁用相机（同框选实现）。
- `#boundsInfo` 浮层中 N/S/E/W 四个值变为数字输入框（step 0.00001，5 位小数），Enter 或失焦应用，自动校验（south<north、经度 ±180 wrap 复用 `_wrapLngWest/East`），非法输入回退并 toast 提示。
- 保留「重新框选」（进入绘制模式）与「删除」。
- 状态栏选区摘要同步更新。

### 4. 记录中心（合并下载/历史）

**交互流程**：

1. 用户框选 → 选区落定后，`#boundsInfo` 浮层出现主按钮「下载」（accent 色）。
2. 点击 → 打开下载弹窗（Bootstrap modal `#downloadModal`），内容为现 dock 下载表单（任务名、样式源、zoom 范围、输出格式、输出路径），并保留 DEM 下载 tab。弹窗顶部显示当前选区四至摘要。
3. 弹窗可随时关闭（× / Esc / 点击遮罩）回到地图继续拖拽调整选区；再次打开时表单内容保留、选区摘要刷新。
4. 确认下载 → `POST /api/tasks`（或 `/api/dem/tasks`）→ 弹窗关闭 → 自动滑出「记录」面板，新任务出现在"进行中"区 → toast 提示。
5. 「处理」同理改为弹窗（等值线 / 本地地形），工具条「处理」按钮直接打开，不再依赖 dock。

**记录面板**（面板元素沿用 `#historyPanel` id 不动，`records` 作为逻辑名注册进 panels.js 的 PANELS——hash `#records` 直达，`#history` 与独立页 `/history` 全部兼容）：

- 顶部「进行中」区：活动任务卡片（从 dock 迁入，socketio 实时刷新，含 start/pause/resume/cancel 操作）。
- 下方「历史」区：统计卡 + 表格 + 历史小地图（现有 history.js 功能原样保留）。
- 左列工具条按钮文案：历史 → 记录。

**移除**：`#workbenchDock` / `#dockReopen` 及其三态同步逻辑（map.js:1001-1061 一带），下载/处理表单 DOM 迁入对应 modal。

### 5. 错误处理与边界

- 无选区时「下载」按钮不出现（依附于选区浮层）；直接打开下载弹窗的入口不存在，避免无选区提交。
- modal 打开期间绘制模式不可用（按钮 disabled），防止状态错乱。
- 活动任务区为空时显示占位文案；记录面板 socket 断开时进行中区保留最后一次状态并标记。

### 6. 测试策略

- 更新契约测试：`test_map_js_contract.py`、`test_tasks_js_contract.py`、`test_css_contract.py`、`test_index_has_contour_option.py` 中引用 dock/旧按钮/旧面板的断言改为新结构断言。
- 新增轻量契约断言：index.html 含 `#splashScreen`、`#downloadModal`、`#recordsPanel`；bounds 浮层含 4 个坐标输入。
- 后端 API 不变，现有 API 测试不受影响；全量跑 `pytest tests/` 收尾。

### 7. 不做（YAGNI）

- 拖动整个矩形平移选区（角点 + 输入已够用）。
- 多选区、选区保存/复用。
- 处理表单交互重构（仅搬入弹窗，逻辑不变）。
- 状态映射表四重重复的公共化（超出本次范围，留 backlog）。
