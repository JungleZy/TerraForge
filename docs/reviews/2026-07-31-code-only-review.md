# 全项目代码审查报告（2026-07-31，只以代码为准）

审查方式：7 个只读审查代理分区通读全部代码（约 1.5 万行 Python/JS/HTML + 71 个测试文件），所有 docs/README/注释的自我声明均被忽略，每条结论回验实际代码行为并附 file:line。测试套件实际运行验证：612 passed / 0 failed（104s）。

**部署前提（2026-07-31 确认）：本程序运行在安全可信环境下，默认无鉴权是可接受的设计决策，不构成缺陷。** 因此原 HIGH 级安全项（零鉴权、凭据明文可读、SSRF 探测面）已从 HIGH 移除，移入下方"部署前提与安全姿态"章节作为已知事实记录，不再计入待修复问题。

## 修复进度

**截至 2026-07-31，本报告全部 HIGH、MEDIUM、LOW 项及已知遗留①-④均已处理完毕，最终全量测试 762 passed / 0 failed（基线 612 → 762，净增 150 个行为测试）。**

- **HIGH #1-#8：已全部修复**。要点：#1 output_path 归一化后入库 + 存量相对路径读取侧归一化（`task_cleanup.resolve_stored_output_dir`）；#2 枚举对账时批量清理 cache 命中的残留 failed 行；#3 删除检查收敛进 manager 层 `_state_lock`（dem 新增 tiling job 检查、local_terrain 补锁+线程检查）；#4 引擎侧 `_report_progress` 包装回调，回调异常只记日志不再触发重试/击穿 gather；#5 等高线渲染进度 0.5s 时间节流 + 单连接复用 + 内存计数载荷 + 结束强制 flush；#6 terrain 链路生成器分批（2048/批）+ worker 封顶 4 + BrokenProcessPool 串行回退 + 逐瓦片容错返回 `{"total","rendered","failed"}`；#7 push-release 改从 `core/config.py` 解析 `APP_VERSION` 并配行为测试；#8 稀疏化迁移改用 `PRAGMA user_version` 幂等标记只跑一次，删除 `migrations/001_add_time_tracking.py` 旁路死代码（文档引用已同步清理）。
- **MEDIUM 22 项：21 项已修复，1 项（#19 采样半像素偏移）经实测前提不成立、公式未改**——GDAL bilinear 降采样的精确逆映射即现行公式，报告提议的"修正"才会引入偏移；已在代码注释写明验证结论并新增甄别测试钉住无偏行为。其余要点：#1 contour_files 补 UNIQUE + 存量去重 + INSERT OR IGNORE；#2 读取路径改走 `Task.from_row` 免校验构造；#3 `_wrapLngEast(180)` 保持 180；#4 时间戳全栈统一 UTC ISO 8601（`core/database.py` 新增 `utc_now_iso`/`parse_db_timestamp`，前端 `parseTaskDate` helper）；#5 上传改流式（FileStorage 直传 + 分块写盘）；#6 commit/start 间隙异常回补状态；#7 pause 状态翻转与时长累计同事务；#8/#9 单遍枚举 + 引擎分批协程（1000/批）+ 进度回调 0.5s 节流；#10-#18、#20、#22 按清单修复；#21 conftest 新增 `fresh_import` 统一隔离工具、双实例 hack 移除、全局 os.path 补丁收窄。#20 两条管线统一为 config 键 `terrain_base_parent_url`（已入 DEFAULT_CONFIGS）。
- **LOW：全部处理**。死代码已删（models/config.py、ctb_runner.py、build_vrt_command、compute_available_from_tiles、contour create_task 死路径、若干死分支）；矛盾注释/docstring 全部对齐实际行为；杂项小瑕疵（-c 越界、busy_timeout 顺序、4xx 不重试、重试 sleep 可中断、earthdata step-2 状态检查、占位符校验、URL 日志脱敏、coerce_number 拒 bool、contour_static 404、前端 11 项等）均已修复。新增配置键：`contour_warp_tmpdir`（已入 DEFAULT_CONFIGS）。
- **已知遗留**：①②③④ 已于 2026-07-31 修复——① 复制阶段 `task_copy_progress` emit 已包 try（socketio 故障不再打断复制）；② `fresh_import` 对 dotted 模块在重导入前记录父包属性原值，teardown 同步恢复，split-brain 缺陷消除（`tests/test_conftest_fresh_import.py` 钉住）；③ `config.updated_at` 改走 `utc_now_iso()`，Python 3.12 sqlite3 适配器 DeprecationWarning 根除（全量 warnings 23 → 3）；④ contour BrokenProcessPool 回退改为 `skip_existing` 模式：已落盘瓦片不覆盖重画、直接计数，进度条快速爬回而非长期停在 0。⑤ 待下载列表在 manager 层物化一次——经评估**维持现状**：每块瓦片全管线只物化一次、协程分批（1000/批），计数对账依赖枚举先于下载完成，生成器贯通会动摇该口径，收益不抵风险，记录为已知边界（百万级瓦片任务内存占用有界但非零）。

