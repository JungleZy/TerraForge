# TerraForge 文档审计报告（2026-08-03）

> 基线：0.2.4（`core/config.py:38`）。审计范围：`docs/` 下全部 **55 份**文档/目录，由 9 组审计员逐份对照代码核实，其中全部 DELETE 判决与全部 high 级问题经一名保守派复核，**8 份初判被复核推翻**。本报告以复核后的最终判定为准。
>
> 本版取代同日的 32 份初稿——初稿未覆盖 `docs/superpowers/` 全量、`docs/reviews/` 与两份未跟踪草稿。若外部文档按行号引用过初稿，行号已失效。

---

## 一、总体判断

**55 份里 46 份需要动，只有 9 份能原样留着：4 份删、13 份改、29 份归档。**

最严重的问题不是「内容过时」，而是**过时内容没有任何时间标记**：29 份历史文档用现在时描述早已被推翻的形态，并与 BUILD.md / INSTALL.md 这类必须被信任的规范性文档平铺在同一层目录。仓库至今没有任何归档约定——`docs/superpowers/plans/` 下 10 份计划零份带完成标记，全部维持「未勾选 `- [ ]` + 逐任务实施」的待办形态；`specs/` 11 份里只有 4 份带「状态：」行，且状态只能写「已实现／已批准」，无法表达「当时已实现、后来被 X 推翻」。被推翻的设计稿和现行设计稿在同一目录里长得一模一样，读者无从分辨。

危害最大的一类不是「照着做会立刻失败」的错误——那种会自曝，读者立刻知道文档不能信；真正危险的是**读起来仍然成立、但与代码行为相反**的描述。本次 high_risk 全部属于后者。最典型的两条：`docs/terrain/cesiumjs-loading.md` 承诺的 parentUrl 级联在 z0–4 根本不生效（瓦片被任务自身的全球假地形遮蔽，全程不报错），读者会去排查 base 构建、URL、CORS 等全部无关方向；`specs/2026-08-02-absolute-save-path-design.md` 头部写「已实现」、正文却保留着被 0.2.4 推翻的「路径必须落在 DOWNLOADS_DIR 内」，读者据此写校验、写测试、甚至把「保存到任意目录」判成 bug。

有一条问题不属于上述任何一类，但后果最远：`docs/BUILD.md` 的 GDAL 安装顺序。按文档装出的 GDAL 缺 `_gdal_array`，**构建成功、CI 冒烟测试（只请求首页拿 200）也成功**，直到用户跑拼接或地形切片才在 `band.ReadAsArray()` 处炸——坏包会被直接分发出去。

PyInstaller → Nuitka 的迁移（c09a70385）在文档层留下成片残骸：`build.spec`、`hook-gdal.py`、`--onefile`、「exe 启动慢因为要解包」散落在 BUILD.md、DISTRIBUTION.md、PACKAGING_REVIEW.md、backlog-post-0.1.0.md 四份文档里互相印证，读者交叉验证后只会更确信项目还在用 PyInstaller。

**执行层面的前置阻塞：`docs/archive/` 目录不存在，而本次有 29 份判 ARCHIVE。这些动作必须一次性做完——只搬一半，等于给留在原地的同类文档隐性盖章「还在原目录 = 现行有效」，比不搬更糟。**

---

## 二、逐文件处置表

计数：**DELETE 4 / FIX 13 / ARCHIVE 29 / KEEP 9**（合计 55）

### DELETE（4）

| 路径 | 理由 | 动作 |
|---|---|---|
| `docs/packaging/PACKAGING_REVIEW.md` | PyInstaller 时代（v0.1.0 前）的打包自查清单，通篇 ✅「已修复」现在时且无日期；打勾的 `build.spec`、`hook-gdal.py` 已随 c09a70385 删除，`PACKAGING.md`、`BUILD_SETUP_SUMMARY.md` 本次刚删；:19-21 的 Chocolatey 方案被 `build.yml:64`「choco gdal package is dead」明确判死；:96-144 的六步发版命令会 `git add` 到不存在的文件并打出倒退的 v0.1.0 标签。与 `DISTRIBUTION.md:79-83` 是同一段 PyInstaller 内容的两份副本 | `git rm docs/packaging/PACKAGING_REVIEW.md`（已入库，`git show HEAD:<path>` 永久可取回）。**前置条件当前未满足**：必须先把 `xattr -cr` 补进 DISTRIBUTION.md 的 macOS 一节——它现在只存在于开发者文档 BUILD.md:142/:223，删除后会从终端用户看得到的地方消失 |
| `docs/FRONTEND_OPTIMIZATION_SUMMARY.md` | 内容是 `FRONTEND_OPTIMIZATION_COMPLETE.md` 的真子集：70 个标题里 51 个逐字重复，且 COMPLETE 每节更细（导航栏 56→72px 等像素值、12 个文件清单 vs 11 个）。归一化行级 diff 证明全部「差异行」都是同一事实的粗版本。唯一无对应标题的「6. 响应式设计」是 4 条零信息 checkbox，同内容在 `FRONTEND_OPTIMIZATION.md:125` 与 `DESIGN_COMPARISON.md:291-304` 各有一份（后者更具体） | 直接删除；同一事件的历史记录保留 COMPLETE 一份（归档后）即可。git commit 61d47155b 有完整原文。注意：删除后 `COMPLETE:236` 会留一个指向不存在文件的条目，它属于历史文件清单，不建议改动 |
| `docs/superpowers/plans/.pytest_cache/` | 不是文档，是 pytest 在 docs 目录下跑出的空缓存：`v/cache/nodeids` 与 `stepwise` 内容都是 `[]`（那次运行收集到 0 个测试），README.md 是 pytest 硬编码模板且正文写着「Do not commit this to version control」，目录内 pytest 自生成的 `.gitignore` 内容就是 `*`。`.gitignore:52` 已忽略、`git log --all` 对该路径为空 | `rm -rf docs/superpowers/plans/.pytest_cache/`；`.gitignore` 不用改，删除不产生 git 变更、不需要 commit。（仓库根的 `./.pytest_cache` 是正常位置产物，不在处置范围） |
| `docs/ui-baseline/_probe.png` | 1440×900 孤立探针截图，无日期无说明。逐帧比对确认是已入库的 `docs/images/ui-review-2026-07/history.png` 的真子集：同一天（相隔半小时的同一次运行）、同一页、同一份运行数据（总任务 2/已完成 1/失败 0/累计 12）、同一张地图框选，而 history.png 视口更宽（1600×1000）并多拍到两行真实数据。唯一物理差异是更窄的视口，而 1440 不是本项目在用的任何断点 | 直接删除该本地文件（被 `.gitignore:140` 排除，不产生 diff）。**这是三个 ui-baseline 子项里唯一判 DELETE 的**——另两个没有入库替代品，删了 git 也捞不回 |

### FIX（13）

