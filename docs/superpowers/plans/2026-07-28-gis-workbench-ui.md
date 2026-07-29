# GIS 工作台界面改版实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把首页重构为全屏地图工作台（工具栏 + 地图画布 + 右侧 dock + 状态栏），强调色青绿换 GIS 蓝，历史/配置页统一外壳。

**Architecture:** 保留 Bootstrap 5.3 基础组件；重构 `base.html` 为 flex 列工作台外壳（toolbar/main/statusbar，不再 fixed-top）；首页地图铺满主区、参数与活动任务装进 380px 右侧 dock（可收起）；契约测试按各断言自带「有意视觉改动请同步更新」的说明同步。

**Tech Stack:** Flask + Jinja、Bootstrap 5.3（本地 vendor）、Leaflet 1.9.4 + Leaflet.draw 1.0.4、Socket.IO 4.5.4、纯 CSS 变量设计令牌、pytest 文本级契约测试。

**设计文档:** `docs/superpowers/specs/2026-07-28-gis-workbench-ui-design.md`（决策依据、ASCII 布局图，先读）。

## Global Constraints

- 全部第三方前端资源走 `static/vendor/`；base.html 三条硬规矩不动：版本号字面量、不加 integrity/crossorigin、`style.css` 必须最后一张表。
- 不引入任何新前端依赖、无 CDN、不做浅色主题。
- 不改后端 API、不改表单字段与校验逻辑；所有既有元素 id（`#map` `#boundsInfo` `#downloadForm` `#createTaskBtn` `#activeTasks` 及全部表单字段 id）保持不变。
- 新 CSS 一律引用 `:root` 既有变量；禁止未定义 var()（`test_no_dangling_custom_property_references` 兜住）；禁止 font-size 带 !important；`!important` 总量上界 68，新增需登记。
- 配色只换 accent 一族：`--color-accent: #38bdf8`、`--color-accent-hover: #7dd3fc`、`--color-accent-strong: #0ea5e9`、`--color-accent-muted: rgba(56,189,248,0.12)`、`--color-on-accent: #041e2b`（#082f49 实测 active 态墨/底对比度仅 4.04:1，加深到此值后最差 4.74:1）。另：`.btn-outline-primary:disabled` 需单独禁用墨 `#828a94`（启用墨 #38bdf8 与通用禁用墨 #9aa0aa 亮度差不足 0.15）。背景/文字/状态色/密度令牌全部不动。
- **本环境不做任何 git 提交**（用户未显式授权 git 变更）；计划中的「Commit」步骤一律跳过，验收以 pytest 全绿 + 截图为准。
- 运行测试：`.venv/bin/python -m pytest tests/ -x -q`（若 `.venv` 不可用则 `python -m pytest`）。

---

### Task 1: accent 配色切换（青绿 → GIS 蓝）

**Files:**
- Modify: `static/css/style.css`（:root accent 五令牌，约 :30-35 行区域）
- Modify: `static/js/map.js:78,101,104`（框选矩形硬编码青绿）
- Modify: `static/js/ui.js:4`（注释里的 "teal 强调色" 提法）
- Test: `tests/test_css_contract.py`

**Interfaces:**
- Produces: 新 accent 色值（见 Global Constraints），后续所有任务的 hover/激活态直接 var() 引用。

- [ ] **Step 1: 改 style.css 令牌**

`:root` 中五行替换为：

```css
    /* 强调色：GIS 蓝（sky 系，与状态色同走 Tailwind 400→300 hover 档位） */
    --color-accent:        #38bdf8;
    --color-accent-hover:  #7dd3fc;
    --color-accent-strong: #0ea5e9;
    --color-accent-muted:  rgba(56,189,248,0.12);
    --color-on-accent:     #082f49;
```

同时把该段上方「强调色：青绿」注释更新为上表所示注释；文件内其它提到「青绿/teal」的说明性注释（如 :519 附近的「发光改用品牌色柔和版」段、`.task-card::before` 段）同步改写为「品牌蓝」，不改变任何声明。

- [ ] **Step 2: 改 map.js 三处硬编码**

`static/js/map.js:78` 与 `:101`：`color: '#2dd4bf'` → `color: '#38bdf8'`；`:104`：`fillColor: '#5eead4'` → `fillColor: '#7dd3fc'`。

- [ ] **Step 3: 改 ui.js 注释**

