# map-download 代码审查总报告（2026-07-29）

> 由 14 个并行审查代理分模块评审后去重合并而成。原始分模块报告见会话归档（未入库）。

**总体评价**：工程质量明显高于同类"脚本改应用"项目——并发状态机（条件 UPDATE + rowcount CAS、孤儿任务恢复）、落盘原子性、路径穿越防护都有系统性思考，测试体系也罕见地扎实。但有两个系统性盲区：**安全出口完全敞开**（无鉴权 + 0.0.0.0 + 凭据明文外发 + 存储型 XSS，互为放大器），以及**地形瓦片产物从未被真实 CesiumJS 客户端端到端验证过**（两个格式级 Critical 因此潜伏至今）。

## 用户裁决记录（2026-07-29）

- C1（Earthdata 凭据明文）：**按设计，不处理**。
- 分歧1（cancel 可取消已完成任务）：**确认是 bug，已修复**（三个 manager 条件收窄为 `IN ('pending','running','paused')`）。
- 分歧2（删除任务是否清磁盘）：**删除时提示用户是否清理产物，已实现**（四个 DELETE 端点支持 `delete_files` 参数 + 前端两步确认）。
- 分歧3（hook-gdal.py 覆盖 GDAL_DATA/PROJ_DATA）：**按设计**。
- 分歧4（刷新后 failed 卡片消失）：**应常驻，已修复**（`loadActiveTasks` 白名单加入 failed）。

## 修复进度（2026-07-29 第二批）

第二批按本文档逐条修复，全部经 TDD（新增 `tests/test_fix_*.py` 共 24 个文件）+ 全量回归验证。**已修复**：C2、C3、C4、C5、C6、C7；I1–I5、I7–I20（I6 属第一批）；Minor 中的日志脱敏、API 健壮性、等高线日志/标签、前端 fetch 检查等低风险项。**明确未做**（需单独决策或属新特性）：C1（裁决按设计）、schema 迁移类（`contour_files` UNIQUE、`retry_count` 死字段、`migrations/` 旁路）、性能重构类（每瓦片 DB 广播、stitch 阻塞事件循环、等高线向量化）、ASTWBD 水体数据源接入（新特性）、backlog 裁决。C2/C3 修复后仍需按"待确认 Top 5"第 1 条做浏览器端到端实测。

## Critical（7 条）

**C1. Earthdata 凭据明文泄露**（6 份报告共同发现）——【用户裁决：按设计】
- 位置：`routes/api.py:656-674`（`GET /api/config` 原样返回 `get_all()`）、`services/config_manager.py:112-137`、`routes/main.py:31-36` + `templates/index.html:357`（全量 config 内嵌首页源码）、`templates/_config_content.html:205-206`（密码渲染进 `value=`）、`database.py:33-34`（明文落库）、`app.py:272-278`（绑定 `0.0.0.0`，全项目零鉴权）、`app.py:150`（`cors_allowed_origins="*"`）。
- 影响：同局域网任何人 `curl http://<ip>:5000/api/config` 即得用户真实 NASA Earthdata 账号密码。

**C2. quantized-mesh 三角形索引位宽选错，全部 `.terrain` 在标准 Cesium 中损坏**
- 位置：`services/terrain_tiling/cesium_terrain.py:264-267`（按 `arr.max()<=65535` 选位宽，规范要求按**顶点数**>65536 判定；high-water-mark 回绕导致实际永远写 uint32，Cesium 按 uint16 读，后续字段全部错位）。
- 已用解释器实跑编码器逐字节验证，并对照 vendor Cesium 1.143 解码代码确认。
- 修复：位宽按 vertex_count 判定；u16 分支对回绕差值 `astype(np.uint16)`。

**C3. `.terrain` 以 gzip 落盘但 HTTP 响应无 `Content-Encoding: gzip`**（2 份报告）
- 位置：`cesium_terrain.py:311`（`gzip.open` 写盘）+ `routes/terrain_static.py:83,92,119`（裸 `send_file`）。
- 与 C2 叠加 = 地形功能整体不可用。修复：落盘不压缩，或响应补 `Content-Encoding: gzip`；修后需浏览器实测。

