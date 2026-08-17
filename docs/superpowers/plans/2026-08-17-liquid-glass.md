# 液态玻璃全站改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把全站前端统一改造为苹果液态玻璃风格，四阶段渐进剥离 Bootstrap。

**Architecture:** 新增独立的液态令牌层（不触碰已有 `--color-glass-surface` 0.72 alpha 契约），令牌挂 `:root` 随 `data-bs-theme` 出亮暗两套、随 `data-accent` 染流光；自有组件类先作皮肤覆盖 Bootstrap 结构，逐页迁移后自研 modal、删除 Bootstrap。

**Tech Stack:** 纯 CSS（并入 `static/css/style.css` 末尾）+ Jinja 模板 + 少量原生 JS（modal）。禁止 CDN、禁止新依赖。

**Spec:** `docs/superpowers/specs/2026-08-17-liquid-glass-design.md`

## Global Constraints

- 玻璃样式**并入 `static/css/style.css` 末尾**，不新增样式表（`test_css_contract.py` 钉住 style.css 最后加载）。
- 组件规则**禁止**出现 `[data-bs-theme=...]` 属性选择器：亮暗差异只能写成 `:root[data-bs-theme="light"]` 块里的令牌翻转（`test_css_contract.py` 层叠模型否则判「已失效」）。亮暗区块按「亮色块选择器字样第一次出现」切分，**新暗色令牌必须写在该字样之前**，亮色翻转写进既有 light 块内。
- **不要改**已有 `--color-glass-surface: rgba(21,23,28,0.72)` / `--color-glass-border` 及其四个既有玻璃面（`.map-panel-triggers`、`.statusbar-pill`、`.map-overlay-chip`、`.map-search__field`）——`test_elevation_glass.py` 钉死。液态玻璃用**新的 `--liquid-*` 令牌**，自动发现机制匹配不到。
- 文字对比度 WCAG AA：正文 ≥ 4.5:1、图形元素 ≥ 3:1，由 `test_css_contract.py` / `test_elevation_glass.py` 兜底。液态玻璃低 alpha 底上放文字必须叠 scrim。
- vendor 版本号字面量、主题首帧脚本、`data-bs-theme="dark"` SSR 默认值一律不动。
- 模板 `<script>` 内禁止出现中文（`test_i18n.py` 裸中文扫描）；新增用户可见文案必须走 `t()` i18n，禁止新增裸文案。
- `prefers-reduced-motion: reduce` 时：禁用流光动画与折射。循环动画沿用既有 `!important` 压制块的写法。
- 提交信息：中文 + conventional 前缀，`git commit -F - <<'EOF'` 方式。
- 每 Task 验证基线：`uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`。

---

### Task 1: 液态令牌层 + 三档 `.tf-glass` 基类

**Files:**
- Modify: `static/css/style.css`（:root 令牌区约 line 38 之后追加暗色令牌；`:root[data-bs-theme="light"]` 块 line 427 起内追加亮色翻转；文件末尾追加组件规则）

**Interfaces:**
- Produces（后续所有 Task 依赖的令牌与类）:
  - 令牌：`--liquid-{1,2,3}-bg`、`--liquid-{1,2,3}-blur`、`--liquid-saturate`、`--liquid-rim-strong`、`--liquid-rim-soft`、`--liquid-stroke`、`--liquid-shadow`、`--liquid-sheen`、`--liquid-scrim`、`--liquid-radius-panel`、`--liquid-radius-control`、`--liquid-motion`
  - 类：`.tf-glass`（基类）、`.tf-glass--1` / `.tf-glass--2` / `.tf-glass--3`（档位，默认 --2）

- [ ] **Step 1: 暗色令牌**

在 `:root` 的 `--color-glass-border` 定义之后（约 line 39，必须在「组件级颜色令牌」注释块之前）追加：