## 部署前提与安全姿态（已知并接受，非缺陷）

以下事实在可信环境前提下成立且被接受；仅当部署假设改变（暴露到不可信网络）时才需要处理。

### A. 零鉴权 + 0.0.0.0 + CORS `*` + 凭据明文

- `app.py:336` 绑 `0.0.0.0`，`app.py:175` SocketIO `cors_allowed_origins="*"`，全项目无任何认证；`core/startup_banner.py:128` 主动打印局域网地址引导访问。
- `GET /api/config`（`routes/api.py:696-714`）原样返回 `earthdata_password` 明文（`services/config_manager.py:118-139` `get_all()` 不脱敏，`core/database.py:33-34` 明文落库）；首页 `templates/index.html:398` 用 `{{ config|tojson }}` 把含密码的全量 config 嵌进每个响应，`templates/_config_content.html:223-224` 把密码渲进 input value。
- 所有写操作（`routes/api.py:318`、`routes/dem_api.py:78`、`routes/contour_api.py:142`、`routes/local_terrain_api.py:109` 的 DELETE 端点等）无保护。
- 若未来部署假设改变，最低成本加固：默认绑 127.0.0.1、配置读取脱敏、可选 token 鉴权。

### B. `verify_tile_url` 的 SSRF 探测面

`routes/api.py:805-836` 接受任意 URL，`services/tile_url_probe.py:57-64` 只校验 http(s) 和 `{z}/{x}/{y}` 占位符，`http://169.254.169.254/...` 或内网地址照发，并回传 status_code/content_type/bytes_read（`tile_url_probe.py:141`）。`should_bypass_proxy`（`tile_url_probe.py:108-123`）对内网目标反而直连放行，无目的地址黑名单。可信环境下这是预期功能（用户本来就要探测自建瓦片服务）；部署假设改变时需加目的地址黑名单。

## HIGH（会导致错误行为/数据损坏）

### 1. output_path "只校验不改写"，校验形同虚设且删除静默失效

`services/task_manager.py:269-270` 用 `resolve_output_dir()` 校验后却把原始值入库，`_execute_task` 直接 `Path(task.output_path)`——相对路径按**进程 CWD** 解析而非 `DOWNLOADS_DIR`，CWD≠BASE_DIR（打包 exe 从快捷方式启动）时文件写到校验范围外，且 `download_engine.py:757-762` 的白名单检查会让 image 类任务必败。删除路径（`routes/api.py:378` → `services/task_cleanup.py:38`）同样按 CWD resolve，拒删后接口仍返回 success。对照：`dem_task_manager.py:98` 存的是解析后的绝对路径——两条管线口径不一致，地图这条是错的。

### 2. 暂停/崩溃瞬间"cache 已写、failed 行未清"的瓦片让任务永久卡死

`services/download_engine.py:585-608` 先写 cache 再调回调；回调第一步就是 stop 检查直接 return（`services/task_manager.py:880-882`）。此后枚举遇 cache 命中直接 continue（:803），**没有任何路径为 cache 命中的瓦片清 failed 行**（唯一的 DELETE 在回调里，:906）；完成判定 `failed_count > 0` 恒真（:1144-1165）→ 任务失败 → 重试 → 同样失败，不自愈，用户只能手删 cache 文件。进程在 cache 落盘与 callback 之间崩溃也走同一路径。

### 3. 删除 DEM 任务不检查进行中的 tiling job

`routes/dem_api.py:78-113` 只查下载线程（`active_tasks`），tiling 线程（`services/dem_task_manager.py:302-308`）不登记、状态在 `dem_terrain_jobs` 表里完全没查。tiling 中删除会 rmtree 正在被 GDAL 写入的 `terrain_tiles/`，job 行被 ON DELETE CASCADE 删掉，最终 UPDATE 静默 0 行（`dem_task_manager.py:321-325`），磁盘留半成品。`services/local_terrain_task_manager.py:355-376` 的删除更无锁、不查 active 线程，同型竞态。

