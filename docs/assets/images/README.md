# images/ —— 已入库的文档图片

本目录下的图片**已纳入 git**，会随仓库分发，且都被文档正文引用。与同级的 [`../ui-baseline/`](../ui-baseline/)（未入库的本地基线截图）区别开。

| 子目录 | 内容 | 被谁引用 |
|---|---|---|
| [`phase2-baseline/`](phase2-baseline/) | 5 张 PNG + `baseline-metrics.json`，UI 改造前（浅色、无 dock）的视觉基线 | `../../superpowers/plans/2026-07-27-phase2-visual.md`（7 处）、`tests/test_css_contract.py` 的模块 docstring、`.gitignore` 的注释 |
| [`ui-review-2026-07/`](ui-review-2026-07/) | 5 张 2026-07 UI 评审截图，视口 1600×1000 | `../../reviews/2026-07-27-ui-review.md` 逐张内嵌 |

## 两条注意事项

**不要随意改目录名或移动文件。** 上表第三列的引用大多分布在历史归档文档里，而那些文档遵循「正文不回改」的约定（见 [`../../archive/README.md`](../../archive/README.md)），不会跟着更新——改了名就是制造一批无人修复的死链。

**这些截图都不是当前 UI。** 两套都拍摄于 2026-07，其后界面经历了单一时间流重构、明暗主题、路径绝对化、缓存管理重做等多轮改动。它们的价值是回溯「当时长什么样」，不是「现在应该长什么样」。各子目录的 README 写明了具体拍摄时间与对应 commit。