```css
    /* 液态玻璃令牌(2026-08-17 设计 §1)。独立于 --color-glass-surface 体系:
       旧令牌 alpha 0.72 是停靠浮层的不透明下限(test_elevation_glass.py 钉死),
       液态玻璃是「厚玻璃」质感,低 alpha + 边缘高光 + 流光,文字可读性靠
       --liquid-scrim 叠层兜住。亮色翻转集中在下方亮色令牌块。 */
    --liquid-1-bg:   rgba(255,255,255,0.06);
    --liquid-1-blur: 12px;
    --liquid-2-bg:   rgba(255,255,255,0.10);
    --liquid-2-blur: 20px;
    --liquid-3-bg:   rgba(255,255,255,0.14);
    --liquid-3-blur: 28px;
    --liquid-saturate: 160%;
    --liquid-rim-strong: rgba(255,255,255,0.55);
    --liquid-rim-soft:   rgba(255,255,255,0.22);
    --liquid-stroke:     rgba(255,255,255,0.16);
    --liquid-shadow: 0 8px 32px rgba(0,0,0,0.35);
    --liquid-sheen: linear-gradient(135deg,
        rgba(255,255,255,0.28), rgba(255,255,255,0.06) 30%, transparent 60%);
    /* 文字可读性叠层:玻璃 alpha 太低,白字压不住亮色地图,叠一层暗化 */
    --liquid-scrim: linear-gradient(rgba(10,12,16,0.42), rgba(10,12,16,0.42));
    --liquid-radius-panel:   16px;
    --liquid-radius-control: 12px;
    --liquid-motion: 240ms cubic-bezier(0.32, 0.72, 0, 1);
```

- [ ] **Step 2: 亮色翻转**

在 `:root[data-bs-theme="light"]` 块内（其既有令牌之后）追加：

```css
    --liquid-1-bg:   rgba(255,255,255,0.42);
    --liquid-2-bg:   rgba(255,255,255,0.55);
    --liquid-3-bg:   rgba(255,255,255,0.66);
    --liquid-rim-strong: rgba(255,255,255,0.90);
    --liquid-rim-soft:   rgba(255,255,255,0.50);
    --liquid-stroke:     rgba(15,23,42,0.12);
    --liquid-shadow: 0 8px 24px rgba(15,23,42,0.18);
    --liquid-sheen: linear-gradient(135deg,
        rgba(255,255,255,0.55), rgba(255,255,255,0.12) 30%, transparent 60%);
    --liquid-scrim: linear-gradient(rgba(255,255,255,0.30), rgba(255,255,255,0.30));
```

- [ ] **Step 3: `.tf-glass` 基类与三档**

追加到 style.css **文件末尾**：

```css
/* ══ 液态玻璃(2026-08-17 设计) ══════════════════════════════════════
   .tf-glass 基类 + --1/--2/--3 档位。结构:底色与 scrim 在 ::before(最底),
   流光在 ::after(内容之上),边缘高光走 inset box-shadow。isolation:isolate
   保证负 z-index 伪元素不外泄。 */
.tf-glass {
    --liquid-blur: var(--liquid-2-blur);
    --liquid-bg:   var(--liquid-2-bg);
    position: relative;
    isolation: isolate;
    border: 1px solid var(--liquid-stroke);
    border-radius: var(--liquid-radius-panel);
    box-shadow: var(--liquid-shadow),
        inset 0 1px 1px var(--liquid-rim-strong),
        inset 0 -1px 1px var(--liquid-rim-soft),
        inset 1px 0 1px var(--liquid-rim-soft),
        inset -1px 0 1px var(--liquid-rim-soft);
    backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
    -webkit-backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
    transition: box-shadow var(--liquid-motion), border-color var(--liquid-motion);
}
.tf-glass::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: -2;
    border-radius: inherit;
    background: var(--liquid-scrim), var(--liquid-bg);
}
.tf-glass::after {
    content: "";
    position: absolute;
    inset: 0;
    z-index: -1;
    border-radius: inherit;
    background: var(--liquid-sheen);
    mix-blend-mode: screen;
    pointer-events: none;
}
.tf-glass--1 { --liquid-blur: var(--liquid-1-blur); --liquid-bg: var(--liquid-1-bg); }
.tf-glass--3 { --liquid-blur: var(--liquid-3-blur); --liquid-bg: var(--liquid-3-bg); }

/* 降级:不支持 backdrop-filter 的浏览器给不透明底 */
@supports not (backdrop-filter: blur(1px)) {
    .tf-glass { background: var(--color-bg-elevated); }
    .tf-glass::before, .tf-glass::after { content: none; }
}

/* 减少动态:关流光(scrim 与边缘高光是静态可读性手段,保留) */
@media (prefers-reduced-motion: reduce) {
    .tf-glass { transition: none !important; }
    .tf-glass::after { content: none !important; }
}
```