| 路径 | 理由 | 动作 |
|---|---|---|
| `docs/BUILD.md` | Nuitka 迁移后的构建/发版主文档。主干正确（uv 命令、`dist/terraforge` 产物路径、GDAL/PROJ 数据目录发现、系统库补拷），但残留 PyInstaller 时代结论、Windows GDAL 装法已被 CI 判死、CI 与发版流程描述与 build.yml 不符 | 逐条修：Windows GDAL 改 conda-forge/OSGeo4W；本地构建补「先 numpy 再 `--no-build-isolation` 装 GDAL」；CI 章节补 `pytest tests/ -q` 门禁与 exe 冒烟测试、改正「tag 构建有 artifact」（`build.yml:154` 带 `if: !startsWith(github.ref,'refs/tags/')`）、说明 Windows/macOS 走 setup-miniconda 而非 setup-python；发版流程前置补 `core/config.py` APP_VERSION 与 `RELEASE_NOTES.md`（Release 用 `body_path` 取它）；删掉两处 `--onefile`（会倒在 `nuitka_build.py:385-394` 的重命名与自检上）；「安全考虑」重写为「Python 逻辑已编译为 C 扩展，无 .py/.pyc 可提取；**前端模板与静态资源仍是明文**」（`--include-data-dir` 原样打包 templates/static）；删掉「启动慢因为解包」；Ubuntu 20.04 改 `ubuntu-latest`。**补两条漏项**：build.sh:26-45 / build.bat:26-46 的 GDAL major.minor 硬校验（不匹配直接 exit 1，本地构建最常撞的墙，前置要求一字未提）、输出布局漏掉的 `gdal-data/` 与 `proj-data/`（`setup_bundle_env()` 的启动硬依赖，漏拷得到启动即死的包） |
| `docs/INSTALL.md` | 源码安装指南，uv 流程、Python 3.12、端口 5000、Releases 链接、`_gdal_array` 排障步骤全部与代码一致；唯一硬伤是主安装路径会静默装出缺 numpy 支持的 GDAL，而验证步骤（只 `from osgeo import gdal`，本来就能过）查不出来——排障段写得对，但位置让人以为这是偶发问题，实际是默认路径的必然结果 | 把「先装 numpy、再 `--no-build-isolation` 装 GDAL」提到步骤 2 主路径（排障段保留作为已装坏时的补救）；验证加 `python -c "from osgeo import gdal_array"`；`uv venv` 改 `uv venv --python 3.12`（否则默认解释器可能是 3.10/3.11，且 :84 的验证路径对不上会被误判成重建失败）；版本不匹配那段补「同时改 requirements.txt 的 pin，否则 build.sh/build.bat 会拒绝构建」 |
| `docs/QUICKSTART.md` | 跑通第一条任务的上手指南，产物路径、配置页功能、0.2.4 的拼接/复制阶段、WebSocket 提示逐条对得上；但「框选之后怎么打开参数表单」这一步漏了——弹窗不会自动出现，要点选区浮层的「下载」按钮（`index.html:89-92` 注释 + `map.js:1582-1588`），新手卡死在这里，文档承诺的 6 步流程实际走不通 | 创建任务流程补一步「框选后点击选区浮层上的『下载』按钮打开参数弹窗」；「矩形工具（□）」改为左侧工具条 `id=mapDrawRect`、title「框选下载区域」的按钮；FAQ 补一条 `_gdal_array` 并指向 INSTALL.md 排障段（现有两条 FAQ 都覆盖不到这个报错） |
| `docs/terrain/cesiumjs-loading.md` | 给外部/前端开发者的 CesiumJS 地形加载示例（2026-05-16）。两个 URL 至今有效，但 §3 承诺的「parentUrl 自动级联」在 z0–4 根本不生效，且漏掉后来新增的整条 local 管线。（成文时间比引入 z≤4 全球化的 tiler commit 早 49 分钟——是写在实现落地之前的设计意图，不到一小时就被自己的实现推翻） | 保留两段示例代码；§3 改写为「级联只在 z>4 生效，z0–4 被任务自身瓦片遮蔽」并注明出处 `docs/reviews/2026-08-03-full-project-review.md` M12 +「代码修复后删除本节」（机理细节不要写进示例文档，代码一改又错）；§1 前加「base 需先离线构建，否则跳过本节；**且目录须与 config 键 `terrain_global_base_path` 一致**」；补 `/terrain/local/<id>/layer.json`；新增一节说明 parentUrl 来自 `terrain_base_parent_url`、无 UI、改配置后需重切片。hillshade 两条 URL 建议降为脚注——它返回的是 PNG 晕渲兜底而非 quantized-mesh，混进加载示例会让人误以为能喂 CesiumTerrainProvider |
| `docs/terrain/global-base-build.md` | 标题写「Offline Global Base Terrain Build」，13 行里**没有一条构建命令**。描述的四项事实（输出目录、产物、maxzoom=8、加载 URL）经核对全部与代码一致，但读者照它做不出任何东西；仓库里现成的 `scripts/build_global_base_terrain.ps1`（与本文同一 commit 2e511cc69 引入）从未被任何文档引用——缺链的代价已实测发生过：`full-project-review:258` 因此断言「只有说明没有脚本」 | 补完整流程：前置条件（**自备全球 DEM，数百 GB，仓库不提供**）、跨平台命令 `uv run python -m services.terrain_tiling.cesium_terrain -i <DEM目录> -o downloads/terrain/base_z8 --max-level 8 --tile-size 65`、指向 ps1 并注明它**不传 `--tile-size`**（CLI 默认 17，应用侧任务切片固定 65，规格与 layer.json 元数据会不一致）且用裸 `python` 不遵守 uv 约定（未激活 venv 时静默落到系统解释器）；说明输出目录必须与 `terrain_global_base_path` 一致（脚本 `-OutDir` 是可选参数，改了不同步配置就整段 404）；说明 `--max-level` 实践上必填（省略走自动估算，30m 全球 DEM 在 tile_size=17 下算出 16 级、65 下 14 级，跑不完且中途看不出是参数问题）；补 `terrain_base_parent_url` 与「两个键都无配置界面」；z0–4 遮蔽提示须带 M12 出处并注明「代码修复后删除本节」 |
| `docs/packaging/DISTRIBUTION.md` | `README.md:71` 指向的唯一终端用户分发说明。快速开始与压缩包名和 CI 产物一致，但「从源码构建」整节停在 PyInstaller 时代（四步无一可跑通），系统要求与实际 arm64/glibc 产物矛盾，端口与防火墙两节把危险后果说轻了 | 保留「快速开始/系统要求/目录结构/故障排除/免责声明」骨架；**「从源码构建」整节删掉只留一行指向 BUILD.md**（环境步骤已有五份副本，重写等于把坑重埋第六次）；macOS 改「仅 Apple Silicon (arm64)，Intel Mac 需自行源码构建」；Linux 改与 CI 一致的 ubuntu-latest/glibc 基线；目录结构补 `gdal-data/`、`proj-data/` 与「必须整目录复制」（根目录还有数十个 GDAL 系统库 .so/.dll）；端口一节改「端口不可配置 + 切勿双开（会污染运行中的任务）」；防火墙一节补「监听 0.0.0.0 且无鉴权，不信任的网络请勿放行」；macOS 补 `xattr -cr`；支持链接换 `JungleZy/map-download`；开头「本目录包含…」改为「本文档说明如何使用预编译发行包」（它从未被打进 dist，只在 GitHub 上被读到）；「配置说明」补应用内配置页与 config 表（Earthdata 账号、保存路径、代理） |
| `docs/PARTIAL_DOM_UPDATE.md` | 本组唯一与今天代码逐条对得上的一份（状态变→完整重建、进度变→局部更新、无变化→不动，`tasks.js:482-485` 原样存在），实际属 A 类现行说明，只是 DOM 单位还写「卡片」且漏一条分支 | 留在 `docs/` 根作现行说明：「任务卡片」通改「时间流的行」；点名 `tasks.js:482-485` 分支、`tasks.js:294 rebuildStreamRow`、`tasks.js:497 updateTaskProgressPartial`、`history.js:216 createTaskRow`（三个 DOM 钩子 `.progress-bar`/`.task-pct`/`.task-count` 都由它产出，改类名会让局部更新静默失效——querySelector 返回 null 不报错、进度条不动）；补等高线 `phaseChanged`（`tasks.js:441`）也走完整重建 |
| `docs/superpowers/plans/2026-05-06-google-maps-downloader.md` | 项目 day-0 的 85KB 完整实施计划，58 个 `- [ ]` 全未勾选、顶部带「逐任务实施」横幅，功能早已发到 0.2.4。**文中大段内嵌 2026-05 的完整可复制源码**——回退风险远大于「勾选框没勾」。文件结构写根目录 config.py/database.py（git 证明当天确实如此，属历史真相非笔误），`cache_max_size_mb` 已删除 | **就地加状态头，不移动**（零入链零出链，移动零收益且会破坏 plans/ 批次一致性）：「历史实施计划（2026-05-06），已全部落地并被后续版本大幅重构，非当前实现。文中源码、文件结构、依赖版本、配置键均为当日快照，**禁止照抄或照行号定位**；58 个复选框状态无效，不要按本计划重新执行。当前架构以 CLAUDE.md 的 Architecture 段为准。」建议同批给 plans/ 下其余 9 份一起补头 |
| `docs/superpowers/specs/2026-05-06-google-maps-downloader-design.md` | 项目最初的设计文档，正文前 14 行无日期无状态（C 类最危险形态）；第 9 节「部署」给出可照抄且已失效的命令（`python database.py`）；技术栈 Leaflet.js 1.9+ / Python 3.9+ 已过时，`cache_max_size_mb` 已删 | **就地改，不移动**（与把 specs/2026-07-28-*、specs/2026-08-02-* 从 ARCHIVE 降为 FIX 同一理由）：标题下加「2026-05-06 初版设计快照，非当前实现；当前形态见 README.md / CLAUDE.md（0.2.4，四条管线 / Cesium / Nuitka）」；四处内联作废标注——技术栈（Leaflet、Python 3.9）、:135/:181 缓存 LRU、:336-376 项目结构、第 9 节上方（指向 INSTALL.md、QUICKSTART.md:41）。**正文不删**：第 9 节末尾「应用在 0.0.0.0:5000 启动」今天仍成立；:267/:270 的 `GET /history`、`GET /config` 也仍存在，不要误标失效 |
| `docs/superpowers/plans/2026-07-28-gis-workbench-ui.md` | 「顶部工具栏 + 380px 右侧 dock」外壳只活了两天就被 07-30 推翻（全仓 grep 不到 `workbenchDock`/`dockReopen`，`base.html:53-55` 注释专门反驳它），但 Task 1 的 accent 五令牌、Task 2 的 `.workbench` 外壳/状态栏/`initConnectionStatus`、Task 5 的 `.page-content` 今天仍在跑——不是整份作废 | **就地加状态头，不移动**：移动会打断本报告三处引用，且会把同日同名的 plan/spec 拆到两个地方。状态头须写全：工具栏/dock 已废、引擎已换 Cesium、**文中所有行号已失效禁止照行号定位**（:53 让改 `map.js:78/101/104` 的色号，那三行今天是 `updateTileEstimate()` 的反经线 wrap 判断，照改会改坏瓦片数预估）、`!important` 上界 68 已失效、「不做浅色主题」已推翻、「本环境不做任何 git 提交」仅对 2026-07-28 那次会话有效；同时**点名保留**仍有效的 `--color-on-accent: #041e2b` 取值理由（#082f49 实测 4.04:1 不达标，加深后最差 4.74:1——全仓唯一记录，无测试钉住）。:9 的 Leaflet 技术栈行不要改：落地本计划的同一提交 38e3e30fc 就换成了 Cesium，改掉等于伪造记录 |
| `docs/superpowers/specs/2026-07-28-gis-workbench-ui-design.md` | 头部「状态：已获用户方向性确认」是现在时，读者当现行界面设计。但只有布局被 07-30 推翻——配色令牌表（68-82 行，与 `style.css:31-35` 逐值一致）与「追加决策（2026-07-29）」整段（右侧滑出面板 920/480px、hash 直达、partial 拆分、懒初始化、z-index 阶梯）今天仍是现行依据；07-30 那份只有 88 行，完全没接手这两块 | **就地改状态行，不移动**：改为「2026-07-28 时点设计快照」并**分段标注失效范围**——顶部工具栏/右侧 dock/Leaflet 控件（:14、:20-62、:82）已被 `specs/2026-07-30-workbench-ux-redesign-design.md` 推翻，「不做浅色主题」（:112）已于 5c4cbefe7 反转；配色令牌表与 07-29 追加决策段仍有效。不要写成「整份已被取代」 |
| `docs/superpowers/specs/2026-08-02-absolute-save-path-design.md` | 头部写「已实现」且 80% 描述的是当前出货行为（绝对路径强制、两级深度底线、路径浏览弹窗与 userEdited 接线、init_database 归一），唯独「边界不变」一节被 0.2.4 全盘放开推翻——**局部失效比整篇过时更容易骗人**，读者不会怀疑一份「已实现」的设计稿只有某一节失效 | **就地改正文，不移动**（56 行文档第 4 行挂免责声明挡不住读者读到第 20 行；挪到 archive 也挡不住 grep，反而因落在陌生目录少了上下文）：第 4 行状态改为「已实现（0.2.3）；『边界不变』一节已被 0.2.4 推翻」；:18-22 整节改写并指向 `geo_validation.py:100-127` 与 `task_cleanup.py:80-97`；:31「相对/越界拒绝」改「相对/浅层拒绝」指 `config_manager.py:299-307`；:36-37 改为全盘可浏览 + 三种 400 指 `routes/api.py:927-971`；**:50-56「测试」一节同样过时**——`tests/test_path_browser.py` 已翻面，现有三个用例断言的正是文档说会被拒的行为，不改会有人按文档去「修复」这几个测试 |
| `docs/reviews/2026-07-31-code-only-review.md` | :11「截至 2026-07-31，本报告全部 HIGH、MEDIUM、LOW 项均已处理完毕」对 MEDIUM #21 是虚报：`conftest.py:30 fresh_import` 工具确实加了、双实例 hack 确实清了，但根因未动——100 个测试文件里 48 个仍各写一份 `sys.modules.pop` 清单，真正迁移的业务测试只有 2 个 | 改 :11/:12 两处措辞：#21 从「已修复」降为「只落了 fresh_import 工具与双实例 hack 清理，48 个文件的 pop 清单未迁移」，加一行指向 `2026-08-03-full-project-review.md` M23；「全部…均已处理完毕」改为「除 #21 未收敛外」。后来人看到「全部已处理完毕」会直接跳过，而执行顺序依赖的根因还在，新写的测试仍在复制旧模式 |