### 4. DEM 链路：progress 回调的 DB 异常被引擎当成下载失败，最终炸掉整个任务

`services/dem_task_manager.py:421-462` 的 progress 每次新开 sqlite 连接同步写库；它被 `services/dem_download_engine.py:174-220` 在 try 块内 await——sqlite 异常落入引擎 `except Exception`（:222）被记为下载失败重试（白下 30-50MB），最终 failed 回调再抛 → `asyncio.gather`（:243，无 `return_exceptions`）传播，其余 granule 协程被 `asyncio.run` 收尾取消，状态半更新。

### 5. 等高线渲染进度回调逐瓦片一次，无节流

`services/contour_task_manager.py:722-733` + `services/contour_engine.py:629-635`：每瓦片 = 新 SQLite 连接 + UPDATE + 又一连接 + SELECT 全行 + socketio.emit。高 zoom 大区域是百万级瓦片 → 百万次写事务 + 百万次广播，渲染被回调 IO 拖垮、前端被事件洪泛打爆。

### 6. terrain 链路原样保留着 contour 已修过的四个坑

同仓库平行演进、修复未横向同步（`services/contour_engine.py:657-662` 的注释白纸黑字记载了这些坑）：

- `services/terrain_tiling/cesium_terrain.py:380-406` 全量物化瓦片任务 list，大区域直接 OOM（contour 用生成器规避了）；
- `:414-416` worker 数默认 `os.cpu_count()` 无封顶（contour 封顶 4）；
- `:426-430` BrokenProcessPool 无串行回退，一个 worker 被杀整个任务失败且 `services/local_terrain_task_manager.py:38-43` 自述无 resume，几小时切片全废；
- `:179-183` 无逐瓦片容错，单个坏块 `ReadAsArray` 返回 None → AttributeError 炸全任务（contour 有逐瓦片 try/except）。

### 7. 发版脚本已断裂，且被契约测试钉成"绿"

`scripts/push-release.sh:11` / `scripts/push-release.bat:11` 仍 `grep APP_VERSION build.spec`，而 `build.spec` 已在 PyInstaller→Nuitka 迁移（commit c09a70385）中删除，不带参数运行必败。版本事实源是 `core/config.py:18`（`Config.APP_VERSION = '0.1.4'`）。`tests/test_fix_build_scripts.py:59-76` 只断言字符串形态（`'$1' in content or 'APP_VERSION' in content`），恰好被坏引用满足——脚本坏了，测试全绿。

### 8. 启动时无条件执行一次性迁移

`core/database.py:216` 每次启动都 `DELETE FROM task_tiles WHERE status != 'failed'`，无版本标记；旧库升级首次启动时几十万行 DELETE 长时间持写锁，表现为"启动卡死"。`migrations/001_add_time_tracking.py` 与 `init_database()` 内联迁移（`core/database.py:163-187`）完全重复且无人引用——两套迁移机制并存且互不感知；且该脚本在新环境单独运行会因 `tasks` 表不存在而失败，也不含稀疏化 DELETE，跑它得到的库与 `init_database()` 产物不一致。

## MEDIUM（特定条件下出问题）

