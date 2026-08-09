# 全项目评审 · 2026-08-08

> 状态：本文是**快照**，代码为准。基线 commit `5758b14`，工作区含一处未提交改动
> （`cesiumlab_terrain.py` 的实验性 TVD 后端，按约定不评）。
> 测试基线：`python -m pytest tests -q` → **1504 passed, 3 skipped, 267.73s**。
> 方法：10 个并行子代理分片评审 + 主评审逐条复核。带「实测」的条目是本次**跑出来**的，
> 其余读码得出并已核对 file:line。

## 修复进度（2026-08-08 当日）

**全部修完。** 四条 P0 + P1 全部 17 条 + P2 全部 + 安全姿态第 1、3 项，
以及一条评审时判为「前提内可接受」但复查时决定收紧的 SSRF 面（见第三批）。
全量测试 **1757 passed / 3 skipped**（基线 1504 → 1757，净增 253 个行为测试；
`fork()` 的 DeprecationWarning 从 15 条降到 0）。
每一条都做了变异校验 —— 把实现换回旧写法，新用例必须变红。

**安全姿态第 2 项已裁定（2026-08-08，用户）：**「不用考虑安全，系统会运行在可信
的环境中。」于是 2026-07-31 立的部署前提**继续有效**，本文档「安全姿态」一节列出
的 5 项（零鉴权 / 绑 `0.0.0.0` / CORS `*` / 凭据明文、`verify_tile_url` 探测面、
`/terrain/base` 任意文件读、`basemap_source` 全读 SSRF、`/api/fs/browse` 全盘枚举）
**全部按已知并接受处理，不计入缺陷**。清单本身的价值在于：它现在是完整的、
有实测证据的，而不是一份漏了三项的旧记录。

**随裁定回退的一处**：路径类配置键的**根目录约束**取消了。它是安全动机加的，
而代价是真实的 —— `stitch_tmpdir` / `contour_warp_tmpdir` 这两个键存在的全部意义
就是把 GB 级中间产物挪到另一块盘（写规则的注释自己就是这么说的），却被限制在
安装目录内；`terrain_global_base_path` 指向的是 224 MB / 4.3 万个文件的底图，
放到大盘上同样正当。而 `default_save_path` 自 0.2.4 起本来就是「全盘可选」——
同为路径键却两套口径，在可信环境前提下没有任何理由。

保留下来的判据全部与安全无关，只管**功能正确性**：两个 tmpdir 仍然拒相对值
（`download_engine` 那侧是 `os.makedirs`，相对值按进程 CWD 解析，打包 exe 从快捷
方式启动时会把中间产物丢到谁也想不到的位置 —— M10 给 output_path 修过同一类坑）；
`terrain_global_base_path` 仍然拒空值（空值让 /terrain/base 的根落到 BASE_DIR
本身，等于把整个安装目录连同 `data/map_downloader.db` 挂上静态服务，而且底图判定
必然失败）。

**没有回退的两处**，理由是它们对正常用法零成本、且都在修一个与安全无关的不一致：
- 密码不再回填真值（`SECRET_UNCHANGED` 哨兵）—— 密码框不回显是任何应用的常规行为；
- 首页 JS 全局收到 6 键 —— 它同时修掉一个内部矛盾：`basemap_source.client_descriptor`
  特意剥掉 `upstream`，而模板把 `tile_servers` 又递回浏览器，同一约束的第二道门没关；
- `basemap_source` / `terrain_base_parent_url` 拒链路本地（169.254/fe80::）——
  没有人会把瓦片服务架在链路本地段，拦它不构成任何限制。

### 发版当天被 CI 打断三次，三次都是同一类毛病

本地 1757 项全绿之后打 tag，构建连断三轮。三条根因值得单独记 —— 它们全都是
**断言靠环境为真**，正是这份评审通篇在修的那一类，只不过这次栽在测试侧：

| 轮次 | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | ubuntu 红 1 条 | `test_docs_claims` 断言 README 结构树里每个路径都存在，而 `data/ downloads/ cache/ logs/` 是首次启动才建的运行时目录。开发机上四个都在，干净 checkout 里没有 | 豁免运行时目录，并给豁免加闸：该名字下不能有被 git 跟踪的文件（「checkout 不会把它建出来」才是豁免成立的性质），且必须仍出现在树里（防陈旧名单） |
| 2 | Windows 红 9 条 | 8 条是 `C:\Windows\System32\bash.exe` —— WSL 的安装占位 stub，`which` 找得到、能执行，没装发行版时用 **UTF-16** 打一句 no installed distributions 并非零退出。**这个坑本来是知道的**：`_find_real_bash()` 有过两份带完整说明的实现，本轮把其中一份所在的那批用例整段换掉时知识跟着没了，紧接着新用例重新踩上 | 探测收口到 `tests/conftest.py`（`find_real_bash` / `REAL_BASH` / `needs_bash`），三处调用点全部改过去。用一个行为与 stub 一致的替身验证过：`REAL_BASH` 变 None，相关用例 skip 而不是红 |
| 2 | 同轮第 9 条 | `node -e`（脚本几行）撞 30 秒超时 —— node 冷启动加杀毒扫描 | 超时按「环境不可用」skip（同一契约由一条无条件的结构断言守着），上限放宽到 120 秒；另两处 `node --check` 同样放宽但保留「超时即失败」（它们没有兜底断言） |
| 3 | Windows 红 1 条 | 在途下载速度断言拿到 0。用例里三个回调背靠背跑、中间没有真实 I/O，而 Windows 的 `time.monotonic()` 分辨率约 15.6 ms（GetTickCount64），三个样本落在同一 tick → `SpeedMeter.bps()` 的 `dt <= 0` 如实返回 0。Linux/macOS 纳秒级所以一直绿，**同一提交的上一轮 Windows 上它还是绿的** —— 靠时钟分辨率碰运气 | 用 `SpeedMeter(clock=...)`（本来就是为可测性留的参数）注入步进时钟。产品代码一字未改：真实下载跨度远大于 15.6 ms。写了个把 `time.monotonic` 量化到 15.6 ms 的插件复刻该分辨率，确认旧用例在它下面必红、新用例在粗时钟与真实时钟下都绿 |

**教训是同一句话**：一份规则不许有第二处实现（第 2 轮），断言不许依赖开发机
恰好具备的环境（第 1、3 轮）。第 4 轮三平台全绿，v0.2.12 已发布
（linux 317MB / macos 274MB / windows 257MB）。验证方式也跟着升级了 ——
从「开发机上跑全套」改成「在 /tmp 下真 clone 一份，在那份干净 checkout 里跑全套」。

### 第一批：四条 P0