### ARCHIVE（29）

> 统一约定：加日期/状态头 + 按 `YYYY-MM-主题` 重命名迁入 `docs/archive/`（需新建）。**`docs/superpowers/plans|specs/` 下的文件不移动，只就地加状态头**——移动会拆散 plan/spec 配对、打断内部互链。

| 路径 | 理由 | 动作 |
|---|---|---|
| `docs/FRONTEND_OPTIMIZATION.md` | 2026-05-10「Cartographic Explorer」琥珀主题设计说明，描述的视觉系统（琥珀 #f59e0b / Outfit + Space Mono / 导航栏 / 表格 / Leaflet / 全局网格 / fadeInUp+shimmer）在代码里全部不存在；`--color-accent-amber` 已被 `test_css_contract.py:522` 列为全前端禁止出现的死名，照它改一提交就撞红测试。还把已实现的明暗主题列为「未来建议」 | 加日期头（「记录 2026-05-10 的琥珀色主题，已被 GIS 蓝令牌体系取代，仅作历史留存」）后改名 `docs/archive/2026-05-frontend-optimization.md`；当前设计以 CLAUDE.md Theming 节 + style.css 内联注释 + test_css_contract.py 三重互锁为准。不 FIX：改成当前设计等于造第四份权威源 |
| `docs/FRONTEND_OPTIMIZATION_COMPLETE.md` | 同一次优化的最详细版本（三份里信息量最大），头部写「完成日期 2026年5月10日 / 优化版本 v2.0」但标题是「完成报告」且 2026-07-29 被改动过（只是把品牌名改成 TerraForge，**别被 mtime 骗成较新**）；「测试结果」里还勾着「✅ 琥珀色强调色一致」，今天是彻底反的。另有三处：`.task-card` 已改为 `.task-row`、toast 默认 3500ms 且失败常驻（非文档说的 3 秒必消）、把明暗主题列为短期待办 | 加日期头后改名 `docs/archive/2026-05-frontend-optimization-report.md`。**正文不回改**——历史文档的基线数字与行号一律保留原样，改掉等于伪造记录；一切现状信息只通过状态头承载 |
| `docs/DESIGN_COMPARISON.md` | 「优化前 vs 优化后」对照表，**两端状态如今都已是历史**（左栏是最初的 Bootstrap 浅色，右栏是已被取代的琥珀主题），无日期标注，读者会把右栏当现状去做新组件，可能踩中 `test_css_contract.py` 的对比度断言。「Google Fonts 异步加载」也与「断网必须能用」的离线约束冲突 | 加日期头后改名 `docs/archive/2026-05-design-comparison.md`。**日期要写对**：琥珀主题落地于 61d47155b（2026-05-14）、终于 7aa66a214（2026-06-15），不是 2026-05-10；建议写「记录 2026-05-14 至 2026-06-15 之间的琥珀主题；两端状态均已作废，当前设计见 style.css 与 test_css_contract.py」。同步修 `FRONTEND_OPTIMIZATION_COMPLETE.md:235` 与 `_SUMMARY.md:232` 的路径引用 |
| `docs/BACKGROUND_COLOR_TROUBLESHOOTING.md` | **初判 DELETE，复核改 ARCHIVE。** 一次性排查笔记（结论是「等待用户清缓存验证」），「白底 = CSS 没生效」的前提在明暗主题上线后失效，预期值 #0a0e1a/#e5e7eb/#f59e0b 全部过期。但它描述的 CSS 机制今天仍成立（`style.css:349-375` 的 html/body `!important`、`.container/.row transparent`），是「明暗主题上线前的准确快照」而非错误文档。单删它也消不掉误导——同一套旧色板同时存在于另外 4 份文档里，且会打破仓库自己刚建立的归档惯例 | 与另外 4 份同批迁入 `docs/archive/`（如 `2026-05-背景色排查.md`），文件头加「2026-05 快照，色板与明暗主题上线前状态，勿照此排查」 |
| `docs/STATE_SYNC_DEEP_ANALYSIS.md` | 2026-05 已修复 bug 的归因分析，结论（全字段同步 + 后端立即推送）今天仍成立且找得到代码，但正文用现在时列故障清单（标题却写「深度分析与修复」），示例里的符号已全部删除：`activeTasks.get(taskId)` 单键（现为 `${taskType}:${taskId}` 复合键）、`createTaskCard`（全仓无定义）、无条件 `outerHTML` 重建（正是同目录 PARTIAL_DOM_UPDATE.md 明确否掉的老做法，两份在同一目录互相打架） | 加「2026-05 的历史分析，所述问题已于当时修复」头后迁 `docs/archive/2026-05-任务状态同步分析.md`。归档头须点名保留「状态变更后必须立即 emit，不走 0.5s 节流」并指向 `task_manager.py:455` 与 `:591`——该约束无注释、无测试守卫，只在这份文档里以文字形式存在。**更稳妥：顺手在这两处加注释**，否则将来有人做「统一走节流」优化会无声地把它退回去 |
| `docs/BUTTON_STATE_FIX.md` | 「暂停按钮疯狂切换」的修复记录，方案（前端不做乐观更新、后端改完库立即推）今天仍是现行做法，但正文现在时描述故障；三处会把读者带到错地方：示例写死 `/api/tasks` 前缀（实际 `tasks.js:781` 按 taskType 分发四前缀，照抄会把 DEM/等高线/本地地形的暂停打到地图管线）、「每瓦片一推」（已 0.5s / DEM 与等高线各 1.0s 节流）、「相关文件」指向只有连接日志的 `routes/socketio_events.py`（所有 emit 都在四个 manager 内） | 加「2026-05 修复记录，所述故障已修复，方案仍是现行做法」头后迁 `docs/archive/`；状态头须点名这三处失准。**不可降级为 DELETE**：「前端为什么不做乐观更新」的根因链条（点暂停→本地置 paused→在途 running 推送覆盖→按钮横跳）是这份文档独有的，代码里 grep 不到，而这个不变量今天仍带电——删掉 pause 那发 emit 或加乐观更新，故障就复活 |
| `docs/SSE_IMPLEMENTATION.md` | **文件名骗人，正文不骗人**：标题写「Socket.IO 实时更新实现」，描述的机制（删轮询、activeTasks Map、事件驱动更新）今天逐条找得到；全仓 `EventSource`/`text/event-stream` 零命中，而 `ls docs/` 时文件名是唯一信息源。另有三处小失准：失败任务不再自动移除（原地转红保留，`test_failed_task_row_is_not_removed` 钉住）、载荷实为 19 字段（多 started_at/created_at/total_running_seconds）、事件实为 6 个（0.2.4 新增拼接/复制三个） | 加 2026-05 日期头并**改名去掉 SSE**（如 `docs/archive/2026-05-轮询改-socketio.md`）；全仓零引用，改名不断链。归档头须保住唯一不可替代的信息：**轮询是被刻意删掉的决策**——这是防止后人「进度不刷新？加个 5 秒轮询兜底」的唯一书面依据 |
| `docs/TIME_DISPLAY_FEATURE.md` | 2026-05 初版计时说明，通篇现在时无日期，描述的是「当前时间 − started_at」那一代，已被 `total_running_seconds` 持久化与 `parseTaskDate` 的 UTC 解析两轮改写取代。另有：字段名 `downloaded_tiles/total_tiles`（现为 `_items`）、称 paused 也每秒更新（`tasks.js:763-766` 直接 return）、称终态已从活动列表移除（failed 明确保留）、把已实现的暂停时长统计写成「未来改进」 | 加「2026-05 快照：描述的是 total_running_seconds 之前的初版计时」头后迁 `docs/archive/2026-05-时长显示.md`；把仍与代码一致的三块（formatDuration 三档格式、预估剩余的线性外推公式、pending/running/paused 显示矩阵）并进 `TIME_TRACKING_SYSTEM.md`。**合并硬约束**：(a) paused 那行必须写全「显示已运行时长，但**不逐秒刷新**」，否则把 PAUSE_TIME_FIX 修掉的 bug 请回来；(b) 字段名必须改成 `downloaded_items`/`total_items`，照抄瓦片字段名会让三条管线的预估恒不显示 |
| `docs/PAUSE_TIME_FIX.md` | **初判 DELETE，复核改 ARCHIVE。** 结论就是 `tasks.js:763-766` 的代码，成因描述对 map 管线已不准；但那行 guard 的**存在理由**只记录在这份文档里：dem/contour/local_terrain 三条管线不写 `total_running_seconds`，必走墙钟回退分支（`test_tasks_js_contract.py:1159` 钉死该回退），而 dem 与 contour 都支持 pause——删掉 guard，暂停中的任务「已运行」会每秒继续增长，正是原 bug 一字未变。代码只有 what 注释没有 why，测试也没有断言保护 | 迁入归档并标注「成因今日仅适用于 total_running_seconds 缺失的 dem/contour/local_terrain 路径」。**更优路径**：先把这句 why 落进 `tasks.js:766` 上方注释并补一条 contract 断言，做完后删除无损 |
| `docs/PROJECT_COMPLETION.md` | **初判 DELETE，复核改 ARCHIVE。** 2026-05-07 MVP 完工报告，标题「🎉 项目状态：100% 完成」「已准备好投入使用」，描述的是单管线 + Leaflet + venv 形态（与今天四条管线差一个数量级），还含一段会直接失败的启动指令（`/home/jungle/...` + `source venv/bin/activate`），技术栈写 GDAL 3.12.4（实际 pin 3.8.4）。信息损失轻微但删除动作过激：会与同类文档处置标准不一致（随机抽杀），并新造 2-4 处指向已删文件的引用 | 改名 `docs/reviews/2026-05-07-mvp-completion.md`，文首写明「非当前状态」，同步改第 37 行自引用。ARCHIVE 成本约 3 分钟，能同时消除全部四条误导 |
| `docs/backlog-post-0.1.0.md` | 2026-07-28 的 0.1.0 发版待办清单，有来源日期但通篇「建议尽快」的现行语气；逐条复核后约四分之三已因 Leaflet→Cesium、PyInstaller→Nuitka、CI 补测试而作废，只剩 3-5 条仍成立 | 文首加「2026-07-28 基线快照；0.2.4 复核后多数条目已作废，勿按此清单派工」后迁入归档，并把仍成立的条目单独搬进当前 backlog：①VENDOR_MANIFEST 只钉字节数无 sha256；②BuildVRT 内部丢瓦片的校验盲区；③`.part.<pid>.<id(tile)>` 用会被复用的内存地址；④徽章 pending 与 cancelled 同色（`style.css:1180-1186` 无 `.status-badge.cancelled`，落进 `.badge.bg-secondary`）；⑤焦点环三处——**这是当前仍成立的实缺陷，不要降级为「待重测」**（全局 `*:focus-visible` 特异度 (0,1,0) 打不过 Bootstrap 的 (0,2,0)+`outline:0`，`.page-link` 连 focus 规则都没写）；小屏溢出两条需按 Cesium 版重测。**归档后不要删**：2026-07-28 那轮 34 个变异验证的数据是唯一幸存记录。另：`fonts/fonts.css` 不在 vendor 清单是有意为之（`_VENDOR_GENERATED` 豁免），不要搬，搬过去是假缺陷 |
| `docs/superpowers/plans/2026-05-08-agent-team-comprehensive-review.md` | 7 人 agent 团队审查计划，声称产出的 10 份报告实际只落地 1 份（git 证明其余 9 份从未入库）；「审查的文件」清单指向根目录 config.py/database.py/models/config.py（全部不存在）且只认识 task_manager 一条管线。**15 个已勾选项全在「计划自审检查清单」（检查文档本身写得完不完整），不是执行进度**——66 个真正执行步骤一个都没勾，即该计划从未被执行 | 就地加状态头：「历史计划（2026-05-08），**从未执行**——10 份报告仅 04-backend-architecture.md 落地；文末 15 个已勾选项是计划自审清单**非执行进度**，66 个执行步骤全部未执行。所述文件结构（根目录 config.py/database.py、单一 task_manager 管线）已整体不存在。后续审查见 docs/reviews/2026-07-29 / 2026-07-31 / 2026-08-03」。真实风险是 agent 捡起它照一份不存在的目录结构跑 7 个 subagent |
| `docs/superpowers/plans/2026-05-16-dem-terrain-tiling-ctb-cesium.md` | 本组最危险的一份：整份计划建立在外部 CTB（`ctb-tile`）上，而这条路线最终被 vendored cesium 切片器替换（`ctb_runner.py` 不存在，全仓无 ctb 调用）；但计划其余产物**全部上线**（dem_terrain_jobs 表、terrain_api、terrain_static、两份 terrain 文档、ps1 脚本）——「看起来对了一半」最容易骗人 | 就地加醒目状态头，**措辞要准**：「切片引擎路线已废弃：ctb-tile 曾实现（f97299384）并于同日被 `services/terrain_tiling/cesium_terrain.py` 替换（1e64065f3），残留代码 2026-07-31 删除（e3a5d82de）。本文所有 ctb-tile / ctb_runner.py / compute_available_from_tiles 内容不对应任何现存代码。计划其余产物已上线。文中 database.py/config.py 位于根目录、Python 3.9+ 均为当时状态」。文件名建议 `-ctb-superseded.md` 而非 `-abandoned`——被替换的是引擎不是整个计划 |
| `docs/superpowers/plans/2026-06-15-frontend-premium-redesign.md` | 青绿（#2dd4bf）强调色重设计计划，当时实施了，但配色与主题机制在 2026-07 两轮改造中被整体替换（现为 sky #38bdf8 + 三态主题），现在照它改 CSS 会把界面改坏；57 个复选框全未勾选让人误判为未执行（后端 `/api/history_stats` 明明已落地） | 就地加 supersede 头（当前 accent 为 sky #38bdf8，完整 teal→sky 映射见 `specs/2026-07-28-gis-workbench-ui-design.md:74-78`；三态主题以 `style.css :root` 为唯一事实源）。**必须同步两件事**：(a) 修 `plans/2026-07-27-phase2-visual.md:18` 对本文件的相对链接及其把 #2dd4bf 当「当前配色」的措辞；(b) 配对的 spec 同等处理——只处理 plan 等于把门关了一半。**不可升级为 DELETE**：Task 2「删廉价特效」清单（网格背景/涟漪/shimmer/渐变条/hover 平移/全大写）是今天仍生效的约束，Task 8 的 `#detail*` DOM 契约清单是 CSS 改动的雷区地图，`.badge.bg-dark` 存在的原因也只在这里 |
| `docs/superpowers/plans/2026-06-16-contour-map.md` | 等高线计划，渲染层契约基本成立（is_index_contour、contour_output_dir_for_task、静态路由前缀都对得上），但入口形态已从「框选 bbox 自动下 DEM」改成「上传 GeoTIFF」——`contour_api.py:89` 只收 multipart 无 bbox 字段，`contour_task_manager.py:11-12` 明写下载驱动的 create_task「已删除」 | 就地加 supersede 头，须覆盖**四条**（只写「入口变了」不够）：create_task 已删除；Task 9 前端代码用 `L.tileLayer`/`map.removeLayer`（Leaflet 已下线，现为 Cesium，照抄必报错）；测试片段的 6 处 `import config` + 2 处 `import_module("database")` 已失效（模块在 core/，agent 跑第一个 Step 即 ImportError）；**产品定位被推翻**——计划把「透明纯线、不做晕渲」列为 YAGNI 非目标，而 `core/database.py:80-93` 今天默认 `#FAF6EC` 背景 + 分层设色 + 晕渲。行号与代码片段一律不可照抄 |
| `docs/superpowers/specs/2026-06-15-frontend-premium-redesign-design.md` | **全仓唯一自称「设计文档」并给出完整令牌表的文件**，青绿令牌已被整套替换；整份只描述深色单主题。另有 5 处过时：兼容别名 `--color-accent-amber/warm/copper`（「保留旧变量名」的决定已被推翻，现零命中）、`--font-sans` 实为 `--font-display`（照写会触发未定义 var() 导致声明静默失效）、Google Fonts 已本地 vendored、Leaflet 已下线、PyInstaller/build.spec 已删 | 就地加 supersede 头，**须注明范围**：令牌与单主题部分已死，但 §6 的 `/api/history_stats`（`routes/api.py:679` 仍在线，统计卡片仍渲染）与 §7 的 DOM/类名契约清单（activeTasks、boundsInfo、createTaskBtn、historyTableBody、pagination、searchInput、config-section）今天全部成立。指向 `specs/2026-07-28-gis-workbench-ui-design.md:74-78`（**不是 07-30，那份没有令牌表**）。它叫「design」、通篇设计规范口吻，被当「当前规范」照抄的概率比配对的 plan 更高 |
| `docs/superpowers/plans/2026-06-13-local-terrain-upload-tiling.md` | 本组与现状吻合度最高的一份（表、manager、blueprint、路由全部按计划落地），只有「读起来像未执行」这一个问题（44 个复选框全未勾） | 就地加状态头：「历史计划（2026-06-13），已按计划实施完成；路径 database.py 现为 core/database.py；前端已经过 2026-07 工作台重构，index.html 结构与文中描述不同」 |
| `docs/superpowers/specs/2026-05-08-agent-team-comprehensive-review-design.md` | 审查团队组织设计稿，正文有明确「审查日期: 2026-05-08」，但 :197-198 画出 10 份产物目录树、:580 起用 **✅ 标记它们「已交付」**，实际只有 1 份存在 | 在 10 份产物清单旁加「实际只产出 04-backend-architecture.md，其余未执行」；**与对应 plan 同批处理**——它带同一份 10 文件清单，只处理 plan 会让误导只搬一半 |
| `docs/superpowers/specs/2026-06-12-local-terrain-upload-tiling-design.md` | 有日期和状态行，内容与实现一致；唯一问题是状态行停在「已与用户确认，待写实现计划」，而计划早在 2026-06-13 就写了、功能也上线了 | 状态行改为「✅ 已实施（计划见 `plans/2026-06-13-local-terrain-upload-tiling.md`）」 |
| `docs/superpowers/specs/2026-06-16-contour-map-design.md` | 描述的用户流程（框选 bbox 自动下 ASTER DEM）和渲染工具（`gdal_contour`）都不是最终实现——实际是上传驱动 + matplotlib `ax.contour` 直接画线；且**无状态行**，比配对的 plan 更像「当前设计」，:20 还把晕渲列为不做 | 在日期下加「设计稿；最终实现改为上传驱动 + matplotlib 直接画线，入口形态见 `routes/contour_api.py`；『不做晕渲』已被推翻，现默认开启分层设色 + 晕渲」；与 plan 同批处理 |
| `docs/superpowers/specs/2026-06-16-terrain-color-design.md` | 有日期头，但高程源前提**当天**就被同日的 copernicus-glo30 设计稿推翻（ASTGTM.003 → COP-DEM-GLO-30，文件名大小写也随之变化，vrt_builder 为此同时匹配两种）；还留着一处「⚠ 云端是单 COG 还是 zip 需实测确认」的开放问题，而实现早已按单 COG 落地 | 日期行下补「高程源其后改为 COP-DEM-GLO-30（见同日 copernicus-glo30-design）；ASTWBD 形态已确认为单 COG `*_att.tif`」。同日期两份设计稿说法相反，读者无从判断哪份有效 |
| `docs/superpowers/plans/2026-07-27-master-plan.md` | 两阶段 GIS 改造总览，已于 2026-07-28 全量落地（merge 44788878f），但正文无任何完成标记、14 个复选框全未勾选，技术栈段还写着 PyInstaller + Leaflet，:47 全局约束还是「要能 PyInstaller 打包」 | 就地加状态头：「本计划已于 2026-07-28 全量落地，合并于 commit 44788878f；本文保留为历史记录，技术栈已迁 Nuitka 打包 + CesiumJS 地图，勿按此文再次执行」 |
| `docs/superpowers/plans/2026-07-27-phase1-data.md` | Phase 1（瓦片配准 3857 化 + 输出格式语义 + map.js 表单重置）的逐任务计划，内容与今天代码一致，但 54 个复选框全未勾选、仍以「逐任务实施」口吻写；:851 还提「PyInstaller 离线打包形态」 | 就地加同样的历史状态头（已于 2026-07-28 落地，合并 44788878f）。被当待办重跑的人会发现「测试已经是绿的」，浪费一轮排查，或反手把已修好的代码改回中间态 |
| `docs/superpowers/plans/2026-07-27-phase2-visual.md` | 97 个复选框全未勾选；「已核实的基线数字（2026-07-27 实测）」是改造**前**的快照——`!important` 92 处（现 raw grep 62，测试上界 ≤59）、`*{}` 在 :67/:1217/:1243、`.progress-bar` 在 :429/:1388，今天全部错位；Task 1「建立视觉基线截图」若被重跑会覆盖 `docs/images/` 下的既有基线 | 就地加状态头并注明「文中基线数字与行号是 2026-07-27 改造前的快照，不代表现状；当前 `!important` 上界以 `tests/test_css_contract.py:440` 为准」。**注意 style.css 注释里逐字讨论代码，裸 grep 计数会被注释污染**，写头时不要直接抄某个数 |
| `docs/superpowers/specs/2026-07-30-workbench-ux-redesign-design.md` | 当前工作台形态（splash / 七项状态栏 / 选区可调 / 记录中心）的设计稿，内容与代码高度吻合，唯一问题是头部状态写「已批准（用户授权…）」，读起来是「方案通过、待实施」 | 状态行改为「已实现（c854e12fe 落地，0.2.x 沿用至今）」，并补一句「左列工具条按钮实际文案为『任务』而非『记录』（逻辑名 records 与文档一致）」；**无需移目录** |
| `docs/reviews/2026-05-08-comprehensive-review/04-backend-architecture.md` | 一次流产 agent-team 审查里唯一产出的一份（00-03/05-08 从未存在，文件编号从 04 开始会让人以为其余被误删），描述的是单管线约 2785 行的旧代码（今天 app.py 369 行 / task_manager.py 1573 行 / 四条管线，models/config.py 已删）；多条 CRITICAL 已被后续三轮审查与用户裁决明确判为「按设计不做」——加鉴权/JWT/限流（部署前提），以及把「全局 manager 经 init_* 注入」判为架构缺陷（那正是 CLAUDE.md 钉死的约定） | 目录内加 README.md：说明这是 2026-05-09 一次未完成审查的唯一产出（计划见 `plans/2026-05-08-agent-team-comprehensive-review.md`）、描述的是单管线时代代码、结论已被 07-29/07-31/08-03 三轮取代、鉴权与限流类建议已裁决为按设计不做；或整体移入 `docs/reviews/archive/` 并把文件改名带上 2026-05-09 |
| `docs/ui-baseline/workbench-2026-07/` | 9 张 2026-07-29 工作台交付态全页截图，作为「基线」已 100% 失效：品牌仍是「Maps Downloader」、配置页有已删的「缓存最大大小 (MB)」、保存路径是相对值 `./downloads` 裸输入框（现为绝对路径 + 浏览按钮）、右侧 dock 式活动任务卡片（已被单一时间流取代）；父目录名 `ui-baseline` 字面就是「界面基线」，无 README 无日期 | 目录内加 README.md：「拍摄于 2026-07-29，记录工作台改造交付态；其后 UI 已整体重做（08-01 单一时间流、主题切换、0.2.3 路径绝对化、0.2.4 缓存管理重做），仅供回溯，勿作当前基线」；可再改名 `archive-2026-07-29-workbench/`。**不要删**：`docs/images/` 下两套已入库截图都是更早的改造前形态（metrics 自述 C1 清理前、浅色、`htmlDataBsTheme: null`），无法替代；本目录被 `.gitignore:140` 排除、从未入库，删除不可逆 |
| `docs/ui-baseline/vendor-localization/` | 2026-07-28 离线 vendor 本地化的 12 张 BEFORE/AFTER 验收截图，有对照价值但整个目录不带日期、无 README，`AFTER-*.png` 字面读作「改完就长这样」——而 AFTER-index.png 里仍是旧品牌、相对保存路径、dock 形态，三点均已被推翻 | 目录改名 `2026-07-28-vendor-localization/`，或加 README.md 写明「拍摄于 2026-07-28，对应 commit 38e3e30fc 的离线 vendor 验收，非当前 UI 基线」。同样被 `.gitignore:140` 排除、未入库，处置只影响本地工作树 |
| `docs/images/phase2-baseline/` | 5 张 PNG + `baseline-metrics.json`，是 UI 改造前（浅色、无 dock）的视觉基线；全部已入库且被引用，但目录名不带日期、时间标注是阶段代号「Phase 2 Task 1 — C1 清理前」，metrics 自述 `bsBodyBg: "#fff"`、`htmlDataBsTheme: null`。若照 `plans/2026-07-27-phase2-visual.md:222/1535` 的指示「重新截图逐张比对」，会得出一堆假差异 | 目录内加 README.md，写明拍摄时间（2026-07-27~29，分支 feat/gis-data-correctness）+「C1 清理前的浅色无 dock 形态，其后 UI 已整体重做，仅供回溯，勿作当前基线」。**不要改目录名**——会打断 `plans/2026-07-27-phase2-visual.md` 的 7 处引用、`tests/test_css_contract.py:5` 以及 `.gitignore:139` 的注释 |