**C4. `downloading` 状态文件成孤儿，任务被静默标记完成（数据丢失）**
- 位置：`services/dem_task_manager.py:377-383`（恢复只查 `pending/failed`）、`:458-481`（终态统计只数 `failed/pending`）、`dem_download_engine.py:179-180`（暂停时抛 stopped 直接 return）。
- 影响：暂停/崩溃时正在下载的文件永久停留 `downloading`，恢复时跳过、统计跳过，任务照样 `completed`——用户拿到缺块的"成功"DEM。

**C5. `output_path`/`task.name` 未校验 → 网络可达的任意目录写**（2 份报告）
- 位置：`services/task_manager.py:976-977`（复制阶段无校验）、`:934`（`task.name` 直接拼文件名）、`models/task.py:146-173`、`routes/api.py:37-96`；DEM 侧 `dem_task_manager.py:96` 同样。
- 修复：创建任务时统一 resolve `output_path` 并强制落在 `Config.DOWNLOADS_DIR` 内；`name` 做文件名消毒。

**C6. 存储型 XSS：用户可控字符串大量直接进 `innerHTML`**
- 位置：`static/js/tasks.js:215`（`task.name`）、`history.js:114/131/209`、`map.js:775/827`、`history.js:491`（`job.output_dir`）。
- 项目明知规矩（`error_message` 全程 `textContent`），但防护只做了那一处。修复：统一 `escapeHtml()` 或 DOM 构建 + `textContent`。

**C7. CI 从不执行测试**
- 位置：`.github/workflows/build.yml`、`test-build.yml`（名为 Test Build，实际只有 pyinstaller + `test -f`）。
- 修复：`test-build.yml` 打包前加 `python -m pytest tests/ -q`。

## Important（20 条）

### 并发与状态机

1. **SQLite 未开 WAL/busy_timeout + 读错误被吞成默认值**（3 份报告）：`database.py:74-78`；`config_manager.py:63-65,135-137` 把锁异常静默转成"配置不存在"（如 `earthdata_username` 读成 `''` → 莫名 401）。修复：`PRAGMA journal_mode=WAL` + `busy_timeout`；`get()` 区分"无行"与"出错"。
2. **start_tiling TOCTOU 竞态**（3 份报告）：`dem_task_manager.py:219-284`、`local_terrain_task_manager.py:221-263` 可并发两线程写同一输出目录。修复：照抄 `start_task` 的锁内"条件 UPDATE + rowcount"范本。
3. **DELETE 任务与运行中线程的竞态**（3 份报告）：`routes/api.py:304-349`、`dem_api.py:75-103`、`contour_api.py:142-149` 绕开 manager 锁直查 DB 再删。
4. **pause/cancel 把 `ValueError` 吞成 500**（2 份报告）：`routes/api.py:237-239,299-301`、`dem_api.py:120-155`。
5. **失败任务永远无法重试（死代码路径）**：`task_manager.py:367` 只允许 `pending/paused` 启动，而 `:762-763` 明确按 `pending/failed` 捞瓦片。
6. ~~**cancel 可把 `completed`/`failed` 改写为 `cancelled`**~~（4 份报告）：`task_manager.py:553-557`、`dem_task_manager.py:206`。**【已修复 2026-07-29】**
7. **dev 模式 reloader 父进程也跑完整 `create_app()`（含 orphan recovery 写库）**：`app.py:235-237` 只挡 multiprocessing worker。修复：守卫加 `WERKZEUG_RUN_MAIN`。
8. **并发 stitch 互删共享中间文件**（代码注释已承认未修）：`download_engine.py:968-970` vs `:791-800`。
9. **惰性导入钩子被 `sys.exit` 击穿，缺 GDAL 时 tiling job 卡死 running**：`cesium_terrain.py:36-39` 抛 `SystemExit`。修复：改 `raise ImportError`。
10. **等高线"每瓦片容错"被 try 块位置架空**：`contour_engine.py:361-404`——读窗口/`ReadAsArray`/level 计算全在 try 外。