| P0 | 改了什么 | 变异校验结果 |
|---|---|---|
| **B1** | GDAL 闸门收口到新增的 `scripts/check_gdal.py`，两个构建脚本各调一行。判据从「有没有 `GDAL==` 精确钉」换成「版本落在 requirements.txt 声明的范围内 + `_gdal_array` 在位」。`docs/guides/INSTALL.md:120` 那句「去改 `GDAL==3.8.4`」也改了 —— 那行从来不存在 | `./build.sh` 实跑：打出 `GDAL check OK (spec GDAL>=3.8,<4, installed 3.11.4, _gdal_array present)` 后一路进到 Nuitka，停在本机缺 `patchelf` 这个**响亮**的环境问题上。旧行为是 exit 1 + 零输出 |
| **B2** | `app_factory._enforce_single_instance()` 的提示不再建议删锁文件，改为当场说明「删掉不会解锁，只会让两个实例同时认为自己持锁」 | 旧文案对新断言 `删除…\.lock…后重试` 命中 → 红；`不会解锁` 缺失 → 红。机制本身由 `test_single_instance_lock.py::test_deleting_the_lock_file_defeats_the_mutex` 钉住（unlink 后第二个进程真的拿到锁） |
| **T1** | 瓦片索引上界改半开区间语义，并把闭包里的算式提成模块级 `intersecting_tile_range()`（原来在 `build_terrain` 的闭包里，无法单测） | 换回 `floor` 版本后 8/8 边界用例变红，含 `z=11 tile (2560,1137) 与 DEM 零重叠却被纳入范围`。内部区间用例断言与旧公式**逐字等价**，保证没砍掉真覆盖 |
| **T2** | 底图迁移改「同盘 `os.replace` 一次改名；跨盘先拷进同级 `.part` 再 `os.replace` 上去」，失败清掉暂存目录。`user_version` 仍无条件推到 3 —— 原设计的理由成立，原子性保证了「没搬成」等价于「没搬」 | 复现旧写法（跨盘 + 拷贝中断）：目标位置留下**只有 layer.json、没有瓦片层级**的半树，正是新用例 `test_interrupted_cross_device_copy_leaves_no_half_tree` 禁止的形态 |

新增/迁移的用例：`tests/test_fix_review_20260808.py`（B2/T1/T2）、
`tests/test_fix_l1_entry_build_misc.py` 的闸门一节（旧用例把缺陷本身钉住了 ——
它断言「缺 `GDAL==` pin 时报错」，而那条报错在 `set -euo pipefail` 下根本打不出来）、
`tests/test_fix_build_scripts.py` 两条、`tests/test_single_instance_lock.py` 一条、
`tests/test_fix_base_path_migration.py` 的失败注入点从 `shutil.move` 换到
`os.replace` + `shutil.copytree`。

### 第二批

| 项 | 改了什么 | 变异校验结果 |
|---|---|---|
| **P1#1/#2** 永久 running | 新增 `task_cleanup.fail_stranded_running_task(table, id, reason)`，三个 manager 的 `_run_task` 在线程退出处调它：只在「自己就是登记在册的那个线程」时补偿，行还停在 `running` 就判 `failed`。一处盖住两条路 —— 异常绕过 `_execute*` 自己的兜底（它活在 `conn = get_connection()` 之后），以及删除 commit 失败后 worker 走 stop 分支**正常** return（没有异常可捕）。竞态由状态机本身闭合：三条管线的 `start_task` 都只接受 pending/paused，行是 `running` 时不可能有新 worker 被登记 | 把补偿换回 no-op：6/6「必须判失败」用例变红（三条管线 × 两条路）；而 4 条护栏用例（用户暂停不被改写、晚退线程不抢新一轮）保持绿 |
| **安全姿态第 1 项** 配置外泄 | 首页 JS 全局从 `config\|tojson`（45 键）收敛到 `routes/main.py:MAP_CONFIG_KEYS`（6 键，= `map.js` 的全部读取点）；密码不再回填真值，改回填 `SECRET_UNCHANGED` 哨兵，`PUT /api/config` 收到哨兵跳过该键 | 实跑 test client：`GET /` 不再含密码、JS 全局恰好 6 键；`GET /api/config` 回哨兵；「读回哨兵再原样提交」不覆盖密码而同批其他键照常生效；改密码、清空密码都正常 |
| **P1#5** 两套 output_path 口径 | 删掉 `dem_task_manager._resolve_task_output_dir` 与 `terrain_static._resolve_dem_task_output_dir`，四个调用点统一走 `resolve_stored_output_dir`（= M10 存量归一用的那个）。`geo_validation.resolve_output_dir` 保留它本来的职责：校验**请求里新传进来的**路径 | 复活旧 helper：2/5 用例变红。另有一条用例先钉住「两套口径在每种相对形式上确实不同」，防止「统一到哪套都一样」的误解 |

**P1#5 的严重度要下调 —— 原文的数据丢失链不可达。** 那条链需要一个**相对**的
`dem_tasks.output_path`，而 DEM 的 `create_task` 走 `require_absolute_output_dir`，
实测拒收一切相对值（`'rel_out'` / `'./downloads/dem'` / `'downloads'` 全部 ValueError），
所以线上该字段恒为绝对路径 —— 两套口径对绝对值结果一致。真实存在的是**代码重复 +
语义分歧**这个漂移隐患（以及 `task_cleanup.py:162` 那句「M10 收敛后的唯一一套」当时是
假的，现在才成真），不是 GB 级产物丢失。这一条按本仓 `2026-08-03` 那份的
「被推翻的误报清单」惯例记在这里，不改 P1 表格的原文。

第二批新增用例：`tests/test_fix_stranded_running_task.py`（23 条）、
`tests/test_fix_config_secrets_to_browser.py`（15 条）、
`tests/test_fix_output_path_one_resolver.py`（5 条）；
迁移一条 `tests/test_fix_dem_output_path.py::test_execute_resolves_relative_output_path_*`
—— 它原先钉的是被删掉那套口径的具体目录，现在钉「与存量归一同口径」+「不依赖 CWD」。

**未闭合（有意，已在下文「安全姿态」记明）**：配置**表单**仍把 `proxy_url` 与
`tile_servers` 渲进可编辑输入框 —— 用户必须看得见才能改。要把它们也拿掉得先有鉴权（S1）。

### 第三批：P1 剩余各条 + P2 + 安全姿态第 3 项

九个并行分片按文件归属切开（一个分片一组文件，互不重叠），主评审合并。
每个分片自己做了变异校验，主评审复核了跨片交互与三处判断。

