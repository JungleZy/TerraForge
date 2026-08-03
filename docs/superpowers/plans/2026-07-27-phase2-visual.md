# Phase 2：视觉与交互 Implementation Plan

> **归档文档 · 非当前实现**
> **记录时间**：2026-07-27 ｜ **状态**：已实施（C1 与 A1-A7 于 2026-07-27/28 全部落地），**勿按此文重跑**
> 「已核实的基线数字」是改造前的快照：`!important` 92 处、`*{}` 与 `.progress-bar` 的行号今天全部错位。当前 `!important` 上界以 `tests/test_css_contract.py:439`（`test_important_count_under_control`，上界 ≤ 59）为准；注意 `static/css/style.css` 的注释里会逐字讨论代码，裸 `grep` 计数会被注释污染而虚高，计数前必须先剥注释。
> Task 9「Leaflet 控件深色化 + 汉化 `drawLocal`」在今天的代码里已无作用对象（前端已换成 CesiumJS）。阶段收尾要求的「`app.py` 与 `build.spec` 两处版本号 bump」两处都已失效：`build.spec` 随 Nuitka 迁移删除，版本号唯一真源是 `core/config.py:38` 的 `APP_VERSION`。
> Task 1「建立视觉基线截图」若被重跑会覆盖 `docs/images/phase2-baseline/` 下的既有基线，**不要重跑**。
> ⚠️ 复选框状态无效（97 个全未勾 ≠ 未执行）；正文源码与行号为当日快照，禁止照抄或照行号定位。
> *正文保持原样未回改。*

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让界面在 1366×768 上一屏放得下，进度条不再骗人，深色主题下没有漏白的控件。

**Architecture:** 先做地基（C1：清掉 `style.css` 里自我覆盖的字号块和死代码），再在干净的地基上做七项视觉修复（A1-A7）。**顺序不可颠倒** —— 92 处 `!important` 会让后面的改动改了不生效。

**Tech Stack:** Bootstrap 5.3 · Leaflet 1.9.4 + Leaflet.draw 1.0.4 · 原生 CSS / JS（无构建工具、无预处理器）

**上级计划：** [`2026-07-27-master-plan.md`](2026-07-27-master-plan.md) —— **Global Constraints 一节对本计划全部任务生效，开工前必读。**

---

## Global Constraints（本阶段补充）

- **C1（Task 1-4）是纯重构，不得改变视觉表现。** 唯一例外是 Task 4 修复的 select 下拉箭头丢失（那是真 bug）。每个清理步骤后必须截图对比。
- **不要推翻 2026-06-15 那次改造的设计意图。** 当前深色配色来自 [`2026-06-15-frontend-premium-redesign.md`](2026-06-15-frontend-premium-redesign.md)：青绿(`#2dd4bf`)是**品牌色不当状态色**、`--shadow-glow: none` 是**有意去发光**。清理时别把有意为之的东西当垃圾删了。
- **每次改 CSS 后必须硬刷新浏览器**（Ctrl+Shift+R）。Flask 静态文件默认带缓存，改了不刷新会让你以为没生效。
- 本阶段所有 pytest 测试都是**文本级契约测试**（断言文件里有/没有某字符串）。它们守不住"看起来对不对"，真实观感由 Playwright 截图对比覆盖。

### 已核实的基线数字（2026-07-27 实测）

| 项 | 值 | 用途 |
|---|---|---|
| `!important` 总数 | **92** | C1 完成后应显著下降 |
| `*{}` 规则 | **3 处**：`:67`、`:1217`、`:1243` | Task 3 合并 |
| `btn-outline` 定义 | **0 处** | Task 11 补 |
| `.progress-bar` 定义 | **2 处**：`:429`、`:1388` | Task 2 合并 |
| `.progress-bar.bg-primary` | **不存在**（success/info/warning/danger 都有） | Task 5 的关键约束 |

---

## File Structure

| 文件 | 责任 | 本阶段改动 |
|---|---|---|
| `static/css/style.css` | 全部自定义样式（1617 行） | C1 清理；A2-A7 的样式改动 |
| `static/js/tasks.js` | 活动任务渲染与 Socket.IO 更新 | A1 状态色；A2 进度数字；失败态保留 |
| `static/js/history.js` | 历史页渲染 | A1/A2 的对应调用点 |
| `templates/base.html` | 布局骨架与 CDN | A3 加 `data-bs-theme`；A4 加 drawLocal 汉化 |
| `templates/index.html` | 主表单 | A5 四至显示压缩 |
| `static/js/map.js` | 地图与四至渲染 | A5 `updateBoundsInfo` 压缩 |
| `tests/test_css_contract.py` | **新建** | CSS 关键契约的回归保护 |
| `docs/images/phase2-baseline/` | **新建** | C1 的视觉基线截图 |

---

# 第一部分：C1 —— CSS 地基清理

## Task 1：建立视觉基线

**Files:**
- Create: `docs/images/phase2-baseline/`（截图存档）

**Interfaces:**
- Consumes: 无
- Produces: 4 张基线截图，Task 2-4 用它们做对比。

**Rationale:** C1 是"不改变视觉表现"的重构。没有基线截图就无法证明这一点，只能靠"我觉得没变"。

- [ ] **Step 1: 起服务**

```bash
DEBUG=0 uv run python app.py
```

- [ ] **Step 2: 截 4 张基线图**

浏览器视口设为 **1600×1000**，逐页截图保存到 `docs/images/phase2-baseline/`：

| 文件名 | 页面 | 操作 |
|---|---|---|
| `home.png` | `http://127.0.0.1:5000/` | 默认状态 |
| `contour.png` | 同上 | 「下载类型」切到「等高线瓦片」，整页截图 |
| `history.png` | `http://127.0.0.1:5000/history` | 默认状态 |
| `config.png` | `http://127.0.0.1:5000/config` | 整页截图 |

> 已有一组同视口的截图在 `docs/images/ui-review-2026-07/`（评审时拍的）。**可以直接复制过来当基线**，省去重拍：
> ```bash
> mkdir -p docs/images/phase2-baseline
> cp docs/images/ui-review-2026-07/{home,contour,history,config}.png docs/images/phase2-baseline/
> ```

- [ ] **Step 3: 额外记录关键计算值**

在浏览器控制台跑，把输出记进本任务：

```javascript
const el = document.querySelector('.form-control');
const cs = getComputedStyle(el);
console.log(JSON.stringify({
  formControlHeight: el.getBoundingClientRect().height,
  fontSize: cs.fontSize,
  padding: cs.padding,
  cardHeaderHeight: document.querySelector('.card-header')?.getBoundingClientRect().height,
  selectBgImage: getComputedStyle(document.querySelector('.form-select')).backgroundImage,
}, null, 2));
```

**预期基线值**（评审实测，用于确认你的环境一致）：`formControlHeight` ≈ 43.7、`fontSize` = 15px、`selectBgImage` = `"none"`（这就是 Task 4 要修的 bug）。

- [ ] **Step 4: 提交基线**

```bash
git add docs/images/phase2-baseline/
git commit -m "chore(css): 存档 C1 清理前的视觉基线截图

C1 是不改变视觉表现的重构,用这组截图做前后对比。
视口 1600x1000,含首页/等高线参数/历史/配置四页。"
```

---

## Task 2：删除自我覆盖的字号块，把最终值合并回原规则

**Files:**
- Modify: `static/css/style.css:1318-1434`（整块删除）+ 各选择器的原始定义处
- Test: `tests/test_css_contract.py`（新建）

**Interfaces:**
- Consumes: Task 1 的基线截图
- Produces: `style.css` 不再有「统一字体大小系统」块。Task 10（A5 密度）依赖这一点——否则改 `:877-887` 的 `font-size` 不生效。

**Rationale:** `:1318-1434` 用 `!important` 重新声明了前面已经定义过的选择器。实测冲突：

| 选择器 | 原始定义 | 覆盖块 | 实际生效 |
|---|---|---|---|
| `.form-label` | `:902` `.9rem` | `:1338` `.875rem!important` | `.875rem` |
| `.nav-link` | `:148` `.95rem` | `:1327` `.9375rem!important` | `.9375rem` |
| `.progress-bar` | `:429`（无 font-size）| `:1388` `.875rem!important` | `.875rem` |

