# 前端「系统层 + 信息架构」重做设计

**记录时间**：2026-08-14 ｜ **基线**：`967d498`（v0.3.5）
**审查依据**：[`../../reviews/2026-08-14-frontend-design-audit.md`](../../reviews/2026-08-14-frontend-design-audit.md)（Dieter Rams 十条，9/30，裁定 REDESIGN）
**配对计划**：[`../plans/2026-08-14-frontend-system-ia-redesign.md`](../plans/2026-08-14-frontend-system-ia-redesign.md)

---

## 0. 一句话

重做的对象是**几何层与信息架构**：铸一套间距刻度、把 33 个按钮类收成一套、把四条管线的入口拉到同一层级、把 12 套显隐机制收成一套；**色彩层、i18n 契约层、焦点环层、reduced-motion 层原样不动**。

**动工顺序是硬的：先扩契约测试的层叠模型，再动 CSS。** 反过来做必然重演 `CHANGELOG.md:32` 记录的那次「为迁就模型把该用子组合符的地方写成后代组合符」。

---

## 1. 三个根因与对应动作

| 用户感觉 | 根因（审查实测） | 本次动作 |
|---|---|---|
| 样式不统一 | 颜色层 0 处硬编码、明暗 100% 对等；**几何层无刻度** —— 间距令牌只有 `--pad-card:10px`/`--gap-field:8px`（`static/css/style.css:193-194`），全文仅 9 条声明消费，另有 31 个离散字面量；按钮态 33 类 / 5 套几何；浮起表面 5 种圆角；一屏 6 种控件高度 | Task 2-4：间距刻度、圆角收敛、控件同高、按钮类合并 |
| 操作不方便 | 四条管线分处两个 Bootstrap 弹窗（`templates/index.html:155` / `:288`），其中两条的入口藏在任务面板筛选行（`templates/_history_content.html:94`，`index.html:39-41` 的注释自认），另有 3 个隐藏入口 | Task 5：一个常驻「新建」入口 → 一个面板 → 四选一 |
| 不够专业 | 12 套显隐机制、3 份独立「关最上层」实现、15 处重复入口、11 处零防重复提交、12 处术语冲突、6 处标签与行为不符 | Task 6-9 |

---

## 2. 信息架构

### 2.1 现状（问题形态）

```
                     ┌─ 首屏可见：12 个交互元素 / 74 个容器里 10 个 ─┐
  ┌──────────┐       ┌────────────── 顶部居中搜索 ──────────────┐
  │ 放大     │                                        ┌ 选区浮层 ┐
  │ 缩小     │                                        │ N/S/E/W │
  │ 框选 ────┼──→ 画框 ──→ 浮层出现「下载」──→ ▣ #downloadModal (500×742)
  │ 区域     │                                        │ 「删除」→ 其实是清选区
  │ 光照     │                                        └─────────┘
  │ 任务 ────┼──→ ▤ #historyPanel (920px) ── 筛选行右端「处理」──→ ▣ #processModal (500×742)
  │ 配置 ────┼──→ ▤ #configPanel (480px)              ↑
  └──────────┘                                         ├─ 命令面板 open_process（无可见入口）
  ┌──── 状态栏：已连接 / 无活动任务 / 经纬度 / z · 高度 / 未选择区域 / 时钟 ────┐
                                                       ├─ 任务行「转成切片任务」
                                                       └─ 窗口级拖放（拖之前无提示）
```

四条管线：瓦片 + 高程在左边那个弹窗，地形切片 + 等高线在右边那个弹窗，**零条在首屏可达**。

### 2.2 目标（屏幕图）

```
  ┌──────────┐       ┌────────────── 顶部居中搜索 ──────────────┐
  │ ✚ 新建 ──┼──→ ▤ #createPanel  ← 唯一的任务创建入口
  │──────────│         ┌───────────────────────────────┐
  │ 放大     │         │ ✚ 新建任务              [×]   │
  │ 缩小     │         ├───────────────────────────────┤
  │ 框选     │         │ ⟨瓦片⟩⟨高程⟩⟨地形切片⟩⟨等高线⟩ │← .status-chips 段控（已有惯例）
  │ 区域     │         ├───────────────────────────────┤
  │ 光照     │         │ 数据范围                       │← 瓦片/高程：读当前选区，
  │──────────│         │   ▸ 已选 N/S/E/W · 预计 N 块   │   无选区则给「去框选 / 手动输入」
  │ 任务 ────┼──→ ▤   │   ▸ 或：选择来源文件 / DEM 任务 │← 地形切片/等高线：上传或选任务
  │ 配置 ────┼──→ ▤   ├───────────────────────────────┤
  └──────────┘         │ 参数（按管线切换）             │
                       │ 输出（仅瓦片/高程有保存路径）  │
                       ├───────────────────────────────┤
                       │ [ 创建任务 ]        ← 常驻底条 │← .config-footer 惯例，永不落到折叠线下
                       └───────────────────────────────┘
  ┌──── 状态栏（去掉时钟）────┐
```