`static/js/ui.js:4`：`（teal 强调色）` → `（GIS 蓝强调色）`。

- [ ] **Step 4: 跑契约测试，同步色值断言**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py -q`
Expected: 若有断言钉住旧 accent 字面量（#2dd4bf 等）而变红，按该断言 docstring 的「有意视觉改动，同步更新期望值即可」说明改成新值；其余全绿。已知 grep 结果：测试文件未直接钉 accent 的 hex 字面量（只引用 var 名与 hover 亮度步长），预期无需改动或极少改动。

---

### Task 2: base.html 工作台外壳（工具栏 + 状态栏 + 连接指示）

**Files:**
- Modify: `templates/base.html`（:35-97 body 结构）
- Modify: `static/css/style.css`（Navbar Styles 段 :241-316 重写）
- Modify: `static/js/ui.js`（末尾新增 initConnectionStatus）
- Modify: `static/js/tasks.js:5-15`（initTasks 里挂上连接指示）
- Test: `tests/test_css_contract.py`（`.navbar*` 字号表不动；几何类断言留到 Task 6 统一同步）

**Interfaces:**
- Produces:
  - CSS 类：`.workbench`（flex 列外壳）、`.workbench-statusbar`、`.statusbar-item`、`.conn-status` / `.conn-status--on` / `.conn-status--off` / `.conn-dot`。
  - JS：`window.initConnectionStatus(socket)` —— 挂 connect/disconnect 监听，切换工具栏连接点状态；无 socket 的页面不调用，指示器保持 `hidden`。
  - Jinja block：`{% block statusbar %}`（各页定制状态栏内容，默认为空）。
  - 后续任务依赖：`.main-content` 不再有 `padding-top`，改为 flex 子项 `flex:1; min-height:0`。

- [ ] **Step 1: 重写 base.html 的 body 结构**

`<nav>` 去掉 `fixed-top` 类，加 `workbench-toolbar` 类；导航 `<ul>` 之后加连接指示；`<main>` 之后加状态栏。vendor 资源区与三条硬规矩注释原样保留。完整新 body（`<head>` 不动）：

```html
<body>
    <div class="workbench">
        <!-- Top Toolbar -->
        <nav class="navbar navbar-expand-lg navbar-dark workbench-toolbar">
            <div class="container-fluid">
                <a class="navbar-brand" href="/">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 8px;">
                        <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
                        <circle cx="12" cy="10" r="3"></circle>
                    </svg>
                    Maps Downloader
                </a>
                <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav"
                        aria-controls="navbarNav" aria-expanded="false" aria-label="展开导航菜单">
                    <span class="navbar-toggler-icon"></span>
                </button>
                <div class="collapse navbar-collapse" id="navbarNav">
                    <ul class="navbar-nav ms-auto">
                        <li class="nav-item">
                            <a class="nav-link" href="/">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                                    <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>
                                    <polyline points="9 22 9 12 15 12 15 22"></polyline>
                                </svg>
                                首页
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link" href="/history">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                                    <circle cx="12" cy="12" r="10"></circle>
                                    <polyline points="12 6 12 12 16 14"></polyline>
                                </svg>
                                历史记录
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link" href="/config">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                                    <circle cx="12" cy="12" r="3"></circle>
                                    <path d="M12 1v6m0 6v6m5.2-13.2l-4.2 4.2m0 6l4.2 4.2M23 12h-6m-6 0H1m18.2 5.2l-4.2-4.2m-6 0l-4.2 4.2"></path>
                                </svg>
                                配置
                            </a>
                        </li>
                    </ul>
                    <span class="conn-status" id="connStatus" hidden>
                        <span class="conn-dot"></span><span id="connStatusText">未连接</span>
                    </span>
                </div>
            </div>
        </nav>

        <script>
          (function () {
            var p = location.pathname;
            document.querySelectorAll('.navbar-nav .nav-link').forEach(function (a) {
              var href = a.getAttribute('href');
              if (href === p || (href === '/' && p === '/')) a.classList.add('active');
            });
          })();
        </script>

        <!-- Main Content -->
        <main class="main-content">
            {% block content %}{% endblock %}
        </main>

        <!-- Bottom Status Bar -->
        <footer class="workbench-statusbar">
            {% block statusbar %}{% endblock %}
        </footer>
    </div>

    <!-- 既有 script 区块（bootstrap / leaflet / leaflet.draw / socket.io / ui.js / extra_js）原样保留 -->