| 分片 | 修了什么 |
|---|---|
| **SecCfgValidate**（安全姿态第 3 项） | `validate_config` 从「10 个 if/elif + 尾部 return True」改成显式键→规则表 `_VALUE_RULES` + `_UNCONSTRAINED_KEYS`，并加一条用例断言 `DEFAULT_CONFIGS` 的每个键必须恰好登记在其中之一（新增键漏登记直接红）。路径类键必须落在 BASE_DIR/DOWNLOADS_DIR/CACHE_DIR（两个 tmpdir 键另允许系统临时目录）内，解析口径与读取侧逐字相同；`terrain_base_parent_url` 拒非 http(s) scheme、协议相对、userinfo、空白换行、端口越界与链路本地段。`_resolve_safe_file` 那句假前提 docstring 重写 |
| **TileEnginePause**（P1#3、#4） | `TaskStillStoppingError`（故意继承 ValueError，路由层现有 `except ValueError` 无需改动）区分「上一轮还在收尾」与「真的在运行」；`stitch_tiles_with_gdal` 在 `_georef_one` 那一层加协作停止点（实测：20 瓦片 z10 网格，标志在第 1 张后置位则 5ms 内在 1/20 处中止且不留产物）。`tiles` 全网格 list 换成生成器，`cache_hit_tiles` 换成计数，`session_status` 整个删掉（`old_status` 由 `failed_keys` 一行推出，并新增用例钉住「每块瓦片每轮恰好上报一次」这个前提） |
| **DeletionCleanup**（P1#6 + 两个 P2） | 新增 `DirRemoval(eligible, removed)` + `remove_task_dir_and_confirm()`，三个需要真相的消费者全部迁过去（原来重复的 `and not dir.exists()` 消失）；快路径先登记 `pending_deletions` 再删、按真实后态回报。新增 `retained_outputs` 表 + `record_retained_output()`：`delete_files=false` 时登记产物目录，消费者是 `_materialised_sweep_roots()`（那个与源数据同量级的物化中间栅格原来随任务行级联消失后彻底无引用）。四条管线路由全部接上 |
| **DemTerrainFixes**（P1#7、#8、#9 + 四个 P2） | 切片进度写库包 try（记账失败不再作废几小时的产出）；`_WORKER_SAMPLER` 释放移出 `if temp_input:`（单输入 tif 时那个条件恒假，串行路径三个入口都会泄漏一个打开的 GDAL dataset）；Earthdata 重定向改 `urlparse(...).hostname` 精确判 + 必须 https，闸设两处，拒绝时零请求；`meta.json` 的非有限值归一成 null；停止后跳过底图植入；hillshade 的 `.tmp.png` 清理；两处死变量删除 |
| **ContourFixes**（P1#10、#11 + 五个 P2） | 颜色改用渲染器自己的 `matplotlib.colors.to_rgba` 校验（不是正则 —— 正则会拒 `red` 和 8 位色，那是另一个方向的口径分歧）；等高距下限 1 米，级数上下限与预览路径收敛到同一对常量；**删掉 160 行无用户可达路径的下载半程**，留 9 行守卫；暂存目录清理移进 `finally`；产物位置三个消费者全部收敛到 `resolve_stored_output_dir`；非栅格上传在创建时 400 |
| **FrontendJSFixes**（P1#14、#17 + 五个 P2） | `initHistory` 改 async 并 await 地图初始化（监听器与统计在 await **之前**接好，避免那一跳往返期间搜索/筛选是死的）；toast 类型 `'error'` → `'danger'` 且无效类型会 `console.warn`；`aria-pressed` 与 `.active` 同步；splash 的 window error 监听器成对移除；`allTasks` 这份会漂移的第二副本删除，小地图改读 store；`path_browser` 的 `currentPath` 重置 |
| **TemplatesA11y**（P1#15、#16 markup + 四个 P2） | 状态栏三个控件从 `<span>` 改真 `<button>`；9 个 chip 补 `aria-pressed`（语言那组由服务端算初值 —— 它没有 JS 切换，是 reload）；两个侧滑面板补 `role="dialog" aria-modal="true" tabindex="-1"`；图标收回 `_macros.html`（模板 SVG 字面量 31 → 23，重复几何体 0）；base.html 那 17 个内联 style 提成 11 个类 |
| **BuildCIHardening**（P1#12、#13 + 四个 P2） | 打包前清理补上 logs 与 `.base_unpack_*` 并把断言改成逐项循环；`nuitka==4.1.3` / `matplotlib==3.11.1` 入 requirements 并把 Nuitka 版本折进 cache key；workflow shell 改 `bash -leo pipefail`；`app.py` 把 `setup_bundle_env()` 提到入口守卫之前（`freeze_support()` 原来在本项目任何构建里都是 no-op，因为 `sys.frozen` 到下一句才被设上）；`single_instance` 只对 EACCES/EAGAIN/EDEADLK 判「已有实例」，NFS/CIFS 的 ENOLCK 落回宽容路径；`nuitka_build` 新增产物数据哨兵校验；`.gitattributes` 补底图分卷 |
| **DocsTruthFix** | README 端点清单补全并双向对账（43 条，含幽灵路由反向断言）；结构树改按目录列；快速开始的安装序列改对；`CLAUDE.md` 补 Basemap 一节（两条硬约束原来只活在源码注释里）、修 conftest 那条假规则；GDAL 陷阱的四处复述收成一处（INSTALL.md 独占），新增 `tests/test_docs_claims.py` 机器化钉住 |

主评审自己另修了四处无人归属的文件：`models/task.py` 三个时间戳统一走
`parse_db_timestamp`（原来 naive/aware 混在同一个对象里）、补 `idx_contour_tasks_status`
（四张任务表里唯一缺的）、`basemap_static` 热路径加 5 秒 TTL 缓存（每张瓦片原来开
三条 sqlite 连接）、`contour_api` 接上 retained_outputs。

#### 三处需要判断的地方

1. **多进程池 fork → spawn：切了。** `DemTerrainFixes` 按批次约定否决了它，理由是
   替身 `_InProcessPool` 的签名里没有 `mp_context` 会 TypeError。复核后推翻：**替身的
   签名不是需要守住的不变量**，改替身即可。改完用真进程池实测 —— `test_process_from_dem_task.py`
   跑通，`fork()` 的 DeprecationWarning 从 12 条降到 0。Windows/macOS 的打包产物本来
   就走 spawn（worker 早已必须 spawn-safe），Linux 走 fork 只是让三平台不一致，而
   Python 3.14 会改默认值。contour 那侧 `ContourFixes` 已经切了并用真渲染验过，两处
   现在一致。
2. **`.btn-info` 不是可删的死代码。** `TemplatesA11y` 确认它零 markup 引用，但
   `test_css_contract.py` 把它登记在 `FILLED_BTN_VARIANTS` 里、按钮层叠模型会算它全部
   四态。删 CSS 会让模型报「算不出来」而不是清掉死代码。保留 + 双向用例（规则消失或
   名字离开清单都会红）。
3. **`_state_lock` 跨 commit：不动。** `DeletionCleanup` 给了具体交错：把 DELETE+commit
   移出锁会重开 `ident` 检查刚堵上的竞态 —— 删除看到 not-running 后释放锁，`start_task`
   在同一把锁内提交 running 并起线程，随后我们的 DELETE 带着过期的 `running=False` 落地，
   于是没有 pending_deletions、没有停止标志、没有墓碑，快路径 rmtree 而新 worker 正在重建。

#### 复查时新收紧的一条（评审原文判为「前提内可接受」）

**`basemap_source` 的链路本地段现在拒收。** 冒烟复测时发现一处**方向反了**的不一致：
`terrain_base_parent_url`（只给浏览器用、服务端从不请求）已经拦了链路本地，而
`basemap_source`（`/basemap/{z}/{x}/{y}` 会**由服务端**去取、并把响应体原样回吐）没有拦。
实测确认那条 SSRF 当时仍然活着（靶机正文完整出现在瓦片响应里）。