### 数据正确性

11. **ASTGTM 勾选 `_swb` 颗粒注定失败**：`dem_granules.py:88-92`，`map.js:535` UI 仍暴露；真正的水体在 ASTWBD.001（`astwbd_v1_att_granules_for_tile` 已写好但从未被调用）。
12. **无覆盖范围/存在性过滤**：`dem_granules.py:34-60`——海洋、|lat|>83° 的 ASTGTM 颗粒必然 404。
13. **下载无完整性校验，截断文件永久污染缓存**：`dem_download_engine.py:146,70-87`（`size>0` 即视为完成）。修复：比对 Content-Length。
14. **COP-DEM 的 `dem_files.local_path` 写错**：`dem_task_manager.py:406` 存嵌套 granule_id 全路径，实际落盘是 basename。
15. **资源上限校验缺失**（一组，4 份报告）：`download_engine.py:153-159,208-213`（10 万瓦片硬上限只是 warning）；`contour_api.py:70-71`（zoom 无 0–21 校验）；`local_terrain_api.py:32`（maxzoom 无上限）；`contour_task_manager.py:298`（裸 `float()`，NaN/inf interval 可入库）。
16. **相对 `output_path` 依赖进程 CWD，与静态服务的冻结模式约定自相矛盾**（3 份报告）：`dem_task_manager.py:373,242,226-243`、`local_terrain_task_manager.py:243-244,356-369` vs `terrain_static.py:28-52,99-115`。
17. **大 DEM 低层级切片 OOM 风险**（2 份报告）：`cesium_terrain.py:152`、`contour_engine.py:361-372` 按原始分辨率整窗读入。修复：`ReadAsArray` 带 `buf_xsize/buf_ysize`。

### 功能/契约

18. **`task_completed` 的 `warning` 字段被前端丢弃**：`task_manager.py:1117-1121` vs `tasks.js:23-26,498`。
19. **等高线 skipped 瓦片不计入进度**：`contour_engine.py:603-608`——进度条停在如 72% 直接 completed。
20. **构建/发布链问题**：`build.sh:16-20`/`build.bat:14-19` 从不装依赖；`build.spec:94-123`/`hook-gdal.py` 对 gdal-data 收集失败全程静默；`push-release.sh/.bat` 硬编码 v0.0.1（当前 0.1.0）；`requirements.txt:14` GDAL pin 与 CI 系统库版本无校验。

### 测试与文档

21. **测试盲区**：`earthdata_client.py`（唯一碰凭据的模块）、`socketio_events.py`、`system_proxy.py` 零测试；`test_terrain_static.py:22` 是同义反复断言；路径穿越测试因 int converter 结构上无效；38 个文件各自复制 `sys.path.insert`，应建 `conftest.py`。
22. **文档体系滞后**（**CLAUDE.md/README.md 已于 2026-07-29 更新**）：`docs/backlog-post-0.1.0.md` 约 1/3 条目已被 Cesium 改造架空，待裁决；工作区相对 HEAD 几乎全部 modified、`static/vendor/cesium` 未提交，建议先提交固基线。

## Minor（按主题压缩）

