# 地图工具条瘦身 + 选区收编进「新建任务」

**日期**：2026-08-15
**基线**：`3e20f5d`
**起因**：用户实测反馈「左上角工具栏东西太多了」，提出删掉放大/缩小、把框选与右上角的位置调整融入新建任务。

## 结论先行

工具条 **9 颗 → 6 颗**；地图右上角的 `#boundsInfo` 浮层**整块退场**，四至读数/点击编辑/手动输入/清除选区全部收进 `#createPanel` 的选区段；缩放能力从两颗按钮换成 `+` / `-` 快捷键 + 命令面板两条命令。

这不是「少画两个按钮」，是三处**重复入口**的收敛：

| 能力 | 改前入口 | 改后入口 |
|---|---|---|
| 框选 | 工具条 `#mapDrawRect` **与** 面板 `#createDrawBtn` | 只剩面板 `#createDrawBtn` |
| 手动输入四至 | 地图 `#boundsManualBtn` **与** 面板 `#createManualBoundsBtn` | 只剩面板 `#createManualBoundsBtn` |
| 打开新建任务 | 工具条「新建」**与** 浮层 `#boundsCreateBtn` | 只剩工具条「新建」 |
| 四至数值 | 浮层 `.bounds-grid`（可编辑）**与** 面板 `#createPanelBounds`（只读句子） | 只剩面板 `.bounds-grid`（可编辑） |

最后一行是这次最实在的一条：同一个四至今天在两个地方各渲染一遍，一份可编辑一份只读。

## 一、工具条

删两组：
- `#mapZoomIn` / `#mapZoomOut`（`templates/index.html:53-66`）
- `#mapDrawRect`（`:67-74`）

剩四组六颗：新建 / 导入区域 / 光照 / (任务·配置·插件)。

九颗按钮**都带可见文字 `<span>`**，所以 `ICON_ONLY_BUTTON_COUNT` 那份计数不受影响（实测确认，不是推断）。

## 二、缩放：按钮 → 键盘 + 命令面板

**为什么不能只删按钮**：Cesium 的 `ScreenSpaceEventHandler` / `ScreenSpaceCameraController` 只处理鼠标与触摸，**没有键盘相机控制**；`command_palette.js` 的命令表里没有任何 zoom；`enableZoom` 全仓没被关过。所以 `#mapZoomIn` / `#mapZoomOut` 是**键盘用户唯一的缩放路径**，直接删等于把地图缩放降级成「只能用鼠标滚轮」。

改法：`map.js:1812-1819` 那两个 handler 的函数体提成一个 `zoomMapBy(dir)`，接三个消费者：
1. `document` 上的 keydown：`+` / `=` 放大，`-` 缩小。
2. 命令面板两条新命令（`js.cmdk.zoom_in` / `js.cmdk.zoom_out`，keys 列 `+` / `-`），因此自动出现在 `?` 速查表里。
3. 无。（不再有按钮。）

**快捷键必须在可编辑元素内失效**：`e.target.closest('input, textarea, select, [contenteditable]')` 命中就直接 return。不加这条，用户在「最大层级」数字框里打 `-` 会缩放地图。同理要放过带修饰键的组合（Ctrl/Meta/Alt），那些是浏览器自己的缩放。

## 三、选区段：`#selectionField` 收编三块

`#boundsInfo`（`templates/index.html:161`）删除。`#boundsAnnounce`（`:162`，选区落定时的 `aria-live` 播报）**保留原位** —— 它是视觉隐藏的 sr-only span，与浮层无关，播报语义一个字不变。

`#selectionField` 改成：

```
#selectionField
├── #createBoundsReadout      ← updateBoundsInfo() 渲染进来（原 #boundsInfo 的内容）
│   ├── .bounds-grid          ← 4 列 N/S/E/W + .bounds-sr 方位词 + data-field 点击编辑
│   ├── .bounds-actions       ← 只剩「清除选区」#boundsClearBtn + .bounds-hint
│   └── .bounds-manual        ← 手动输入态（4 输入 + 确定/取消），入口是下面那颗
├── #tileEstimate             ← 不动
└── #createBoundsEntries      ← 不动（去框选 / 手动输入范围两颗，无选区时出现）
```

三条明确决定：
- **`.bounds-grid` 与 `.bounds-sr` 的 markup 结构一个字不动**，只换宿主。它们各有一条测试逐格钉住（4 列恰好 2 行、四个方位都带读屏词），结构一动那两条就从「检查布局」退化成「本测试已失效」。
- **`#boundsCreateBtn` 删除**：它现在是面板里一颗指向面板自己的「新建任务」。
- **`#createPanelBounds`（只读摘要句）删除**，连同 `.modal-bounds-summary` 与 `js.map.download.bounds_summary` 键：可编辑的读数搬进来之后，这句话就是同一个数字的第二处渲染。