</body>
```

- [ ] **Step 2: 重写 style.css Navbar Styles 段（:241-316）**

整段（`/* Navbar Styles */` 到 `.main-content` 规则结束）替换为：

```css
/* Workbench shell：flex 列布局占满视口。navbar 不再 fixed-top，
   .main-content 不再有 padding-top 补偿。 */
.workbench {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
}

/* Toolbar Styles（48px，比原 60px navbar 紧凑） */
.navbar.workbench-toolbar {
    flex: 0 0 48px;
    background: var(--color-bg-secondary) !important;
    border-bottom: 1px solid var(--color-border);
    box-shadow: var(--shadow-sm);
    padding: 0 0.75rem;
    min-height: 48px;
}

.navbar-brand {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: var(--font-size-xl);
    letter-spacing: -0.02em;
    color: var(--color-accent-hover) !important;
    transition: background-color 0.15s ease, border-color 0.15s ease,
                color 0.15s ease, box-shadow 0.15s ease;
    padding: 0.25rem 0.75rem;
}

.navbar-brand:hover {
    color: var(--color-accent) !important;
    text-shadow: none;
}

.nav-link {
    color: var(--color-text-secondary) !important;
    font-weight: 400;
    transition: background-color 0.15s ease, border-color 0.15s ease,
                color 0.15s ease, box-shadow 0.15s ease;
    position: relative;
    padding: 0.4rem 0.8rem !important;
    font-size: var(--font-size-base);
}

.nav-link::after {
    content: '';
    position: absolute;
    bottom: 0;
    left: 50%;
    width: 0;
    height: 2px;
    background: var(--color-accent);
    transition: width 0.15s ease;
    transform: translateX(-50%);
}

.nav-link:hover {
    color: var(--color-accent-hover) !important;
}

.nav-link:hover::after {
    width: 80%;
}

.nav-link.active {
    color: var(--color-text-primary) !important;
}

.nav-link.active::after {
    width: 80%;
}

/* 连接状态指示（仅在有 socket 的页面由 initConnectionStatus 点亮） */
.conn-status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: 12px;
    padding: 2px 10px;
    border: 1px solid var(--color-border);
    border-radius: 999px;
    font-size: var(--font-size-xs);
    color: var(--color-text-secondary);
}

.conn-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-neutral);
}

.conn-status--on .conn-dot { background: var(--color-success); }
.conn-status--off .conn-dot { background: var(--color-danger); }

/* Main content area：flex 子项，首页内部自己撑满，其它页滚动 */
.main-content {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    background: var(--color-bg-primary);
    position: relative;
}

/* Bottom status bar（28px，等宽字体读数） */
.workbench-statusbar {
    flex: 0 0 28px;
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 0 12px;
    background: var(--color-bg-secondary);
    border-top: 1px solid var(--color-border);
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    color: var(--color-text-secondary);
    white-space: nowrap;
    overflow: hidden;
}

.statusbar-item b {
    color: var(--color-text-primary);
    font-weight: 500;
}
```

注意：`!important` 计数——本段删除 `.navbar { background ... !important }` 1 处、`min-height` 改写；`.navbar-brand`/`.nav-link` 的 `!important` 数量与原段逐条对齐（background 1 + color 3 + padding 1，原样保留）。改完立刻跑 `pytest tests/test_css_contract.py::test_important_count_under_control` 确认不超 68；若实测变少，按棘轮规则把上界降为「实测 + 3」并登记。

- [ ] **Step 3: ui.js 新增 initConnectionStatus**

在 `window.showNotification = showToast;` 一行之前插入：

```js
    // ------------------------------------------------------ Connection status
    // 工具栏连接状态点。有 socket 的页面（首页 tasks.js）在 socket 建立后调用；
    // 无 socket 的页面不调用，指示器保持 hidden。
    function initConnectionStatus(socket) {
        const el = document.getElementById('connStatus');
        const text = document.getElementById('connStatusText');
        if (!el || !text || !socket) return;
        el.hidden = false;
        function apply(connected) {
            el.classList.toggle('conn-status--on', connected);
            el.classList.toggle('conn-status--off', !connected);
            text.textContent = connected ? '已连接' : '已断开';
        }
        apply(socket.connected);
        socket.on('connect', function () { apply(true); });
        socket.on('disconnect', function () { apply(false); });
    }