收紧的范围是**只拦链路本地**（169.254.0.0/16、fe80::/10），不拦回环与私网 ——
自建瓦片镜像住在 `127.0.0.1` 或 `192.168.x.x` 是项目文档里就有的正当用法，而
169.254.x.x 从来不是一个瓦片服务地址。写入侧与取瓦片侧各一道闸（存量库里可能已经
存着这样的值，校验只管新写入）。判据抽到 `tile_url_probe.is_link_local_host`，
与 `should_bypass_proxy` 分开 —— 后者把回环与私网也算进去，是「该不该走代理」的
路由判断而不是安全边界。**已知缺口**：域名形式的元数据端点（如
`metadata.google.internal`）判不了，补它需要取瓦片前解析 DNS 并防 rebinding。

**这不改变部署前提本身**：零鉴权 / 绑 `0.0.0.0` / CORS `*` 仍是 2026-07-31 记录的
已接受决策，仍等重签。收紧的只是「服务端会替浏览器去取的那个地址」这一处。

## 结论

**四条 P0，都不是安全问题。** 安全姿态这一块本次**没有新发现**——零鉴权 / 绑 `0.0.0.0` /
CORS `*` / 凭据明文早在 `2026-07-31-code-only-review.md:5,20-25` 被显式接受为设计决策。
本次能补充的只有一条：**那个决策的爆炸半径在之后 8 天里翻了一倍多，而决策记录没跟上**（见
「安全姿态」一节，含实测）。

四条 P0 按性质分两类：

| 类 | 条目 | 一句话 |
|---|---|---|
| 静默产出错数据 | T1 幽灵地形瓦片 | DEM 边界外多出一整行瓦片并声明 `available`，用户看到真实地形旁贴着假高原，HTTP 全 200、作业 completed、零报错 |
| 静默产出错数据 | T2 底图迁移半拷贝 | 224MB 跨盘中断留半棵树，`user_version` 无条件 bump 永不重试 → 静默错高程（4154m 读成 −744m） |
| 不变量只写在注释里 | B1 `./build.sh` | 文档化的构建命令**静默 exit 1**，友好的错误提示是死代码；CI 走 `nuitka_build.py` 绕过它，所以一直是绿的 |
| 不变量只写在注释里 | B2 单实例锁提示 | 错误提示教用户删锁文件——POSIX 上这会让互斥失效，第二个实例开机就 rmtree 掉第一个实例正在写的 GB 级中间产物 |

结构性问题一条：**四条管线是四份纵向拷贝**（四套表 + 四个 manager + 四组蓝图），真正抽出来共用的
只有 `delete_task_row` / `remove_task_dir_if_safe` / `_resolve_safe_file`。拷贝已在漂移，
contour 的下载半程甚至已无用户可达路径却仍在被拷贝维护。

引擎内部质量明显高于平均（见「做得好的部分」）。缺陷集中在边界与治理，不在算法。

---

## P0

### T1 幽灵地形瓦片：DEM 边界外多出一行/列，并被声明为 `available`

`src/services/terrain_tiling/cesiumlab_terrain.py:1401,1403` 用 `floor` 算瓦片索引**上界**：

```python
ix1 = min(nx - 1, int(math.floor((src_e + 180.0) / 360.0 * nx)))
iy1 = min(ny - 1, int(math.floor((src_n + 90.0) / 180.0 * ny)))
```

DEM 四至落在瓦片边界上时上界会多算一格。granule 是 1°×1°，`src_n` 是整度，而
`(45+90)/180*2^z = 0.75*2^z` 对 z≥2 恒为整数——**多出的那一行与 DEM 零重叠**。

实测（用仓内 `GeographicTilingScheme`，DEM 覆盖 lat[44,45]）：

```
z=5 ny=32  iy0=23 iy1=24  rows emitted=2
   row 23: lat[  39.375,  45.000]  overlap = +1.000°
   row 24: lat[  45.000,  50.625]  overlap = +0.000°   <-- 零重叠，仍出图 + 声明 available
z=8 ny=256 iy0=190 iy1=192 rows emitted=3
   row 192: lat[  45.000,  45.703]  overlap = +0.000°   <-- 同上
```

后续链条：这行瓦片进 `_iter_tasks` 正常出图，`DemSampler.sample` 外扩 1px 后 `ix/iy` 被
`np.clip` 钳死，于是整张瓦片是 DEM 最北一行像素向北抹平的台地（M12 注释里实测过的
`hmin=913 hmax=2502` 就是这个形态）。Cesium 取「首个声明可用的层」，不会回落到 base/parentUrl。
东侧 `src_e ∈ {0,±45,±90,±135}` 同理多一列。

M12 收窄 `available` 就是为了堵这条链，这是残留的 off-by-one。
修法：上界改半开区间语义，`ix1 = min(nx-1, max(ix0, math.ceil((src_e+180.0)/360.0*nx) - 1))`，
`iy1` 同理——非边界值时与 `floor` 完全等价。

### T2 随包底图迁移：半拷贝 + 无条件 bump user_version

`src/core/database.py:318` `if old_dir.is_dir() and not new_dir.exists(): shutil.move(...)`。
跨文件系统时 `shutil.move` 退化为 `copytree`+`rmtree`，可被中断并在 `new_dir` 留半棵树；
`os.walk` 先拷根级文件，所以中断后典型状态是**有 `layer.json` 没有瓦片层级**——而
`terrain_tiling/layer_json.py:67` 的 `parent_url_if_base_available` 只看 `layer.json` 判可用。

更糟的三处顺序：config 行在 move **之前**就切到 `_NEW_BASE_PATH`；`except OSError` 只 log；
`PRAGMA user_version = 3`（`:330`）在 `try` **外面**无条件执行。于是半成品状态永久化、永不重试，
而日志声称「新位置会重新解压」。

这正是该函数 docstring（`:284-291`）说自己要堵的 v0.2.8 heightmap 陷阱；同仓
`base_terrain.ensure_base_unpacked`（`:308-312`）已经用「解压到 tmp 再 rename」解过同一问题。
修法：move 到 `new_dir.with_name(name + '.part')` 再 `os.replace`，rename 成功后才切 config 行和
bump `user_version`。

### B1 文档化的构建命令 `./build.sh` 静默失败

`build.sh:26` `REQUIRED_GDAL=$(grep -oE '^GDAL==[0-9.]+' requirements.txt | ...)`，
而 `requirements.txt:28` 是 `GDAL>=3.8,<4`，且 `:17` 明写「⚠ 这里【不能】用精确钉」。
`set -euo pipefail` 让失败的 grep 在**赋值处**就终止脚本——`:27-30` 那句友好的
`Error: requirements.txt 缺少 GDAL== pin` 永远不会被打印。

实测 `bash -x build.sh`：

```
+ uv pip install -r requirements.txt
Checked 13 packages in 4ms
++ grep -oE '^GDAL==[0-9.]+' requirements.txt
+ REQUIRED_GDAL=          <-- 脚本在此退出，exit=1，零错误信息
```

`build.bat:26-31` 同一道死门，但 Windows 上会打印错误再 exit 1（响亮失败）。
CI 从不走这两个脚本（`.github/workflows/build.yml:161` 直接 `python nuitka_build.py`），
发版一直绿——这就是它长期没被发现的原因。