- [ ] **Step 4: 跑契约测试基线**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`
Expected: 全绿。新令牌与类不触发任何既有断言（自动发现机制只认旧的两个玻璃令牌）。

- [ ] **Step 5: Commit**

```bash
git add static/css/style.css
git commit -F - <<'EOF'
feat(ui): 液态玻璃令牌层与三档 .tf-glass 基类

独立于既有 --color-glass-surface 体系，亮暗双套参数，含降级与减少动态处理。
EOF
```

---

### Task 2: config/history 页等高线环境背景

**Files:**
- Modify: `static/css/style.css`（末尾追加）
- Modify: `templates/config.html`、`templates/history.html`（最外层容器加类）

**Interfaces:**
- Produces: `.page-ambient`（套在非地图页最外层 wrapper 上，提供玻璃可采样的环境背景）

- [ ] **Step 1: 环境背景样式**

追加到 style.css 末尾。等高线用内联 SVG data-URI（极淡描边），叠主题联动渐变：

```css
/* 非地图页环境背景:玻璃需要丰富背景可采样(设计 §3)。
   渐变取页面底与强调色的极淡混合,等高线纹理描边用边框令牌,亮暗自动跟随。 */
.page-ambient {
    background:
        url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='480' height='480'%3E%3Cg fill='none' stroke='%237dd3fc' stroke-opacity='0.05'%3E%3Cpath d='M-20 120 C120 60 240 180 500 100'/%3E%3Cpath d='M-20 200 C140 140 260 260 500 180'/%3E%3Cpath d='M-20 280 C120 220 260 340 500 260'/%3E%3Cpath d='M-20 360 C140 300 240 420 500 340'/%3E%3Cpath d='M-20 440 C120 380 260 500 500 420'/%3E%3C/g%3E%3C/svg%3E"),
        radial-gradient(1200px 600px at 80% -10%, var(--color-accent-muted), transparent 60%),
        radial-gradient(900px 500px at 10% 110%, var(--color-accent-muted), transparent 55%),
        var(--color-bg-primary);
    background-attachment: fixed;
    min-height: 100vh;
}
```

- [ ] **Step 2: 模板挂类**

`templates/config.html` 与 `templates/history.html`：找到各自 `{% block %}` 内最外层容器（当前应是 `<div class="container...">` 一类），在 class 列表**最前**加 `page-ambient`。不改任何其他属性与结构。

- [ ] **Step 3: 浏览器验证**

起服务（`uv run python -m src.app` 或项目既有入口，端口 5000），浏览器打开 `/config` 与 `/history`，亮/暗主题各截图：等高线纹理与径向辉光可见但不抢眼。

- [ ] **Step 4: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`
Expected: 全绿。

```bash
git add static/css/style.css templates/config.html templates/history.html
git commit -F - <<'EOF'
feat(ui): 非地图页补等高线环境背景

液态玻璃的采样底，亮暗随令牌自动翻转。
EOF
```

---

### Task 3: 地图页悬浮 chrome 上玻璃 + `.tf-btn`

**Files:**
- Modify: `static/css/style.css`（末尾追加）
- Modify: `templates/index.html`（悬浮元素加 `.tf-glass` 类）

**Interfaces:**
- Consumes: Task 1 的 `.tf-glass` / `--liquid-*`
- Produces: `.tf-btn`（玻璃胶囊按钮，含 `.tf-btn--accent` 变体），后续 config/history 迁移复用

- [ ] **Step 1: 按钮组件**

追加到 style.css 末尾：