### KEEP（9）

| 路径 | 理由 | 动作 |
|---|---|---|
| `docs/TIME_TRACKING_SYSTEM.md` | 计时系统的当前口径说明（2026-07-31 随代码审查同步过），逐条对照代码全部成立 | 保持原样。可选补一句：该口径**只适用地图管线**——DEM/等高线/本地地形 manager 不写 `total_running_seconds`，前端按 `started_at` 回退墙钟（`tests/test_tasks_js_contract.py:1159` 钉了这条回退） |
| `docs/superpowers/specs/2026-06-16-copernicus-glo30-design.md` | GLO-30 设为默认数据源的设计稿，有日期头，内容与当前代码完全对得上，是该组质量最高的一份 | 不动 |
| `docs/superpowers/specs/2026-08-01-concurrency-recommend-design.md` | 并发数「测速推荐」设计稿，标注日期 + 「已实现」，逐条与代码吻合，可直接当该特性的现行说明读 | 不动 |
| `docs/reviews/2026-07-29-swarm-review.md` | 多代理审查总报告，标题和「修复进度」小节都带日期；抽查 6 条「已修复」全部在代码里落实（C2 索引位宽、C3 gzip 响应头、C4 downloading 孤儿、C6 存储型 XSS 的 12 处 escapeHtml、C7 CI 跑 pytest、cancel 不改写终态） | 不动 |
| `docs/reviews/2026-08-03-full-project-review.md` | 今天产出的 11 维度审查报告，标题带日期、每条附 file:line；反向抽查两条 HIGH 确认仍未修（`contour_engine.py:524-525`、`test_delete_files_cleanup.py:322`），是准确的待办清单而非事后总结 | 内容不动；**当前是未跟踪文件且未被 .gitignore 排除，建议 `git add` 入库**，否则唯一副本只在工作树里 |
| `docs/ui-review-2026-07.md` | 2026-07-27 界面专业度评审，开头表格钉死评审日期、commit（9a3b7fd95）、版本（v0.0.9），第 6 节自带「本节已被实际执行决策覆盖」的更正块，历史属性自证得最干净的一份 | 不动 |
| `docs/cache-exclusive-cleanup-plan.md` | 尚未实施的缓存独占清理设计方案，是有效 backlog；对现状的每条代码引用都已逐条核实为真，与 0.2.4 的缓存约定完全自洽 | 不动。可选：移进 `docs/superpowers/plans/` 与其它计划同处，但不改动内容 |
| `docs/geolibre-takeaways.md` | 2026-08-03 对 GeoLibre（上游 commit 483c663）的调研笔记，开头自带日期与 commit；全文是「可借鉴/不采纳」的评估，不含待办承诺，对本仓库的每条断言都核实为真 | 不动。纯参考笔记 + 可选建议清单，不构成必须兑现的待办 |
| `docs/images/ui-review-2026-07/` | 2026-07 UI 评审的 5 张截图，目录名自带日期，全部已入库、全部被 `docs/ui-review-2026-07.md` 逐张内嵌引用，无孤儿文件 | 不动 |