```

并在文件末尾导出处加一行：`window.initConnectionStatus = initConnectionStatus;`

- [ ] **Step 4: tasks.js 挂上连接指示**

`static/js/tasks.js` 的 `initTasks()` 里 `socket = io();` 之后加一行：

```js
    if (window.initConnectionStatus) window.initConnectionStatus(socket);
```

- [ ] **Step 5: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py -q`
Expected: 几何类断言（引用 `.main-content` padding-top 60px 的，约 :3861 附近）此时变红属预期——**先记录，不急着改**，Task 6 统一同步；其余全绿。

---

### Task 3: index.html 重构（地图全屏 + dock + bounds 浮层 + 状态栏）

**Files:**
- Modify: `templates/index.html`（整体重写 content block）
- Modify: `static/css/style.css`（Index page layout 段 :318-379 重写；Map Styles 段 :381-387 增补）
- Test: `tests/test_css_contract.py`（同步留到 Task 6）

**Interfaces:**
- Consumes: Task 2 的 `.workbench-statusbar` / `{% block statusbar %}`。
- Produces:
  - DOM：`.index-layout`（保留类名，语义变为 flex 行：地图 + dock）、`.index-map`（替代 `.index-left`）、`.index-right`（保留类名，语义变为 380px dock）、`.bounds-overlay`（`#boundsInfo` 的新容器类，替代 `alert alert-info`）、`.dock-reopen-handle`、`#dockCollapse` / `#dockReopen`。
  - 状态栏元素 id：`#statusCoords` `#statusZoom` `#statusSelection`（Task 4 的 map.js 写入目标）。
  - 表单字段、`#activeTasks` 卡片内部结构逐行不变。

- [ ] **Step 1: 重写 index.html**

要点（完整结构如下，表单字段部分——现文件 :47-176 的 `<div class="form-group-label">基础</div>` 到 `</button>` ——**逐行不变**搬入 dock）：

```html
{% extends "base.html" %}

{% block title %}地图下载 - Maps Downloader{% endblock %}

{% block content %}
<div class="index-layout">
    <!-- Map canvas -->
    <div class="index-map">
        <div id="map"></div>
        <div class="bounds-overlay" id="boundsInfo" role="status" aria-live="polite"></div>
        <button type="button" class="dock-reopen-handle" id="dockReopen" title="展开参数面板" hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"></polyline></svg>
        </button>
    </div>

    <!-- Right dock: parameters + active tasks -->
    <aside class="index-right" id="workbenchDock">
        <div class="card dock-panel">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5 style="margin: 0;">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 6px;">
                        <line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>
                        <line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>
                        <line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>
                        <line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>
                        <line x1="17" y1="16" x2="23" y2="16"></line>
                    </svg>
                    下载参数
                </h5>
                <button type="button" class="dock-collapse-btn" id="dockCollapse" title="收起参数面板">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"></polyline></svg>
                </button>
            </div>
            <div class="card-body">
                <form id="downloadForm">
                    <!-- 现 index.html :47-176 的全部字段原样搬入，唯一改动：
                         删除 <div class="alert alert-info" id="boundsInfo"> 整个块
                         （它已搬到地图上的 .bounds-overlay）。 -->
                </form>
            </div>
        </div>

        <div class="card dock-panel">
            <div class="card-header">
                <h5 style="margin: 0;">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 6px;">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                    </svg>
                    活动任务
                </h5>
            </div>
            <div class="card-body" id="activeTasks">
                <p class="text-muted">暂无活动任务</p>
            </div>
        </div>
    </aside>
</div>
{% endblock %}

{% block statusbar %}
<span class="statusbar-item" id="statusCoords">经度 — 纬度 —</span>
<span class="statusbar-item" id="statusZoom">z—</span>
<span class="statusbar-item" id="statusSelection">未选择区域</span>
{% endblock %}

{% block extra_js %}
<script src="{{ url_for('static', filename='js/map.js') }}"></script>
<script src="{{ url_for('static', filename='js/tasks.js') }}"></script>
<script>
    const config = {{ config|tojson }};
    initMap(config);
    initMapWorkbench();
    initDownloadTypeToggle();
    initTasks();
    initContourPreview();
</script>
{% endblock %}
```