```css
/* 液态玻璃按钮:胶囊形,glass-1 档位,hover 抬升边缘高光 */
.tf-btn {
    --liquid-blur: var(--liquid-1-blur);
    --liquid-bg: var(--liquid-1-bg);
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 16px;
    border: 1px solid var(--liquid-stroke);
    border-radius: 999px;
    color: var(--color-text-primary);
    font: inherit;
    cursor: pointer;
    position: relative;
    isolation: isolate;
    backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
    -webkit-backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
    box-shadow: inset 0 1px 1px var(--liquid-rim-strong),
        inset 0 -1px 1px var(--liquid-rim-soft);
    transition: transform var(--liquid-motion), box-shadow var(--liquid-motion);
}
.tf-btn::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: -1;
    border-radius: inherit;
    background: var(--liquid-scrim), var(--liquid-bg);
}
.tf-btn:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: var(--liquid-shadow),
        inset 0 1px 1px var(--liquid-rim-strong),
        inset 0 -1px 1px var(--liquid-rim-soft);
}
.tf-btn:disabled { opacity: 0.5; cursor: default; }
.tf-btn:focus-visible {
    outline: 2px solid var(--color-accent);
    outline-offset: 2px;
}
.tf-btn--accent::before {
    background: linear-gradient(rgba(0,0,0,0.18), rgba(0,0,0,0.18)), var(--color-accent);
}
.tf-btn--accent { color: var(--color-on-accent); }
@supports not (backdrop-filter: blur(1px)) {
    .tf-btn { background: var(--color-bg-elevated); }
    .tf-btn::before { content: none; }
}
```

- [ ] **Step 2: 地图页悬浮元素上玻璃**

`templates/index.html` 中给既有四个玻璃面（`.map-panel-triggers`、`.statusbar-pill`、`.map-overlay-chip`、`.map-search__field`）的元素**追加** `.tf-glass tf-glass--1` 类（旧类保留，本阶段是叠加皮肤）。左右滑出面板（`.map-panel` 或等价容器）追加 `.tf-glass`。

注意：这四个元素已有旧玻璃规则；叠加后视觉上是液态质感压旧质感（style.css 末尾的规则后胜）。**不要删旧规则**，清退阶段统一处理。

- [ ] **Step 3: 浏览器验证**

打开 `/`：四个悬浮 chrome 呈现厚玻璃质感（边缘高光 + 流光 + 背景模糊），地图上拖动时玻璃下内容模糊跟随；切亮色主题玻璃变乳白质感。截图存档对比。

- [ ] **Step 4: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`
Expected: 全绿。若 `test_every_text_context_meets_wcag_aa` 因 `.tf-btn` 颜色规则变红，检查是否漏了 scrim 叠层，不得通过放宽测试过关。

```bash
git add static/css/style.css templates/index.html
git commit -F - <<'EOF'
feat(ui): 地图页悬浮 chrome 上液态玻璃，新增 .tf-btn 玻璃按钮

旧玻璃类暂保留作叠加皮肤，清退阶段统一删。
EOF
```

---

### Task 4: 表单与卡片组件（`.tf-field` / `.tf-card`）

**Files:**
- Modify: `static/css/style.css`（末尾追加）

**Interfaces:**
- Consumes: Task 1 令牌
- Produces: `.tf-field`（input/select/textarea 玻璃控件）、`.tf-card`（glass-2 卡片），Task 5/6 直接消费

- [ ] **Step 1: 组件 CSS**

追加到 style.css 末尾：

```css
/* 液态玻璃表单控件:glass-1 底,聚焦时描边染强调色 */
.tf-field {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--color-control-border);
    border-radius: var(--liquid-radius-control);
    color: var(--color-text-primary);
    font: inherit;
    background: var(--liquid-1-bg);
    backdrop-filter: blur(var(--liquid-1-blur)) saturate(var(--liquid-saturate));
    -webkit-backdrop-filter: blur(var(--liquid-1-blur)) saturate(var(--liquid-saturate));
    box-shadow: inset 0 1px 1px var(--liquid-rim-soft);
    transition: border-color var(--liquid-motion), box-shadow var(--liquid-motion);
}
.tf-field:focus {
    outline: none;
    border-color: var(--color-accent);
    box-shadow: inset 0 1px 1px var(--liquid-rim-soft),
        0 0 0 3px var(--color-accent-muted);
}
.tf-field::placeholder { color: var(--color-text-secondary); }