**要点：合并时以「覆盖块的值」为准**，因为那才是当前实际生效的。目标是删掉这一块而**页面看起来完全不变**。

- [ ] **Step 1: 写契约测试**

创建 `tests/test_css_contract.py`：

```python
"""
style.css structural contract tests.

这些是文本级断言,守不住"看起来对不对"(那由 Playwright 截图对比覆盖),
但能防止已清理的坏味道复活。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _css():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'css', 'style.css'), encoding='utf-8') as f:
        return f.read()


def test_no_font_size_override_block():
    """「统一字体大小系统」块必须已删除——它用 !important 覆盖前面的定义"""
    css = _css()
    assert '统一字体大小系统' not in css, (
        "style.css 仍有「统一字体大小系统」覆盖块,它会让后续字号改动不生效"
    )


def test_important_count_under_control():
    """!important 数量必须显著低于清理前的 92"""
    css = _css()
    count = css.count('!important')
    assert count <= 70, f"!important 有 {count} 处,清理后应 <= 70(清理前 92)"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_css_contract.py -v
```

期望：两个都 FAIL（覆盖块还在，`!important` 92 处）。

- [ ] **Step 3: 逐条把覆盖块的值合并回原始规则**

对 `:1318-1434` 里的**每一条**，找到该选择器在前面的原始定义，把 `font-size` 写进去（**用覆盖块的值**），然后从覆盖块删掉这一条。

逐条对照表（值取自 `:58-63` 的变量）：

| 选择器 | 合并后的 font-size | 原始定义位置（用 grep 定位） |
|---|---|---|
| `.navbar-brand` | `1.25rem` | `grep -n '^\.navbar-brand' style.css` |
| `.nav-link` | `0.9375rem` | `:148` 附近 |
| `.card-header`, `.card-header h5` | `0.9375rem` | grep `.card-header` |
| `.form-label` | `0.875rem` | `:902` 附近 |
| `.form-control`, `.form-select` | `0.9375rem` | `:877` 附近 |
| `.btn` | `0.9375rem` | `:622` |
| `.btn-sm` | `0.875rem` | `:700` |
| `.task-card h6` / `.badge` / `.progress-detail` | `0.9375rem` / `0.75rem` / `0.875rem` | grep 各自 |
| `.table` / `.table th` / `.table small` | `0.9375rem` / `0.875rem` / `0.875rem` | grep `.table` |
| `.config-section h3` | `1rem` | grep |
| `.progress-bar` | `0.875rem` | `:429`（替换掉原有的 `font-size: 0.85rem`）|
| `.badge`, `.status-badge` | `0.75rem` | grep |
| `.modal-title` / `.modal-body` | `1.125rem` / `0.9375rem` | grep |
| `.page-link` | `0.875rem` | grep |
| `.alert` | `0.9375rem` | `:718` |
| `h3` / `h4,h5,h6` | `1.125rem` / `0.9375rem` | grep 或新建规则 |
| `small` / `code` | `0.875rem` / `0.875rem` | 新建规则（原先没有） |

**没有原始定义的选择器**（如 `h3`、`small`、`code`），在 `:64`（`:root` 结束）之后的「Global Styles」区块新建一条**不带 `!important`** 的规则。

全部合并完后，删除 `:1318-1434` 整块，包括那个 `/* ======== 统一字体大小系统 ======== */` 注释头。

**注意 `:1436-1445` 的 `.form-group-label` 和 `:1447` 起的 `.form-text` 不属于这一块，保留。**

- [ ] **Step 4: 跑测试 + 视觉对比**

```bash
uv run pytest tests/test_css_contract.py -v
uv run pytest tests/ -q
```

然后硬刷新浏览器（Ctrl+Shift+R），重新截 4 张图，与 `docs/images/phase2-baseline/` 逐张比对。

**期望：肉眼无差异。** 如果某处字号明显变了，说明某条合并漏了或值取错了，回到 Step 3 查那一条。

再跑一次 Task 1 Step 3 的控制台脚本，`formControlHeight` 和 `fontSize` 应与基线一致（43.7 / 15px）。

- [ ] **Step 5: 提交**

```bash
git add static/css/style.css tests/test_css_contract.py
git commit -m "refactor(css): 删除自我覆盖的字号块,值合并回原始规则

style.css:1318-1434 用 !important 重新声明了前面已定义的选择器:
.form-label 在 902 行是 .9rem、在 1338 行变 .875rem!important;
.nav-link 在 148 行是 .95rem、在 1327 行变 .9375rem!important;
.progress-bar 在 429 和 1388 两处定义。

结果是改前面的规则不生效——这是后续密度调整的拦路虎。本次把覆盖块的
值(即当前实际生效的值)合并回各自的原始规则,再整块删除。

视觉表现不变,已用 1600x1000 四页截图对比确认。"
```

**Risks:**
- **必须以覆盖块的值为准**，不是原始规则的值。取错会让页面字号肉眼可见地变化
- `.progress-bar` 在 `:429` 原本有 `font-size: 0.85rem`，要**替换**成 `0.875rem`（覆盖块的值），不是两条并存
- 合并过程中容易漏条。对照表逐行打勾，别凭记忆

---

## Task 3：清理死代码、重复规则与别名污染

**Files:**
- Modify: `static/css/style.css`（多处）
- Test: `tests/test_css_contract.py`（追加）

**Interfaces:**
- Consumes: Task 2 完成
- Produces: 无

- [ ] **Step 1: 追加契约测试**

```python
def test_no_duplicate_universal_selector():
    """三条独立的 *{} 规则应合并为一条"""
    css = _css()
    matches = re.findall(r'^\*\s*\{', css, re.M)
    assert len(matches) <= 1, f"发现 {len(matches)} 条顶层 *{{}} 规则,应合并为 1 条"


def test_no_fake_color_aliases():
    """amber/warm/copper 三个别名全指向青绿,是误导性死名"""
    css = _css()
    for alias in ('--color-accent-amber', '--color-accent-warm', '--color-accent-copper'):
        assert alias not in css, f"{alias} 是指向青绿的假别名,应已替换为真实语义名"


def test_text_center_does_not_set_color():
    """纯布局类不该管颜色"""
    css = _css()
    match = re.search(r'\.text-center\s*\{([^}]*)\}', css)
    if match:
        assert 'color' not in match.group(1), (
            ".text-center 是布局类,不该设 color——它靠规则顺序才侥幸没压掉 .text-danger"
        )
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_css_contract.py -v
```

期望：三个新测试 FAIL。

- [ ] **Step 3: 合并三条 `*{}` 规则**

`:67`、`:1217`、`:1243` 三处。把 `:1217` 和 `:1243` 的声明**合并进** `:67` 那条，删除后两处。

合并后形如（具体声明以你读到的实际内容为准，不要凭这里的示例照抄）：

```css
* {
    box-sizing: border-box;
    /* 合并自原 :1217 和 :1243 的声明 */
}
```

> `:1243` 那条含全局 `transition-duration: .3s`。**本任务只做合并，不改值** —— 改动画是 Task 13 的事，一次只做一件事。

- [ ] **Step 4: 替换假别名**

`:28-30` 定义了三个指向青绿的别名：

```css
    --color-accent-amber:  var(--color-accent);
    --color-accent-warm:   var(--color-accent-hover);
    --color-accent-copper: var(--color-accent-strong);
```

先查它们的实际引用点：

```bash
grep -rn "color-accent-amber\|color-accent-warm\|color-accent-copper" static/ templates/
```

把每个引用替换成它指向的真名（`--color-accent` / `--color-accent-hover` / `--color-accent-strong`），然后删掉 `:27-30` 这四行（含注释）。

**已知引用点**：`static/js/map.js:147-150` 的四至符号用了 `var(--color-accent-warm)`。改成 `var(--color-accent-hover)`。

> **提示：这四行会在 Task 10 Step 5 被整段重写掉**（`updateBoundsInfo` 换成 N/S/E/W 网格，那些 `<span>` 不复存在）。这里仍然要改，原因是 Task 3 的测试 `test_no_fake_color_aliases` 断言全库不含该别名——不改这里，Task 3 到 Task 10 之间的每一次提交都是红灯。改动量只有 4 处 `var()` 替换，不值得为省这一步打乱任务边界。

- [ ] **Step 5: 删掉 .text-center 的颜色强制**