**三条硬约束下的取舍**（三条都来自已被否决的清单，见 §6）：不加顶栏、不加常驻侧边栏、不加挤占式 dock。所以新入口**只能**落在既有左侧工具条里 —— 那里已经是「常驻入口」的既定位置（`任务`/`配置` 就在那儿）。

### 2.3 为什么是面板而不是第三个弹窗

| 维度 | Bootstrap 弹窗（现状） | `workbench-panel`（目标） |
|---|---|---|
| 1366×768 折叠线 | 实测 dialog 高 742px、余量仅 23px，**1366×720 时提交按钮在折叠线下 91px** | 面板满高 + `.config-footer` 常驻底条，提交按钮**结构上**不可能落到折叠线下 |
| 调整选区 | 必须关掉弹窗才能拖角点（`2026-07-30` 设计稿 :68 就是这么设计的） | 非模态，地图始终可交互，改选区时面板里的四至与预估同步刷新 |
| 惯例一致 | 与 `任务`/`配置` 两个 rail 按钮打开的东西形态不同 | 三个 rail 按钮打开三个同构面板 |
| 宽度可调 | 固定 500px | 复用 `panels.js:150-203` 的拖拽调宽 + localStorage |
| 焦点/Esc | Bootstrap 自带 | `panels.js:50-58,103-116` 已有（非模态、焦点收进关闭钮、Esc 关闭） |

结论：`#downloadModal` 与 `#processModal` 双双退场，内容并入 `#createPanel`。这不是「换皮沿用旧结构」——两张表单合并成一张按管线切换的表单，四个入口收敛成一个。

### 2.4 四条管线在新容器里的形态

字段矩阵取自 `templates/index.html:152-499` 逐控件核对（40 个控件）。跨表单同义字段 4 组必须归一：

| 现状两份 | 归一后 | 依据 |
|---|---|---|
| `#taskName` / `#processTaskName` | `#taskName` | 同为 text+required；`#processNameRow`（`index.html:301`）是**无任何 JS 引用的死钩子**，一并删除 |
| `#zoomMin/#zoomMax` / `#processZoomMin/#processZoomMax` | `#zoomMin/#zoomMax` | 同 min/max/语义；顺带修掉假旋钮（见下） |
| `#createTaskBtn` / `#createProcessBtn` | `#createTaskBtn` | 底条唯一提交钮 |
| `#localTerrainFiles` / `#contourFiles` | `#sourceFiles` | 同 `accept=".tif,.tiff"`、同 multiple、同一套 `updateTifInfo` 实现 |

管线切换要驱动的显隐谓词，现在分散在两个函数里（`static/js/map.js:1809-1833` 一维、`:1879-1895` 二维），合并为**一张表 + 一个 `apply()`**：

| 字段组 | 瓦片 | 高程 | 地形切片 | 等高线 |
|---|---|---|---|---|
| 选区（四至 + 预估 + 框选入口） | ● | ● | — | — |
| 来源文件 / DEM 任务二选一 | — | — | ● | ● |
| 输出格式（瓦片/GeoTIFF + MBTiles） | ● | — | — | — |
| 地图样式 + 缩略图 | ● | — | — | — |
| 缩放范围 | ● | — | — | ● |
| DEM 数据集 + NUM | — | ● | — | — |
| 切片层级（含「自动」）+ 档位 + 法线 | — | — | ● | — |
| 等高距 + 配色 + 晕渲 | — | — | — | ● |
| 保存路径 | ● | ● | — | — |

### 2.4b 主流程低保真线框：现状 vs 目标（并排）

**流程 A —— 下载一块区域的瓦片**（唯一在首屏可达的管线）

```
现状（6 步，2 个容器）                    目标（4 步，1 个容器）
─────────────────────────────────        ─────────────────────────────────
1 点「框选」                              1 点「框选」
2 在地图上拖出矩形                        2 在地图上拖出矩形
3 选区浮层出现 → 点「下载」               3 选区浮层出现 → 点「新建任务」
   （标签说下载，其实是开表单）              （标签 = 行为）
4 弹窗盖住地图 500×742                    4 面板从右侧滑出，地图仍可见可拖
5 填 6 个字段 → 滚到底                       ├ 段控已停在「瓦片」
   （1366×720 时提交钮在折叠线下 91px）        ├ 顶部就是四至 + 预计块数
6 点「创建下载任务」                          └ 底条常驻「创建任务」
                                          ← 改选区不必关任何东西
```

**流程 B —— 把一份本地 GeoTIFF 切成地形**（现状首屏不可达）