`initMapWorkbench` 在 Task 4 定义。注意：`updateBoundsInfo()` 在 `initMap` 之前不会被调用；`#boundsInfo` 初始为空，由首次框选/删除填充——为保证首屏有提示，在 `initMapWorkbench()` 里调一次 `updateBoundsInfo()`（Task 4 代码含此调用）。

- [ ] **Step 2: 重写 style.css 的 Index page layout 段（:318-379）与 Map Styles 段**

`:318-379`（`.index-layout` / `.index-left` / `.index-right` 及其滚动条规则）整段替换为：

```css
/* Index page layout：地图全屏 + 右侧 dock */
.index-layout {
    height: 100%;
    display: flex;
    flex-direction: row;
    overflow: hidden;
    position: relative;
}

.index-map {
    flex: 1;
    min-width: 0;
    position: relative;
    overflow: hidden;
}

/* 右侧 dock：固定 380px，可收起（margin-right 负值滑出） */
.index-right {
    flex: 0 0 380px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 10px;
    overflow-y: auto;
    background: var(--color-bg-secondary);
    border-left: 1px solid var(--color-border);
    position: relative;
    transition: margin-right 0.2s ease;
}

.index-right.dock-collapsed {
    margin-right: -380px;
}

.dock-panel {
    flex: 0 0 auto;
}

.dock-collapse-btn,
.dock-reopen-handle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--ctl-h);
    height: var(--ctl-h);
    padding: 0;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--color-text-secondary);
    cursor: pointer;
    transition: background-color 0.15s ease, color 0.15s ease;
}

.dock-collapse-btn:hover,
.dock-reopen-handle:hover {
    background: var(--color-bg-tertiary);
    color: var(--color-text-primary);
}

/* 收起后地图右缘的展开把手 */
.dock-reopen-handle {
    position: absolute;
    top: 12px;
    right: 0;
    z-index: 1000;
    width: 24px;
    height: 48px;
    border-radius: var(--radius-sm) 0 0 var(--radius-sm);
    background: var(--color-bg-secondary);
    border-right: none;
}

/* 地图上的范围信息浮层（替代原表单底部 alert） */
.bounds-overlay {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 1000;
    max-width: min(420px, calc(100% - 60px));
    padding: 6px 10px;
    background: rgba(21, 23, 28, 0.92);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-md);
    color: var(--color-text-primary);
    font-size: var(--font-size-sm);
}

.index-right::-webkit-scrollbar {
    width: 8px;
}

.index-right::-webkit-scrollbar-track {
    background: var(--color-bg-secondary);
    border-radius: 4px;
}

.index-right::-webkit-scrollbar-thumb {
    background: var(--color-border-strong);
    border-radius: 4px;
    transition: background-color 0.15s ease;
}

.index-right::-webkit-scrollbar-thumb:hover {
    background: var(--color-text-muted);
}
```

`#map` 规则（:382-387）保留，追加一条让 z-index 语境明确（可选）；`.history-map` 不动。

⚠️ `.bounds-grid` / `.bounds-k` / `.bounds-v` / `.bounds-sr` 的既有规则不动——`updateBoundsInfo()` 的 innerHTML 结构不变，`test_bounds_readout_is_exactly_two_rows` 与 `test_bounds_labels_bind_to_the_right_coordinate` 继续绿。

- [ ] **Step 3: 烟测**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py -q`
Expected: 与 Task 2 Step 5 相同，几何/DOM 耦合断言变红记录待 Task 6；不允许出现新的**非**耦合类失败（如 var() 悬空、!important 超限、font-size !important）。

---

### Task 4: map.js 工作台行为（状态栏数据源 + dock 开合 + 比例尺）

**Files:**
- Modify: `static/js/map.js`（`initMap` 尾部、`updateBoundsInfo`、文件末尾新增函数）
- Test: `tests/test_css_contract.py`（同步留到 Task 6）

**Interfaces:**
- Consumes: Task 3 的 `#statusCoords` `#statusZoom` `#statusSelection` `#dockCollapse` `#dockReopen` `#workbenchDock`。
- Produces: `initMapWorkbench()` 全局函数（index.html init 块调用，须定义在 `initMap` 之后任意位置）。

- [ ] **Step 1: 新增 initMapWorkbench 与状态栏更新**

`static/js/map.js` 末尾追加：

