# TerraForge 液态玻璃（Liquid Glass）全站改造设计

日期：2026-08-17
状态：已获用户认可，待写实施计划

## 目标

把全站前端（地图页 index、config、history 及全部弹窗面板）统一改造为苹果 WWDC25 液态玻璃风格。纯视觉/前端架构改造，不碰后端 API 与 JS 业务逻辑。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 范围 | 全部页面统一改造 |
| 折射 | 跨浏览器核心打底 + Chromium 折射增强（`@supports` 门控） |
| 主题系统 | 完整保留 dark/light/system + `tf-accent` 强调色预设，玻璃令牌出亮暗两套参数 |
| Bootstrap | 逐步剥离，最终删除 |
| 实施方案 | 方案 A：令牌层 → 组件皮肤 → 自研 modal → 删 Bootstrap，四阶段渐进替换 |

## 技术依据

液态玻璃 Web 实现分两层：

1. **跨浏览器核心**（纯 CSS，所有现代浏览器可用）：`backdrop-filter: blur + saturate + brightness`、半透明底色、四边 inset 边缘高光（顶强侧弱）、1px 半透描边、投影、伪元素斜向流光（sheen）。
2. **真实折射**（背景透过玻璃扭曲）：SVG `feTurbulence + feDisplacementMap` 作为 `backdrop-filter: url(#filter)` 输入。**仅 Chromium 支持**，Safari/Firefox 必须 `@supports` 降级到核心层。

硬约束：

- 玻璃必须有丰富背景可采样，平背景上效果不可见。地图页 Cesium 3D 地球是天然主场；config/history 平背景页必须补环境背景。
- 大面积、多数量、滚动区域的 backdrop-filter 耗性能，须设预算。

## 现状盘点（改造前事实）

- Flask + Jinja 服务端渲染：`templates/base.html`（骨架）+ `index.html`（761 行）/ `config.html` / `history.html` + 局部模板。
- 5547 行手写 `static/css/style.css`；Bootstrap 5.3.0、Cesium 1.143.0、Vue 3.5.13、字体全部本地 vendored，**禁止 CDN**（离线打包桌面工具）。
- 主题系统：`tf-theme`（dark/light/system，缺省 dark）+ `tf-accent` 强调色预设，base.html 首帧前同步脚本；`html data-bs-theme="dark"` 字面量是无 JS 时的 SSR 默认值。
- Bootstrap 使用面：JS 仅 2 处 `bootstrap.Modal`（`history.js` 任务详情、`path_browser.js` 路径浏览器）；CSS 类为表单约 110 处、按钮约 40 处、栅格/间距工具类约 150 处（集中 config 页）、卡片/模态少量。无 dropdown/tooltip/popover 等 JS 组件。
- `tests/test_css_contract.py` 钉住：vendor 版本号字面量（正则从模板源码抠）、`style.css` 必须最后加载、`data-bs-theme` SSR 默认值。

## 设计

### 1. 令牌体系（阶段一交付物）

玻璃按层级深度分三档令牌，全部挂进现有 `:root`，随 `data-bs-theme` 出亮暗两套参数，随 `data-accent` 染流光色：

| 档位 | 用途 | 暗色参数（亮色另有一套） |
|---|---|---|
| `glass-1` | 悬浮工具栏、按钮、chip | 底 `rgba(255,255,255,0.06)`，blur 12px，saturate 160% |
| `glass-2` | 面板、卡片、下拉 | 底 `rgba(255,255,255,0.10)`，blur 20px |
| `glass-3` | 模态框、命令面板 | 底 `rgba(255,255,255,0.14)`，blur 28px + 折射增强 |

每档统一带：1px 半透描边、四边 inset 边缘高光（顶部强、其余三边弱）、投影、伪元素斜向流光。

圆角体系：面板 16px、控件 12px、按钮胶囊形（pill）。

动效令牌：回弹缓动 `cubic-bezier(0.32, 0.72, 0, 1)`，时长 200–280ms。

### 2. 折射增强（Chromium only）

SVG `feTurbulence + feDisplacementMap` 滤镜，`@supports (backdrop-filter: url(#…))` 门控，只加在 `glass-3`（模态/命令面板）——数量少、面积小，性能可控。`prefers-reduced-motion` 时全局禁用折射与流光动画。

### 3. 页面处理

- **地图页**：顶部工具栏、两侧面板、命令面板浮于 3D 地球之上，逐级使用 glass-1/2/3。Cesium 自带 widget（导航罗盘等）做玻璃覆写。
- **config / history 页**：补**环境背景**——主题联动的多层渐变 + 极淡地形等高线纹理（SVG），让玻璃有背景可采样。此为全站**签名元素**：管理页从平板灰底变为"等高线地形上浮着玻璃"。
- **性能预算**：同屏 backdrop-filter 表面 ≤ 8 个；history 长列表行与滚动区域不用玻璃（实色 + 细描边），只在列表容器壳上使用。

### 4. 组件与 Bootstrap 剥离路径

自有组件类（`.tf-glass`、`.tf-btn`、`.tf-field`、`.tf-modal` 等）先作为皮肤覆盖 Bootstrap 结构类，页面逐个迁移；随后自研 modal（焦点陷阱、ESC、遮罩点击关闭）替换仅有的 2 处 `bootstrap.Modal`；最后删除 `bootstrap.min.css` 与残余类。迁移时逐处核对 JS 里操作 DOM class 的逻辑（`ui.js` 812 行、`panels.js` 416 行等）。

### 5. 契约与测试变更（有意为之）

- 玻璃样式**并入 `style.css` 末尾**，不新增样式表，保住"style.css 最后加载"契约不动。
- vendor 版本号字面量、主题首帧脚本、`data-bs-theme="dark"` SSR 默认值等其余契约全部保留。
- `<script>` 内不写中文（`test_i18n.py` 裸中文扫描）。
- 纯样式变更，不加行为测试；沿用现有契约测试。
- 验证方式：起 Flask :5000，浏览器驱动地图页 + config 页，亮/暗主题各截图确认玻璃质感、可读性、降级行为。

## 实施阶段（方案 A）

1. **令牌层**：`:root` 玻璃令牌（亮暗两套）+ `.tf-glass` 三档基类 + 等高线环境背景，并入 style.css 末尾。
2. **组件皮肤**：`.tf-btn` / `.tf-field` / `.tf-card` 等覆盖 Bootstrap 结构类，地图页先行。
3. **逐页迁移**：config、history 页迁移到自有组件；折射滤镜上线。
4. **清退 Bootstrap**：自研 modal 替换 2 处 `bootstrap.Modal`，删 `bootstrap.min.css` 与残余类，更新相关契约测试。

## 非目标

- 不改后端 API、不改 JS 业务逻辑（DOM class 核对除外）。
- 不引入任何 CDN / 新第三方依赖。
- 不动主题首帧脚本机制与 `tf-theme`/`tf-accent` 值口径。