```
现状（7 步，3 个容器，入口需要先知道在哪)   目标（4 步，1 个容器）
─────────────────────────────────────    ─────────────────────────────────
1 点「任务」打开任务面板（920px）         1 点「新建」
2 在筛选行右端找到「处理」                2 段控点「地形切片」
   （筛选行里放创建入口 = 唯一线索是      3 选来源：上传 .tif / 挑一个已完成
     index.html:39-41 那条注释）              的高程任务
3 弹窗叠在面板上（四层混叠：地图/面板/    4 底条「创建任务」
  遮罩/弹窗，面板关闭钮留在遮罩外）
4 处理类型下拉选「本地高程切片」          面板里的信息卡（坐标系/范围/分辨率/
5 数据来源下拉选「上传文件」                建议层级）与起切规模预告原样保留
6 选文件 → 等信息卡 → 调层级/档位/法线
7 点「创建处理任务」
```

**流程 C —— 从任务列表深链去处理某个已完成的高程任务**（保留，形态变）

```
现状                                      目标
──────────────────────────────────       ─────────────────────────────────
任务行「转成切片任务」                    任务行「用它切地形」（标签 = 行为）
  → 打开 #processModal                      → 打开 #createPanel
  → JS 改两个下拉的 value 并补发             → 预选「地形切片」+「已有高程任务」
     change 事件（map.js:2434-2456）        → 预选那个任务（同一段预填逻辑搬迁）
  → 350ms 后聚焦任务名
```


**四个「假旋钮」顺手修掉**（都是本次动这张表单时零成本的，不修就是明知故犯）：

| 假旋钮 | 现状 | 处置 |
|---|---|---|
| `default_style` | 配置页有控件（`_config_content.html:121-126`），但不在 `MAP_CONFIG_KEYS`（`src/routes/main.py:33-34`），进不了页面 config，`#mapStyle` 永远是首项 | 把 `default_style` 加进白名单并在表单里 selected；或明确改配置项文案为「底图样式」（它实际只喂 Cesium 底图）。**二选一，计划里定的是后者**（改文案，成本最低且不新增页面级配置面） |
| `contour_default_interval` | 出厂 50、后端会回落，但 `#contourInterval` 硬编码 `value="50"` 且提交端 `|| 50` 永远发非空 | 服务端渲染初值（照抄 `index.html:363-370` 的 Jinja 写法） |
| `default_zoom_min/max` → `#processZoomMin/Max` | 只覆盖 `#zoomMin/#zoomMax`（`map.js:427-428`），等高线那一腿不覆盖 | 归一后自动修好（同一对字段） |
| `default_output_format` | 全项目**零消费**（除 DEFAULT_CONFIGS 与 `_UNCONSTRAINED_KEYS`） | 删键；`database.py:50` + `config_manager.py:372` 同步 |

### 2.5 重复入口的收敛

| 现状 | 目标 |
|---|---|
`#processModal` 四个入口：筛选行 `#processOpenBtn`、命令面板 `open_process`、任务行「转成切片任务」、窗口拖放 | 一个 rail 按钮；命令面板与任务行深链改为**打开 `#createPanel` 并预选管线**（`openProcessForDemTask` 的预填逻辑 `map.js:2434-2456` 原样搬迁）；窗口拖放保留（它是快捷路径，不是唯一路径），但改为打开面板并预选「地形切片」
命令面板同时列 `open_tasks`+`goto_history`、`open_config`+`goto_config` | 各留面板那一条；`/history`、`/config` 两条路由**保留**（深链与打包可达性需要），但从命令面板移除
工具条按钮不是开关（`panels.js:41` `if (current === name) return;`） | 改成 toggle，并补 `aria-expanded`（`index.html:99,103,541` 三处触发器均缺）
选区浮层「下载」（打开表单却叫下载）、「删除」（清的是选区，title 却写「清除选区」） | 「新建任务」+ title「用当前选区新建下载任务」；「清除选区」与 title 一致 |

`/history` 与 `/config` 两条独立路由与 `#historyPanel`/`#configPanel` 抽屉的重复**本次不合并**——它们 include 同一份 partial，重复的是「路由外壳」而不是内容，合并要动打包可达性与深链。记账在 §6。

---

## 3. 令牌决策

### 3.1 间距刻度（本次的核心）

**硬约束（本次新发现，审查未列）**：`tests/test_css_contract.py:2747-2763` 的 `_resolve_length_px` 明确不支持 `calc()` 与 `var(--x, fallback)`，解析不了就返回 `None`，让约 10 处调用方报「模型已失效」。**所以刻度必须是扁平字面量，不能写 `calc(var(--space-unit) * 3)`。**

实测现有分布（`static/css/style.css` 全文 padding/margin/gap）：`8px`×26 · `12px`×16 · `6px`×15 · `10px`×12 · `4px`×5 · `14px`×5 · `2px`×7 · `5px`×2 · `7px`×1 · `16px`×1 · `18px`×2 · `1px`×1，另有 19 个 rem 值。