```js
/**
 * 工作台行为：状态栏读数（鼠标经纬度 / 缩放级别 / 选区摘要）、
 * 比例尺控件、dock 收起展开。在 initMap 之后由页面 init 块调用。
 */
function initMapWorkbench() {
    if (!map) return;

    // 比例尺（左下，与状态栏视觉相邻）
    L.control.scale({ position: 'bottomleft', imperial: false }).addTo(map);

    const coordsEl = document.getElementById('statusCoords');
    const zoomEl = document.getElementById('statusZoom');

    // 缩放级别
    function updateZoom() {
        if (zoomEl) zoomEl.textContent = 'z' + map.getZoom();
    }
    map.on('zoomend', updateZoom);
    updateZoom();

    // 鼠标经纬度：rAF 节流，避免 mousemove 高频刷新
    if (coordsEl) {
        let pending = null;
        map.on('mousemove', function (e) {
            pending = e.latlng;
        });
        setInterval(function () {
            if (!pending) return;
            coordsEl.textContent =
                '经度 ' + pending.lng.toFixed(4) + '°  纬度 ' + pending.lat.toFixed(4) + '°';
            pending = null;
        }, 50);
        map.on('mouseout', function () {
            pending = null;
            coordsEl.textContent = '经度 — 纬度 —';
        });
    }

    // dock 收起 / 展开：margin 动画结束后 invalidateSize，300ms 兜底
    const dock = document.getElementById('workbenchDock');
    const collapseBtn = document.getElementById('dockCollapse');
    const reopenBtn = document.getElementById('dockReopen');
    function refreshMapSize() {
        let done = false;
        const once = function () {
            if (done) return;
            done = true;
            map.invalidateSize();
        };
        if (dock) dock.addEventListener('transitionend', once, { once: true });
        setTimeout(once, 300);
    }
    if (dock && collapseBtn) {
        collapseBtn.addEventListener('click', function () {
            dock.classList.add('dock-collapsed');
            if (reopenBtn) reopenBtn.hidden = false;
            refreshMapSize();
        });
    }
    if (dock && reopenBtn) {
        reopenBtn.addEventListener('click', function () {
            dock.classList.remove('dock-collapsed');
            reopenBtn.hidden = true;
            refreshMapSize();
        });
    }

    // 首屏填充「请在地图上框选下载区域」提示与状态栏选区摘要
    updateBoundsInfo();
}
```

- [ ] **Step 2: updateBoundsInfo 同步状态栏选区摘要**

`updateBoundsInfo()` 函数体两处分支末尾各加状态栏写入（函数开头取元素）。把函数签名下方改为：

```js
function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    const statusSel = document.getElementById('statusSelection');
    if (currentBounds) {
        const f = (v) => v.toFixed(5);
        boundsInfo.innerHTML = `...（原 bounds-grid 分支逐字不变）...`;
        if (statusSel) {
            const w = (currentBounds.east - currentBounds.west).toFixed(3);
            const h = (currentBounds.north - currentBounds.south).toFixed(3);
            statusSel.textContent = `已选区域 ${w}° × ${h}°`;
        }
    } else {
        boundsInfo.innerHTML = `...（原提示分支逐字不变）...`;
        if (statusSel) statusSel.textContent = '未选择区域';
    }
}
```

同时更新该函数的 docstring：容器从「表单底部 alert」改为「地图浮层 + 状态栏摘要」，结构（bounds-grid 两分支）不变的说明保留。