---

## 三、高风险清单

判断标准：**「读起来仍然正确但和代码行为相反」的静默误导危害高**；「照着做会立刻失败」的自曝型危害低，不入此列（如 BUILD.md 的 `choco install gdal`、specs/2026-05-06 的 `python database.py`）。

| # | 文件 | 问题 | 后果 |
|---|---|---|---|
| 1 | `docs/terrain/cesiumjs-loading.md` | §3 承诺「只挂子层 provider，Cesium 会按 parentUrl 自动级联到 base」；实际 `cesium_terrain.py:449-450` 对 z≤4 无条件把瓦片范围改成全球，`:463` 把它原样写进 layer.json 的 available，`dem_task_tiler.py:59` 又固定 min_level=0 | Cesium 在 z0–4 认为子层已覆盖全球，**永不请求 parentUrl**，看到的是任务 bbox 之外被填成 0 的平坦假地形。全程不报任何错，读者会去排查 base 构建、URL、CORS 等完全无关的方向 |
| 2 | `docs/superpowers/specs/2026-08-02-absolute-save-path-design.md` | 头部写「已实现」，「边界不变」一节仍称「任务产物必须落在 `Config.DOWNLOADS_DIR` 之内」「浏览也只浏览该根目录之内」；0.2.4 已全盘放开（`geo_validation.py:100-127` 的 docstring 逐字写「不再强制落在 DOWNLOADS_DIR 内」，`base_dir` 形参已不参与校验） | 读者（含 agent）据此写校验、写测试、或在评审时把「保存到 `D:\任意目录`」判成 bug；反过来也可能误以为不必再做符号链接/浅路径防护。同节还会让人造出一个不存在的「越界 400」分支 |
| 3 | `docs/BUILD.md` | 本地构建只写 `uv pip install -r requirements.txt`，没提 GDAL sdist 编译时构建隔离必须能 import numpy（CI 为此专门先装 numpy 再 `--no-build-isolation`，注释逐字写明） | 构建出的 exe 缺 `_gdal_array`，`band.ReadAsArray()/WriteArray()` 全废，拼接 GeoTIFF 与地形切片运行时才炸；而 CI 冒烟测试只请求首页拿 200，**构建和冒烟都「成功」，坏包会被直接分发出去** |
| 4 | `docs/packaging/DISTRIBUTION.md` | :61-62 把防火墙提示写成一句无害操作：「请允许应用接受传入连接」；实际 `app.py:364` 绑 `0.0.0.0`、`startup_banner.py:128` 主动打印局域网 URL、`routes/` 下零鉴权（grep login_required/Authorization/basic_auth 零命中），而 API 可写任意磁盘路径并能删除目录 | 用户点「允许」，等于把一个**无鉴权、可读写本机文件系统**的服务开放给整个局域网，且完全不知情 |
| 5 | `docs/TIME_DISPLAY_FEATURE.md` | `calculateTimeInfo` 样例用 `new Date(task.started_at)` 与当前时间相减；现行实现以 `total_running_seconds` 为基准、仅 running 叠加当前段，时间戳走 `parseTaskDate` 按 UTC 解析（`tests/test_fix_timestamp_utc.py` 钉这条口径） | 照样例改代码会**一次性撤销两轮修复**：暂停期间重新计入时长，以及裸 SQLite 时间戳被当本地时间解析——东八区表现为任务一启动就「已运行 8 小时」。全程不报错，只是数字错 |
| 6 | `docs/superpowers/plans/2026-05-06-google-maps-downloader.md` | 顶部「REQUIRED SUB-SKILL: 逐任务实施」横幅 + 58 个未勾选复选框 + **正文内嵌 2026-05 的完整可复制源码**（Task 1-16 几乎全是可直接粘贴的代码块） | 新人或 agent 会当成待执行工单重跑一遍，把已演进三个月的代码按当日源码回退。危害远大于「勾选框状态错」，光写「复选框无效」压不住 |
| 7 | `docs/packaging/DISTRIBUTION.md` | :58-59 把端口占用写成「应用将无法启动，请关闭占用端口的其他应用」；实际 `create_app()` 在模块级执行、早于 `socketio.run`，`init_database()`、启动清扫与四条管线的孤儿恢复在绑定端口失败**之前**已全部跑完 | 「双击第二个 exe」被框成无害地起不来。实际第二个实例已对同一 `data/` 跑完启动清扫与孤儿恢复，**会污染第一个实例正在 running 的任务状态**。文档还漏了「端口不可配置」，读者会去找并不存在的端口设置 |
| 8 | `docs/superpowers/specs/2026-06-15-frontend-premium-redesign-design.md` | 全仓唯一自称「设计文档」并给出完整令牌表的文件，`--color-accent: #2dd4bf`（青绿）已被 sky `#38bdf8` 整套替换；且整份只描述深色单主题（现为 dark/light/system 三态） | 做 UI 的人极可能拿它当设计系统基准按青绿改色，破坏现有配色与对比度校验；同时不会意识到需同步维护 `[data-bs-theme="light"]` 分支，只改暗色 → 亮色模式配色错乱 |
| 9 | `docs/backlog-post-0.1.0.md` | 要求清理 CRLF 时必须写 `static/vendor/** -text`；现行 `.gitattributes` 是方向相反的 `* text=auto eol=lf`，注释明写这是为让 Windows runner 上 vendor 字节清单校验成立 | **唯一一条「照做会造成新损害」的条目**：今天再加 `-text` 会与现行 LF 归一策略冲突，重新引入 Windows CI 上的字节数校验失败 |
| 10 | `docs/BACKGROUND_COLOR_TROUBLESHOOTING.md` | 全文按「白底 = CSS 没生效」展开排查；明暗主题上线后，浅色背景是 `:root[data-bs-theme="light"]` 的合法状态（`--color-bg-primary: #eef0f3`、`--color-bg-secondary: #ffffff`） | 亮色/跟随系统模式下的浅色背景会被判定成 bug，读者去清缓存、重启 Flask、甚至给样式加 `!important` 强行压回深色，**破坏主题系统** |

