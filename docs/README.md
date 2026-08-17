# TerraForge 文档索引

本目录**按用途分层**：每个子目录只放一类文档，一份文档属于哪一层，就决定了它能不能被当作现状依据。翻文档前先看下面这张表，别在历史归档里找当前实现。

## 目录一览

| 目录 | 放什么 | 什么时候看它 |
|---|---|---|
| [`guides/`](guides/) | 照着做的操作指南：快速上手、安装、构建打包、成品分发、插件开发 | 要装环境、要跑起来、要打包发版、拿到 zip 包不知道怎么用、要给系统写插件 |
| [`reference/`](reference/) | 当前实现说明：计时系统、DOM 局部更新、`terrain/` 地形加载与全球底图构建 | 想知道某个已上线机制现在是怎么跑的 |
| [`notes/`](notes/) | 外部调研笔记与设计稿：同类项目调研合并结论、共享缓存清理方案。**混着已实施与未实施两种**，逐份看 [`notes/README.md`](notes/README.md) 那张表的「状态」列 | 找 backlog、评估要不要做某件事、查某条已落地的能力当初的决策依据与落点 |
| [`reviews/`](reviews/) | 带日期的时点审查快照，外加一个流产的审查目录 | 想知道某个决策/取舍的来龙去脉，或某轮审查发现了什么 |
| [`archive/`](archive/) | 历史文档归档（全部 2026-05 ~ 07） | 考古：某个旧做法为什么被推翻 |
| [`superpowers/`](superpowers/) | 历史实施计划 `plans/` 与配对设计稿 `specs/` | 考古：某个功能当初是怎么设计和排期的 |
| [`assets/`](assets/) | 图片资源：`images/` 供文档内引用（已入库）；[`diagrams/`](assets/diagrams/) 是根 README 的三张架构 / 状态机示意图 —— HTML 图源加渲染出的 PNG，改完用同目录的 `render.py` 重渲；`ui-baseline/` 是 UI 回归基线截图（**未入库**，被 `.gitignore` 排除，只在本地存在） | 一般不用直接翻，由各文档引用 |
| [`examples/`](examples/) | 可直接运行的示例代码。目前只有 [`plugin-hello/`](examples/plugin-hello/)——拷进 `plugins/` 就能跑通的最小插件，`tests/test_docs_plugin_example.py` 钉住它不腐烂 | 照着 `guides/PLUGINS.md` 写插件时，想先跑通一个再改 |

这张表**不再写份数**。原先写着 `plans` 10 份、`specs` 11 份、`reviews` 6 份，2026-08-08 实数是 15 / 17 / 7 —— 三处全错，而这个索引每周都在动。要数量自己 `ls` 一下，比读一个注定会烂的数字快。

## 哪些能信，哪些不能

这是本次文档重构要解决的核心问题——历史件和规范件以前混在一起，新人读到一份 2026-05 的报告会以为那就是现状。

**可信（描述当前实现，可以照着做）**

- `guides/` —— 操作步骤与代码/CI 现状同步，照做能跑通。
- `reference/` —— 描述已上线机制的当前口径。个别文档有局部失准的地方，会写在该文档自己的开头或对应小节里，读到时会看见。

**不可信（时点快照与历史归档，不代表当前实现）**

- `reviews/` —— 每份是某一天的审查结论，问题可能早已修掉，也可能被后续裁决判为「按设计不改」。
- `archive/` —— 每份记录的是写下那天的状态，**正文一律未回改**，里面的路径引用、行号、复选框、基线数字全是当日快照。
- `superpowers/` —— 计划和设计稿，复选框状态无效，内嵌代码与行号是当日快照，**不要按它重新执行**。
- `notes/` —— **混着三种东西**：已核实的调研结论、已经落地的设计稿（正文仍是撰写当日的原貌，措辞会说得像还没做）、以及代码里根本不存在的拟新增接口。每份文档开头都有状态头，但**状态头本身会过期** —— 以 [`notes/README.md`](notes/README.md) 目录表的「状态」列为准，那一列是回改的。

