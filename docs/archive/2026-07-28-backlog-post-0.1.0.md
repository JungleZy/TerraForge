# v0.1.0 发版后待办

> **归档文档 · 非当前实现**
> **记录时间**：2026-07-28 ｜ **状态**：约四分之三已作废
> **勿按此清单派工。** 三条前提已变：Leaflet 全线换成 Cesium（`static/vendor/` 下已无 leaflet）、PyInstaller 换成 Nuitka（`build.spec` 已删，现为 `nuitka_build.py`）、CI 已补 pytest（`.github/workflows/build.yml` 与 `test-build.yml` 都有 `python -m pytest tests/ -q`）。凡涉及 leaflet / spritesheet / drawLocal / build.spec / 「CI 一条测试都不跑」的条目一律作废。
> 💣 **照做会造成新损害的一条**：「CRLF 清理的地雷」要求 `.gitattributes` 写 `static/vendor/** -text`。现行 `.gitattributes` 是方向相反的 `* text=auto eol=lf`，注释写明这正是为了让 Windows runner 上的 vendor 字节清单校验成立。今天再加 `-text` 会重新引入 Windows CI 字节数校验失败。
> 仍成立、可搬进当前 backlog 的五条：① `VENDOR_MANIFEST` 仍只钉字节数、无 sha256（`tests/test_css_contract.py:6302`）；② BuildVRT 丢**内部**瓦片的校验盲区（`services/download_engine.py` 的 `_assert_vrt_covers_tile_grid` docstring 明写 Known blind spot）；③ `.part` 临时文件名仍用 `id(tile)` 内存地址（会被回收复用）；④ 徽章上 pending 与 cancelled 同色；⑤ 焦点环三处（`.nav-link` / `.page-link` / `.form-check-input`）仍未达 3:1。注：「顺带修 `fonts/fonts.css` 不在清单里」那句已不适用 —— 它是**有意豁免**（`tests/test_css_contract.py` 的 `_VENDOR_GENERATED`），不是缺陷。
> **本文的 34 个变异验证数据是那轮评审唯一幸存的记录，归档后不要删。**
> *正文保持原样未回改。*

---

来源：2026-07-28 的整分支发版评审（5 路只读审计 + 34 个变异验证 + 裁决）。

**这些都不是当前产物的缺陷** —— 每条都实读过产物、确认今天是对的。它们是**护栏的盲区**和**已知限制**，所以没有一条卡住 0.1.0 发版。按性价比排序。

---

## 🔴 第一批（危害上限最高，建议尽快）

### 1. vendor 清单只钉字节数，等长改写能撤销头条修复

`VENDOR_MANIFEST` 记的是「路径 → 字节数」。变异实测：对 vendor 文件做**等长**改写，能把「禁用态 2.83:1 → 6.21:1」和「弹窗遮罩会变暗」两个头条修复整个撤销，而 **302 条测试全绿**。

**修法**：清单升级成「路径 → (字节数, sha256)」。一处改动同时堵掉三个变异（M23 / M16b / M19b 的一半）。

顺带修：冻结包里 `fonts/fonts.css` 在包中但**不在**清单里（清单钉 14 个，实发 15 个）。

### 2. 雪碧图墨色是手抄常量，模型输入端没接真文件

`SPRITE_INK_HEX = '#464646'` 是人工抄进测试的。把 `spritesheet.svg` 里的墨色换成等长的 `#f0f0f0` → 绘制矩形按钮从 13.89:1 掉到 **1.22:1**（整个应用的主入口图标看不见），302 全绿。

**修法**：从本地 `static/vendor/leaflet.draw/1.0.4/images/spritesheet.svg` 实读 fill 值。vendor 本地化之后源码已经在仓库里了。

---

## 🟡 第二批（按性价比）

| # | 项 | 修法 | 变异证据 |
|---|---|---|---|
| 3 | `!important` 计数可被大小写绕过 | 改一行 `_IMPORTANT_RE`，已验证读数中性 | — |
| 4 | 字号快照未剥注释 | 复用 `_rules_ctx()` 的剥注释逻辑 | — |
| 5 | Leaflet 扫描没覆盖模板内联 `<script>`/`<style>` | 扩大扫描范围 | M17/M12 实测能造出真 404、能废掉遮罩 |
| 6 | bbox 方位扫描没覆盖 `history.js` | 扫描器加 `history.js`（它用 Leaflet 数组形态，非对象字面量） | M10b：转置经纬度后矩形画到地球另一边，全绿 |
| 7 | markup 类名反查 | 参照 `_history_error_cell()` 的范式 | M20-A |
| 8 | 版本常量一致性 | 断言 `config.py` 与 `build.spec` 的 `APP_VERSION` 相等 | — |
| 9 | 三张 leaflet 图片未纳入清单 | 补进 `VENDOR_MANIFEST` | — |
| 10 | leaflet locale 新键检测 | 从本地 `leaflet.draw.js` 解析 `L.drawLocal` | — |