未入选但值得留意的两条：`DISTRIBUTION.md:34` 的「macOS 10.15+」会让 Intel Mac 用户把「架构不匹配」（产物 arm64-only）误诊成「Gatekeeper 拦截」，去反复折腾安全设置；`PROJECT_COMPLETION.md` 的「100% 完成」+ 单管线功能清单会让新人看不到 DEM/地形/本地地形/等高线三条管线的存在。

---

## 四、死链汇总

### 4.1 Markdown 链接：**0 条真死链**

全仓 55 份 .md/.txt 逐条抽取相对链接并 resolve：指向本次刚删的 `docs/packaging/` 下 9 个文件的 Markdown 链接**一条都没有**。`README.md` 的 5 处 docs/ 链接、`CLAUDE.md:110`、`CHANGELOG.md:108`、`RELEASE_NOTES.md:24` 全部命中真实文件。唯一报警是正则误报（`plans/2026-05-06-google-maps-downloader.md:531` 代码示例里的 `[key](str(value)`）。

### 4.2 裸文件名引用已删文档（不会 404，只会白费时间）

| 位置 | 引用的已删文件 |
|---|---|
| `docs/packaging/PACKAGING_REVIEW.md:32` | `` `PACKAGING.md` `` — 打包指南 |
| `docs/packaging/PACKAGING_REVIEW.md:54` | ✅ `` `PACKAGING.md` `` — 打包使用指南 |
| `docs/packaging/PACKAGING_REVIEW.md:57` | ✅ `` `BUILD_SETUP_SUMMARY.md` `` — 配置总结 |