1. **`contour_files` 表缺 `UNIQUE(task_id, granule_id)`**（`core/database.py:377-390`），与 `dem_files`（:268 有 UNIQUE）不一致；写入方是裸 INSERT（`services/contour_task_manager.py:318,426`），重复枚举即产生重复行、进度计数错乱。
2. **`Task.__post_init__` 校验连 DB 读取路径一起卡**（`models/task.py:162-189`）：`services/task_manager.py:619,670,754` 都用 `Task(...)` 从库行重建对象，任何历史遗留非法行会让 `get_active_tasks` 整个接口 500，一个坏行拖垮全部任务展示。
3. **`_wrapLngEast(180) === -180`**（`static/js/map.js:12-14`）：公式 `((lng-180)%360+360)%360-180` 在输入 180 时产出 -180，与 `map.js:6` 注释承诺矛盾；用户输入东经 180 被 wrap 成 -180 → `east < west` → 后端 400 拒绝，前端只弹看不懂的报错。
4. **时间戳全栈无统一约定**：`started_at`/`completed_at` 用 `datetime.now()` 存成空格分隔本地时间（`services/task_manager.py:388`），`created_at` 是 UTC `CURRENT_TIMESTAMP`（`core/database.py:149`）；前端 `new Date(...)`（`tasks.js:720`、`history.js:314`）一律按本地解析 → Safari 显示 NaN秒/Invalid Date，所有浏览器创建时间偏一个时区偏移。
5. **上传全部读进内存**：`routes/contour_api.py:92`、`routes/local_terrain_api.py:53` 把最多 100 个文件 × `MAX_CONTENT_LENGTH` 2GiB 上限（`core/config.py:51`）一次性 materialize。可信环境下无 DoS 威胁，但单请求峰值内存仍可能拖垮本机（大文件上传场景）；manager 签名 `UploadFile = Tuple[str, bytes]` 固化了该设计，改流式需动接口。
6. **commit 与 `thread.start()` 之间的异常**留下"DB 是 running、线程从未启动"的任务（`services/task_manager.py:392-433`；`dem_task_manager.py:188` 同款），靠用户重试或重启孤儿回收恢复。
7. **pause 与 resume 并发时整段运行时长丢失**（`services/task_manager.py:478-486` vs :404）：pause 先 commit 再算时长，窗口内 resume 写入新记录则 elapsed≈0，`total_running_seconds` 永久少计。
8. **0.1.4 放开瓦片数上限后三个全量物化点是真实 OOM 风险**：`services/task_manager.py:787`（待下载 List[Tile]）、`:997`（completed_tiles 全量枚举 stat）、`services/download_engine.py:703-706`（每瓦片一个协程再 gather）。
9. **每瓦片进度回调在事件循环里做同步 DB + emit**（`services/task_manager.py:928`、`services/dem_task_manager.py:421-462`），高并发下载被 DB I/O 串行化；busy_timeout 最长 30s（`core/database.py:84`）会误杀健康下载（sock_read 超时，`dem_download_engine.py:58`）。
10. **`_tile_ranges` 对 east<west 静默交换 x 范围**（`services/download_engine.py:203-206`），与 `services/geo_validation.py:11-12` 明确拒绝跨反经线的规则直接矛盾；当前靠 `Task.__post_init__` 挡住不可达，但地雷留在引擎层（west=170, east=-170 会下载几乎全球）。
11. **全量在 ASTGTM 覆盖外（|lat|>83）的选区**创建 total_files=0 的空任务并"成功完成"（`services/dem_granules.py:94-95` 返回空列表，`services/dem_task_manager.py:119-153` 不检查 `total_files == 0`）。
12. **创建失败后磁盘残留 + rowid 复用污染下一个任务**（`services/contour_task_manager.py:418-451`、`services/local_terrain_task_manager.py:121-158`）：文件先落盘，后续失败只回滚 DB 不清目录；SQLite rowid 复用后残留 tif 被下个同 id 任务的 `list_dem_tifs` 扫进渲染。
13. **`limit=-1` 绕过上限返回全表**：dem/local_terrain/contour 三条管线（`dem_task_manager.py:366`、`local_terrain_task_manager.py:210`、`contour_task_manager.py:560`）只 clamp 上限不挡负数，SQLite `LIMIT -1` = 无上限；地图管线在 `routes/api.py:119-122` 做了 `<1` 钳制。
14. **`apply_system_proxy` 丢弃 bypass 列表**，且 `all` → `ALL_PROXY` 是 aiohttp trust_env 不读的死写入（`services/system_proxy.py:53-61`）；用户系统设置里排除的内网地址会被错误走代理。
15. **`cancel` 端点缺 `except ValueError` 返回 500**（`routes/contour_api.py:208-217`），同文件其它五个端点全映射 400，复制遗漏；错误码约定全项目不统一（ValueError → 400/404/500 三种，`contour_api.py:158-160` 甚至用 `"not found" in msg` 字符串匹配决定 404/400）。
16. **`start_dem_tiling` 不校验下载状态**（`routes/terrain_api.py:24-32` → `dem_task_manager.py:242-308` 只查存在性），pending/running 的下载也能触发 tiling，在残缺数据上"成功"完成；路由 `except Exception → 400` 把服务器内部错误谎报成客户端错误。
17. **`previewTask` 地形分支竞态 + 未捕获 Promise 拒绝**（`static/js/map.js:1122-1127`）：await 期间用户点另一个预览，后 resolve 的覆盖 `_previewState`；`CesiumTerrainProvider.fromUrl` 对损坏 layer.json reject 无人捕获 → unhandled rejection，用户零反馈。
18. **临时文件泄漏**：`services/terrain_tiling/cesium_terrain.py:92-103` 多输入时 `tempfile.mkstemp(suffix=".vrt")` 的文件从不删除；`services/dem_download_engine.py:223-227` interrupted 路径在 `.part` 清理代码之前 return，留残留（与自身注释 :229-230 矛盾）。
19. **采样坐标系统性半源像素偏移**（`services/terrain_tiling/cesium_terrain.py:193-194`）：无偏双线性坐标应为 `(px - x0c)/sx - 0.5`，代码为 `(px - x0c)/sx - 0.5*(1 - 1/sx)`，偏差恒 +0.5 源像素（30m DEM 约 15m 水平位移），逐瓦片一致所以无接缝但整体平移。
20. **两处硬编码 `http://localhost:5000`**（`services/dem_task_manager.py:257`、`services/local_terrain_task_manager.py:102,258`），非 5000 端口/反代部署下 layer.json 的 parentUrl 必 404，且存量 DB 记录无法通过配置修正。
21. **测试隔离模式复制粘贴失控**：`tests/conftest.py:43-44` 只 pop `"app"`、`"core.database"`，而约 40 个文件各写一份 pop sys.modules 清单且互不相同（`test_fix_api_hardening.py:32-38` pop 10 个模块），已实测产生模块双实例——`tests/test_fix_contour_review.py:236-240` 被迫用 `view.__globals__` 绕过；测试结果依赖执行顺序。
22. **`estimate_max_level` 硬编码 `180.0 / 64.0` 无视 `self.tile_size`**（`services/terrain_tiling/cesium_terrain.py:82-86`）：默认 tile_size=17 时假设 65 顶点网格，自动层级少算约 2 级；生产链路显式传 max_level 绕开，只影响 CLI/直接调用。

