# images/ —— 已入库的文档图片

本目录下的图片**已纳入 git**，会随仓库分发，且都被文档正文引用。与同级的 [`../ui-baseline/`](../ui-baseline/)（未入库的本地基线截图）区别开。

| 子目录 | 内容 | 被谁引用 |
|---|---|---|
| [`phase2-baseline/`](phase2-baseline/) | 5 张 PNG + `baseline-metrics.json`，UI 改造前（浅色、无 dock）的视觉基线 | `../../superpowers/plans/2026-07-27-phase2-visual.md`（7 处）、`tests/test_css_contract.py` 的模块 docstring、`.gitignore` 的注释 |
| [`ui-review-2026-07/`](ui-review-2026-07/) | 5 张 2026-07 UI 评审截图，视口 1600×1000 | `../../reviews/2026-07-27-ui-review.md` 逐张内嵌 |
| [`readme/`](readme/) | 2 张 v0.3.5 当前界面截图（中文）+ `en/` 下同构的 2 张英文界面截图：浅色主题主界面、数据处理·等高线弹窗 | 仓库根 `README.md` / `README.en.md`（各 2 处） |
| [`design-audit-2026-08-14/`](design-audit-2026-08-14/) | 11 张 WebP，2026-08-14 Rams 设计审计截图，记录的是**重构前**的状态（弹窗式新建、提交钮落在折叠线下、快捷键徽章灰压灰） | `../../reviews/2026-08-14-frontend-design-audit.md`（2 处） |
| [`frontend-ia-redesign-2026-08-15/`](frontend-ia-redesign-2026-08-15/) | 48 张 WebP = 6 个场景 × 1366×768/1600×900 × 明暗 × 中英，2026-08-15 系统层与信息架构重构的**验收**截图 | `../../superpowers/plans/2026-08-14-frontend-system-ia-redesign.md`（5 处） |

## 两条注意事项

**不要随意改目录名或移动文件。** 上表第三列的引用大多分布在历史归档文档里，而那些文档遵循「正文不回改」的约定（见 [`../../archive/README.md`](../../archive/README.md)），不会跟着更新——改了名就是制造一批无人修复的死链。

**只有后两套是当前 UI。** `phase2-baseline/` 与 `ui-review-2026-07/` 拍摄于 2026-07，`design-audit-2026-08-14/` 拍的是 2026-08-15 重构前的样子——这三套的价值都是回溯「当时长什么样」，不是「现在应该长什么样」。`frontend-ia-redesign-2026-08-15/` 与 `readme/` 反映当前界面，界面改了就该重拍。