`:795-797`。删掉其中的 `color: ... !important` 声明，只保留 `text-align: center`。

> 这修掉一个隐患：`history.js:56` 的 `class="text-center text-danger"` 现在靠 `.text-danger`(`:799`) 排在 `.text-center`(`:795`) 之后才侥幸显示红色。调一下规则顺序就会静默变色。

- [ ] **Step 6: 删死代码**

逐条确认后删除：

| 位置 | 内容 | 删除依据 |
|---|---|---|
| `:1168-1170` | `.mb-3 { ... }` | 被 `:915` 的 `!important` 完全压死，改它没有任何效果 |
| `:1185-1190` | `.row { ... }` | 与前面的 `.row` 规则重复 |
| `:50` | `--shadow-glow: none;` | grep 确认零引用后删除 |
| `:1268` | `.leaflet-control-layers-toggle { ... }` | `map.js` 从未调用 `L.control.layers`，该元素不存在 |

**删每一条之前先 grep 确认零引用**：

```bash
grep -rn "shadow-glow" static/ templates/
grep -rn "control-layers" static/ templates/
```

- [ ] **Step 7: 修「发光只删了一半」**

`:50` 的 `--shadow-glow: none` 注释写着"去发光"，但 `:345-354` 的 `pulse` 关键帧还硬编码着：

```css
box-shadow: 0 0 20px 5px rgba(59, 130, 246, 0.3);
```