连带两条：`docs/guides/INSTALL.md:120` 让读者去改 `requirements.txt` 里的 `GDAL==3.8.4`，
该行不存在。修好版本门之后的下一个坑是 `build.sh:19` 的 `uv pip install -r requirements.txt`
带 build isolation，产出的绑定缺 `_gdal_array`（`requirements.txt:22-23` 自己写明了），
而版本门只读 `gdal.__version__` 检不出来——exe 能构建、能起、能服务 `/`，但所有
DEM/地形/等高线作业全炸。

修法：门改成 `uv run python -c "from osgeo import gdal, gdal_array; print(gdal.__version__)"`
比对 `>=3.8,<4` 范围；失败时打印 `docs/guides/BUILD.md:66-68` 的 `--no-build-isolation` 咒语。

### B2 单实例锁的错误提示会让互斥失效，并毁掉在跑的中间产物

`src/app_factory.py:125` 告诉被拦下的用户「若确认上一个实例已崩溃退出，删除
`data/.terraforge-instance.lock` 后重试」。但 `single_instance.py:77-92` 锁的是**打开句柄
对应的 inode**（`fcntl.flock` / `msvcrt.locking`），不是路径：POSIX 上删掉文件后下一次启动走
`path.touch()` 建**新 inode** 并锁住它——两个实例都认为自己持锁。

实测（复刻 `single_instance.py:66-95` 的 POSIX 分支）：

```
instance A acquired: True
instance B blocked  : True (correct)
p.unlink()                      # <-- 正是错误提示教用户做的事
instance C after user deletes lock file: ACQUIRED -> two live instances
A still holds its lock on the old inode: True
```

后果正是这个锁存在的理由（`app_factory.py:106-113` 自己写了）：第二个实例的
`sweep_startup_residue()` 会 rmtree 掉第一个实例正在写的 GB 级 `map_dl_stitch_*` /
`contour_warp_*` 工作目录（窗口数分钟到数十分钟），四轮孤儿恢复把它 `running` 的任务改判
`paused`。提示的前提也是错的——硬杀之后 OS 已经释放了锁，根本不需要删。
（Windows 偶然免疫：CPython 的 `open()` 不带 `FILE_SHARE_DELETE`，unlink 会失败。）

修法：删掉那条 bullet，改成「锁在进程退出时由系统自动释放，无需手动删除」，
只保留「切到已打开的窗口」与 `TERRAFORGE_ALLOW_MULTI_INSTANCE` 两条。

---

## 安全姿态：不是新发现，但爆炸半径变大了

`2026-07-31-code-only-review.md:5` 立了部署前提：**「本程序运行在安全可信环境下，默认无鉴权
是可接受的设计决策，不构成缺陷」**，并在 `:16-29` 记录了两个被接受的暴露面：

- **A** 零鉴权 + `0.0.0.0` + CORS `*` + 凭据明文（含 `GET /api/config` 与 `index.html` 的
  `{{ config|tojson }}`——与今天完全一样，本次实测只是再次确认，不算新发现）
- **B** `verify_tile_url` 的 SSRF **探测**面（返回 status_code/content_type/bytes_read）

**这份清单已经过期。** 决策之后 4–8 天里新增了三处更强的接收面，且都没有回写进那份记录
（首次提交日期由 `git log --diff-filter=A` 确认）：

| 新增面 | 引入 | 比原清单强在哪 |
|---|---|---|
| `terrain_global_base_path` → `/terrain/base/<path>` | 2026-08-04 `7576b4e` | 从「配置可读」升级为**任意文件读**：路径类配置键零校验，根设成 `/` 后 `_resolve_safe_file` 的 `relative_to` 检查恒真 |
| `basemap_source` 接受任意 http(s) 模板 | 2026-08-07 `7c3aa56` | 无主机白名单、无内网/回环拦截（只要求 netloc 非空 + 含 `{z}{x}{y}`） |
| `/basemap/{z}/{x}/{y}` 后端转发 | **2026-08-08 `843b1fe`（HEAD）** | 从「探测 oracle」升级为**全读型 SSRF**：`opener.open()` 取回 body，`Response(body, ...)` 原样回吐 |

三条都实测跑通了（本机起靶、跑完即回滚 DB 与配置）：

```
# 任意文件读
PUT /api/config {"terrain_global_base_path":"/"}   -> 200 {"updated":["terrain_global_base_path"]}
GET /terrain/base/etc/passwd                       -> HTTP 200, 1427 bytes
                                                      root:x:0:0:root:/root:/bin/bash

# 全读型 SSRF（靶：python -m http.server 9999 --bind 127.0.0.1）
PUT /api/config {"basemap_source":"http://127.0.0.1:9999/index.html?z={z}&x={x}&y={y}"}
GET /basemap/1/0/0  ->  HTTP/1.1 200 OK
                        INTERNAL-SECRET-TOKEN-abc123

# 凭据明文（原清单已记录，此处只是确认今天仍然如此）
PUT /api/config {"earthdata_password":"S3cr3t-NASA-pw","proxy_url":"http://alice:hunter2@10.0.0.9:8080"}
GET /api/config -> earthdata_password = S3cr3t-NASA-pw ; proxy_url = http://alice:hunter2@10.0.0.9:8080
GET /           -> value="S3cr3t-NASA-pw" ; value="http://alice:hunter2@10.0.0.9:8080"
```

**在「可信环境」前提下这三条依然不是缺陷**（本机用户读自己的文件、探自己的内网，本来就是权限内的）。
真正需要动的是三件事，按重要性：

1. **有一条与前提无关的内部矛盾，是真缺陷**：`src/services/basemap_source.py:116-128` 的
   `client_descriptor` 特意剥掉 `upstream`，docstring 写明「前端一旦拿到上游地址就会有人图省事
   直连回去」；而 `templates/index.html:439` 的 `const config = {{ config|tojson }};` 把
   `tile_servers`（`mts0,mts1,mts2,mts3`）连同另外 44 个键一起递回浏览器——同一约束的第二道门
   没关。`map.js` 实际只读 6 个键（`default_save_path`、`default_zoom_min/max`、
   `map_center_lat/lng`、`map_initial_zoom`）。**换成 6 键白名单即可，与部署前提无关。**
2. **重新签一次那个决策**，并把上表三条补进 `2026-07-31` 那节的清单（或在本文档接管）。
   决策本身可以不变，但「已知并接受」的对象必须是当前真实的清单——现在是 5 项而非 2 项，
   且新增的三项分别是任意文件读与全读 SSRF，比原清单的量级高一档。
3. **补两处与前提无关的一致性缺口**（成本都在分钟级）：
   - `src/services/config_manager.py:339` 的 `validate_config` 只覆盖 10 个键，其余
     （**含全部路径类与 URL 类键**）落到 `return True`。这不只是安全问题：`stitch_tmpdir`
     写错会让 `download_engine.py:1028` 的 `os.makedirs` 在任意位置建目录，
     `terrain_base_parent_url` 写错会把 `layer.json` 的 `parentUrl` 指向任意外部源。
   - `src/routes/terrain_static.py:75-90` `_resolve_safe_file` 的 docstring 声称
     「base_dir 全部来自 DB 任务行或管理员配置，不是请求方输入」——对 `/terrain/base` **不成立**。
     注释与行为矛盾，比缺检查更危险。