**决定的刻度（7 级，扁平 px 字面量）**：

```css
--space-hair:  2px;   /* 紧凑内衬：色块内边距、分隔线让位 */
--space-1:     4px;
--space-2:     8px;
--space-3:    12px;
--space-4:    16px;
--space-5:    24px;
--space-6:    32px;
```

迁移映射（**含视觉变化，必须目测验收**）：

| 现值 | 出现次数 | 迁到 | 视觉影响 |
|---|---|---|---|
| 2px / 4px / 8px / 12px / 16px | 7 / 5 / 26 / 16 / 1 | 原位对应 | 无 |
| **6px** | 15 | `--space-2`（8px） | 略松，+2px |
| **10px** | 12 | `--space-3`（12px） | 略松，+2px（含 `--pad-card`） |
| **14px** | 5 | `--space-4`（16px） | 略松，+2px |
| **18px** | 2 | `--space-4`（16px） | 略紧，−2px |
| **5px / 7px** | 2 / 1 | `--space-1` / `--space-2` | ±1px |
| rem 值 19 个 | — | 逐个换算到最近级 | 逐处记账 |

**不在本次范围**：`--ctl-pad-y:3px` / `--ctl-pad-x:8px` / `--ctl-line-h:20px` 这一组控件密度令牌（`style.css:187-190`）原样不动 —— 它们参与 `2*3+20+2*1=28` 的算术契约（`tests/test_css_contract.py:2811`），是另一套经过实测的独立系统。间距刻度只管**容器与组之间**，不管控件内部。

保留 `--pad-card` / `--gap-field` 作为语义别名，值改为指向刻度：`--pad-card: var(--space-3)`、`--gap-field: var(--space-2)`。这样 `FIELD_GAP_MAX_PX = 8`（`:2729`）与 `.mb-3` 必须字面包含 `var(--gap-field)` 的断言（`:2835`）继续成立。

### 3.2 圆角收敛：7 级 → 4 级

现有 7 级（`style.css:137-145`）在「浮起表面」上实际用出 5 种，且同一张卡里外框 12px、表头 10px（`:2950` vs `:2970`）。

| 保留 | 值 | 用途 |
|---|---|---|
| `--radius-xxs` | 4px | 紧凑内衬（色块、微标） |
| `--radius-xs` | 6px | **一切控件**（按钮、输入、选择） |
| `--radius-card` | 12px | **一切浮起表面**（卡片、面板、弹层、toast、confirm、搜索下拉） |
| `--radius-pill` | 999px | 胶囊 |

退役：`--radius-sm`(8) / `--radius`(10) / `--radius-lg`(14)。定点修复：`.card-header` 10→12（`:2970`，连带那条 `!important`）、`.stat-card` 10→12（`:3052`）、`.app-confirm` 14→12（`:3760`）、`.map-search__panel` 8→12（`:1966`）、`.btn.btn-icon`（`:2914`）与 `.btn.btn-compact`（`:4375`）**补上显式 6px**（现在落回 Bootstrap 默认，`:2213` 还为其中一个实例单独补过一次）。

**成本最低的一项**：实测三个目标测试文件里**没有任何一条断言 radius / z-index / font-weight 的值**。

### 3.3 控件高度

现状一屏并存 6 种：工具条 58/60/64（文字长度不同导致不齐）、`.btn-primary` 36.5、`.btn-sm` 35.8、`.form-control`/`.btn-compact` 28、`.statusbar-pill` 34、`.map-search__input` 23、`.bounds-edit-input` 20。

决定**两级 + 一条规则**：

```css
--ctl-h:    28px;   /* 已有，密控件：输入、选择、图标钮、compact 钮 */
--ctl-h-lg: 36px;   /* 新增，主操作：底条提交钮 */
```

规则：**同一行内的控件与按钮必须同高。** 定点修复三处违例：
- `.map-search__input` 23 → `--ctl-h`（`:1905-1913`）
- `.bounds-edit-input` 20 → `--ctl-h`（`:4236-4246`），同时删掉基础态那条无替代的 `outline:none`（`:4245`，全仓唯一压掉全局焦点环的地方）
- 手动四至面板的「确定/取消」36 → `--ctl-h`（与它们同行的输入框同高）

工具条按钮高度不齐的根因是 `.map-panel-btn` 靠内容撑高、`span` 是 12px `nowrap`（`:918,966-968`）。改为**固定 `height: 56px`**（一档，非刻度内的组件专有尺寸，登记在注释里），标签溢出由既有的 `html[lang=en]` 逃生舱（`:950-954` `width:auto` + `min-width:max-content`）继续兜。

`.statusbar-pill` 34px **不动** —— 它消费 `--statusbar-h:34px`（`:152`），是状态栏自己的尺寸系统。