/* 液态玻璃卡片:glass-2 档位 */
.tf-card {
    --liquid-blur: var(--liquid-2-blur);
    --liquid-bg: var(--liquid-2-bg);
    position: relative;
    isolation: isolate;
    border: 1px solid var(--liquid-stroke);
    border-radius: var(--liquid-radius-panel);
    box-shadow: var(--liquid-shadow),
        inset 0 1px 1px var(--liquid-rim-strong),
        inset 0 -1px 1px var(--liquid-rim-soft);
    backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
    -webkit-backdrop-filter: blur(var(--liquid-blur)) saturate(var(--liquid-saturate));
}
.tf-card::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: -2;
    border-radius: inherit;
    background: var(--liquid-scrim), var(--liquid-bg);
}
@supports not (backdrop-filter: blur(1px)) {
    .tf-field, .tf-card { background: var(--color-bg-elevated); }
    .tf-card::before { content: none; }
}
```

- [ ] **Step 2: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`
Expected: 全绿（本 Task 无模板改动，组件待 Task 5/6 消费）。

```bash
git add static/css/style.css
git commit -F - <<'EOF'
feat(ui): 液态玻璃表单控件 .tf-field 与卡片 .tf-card
EOF
```

---

### Task 5: config 页迁移

**Files:**
- Modify: `templates/_config_content.html`（467 行，表单主战场）
- Modify: `static/css/style.css`（如有覆盖补丁，末尾追加）

**Interfaces:**
- Consumes: `.tf-btn`、`.tf-field`、`.tf-card`、`.page-ambient`

- [ ] **Step 1: 逐元素替换类**

映射规则（对 `_config_content.html` 全文执行）：
- `form-control` / `form-select` → 追加 `tf-field`（**保留**原类，清退阶段再删；select 的下拉箭头仍由 Bootstrap 背景图提供，见 style.css line 3647 附近注释，不动）
- `btn btn-primary` → 追加 `tf-btn tf-btn--accent`；`btn btn-secondary` / `btn-outline-*` → 追加 `tf-btn`
- `card` → 追加 `tf-card`
- `form-check-input`（checkbox/radio 开关）**不动**：原生控件样式由既有规则覆盖，玻璃化收益低、可访问性风险高
- `row` / `col-md-*` / `mb-3` 等布局工具类**本阶段不动**

- [ ] **Step 2: 浏览器验证**

`/config` 亮/暗主题截图：卡片浮在等高线背景上呈玻璃质感，输入框聚焦有强调色描边光晕，按钮胶囊形。填写并保存一次配置，确认表单功能无回归。

- [ ] **Step 3: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`
Expected: 全绿。

```bash
git add templates/_config_content.html static/css/style.css
git commit -F - <<'EOF'
feat(ui): config 页迁移液态玻璃组件

原 Bootstrap 类保留作过渡，布局工具类留待清退阶段。
EOF
```

---

### Task 6: history 页迁移（含性能预算）

**Files:**
- Modify: `templates/_history_content.html`、`templates/history.html`
- Modify: `static/css/style.css`（末尾追加列表补丁）

**Interfaces:**
- Consumes: `.tf-btn`、`.tf-card`

- [ ] **Step 1: 迁移（遵守性能预算）**

- 列表**容器壳**追加 `.tf-card`；`.task-row` 行**保持实色**（滚动区域禁玻璃，设计 §3 性能预算）
- 页内 `btn` → 追加 `tf-btn`；筛选控件 `form-select`/`form-control` → 追加 `tf-field`
- 任务详情 modal（`taskDetailModal`）的 `.modal-content` 追加 `.tf-glass tf-glass--3`（折射在 Task 7 统一上线）

- [ ] **Step 2: 浏览器验证**

`/history` 亮/暗截图：容器玻璃、行实色分明；滚动长列表无卡顿（Chrome DevTools Performance 抽查一屏滚动不掉帧）。

- [ ] **Step 3: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`

```bash
git add templates/_history_content.html templates/history.html static/css/style.css
git commit -F - <<'EOF'
feat(ui): history 页迁移液态玻璃，长列表行保持实色守性能预算
EOF
```

---

### Task 7: glass-3 折射滤镜上线（Chromium 增强）

**Files:**
- Modify: `templates/base.html`（`</body>` 前注入 SVG 滤镜定义）
- Modify: `static/css/style.css`（末尾追加 @supports 块）
- Modify: `templates/_command_palette.html`（命令面板容器追加 `.tf-glass tf-glass--3`）

**Interfaces:**
- Consumes: `.tf-glass--3`

- [ ] **Step 1: SVG 滤镜定义**

`templates/base.html` 在 `</body>` 之前追加（SVG 为标记非 script，不受裸中文扫描约束；不写任何文本内容）：

