# TerraForge 前端 UI/UX 改版设计:借鉴 GeoLibre,保留自身特色

日期:2026-08-11
状态:待评审
参考:https://web.geolibre.app/(opengeos/GeoLibre,React + MapLibre 云原生 GIS)

## 1. 背景与目标

TerraForge 现行前端是 2026-07-27 UI 大评审(`docs/reviews/2026-07-27-ui-review.md`)确立的「暗色专业 GIS 工具」体系:全屏 Cesium 地图 + 浮动胶囊控件 + 右侧滑出面板,无顶栏、无常驻侧栏。本次改版参考 GeoLibre 的 UI/UX,目标是**吸收其成熟的视觉层级与交互模式,但不动摇 TerraForge 的产品身份**。

### GeoLibre 值得借鉴的(经源码核实)

来源:`/tmp/geolibre-src/apps/geolibre-desktop/src`(桌面端源码,与 web 端同一外壳)、其构建产物 CSS 令牌。

1. **「玻璃」视觉语言**:浮在地图上的元素一律半透明 + `backdrop-blur(12px)` + `saturate(140%)`;停靠在边缘的 chrome 一律不透明。一条规则统一所有浮层。
2. **暗色 elevation 阶梯**:背景亮度逐级抬升(background 9% → card 12% → popover 15% → muted 20% → accent 22%),「越高越亮」;阴影分语义档位,暗色下阴影用高透明纯黑。
3. **命令面板**:Ctrl/Cmd+K 唤起,单一命令注册表同时驱动面板、全局快捷键、`?` 速查表;键盘监听统一豁免输入框。
4. **面板拖拽调宽**:8px 热区 + pointer capture + rAF,拖拽中只写 CSS 变量,松手才持久化。
5. **主题 = 模式 × 强调色**:light/dark × 5 套强调色预设,只覆盖 accent 相关少数令牌即换肤。
6. 小圆角(0.375rem)高密控件的工具质感 —— TerraForge 已有同等密度体系(`--ctl-h:28px`),无需借鉴。

### TerraForge 必须保留的特色