三处随 PACKAGING_REVIEW.md 的 DELETE 一并消失，无需单独处理。同文件 :37-42 还把 `build.spec`、`hook-gdal.py` 列为现存配置（c09a70385 已删）。

### 4.3 描述失真（链接能解析，承诺的内容已不存在）

| 位置 | 问题 | 修法 |
|---|---|---|
| `README.md:308` | `[docs/packaging/](docs/packaging/) — 打包与发版资料（分发说明、**发布检查清单**、历史记录）`。「发布检查清单」指的正是刚删的 `CHECKLIST.md`，全仓已无发布检查清单文档 | 括号内改为「分发说明 + 一份历史打包审查记录」；并把 CHECKLIST.md 的「发布前检查」内容（改 APP_VERSION、更新 RELEASE_NOTES.md）补进 BUILD.md 的发版章节——这是九份被删文档里唯一没被接住的独立信息 |

### 4.4 指向从未产出的文档（既有噪声，与本次删除无关）

`docs/superpowers/plans/2026-05-08-agent-team-comprehensive-review.md` 内共 38 处引用 `docs/reviews/2026-05-08-comprehensive-review/` 下的 9 份报告：`00-executive-summary.md`、`01-security-audit.md`、`02-performance-analysis.md`、`03-code-quality-review.md`、`05-frontend-review.md`、`06-devops-assessment.md`、`07-qa-testing-review.md`、`08-remediation-plan.md`、`issues.json`。git 证明全部从未入库——这是那份计划指示子代理「写到这里」的产出路径，随该计划加状态头一并说明即可。

另一处误报不必处理：`docs/reviews/2026-07-31-code-only-review.md:3` 的 `docs/README/` 实为中文顿列「docs/README/注释的自我声明」，不是路径。

### 4.5 归档动作会**新造**的死链（执行前必读）

- `docs/superpowers/plans/2026-07-27-phase2-visual.md:18` 用相对路径指向 `2026-06-15-frontend-premium-redesign.md`（原文还写着「不要推翻那次改造的设计意图」）——若移动必须同步改路径。
- `docs/FRONTEND_OPTIMIZATION_COMPLETE.md:235` 与 `docs/FRONTEND_OPTIMIZATION_SUMMARY.md:232` 按原路径引用 `docs/DESIGN_COMPARISON.md`——三件套必须同批迁移并同步改这两行。
- `docs/superpowers/plans/2026-07-28-gis-workbench-ui.md` 被本报告以完整路径引用三处——该文件判 FIX 就地改，不移动。
- 删除 `docs/PROJECT_COMPLETION.md` 会新造 2-4 处指向已删文件的引用——这是把它从 DELETE 改判 ARCHIVE 的直接原因之一。
- `docs/images/phase2-baseline/` 若改目录名，会打断 `plans/2026-07-27-phase2-visual.md` 的 7 处引用、`tests/test_css_contract.py:5` 与 `.gitignore:139` 的注释。