这条蓝色发光与「去发光」的设计意图矛盾，且颜色(#3b82f6 蓝)不在当前调色板里。改为用品牌色的柔和版：

```css
box-shadow: 0 0 0 4px var(--color-accent-muted);
```

> `pulse` 被 `map.js:66` 用在框选成功后的按钮反馈上。改完后画个框验证一下动画还在、但不刺眼。

- [ ] **Step 8: 跑测试 + 视觉对比**

```bash
uv run pytest tests/test_css_contract.py -v
uv run pytest tests/ -q
```

硬刷新，重新截 4 张图与基线比对。**期望：除 pulse 动画的发光颜色外，无差异。**

- [ ] **Step 9: 提交**

```bash
git add static/css/style.css static/js/map.js tests/test_css_contract.py
git commit -m "refactor(css): 清理死代码、重复规则与假别名

- 合并三条独立的 *{} 规则(原 :67/:1217/:1243)
- 删掉 amber/warm/copper 三个全指向青绿的假别名,引用点换成真名
- 删掉 .text-center 的 color!important(纯布局类不该管颜色,它现在
  靠规则顺序才侥幸没压掉 .text-danger)
- 删死代码::1168 .mb-3(被 :915 的 !important 压死)、:1185 .row 重复、
  :50 --shadow-glow 零引用、:1268 .leaflet-control-layers-toggle
  (map.js 从未调用 L.control.layers)
- 修「发光只删了一半」:--shadow-glow 设成 none 了,但 pulse 关键帧
  还硬编码着 rgba(59,130,246,.3) 蓝色发光,换成品牌色柔和版

视觉表现不变(pulse 发光颜色除外),已用四页截图对比确认。"
```

---

## Task 4：修复 select 下拉箭头丢失

**Files:**
- Modify: `static/css/style.css`（`.form-select` 规则）
- Test: `tests/test_css_contract.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: 无

**Rationale:** 这是 C1 里唯一一个**改变视觉表现**的任务，因为它修的是真 bug。`.form-select` 被 `background:` 简写覆盖，连带清掉了 Bootstrap 的下拉箭头 SVG。**当前所有下拉框都没有三角指示符**（实测 `getComputedStyle(...).backgroundImage === "none"`）。

- [ ] **Step 1: 写测试**

```python
def test_form_select_does_not_kill_bootstrap_arrow():
    """
    .form-select 不能用 background 简写——那会连带重置 Bootstrap 的
    下拉箭头 background-image,导致所有 select 没有三角指示符。
    """
    css = _css()
    for match in re.finditer(r'\.form-select[^{]*\{([^}]*)\}', css):
        body = match.group(1)
        assert not re.search(r'(^|;)\s*background\s*:', body), (
            ".form-select 用了 background 简写,会清掉 Bootstrap 的下拉箭头。"
            "改用 background-color。"
        )
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_css_contract.py::test_form_select_does_not_kill_bootstrap_arrow -v
```

- [ ] **Step 3: 把 background 简写改成 background-color**

```bash
grep -n "form-select" static/css/style.css
```

找到所有 `.form-select` 规则里的 `background:` 简写，逐个改成 `background-color:`。

> 如果某条规则是 `.form-control, .form-select { background: ... }` 这种合并选择器，**不能只改一半**——`.form-control` 用 `background` 简写是无害的（它本来就没有背景图），但为了一致性一并改成 `background-color` 更安全。

- [ ] **Step 4: 验证箭头回来了**

硬刷新后在控制台跑：

```javascript
console.log(getComputedStyle(document.querySelector('.form-select')).backgroundImage);
```

期望：输出一个 `url("data:image/svg+xml,...")`，**不再是 `"none"`**。

肉眼确认首页「下载类型」「地图样式」「输出格式」三个下拉框右侧都出现了灰色小三角。

- [ ] **Step 5: 跑全量并提交**

```bash
uv run pytest tests/ -q
git add static/css/style.css tests/test_css_contract.py
git commit -m "fix(css): 修复所有 select 下拉箭头丢失

.form-select 用 background 简写覆盖背景色,连带把 Bootstrap 的
下拉箭头 background-image 一起重置了。实测 backgroundImage 为 none,
即当前所有下拉框都没有三角指示符,看起来像普通输入框。

改用 background-color 保留背景图。这是 C1 清理里唯一改变视觉表现的
一处,因为它修的是 bug 而非坏味道。"
```

---

# 第二部分：A1-A7 —— 视觉修复

## Task 5：修正进度条状态色语义反转

**Files:**
- Modify: `static/js/tasks.js:504-510`（删除 `getProgressColor`）、`:244`、`:432`、`static/js/history.js:296`
- Modify: `static/css/style.css`（补 `.progress-bar.bg-primary`）
- Test: `tests/test_css_contract.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: 进度条颜色由 `getStatusColor(task.status)` 决定。Task 6/7 依赖。

**Rationale:** `getProgressColor(progress)`（`:504-510`）：`>=100 success`、`>=75 info`、`>=50 primary`、`>=25 warning`、**其余 `return 'danger'`**。刚启动的健康任务立刻是红色。而绿色**永远不会出现** —— `handleTaskCompleted`(`:455-469`) 在到 100% 之前就 `card.remove()` 了。

**关键约束（评审漏掉的）：** `getStatusColor(:492-502)` 把 `running` 映射成 `'primary'`，但 `style.css` 只有 `.progress-bar.bg-success/info/warning/danger` 四条覆盖（`:441-456`），**没有 `.bg-primary`**。直接替换会让运行中的进度条变成 Bootstrap 默认蓝 `#0d6efd`，跳出配色系统。必须补一条 CSS。

- [ ] **Step 1: 写测试**

```python
def test_progress_bar_has_primary_override():
    """
    getStatusColor 把 running 映射成 primary,但 style.css 原本只覆盖了
    success/info/warning/danger。缺 bg-primary 会让运行中的进度条变成
    Bootstrap 默认蓝 #0d6efd,跳出配色系统。
    """
    css = _css()
    assert '.progress-bar.bg-primary' in css, (
        "缺少 .progress-bar.bg-primary 覆盖,运行中的进度条会是 Bootstrap 默认蓝"
    )
```

再新建 `tests/test_tasks_js_contract.py`：

```python
"""
tasks.js / history.js behavioural contract tests (text-level).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _js(name):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'js', name), encoding='utf-8') as f:
        return f.read()


def test_progress_color_by_status_not_percentage():
    """
    进度条颜色必须由任务状态决定,不能由百分比决定。
    原实现 getProgressColor(progress) 让刚启动的健康任务显示红色。
    """
    src = _js('tasks.js')
    assert 'function getProgressColor(' not in src, (
        "getProgressColor 应删除,改用 getStatusColor(status)"
    )
    assert 'getProgressColor(' not in src, "tasks.js 仍有 getProgressColor 调用"

    hist = _js('history.js')
    assert 'getProgressColor(' not in hist, "history.js 仍有 getProgressColor 调用"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_css_contract.py::test_progress_bar_has_primary_override tests/test_tasks_js_contract.py -v
```

- [ ] **Step 3: 补 CSS 覆盖**

在 `style.css` 的 `.progress-bar.bg-danger`（`:453` 附近）之后追加：

```css
.progress-bar.bg-primary {
    background: var(--color-accent-strong) !important;
}

.progress-bar.bg-secondary {
    background: var(--color-text-muted) !important;
}

.progress-bar.bg-dark {
    background: var(--color-bg-tertiary) !important;
}
```

> 三条都要补 —— `getStatusColor` 会返回 `secondary`（pending）和 `dark`（cancelled），它们同样没有覆盖。

- [ ] **Step 4: 删掉 getProgressColor，改用 getStatusColor**

`static/js/tasks.js:504-510` **整个函数删除**。

然后改三个调用点。**`tasks.js:432` 是 Socket.IO 增量刷新路径，运行时主要走这条，最容易漏**：

`tasks.js:432`：
```javascript
        progressBar.className = `progress-bar bg-${getStatusColor(task.status)}`;
```

`tasks.js:244` 与 `history.js:296` 同样把 `getProgressColor(progress)` 换成 `getStatusColor(task.status)`。三处作用域都已持有 `task` 对象，无需额外传参。

> `history.js` 有自己的 `getStatusText`（`:209`），但**没有** `getStatusColor`。检查一下：如果 `history.js:296` 调用的是本文件内不存在的函数，需要在 `history.js` 里也定义一份，或者直接内联映射。**注意 `history.js:209` 的 `getStatusText` 只映射三态、其余 fallback 回英文原值** —— 这就是历史页状态列中英混杂的根源，但修它属于 Task 12 的范围，本任务不要顺手改。

- [ ] **Step 5: 跑测试**

```bash
uv run pytest tests/ -q
```

- [ ] **Step 6: 实测验证**

起服务，创建一个任务，观察：

- **期望：刚启动（进度 0-10%）的进度条是青绿色（`--color-accent-strong`），不是红色**
- 暂停任务 → 进度条变黄（warning）
- 进度条颜色在整个下载过程中**不随百分比跳变**

- [ ] **Step 7: 提交**

```bash
git add static/js/tasks.js static/js/history.js static/css/style.css tests/
git commit -m "fix(ui): 进度条颜色改由任务状态决定,不再由百分比决定

getProgressColor(progress) 的映射是反的:>=25 才给 warning,其余一律
return 'danger'——刚启动的健康任务立刻显示红色。而绿色永远不会出现,
因为 handleTaskCompleted 在到 100% 之前就 card.remove() 了。

改用同文件已有的 getStatusColor(status)。三个调用点全改,包括
tasks.js:432 这个 Socket.IO 增量刷新的实际主路径。

顺带补 .progress-bar.bg-primary/secondary/dark 三条 CSS 覆盖:
getStatusColor 会返回这三个值,而 style.css 原本只覆盖了
success/info/warning/danger,缺的会回落到 Bootstrap 默认色。"
```

**Risks:**
- 漏改 `tasks.js:432` 是最可能的失误——它是 Socket.IO 路径，页面初次渲染看不出问题，任务跑起来才暴露
- `history.js` 可能没有 `getStatusColor`，替换前先 grep 确认

---

## Task 6：任务失败后保留卡片并显示原因

**Files:**
- Modify: `static/js/tasks.js:471-490`（`handleTaskFailed`）
- Test: `tests/test_tasks_js_contract.py`（追加）

**Interfaces:**
- Consumes: Task 5
- Produces: 无

**Rationale:** `handleTaskFailed`(`:471-490`) 直接 `card.remove()`，错误信息只 `console.error`。用户盯着 63% 看，卡片突然消失，零提示。

- [ ] **Step 1: 写测试**

```python
def test_failed_task_card_is_not_removed():
    """任务失败时不能直接删卡片,用户会以为任务凭空消失"""
    src = _js('tasks.js')
    start = src.index('function handleTaskFailed(')
    end = src.index('function getStatusColor(')
    body = src[start:end]

    assert 'card.remove()' not in body, (
        "handleTaskFailed 仍在删除卡片,用户看不到失败原因"
    )
    assert 'showToast(' in body, "handleTaskFailed 应弹出常驻错误提示"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_tasks_js_contract.py::test_failed_task_card_is_not_removed -v
```

- [ ] **Step 3: 改写 handleTaskFailed**

把 `tasks.js:471-490` 整个函数替换为：

```javascript
function handleTaskFailed(taskId, taskType, errorMessage) {
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (!task) return;

    task.status = 'failed';
    task.error_message = errorMessage;
    // 注意：不从 activeTasks 删除，卡片要留在页面上让用户看到失败原因。
    // 用户点「移除」按钮时才真正清掉。

    const card = document.getElementById(`task-${key}`);
    if (card) {
        const progressBar = card.querySelector('.progress-bar');
        if (progressBar) {
            progressBar.className = 'progress-bar bg-danger';
        }

        const badge = card.querySelector('.status-badge, .badge');
        if (badge) {
            badge.className = 'badge bg-danger status-badge';
            badge.textContent = getStatusText('failed');
        }

        if (errorMessage && !card.querySelector('.task-error')) {
            const errBox = document.createElement('div');
            errBox.className = 'task-error';
            errBox.textContent = errorMessage;   // textContent 防 XSS
            card.appendChild(errBox);
        }
    }

    if (errorMessage) {
        console.error(`Task ${taskId} failed: ${errorMessage}`);
        // duration=0 → 常驻不自动消失（见 ui.js）
        showToast(`任务失败：${errorMessage}`, 'danger', { duration: 0 });
    }
}
```

- [ ] **Step 4: 加错误框样式**

在 `style.css` 的 `.task-card` 规则附近追加：

```css
.task-error {
    margin-top: 8px;
    padding: 6px 8px;
    border-radius: var(--radius-sm);
    background: var(--color-danger-bg);
    border: 1px solid var(--color-danger);
    color: var(--color-text-primary);
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    word-break: break-word;
}
```

- [ ] **Step 5: 核对 showToast 的签名**

```bash
grep -n "function showToast" -A 12 static/js/ui.js
```

**确认第三个参数确实接受 `{duration: 0}` 且 0 表示不自动消失。** 如果签名不同（比如是 `showToast(msg, type, duration)`），按实际签名调整调用。**不要凭这里的写法照抄。**

- [ ] **Step 6: 跑测试 + 实测**

```bash
uv run pytest tests/ -q
```

实测：制造一个必然失败的任务（例如把配置页代理改成一个不通的地址，然后建一个地图瓦片任务）。

- **期望：卡片保留、变红、显示错误文本，toast 常驻不消失**

- [ ] **Step 7: 提交**

```bash
git add static/js/tasks.js static/css/style.css tests/test_tasks_js_contract.py
git commit -m "fix(ui): 任务失败后保留卡片并显示原因,不再静默消失

handleTaskFailed 原本直接 card.remove(),错误信息只 console.error。
用户盯着 63% 看,卡片突然没了,什么提示都没有。

改为:卡片留在页面、进度条转红、状态徽章改「失败」、卡片内展开错误
文本(textContent 防 XSS),并弹常驻 toast(duration=0 不自动消失)。

暂不加「重试」按钮:三个 manager 的 start_task 都要求
status in ('pending','paused'),对 failed 调用会抛 ValueError。
重试需要先改后端状态机,属于第二档范围。"
```

**Risks:**
- **不要加「重试」按钮。** `task_manager.py:356`、`dem_task_manager.py:160`、`contour_task_manager.py:154` 三处都硬性要求 `status in ('pending','paused')`，对 failed 调用直接抛 ValueError
- 卡片不再从 `activeTasks` 删除，意味着 `renderActiveTasks` 会持续渲染它。确认「移除」按钮能真正清掉（如果现有卡片没有移除按钮，本任务需要补一个）

---

## Task 7：进度条百分比改为覆盖层

**Files:**
- Modify: `static/css/style.css:415-439`、`static/js/tasks.js:243-250`、`:431`、`static/js/history.js:301-307`

**Interfaces:**
- Consumes: Task 5
- Produces: 无

**Rationale:** 百分比数字是进度条**自身**的子元素，且 `.progress` 有 `overflow: hidden`（`:419`）→ progress=0 时数字完全消失，0-3% 被裁掉一半。文字色继承 Bootstrap 的 `#fff`，实测对比度：warning **1.67:1**、success 1.92、info 2.54、danger 2.77（WCAG AA 要求 4.5）。

- [ ] **Step 1: 改 CSS**

`style.css:415-422` 的 `.progress` 加 `position: relative`：

```css
.progress {
    height: 8px;
    border-radius: 999px;
    background: var(--color-bg-tertiary);
    overflow: hidden;
    border: none;
    box-shadow: none;
    position: relative;
}
```

`.progress-bar`（`:429-439`）删掉 `display:flex` / `align-items` / `justify-content` / `overflow:hidden`（它不再需要居中子元素）：

```css
.progress-bar {
    transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1);
}
```

新增覆盖层规则：

```css
.progress__label {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-size: 0.875rem;
    font-weight: 700;
    color: #0b1220;
    z-index: 1;
    pointer-events: none;
}
```

> **不要用 `mix-blend-mode: difference`。** 已实测是负优化：over `bg-primary` 对比度从 4.50 掉到 1.48，over `bg-danger` 从 2.77 掉到 1.88，五档里只有一档过 AA。显式深色 `#0b1220` 在全部五档饱和填充上都过 4.5:1。
>
> `font-variant-numeric: tabular-nums` 是「等宽数字」，让 3% → 13% 时数字不左右抖动。

**注意 `.progress` 高度只有 8px**（`:416`），放不下 0.875rem 的文字。检查两种情况：
- 卡片内的细进度条（8px）：**不显示数字**，数字放在进度条下方的 `.progress-detail` 里（那里已经有 `已下载: X / Y` 文本）
- 弹窗内的进度条（`.modal .progress` 是 22px，`:424-427`）：显示覆盖层数字

据此，`.progress__label` 的基础规则**默认隐藏**，只在弹窗里显示：

```css
.progress__label {
    /* ...上面那些声明... */
    display: none;
}

.modal .progress__label {
    display: flex;
}
```

**为什么用「默认隐藏 + 弹窗显示」而不是把整条规则限定成 `.modal .progress__label`：** Step 2 的渲染代码在卡片和弹窗里都会输出 `<span class="progress__label">`（同一段渲染函数复用）。如果只给 `.modal` 后代写样式，卡片里那个 span 就是无样式的裸元素——文字不会绝对定位，而是直接挤进卡片布局里。默认 `display:none` 才能保证卡片内不显示。

- [ ] **Step 2: 改 JS 渲染点**

三处：`tasks.js:243-250`（创建卡片）、**`tasks.js:431`（Socket.IO 路径，`progressBar.textContent = ...`，漏改会出重复标签）**、`history.js:301-307`。

把原本往 `.progress-bar` 里塞文字的写法，改成在 `.progress` 容器内另加一个 `<span class="progress__label">`：

```javascript
// 渲染时
`<div class="progress">
    <div class="progress-bar bg-${getStatusColor(task.status)}" role="progressbar"
         style="width: ${progress}%" aria-valuenow="${progress}"
         aria-valuemin="0" aria-valuemax="100"></div>
    <span class="progress__label">${progress}%</span>
</div>`
```

`tasks.js:431` 那处改为更新 label 而不是 bar 的 textContent：

```javascript
        const label = card.querySelector('.progress__label');
        if (label) label.textContent = `${progress}%`;
```

**并删掉原来的 `progressBar.textContent = \`${progress}%\`;`。**

- [ ] **Step 3: 验证对比度**

硬刷新后，在控制台跑：

```javascript
function luminance(hex) {
  const c = hex.replace('#','').match(/../g).map(h => {
    const v = parseInt(h,16)/255;
    return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
  });
  return 0.2126*c[0] + 0.7152*c[1] + 0.0722*c[2];
}
function contrast(a, b) {
  const [l1,l2] = [luminance(a), luminance(b)].sort((x,y)=>y-x);
  return ((l1+0.05)/(l2+0.05)).toFixed(2);
}
const text = '#0b1220';
['#34d399','#f87171','#fbbf24','#60a5fa','#14b8a6'].forEach(bg =>
  console.log(bg, '→', contrast(text, bg))
);
```

**期望：五个值全部 ≥ 4.5。**

- [ ] **Step 4: 跑测试并提交**

```bash
uv run pytest tests/ -q
git add static/css/style.css static/js/tasks.js static/js/history.js
git commit -m "fix(ui): 进度条百分比改为绝对定位覆盖层,修复不可读

数字原本是 .progress-bar 自身的子元素,而 .progress 有 overflow:hidden,
导致 progress=0 时数字完全消失、0-3% 被裁一半。文字色继承 Bootstrap 的
#fff,实测对比度 warning 1.67:1 / success 1.92 / info 2.54 / danger 2.77,
全部达不到 WCAG AA 的 4.5。

改为 .progress 内的绝对定位覆盖层,显式深色文字 #0b1220,五档填充色上
实测均 >= 4.5。加 tabular-nums 让数字变化时不左右抖动。

没有用 mix-blend-mode:difference——实测是负优化(over bg-primary 从
4.50 掉到 1.48)。

8px 高的卡片内进度条放不下文字,数字仍走下方 .progress-detail;
覆盖层只用在 22px 的弹窗进度条上。"
```

---

## Task 8：深色主题补完

**Files:**
- Modify: `templates/base.html:2`、`static/css/style.css`
- Test: `tests/test_css_contract.py`（追加）

**Rationale:** 实测 `--bs-body-bg: #fff`、`--bs-tertiary-bg: #f8f9fa` —— Bootstrap 仍处于亮色模式。表现：文件选择按钮灰白、number 微调箭头白色、select 弹层白底、取色器色块被挤成 18.8×15.3px。

- [ ] **Step 1: 写测试**

```python
def test_bootstrap_dark_theme_is_enabled():
    """
    Bootstrap 5.3 的 [data-bs-theme=dark] 自带 color-scheme: dark,
    浏览器据此把原生控件(select 弹层、number 微调箭头、文件选择按钮)
    渲染成深色。缺了它,深色界面上会漏出白色原生控件。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'templates', 'base.html'), encoding='utf-8') as f:
        html = f.read()
    assert 'data-bs-theme="dark"' in html, (
        'base.html 的 <html> 标签应加 data-bs-theme="dark"'
    )
```

- [ ] **Step 2: 跑测试确认失败，然后改 base.html**

`templates/base.html:2`：

```html
<html lang="zh-CN" data-bs-theme="dark">
```

- [ ] **Step 3: 修取色器尺寸**

`.form-control-color` 继承了 `.form-control` 的 padding，色块被挤成 18.8×15.3px。在 `style.css` 追加：

```css
.form-control-color {
    padding: 2px;
    width: 36px;
    min-width: 36px;
}
```

- [ ] **Step 4: 实测四项**

硬刷新后逐项确认：

| 检查项 | 位置 | 期望 |
|---|---|---|
| select 弹层 | 首页「下载类型」，点开 | 深色背景，不是白底 |
| number 微调箭头 | 首页「最小缩放级别」 | 深色，不是白色方块 |
| 文件选择按钮 | 「下载类型」→「本地高程切片」 | 深色，不是灰白 |
| 取色器 | 「下载类型」→「等高线瓦片」→「背景」 | 色块 ≥ 30px 宽，看得出颜色 |

控制台确认变量已翻深：

```javascript
const cs = getComputedStyle(document.documentElement);
console.log('--bs-body-bg:', cs.getPropertyValue('--bs-body-bg'));
console.log('--bs-tertiary-bg:', cs.getPropertyValue('--bs-tertiary-bg'));
```

期望：都是深色值，不再是 `#fff` / `#f8f9fa`。

- [ ] **Step 5: 全页面回归**

`data-bs-theme="dark"` 是全局开关，会影响**所有** Bootstrap 组件。逐页硬刷新检查：首页、历史页、配置页、任务详情弹窗。

**重点看表格、徽章、模态框、分页** —— 这些组件的 Bootstrap 默认色会随主题切换。如果某处变得比之前难看，在 `style.css` 里补针对性覆盖，**不要回退 `data-bs-theme`**。

- [ ] **Step 6: 提交**

```bash
git add templates/base.html static/css/style.css tests/test_css_contract.py
git commit -m "fix(ui): 开启 Bootstrap 深色主题,修复原生控件漏白

<html> 加 data-bs-theme=\"dark\"。这一个属性同时解决两件事:
Bootstrap 5.3 的 [data-bs-theme=dark] 自带 color-scheme: dark,浏览器
立刻把 select 弹层、number 微调箭头渲染成深色;同时把 --bs-tertiary-bg
翻深,修掉文件选择按钮的灰白底。

实测改前 --bs-body-bg 是 #fff、--bs-tertiary-bg 是 #f8f9fa,即整个
Bootstrap 仍在亮色模式下,自定义 CSS 只是在上面刷了一层深色漆。

顺带修 .form-control-color:它继承 .form-control 的 padding,色块被挤成
18.8x15.3px,几乎看不出颜色。"
```

---

## Task 9：Leaflet 控件主题化

**Files:**
- Modify: `static/css/style.css`（`:1289` 之后追加）、`templates/base.html` 或 `static/js/map.js`（汉化）

**Rationale:** `style.css:1266-1289` 只覆盖了 4 个 Leaflet 选择器。首页最核心的交互入口（矩形绘制按钮）是 Leaflet 出厂白盒，在深色地图上刺眼，提示条还是英文。

- [ ] **Step 1: 追加 CSS**

在 `style.css` 原 Leaflet 段落之后追加：

```css
/* Leaflet 控件深色化 */
.leaflet-bar,
.leaflet-draw-toolbar {
    background-color: var(--color-bg-secondary) !important;
    border: 1px solid var(--color-border) !important;
    box-shadow: var(--shadow-md) !important;
}

.leaflet-bar a,
.leaflet-draw-toolbar a {
    background-color: transparent !important;
    color: var(--color-text-primary) !important;
    border-bottom-color: var(--color-border) !important;
}

.leaflet-draw-toolbar a {
    filter: invert(1) brightness(1.4);
}

.leaflet-bar a:hover,
.leaflet-draw-toolbar a:hover {
    background-color: rgba(255, 255, 255, 0.08) !important;
}

.leaflet-draw-actions a {
    background-color: var(--color-bg-tertiary) !important;
    border-left-color: var(--color-border) !important;
    color: var(--color-text-primary) !important;
}

.leaflet-control-attribution {
    background: rgba(12, 13, 16, 0.75) !important;
    color: var(--color-text-muted) !important;
}

.leaflet-control-attribution a {
    color: var(--color-text-secondary) !important;
}
```

**两个必须避开的坑：**

1. **必须用 `background-color` 而不是 `background` 简写。** 简写会把 `leaflet.draw.css` 的 `background-image: url(spritesheet.png)` 一并重置，结果是**几个没有图标的空白按钮**。
2. **深色底放在 `.leaflet-draw-toolbar` 容器上，`<a>` 背景设 transparent 再对 `<a>` 反色。** 如果给 `<a>` 同时设深色背景和 `filter: invert(1)`，两条规则互相抵消，净效果等于什么都没改。

- [ ] **Step 2: 汉化绘制提示**

Leaflet.draw 的文案通过 `L.drawLocal` 配置，必须在 `new L.Control.Draw(...)` **之前**设置。在 `static/js/map.js` 的 `initMap` 函数内、`const drawControl = new L.Control.Draw({` 之前插入：

```javascript
    // 页面 lang="zh-CN" 但 Leaflet.draw 的提示条默认是英文
    if (window.L && L.drawLocal) {
        L.drawLocal.draw.handlers.rectangle.tooltip.start = '点击并拖动绘制矩形';
        L.drawLocal.draw.handlers.simpleshape.tooltip.end = '松开鼠标完成绘制';
        L.drawLocal.draw.toolbar.actions.title = '取消绘制';
        L.drawLocal.draw.toolbar.actions.text = '取消';
        L.drawLocal.draw.toolbar.buttons.rectangle = '绘制矩形选区';
        L.drawLocal.edit.toolbar.actions.save.title = '保存修改';
        L.drawLocal.edit.toolbar.actions.save.text = '保存';
        L.drawLocal.edit.toolbar.actions.cancel.title = '取消修改';
        L.drawLocal.edit.toolbar.actions.cancel.text = '取消';
        L.drawLocal.edit.toolbar.buttons.edit = '编辑选区';
        L.drawLocal.edit.toolbar.buttons.remove = '删除选区';
        L.drawLocal.edit.handlers.edit.tooltip.text = '拖动顶点或图形以修改选区';
        L.drawLocal.edit.handlers.remove.tooltip.text = '点击选区将其删除';
    }
```

- [ ] **Step 3: 实测（图标是本任务最容易搞砸的地方）**

硬刷新后确认：

- [ ] 绘制工具条背景是深色，与界面一致
- [ ] **工具条上的图标仍然可见**（矩形、编辑、垃圾桶三个图标）—— 如果变成空白按钮，说明你用了 `background` 简写，回到 Step 1
- [ ] 悬停有反馈
- [ ] 点矩形工具，提示条显示中文「点击并拖动绘制矩形」
- [ ] 进入编辑模式，「保存」「取消」按钮是中文且深色
- [ ] 右下角 attribution 不再是刺眼白底

- [ ] **Step 4: 提交**

```bash
git add static/css/style.css static/js/map.js
git commit -m "fix(ui): Leaflet 控件深色化并汉化绘制提示

style.css 原本只覆盖了 4 个 Leaflet 选择器,.leaflet-draw-toolbar /
.leaflet-bar / .leaflet-control-attribution 一条没有。首页最核心的交互
入口(矩形绘制按钮)是 Leaflet 出厂白盒,在深色地图上刺眼。

两个坑已避开:
1. 用 background-color 而非 background 简写——简写会重置
   leaflet.draw.css 的 background-image:url(spritesheet.png),
   结果是几个没有图标的空白按钮
2. 深色底放容器上、<a> 设 transparent 再反色——若给 <a> 同时设深色
   背景和 filter:invert(1),两条互相抵消,净效果为零

页面 lang=\"zh-CN\" 但 Leaflet.draw 提示条是英文,一并汉化。"
```

---

## Task 10：密度令牌落地

**Files:**
- Modify: `static/css/style.css`（`:root`、`.form-control`、`.card-body`、`.card-header`、删 `.config-section` 覆盖）、`static/js/map.js:138-152`

**Interfaces:**
- Consumes: Task 2（字号覆盖块已删除）
- Produces: 无

**Rationale:** 实测控件高 **43.7px**（QGIS/ArcGIS Pro 是 22-26px，VS Code 输入框 26px）。右栏卡片实测 **800.3px**，而 1366×768 的可用高度只有 **676px** —— 提交按钮在折叠线以下。

- [ ] **Step 1: 定义密度令牌**

在 `style.css:5-64` 的 `:root` 里追加：

```css
    /* 控件密度 */
    --ctl-h: 28px;
    --ctl-pad-y: 4px;
    --ctl-pad-x: 8px;
```

> **必须先定义再引用。** 未定义的自定义属性会让引用它的 `height`/`padding` 声明整条失效（退回 auto），表现为"改了没反应"。

- [ ] **Step 2: 收紧表单控件**

`style.css:877-887` 的 `.form-control`：

```css
.form-control,
.form-select {
    padding: var(--ctl-pad-y) var(--ctl-pad-x);
    line-height: 20px;
    font-size: 0.875rem;
    /* ...保留原有的 background-color / border / color 等声明... */
}
```

> **Task 2 已经删掉了 `:1342-1345` 那条 `!important`**，所以这里的 `font-size` 现在能生效。如果你跳过了 Task 2，这一步会改了没反应 —— 那就回去先做 Task 2。

- [ ] **Step 3: 删掉配置页的重复覆盖**

`style.css:537-546` 的 `.config-section .form-control { padding: .75rem 1rem }` 让同一控件在首页和配置页差 5px。**整条删除**，统一走全局规则。

- [ ] **Step 4: 收紧卡片内边距**

```css
.card-body { padding: 10px; }
.card-header { padding: 6px 10px; }
```

（在各自的原始规则里改，不要新增覆盖规则。）

- [ ] **Step 5: 压缩四至显示（本任务最值钱的单项，约 90px）**

`static/js/map.js:138-152` 的 `updateBoundsInfo`，把 5 行改成 2 行：

```javascript
function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    if (currentBounds) {
        const f = (v) => v.toFixed(5);
        boundsInfo.innerHTML = `
            <div class="bounds-grid">
                <span class="bounds-k">N</span><span class="bounds-v">${f(currentBounds.north)}</span>
                <span class="bounds-k">S</span><span class="bounds-v">${f(currentBounds.south)}</span>
                <span class="bounds-k">E</span><span class="bounds-v">${f(currentBounds.east)}</span>
                <span class="bounds-k">W</span><span class="bounds-v">${f(currentBounds.west)}</span>
            </div>
        `;
        boundsInfo.style.background = 'rgba(96, 165, 250, 0.10)';
        boundsInfo.style.borderColor = 'var(--color-info)';
    } else {
        boundsInfo.innerHTML = '<small>请在地图上框选下载区域</small>';
        boundsInfo.style.background = '';
        boundsInfo.style.borderColor = '';
    }
}
```

配套 CSS：

```css
.bounds-grid {
    display: grid;
    grid-template-columns: auto 1fr auto 1fr;
    gap: 2px 6px;
    align-items: baseline;
    font-family: var(--font-mono);
    font-size: 0.8125rem;
}

.bounds-k {
    color: var(--color-text-secondary);
    font-weight: 600;
}

.bounds-v {
    color: var(--color-text-primary);
    font-variant-numeric: tabular-nums;
}
```

> 顺带把 `▲▼▶◀` 换成 GIS 惯例的 `N/S/E/W`，并把小数位从 6 位减到 5 位（约 1 米精度，对选范围足够）。
>
> **注意：这一步与 Phase 1 Task 9 有交集** —— 那边给 `syncBoundsFromDrawnItems` 加了对 `updateBoundsInfo()` 的调用。函数签名没变，两边可以独立合并。

- [ ] **Step 6: 验证 1366×768 一屏放得下**

把浏览器视口调成 **1366×768**，打开首页：

```javascript
const card = document.querySelector('.index-right .card');
const rect = card.getBoundingClientRect();
const btn = document.getElementById('createTaskBtn').getBoundingClientRect();
console.log(JSON.stringify({
  cardHeight: rect.height,
  buttonBottom: btn.bottom,
  viewportHeight: window.innerHeight,
  fitsOnScreen: btn.bottom <= window.innerHeight,
  formControlHeight: document.querySelector('.form-control').getBoundingClientRect().height,
}, null, 2));
```

**期望：`fitsOnScreen: true`，`formControlHeight` ≈ 28。**

基线对照：改之前 `cardHeight` 800.3、`formControlHeight` 43.7、`fitsOnScreen` false。

- [ ] **Step 7: 提交**

```bash
git add static/css/style.css static/js/map.js
git commit -m "feat(ui): 控件密度收紧到专业工具水平,1366x768 一屏放得下

实测改前:控件高 43.7px(QGIS/ArcGIS Pro 是 22-26px、VS Code 26px),
右栏卡片 800.3px 而 1366x768 可用高度只有 676px——提交按钮在折叠线以下。

- 新增 --ctl-h/--ctl-pad-y/--ctl-pad-x 密度令牌
- .form-control 高度 43.7 -> 28px
- 删掉 .config-section .form-control 的重复覆盖(同一控件两页差 5px)
- .card-body 1rem -> 10px、.card-header .85rem 1rem -> 6px 10px
- 四至显示从 5 行压成 2 行网格(单项就值 90px),符号 ▲▼▶◀ 换成 GIS
  惯例的 N/S/E/W,小数 6 位减到 5 位(约 1m 精度)

没有动 .history-table td:该类在 DOM 里不存在(history.html:52 用的是
Bootstrap 原生 table table-hover),改了零效果。"
```

**Risks:**
- **`.history-table` 是不存在的类**，别浪费时间改它。历史表行高 62px 是「区域」列经纬度换行造成的，要压缩得让 bbox 单元格 `white-space: nowrap`
- 密度改动会影响所有页面，改完要三页都看一遍

---

## Task 11：按钮状态补齐

**Files:**
- Modify: `static/css/style.css:643-715`、`static/js/tasks.js`、`static/js/history.js`（aria-label）

**Rationale:** `.btn-success`(`:643-647`) 和 `.btn-success:hover`(`:649-652`) **两条规则的值完全相同** —— hover 是空操作。`.btn-warning`(`:654-663`)、`.btn-danger`(`:665-674`)、`.btn-info`(`:706-715`) 同样。这四个类正是任务卡上的启动/暂停/取消按钮。

另外 `btn-outline` 在 `style.css` 里**零定义**，而 `.btn{border:none}`(`:622-628`) 压掉了 Bootstrap 的边框 → `history.html:159` 的「刷新」按钮渲染成**无边框的灰色纯文字**。

- [ ] **Step 1: 补真实的 hover 状态**

把 `:649-652`、`:660-663`、`:671-674`、`:712-715` 四条空 hover 改成真实提亮：

```css
.btn-success:hover { background: #4ade80; color: #06251a; }
.btn-warning:hover { background: #fcd34d; color: #1a1206; }
.btn-danger:hover  { background: #fca5a5; color: #2a0b0b; }
.btn-info:hover    { background: #93c5fd; color: #08203f; }

.btn-success:active,
.btn-warning:active,
.btn-danger:active,
.btn-info:active,
.btn-primary:active { filter: brightness(0.9); }
```

- [ ] **Step 2: 补 outline 变体**

```css
.btn-outline-primary {
    background: transparent;
    border: 1px solid var(--color-accent-strong);
    color: var(--color-accent);
}

.btn-outline-primary:hover {
    background: var(--color-accent-muted);
    border-color: var(--color-accent);
}

.btn-outline-secondary {
    background: transparent;
    border: 1px solid var(--color-border-strong);
    color: var(--color-text-secondary);
}

.btn-outline-secondary:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--color-text-secondary);
    color: var(--color-text-primary);
}
```

> **必须显式写 `border`。** `:622` 的 `.btn { border: none }` 会吃掉 Bootstrap 的边框简写。`.btn-secondary`(`:676-681`) 已经这么补过一次 —— 作者知道这个坑，只是漏了 outline 变体。

- [ ] **Step 3: 定义图标按钮规格**

```css
.btn.btn-icon {
    width: 28px;
    height: 28px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}
```

> **选择器必须写 `.btn.btn-icon`（两个类）。** `tasks.js:200` 的容器是 `<div class="btn-group btn-group-sm">`，而 `:695` 的 `.btn-group-sm .btn { padding: .4rem .9rem }` 特异度是 (0,2,0)，会压掉裸 `.btn-icon` 的 (0,1,0)。

- [ ] **Step 4: 补 aria-label**

6 个图标按钮：`tasks.js:203/210/218/224`、`history.js:110/117`。把 `title="X"` 补成 `title="X" aria-label="X"`。

（图标按钮没有可见文本，屏幕阅读器只能读 `aria-label`；`title` 不是可靠的无障碍名称来源。）

- [ ] **Step 5: 实测**

- [ ] 任务卡上的启动/暂停/取消按钮，鼠标悬停有明显颜色变化
- [ ] 按下时有变暗反馈
- [ ] 历史页「刷新」按钮有可见边框，不再是裸文字
- [ ] 图标按钮是 28×28 的方形，不是被拉长的胶囊

- [ ] **Step 6: 提交**

```bash
git add static/css/style.css static/js/tasks.js static/js/history.js
git commit -m "fix(ui): 补齐按钮 hover/active/outline 状态

.btn-success 和 .btn-success:hover 两条规则的值完全相同,hover 是空操作。
.btn-warning/.btn-danger/.btn-info 同样。这四个类正是任务卡上的
启动/暂停/取消按钮——用户点之前得不到任何反馈。

btn-outline 在 style.css 里零定义,而 :622 的 .btn{border:none} 压掉了
Bootstrap 的边框,导致 history.html:159 的「刷新」按钮渲染成无边框的
灰色纯文字,紧挨着实心青绿的「启动」按钮。补 outline 变体时显式写
border(.btn-secondary 已经这么补过一次)。

图标按钮选择器写成 .btn.btn-icon 而非裸 .btn-icon:容器是
btn-group-sm,:695 的 .btn-group-sm .btn 特异度 (0,2,0) 会压掉 (0,1,0)。

6 个图标按钮补 aria-label(title 不是可靠的无障碍名称来源)。"
```

---

## Task 12：文字对比度与状态文案

**Files:**
- Modify: `static/css/style.css:1437-1444`（`.form-group-label`）、`.detail-k`、`static/js/history.js:209`

**Rationale:** `--color-text-muted: #5f6670`（`:18`）对 `#15171c` 实测 **3.09:1**（AA 要求 4.5）。它被用在 `.form-group-label`（首页三个分组标题）和 `.detail-k`（详情弹窗**全部**字段名）上。

另外 `history.js:209` 的 `getStatusText` 只映射三态、其余 fallback 回英文原值 —— 这是历史页 `paused` 与 `✓ 已完成` 中英混杂的根源。

- [ ] **Step 1: 提高对比度并制造字号断层**

`.form-group-label`（`:1437-1444`）：

```css
.form-group-label {
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--color-text-secondary);
    margin: 14px 0 6px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--color-border);
}
```

`--color-text-secondary` 是 `#9aa0aa`，对 `#15171c` 实测 **6.82:1**，过 AA。

同样把 `.detail-k` 的 `color` 从 `--color-text-muted` 改成 `--color-text-secondary`。

> **不要加 `text-transform: uppercase`。**「基础」「范围与层级」「输出」是中文，没有大小写形态，这是空操作。中文界面只有 color、font-weight、font-size、letter-spacing 四个杠杆。

- [ ] **Step 2: 统一状态文案**

`static/js/history.js:209` 的 `getStatusText` 补全六态，与 `tasks.js:512-522` 保持一致：

```javascript
function getStatusText(status) {
    const texts = {
        'pending': '等待中',
        'running': '运行中',
        'paused': '已暂停',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return texts[status] || status;
}
```

> 两个文件各有一份实现是重复，收敛到公共文件属于 C2（第三档），本任务只做行为对齐。

- [ ] **Step 3: 验证对比度**

```javascript
// 复用 Task 7 Step 3 的 contrast() 函数
console.log('muted   :', contrast('#5f6670', '#15171c'));   // 3.09 —— 不达标
console.log('secondary:', contrast('#9aa0aa', '#15171c'));  // 6.82 —— 达标
```

肉眼确认：首页「基础」「范围与层级」「输出」三个分组标题清晰可读；历史页状态列**全中文**，不再有 `paused`。

- [ ] **Step 4: 提交**

```bash
git add static/css/style.css static/js/history.js
git commit -m "fix(ui): 提高辅助文字对比度,统一状态文案为中文

--color-text-muted (#5f6670) 对背景 #15171c 实测 3.09:1,达不到
WCAG AA 的 4.5。它用在首页三个分组标题和详情弹窗全部字段名上。
改用 --color-text-secondary (#9aa0aa, 6.82:1),配 font-weight 600 和
字号断层保持层级感。

没有加 text-transform:uppercase——中文没有大小写形态,是空操作。

history.js:209 的 getStatusText 只映射三态、其余 fallback 回英文原值,
这是历史页 paused 和「✓ 已完成」中英混杂的根源。补全六态与
tasks.js:512 对齐。两处重复实现收敛到公共文件属于第三档,本次只对齐行为。"
```

---

## Task 13：动画降噪与最终验收

**Files:**
- Modify: `static/css/style.css`（`*{}` 过渡、`.task-card` 动画、进度条过渡）

**Rationale:** `:1243-1247` 的 `* { transition-duration: .3s }` 给每个 `td`、每个 `span` 都挂了色彩过渡；`:1303-1316` 的 `.task-card { animation: fadeInUp .5s }` + nth-child 递延，而 `tasks.js:171` 每次进度更新都 `container.innerHTML = ...` 全量重建 → **所有卡片集体重放上浮动画**。

- [ ] **Step 1: 把全局过渡改成按需**

Task 3 已经把三条 `*{}` 合并成一条。现在从那条里**删掉** `transition-duration` / `transition-property` 声明，改为只给交互元素加：

```css
.btn,
.form-control,
.form-select,
.nav-link,
.card,
.task-card {
    transition: background-color 0.15s ease,
                border-color 0.15s ease,
                color 0.15s ease;
}
```

- [ ] **Step 2: 删掉卡片入场动画**

删除 `:1303-1316` 的 `.task-card { animation: fadeInUp ... }` 整块以及配套的 nth-child 递延规则。

> 根因是 `tasks.js:171` 的全量重建。真正的修复是增量渲染（C5，第三档）。在那之前，删掉动画是成本最低的止血 —— 每次进度更新闪一次全屏动画，比没有动画糟糕得多。

- [ ] **Step 3: 加速进度条过渡**

`.progress-bar` 的 `transition: width 0.6s cubic-bezier(...)` 改为：

```css
    transition: width 0.2s linear;
```

> 0.6s 的缓动曲线让进度条明显滞后于真实进度。下载进度应该是线性、跟手的。

- [ ] **Step 4: 尊重系统的减少动画偏好**

在文件末尾追加：

```css
@media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }
}
```

- [ ] **Step 5: 最终验收（对照主计划的验收标准）**

跑全量测试：

```bash
uv run pytest tests/ -q
```

期望：**0 failed**。

起服务，视口 **1366×768**，逐条勾选：

- [ ] 首页「创建下载任务」按钮在折叠线**以上**
- [ ] 刚启动（进度 <25%）的任务，进度条**不是红色**
- [ ] 任务失败后卡片**不消失**，显示错误原因，toast 常驻
- [ ] 文件选择按钮、number 微调箭头、select 弹层**无白底**
- [ ] Leaflet 绘制工具条深色，且**图标仍然显示**
- [ ] 所有 select 有下拉三角
- [ ] 按钮 hover/active 有反馈，历史页「刷新」按钮有边框
- [ ] 分组标题清晰可读，历史页状态列全中文
- [ ] 任务进度更新时，卡片**不再集体闪动**

再切到 **1600×1000** 视口，重新截 4 张图，与 `docs/images/phase2-baseline/` 并排对比，确认整体观感提升且没有破相。

- [ ] **Step 6: 提交**

```bash
git add static/css/style.css
git commit -m "fix(ui): 动画降噪——全局过渡改按需,删除卡片入场动画

:1243 的 *{transition-duration:.3s} 给每个 td、每个 span 都挂了色彩
过渡。改为只给 .btn/.form-control/.nav-link/.card 等交互元素加,
时长 .3s -> .15s。

删掉 .task-card 的 fadeInUp 入场动画 + nth-child 递延:tasks.js:171
每次进度更新都全量 innerHTML 重建,导致所有卡片集体重放上浮动画。
根治要做增量渲染(第三档 C5),在那之前删动画是成本最低的止血。

进度条 width 过渡 .6s 缓动 -> .2s linear:下载进度应该跟手,
不该滞后于真实进度。

补 prefers-reduced-motion 支持。"
```

---

## 阶段收尾

- [ ] `uv run pytest tests/ -q` → 0 failed
- [ ] 主计划 [Phase 2 验收标准](2026-07-27-master-plan.md#phase-2--2026-07-27-phase2-visualmd) 逐条勾选
- [ ] `!important` 数量已从 92 降到 70 以下（`grep -c '!important' static/css/style.css`）
- [ ] **本阶段完成后才进入发版流程**（用户已决定全部改造完成后统一发版）：版本号 bump（`app.py` 与 `build.spec` 两处，参照 `git log` 里既往 bump 的做法），然后走项目既定发版流程（项目记忆：WSL 下 `git push` 会挂起，tag 用 `gh api` 创建）

## 本阶段明确不做

| 项 | 为什么 |
|---|---|
| 「重试」按钮 | 三个 manager 的 `start_task` 都要求 `status in ('pending','paused')`，需要先改后端状态机 |
| `getStatusText`/`formatDate` 收敛到公共文件 | 属于 C2（第三档），本阶段只对齐行为不动结构 |
| 任务列表增量渲染 | 属于 C5（第三档）。Task 13 删掉入场动画已止血 |
| 图标 sprite 化、修图标语义错误 | 属于 C3（第三档） |
| 可拖拽分栏 | 属于 C4（第三档） |
| `.history-table td` 密度 | **该类在 DOM 里不存在**，改了零效果 |