关于 `verify_tile_url`：原清单已接受，本次无新增，不重复列。

---

## P1

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| 1 | `task_manager.py:828`（`try` 开在 `:829`）+ `_run_task:749` 只 log | `get_connection()` 或 `asyncio.run` 建 loop 失败会绕过所有状态补偿。**已复核代码** | 任务行永久 `running` 且无线程；`start_task:429` 拒绝，只能先点暂停或重启进程 |
| 2 | `task_deletion.py:242-259` | 停止标志先置（`:240`），DELETE/commit 失败只回滚不清标志 | 同上永久 `running`；`:285` 注释「只能重启进程」是错的，掩盖了廉价修法 |
| 3 | `task_manager.py:414-416` + `:1430` | `stitch_tiles_with_gdal` 整段无取消点，注释自称「十分钟级」 | UI 显示「已暂停」，而每次点恢复都返回 400「already running」——两个互相矛盾的事实 |
| 4 | `task_manager.py:892-935,1028` | 全网格物化三个 list + 一个 per-tile dict，抵消引擎的惰性消费设计（`download_engine.py:825-826` 明写 tiles 可以是生成器） | 硬上限已改软告警，百万级瓦片任务可打爆桌面内存；两个并发任务翻倍 |
| 5 | `database.py:262` | M10 归一化用 `resolve_stored_output_dir`，而 DEM 侧读写用 `resolve_output_dir`——子代理实测**每种相对形式结果都不同** | 升级后旧 DEM 产物指针被改错：`/terrain/dem/<id>` 404、恢复即全量重下、`delete_files=true` 报成功而真产物滞留且不进 `pending_deletions` |
| 6 | `task_deletion.py:272` | 快路径把「可删」当「已删」（`rmtree(ignore_errors=True)` 恒返回 True），另两个消费者都会复查（`:156`、`:471`），这一个不复查 | Windows 上文件被占时返回 `files_removed: true` 而整个瓦片金字塔留在盘上，且没写 `pending_deletions` → 启动清扫永远收不回 |
| 7 | `dem_task_manager.py:426-430` | 逐瓦片进度回调里的 sqlite 写没兜底（同函数的 emit 有） | 一次 `database is locked` 作废跑了几小时、99% 已落盘的切片作业；切片无恢复模型，重跑从 z8 全量重算 |
| 8 | `cesiumlab_terrain.py:1590-1595` | `_WORKER_SAMPLER` 的释放整段挂在 `if temp_input:` 下，而单输入 tif 时 `temp_input` 恒为 None（`:1385`）。**已复核代码** | 串行路径（`workers==1` / `total<=4` / `BrokenProcessPool` 回退）泄漏一个打开的 GDAL dataset；Windows 上源文件被占，删除任务报成功而文件留下 |
| 9 | `earthdata_client.py:63` | `if "urs.earthdata.nasa.gov" in loc` 是**子串**匹配，随后把 BasicAuth 明文凭据发给该 URL。文件顶部 import 了 `urlparse` 却全文未用。**已复核代码** | `https://attacker.example/cb?next=https://urs.earthdata.nasa.gov/oauth` 通过判据。前置条件是上游存在开放重定向或 TLS 被攻破，不是无条件可触发 |
| 10 | `contour_task_manager.py:185-188,317-319` | 颜色只查 `#` 前缀，`#zzzzzz` 一路通到渲染才在 per-tile `except` 里被吞 | 每张瓦片 failed → 报「No contour tiles rendered (check DEM coverage / interval / zoom range)」，指向三个都正确的参数；同样的值 `/api/contour/style_preview` 会 400 |
| 11 | `contour_engine.py:444-449` | per-tile 等高线级数无上限（预览侧有 `2<=n<=200` 闸门），瓦片**内部**无停止检查 | `interval=0.1` + 1000m 起伏 ≈ 单瓦片 1 万条 trace，且暂停/删除都打不断 |
| 12 | `.github/workflows/build.yml:200-207` | 打包前清理只删 `data`/`downloads`/`cache`/`smoke.log`，漏了 smoke test 产生的 `logs/terraforge.log` 与 `.base_unpack_<pid>_*` | 每个发布归档带一份含 CI runner 路径的日志 + 一份未完成的 4.3 万文件解压残留 |
| 13 | `requirements.txt` + 三处安装 | `nuitka` 裸装 latest，而 `nuitka_build.py:219,229-238` 调其**私有 API**（8 个 kwarg）；`matplotlib` 完全无版本约束 | 上游发版会在 tag 推出去**之后**打断 Windows 构建；同一 commit 两次构建不可复现 |
| 14 | `history.js:13-14` | `initHistoryMap()` 是 async 却没 await，`loadHistory(1)` 与它竞争 | 历史响应先到时 `renderHistoryMap` 在 `:242` 直接 return，独立 `/history` 页无任何恢复路径，小地图静默空白 |
| 15 | `templates/index.html:407,416,418` | 三个状态栏控件是绑了 click 的 `<span>`，无 `tabindex`/`role`/keydown | 键盘用户无法从状态栏打开任务面板、无法复制坐标或选区 bbox |
| 16 | 三处 `.status-chips` + `index.html:383,395` | 选中态只有 CSS class 无 `aria-pressed`；面板行为上是模态（backdrop + Esc）却无 `role="dialog"`/焦点管理 | 读屏用户读不出当前选中的状态筛选/主题/语言；面板打开后 Tab 走到被 backdrop 遮住但仍可聚焦的控件上。`ui.js:128-241` 的自定义 confirm 已把这套做对了，照抄即可 |
| 17 | `map.js:1978` | `showToast(..., 'error')`，而 `ui.js:23` 的 `VALID_TYPES` 无 `'error'`，`:43` 静默降级为 `'info'` | 剪贴板降级失败（正是 LAN `http://IP` 非安全上下文这条路）时给出蓝色 ⓘ 提示，读起来像成功 |

## P2 · 结构性（择要）

- **contour 的下载半程无用户可达路径却仍在维护**（`contour_task_manager.py:867-891`，约 110 行
  从 `dem_task_manager` 拷来）：两个构造器硬编码 `dataset='upload'`/`'dem_task'`，
  `has_local_source` 直接跳过该分支，只有测试合成的 `ASTGTM.003` 行还在喂它。拷贝不可被执行
  → 必然漂移，已漂三处（emit 节流的 counts 豁免、per-callback `SELECT *`、终态 emit 未包 try），
  而注释仍声称与 dem 对齐。建议删除，留一行守卫。
- **`contour_task_manager.py:462-463` 的暂存目录清理是死代码**：`:448` 的 `return task_id`
  在外层 `try` 内退出。同族的 `local_terrain_task_manager.py:249` 因为后面还有语句所以**会**执行。
  每个成功的上传任务泄漏一个空 `contour_upload_*` 目录。**已复核代码**