```html
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <filter id="tf-liquid-refraction" x="0" y="0" width="100%" height="100%">
    <feTurbulence type="fractalNoise" baseFrequency="0.008 0.008" numOctaves="2" seed="4" result="noise"/>
    <feGaussianBlur in="noise" stdDeviation="2" result="blurred"/>
    <feDisplacementMap in="SourceGraphic" in2="blurred" scale="40" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
</svg>
```

- [ ] **Step 2: @supports 折射块**

style.css 末尾追加：

```css
/* 折射增强(仅 Chromium):glass-3 的背景扭曲。@supports 探测失败时
   保持 Task 1 的普通模糊,天然降级。 */
@supports (backdrop-filter: url("#tf-liquid-refraction")) {
    .tf-glass--3 {
        backdrop-filter: blur(2px) url("#tf-liquid-refraction") saturate(var(--liquid-saturate));
        -webkit-backdrop-filter: blur(2px) url("#tf-liquid-refraction") saturate(var(--liquid-saturate));
    }
}
@media (prefers-reduced-motion: reduce) {
    .tf-glass--3 {
        backdrop-filter: blur(var(--liquid-3-blur)) saturate(var(--liquid-saturate)) !important;
        -webkit-backdrop-filter: blur(var(--liquid-3-blur)) saturate(var(--liquid-saturate)) !important;
    }
}
```

- [ ] **Step 3: 命令面板上 glass-3**

`_command_palette.html` 容器追加 `.tf-glass tf-glass--3`。

- [ ] **Step 4: 浏览器验证**

Chromium：打开任务详情 modal 与命令面板，玻璃下背景可见扭曲折射；Firefox/降级路径只模糊不扭曲。亮/暗各截图。

- [ ] **Step 5: 契约测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`

```bash
git add templates/base.html templates/_command_palette.html static/css/style.css
git commit -F - <<'EOF'
feat(ui): glass-3 折射滤镜上线，Chromium 增强，@supports 天然降级
EOF
```

---

### Task 8: 自研 modal 替换 bootstrap.Modal

**Files:**
- Create: `static/js/modal.js`
- Modify: `static/js/history.js:659-661`、`static/js/path_browser.js:117-119`
- Modify: `templates/base.html`（script 标签引入 modal.js）

**Interfaces:**
- Produces: `window.TfModal = { getOrCreateInstance(el) -> { show(), hide() } }`（与 bootstrap.Modal 同形接口，调用处零改动语义）

- [ ] **Step 1: modal.js**

```javascript
/* 极简 modal:焦点陷阱 / ESC / 遮罩点击。接口与 bootstrap.Modal 同形。 */
(function () {
    'use strict';
    const instances = new WeakMap();

    class TfModalInstance {
        constructor(el) {
            this.el = el;
            this._onKeydown = (e) => {
                if (e.key === 'Escape') this.hide();
                if (e.key === 'Tab') this._trapFocus(e);
            };
            this._onBackdrop = (e) => { if (e.target === this.el) this.hide(); };
        }
        show() {
            this._prevFocus = document.activeElement;
            this.el.classList.add('show');
            this.el.style.display = 'block';
            this.el.removeAttribute('aria-hidden');
            document.body.classList.add('modal-open');
            const bd = document.createElement('div');
            bd.className = 'modal-backdrop fade show';
            document.body.appendChild(bd);
            this._backdrop = bd;
            document.addEventListener('keydown', this._onKeydown);
            this.el.addEventListener('mousedown', this._onBackdrop);
            const first = this.el.querySelector('[autofocus], button, input, select, textarea, [tabindex]');
            if (first) first.focus();
        }
        hide() {
            this.el.classList.remove('show');
            this.el.style.display = 'none';
            this.el.setAttribute('aria-hidden', 'true');
            document.body.classList.remove('modal-open');
            if (this._backdrop) { this._backdrop.remove(); this._backdrop = null; }
            document.removeEventListener('keydown', this._onKeydown);
            this.el.removeEventListener('mousedown', this._onBackdrop);
            if (this._prevFocus) this._prevFocus.focus();
        }
        _trapFocus(e) {
            const items = this.el.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])');
            if (!items.length) return;
            const first = items[0], last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
            else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
        }
    }

    window.TfModal = {
        getOrCreateInstance(el) {
            if (!instances.has(el)) instances.set(el, new TfModalInstance(el));
            return instances.get(el);
        }
    };
})();
```

- [ ] **Step 2: 替换调用处**

- `history.js:661`：`bootstrap.Modal.getOrCreateInstance(...)` → `TfModal.getOrCreateInstance(...)`
- `path_browser.js:118`：同样替换
- `base.html` 在既有 script 序列中引入 `modal.js`（放在 history.js/path_browser.js 之前；保持既有加载顺序约定）

- [ ] **Step 3: 浏览器验证**

打开任务详情 modal 与路径浏览器：打开/ESC 关/遮罩点关/Tab 焦点循环全部正常；重复打开不叠遮罩（getOrCreateInstance 语义保持）。

- [ ] **Step 4: 测试 + Commit**

Run: `uv run pytest tests/test_css_contract.py tests/test_elevation_glass.py tests/test_i18n.py -x -q`；若仓库有 JS 相关测试一并跑。

```bash
git add static/js/modal.js static/js/history.js static/js/path_browser.js templates/base.html
git commit -F - <<'EOF'
feat(ui): 自研 TfModal 替换仅有的两处 bootstrap.Modal