- 全屏 Cesium 3D 球为首屏的沉浸工作台,地图不被任何常驻 chrome 挤占
- 浮动胶囊工具条(左列)+ 浮动状态栏胶囊(底部)+ 右侧滑出覆盖面板
- 品牌:sky 蓝强调色(#38bdf8 系)、Inter + JetBrains Mono、splash 首屏、山形 logo
- 专业工具密度 token 体系、zh/en 双语、零 CDN 离线可用、无构建步骤(Flask + Jinja SSR)
- 契约测试守护的一切(`tests/test_css_contract.py`、`test_i18n.py` 等)

## 2. 方案选型

### 方案 A:视觉与交互增强(推荐)

布局模型不动,系统性吸收 GeoLibre 的视觉规则与交互组件:玻璃浮层、elevation 阶梯、命令面板、面板调宽、强调色预设。改动集中在 `static/css/style.css` 令牌层 + 新增 1 个 JS 组件 + 少量模板/配置改动,契约测试以小步适配,风险低、收益直接。

### 方案 B:经典 GIS 外壳重构(否决)

引入顶栏菜单 + 左右可折叠 dock 面板(rail 模式)+ 底部 dock,面板改挤占式布局。最像 GeoLibre,但会直接抹掉 TerraForge「全屏地图 + 浮动 chrome」的身份,与用户「保留本系统特色」的要求冲突;模板、panels.js、全部布局相关契约测试都要重写。否决。

### 方案 C:混合式(不推荐)

保留全屏地图,滑出面板增加「可 pin 成挤占式 dock + rail 折叠」的第二形态。一套面板两种布局语义,状态管理和测试面翻倍,收益边际。不推荐,如未来确有需求可单独立项。

**结论:采用方案 A。**

## 3. 设计详情

### 3.1 玻璃浮层视觉语言(P1)

**规则**:浮在地图之上的元素半透明;停靠/覆盖内容层的元素不透明。

- 玻璃化对象:左列地图工具条胶囊(`.map-toolbar` 各组)、底部状态栏胶囊、框选浮层 `#boundsInfo`、任务预览条 `.task-preview-chip`。
- 不玻璃化:右侧滑出面板、所有模态、下拉菜单、toast、confirm。
- 实现:
  - 新增令牌 `--color-glass-surface`(暗色约 `rgba(21,23,28,0.72)`;亮色约 `rgba(255,255,255,0.78)`,实现时以实测为准)+ 配套 `--color-glass-border`(比 `--color-border-strong` 亮一档)。
  - 玻璃元素统一 `backdrop-filter: blur(12px) saturate(140%)`。
  - 降级:`@supports not (backdrop-filter: blur(1px))` 时回落到现有 `--color-overlay-surface`(0.92 不透明);`@media (prefers-reduced-transparency: reduce)` 同样回落不透明。
- WCAG 风险(实现期第一验证点):`test_every_text_context_meets_wcag_aa` 的层叠模型按不透明底计算。玻璃面 alpha < 1 时,必须确认模型如何合成;若模型不接受半透明,则玻璃面内文字一律 `--color-text-primary` 并把 alpha 下限抬到模型可通过的值,或在测试中为玻璃令牌登记「合成假设」。此点不确定,先在实现阶段跑测试再定 alpha 终值。

### 3.2 Elevation 阶梯与阴影语义(P1)

- 现状三层背景:primary `#0c0d10` / secondary `#15171c` / tertiary `#1c2027`。补一档 `--color-bg-elevated`(暗色约 `#242a33`,亮色纯白)给「最高层」表面:滑出面板、模态(含 header),使「页面 < 卡片 < 控件 < 浮层」亮度单调递增。(~~下拉菜单~~ → 实施计划阶段核实:全站 markup 无 `.dropdown-menu` 消费者,为其写规则是死代码,已删项。)
- 阴影三档不写新数值,只补语义注释并校正误用:`--shadow-sm` = 表单控件、`--shadow-md` = 下拉/小浮层、`--shadow-lg` = 模态与滑出面板;玻璃浮层 = `--shadow-md` + 玻璃边框。
- 涉及表面改色处逐处核对亮色档(亮色 elevated 建议纯白 + `--shadow-lg`,与现有亮色模态一致则无需新档)。

### 3.3 命令面板(P1)

借鉴 GeoLibre 的 Ctrl/Cmd+K 命令面板,无依赖原生实现。

- 新文件:
  - `static/js/command_palette.js` —— IIFE 挂 `window.TerraCommands`,内含**命令注册表**(数组 `{id, titleKey, hint, shortcut, run}`),面板渲染、子串过滤(中文/英文标题均参与匹配)、键盘导航(↑↓/Enter/Esc)。
  - `templates/_command_palette.html` —— 面板 DOM(combobox/listbox ARIA),由 `base.html` include,全站可用。
  - 快捷键速查小弹窗复用同一注册表,`?` 唤起。
- 键盘监听:单一 window keydown;`input/textarea/select/contenteditable` 豁免;尊重 `defaultPrevented`。`Ctrl/Cmd+K` 与 `?` 两条全局键在此落地。
- 首批命令(用现有公开入口,不发明新能力):开始框选、清除选区、新建下载任务(有选区时)、打开任务面板、打开配置面板、前往历史页、前往配置页、打开本地处理弹窗、切换主题(暗/亮)、切换语言(中/EN)、复制当前光标坐标、快捷键速查。
- 加载顺序登记进 `base.html` 的依赖注释图:位于 `i18n.js`、`ui.js`、`theme.js` 之后,业务脚本之前。
- 样式:顶部居中(距顶约 15%)、最大宽 560px、不透明(属停靠层规则)、`--color-bg-elevated` + `--shadow-lg`;选中项 accent 描边。

### 3.4 面板拖拽调宽(P1)

- 对象:任务面板(现 920px)、配置面板(现 480px)。左缘 8px 热区。
- 行为:pointer capture + rAF;拖拽中只写 CSS 变量(`--panel-tasks-w` / `--panel-config-w`),松手写 `localStorage`(`tf-panel-w-tasks` / `tf-panel-w-config`)。
- 范围:任务 560–1100px,配置 320–640px;视口不足时 clamp 到 `92vw`。
- 窄屏(<768px)面板已是全屏覆盖,禁用拖拽。
- 实现位置:`static/js/panels.js`(沿用现有焦点管理,不改变面板的打开/关闭语义)。

### 3.5 强调色预设(P1)

借鉴 GeoLibre「模式 × 强调色」机制,默认仍是 TerraForge 品牌 sky 蓝。

- 预设 5 套:**sky(默认,品牌色)/ teal / violet / rose / orange**。teal 即 GeoLibre 品牌色系,作为借鉴的致敬,不设为默认。
- **选色规则:预设色相必须避开四个状态色**(success=emerald、warning=amber、danger=red、info=blue)——emerald/amber 正是因此不入选;rose 与 danger 是最近的一对(#fb7185 vs #f87171),靠色相/明度差异区分,且状态徽章自有 bg/border 令牌、不单独使用 accent,可接受。
- 机制与主题完全同构:`localStorage tf-accent` + `<html data-accent="...">` 属性 + `base.html` 内联引导脚本扩展(首帧前同步,防闪烁);`theme.js` 增加 `TerraTheme.setAccent()`,广播同一 `terraforge:themechange` 事件。
- 每套预设在 `style.css` 提供暗/亮两块覆盖(选择器 `:root[data-accent="x"]` 与 `:root[data-accent="x"][data-bs-theme="light"]`),只覆盖 accent 族令牌:`--color-accent{,-hover,-strong,-muted}`、`--color-on-accent`、`--color-accent-border`、splash 四件套(`--color-splash-{grid,scan,glow,bar-glow}`,splash 跟随 accent,品牌识别靠 logo 图形而非固定色)。sky 为缺省,`data-accent` 缺省/为 sky 时零覆盖、现状逐字不变。
- 档位与数值(沿用暗色 400 基 → 300 hover、亮色 800 基 → 900 hover、填充按钮 700/500 的现有规则;**以下对比度已实算**,白墨/深墨均 ≥4.5:1):

  | 预设 | 暗色 accent/hover/strong | 暗色 on-accent | 亮色 accent/hover/strong | 亮色 on-accent |
  |---|---|---|---|---|
  | sky(现状) | #38bdf8 / #7dd3fc / #0ea5e9 | #041e2b(6.18:1) | #075985 / #0c4a6e / #0369a1 | #fff(5.93:1) |
  | teal | #2dd4bf / #5eead4 / #14b8a6 | #020617(8.10:1) | #115e59 / #134e4a / #0f766e | #fff(5.47:1) |
  | violet | #a78bfa / #c4b5fd / #8b5cf6 | #020617(4.76:1) | #5b21b6 / #4c1d95 / #6d28d9 | #fff(7.10:1) |
  | rose | #fb7185 / #fda4af / #f43f5e | #020617(5.49:1) | #9f1239 / #881337 / #be123c | #fff(6.29:1) |
  | orange | #fb923c / #fdba74 / #f97316 | #020617(7.20:1) | #9a3412 / #7c2d12 / #c2410c | #fff(5.18:1) |

  其余令牌按 sky 现有 alpha 模式派生:暗色 accent-muted=rgba(400 色,0.12)、accent-border=rgba(400 色,0.25)、splash=rgba(400 色,0.06/0.09/0.45/0.5);亮色 accent-muted=rgba(700 色,0.10)、accent-border=rgba(700 色,0.35)、splash=rgba(700 色,0.07/0.10/0.35/0.40)。
- 入口:配置「外观」组新增「强调色」下拉(`_config_content.html`,配置面板与 /config 页共用,天然两处生效)。
- 契约测试适配:accent 相关断言从「唯一字面量」改为「默认 sky + `data-accent` 覆盖集」;hover 抬亮规则对每个预设分别成立。

### 3.6 全窗口拖拽打开本地处理(P2,可选)

借鉴 GeoLibre 的全窗口 drag-drop:拖 `.tif/.tiff` 到窗口任意处 → 全屏半透明遮罩提示 → 松手打开 `#processModal` 并载入该文件。enter/leave 深度计数器防闪烁。复用现有上传逻辑,不新增后端端点。范围控制起见列为可选,评审时决定去留。

## 4. 工程约束(不可违反)

- **零 CDN / 离线**:所有新代码无外部依赖;不引入任何 npm/构建步骤。
- **契约测试全绿**:`tests/test_css_contract.py`(令牌、层叠模型、密度算式、亮色块只放自定义属性)、`test_i18n.py`(模板禁裸中文)、面板/任务行 DOM 结构测试。新增令牌需在令牌区登记并配注释;亮色差异只能走 `:root[data-bs-theme="light"]` 块的令牌翻转;`[data-accent]` 覆盖块同此规则(只放自定义属性)。
- **i18n**:命令面板、速查表、强调色下拉等所有新文案走 `src/i18n/catalog/`,zh/en 双语齐全(合并期校验);JS 文案走 `js.` 前缀内联注入。
- **加载顺序**:新脚本在 `base.html` 登记依赖注释。
- **断点登记**:本次不新增断点;若实现中确需,登记到 `style.css:6-16` 清单。
- **工作区未提交改动**(状态栏图标、favicon、底图代理)与本次叠加,不回滚、不混入。
- 动效遵守既有 0.15s 显式属性表与 `prefers-reduced-motion` 整块压制;禁 `transition: all`。

## 5. 测试计划

- 新增:
  - 命令面板:打开/关闭(快捷键、Esc)、过滤、命令执行、输入框聚焦时快捷键豁免、注册表命令均有 i18n 键。
  - 强调色:`data-accent` 属性与 localStorage 往返、每套预设明暗两主题 WCAG 对比度(并入现有层叠模型测试)。
  - 面板调宽:CSS 变量写入、localStorage 持久化、范围 clamp、窄屏禁用。
  - 玻璃令牌:存在性、`@supports` 降级块存在、亮色翻转块只含自定义属性(现有契约自动覆盖)。
- 更新:`test_css_contract.py` 中 accent 字面量断言、背景层数假设(新增 elevated 档)、阴影语义注释对应关系。
- 回归:全量 `pytest`;人工核对首页、/config、/history 明暗两主题 × 各预设。

## 6. 明确不做(YAGNI)

- 顶栏 / 常驻侧边栏 / rail 折叠 / 挤占式 dock / 分屏地图(方案 B、C 的内容)
- 更换字体(IBM Plex 是 GeoLibre 的身份,Inter/JetBrains Mono 是 TerraForge 的)
- 更换默认强调色、状态色系重排
- 状态栏坐标格式循环切换(GeoLibre 特色;TerraForge 点击复制已满足,不叠加第二语义)
- 更多语言(保持 zh/en)、RTL
- 通知体系替换(现有自定义 toast 保留)

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 玻璃面 alpha 与 WCAG 层叠模型冲突 | 实现期第一件事跑 `test_css_contract.py`;不通过则抬 alpha 下限或在测试中登记合成假设 |
| `backdrop-filter` 低端机性能 | 玻璃面仅小面积胶囊;`prefers-reduced-transparency` 降级不透明 |
| accent 预设放大测试矩阵(5 套 × 2 主题) | 档位规则统一(400→300 / 700→800),测试参数化遍历预设,数值一次实算写入 |
| 命令面板与现有快捷键/Esc 层级冲突 | 单一 keydown 入口 + `defaultPrevented` 尊重;Esc 层级沿用 panels.js 现有约定,实现期梳理登记 |
| 误伤工作区未提交改动 | 只新增/追加,不改动状态栏图标、favicon、底图代理相关代码路径 |

## 8. 实施顺序建议

1. 令牌层:elevated 档 + 阴影语义 + 玻璃令牌与降级(纯 CSS,先过契约测试)
2. 玻璃化改造:apply 到工具条/状态栏/浮层
3. 强调色预设:令牌 + 引导脚本 + theme.js + 配置入口
4. 面板调宽(panels.js)
5. 命令面板 + 速查表(新组件,独立性最强,放最后)
6. (可选)全窗口拖拽