- **`ProcessPoolExecutor` 在 Linux 上用默认 fork 启动多线程进程**（`contour_engine.py:814`、
  `cesiumlab_terrain.py:1516`）。本次测试运行里出现
  `DeprecationWarning: This process (pid=81862) is multi-threaded, use of fork() may lead to
  deadlocks in the child`。Windows 走 spawn 不受影响；Python 3.14 将把 Linux 默认改为 forkserver。
  建议显式 `mp_context`。
- **`task_cleanup.py:76` 的 `*.part.*` 登记表漏了第三个生产者**：
  `task_manager._stream_copy_tile:159` 往**任务输出目录**写 `<name>.part.<thread_ident>`，
  而 `_sweep_cache_part_files` 只走 `CACHE_DIR`；名字里放的是线程 id 而 `_part_owner_pid`
  期望 pid，将来把该根纳入清扫时归属判断会失效。
- **contour 产物位置有三个消费者、两套根**（`contour_static.py:57` 重算 `DOWNLOADS_DIR/dem`、
  writer 用存储值、deleter 用 `resolve_stored_output_dir`）。今天巧合一致；frozen exe 被移动后
  （`terrain_static.py:219-225` 记录为真实场景）永久 404。
- **`process_entry.py:24-29` 的 `freeze_support()` 在本项目产出的任何构建里都是 no-op**，
  而注释称它是「frozen 下真正有效的拦截点」。三个独立原因：非 win32 直接短路；win32 还需
  `sys.frozen`，而它到 `app.py:25` 才被设上；frozen worker 走不到 Python `__main__`
  （Nuitka 的 C bootstrap 分派到 `__parents_main__`，`_MP_RERUN_NAMES` 已正确排除）。
- **`build.yml:58` 的 `shell: bash -l {0}`** 覆盖了 GitHub 默认的
  `bash --noprofile --norc -eo pipefail`，全工作流多命令 step 因此丢掉 `set -e`/`pipefail`：
  「Install GDAL (Windows/macOS)」以一句恒成功的 `echo >> $GITHUB_ENV` 收尾，conda 装失败会报绿。
- **`single_instance.py:93-95` 把任何 `OSError` 当成「已有实例」**：NFS/CIFS/部分 FUSE 上
  `flock` 的 `ENOLCK`/`ENOTSUP` 与 `EWOULDBLOCK` 走同一分支，数据目录在网络盘上会被
  「另一个实例正在运行」拒绝启动。
- **`nuitka_build.py:403-406` 只校验 GDAL/PROJ 数据落地，不校验 `templates/`/`static/`**：
  CI smoke 只断言 `/` 返回 200，从不碰 `static/vendor/`。缺一个 Cesium 文件 → 用户端白图、
  日志里零错误。
- **DEM 删除默认 `delete_files=false`** 时（`dem_api.py:103-110` 不传 artifact_dir），
  半成品目录既不删也不进 `pending_deletions`，从此没有任何 DB 引用，启动清扫也扫不到——
  永久留在用户磁盘上。
- **`dem_task_tiler.py:150-151`**：协作停止后仍执行底图植入（4.3 万硬链接）与 layer.json 合并；
  植入失败会把一个**用户主动停掉**的作业记成 `failed`，错误文案指向底图。
- **`cesiumlab_terrain.py:1462,1577-1583`**：全部瓦片失败时 `meta.json` 写出
  `"minHeight": Infinity`（`json.dumps` 默认允许），非法 JSON。今天无消费者，是给下一个消费者埋的雷。
- **`hillshade_preview.py:114-121`**：渲染失败时 `.tmp.png` 不清理，五类启动清扫都不匹配这个名字。
- **dead code / 失真注释**：`.btn-info` 等 6 个 Bootstrap 覆盖类零引用（`style.css:1682` 起约 40 行）；
  `history.js:164` 的 `HISTORY_UNKNOWN_ERROR` 与它 4 行的注释约束都没有读者；
  `history.js:54` 的 `typeof basemap !== 'undefined'` 快路径在每个页面都是死的
  （`basemap` 是 `initMap` 的**函数参数**不是全局），而它多打的那次 `/api/basemap` 正是 P1#14
  竞争的成因；`_config_content.html:233` 与 `database.py:60` 都写着「底图走浏览器直连」——
  与 `/basemap` 转发路由的整个前提相反；`cesiumlab_terrain.py:1020,1031,1237` 两处死变量长得像 worker 状态。
- **`index.html:86,96` 与 `_config_content.html:215` 把 `_macros.html` 已有的三个图标又内联了一遍**
  （31 处 SVG 收敛为 23 个几何体，6 个重复）；`base.html:126-173` 一个组件占掉全模板树
  18 个内联 `style=` 中的 17 个。

## 文档一致性

| 位置 | 问题 |
|---|---|
| `src/core/config.py:38` vs `RELEASE_NOTES.md:1` | `APP_VERSION='0.2.11'` 而发版说明写 v0.2.12，tag 只有 v0.2.11。`push-release.sh` 从 APP_VERSION 取 tag，无参发版会响亮中止（好）；危险的是文档化的 `./push-release.sh 0.2.12`——banner 印 0.2.11 而 Release 标题是 0.2.12 |
| `CLAUDE.md:168` | 写「no `conftest.py`」，而 `tests/conftest.py` 存在（236 行）且要求新测试必须用 `fresh_import`/`isolated_app`；`CLAUDE.md:74` 自己又按名引用了它 |
| `CLAUDE.md` 全文 | grep `basemap` 零命中。两条最容易被下一个人破坏的硬约束（禁 GCJ-02 源、上游 URL 不出服务端）只活在 `basemap_source.py` 的注释和两个测试里 |
| `docs/reviews/2026-07-31-code-only-review.md:16-29` | 「已知并接受」的安全清单是 2 项，实际已是 5 项（见「安全姿态」一节）。按 `docs/reviews/README.md:8`「正文不回改」的约定，应由本文档接管当前清单 |
| `README.md:214-291` | 8 条现役路由未记录，含每个页面都依赖的 `/basemap/<z>/<x>/<y>`、`/api/basemap`、`/api/config/proxy_status` 与四条 hillshade 路由 |
| `README.md:84-86` | 快速开始只给 `uv venv` + `uv pip install -r requirements.txt`，正是 `INSTALL.md:47-59` 说会产出坏 GDAL 绑定的那条命令；`README.md:365-370` 则在教用户修 README 自己造成的故障 |
| `README.md:156-212` | 项目结构树是 2026-08-04 快照，漏掉整个 `src/i18n/`、`src/app_factory.py`（它才是组合根，树里说是 `app.py`）、`src/core/` 11 个文件里的 6 个 |
| `README.md:58` / `docs/README.md:14` / `docs/reviews/README.md:24` | 「90+ 测试文件」实为 139；superpowers 计数 10/11 实为 15/17；「48 个文件」实为 57 |
| GDAL `_gdal_array` 陷阱 | 在 4 份文档 + 2 个 shell 脚本里各写一遍，pin-vs-range 政策已漂移到互相矛盾（即 B1） |