## LOW（小瑕疵/死代码，选要）

**死代码：**
- `models/config.py` 整个 `ConfigModel`（除 `models/__init__.py:5` 导出外无引用）。
- `routes/socketio_events.py:13` `connected_clients` 集合只写不读。
- `services/terrain_tiling/ctb_runner.py` 全文件、`vrt_builder.py:32-38 build_vrt_command`（拼接 shell 字符串，留着是 `shell=True` 注入诱饵）、`layer_json.py:14-68 compute_available_from_tiles`——仅被 tests/docs 引用，生产路径已改纯 Python。
- `migrations/001_add_time_tracking.py`（见 HIGH #8）。
- `services/contour_task_manager.create_task` 整条下载驱动路径在 `routes/` 无人调用，且 `:281` 接受用户 `output_path` 零校验——遗留地雷。
- `services/download_engine.py:463` proxy_url 的 None 回退分支；`services/dem_download_engine.py:30` `ProgressCallback` 类型别名（注解还与实际签名不符）。
- `services/task_cleanup.py:47` 第三个条件不可达。
- 根 `__pycache__/` 里 `config/database/process_watchdog/startup_banner` 的 pyc 是重构前根级模块的残留，应清理。
- `nuitka_build.py:56-63` darwin 分支重复候选路径。

**矛盾注释/docstring（约 10 处）：**
- `services/download_engine.py:403-405` docstring 声称 `cache/task_{task_id}/{style}/...`，实际（`models/task.py:248`）是 `cache/{style}/{z}/{x}/{y}.png` 无 task 前缀。
- `app.py:152` docstring 声称返回 `(app, socketio)`，实际返回 6 元组（:245-246）。
- `services/config_manager.py:149` 声称 "re-inserts 18 default values"，实际 `DEFAULT_CONFIGS` 有 42 项；类 docstring 校验清单缺 `tile_servers`。
- `services/contour_task_tiler.py:28` 注释 "0 = auto (os.cpu_count())"，实际封顶 4；`:48` 无 DEM 时返回缺 `"skipped"` 键。
- `models/task.py:144,152` 字段注释与枚举不符（漏 `cancelled`，格式还停在 "png, jpg"）。
- `routes/api.py:646` docstring "all three task tables"，实际聚合四张表。
- `services/dem_download_engine.py:546-549` 结果 dict 可能出现 `'status': 'cancelled'`，与 docstring 声称的 "completed or failed" 不符。
- `tests/test_map_js_contract.py:93-95` 注释把已删除的 `syncBoundsFromDrawnItems` 算作调用点，与同文件 :140 的断言矛盾。