### 3.4 字号：11 种 → 6 种（刻度全集）

清掉 5 个字面量：

| 位置 | 现值 | 改为 | 备注 |
|---|---|---|---|
| `.workbench-statusbar`（`:671`） | `13px` | `--font-size-xs`（12px） | `:668-670` 为 13px 写过理由；本次判定「统一优先」，需目测 |
| `.tint-stop-label`（`:1080`） | `10px` | `--font-size-xs`（12px） | 低于刻度下限，无理由注释 |
| `.detail-terrain-info`（`:3644`） | `0.9rem` | `--font-size-sm`（0.875rem） | 注释自认off-scale |
| `.app-toast__close`（`:3718`） | `1.15rem` | `--font-size-lg`（1.125rem） | |
| `.splash-wordmark`（`:4161`） | `1.35rem` | `--font-size-xl`（1.25rem） | **正好消费掉唯一零引用的令牌**（`:168`） |

改完必须同步更新选择器→字号对照表（`tests/test_css_contract.py:153-197`，20 条精确选择器字符串）与 `test_no_font_size_uses_important`（`:106`，全文禁止 `font-size` 带 `!important`）。

### 3.5 字重：4 个裸整数 → 3 级令牌

同一种控件上现在有三种字重（`.btn` 500 `:2642` / `.btn-primary` 600 `:2658` / `.btn-compact` 400 `:4385`）。

```css
--weight-normal: 400;
--weight-medium: 500;   /* 控件默认 */
--weight-strong: 600;   /* 标题、主操作 */
```

`700` 只保留在 `.modal-title`/`.progress__label`/`.page-item.active`（三处），或一并降到 600 —— 计划里按「先降到 600，目测不满意再回滚」执行。

### 3.6 z-index：14 个裸整数 → 一组令牌

现有阶梯只以散文注释存在（`style.css:1217-1220`）。铸令牌，值**照抄现值**（本次不改层序，只把数字变成名字）：

```css
--z-map-veil:    2;      --z-map-chrome:  20;
--z-panel:       1000;   --z-panel-drag:  1450;   --z-statusbar: 1500;
--z-backdrop:   11000;   --z-modal:      12000;
--z-toast:      13000;   --z-cmdk:       13100;
```

唯一的 z-index 断言在 `tests/test_fix_terrain_preview_transition.py:94-98`（`.map-transition-veil` 必须低于 `.map-toolbar`），照抄现值即自动成立。

### 3.7 动效：14 种 → 4 种

`0.15s ease` 已是家规且守住 17/20。铸 `--dur-fast: 0.15s` / `--dur-base: 0.2s` / `--dur-slow: 0.45s` / `--ease: ease`，把 `0.12/0.14/0.18/0.22/0.25/0.35s` 六个孤值归到最近级。三个进度条时长（`:2369` 0.2s linear / `:4274` 0.3s ease / `:4184` 0.12s linear）统一为 `--dur-base` + linear。

**每一次增删都必须同步 `_MOTION_BRANCH_COUNT`（`tests/test_css_contract.py:6385`，现值 42）并在 `:6372-6374` 的记账注释里补一行**，格式照抄现有的「38 -> 37（删死代码）：…」。

---

## 4. 六态清单

逐组件核对「空 / 加载 / 错误 / 成功 / 焦点 / 禁用」。✅=已有且合格；⚠️=有但粗糙；❌=缺。

| 组件 | 空 | 加载 | 错误 | 成功 | 焦点 | 禁用 | 本次要补的 |
|---|---|---|---|---|---|---|---|
| 新建面板（四管线） | — | ⚠️ | ⚠️ | ✅toast | ✅ | ⚠️ | 提交按钮 in-flight 已有（`map.js:3144-3152`）；原生英文校验气泡（实测 `"Please fill out this field."`）改走应用自己的行内错误 |
| 任务列表 | ✅`task_list.js:50-58` | ✅初始 spinner | ⚠️仅一行文字（`:44`） | ✅ | ✅ | — | 加载失败给**重载列表**按钮（注意：**不是**给任务加「重试」，那条已被否决两次） |
| 任务行动作 | — | ❌ | ❌ | ✅socket | ✅ | ❌ | 5 处 POST 上 in-flight 守卫（`task_center.js:745,763,777,893,911`）；三块饱和色块改为幽灵钮，仅删除保留危险色 |
| 任务详情弹窗 | — | ❌等 fetch 完才开 | ⚠️只 toast | ✅ | ✅BS | — | 先开窗再填内容 + 骨架态 |
| 目录选择器 | ✅`no_subdirs` | ❌ | ✅错误框 | — | ❌ | — | loading 指示 + `.focus()` + Enter 确认（`path_browser.js` 全文无 keydown） |
| 配置保存/重置 | — | ❌ | ✅ | ✅ | ✅ | ❌ | 两处按钮 in-flight（`config.js:715,752`） |
| toast | — | — | ✅ | ✅ | — | — | 容器补 `aria-live`（`ui.js:39-41` 现在只在每个 toast 节点上设 `role=alert`） |
| 进度对话框 | — | ✅ | ❌ | ✅ | ❌无焦点管理 | — | 焦点/Esc/aria-live（`ui.js:279-350`，且它声明了 `aria-modal="true"` 却零 Tab 拦截） |
| confirm 对话框 | — | — | — | — | ⚠️默认焦点在**销毁**键（`ui.js:250-253`） | — | `danger:true` 时默认焦点改到取消键 |
| 选区浮层 | ✅无选区分支 | — | ✅toast | ✅ | ⚠️4 个 `span` 无 `tabindex`/`role` | — | 四个读数改真 `<button>`（全仓 `tabindex="0"`/`role="button"` 命中数为 0） |
| 地名搜索 | ✅ | ⚠️ | ✅ | ✅ | ✅ | — | `role=combobox` 指向的容器补 `role=listbox` + 选项 `role=option` |