建议归属：`INSTALL.md` 独占 GDAL 安装与恢复；`BUILD.md` 独占构建/发版流程（含 APP_VERSION bump）；
`CLAUDE.md` 独占架构不变量（需新增 basemap 一节）；`README.md` 独占功能巡览 + 端点清单，其余只链接。

## 测试套件

139 个测试文件 / 1859 KB 源码。分组（命令测得）：122 文件 1128 KB 走真实代码路径；
16 文件 701 KB 是对 JS/CSS/HTML/脚本的**源文本契约测试**；1 文件 i18n 目录完备性；0 个纯快照。

- 两个巨型文件（`test_css_contract.py` 427KB/8270 行/80 测试、`test_tasks_js_contract.py`
  98KB/1915 行/49 测试）**是手写的、承重的**回归网，不是脆弱的文本匹配：实现了带 `@media`
  处理的花括号深度 CSS 规则扫描器、含 WCAG 对比度计算的特异性/层叠模型、处理相邻 margin 折叠的
  盒模型，以及用 AST 解析 `src/models/task.py` 交叉核对四个 manager 实际写入的状态字面量。
  每个测试的 docstring 记录了「证明朴素断言无效」的变异实验。脆弱点集中在约 5 个断言
  （vendor 字节清单、`!important` 上限 68、Bootstrap 精确版本 pin、两处「恰好 1 条规则」前置条件）。
- 覆盖缺口（按名无专属测试文件）：`task_manager.py`(82KB)、`cesiumlab_terrain.py`(88KB)、
  `routes/api.py`(43KB)、`local_terrain_task_manager.py`(30KB)；`dem_task_manager.py`(45KB)
  只有一个 2.5KB 单一关切的文件；`process_entry.py` 在 `tests/` 下零引用。
- `test_fix_*.py` 家族 55 个文件（39.6%）：主题批次（infra_e / l1_entry_build_misc /
  api_hardening / gdal_silent_failure_gaps）是有意结构，20 个 `test_fix_dem_*` 是沉积——
  22 个文件可并成 5 个且不丢断言。
- 反模式计数：13 文件断言日志字符串（多数合理——凭据脱敏没有别的可观察面）、11 文件 sleep
  （6 处阻塞在测试线程）、0 真实网络、0 写 `tmp_path` 之外、0 已知顺序依赖
  （由 `test_conftest_isolation_contract.py` 的双向 AST 棘轮守着）。

## 做得好的部分（不是客套）

- **异步/线程纪律**：aiohttp session 每批一次 `async with` 且自持 connector；协作取消做在三层
  （semaphore 队列、请求前、可中断 backoff）；重试有界且对永久 4xx 短路；进度批处理的
  「事件循环里摘、工作线程里写」正确识别了 `sqlite3` 在 `executemany` 中途释放 GIL。
- **GDAL 侧「不报错但产物是坏的」每条路径都有闸门**：`_assert_no_input_dropped` 用
  `GetFileList()` 对账、`_verify_materialised` 双角点全分辨率比对 + overview 塌陷探测、
  `_raise_on_gdal_error` 配 `ExceptionMgr` 就地钉死非异常模式。`DemSampler.sample` 把降采样格网
  锚定到源像素网格并显式补偿 GDAL 中心制重采样的 `0.5*(1-1/sx)`——这段最难写对，且确实写对了。
- **设计令牌纪律是同体量文件里最好的**：声明块外**零**硬编码 hex/rgb/hsl（35 处 raw 命中全在
  记录对比度的注释里）、零悬空 `var()`、56 个可主题化令牌深浅色全配对、仅 1 个未使用令牌。
  离线约束完整且被 4 个契约测试机器化守住。三条命名的布局地雷（面板/backdrop 全高、
  `--statusbar-clearance` 仅 3 个消费者、Bootstrap `.row` 不进零内边距容器）全部被遵守。
- **前端状态层**：`task_store.js` 是真正的单一数据源（响应式 + O(1) 键索引 + 时间流/活动集分离），
  `tasks.js`/`task_list.js` 确实交出了 DOM 写权。转义纪律扎实（Vue 插值 + 每个残余字符串 sink 都过
  `escapeHtml`），XSS 排除、上游 URL 不出服务端排除、监听器泄漏仅 1 处（`map.js:150`）。
- **删除/墓碑/`pending_deletions` 设计正确**：主评审确认「没有任何路径能删到任务输出目录之外」，
  四张任务表的 `AUTOINCREMENT` 挡住了 id 复用这条唯一的陈旧队列命中活任务的路。
- **启动身份判定**：八种进程身份逐一走过 `detect_startup_role`，**没有**一条路径让非服务进程
  走到 `create_app()` / `init_database()` / 绑端口。
- **`tests/conftest.py`** 是套件里工程质量最高的文件：session 级 autouse 沙箱阻止测试 rmtree
  真实 `/tmp`、阻止把 224MB 解压进 `assets/terrain/`。

## 建议修复顺序

1. **B2 + B1**：各 10 分钟量级，一条防数据毁坏、一条让文档化的构建可用。
2. **T1**：瓦片索引上界改半开区间（非边界值时与 `floor` 等价）——静默错产出，优先级高于任何重构。
3. **T2**：底图迁移改 `.part` + `os.replace`，rename 成功后才切 config 行、才 bump `user_version`。
4. **P1 #1/#2**：给 `_run_task` 的 except 加状态补偿、`get_connection()` 移进 `try`、
   删除失败时清停止标志。三处小改，消掉「永久 running」这一整类。
5. **安全姿态第 1、3 项**：`index.html:439` 换 6 键白名单；给路径类/URL 类配置键补校验。
   与部署前提无关，属于内部一致性。
6. **P1 #5**：`output_path` 收敛到一个解析器（`resolve_stored_output_dir`），删掉 DEM 侧的两份。
7. **治理**：`APP_VERSION` bump + 一个断言它等于 `RELEASE_NOTES.md` 顶部版本的契约测试
   （`tests/test_fix_build_scripts.py:92-99` 已有解析器）；`CLAUDE.md` 补 basemap 一节、
   修 `:168` 那句 conftest。
8. **重新签部署前提决策**，清单更新为当前 5 项。

## 本次未评审

- `cesiumlab_terrain.py` 未提交的 TVD 实验段（`_TVD_RATIO` / `_full_grid_tris` / `_border_mask` /
  `_tvd_mesh`）：按约定跳过。已确认它无法从生产配置到达（`import tvdnb` 在函数内部，
  `triangulator='tvd'` 在 UI/DB/API 都无入口）。
- `static/vendor/` 下的第三方库（Cesium / Vue / Bootstrap / socket.io）。
- `CHANGELOG.md` 顶部约 65 行之外的历史条目；`docs/{superpowers,archive,notes,reference}` 正文
  （`docs/README.md:26-33` 已声明为非权威快照）。
- 真实浏览器驱动：所有 UI 结论来自源码 + 一次 Flask test client 渲染，未观察真实 tab 序列。
- 一次完整 Nuitka 构建（只读评审，且整包编译不是有针对性的验证）。
- 迁移在**真实存量库**上的行为：仓内 `data/map_downloader.db` 十二张表全空，P1#5 与 T2 由代码
  与解析器直接比对得出，未对真实旧库执行迁移。