**杂项小瑕疵：**
- `app.py:31-34` `-c` 是 argv 最后元素时 `sys.argv[_c_idx + 1]` 抛 IndexError。
- `core/database.py:83-84` `busy_timeout` 设在 `journal_mode = WAL` 之后，多开实例时 journal_mode 切换不带超时直接 `database is locked`。
- `core/config.py:51` `MAX_CONTENT_LENGTH` 环境变量非法值时 import 期抛 ValueError，报错不含变量名。
- `app.py:110-113` SECRET_KEY 未配置的警告只在 `_PRINT_BANNER` 分支打印，WSGI 部署看不到。
- `core/process_watchdog.py:62-68` 看门狗不防 PID 复用，孤儿 reloader 子进程可能永不退出。
- pause/cancel 对终态任务静默成功且打误导性日志，三处行为互不一致：`services/task_manager.py:572-585`（打 "cancelled" 日志但什么都没改）、`services/dem_task_manager.py:217-223`（静默成功，与同文件 pause_task 抛错不一致）、`services/local_terrain_task_manager.py:330-351`（静默成功）。
- `services/task_manager.py:384-388` 每次 resume 覆写 `started_at`，字段名不副实。
- `services/task_manager.py:1027` + `models/task.py:78-84`：`'png'/'jpg'` 仍合法 output_format 但输出路径硬编码 `.tif`。
- `services/task_manager.py:955,977`：progress_callback 裸 `except Exception` 吞掉 DB 故障（失败瓦片静默不记录）；finally 里 `flush_progress_counts()` 抛异常会掩盖原始异常且 `progress_conn.close()` 被跳过。
- `services/task_manager.py:997-1015` + `download_engine.py:712`：`cache_enabled=false` 时 completed_tiles 恒空，image_only/tiles_only 任务零产出也标 completed。
- `services/download_engine.py:498` 对 4xx 也指数退避重试（404 白等 1+2+4 秒）；`:520` `max_retries` 为负时 `raise None` 变 TypeError。
- `services/contour_task_manager.py:634-636` 写库的 `local_path` 与引擎实际扁平落盘路径（`dem_download_engine.py:150-151`）不一致（Copernicus 嵌套路径，当前无人读，暂无害）。
- `services/contour_task_manager.py:789-799` 兜底 UPDATE 条件未排除 `'completed'`，emit 抛异常会把已完成任务改判 failed。
- `services/contour_engine.py:581-582` warp 产物固定写系统临时目录，大区域数十 GB 无配置项；`:702-713` BrokenProcessPool 回退重跑时进度计数清零，前端进度条先冲 100% 再跳回 0。
- `services/earthdata_client.py:94-95` 不检查登录跟随链响应状态，失败被吞、错误延迟且含糊；每个 granule 独立走签名解析，并发首任务对 URS 发起 N 路并行 authorize 有触发限流风险。
- `services/dem_download_engine.py:238` 重试 sleep 持有信号量且不响应 stop，暂停最多延迟 10s。
- `services/geo_validation.py:23-31` `coerce_number` 接受 JSON 布尔（`north: true` → 1.0）。
- `services/tile_url_probe.py:61-64` 含 `{s}` 等其它占位符的模板能过校验但永远不可用；`:182-186` 探测日志打印完整 URL，内嵌 `user:pass@` 时凭据进日志。
- `services/local_terrain_task_manager.py:166-171` 创建即自动 start_tiling，`cancel_task` 只翻 `pending` → 取消入口实际不可达；`:102,258` start_tiling 失败时任务行已 commit 为 pending，重试提交产生重复任务。
- `routes/api.py:347-348` 路由直接摸 `task_manager._state_lock`/`active_tasks` 私有成员（`dem_api.py:92-93` 同款）；孤儿 running 行会被 :362-365 拒删，提示"请先暂停或取消"有误导性。
- `routes/api.py:72-82` `name` 传 list/dict 时 sqlite 绑定抛 InterfaceError → 500 而非 400（dem 管线用 `sanitize_filename` 兜了）。
- `routes/contour_static.py:18-24` 不查任务存在性直接发文件，与 tiles_static/terrain_static 不一致（`_resolve_safe_file` 本身严实，无安全问题）。
- 前端：`map.js:484-501` 切下载类型不刷新瓦片预估；`map.js:1062` submitContour 误用默认 `clearBounds=true` 清掉用户选区（与自身注释约定和 submitLocalTerrain 行为矛盾）；`map.js:49-50` estimateTileCount 对跨反经线静默 swap；`map.js:158` `_baseMapUrl` 硬编码 `http://`；`tasks.js:777-814` start/pause/resume 丢弃服务端错误原因；`tasks.js:112-131` 上传型等高线任务 pending 即显示 100%"下载 DEM"；`tasks.js:716` dem/contour 任务已运行时间不累计（manager 不写 `total_running_seconds`）；`panels.js:72-77` 关闭守卫按名字而非元素判断（records/history 同元素窄触发面）；`history.js:543` 删除当前页最后一条不回退页码；`history.js:18-19` 历史小地图硬编码外网 OSM（与断网可用定位矛盾）；`tasks.js:685`、`history.js:292` getStatusText 兜底把英文状态原样渲染。
- `nuitka_build.py:93` `pkg-config` 调用无平台守卫（同文件 :52 的 gdal-config 有）；`:267-277` ldd 自检漏 exe 本体。
- `build.bat:26`/`build.sh:26` `GDAL==` pin 缺失时报错信息打印空值而非"pin 缺失"（当前 requirements.txt:14 有 pin，未来改动时暴露）。
- `tests/test_fix_nuitka_build_gdal_data.py:33` monkeypatch 的其实是全局 `os.path.isfile`；`tests/test_config_manager.py:29-44`、`tests/test_download_engine.py:28-47` fixture 直接赋值 Config 类属性不走 monkeypatch，并行或恢复异常时污染后续测试。

