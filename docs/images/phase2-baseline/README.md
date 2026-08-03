# Phase 2 视觉基线截图 —— 改造前形态，仅供回溯

> **拍摄时间：2026-07-27 ~ 2026-07-29**，分支 `feat/gis-data-correctness`
> **状态：C1 清理**前**的浅色、无 dock 形态。仅供回溯，勿作当前基线。**

## 这是什么

5 张 PNG + `baseline-metrics.json`，是 GIS 界面改造 Phase 2 开工前拍的对照基线。metrics 自述得很清楚：`capturedAt` 是「Phase 2 Task 1 — C1 清理前」，`bsBodyBg: "#fff"`、`htmlDataBsTheme: null`——即**浅色、未启用 `data-bs-theme`、`style.css` 的自我覆盖字号块还没清**的那个状态。

其后 UI 已整体重做（工作台改造、单一时间流、dark/light/system 三态主题、0.2.3 路径绝对化、0.2.4 缓存管理），这些截图与今天的界面没有可比性。

## ⚠️ 照计划重新截图比对会得出一堆假差异

`docs/superpowers/plans/2026-07-27-phase2-visual.md` 里有「重新截图逐张比对」的指示。**今天照做没有意义**：基线本身就是改造前的形态，差异是改造的成果，不是回归。同理，Task 1「建立视觉基线截图」若被重跑，会直接覆盖掉本目录这套唯一的改造前留档。

## 不要改本目录的名字

`phase2-baseline` 这个名字被三处引用，改名会全部打断：

- `docs/superpowers/plans/2026-07-27-phase2-visual.md` —— 7 处引用
- `tests/test_css_contract.py:5` —— 模块 docstring 里说明「渲染效果由本目录的截图 + 计算值对拍覆盖」
- `.gitignore:139` —— 注释「已入库的基线在 docs/images/phase2-baseline/」

与 `docs/ui-baseline/` 不同，本目录**已入库**。