- [ ] **Step 3: 验证**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py -q`
Expected: 同 Task 3 Step 3；`test_bounds_labels_bind_to_the_right_coordinate`、`test_bounds_readout_is_exactly_two_rows` 必须仍绿（innerHTML 结构未动）。

---

### Task 5: 历史记录 / 配置页接入外壳

**Files:**
- Modify: `templates/history.html:6`、`templates/config.html:6`（内容容器）
- Modify: `static/css/style.css`（追加 `.page-content` 容器规则）
- Test: `tests/test_css_contract.py`

**Interfaces:**
- Consumes: Task 2 外壳。
- Produces: `.page-content`（居中限宽内容容器，max-width 1100px）。

- [ ] **Step 1: 模板接入**

`history.html:6` 的 `<div class="container-fluid" style="padding: 1rem; max-width: 1400px;">` → `<div class="page-content">`；`config.html:6` 的 `<div class="container-fluid" style="padding: 1rem; max-width: 1200px;">` → `<div class="page-content">`。两页其余内容不动；状态栏 block 不覆写（默认空）。

- [ ] **Step 2: style.css 追加容器规则**（放在 Index page layout 段之后）

```css
/* 非地图页的内容容器：居中限宽，与 dock 同一套间距 */
.page-content {
    max-width: 1100px;
    margin: 0 auto;
    padding: 16px;
}
```

- [ ] **Step 3: 验证**

Run: `.venv/bin/python -m pytest tests/test_css_contract.py -q`
Expected: 若有断言引用 history/config 的 `container-fluid` 内联样式或 max-width 值则按新值同步；其余同前。

---

### Task 6: 契约测试统一同步 + 全套 pytest 绿

**Files:**
- Modify: `tests/test_css_contract.py`（仅同步「有意视觉改动」类断言的期望值/结构模型）
- Test: `tests/`（全套）

**Interfaces:**
- Consumes: Task 1-5 的全部新 DOM/CSS。
- Produces: 全套 pytest 绿。

原则：这个文件的大量断言自带「⚠️ 给后续任务的说明：本表钉的是当时的值，不是禁止后续改动，同步更新即可」。本任务只做**同步**，不削弱断言强度（不删断言、不放宽阈值；!important 上界按棘轮规则只降不升）。

已知耦合点（Task 2-5 中实测变红的为准，以下按行号索引供定位）：

- `:154-155` `MERGED_FONT_SIZES` 的 `.navbar-brand` / `.nav-link` —— 字号未改，应仍绿。
- `:1453`、`:2426`、`:7630-7694`、`:8251` 附近的 DOM 结构模型（`index-layout`/`index-left`/`index-right`）：`index-left` → `index-map`；`index-right` 的背景断言从透明改为 `var(--color-bg-secondary)`；新增 `aside` 标签名时同步模型中的标签。
- `:3861-3920` 首屏几何模型：`.main-content` `padding-top: 60px` 已删除（改 0/无声明），navbar 高度 60 → 48，`.index-right` `padding: 1rem` → `10px`，按新值重算并更新公式注释。
- `:6531` `_REDUCED_MOTION_EXEMPT`：`.index-right::-webkit-scrollbar-thumb` 类名未变，应仍正确。
- `:8011` 附近 `#boundsInfo` 背景断言：容器类从 `.alert alert-info` 改为 `.bounds-overlay`，背景期望从 `rgba(59,130,246,0.1)` 改为 `rgba(21, 23, 28, 0.92)`；选择器写法同步。
- 响应式段：若 style.css 响应式规则（:1697-1739）引用 `.index-left`/旧 navbar 高度，一并改写并同步对应断言。

- [ ] **Step 1: 跑全套，逐个同步**

Run: `.venv/bin/python -m pytest tests/ -q`
对每个红点：读该断言 docstring 的「后续任务说明」→ 按新视觉值更新期望 → 重跑。禁止为了让测试变绿而回退 Task 1-5 的视觉效果（那是设计已确认的方向）；若某断言与新设计**本质冲突**（如「boundsInfo 必须是 alert」这类形态断言），把断言改写为等价强度的新形态（如「bounds-overlay 必须存在且含 bounds-grid」），并在断言 docstring 里登记改动理由。

- [ ] **Step 2: 全套绿后复跑确认**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿，无 skip 异常。

---

### Task 7: 截图验收

**Files:**
- Create: `docs/ui-baseline/workbench-2026-07/01-index.png`（1600x1000，含框选状态）
- Create: `docs/ui-baseline/workbench-2026-07/02-index-dock-collapsed.png`
- Create: `docs/ui-baseline/workbench-2026-07/03-history.png`
- Create: `docs/ui-baseline/workbench-2026-07/04-config.png`

- [ ] **Step 1: 启动应用并截图**

启动 `.venv/bin/python app.py`（后台），用项目已有的截图方式（参考 docs/ui-baseline/ 之前批次的产出方式；若无脚本则用 Playwright headless，视口 1600x1000）截取首页（先框选一块区域）、dock 收起态、历史页、配置页。

- [ ] **Step 2: 人工读图验收**

用 ReadMediaFile 逐张检查：工具栏 48px 不拥挤、状态栏读数正确、地图铺满、dock 收起后地图全宽且展开把手可见、bounds 浮层不遮控件、蓝色 accent 生效。发现问题回到对应 Task 修复。

- [ ] **Step 3: 最终全量回归**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 全绿。