## 系统性结论

架构本身是健康的：core 收敛配置/DB 唯一入口（WAL + busy_timeout 统一）、manager/engine 分层、状态机全部"条件 UPDATE + rowcount"、`.part` 原子写 + 启动对账 + VRT 断言、路径安全三重防护（`_resolve_safe_file`/`resolve_output_dir`/`remove_task_dir_if_safe`）、参数化 SQL 无注入面、XSS 防护执行一致（escapeHtml/textContent/Jinja autoescape）、app.py 对 multiprocessing/Nuitka/reloader 三重进程模型的防御性处理细致、612 个测试真实覆盖关键路径（取消状态机、TOCTOU 竞态、孤儿恢复、拼接 georeference、XSS/路径穿越）。

安全姿态（零鉴权、凭据明文可读、SSRF 探测面）已确认为可信环境下的设计决策，不再列为问题。剩余发现反复出现的模式指向三个根因：

1. **"校验在一层、使用在另一层"**：output_path 只校验不改写、`_tile_ranges` 静默交换 vs geo_validation 拒绝、limit 校验有的管线挡有的没挡、前端 resetForm 参数两条上传路径不一致。需要一个"规范化后入库、下游只用规范值"的统一约定（DEM 管线已做到，地图管线没跟上）。
2. **修复不横向同步**：contour 链路修过的 OOM/worker 封顶/回退/逐瓦片容错，terrain 链路全部原样保留；删除 vs 后台线程的竞态修了三次漏了两处（地图管线手抓私有锁、dem 漏 tiling job、local 没锁，只有 contour 在 manager 里做对了）。平行管线需要共享抽象，而不是互相在注释里引用"范本"。
3. **两套状态源缺对账**：cache 与稀疏失败表之间（HIGH #2）、DB 与磁盘之间（孤儿 tif、tiling 竞态）都缺 reconciliation；schema 演进靠内联 ALTER + 每次启动 DELETE + 旁路 migrations 三件套，该收敛成版本化迁移。另：时间戳全栈无统一约定（UTC 与本地时间混存、非标准格式直接喂 `new Date`），显示层 bug 全源于此，修一处不如统一改 ISO 8601 带时区。

## 修复优先级建议

1. HIGH #1-#3（数据正确性：output_path 规范化入库、cache 命中清 failed 行、删除统一查后台线程）
2. HIGH #7（发版链：push-release 改读 `core/config.py`）
3. HIGH #4-#6、#8（DEM 回调异常隔离、等高线进度节流、terrain 链路对齐 contour 修复、迁移版本化）
4. 其余按系统性根因归并处理