`catch` 定策（现状 46 处静默 + 8 处仅 console）：三档 —— **A 用户可见**（影响用户当前动作）、**B 仅日志**（后台补偿路径）、**C 明确忽略**（必须写一行理由注释）。逐处归档，不许再有无注释的空 catch。

---

## 5. 契约测试模型的扩展方案（先于任何 CSS 改动）

**三个封锁点，全部已读源码确认**：

| # | 位置 | 现状 | 最小扩展 | 供体代码 |
|---|---|---|---|---|
| 1 | `tests/test_css_contract.py:5427,5434` | 任何声明 `color` 的规则里出现 `>` `+` `~` 即判「模型已失效」。**实测确认现状是「一律拒答」而不是「静默算错」**：`_text_branch_applies:5244` 的 `branch.split()` 把 `>` 切成独立 token，`_parse_compound('>')`（`:5194`）读不懂返回 `None`，函数在 `:5249` 提前返回 `None`，上层 `_winning_color_decl:5350` 的 `assert hit is not None` 于是报「本测试已失效」 | 把子组合符支持移植进 `_text_branch_applies`（= 把「拒答」变成「答对」） | **同文件已有**：`_split_branch:7809-7828`（把 `.a > .b .c` 拆成带组合符的序列）+ `_branch_matches:7831-7874`（支持后代与 `>`，遇 `+`/`~` 返回 `None`）。`+`/`~` 继续不支持是对的 —— 祖先链里确实没有兄弟信息 |
| 2 | `tests/test_css_contract.py:5429,5439` | 任何 at-rule 内声明 `color` 即失效，`_winning_color_decl:5334`（签名 `(css, chain, label)`）只算顶层规则 | 引入「环境判决」三值返回 | **同文件已有**：`_motion_media_verdict:6209-6220` 的 `True/False/None` 形态（宽度类断点一律 `None` = 响亮失败），与 `_btn_media_applies:4091-4108` 同口径 |
| 3 | `tests/test_css_contract.py:2747-2763` | `_resolve_length_px` 不支持 `calc()` 与 `var(--x, fallback)`，返回 `None` 让约 10 处调用方报「模型已失效」 | **不扩展**，改约束设计：间距刻度写扁平 px 字面量（§3.1 已按此定） | — |

**爆炸半径比审查估的大**：实测 **18 个测试文件**读 `static/css/style.css`，合计 **365 个节点**。本次的前端闸门命令与实测基线：

```bash
uv run pytest $(grep -rln "style\.css" tests/ --include='*.py' | tr '\n' ' ') \
  tests/test_i18n.py tests/test_map_js_contract.py tests/test_output_format.py \
  tests/test_config_form_submittable.py tests/test_index_has_contour_option.py \
  tests/test_path_browser.py -q
# 2026-08-14 基线：478 passed in 27.36s
```

**一处双向跨文件锁必须与按钮合并同一个 commit 改**：`tests/test_fix_templates_a11y.py:440-472` 既断言 `style.css` 里 `.btn-info` 恰好 4 条规则分支，又去 grep `test_css_contract.py` 的源码确认 `FILLED_BTN_VARIANTS` 里还留着 `'btn-info'` —— 只动一头，这条豁免就变成无人看守的死代码（该测试的 docstring 自己写明了这一点）。

**必须同步的普查常量**（改 CSS 时忘了改就红在很远的地方）：