这三类（`reviews/` `archive/` `superpowers/`）每份文件开头都有**状态头**，写清楚它记录于何时、哪几节今天还成立、当前事实源在哪里。动手前先读那一段；`archive/`、`superpowers/` 和 `reviews/2026-05-08-comprehensive-review/` 还各有一份 README 做全目录对照表。

## ⚠️ 源码路径整体变过一次（2026-08-04）

四个顶级包 `core/` `models/` `routes/` `services/` 已整体移入 **`src/`**，导入写法随之从 `from core.config import Config` 变为 `from src.core.config import Config`。`app.py`、`nuitka_build.py`、`templates/`、`static/`、`tests/` 位置不变。

`guides/`、`reference/`、`notes/` 与根目录 `CLAUDE.md`、`README.md` 已同步更新。**`archive/`、`reviews/`、`superpowers/` 三类历史件的正文一律未回改**（改了就不再是当日快照）——读到里面的 `services/task_manager.py`、`core.database` 这类路径时，按旧布局理解，实际文件在 `src/` 下。

## 我想…… 该看哪份

| 我想…… | 看这份 |
|---|---|
| 环境已装好，尽快跑起来做第一个下载任务 | [`guides/QUICKSTART.md`](guides/QUICKSTART.md) |
| 从零搭开发环境（含 GDAL 这个最大的坑） | [`guides/INSTALL.md`](guides/INSTALL.md) |
| 打包成可执行文件 / 走 CI 构建 / 发版 | [`guides/BUILD.md`](guides/BUILD.md) |
| 我拿到的是 zip 成品包，只想把它跑起来 | [`guides/DISTRIBUTION.md`](guides/DISTRIBUTION.md) |
| 给系统写一个插件（新数据源 / 新管线 / 新导出格式 / 任务钩子） | [`guides/PLUGINS.md`](guides/PLUGINS.md)，可运行示例在 [`examples/plugin-hello/`](examples/plugin-hello/) |
| 搞清楚某个机制现在怎么实现的（计时、局部刷新、地形加载） | [`reference/`](reference/) |
| 搞清楚某个决策为什么这么定、某个旧做法为什么被推翻 | [`reviews/`](reviews/) 找近期的，[`archive/`](archive/) 与 [`superpowers/`](superpowers/) 找更早的 |
| 找还没做的事 / 评估外部项目有什么可借鉴 | [`notes/`](notes/) |
| 了解整体架构、四条管线的分工、目录与数据库约定 | 仓库根的 `CLAUDE.md`（不在 docs/ 下） |

## docs/ 之外的两个事实源

- **`README.md`（仓库根）** —— 功能总览、版本、API 端点、目录结构。
- **`CLAUDE.md`（仓库根）** —— 架构约定与开发须知：四条管线的分工、组合根 `create_app()` 的注入顺序、数据库演进方式、底图的两条硬约束、打包模式差异、测试写法。改代码前读它。

## 谁负责写什么（动笔前先看这张表）

同一件事在四份文档里各写一遍，就会各自漂移——文档化的本地构建命令那次静默失败正是这么来的：依赖声明的版本政策改了，而复述它的两个脚本和几份文档都没跟上。所以每个话题**只有一个主人**，其余地方只放链接（判据落在主人那份里，本文件不复述任何代码事实）：

| 话题 | 主人 | 别处怎么办 |
|---|---|---|
| GDAL 安装顺序、`_gdal_array` 陷阱与重建 | [`guides/INSTALL.md`](guides/INSTALL.md) | 只给链接，不复述命令 |
| 构建与发版流程（含 `APP_VERSION` bump、CI 覆盖边界） | [`guides/BUILD.md`](guides/BUILD.md) | 只给链接 |
| 架构不变量与管线契约 | 仓库根 `CLAUDE.md` | 只给链接 |
| 功能巡览、端点清单、配置页巡览 | 仓库根 `README.md` | 只给链接 |
| 文档分层与可信度 | 本文件 | 本文件不写任何代码事实 |

代码本身永远优先于任何文档。文档与代码冲突时，以代码为准，并顺手把文档改对。