---

## 五、结构性问题与目录约定建议

### S1 —— 同一事件多份副本，读者不知以哪份为准

- **前端优化三件套**：`FRONTEND_OPTIMIZATION.md`（叙述版 37 标题）/ `_SUMMARY.md`（清单版 70 标题）/ `_COMPLETE.md`（清单 + 像素值 + 文件清单 72 标题），同一 commit 61d47155b 产出，_SUMMARY 的 51/70 标题在 _COMPLETE 里逐字重复。→ 删 _SUMMARY，另两份归档。
- **时间系统三份**：`TIME_DISPLAY_FEATURE`（初版 now − started_at）/ `PAUSE_TIME_FIX`（第二代只刷新 running）/ `TIME_TRACKING_SYSTEM`（现状 total_running_seconds），对同一个 `calculateTimeInfo` 给出三种互斥口径，只有最后一份对。→ 前两份归档，把仍有效的三块并进第三份。
- **环境安装步骤五份副本**：README.md / INSTALL.md / QUICKSTART.md / BUILD.md / packaging/DISTRIBUTION.md 各写一遍，DISTRIBUTION.md 那份至今是坏的且长期无人发现——因为 PACKAGING_REVIEW.md 也这么写，两份互相印证。→ DISTRIBUTION.md 删正文留一行指针，别写第六份。
- **行为说明四份**：`SSE_IMPLEMENTATION` / `BUTTON_STATE_FIX` / `STATE_SYNC_DEEP_ANALYSIS` / `PARTIAL_DOM_UPDATE` 出自同一次提交 4061ef4ed，共享同一套已消失的符号（`createTaskCard`、`activeTasks.get(taskId)` 单键、写死 `/api/tasks`、「每瓦片一推」），读者交叉验证会越读越确信。其中 STATE_SYNC 的「无条件 outerHTML 重建」与 PARTIAL_DOM_UPDATE 的「条件局部更新」在同一目录里直接打架，两份都没日期，谁先读到谁赢。

### S2 —— `docs/archive/` 不存在，是一切归档动作的前置阻塞

29 份判 ARCHIVE，而仓库至今没有任何 archive/ 目录、没有任何归档先例。**必须一次性建立并一次性搬完**：只搬一半，等于给留在原目录的同类文档隐性盖章「还在原地 = 现行有效」，比不搬更糟。这也是本次把 `plans/2026-05-06-*`、`plans/2026-07-28-*`、`specs/2026-05-06-*`、`specs/2026-07-28-*`、`specs/2026-08-02-*` 五份从 ARCHIVE 降为 FIX（就地加头）的核心理由——单挑几份搬走是双标。

### S3 —— 历史件与规范性文件平铺在 `docs/` 根

BUILD.md / INSTALL.md / QUICKSTART.md（A 类，读者必须信）与 PROJECT_COMPLETION.md / SSE_IMPLEMENTATION.md / FRONTEND_OPTIMIZATION*.md / BACKGROUND_COLOR_TROUBLESHOOTING.md（2026-05 历史件，读者绝不能信）同层平铺，且历史件大多没有日期头。docs/ 根也没有 README 索引，这些文件只能靠 `ls` 偶遇。这是新人误判现状的最主要来源。

### S4 —— `docs/superpowers/` 缺一个 README 对照表

目录下只有 plans/ 和 specs/ 两个裸文件夹，全仓没有任何地方说明这是什么。六份 plan 合计 296 个复选框只勾了 15 个，**而那 15 个还是「计划自审清单」不是执行进度**——读者拿到的信号是「一堆待办工单」。plan 与 spec 也不成对：2026-05-16（CTB）没有 design 稿（恰恰是唯一改道的那条线缺少设计记录，改道原因在仓库里查不到）；2026-06-16 的 copernicus 与 terrain-color 没有 plan；只有 local-terrain 那一对在正文里显式写了「设计依据：」指针，其余全靠文件名猜。

**README 至少写三件事**：(a) 本目录是历史实施计划与设计稿，不代表当前实现，现状以 CLAUDE.md 和代码为准；(b) 一张「计划 ↔ 设计稿 ↔ 实施结果（已实施 / 部分执行 / 未采用）」对照表，把「CTB 未采用」「2026-05-08 只产出 1/10 份报告」写进去；(c) 声明复选框状态无效、禁止按计划重新执行、**文中源码与行号为当日快照禁止照抄或照行号定位**。有了这张表，本组绝大多数「加状态头」的动作可以合并成一次改动。**若只能做一件事，做这张 README 对照表，别做目录搬迁。**

### S5 —— `docs/ui-baseline/` 整个目录未入库，删除不可逆

被 `.gitignore:140` 排除、从未提交，所以 DELETE 的成本与其它文档不同（git 捞不回）。而 `docs/images/` 下两套已入库截图都是 2026-07-27~29 改造**前**的更旧形态，不能替代 workbench 交付态。三个子项里只有 `_probe.png` 有严格更完整的入库替代，判 DELETE；另两个判 ARCHIVE。**每个基线目录必须有 README 写明拍摄时间与对应 commit**——失效基线不会让任何测试变红（tests/ 零命中 ui-baseline，`test_css_contract.py` 走 CSS 文本断言不做图像比对），只会误导人，这正是它比没有更糟的原因。

### S6 —— PyInstaller 残骸跨四份文档互相印证

`build.spec`、`hook-gdal.py`、`--onefile`、「exe 启动慢因为解包」散落在 BUILD.md、DISTRIBUTION.md、PACKAGING_REVIEW.md、backlog-post-0.1.0.md 里。**删掉 PACKAGING_REVIEW.md 不等于清干净了**——`BUILD.md:202-206` 与 :208 的两处仍在，属 BUILD.md 的 FIX 范围，别漏。

### S7 —— 两个 terrain config 键无配置界面且两份文档零提及

`terrain_global_base_path`（`core/database.py:68`）与 `terrain_base_parent_url`（`:71`）都没有 UI（grep templates/ static/js/ 零命中），只能走 `PUT /api/config`（且只接受 DEFAULT_CONFIGS 内的键）或直接改库；而两份 terrain 文档一个字没提。换端口或反向代理部署时，级联会静默 404，且没有任何提示指向配置键。→ 补进 `docs/terrain/` 两份文档。

### 建议的目录约定

```
docs/
├── BUILD.md / INSTALL.md / QUICKSTART.md    # A 类：规范性，必须与代码一致，改代码同步改
├── TIME_TRACKING_SYSTEM.md / PARTIAL_DOM_UPDATE.md   # A 类：现行行为说明
├── terrain/                                  # A 类：运维/集成说明
├── reviews/YYYY-MM-DD-*.md                   # B 类：时点审查快照，不要求与现状一致
├── archive/YYYY-MM-主题.md                   # B 类：历史文档，文件头必须有日期 + 失效声明
├── superpowers/{plans,specs}/                # B 类：不移动，README 对照表 + 每份强制状态头
├── images/                                   # 已入库截图，每个子目录必须有 README
└── ui-baseline/                              # 未入库基线，每个子目录必须有 README + 日期
```

三条硬规则：

1. **A 类以外的每一份文档，文件头第一行必须是日期 + 状态**（现行 / 已实施 / 已被 X 取代 / 从未执行）。文件名带日期不够——读者只知道「哪天写的」，不知道「做没做、还算不算数」。
2. **历史文档的正文不回改。** Tech Stack 行、基线数字、行号引用一律保留原样，改掉等于伪造记录；一切现状信息只通过状态头承载。
3. **计划类文档的状态头必须额外声明**：复选框状态无效、文中源码与行号为当日快照、禁止照抄或照行号定位。仅写「已完成」压不住内嵌源码的回退风险。

---

## 六、执行顺序建议

1. 建 `docs/archive/`，把该迁的一次性迁完 / 加头（superpowers 下只加头不搬）——**不要分批**。
2. 补 `docs/superpowers/README.md` 对照表：一份顶十几次单独加头。
3. 修三份 A 类规范性文档的 GDAL 安装顺序（BUILD / INSTALL / QUICKSTART 主路径统一）——这是唯一会把坏产物发出去的问题。
4. 补 DISTRIBUTION.md 的 `xattr -cr`，然后才能删 PACKAGING_REVIEW.md（硬前置条件）。
5. 修 `README.md:308` 的「发布检查清单」措辞，并把该清单内容补进 BUILD.md 发版章节。
6. 其余 DELETE 与 FIX 按表执行。
7. `git add docs/reviews/2026-08-03-full-project-review.md` 与本报告——两份都还是未跟踪文件。