| 常量 | 位置 | 何时要动 |
|---|---|---|
| 选择器→字号对照表（20 条精确字符串） | `:153-197` | 任何字号或选择器变化 |
| 类型刻度 rem 字面量 | `:236-241` | 字号刻度变化 |
| `!important` 上界 37 | 断言在 `:534`，docstring `:335-533` | 增删 `!important`；**棘轮规则 `:494-501`**：清理型必须把上界降到「新实测 + 3」，新增型必须逐条登记「新增几处、压的是谁、为什么非它不可」 |
| `CONTROL_HEIGHT_MAX_PX = 30` | `:2726` | 控件高度变化（新增 `--ctl-h-lg:36px` 用在**按钮**上，需确认该常量的作用域只覆盖 form-control） |
| `FIELD_GAP_MAX_PX = 8` | `:2729` | 字段纵向间距变化 |
| `BUTTON_CONTEXTS`（11）/ `len(cells)==55` | `:4518` / 按钮矩阵断言 | 按钮类合并 |
| `FILLED_BTN_VARIANTS`（5）/ `TRANSPARENT_BTN_VARIANTS`（3） | `:4478` / `:4492` | 同上 |
| `ICON_ONLY_BUTTON_COUNT = 21` | `:4986` | rail 新增「新建」按钮 → 22 |
| `_MOTION_BRANCH_COUNT = 42` | `:6385` | 增删任何声明 transition/animation 的分支 |
| `_RUNTIME_INJECTED_DIVS` | `:7943` | 新增 JS 注入的 div |
| `_KNOWN_INACTIVE_AT_RULES` | `:8021` | 新增能力兜底 at-rule |
| `_REDUCED_MOTION_EXEMPT` | `:6562` | 新增压不进 reduce 块的豁免 |
| `VIEWPORT_1366_HEIGHT_PX = 768` + `_index_form_vertical_model:3537-3668` | `:3209` | **弹窗改面板后这套折叠线模型的被测对象消失** —— 不是改阈值，是改被测对象（见计划 Task 5） |

`CLAUDE.md:189` 的规矩照办：**改期望值之前先读旁边的注释，每个数字记的都是一次实测失败，不是偏好。**

---

## 6. 明确不做

| 项 | 为什么 |
|---|---|
| 顶栏 / 常驻侧边栏 / rail 折叠 / 挤占式 dock / 分屏地图 | `specs/2026-08-11-geolibre-inspired-ui-design.md:135` 已判不做；且 2026-07-28 真做过一次「顶栏 + 380px 右侧 dock」，**只活了两天就被 `c854e12fe` 删掉**（`plans/README.md:32`） |
| 给任务加「重试」按钮 | 两次被否：`plans/2026-07-27-phase2-visual.md:1581`（三个 manager 的 `start_task` 只收 pending/paused，要先改后端状态机）、`specs/2026-08-07-task-lifecycle-simplification-design.md:290`（D3 已定：失败就删掉重建）。本次只给**列表加载失败**加「重载」，那是 fetch 失败，与任务状态机无关 |
| 恢复「取消任务」 | `task_store.js:265-268` 记录该功能已下线（见 `models/task.py` 的 TaskStatus）。恢复它要动三个 manager 的状态机，超出前端范围。本次只做到「明确告知不可取消」 |
| 往 `div` 兜底重置的 `:not()` 白名单里塞类 | `tests/test_css_contract.py:377-380`：「那是在给已知的结构债继续加码，明确不做」 |
| 把 `{% block extra_css %}` 挪到 `style.css` 之前 | `tests/test_css_contract.py:2620-2626`：会把「页面级 CSS 覆盖全局 CSS」的语义反过来，且当前无任何模板用这个 block |
| 用子组合符写按钮/文字颜色选择器（在 Task 1 落地之前） | `CHANGELOG.md:32` 记录的那次妥协就是这么来的。Task 1 之后才允许 |
| 换字体 / 换默认强调色 / 重排状态色 / 加语言 / RTL / 换 toast 体系 | `specs/2026-08-11-geolibre-inspired-ui-design.md:136-141` 六条 YAGNI |
| 合并 `/history`、`/config` 两条路由与对应抽屉 | 重复的是路由外壳而非内容（include 同一份 partial）。合并要动深链与打包可达性，收益远小于风险。**记账，不做** |
| 图标 sprite 化 | `plans/2026-07-27-phase2-visual.md:1582` 判为第三档；本次仍不做，但**新增的 rail 图标必须走 `_macros.html`**，不许再手写内联 SVG |
| splash 的存在与否（含那条模拟进度条） | 审查把它记为潮流标记与一处不诚实。改它属于产品决策，本次只记账 |
| 压缩 `style.css` / 加 `defer` / 治理 20Hz 空闲循环 / `/history` 少付 5.9MB | 都是审查里「一天能修完」的独立小改，与系统层重做正交。**另开一份计划**，不要顺手混进来 |

> 「顺手就能修的东西不在计划里 —— 记下来，不要顺手改。」（`plans/2026-07-27-master-plan.md:176`）

---

## 7. 必须保留项与回归判据