`updateBoundsInfo()` 与 `updateCreatePanelBounds()` 的分工不变（前者渲读数、后者管两个入口的显隐），只是前者的宿主从 `#boundsInfo` 换成 `#createBoundsReadout`；委托在 `#boundsInfo` 上的那两个监听器（点击编辑、手动输入面板的 keydown）跟着换宿主。

### 面板关着的时候怎么办

用户明确选了「读数也搬进面板，地图上不再留浮层」。代价与已有兜底：
- 地图上仍然画着矩形与四个角手柄，拖拽调整照旧。
- 状态栏 `#statusSelectionText` 一直显示选区摘要（现在就有，不是新加的）。
- 需要具体数值的时刻就是建任务的时刻，那时面板本来就开着。

## 四、命令面板两条命令改成调函数

`start_bounds` / `clear_bounds` 现在是 `el('mapDrawRect').click()` / `el('boundsClearBtn').click()`（`command_palette.js:31-36`）。节点搬走后 `guard` 会返回 false，命令**静默从面板里消失** —— 这正是 `:37-45` 那段注释记下的旧坑（`new_download` 已经因为同样的原因改成了调函数）。

改成：`start_bounds` → `window.startRectDraw()`；`clear_bounds` → `window.clearSelection()`。两个函数都要挂到 `window` 上（`map.js` 现在是扁平全局，`clearSelection` 已经可达，`startRectDraw` 需要确认导出形态）。

## 五、焦点与引导

两处落点跟着搬：
- 选区落定后 pulse `#boundsCreateBtn`（`map.js:1791-1794`）→ 面板开着时 pulse 提交钮，面板关着时 pulse 工具条「新建」。
- 关手动输入面板后焦点回 `#boundsManualBtn`（`:3216-3217`）→ 回 `#createManualBoundsBtn`。

## 六、要迁移的契约（8 处）

| 契约 | 现在钉什么 | 迁移动作 |
|---|---|---|
| `test_css_contract.py:3044-3124` | `.bounds-grid` 4 列、恰好 2 行、列间距 ≤ 4px | 换宿主，判据不变 |
| `test_css_contract.py:3174-3222` | `.bounds-sr` 四方位词 + `position:absolute` | 换宿主，判据不变 |
| `test_css_contract.py:4017-4019` | `.bounds-grid` 字号与行间距 | 换宿主 |
| `test_css_contract.py:6855` | 文字对比度上下文链含 `.map-overlay-chip.bounds-overlay#boundsInfo` | **对比度要重算**：宿主从玻璃浮层换成面板的 `--color-bg-elevated` |
| `test_button_geometry.py:207-208` | `.bounds-actions .btn` 密档高度 | `.bounds-actions` 仍在，核对宿主链 |
| `test_geometry_scales.py:126,347` | 逐字选择器 `.btn.path-browse, .bounds-actions .btn` | 若 CSS 选择器串变化则同步 |
| `test_layer_stack.py:195-196` | 元素级 Escape：`boundsInfo.addEventListener('keydown', …)` | 换宿主，「Esc 分支必须 stopPropagation」不变 |
| `test_spacing_scale.py:112` | `.bounds-sr` 的 `margin: -1px` 白名单条目 | 不动（配方没变） |

另外三处连带：
- 命令面板用例（命令数、guard 形态）。
- i18n 双向闭合：退役 `tpl.index.toolbar.zoom_in` / `zoom_out` / `draw_rect` / `draw_rect_title`、`js.map.bounds.create_task_title` / `manual`、`js.map.download.bounds_summary`；新增 `js.cmdk.zoom_in` / `zoom_out`。
- `test_create_panel.py` 的显隐矩阵：`selectionField` 那一行的语义没变（仍是 map/dem），但新增的 `#createBoundsReadout` 不是独立字段组、不进矩阵。

## 七、新增断言

1. **键盘缩放存在且不吃输入**：`zoomMapBy` 有 keydown 消费者；handler 里有可编辑元素的排除判据；命令面板注册了两条 zoom 命令。（守的是本次的 a11y 底线：删按钮不许删能力。）
2. **工具条只剩 6 颗**，且 `#mapZoomIn` / `#mapZoomOut` / `#mapDrawRect` 全仓零命中（含注释外的引用）。
3. **选区读数只有一处渲染**：`.bounds-grid` 在 `static/js/` 里只被一个函数生成，且宿主是面板里的容器而不是地图浮层。

## 八、验证

- 真浏览器一条链：框选 → 面板读数出现 → 点数值改一位 → 手动输入改四至 → 清除 → 提交；键盘 `+` / `-` 缩放；在缩放输入框里打 `-` 不缩放；工具条 tab 序只剩 6 颗；明暗两套截图。
- 全量套件 + 每条迁移/新增断言的变异验证（还原修复 → 必须变红）。

## 明确不做

- 不接管 Cesium 的 InfoBox / 其它 widget 外观（上一轮已记）。
- 不给工具条加折叠/更多菜单：那是把「东西太多」藏起来而不是减少。
- 不动 `#boundsAnnounce` 的播报语义。
- 不加任务级「重试」类新入口（本仓两次否决过）。