- **凭据/敏感信息日志类**：`config_manager.py:103` 只掩码 password 不掩码 username；`system_proxy.py:47` 和 `config_manager.set` 会把含 `user:pass@` 的 proxy_url 打进日志；`earthdata_client.py:48`/`dem_download_engine.py:201` 子串匹配域名、签名 URL 可能进日志/DB。
- **API 健壮性类**：`PUT /api/config` 无 `silent=True`/无键白名单；`limit`/`page` 负数钳制不统一（`api.py:112-116,367`、`local_terrain_task_manager.py:209`）；`terrain_api.py:31-32` 所有异常以 400 + `str(e)` 返回；`MapStyle.from_shorthand(None)` 抛 AttributeError→500。
- **性能/资源类**：每瓦片 2–3 次 DB 连接 + 全行广播（`task_manager.py:794-830`、`contour_task_manager.py:605-612`）；stitch 在事件循环里同步阻塞（`task_manager.py:938`）；上传文件全量读内存峰值 ~2GB；DEM 缓存无上限；等高线索引/编码纯 Python 循环可向量化。
- **数据/schema 类**：`contour_files` 缺 `UNIQUE(task_id,granule_id,kind)`（`database.py:362-375`）；`retry_count` 两条管线都是死字段；ALTER 吞错靠匹配 `"duplicate column name"` 子串；`total_running_seconds` 在表不在模型。
- **前端类**：等高线 start 响应不检查（`map.js:678`）；`_wrapLngEast` 与注释承诺不符（±180→-180）；搜索只过滤当前页；四路 fetch 不查 `ok`；迟到 `task_progress` 可能复活已完成卡片；日期解析依赖浏览器实现；硬编码 OSM 底图与离线定位矛盾；`map.js` 全文件 CRLF。
- **冻结/打包杂项**：`default_save_path='./downloads'` 相对路径依赖 CWD（`database.py:13`）；端口 5000 硬编码且横幅先于 bind 打印；`build.spec` 的 `APP_VERSION` 未使用、dnspython 死配置；certifi 被打包但无人引用；版本号三种说法并存。
- **其他**：`contour_engine.py:479` 瓦片失败完全无日志；att warp 失败静默（`:570-571`）；`fmt="%d"` 截断非整数 interval 标签；fork 下并行 worker 继承 matplotlib 状态；看门狗 PID 复用边角；`SECRET_KEY` 每次启动随机（可接受）。

## 亮点 / 健壮设计

- **并发状态机防护是范本级**：锁内 CAS、终态 UPDATE 守卫、stop_flag identity 比较、orphan recovery 区分可续传/不可恢复。
- **落盘原子性**：`.part` + `os.replace` 贯穿全线；缓存硬链接零拷贝；`GEOREF_SUFFIX` 防御跨版本残留。
- **路径安全教科书式**：`_resolve_safe_file` 先 `resolve()` 再双重 `relative_to` 校验，有回归测试；上传文件名不信任原名。
- **冻结/多进程边界处理**：`freeze_support()` 前置 + `parent_process()` 守卫 + 专项测试。
- **地理/渲染正确性**：`geo_validation` 统一三管线语义；等高线跨瓦片无色差四重设计；quantized-mesh 编码主体与规范逐点对上。
- **安全意识部分在线**：密码日志掩码、SQL 全参数化、无 pickle/eval/`shell=True`。
- **测试文化**：docstring 写明"防的是哪次真实回归"；取消语义并发测试精确断言行为。
- **前端细节**：`error_message` 全程 `textContent`、socket 重连全量重同步、失败 toast 按任务 key 去重。

## 最需要人工验证的"待确认"事项（Top 5）

1. **地形预览端到端实测**：C2 已字节级证实、C3 为代码结论——修复后用本地 vendor Cesium 实际加载一次 `layer.json` + 若干 `.terrain`。
2. **certifi 的 SSL 修复是否接线**：`build.spec:38-42` 声称修复冻结包 SSL，但全仓无代码引用 certifi——冻结 exe 上实测一次 HTTPS 下载。
3. ~~产品语义裁决~~（已裁决，见顶部"用户裁决记录"）。
4. **Linux fork 下等高线并行渲染的死锁风险**（`contour_engine.py:640-643`）：实测一次并行渲染 + 预览并发；或显式改 spawn 上下文。
5. **工作区基线固化 + backlog 裁决**：Cesium 改造几乎全部未提交、`static/vendor/cesium` 未跟踪；`docs/backlog-post-0.1.0.md` 约 1/3 条目基于已删除的 Leaflet。