---

## 🔵 工程与流程

### CI 一条测试都不跑

`.github/workflows/build.yml` 和 `test-build.yml` **都没有 pytest 步骤**（grep 零命中）。302 条测试只在本地跑过。

考虑到这条链反复出现的教训是「测试全绿而 bug 逐位复现」，CI 里跑测试的价值有限但不为零——至少能拦住「有人改坏了却没本地跑」。值得单独排期。

### 💣 CRLF 清理的地雷

全仓 8 个文件是 CRLF（`.gitignore` / `README.md` / `config.py` / `config_manager.py` / `task_manager.py` / `config.js` / `map.js` 等），无 `.gitattributes`、无 `core.autocrlf`。这是 2026-05 就有的既有状况，用户已裁定**发版后单独一笔处理**。

**做这笔清理时，`.gitattributes` 必须写：**

```gitattributes
static/vendor/** -text
```

否则 `leaflet.css` 会从 14806 字节被归一化到 **14145 字节**，直接砸掉刚立起来的离线护栏（M30 实测，预测数字精确命中）。

### Windows / macOS 冻结产物无人验证

PyInstaller 只在 Linux 本地跑通过。`build.spec` 与 `requirements.txt` 相对 master 零 diff、vendor 靠整树递归进包、Linux 上 15 个文件已逐字节核对——残余风险低但非零。

**建议**：v0.1.0 的 Windows 包下下来点开首页看一眼。

---

## ⚪ 已知限制（写进了 RELEASE_NOTES，此处备查）

- **底图仍走公网 OSM**（`map.js` / `history.js` 硬编码）。用户已明确选择「先只做库本地化，底图单独议」。
- **拼图临时磁盘峰值约为旧版 3 倍**。中间件是无压缩 GTiff 且调色板已展开成 3 波段（每瓦片 ~196KB vs 旧版 1 波段 ~65KB），一个 zoom 的全部中间件在 BuildVRT 之前同时存在。10 万瓦片量级峰值约 19GB vs 之前 6.5GB。**属读代码推算，未实测**。加 `creationOptions=['COMPRESS=LZW']` 可缓解。
- **BuildVRT 丢内部瓦片时尺寸校验守不住**。实测在 3×3 网格中心种一个未配准的中间文件，拼接正常返回、产物 11% 是纯黑、任务报 completed。触发条件苛刻（两个 bbox 重叠的任务并发拼接），master 上同样存在且更糟。`download_engine.py` 的 docstring 已如实标注。
- **键盘焦点环在三处形同虚设**（非本分支引入，master 一模一样）：导航栏 `.nav-link`、分页 `.page-link` 用 Bootstrap 默认蓝环，深底上 **1.31:1**；配置页 `.form-check-input` **1.27:1**。WCAG 2.2 SC 2.4.11 要求 3:1。Phase 2 的 focus-visible 改造覆盖了 `.btn` / `.form-control` / `.form-select`，漏了这三类。
- **等高线类型下小屏按钮溢出**：1366×768 选「等高线瓦片」时提交按钮 bottom ≈ 874（超出约 105px）。面板可滚动、滚到底能点，不致命。高度模型只算 `index.html` 里可见的直接子元素，`#contourOptions` 带 `display:none` 被排除，这一档不在护栏内。
- **1280×768 余量只剩 4px**（bottom 716 / vh 720）。Windows 上字体度量差几 px 就会掉到折叠线以下（同样可滚动）。
- **徽章上 pending 与 cancelled 完全同色**，靠图标和文字区分。「六态三处各不相同」在卡片左边条和地图描边上成立，徽章上只有 5 个不同色。
- **Leaflet 弹窗关闭按钮 ×** 仍是默认 `#757575`（3.84:1，作为图形控件达标），是唯一没被主题化的 Leaflet 元素。
- `.part.<pid>.<id(tile)>` 里的 `id()` 是会被回收复用的内存地址。同进程两个并发拼接、同一张瓦片、对象地址恰好复用时理论上会串。概率极低且内容是同一张瓦片，master 根本没有 part 机制（更差）。用 `uuid4` 更干净。
- `calculate_tiles` 的 docstring 承诺「超过 MAX_TILES 抛 ValueError / 上限 100,000」，但**全仓没有 MAX_TILES 的实现**，只有 `WARN_TILES_THRESHOLD` 写一条日志。master 就有的假承诺。

---

## 📌 文档需要同步的过期描述

`CLAUDE.md` 里这些已过期：
- 测试基线 148 → **302**
- 新增了 `static/vendor/`（8 个库本地化，CDN 请求归零）
- `style.css` 的 `div:not(...)` 兜底重置已删除
- `templates/base.html` 不再引用任何外部域名

`tests/test_map_js_contract.py:105` 的注释仍写着「走 CDN」，已过期。