| 保留项 | 位置 | 回归判据（每个 Task 结束都要过） |
|---|---|---|
| 色彩令牌体系：0 处硬编码色 | `style.css:19-250` / `:277-380` / `:396-498` | 属性锚定的 hex/rgba 扫描在 `:500-4883` 仍须 **0 命中**；`tests/test_elevation_glass.py` + `tests/test_accent_switch.py` + `tests/test_theme_switch.py` 全绿 |
| 明暗 100% 对等 | 59 个受主题令牌全有明色对位 | `test_css_contract.py` 的双主题对比度断言全绿 |
| i18n import 期硬失败契约 | `src/i18n/catalog/__init__.py:50-56` | `uv run pytest tests/test_i18n.py -q` 全绿；新键必须以**完整引号字面量**出现在源码（双向闭合按字面量扫描，`:280-326`） |
| 焦点环体系 | `style.css:3474-3477` + `:2805-2809` + 10 处组件级替代 | `test_focus_visible_has_a_visible_outline` 绿；`outline:none` 无替代的地方**从 1 处（`:4245`）降到 0 处** |
| `prefers-reduced-motion` 块 | `style.css:4064-4072`（含 `0.01ms` 而非 `0` 的理由） | `-k 'motion'` 4 个节点全绿；`_REDUCED_MOTION_EXEMPT` 名单不新增未记账条目 |
| 纯图标按钮无障碍名 | `tests/test_css_contract.py:4986,5048-5068` | 每个无可见文本的 `<button>` 都有非空 `aria-label`；`ICON_ONLY_BUTTON_COUNT` 从 21 改成 22 并在注释里记一行 |
| 命令面板 ARIA 实现（全仓唯一完全正确的自定义组件） | `static/js/command_palette.js:106-121,142,171-172` | `tests/test_command_palette.py` 全绿；**新组件照它抄**，不许它反过来退化 |
| `style.css` 是最后一张样式表 | `tests/test_css_contract.py:2597-2653` | 子模板一律不许出现 `<link rel=stylesheet>` |
| 离线不变量 | `CLAUDE.md:179-181` | 无 CDN / 无 `@import` / 无远程资源；vendor 字节清单与文件数断言全绿 |
| 无构建步骤 | `CLAUDE.md` + 三份前序计划 | 不引入 npm / 预处理器 / bundler；JS 用 `var` + IIFE 挂 `window` |

---

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 间距迁移带来的密度变化（6→8、10→12、14→16、18→16）在真实界面上不好看 | 每个 Task 结束在 1600×900 与 1366×768 两档、暗/亮两主题各截一次图，与 `docs/assets/images/design-audit-2026-08-14/` 的改前图逐屏对比。不满意就调整映射，**不要**回退成「保留孤值」 |
| 扩模型（Task 1）本身写错，导致后续断言给出「错误的信心」 | Task 1 的验收不是「绿」，而是**变异检验**：故意往 `style.css` 塞一条 `.a > .b { color: red }`，扩展后的模型必须算出正确的赢家；再塞一条 `@media (max-width: 576px) { .x { color: blue } }`，宽度类断点必须**响亮失败**而不是静默通过 |
| 弹窗改面板后 `_index_form_vertical_model`（1366×768 折叠线模型）失去被测对象 | 不是删测试。改成对**新面板**建模：面板满高 + 常驻底条 ⇒ 断言「提交钮 bottom ≤ 视口高」恒成立，并把 `test_submit_button_lives_inside_download_modal:3695-3736` 的三条结构断言改写为「`#taskForm` 与 `#createTaskBtn` 是 `#createPanel` 后代 + 底条是 sticky」 |
| 按钮类合并触发 8 个硬编码计数 + 双向跨文件锁 | 合并与 `tests/test_fix_templates_a11y.py:440-472`、`test_css_contract.py` 的六个常量在**同一个 commit** 里改完；先跑 `-k 'button or icon or focus_visible'` 小闸门（12 节点）再跑全量 |
| 术语归一（12 处冲突）改动 i18n 值，撞上 6 处值级断言 | 这 6 处不在 `test_i18n.py` 里：`test_tasks_js_contract.py:1124-1133`（各状态 zh 必须含特定词，如 `completed_with_gaps` 必须含「缺块」）、`:2001-2013`、`:2306`、`:2372-2376`、`test_terrain_lighting_frontend.py:503-507`、`test_i18n.py:466-476`。**「缺块」是被测试锁住的那一侧 —— 归一时以「缺块」为典范，把「缺口」改过去，不是反向** |
| 一次改太多导致无法二分定位 | 每个 Task 一个 commit，Step 5 固定跑前端闸门（478 passed 基线）。任何 Task 结束时闸门不绿，先修再进下一个 Task |
| 计划里的行号在执行时已漂移 | 每个 Task 的 Step 3 开始前先 `read` 目标区间确认构造，不许按行号盲改（`plans/README.md:11` 第三条硬约束） |