焦点陷阱/ESC/遮罩关闭齐备，接口同形调用处零语义变更。
EOF
```

---

### Task 9: 清退 Bootstrap

**Files:**
- Modify: `templates/base.html`（删 bootstrap.min.css 与 bootstrap JS 的 link/script）
- Modify: `templates/*.html`（删过渡残留 Bootstrap 类）
- Modify: `static/css/style.css`（补删类后失保的样式：栅格、间距、modal 定位、表单布局）
- Modify: `tests/test_css_contract.py`（移除/改写钉 Bootstrap 版本号与行为的断言——**有意变更**）
- Delete: `static/vendor/bootstrap/`（整个目录）

**Interfaces:**
- Consumes: 前 8 个 Task 的全部组件

- [ ] **Step 1: 盘点残留**

`grep -oE 'class="[^"]*"' templates/*.html` 统计仍存在的 Bootstrap 类（`btn`、`form-*`、`row`、`col-*`、`mb-*`、`modal`、`fade`、`card` 等），逐一决定：删类、换自有类、或把缺失样式补进 style.css（栅格用 flex/grid 自写，间距工具类换成自有 utility 或直接进组件样式）。

- [ ] **Step 2: 删引用 + 删 vendor + 改契约测试**

- base.html 删两行 Bootstrap 引用
- 删 `static/vendor/bootstrap/` 目录
- `test_css_contract.py` 中钉 Bootstrap 版本号字面量与 Bootstrap 行为的断言改写为钉自有组件（或直接删除已无对应物的断言，提交信息里逐条登记）

- [ ] **Step 3: 全页面浏览器回归**

`/`、`/config`、`/history` 亮/暗主题全截图；modal、表单、布局、按钮逐项过一遍；确认控制台无 404（vendor 删除无漏引）。

- [ ] **Step 4: 全量测试 + Commit**

Run: `uv run pytest tests/ -x -q`
Expected: 全绿。

```bash
git add -A
git commit -F - <<'EOF'
refactor(ui): 清退 Bootstrap，液态玻璃组件全面接管

删除 bootstrap.min.css/JS 与 vendor 目录；栅格与间距样式自有化；
契约测试中 Bootstrap 相关断言逐条改写登记。
EOF
```

---

## Self-Review 记录

- Spec 覆盖：令牌层(T1)、折射(T7)、页面处理(T2/T3/T5/T6)、剥离路径(T3-T9)、契约变更(T9)、浏览器验证(每 Task)——全覆盖。
- 占位符：无 TBD/TODO；所有 CSS/JS/模板改动均给出 verbatim 代码或逐条映射规则。
- 类型一致：`.tf-glass`/`.tf-glass--{1,2,3}`、`.tf-btn`/`.tf-btn--accent`、`.tf-field`、`.tf-card`、`.page-ambient`、`TfModal.getOrCreateInstance` 在所有 Task 间口径一致。
- 已知风险：T9 是最大的一个 Task，执行时若残留类盘点结果超出预期，允许把「栅格自有化」拆成独立 Task（执行期决策，不阻塞本计划生效）。
