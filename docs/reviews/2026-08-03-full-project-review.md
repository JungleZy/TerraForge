# 全项目代码审查报告（2026-08-03，11 维度 + 对抗性验证）

**结论：整体健康，45 个真问题，其中 4 个 high、没有 critical。** 没有一条会导致用户数据永久丢失或安全越界；最严重的两类是「静默产出错误的地理数据」（等高线坐标算错）和「静默产出不完整的产物却报成功」（缓存写盘失败仍计 completed）。

> 原稿写 46 个；二次核对推翻 U9（见下节），故为 45。

## 审查方式与可信度

- 11 个只读审查员分维度通读（并发与状态机 / 下载引擎与网络 / 数据库与数据完整性 / REST 路由 / 静态服务与文件系统安全 / 地理数据与渲染 / 前端 JS / CSS 与 UI 契约 / 打包启动运行时 / 测试套件质量 / 文档与代码一致性）。
- 每条 medium 及以上发现交给一名**独立怀疑者**做对抗性验证：默认判定为误报，逐条尝试从「上游已兜底 / 有锁或调用方保证 / 场景不可达 / 已有测试证明现状正确 / 已声明为已知取舍 / 报告者读错语义」六个方向推翻，推不翻才保留，并允许下调级别与改写描述。
- **验证结果：45 条送验、0 条被整条推翻、22 条被下调级别、0 条被上调**（另有 1 条 critical→high）。结论：事实认定可靠，但审查员系统性高估严重度，本报告一律以验证后的级别为准。
- 多条结论有实机复现（等高线坐标偏移量实测、reset 配置后建任务 400 实测、pytest 互删 /tmp 工作目录实测、emit 抛异常把 completed 改判 failed 实测、GDAL MEM 晕渲内存增量实测），非纯静态推断。

## 二次核对（2026-08-03 晚，修复动工前）

修复动工前又派 6 名独立核对员把全部 52 条逐条对回代码（含未送验的 14 条 low），核对位置、因果链、改法可行性与「是否已被修掉」。结果：

- **51 条属实，1 条推翻**：`U9`（转圈图标的 `@keyframes spin` 缺失）是**误报** —— 该 keyframes 就在 `static/js/map.js:1077`，由同文件 `:1085` 在脚本求值时注入 `<style>`，动画正常工作。审查员只 grep 了 `static/css/` 与 vendor，漏了 JS 运行时注入的样式。已移入误报清单。
- **2 条已被上一提交修掉**：`M24`、`M25`（提交 `1cd918598`，commit body 逐条复述了这两条发现）；`M26` 修了 README 与 `docs/notes/geolibre-takeaways.md`，**漏了 CLAUDE.md:12 的 `water mask`**。
- **2 处改法会误导修复，已在条目内订正**：`M17` 建议的亮色令牌 `#9ca3af` 对白底仅 **2.54:1**，低于它自己要求的 3:1；`M12` 建议的「仅在 parent_url 为空时启用 z≤4」在当前代码里恒不成立，照做会把「低层被遮蔽」恶化成「地形完全不加载」。
- 其余为行号漂移与数字订正（约 26 处），已就地改入各条目，均不影响结论。
- 核对期间复跑全量 `uv run pytest tests/` = **900 passed**，印证这批发现全部落在测试盲区，不是过期条目。

**沿用 2026-07-31 的部署前提**：本程序运行在可信环境，零鉴权 / 绑 0.0.0.0 / CORS `*` / 凭据明文 / `verify_tile_url` 的 SSRF 探测面均为已接受的设计决策，本轮不再重复列出。

## 问题计数

| 级别 | 数量 | 说明 |
| --- | --- | --- |
| critical | 0 | — |
| high | 4 | 静默数据错误 / 跨进程破坏 / 测试自身具破坏性 |
| medium | 26 | 特定条件下出错，多数用户可见、可恢复 |
| low（已验证） | 8 | 触发面窄或纯展示层 |
| low（未经对抗性验证） | 13 | 原 14 条，二次核对推翻 U9 |
| **合计** | **51** | 去重后 45 个独立根因（6 条为多维度重复报告合并） |

---

# HIGH（会静默产出错误结果，或造成跨进程破坏）

## H1. 等高线读窗口降采样后坐标网格算错，低 zoom 瓦片整片丢线

**位置**：`services/contour_engine.py:524-525`

**问题**：
```python
xs = ctx.originX + (col0 + np.arange(arr.shape[1]) + 0.5) * eff_px_w
```
把窗口偏移 `col0` 也乘上了**重采样后**的等效像元 `eff_px_w`。正确映射是 `originX + col0*pxW + (i+0.5)*eff_px_w` —— 偏移量必须用原始像元 `pxW`，只有窗口内部的步进才用 `eff_px_w`。同函数第 435 行的 `arr_extent`（imshow 图层）用的正是 `originX + col0*ctx.pxW`，两处口径自相矛盾即证明 524/525 是错的。

误差 = `col0*pxW*(win_x-258)/258`，随瓦片在栅格中越靠东/南线性增大；只有 `col0/row0` 被钳到 0（瓦片压在栅格西/北边缘）或窗口 ≤258 源像素（不降采样）时为 0。这是 `_read_band_window` 那次 OOM 修复引入的回归：`git show 38e3e30fc4^` 显示原式用的是 `ctx.pxW`，修复时整体替换成 `eff_px_w` 而漏了拆分偏移项。

**触发场景**：1°/30m DEM（warp 到 3857 后 `pxW≈30.9 m`、`nx≈3601`）、默认 `zoom_min=10` 时，z10/z11 基本整片丢线 —— 按上式算最大偏移达 280 km / 130 km 量级，而瓦片宽仅 39.1 km / 19.6 km，等高线全部偏出瓦片外；z12 从数百米错位渐变到整片丢失，z≥13 因窗口 ≤258 正常。（原稿此处写「73.6 km / 2.5 km」，二次核对判定不可复现且自相矛盾 —— 2.5 km 反而小于它对比的 19.6 km 瓦片宽，那只是 13% 错位而非整片丢线；已按 `pxW≈30.9 m` 重算。）多度区域丢线的 zoom 上界更高。`ax.set_xlim` 是瓦片自身范围，偏出去的等高线全部被裁掉，PNG 里只剩分层设色/晕渲，`drew` 仍为 True，照常落盘、计 rendered、任务标 completed，零告警。

**改法**：
```python
xs = ctx.originX + col0 * ctx.pxW + (np.arange(arr.shape[1]) + 0.5) * eff_px_w
ys = ctx.originY + row0 * ctx.pxH + (np.arange(arr.shape[0]) + 0.5) * eff_px_h
```
补一个 `col0>0 且 win>_MAX_READ_DIM` 的用例，断言 xs 落在 [xmin,xmax] 内。现有 `tests/test_fix_contour_resample.py` 只钉了 `ReadAsArray` 的 buf 参数，`tests/test_contour_engine_render.py` 的源 DEM 只有 60×60，降采样分支根本不触发。

---

## H2. 缓存写盘失败仍上报 completed，任务静默缺瓦片且计数虚高

**位置**：`services/download_engine.py:667-696`（异常吞在 682，无条件上报在 689-690）

**问题**：下载成功后写 cache 的整段被 `except Exception as cache_write_error` 包住，只打一条 warning，然后**无条件**调 `progress_callback(tile,'completed',None)` 并返回 `status='completed'`。于是这块瓦片：磁盘上不存在任何文件、`task_tiles` 里没有 failed 行、`tasks.downloaded_tiles` 却 +1。完成判定只看 `SELECT COUNT(*) FROM task_tiles WHERE status='failed'`（`task_manager.py:1462-1468`），failed=0 → 任务标 completed。而 `start_task`（`task_manager.py:417-421`）只允许 pending/paused/failed 重启，completed 任务无法原地续传自愈。

从下载到收尾一共有**三次**能发现「这块瓦片不存在」的机会，三次全部只写日志：边下边复制的 `shutil.copy2` FileNotFoundError（`task_manager.py:152` → 979-982 吞成 warning）、收尾对账的 `cache_path.exists()` 为假（`task_manager.py:1404/1428`）、`copied_count` 与 `total_to_copy` 的差值（1447，只进日志不参与状态判定）。

**触发场景**：cache 盘 ENOSPC、cache 目录只读/权限变更、Windows 上 `part_path.replace()` 因目标被杀软/预览进程占用抛 PermissionError。0.2.4 删掉了 LRU 自动淘汰，缓存只增不减，磁盘写满是这个版本设计上必然到达的状态。`tiles_only` 任务完全无声；`both`/`image_only` 任务在拼接时报「Tile not found in cache」，指向的原因完全是错的。

**改法**：`cache_enabled=True` 下 cache 文件就是完成态的唯一真相（`_execute_task` 枚举段注释已这么写，且 `task_manager.py:833-845` 对 `cache_enabled=false` 已做过配置级硬失败）。把 682 行的 except 改成上报 `progress_callback(tile,'failed', f'cache write failed: {e}')` 并 `return {'status':'failed', ...}`，让稀疏失败表登记该瓦片、完成判定判 failed、用户可重试续传。

---

## H3. 启动清扫不做存活校验，第二个实例（含 pytest）会删掉运行中实例的工作目录

**位置**：`services/task_cleanup.py:132-147`（`_sweep_tmp_dirs` 纯前缀 rmtree）、`:150-176`（`.part` unlink）、`:186-194`（用真实 `tempfile.gettempdir()`）、调用点 `app.py:205`
**受影响的创建点**：`services/download_engine.py:931/936`（`map_dl_stitch_*`）、`services/contour_engine.py:649`（`contour_warp_*`）
**测试侧触发面**：`tests/conftest.py:81` 及 27 个各自 `_load_app` 的测试文件（放宽到「任何方式导入 app」也只有 31 个；全项目只有 `tests/test_startup_residue_sweep.py:85` patch 了 `gettempdir`）

**问题**（三个维度各报一次，同一根因）：`sweep_startup_residue()` 按**纯文件名前缀**删除，没有任何 PID 归属、mtime 年龄门槛或锁文件判据，分不清「上次进程的残留」和「另一个活着的进程正在写的工作目录」。它在 `create_app()` 里被无条件调用，而 `create_app()` 在模块导入期就执行，**远早于 `socketio.run()` 绑定 5000 端口** —— 即使第二个实例最终因端口占用崩溃，破坏也已经完成。全项目没有任何跨进程互斥（grep 无 pid 文件 / mutex / flock / 启动前端口探测）。

**触发场景**：
- **生产**：用户开着 TerraForge 跑大区域拼接（GB 级中间产物在 `/tmp/map_dl_stitch_*`，窗口数分钟到数十分钟），窗口被最小化以为没启动，再双击一次 exe → 第一个实例的 work_dir 被 rmtree → GDAL 读不到中间件 → 该 zoom 拼接失败，全部 zoom 失败时任务判 failed。等高线 warp 同理（产物可达数十 GB）。
- **开发/CI**：任何导入 app 的测试都会执行这段。实测：手建 `/tmp/map_dl_stitch_PROOF`、`/tmp/contour_warp_PROOF` 后跑只有 4 个用例的 `tests/test_tiles_static.py`，两个目录全部消失。并发跑两份测试会互删，已复现真实 flaky（`ERROR 4: Attempt to create new tiff file /tmp/map_dl_stitch_xxx/... failed: No such file or directory`，12 次里只有 2 次 passed）。

**损失界定**（两处夸大已修正）：被删的是**可重新派生的中间产物** —— 瓦片仍在共享 cache 并已镜像到输出目录，DEM granule 仍在任务目录里，重跑只需重做拼接/warp，不需要重新下载。`.part` 被删也**不会**让瓦片记失败（写缓存异常被 H2 那段 except 吞掉）。

**改法**：两层都要做。
1. 加进程级互斥：启动时先取 `Config.BASE_DIR` 下的锁文件（flock/LockFileEx），或先试探性 bind 5000，拿不到就跳过清扫与孤儿恢复并直接退出。这同时修掉「第二实例的孤儿恢复把第一实例正在 running 的任务改判 paused/failed」这个 `app.py:148-158` 自己已经承认的危害。
2. 清扫加存活门槛：三类清扫对象里只有 `.part` **文件名**带 `os.getpid()`（`download_engine.py:671`、`dem_download_engine.py:84` —— 形如 `{name}.part.{pid}.{id}`），这一类可直接用 `core/process_watchdog.py` 已有的 `pid_alive` 跳过活进程；两类 mkdtemp **目录**（`map_dl_stitch_*` / `contour_warp_*`）名里不含 pid，只能退而求其次按 mtime 早于本进程启动时间过滤。（原稿把 pid 说成在目录名里，实为 `.part` 文件名。）
3. 测试侧：conftest 加 autouse fixture 把 `services.task_cleanup.tempfile.gettempdir` 指向 `tmp_path`（或把 `sweep_startup_residue` patch 成 no-op），只保留 `test_startup_residue_sweep.py` 里那份显式的真实调用。

---

## H4. 守卫回归时，这条测试会真的 rmtree 掉开发者/CI 的家目录

**位置**：`tests/test_delete_files_cleanup.py:322`

**问题**：断言直接把真实 `Path.home()` 喂给 `remove_task_dir_if_safe` —— 一个内部会 `shutil.rmtree` 的函数。它「验证」的方式是：守卫还在就返回 False；守卫被改坏就一路走到 `services/task_cleanup.py:124` 把家目录删了，**然后才**因为返回 True 让断言变红。捕获回归的手段是先造成灾难。

家目录只有 `task_cleanup.py:112` 这**一道**守卫兜底：实测在 `DOWNLOADS_DIR=tmp_path/downloads` 下逐条判定，symlink=False、`parts<3`=False、downloads 祖先=False、cache=False，只有 home 那条为 True。实测把 `:112` 改成 `if False:`（一次很自然的守卫重排/条件合并就能造成），在 `HOME=/tmp/fakehome` 下跑本文件，用例确实红了，同时 `/tmp/fakehome` 被整个删除。

**触发场景**：任何人修改守卫链（合并条件、调整顺序、把 `Path.home()` 换成配置项）后按项目规约跑 `uv run pytest tests/`。CI 上更直接：GitHub Actions 三平台的 checkout（`/home/runner/work/...`、`/Users/runner/work/...`、`C:\Users\runneradmin\...`）全部位于 HOME 之内，守卫一坏就把自己的工作区连同后续步骤一起端掉。

**改法**：`monkeypatch.setenv("HOME", str(tmp_path/"home"))` 并同步 setenv `USERPROFILE`（Windows 上 `Path.home()` 读它），再断言 `is False`；配 `monkeypatch` 把 `shutil.rmtree` 换成 spy，断言未被调用。已实证 `Path.home()` 跟随 HOME 环境变量，断言效力不减、风险归零。

**注**：同函数体内相邻的 `:321`（根目录 `/`）是**安全**的 —— 实测同时废掉 shallow 与 home 两道守卫，`/` 仍被「DOWNLOADS_DIR 祖先」守卫拦下。只需修 322 一行。

---

# MEDIUM（特定条件下出错）

## M1. 兜底 except 会把已 completed 的任务改判 failed（map 与 dem 管线未同步 contour 的修复）

**位置**：`services/task_manager.py:1557`、`services/dem_task_manager.py:708`
**对照组**：`services/contour_task_manager.py:806-808` 已把 `'completed'` 加进排除列表，并有中文注释说明原因；`tests/test_contour_execute.py:392-419` 有专门的回归测试

**问题**：任务成功时先 `UPDATE ... status='completed'` 并 commit（`task_manager.py:1525-1531`），随后**仍在同一个 try 内**执行 `socketio.emit('task_completed')`（1543）。一旦它抛异常，控制流落到 1549 的兜底 except，执行 `UPDATE tasks SET status='failed', error_message=?, completed_at=? WHERE id=? AND status NOT IN ('cancelled','paused')` —— `'completed'` 不在排除列表里，已落库的终态记录被改写成 failed，`error_message` 变成无关的 socketio 异常文本，`completed_at` 被重写成异常时刻，1525 行写入的 stitch_warning 被覆盖，前端在收到 `task_completed` 之后紧接着收到 `task_failed`。违反 CLAUDE.md「终态记录绝不可被改写」的约定。

已实测复现（socketio 桩在 `task_completed` 抛 RuntimeError）：`STATUS = failed | ERR = client gone`，`EVENTS = ['task_copy_progress','task_completed','task_failed']`。

**触发场景**：窗口内唯一能抛的语句就是那一发 emit（`_update_total_running_time`/`_record_time_action` 自带 try 吞异常）。flask_socketio 的广播式 emit 在客户端断开时由 python-socketio 内部吞掉 KeyError、payload 又全是可序列化值，所以概率不高，主要剩服务端关停中、engineio 内部错误、自定义/桩 socketio。DEM 侧只有 emit 一个可抛点，触发面比 map 更窄。

**改法**：两处 WHERE 补 `'completed'`（与 contour 对齐），或干脆改成 `AND status='running'`（与 `local_terrain_task_manager.py:408` 对齐）；同时把收尾 emit 各自包一层 try 只记日志 —— 同文件 1437-1445 的 copy 进度 emit 已经这么做了，注释就在 1436：「emit 故障（客户端断开等）不应打断复制本身」。（原稿引的 1429 是 `shutil.copy2` 的 except，不是 emit 的。）

## M2. 拼接产物非原子写，半截 GeoTIFF 被断点续跑当成已完成

**位置**：`services/download_engine.py:1055`（写最终路径）、`:1060-1079`（finally 从不删残留）、判据在 `services/task_manager.py:1293`

**问题**：`gdal.Translate` 直接把最终 GeoTIFF 写到 `output_path_obj`，没走本文件其它写盘点都在用的「.part + os.replace」—— 瓦片 cache 落盘（671-676）和 `_add_georeference`（1370-1413）都做了原子写，后者的注释还白纸黑字解释了为什么必须这么做（`driver.Create()` 自己就会先在磁盘上放一个文件）。而断点判定是 `if output_path.exists() and output_path.stat().st_size > 0` 就跳过重拼并计入 `stitched_zooms`。GDAL 写 GTiff 边写边落盘，被杀时留下的必然是**非空**半成品，恰好满足这个判据。注释自称「这里无法区分」—— 但同文件已经用原子写解决过同一问题两次。

**触发场景**（第二条比断电常见得多）：
1. 拼接途中关 exe/断电 → 孤儿恢复翻 paused → 用户点继续 → 命中短路 → 该 zoom 记成功 → 任务 completed 无 warning。
2. **Translate 抛异常**（磁盘写满、目标盘掉线）→ finally 同样不删残留 → 任务判 failed → `task_manager.py:430` 明确允许 failed 重启 → 用户点一次「重试」就命中短路，warning 消失、任务转 completed。纯正常错误处理路径即可复现。

产物是一个**损坏状态不确定**的 tif（从「打不开」到「下半张空白」都可能）。

**改法**：Translate 写到同目录 `<name>.part.<pid>`，`output_ds = None` 关闭后再 `os.replace`，异常路径 finally 删 .part。这样 `task_manager.py:1293` 的「非空即完成」才真正可信，断点逻辑无需改动。对照 `task_manager.py:1416-1418` 的瓦片复制短路用的是「dest 存在**且大小与源一致**」—— 同一条产物链上两套严格度。

**落地 caveat**：驱动由 `TranslateOptions(format=...)`（1034-1052）显式指定，改扩展名不影响驱动选择；但 `output_ext` 允许 `.png`（1039-1044），PNG 驱动会另写 `.aux.xml` 边车，只 rename 主文件会留下 `<name>.part.<pid>.aux.xml`。地图管线固定写 `.tif`（`task_manager.py:1286`）故现网不踩，实现时仍需一并处理边车。

## M3. 下载回调里同步 copy2 阻塞事件循环，网络盘上并发数形同虚设

**位置**：`services/task_manager.py:1147`（async 回调里同步调 `_stream_copy_quiet`）→ `:131-154`（`_stream_copy_tile`）

**问题**：0.2.4 的「边下边复制」在 `progress_callback`（async，跑在下载的 asyncio 事件循环线程上）里同步执行 `mkdir` + `dest.exists()` + `dest.stat()` + `src.stat()` + `shutil.copy2` + `tmp.replace`，全是阻塞 syscall，没有 `asyncio.to_thread`。而 `_download_single_tile` 里连一次 `cache_path.stat()` 都特意挪出了事件循环（`download_engine.py:640/670/676`），下载引擎那一侧是认这个约定的。

**范围说明**（二次核对补充，避免把它说成孤例）：同一个回调里的 `flush_progress_counts()`（定义在 `task_manager.py:1030`、调用在 `:1151`）也在事件循环上做同步 sqlite `executemany`+commit，同样没包 `to_thread`（DEM 侧倒是包了，`dem_task_manager.py:629`）。copy 每块都跑、flush 每 N 块才跑，copy 仍是主要阻塞源，但改完 copy 后事件循环仍不干净。

**触发场景**：默认本地盘影响接近零（一块 10-30KB PNG 的 copy2 是几十微秒，吞吐上限万级/秒）。真正踩到的是 0.2.4「保存目录全盘可选」鼓励的用法：SMB/VPN 网络共享上每次 `exists`/`stat`/`copy2`/`replace` 各一次往返，累计 10-30ms/块 → 吞吐被钉在 30-100 块/秒，`concurrent_downloads` 调多少都没用（0.2.1 的「并发测速推荐」走的是不复制的另一条路径，测出的膝点因此失效）；同时 stop_flag 检查被推迟，暂停/取消响应变慢。网络盘上三次元数据 syscall 往往比数据拷贝本身更贵。

**改法**：`await asyncio.to_thread(_stream_copy_quiet, tile)`，或把即时复制交给一个小的写盘队列（复用已有的补拷线程模型），让事件循环只做网络 IO。

## M4. 下载路径对内网瓦片服务不 bypass 代理，与「验证」按钮口径相反

**位置**：`services/download_engine.py:558`（`proxy=proxy_url or None` 无条件套）、`:753`/`:800`（读配置并原样透传）
**对照组**：`services/tile_url_probe.py:120-135` `should_bypass_proxy`，被 188-189（验证）与 344-345（测速）使用

**问题**：`should_bypass_proxy` 明确规定目标是 localhost/环回/私网/link-local 时必须绕过 proxy_url，注释还专门写了 WSL 踩坑理由。但下载路径一次都没调用它（全仓 grep 该函数，`download_engine.py` 零命中）。aiohttp 的显式 `proxy=` kwarg 会完全覆盖 trust_env 那一套（包括系统 bypass 列表），连 NO_PROXY 都救不了。三条路径两套口径。

**触发场景**：用户配了自建瓦片服务 `http://192.168.1.10:8080/{z}/{x}/{y}.png`（模块 docstring 把这列为一等用法）同时为访问 Google 填了 proxy_url。点「验证」→ 绕过代理直连 → 绿灯；建任务下载 → 每块瓦片经代理转发到代理自己到不了的内网地址 → 全部失败。用户面对「验证明明通过了，下载全失败」，日志指不到代理这一层。注意 `should_bypass_proxy` 对**域名**一律返回 False，所以口径矛盾只在自建服务写成字面 IP/localhost 时出现。

**改法**：不能在 753/800 处按批清空（tile_servers 可混配公网+内网），必须在 `download_tile` 内拿到实际 url 之后逐 URL 判断，两处共用同一个函数避免再次分叉。

## M5. HTTP 200 的非图片响应被当成瓦片永久写进共享缓存

**位置**：`services/download_engine.py:560-561`（只 `raise_for_status()` 就 `await response.read()`）、写入在 `:667-686`、复命中判据在 `:638-642` 与 `services/task_manager.py:889-896`

**问题**：既不看 Content-Type 也不校验 PNG/JPEG 魔数（全仓 grep：下载链路零命中，连 `tile_url_probe.py:159-164` 的验证判定也只看 `status==200 and data 非空`）。这堆字节被原子写进 `cache/<style>/<z>/<x>/<y>.png`，之后两个判定点都只查 `size > 0`。0.2.4 起没有任何自动淘汰 → **永久**命中；缓存跨任务共享 → 污染扩散到之后所有覆盖同一区域同样式的任务。更狠的是 `task_manager.py:894-896` 会把这些瓦片收进「已完成」集合、`:927-935` 把它们的历史失败行 DELETE 掉，把上一轮记为 failed 的瓦片「洗白」成完成。

**触发场景**：默认 mts0-3 别名展开成**明文 http://**（`tile_url_probe.py:57`），透明代理/酒店 Portal/运营商劫持在明文链路上返 200+HTML 是教科书场景；自建服务对越界坐标返 200+JSON 同理。`tiles_only` 任务：产物目录里躺着一堆扩展名 .png 的 HTML，任务 completed。`both`/`image_only`：拼接失败**不是**静默的（会进 `stitch_failures` 并给出明细），但**不可自愈** —— 修好网络后重建任务，枚举全部命中被污染的缓存、零下载，拼接以完全相同的方式再次失败，除了到配置页手工清整个缓存分类外没有任何恢复途径。

**改法**：`download_tile` 返回前加一道廉价校验 —— **以魔数为准**：body 前几字节不是 PNG(`\x89PNG`)/JPEG(`\xff\xd8`)/WebP(`RIFF....WEBP`)/GIF 之一时，当作可重试的下载失败抛出（不写缓存）。探测接口已经把 content_type 回传给前端了，下载路径缺的就是同一道检查。

**注意别写成「Content-Type 非 image/\* 或魔数不符」的或逻辑**（原稿如此）：那会误杀返回 `application/octet-stream` 但内容合法的自建瓦片服务 —— 而自建服务恰是 M4 里被称作「一等用法」的场景，两条改法会互相打架。Content-Type 只可用于补充日志，判定以魔数为准。

## M6. 「恢复默认配置」写回相对保存路径，之后地图/DEM 建任务全部 400

**位置**：`core/database.py:43`（`('default_save_path','./downloads')`）、`services/config_manager.py:250-256`（`reset_to_defaults` DELETE 全表 + executemany 重插）、`routes/api.py:854`（reset 端点无后续归一）
**归一化只在**：`core/database.py:534-554`，仅 `init_database()` 内、仅进程启动时跑一次、且只处理相对值

**问题**（并发/数据库与 REST 两个维度各报一次）：`reset_to_defaults` 绕过了 `set/set_many` 的 `validate_config` 校验（135、173 行），也绕过了归一化，于是 config 表里被写入 `default_save_path='./downloads'` —— 一个 `validate_config` 自己会判 False 的非法值。`services/config_manager.py:158-160` 的 docstring 明写「callers never observe a half-updated configuration」、`tests/test_config_manager.py:100-111` 把「任一键非法整批拒绝」固化成契约，reset 这条路径把它绕开了。

已实测：init 后为 `/tmp/xxx/downloads` → `POST /api/config/reset` 200 → 值变回 `./downloads` → `POST /api/tasks`（`output_path='./downloads/map'`）返回 400「保存路径必须是绝对路径」。

**触发场景**：用户在配置页点「重置为默认」（带二次确认）→ 自动 reload → 首页表单被 `static/js/map.js:553-560` 预填成 `./downloads/map` 或 `./downloads/dem` → 提交被 `task_manager.py:303` / `dem_task_manager.py:104-106` 拒为 400。**只影响地图与 DEM 两条管线**：等高线（`contour_task_manager.py:324`）与本地地形（`local_terrain_task_manager.py:133`）的 output_path 硬编码为 `Config.DOWNLOADS_DIR` 子目录，不读该配置键。

**恢复成本低但不自明**：报错文案自解释（「可点输入框旁的『浏览』选择」），在任务表单或配置页填一个绝对路径即可一步恢复，重启也会由 `init_database` 静默修好。

**改法**：**首选**把归一化抽成函数，供 `init_database` 与 `reset_to_defaults` 共用。次选（有隐患，非必要不用）才是让 `DEFAULT_CONFIGS` 的 `default_save_path` 由 `str(Config.DOWNLOADS_DIR)` 动态生成 —— 那会把模块级常量绑死在导入时刻的 Config 状态，而本项目的测试套路正是「先 monkeypatch `Config.DOWNLOADS_DIR` 再重新导入 app」（CLAUDE.md 明写），只要 `core.database` 在更早的用例里被导入过，常量就固化成上一个 tmp_path。补一条「reset 后 `default_save_path` 必为绝对路径」的测试 —— `tests/test_path_browser.py` 目前只覆盖 PUT 和 init。顺带把 `config_manager.py:235` 的 docstring「re-inserts 18 default values」改对（AST 实测 **44** 项，原稿写 42 也不对）。

## M7. DEM 海洋颗粒 404 记 skipped 但不计数，完成的任务显示「已完成 · 4 / 10 文件」且进度条卡 40%

**位置**：`services/dem_task_manager.py:33-36`（`_status_count_deltas` 对 skipped 两个增量都是 0）、`:678`（收尾判定把 skipped 算终结）、`:635`（进度 `done` 同样漏 skipped）
**复制品**：`services/contour_task_manager.py:157`

**问题**：`dem_download_engine.py:202-209` 对 404 颗粒（海洋/覆盖外，Copernicus GLO-30 对海面本来就没瓦片）上报 `skipped`，这是有意的部分成功语义。但计数增量函数只认 completed/failed（实测 `('downloading','skipped') -> (0,0)`，且 `downloaded_delta or failed_delta` 为假时连 UPDATE 都不发），而收尾判定把 skipped 算作已终结、任务照常 completed。结果终态下 `downloaded_files + failed_files < total_files` 这个不变量被破坏，且恢复路径只把 `pending/failed` 重新入队，skipped 永不回补，全文件没有任何按 `dem_files` 重算 `downloaded_files` 的地方。

**触发场景**：任何带海岸线的选区。10 个 1° 颗粒里 6 个在海上 → 记录面板行渲染 `已完成 · 4 / 10 文件`（`static/js/history.js:281`），详情弹窗给一个**已完成任务显示 40% 的进度条**（`history.js:595-603`），下载过程中进度条同样封顶 40%，「预计剩余」偏大。磁盘产物、任务终态、后续切片（按磁盘实存 tif 走）都正确 —— 纯计数/展示口径问题。

**改法**：给 `dem_tasks` 增加 `skipped_files` 列并把前端改成「N 下载 + K 跳过 / M」；或最小改动 —— 给 `_status_count_deltas` 增加 skipped 增量并在收尾把 `downloaded_files` 重写成 `completed+skipped`，让终态不变量恒成立。contour 侧同口径但用户可见面小（记录面板给等高线渲染的是 `rendered_tiles`），且下载路径已属遗留。

## M8. 清理缓存不检查运行中的任务，产物目录静默缺瓦片且任务仍报完成

**位置**：`routes/api.py:882-924`（clear_cache_api 不查任何 manager 的 active_tasks、不加锁、不延后）→ `services/task_cleanup.py:336-337`（`shutil.rmtree`）

**问题**：删除任务路径是会查运行中任务的 —— 地图侧在 `routes/api.py:369` 锁内查 `active_tasks`，DEM/等高线侧的守卫在各自 manager 的 `delete_task` 里（见 `routes/dem_api.py:88-92`、`routes/contour_api.py:161-165` 的注释）。「检查运行中任务」在本项目是既有惯例，缓存清理漏了。（原稿把 `routes/api.py:132-138` 列为删除路径，实为 `GET /api/tasks?status=active` 的列表分支。）地图任务在枚举阶段就把 cache 命中的瓦片移出待下载列表（`task_manager.py:885-897`）并计入 `downloaded_tiles`（`:944-955`），产物目录靠补拷线程/收尾复制从 cache 复制。清掉分类目录后这些瓦片既不重下，复制失败也只吞成 warning（`:978-982`、`:1427-1430`），完成判定只看 `task_tiles` 的失败行 —— 而 cache 命中瓦片从不在那张表里（`:927-939` 还会主动清掉它们的历史失败行）。前端两次确认弹窗（`static/js/config.js:85-93`）只说「不可恢复」，不提运行中的任务。

（正在下载中的瓦片无害：引擎每块都 `mkdir(parents=True, exist_ok=True)`，会自愈。）

**触发场景按 output_format 分叉**：
- `tiles_only`（不走拼接）：**完全无声** —— 任务 completed、`downloaded_tiles` 满值、产物目录静默缺瓦片、`/tiles/<id>/` 预览大片空洞。缓存在拼接完成之后、收尾复制途中被清时，其他格式也落到这一形态。
- 默认 `both` / `image_only`：受影响 zoom 拼接失败（`download_engine.py:970-972` 抛 FileNotFoundError），任务标 failed 或 completed 带「部分缩放级别拼接失败」警告 —— 有可见信号，但信号指向拼接、不说明瓦片缺失。

**改法**：清理前查 DB 里 `status IN ('pending','running','paused')`（**首选**，低成本可行 —— `clear_cache_api` 只能拿到 `api.py` 自己的 `task_manager` 全局，要查四个 manager 得额外注入），有活动任务返回 409 并说明「请先暂停或取消任务 #N」；要允许强清就加显式 `force=1` 并回传受影响任务。另建议把补拷/收尾复制里「源文件不存在」从 warning 升级为写入 `task_tiles` 失败行。

**残余竞态需明说**：检查后再清仍是 check-then-act —— 清理途中仍可能有任务被 start。要彻底安全需拿 manager 锁；否则应在文档里明确接受这段残余窗口。

**注**：`docs/notes/cache-exclusive-cleanup-plan.md`（已跟踪，随 `a439aa6e2` 的 docs 重构移入 notes/）承认了粒度太粗，但把最坏后果断言成「cache miss 后重下，不产生数据丢失」—— 这个心智模型对运行中的地图任务恰恰不成立（枚举快照早于清理，不会重下）。

## M9. PUT /api/config 部分键非法时先写库再返回 400，报「保存失败」却已生效一半

**位置**：`routes/api.py:800-828`（逐键校验 → 对合法键调 set_many 落库 → errors 非空返回 400 + `success:false`）、前端 `static/js/config.js:273-292`（每次保存提交全部 18 个键）、`:305-311`（非 2xx 一律弹「保存失败」，不读 `result.updated`）

**问题**：路由通过「先把非法键过滤掉再调 set_many」的写法，让 `config_manager.set_many` 自己的「任一键非法整批拒绝」保护在这条路径上永远不触发 —— 等于在路由层反转了管理层声明并测试过的语义。实测：PUT `{concurrent_downloads:80, max_retries:7, tile_servers:<缺 {y}>}` → `STATUS=400`，`BODY={'success':False,'updated':['concurrent_downloads','max_retries'],'errors':[...]}`，而库里 `concurrent_downloads` 已从 50 变成 80。

**触发场景**：因为前端每次都提交全部 18 个键，**任何单个字段填错都会让另外 17 个键在用户被告知失败的同时静默生效**。用户以为并发数没改，下一次下载却按 80 跑；与 M6 叠加时，配置页会变成「怎么保存都提示失败，但设置在偷偷变」。

**改法**：二选一并写进 docstring —— (a) 严格全或无：有任何非法键就直接 400 且不调 set_many；(b) 部分成功：写库后返回 200/207 + `success:true` + `updated`/`errors`，前端按「部分保存」提示并刷新表单。当前是两者最差的组合。

## M10. 删除产物这条链上有四套路径解析口径，且拒删/删错一律谎报 success

**位置**：
- 口径 A（写侧/地图删除侧）：`services/task_cleanup.py:50-63` `resolve_stored_output_dir` —— 相对值一律拼到 `Config.DOWNLOADS_DIR` 下
- 口径 B（读侧）：`routes/terrain_static.py:63-87` `_resolve_config_path`（被 `routes/tiles_static.py:62` 复用）—— 对 `./downloads/...` 做前缀剥离
- 口径 C（DEM/contour 删除侧）：`routes/dem_api.py:101`、`routes/contour_api.py:172` —— 裸 `Path(task["output_path"])`，按**进程 CWD** 解析
- 口径 D（local terrain 删除侧，原稿漏列）：`services/local_terrain_task_manager.py:492-504` —— 完全**忽略**库里的 output_path，按当前 `Config.DOWNLOADS_DIR` 重算路径，自带内联守卫 + `shutil.rmtree(ignore_errors=True)`。它才是真正「删失败也静默、路由（`routes/local_terrain_api.py:122-127`）仍返回 200 success」的那一个
- 返回值丢弃：`routes/api.py:406`、`routes/dem_api.py:101`、`routes/contour_api.py:172`

> 原稿把 `services/local_terrain_task_manager.py:220/236` 也列进「返回值丢弃」，那是错的 —— 这两处是 **create_task 的失败回滚清理**，其后紧跟 `raise`（`:221`/`:238`），客户端拿到 400/500，不存在假成功。

**问题**：同一个存量字段三套解析规则。`core/database.py:527-533` 的注释已经认定历史语义是口径 B（「'./downloads' 就是 DOWNLOADS_DIR 本身……按 resolve_output_dir 归一会把它错置成 downloads/downloads」）并据此归一了 `default_save_path` 配置键 —— 但**任务行从来没被同样归一过**。实测每一种相对形态两侧都分叉：`'./downloads/map'` → 写/删侧 `<BASE>/downloads/downloads/map`，读侧 `<BASE>/downloads/map`。

同时 `remove_task_dir_if_safe` 用 bool 区分「已删」与「越界拒删」，四个调用点全部丢弃返回值，任何护栏命中都只写一条 warning，HTTP 响应仍是 200 `{"success": true}`；`task_cleanup.py:123-126` 目标不存在时也直接 `return True`。

**触发场景**：受影响的是 commit `38e3e30fc`（2026-07-29，约 v0.0.9 及更早的多个真实发布版本）之前建的任务行，那时表单默认值硬编码 `./downloads/map` 且原样入库。
1. 点「删除并删文件」→ 删的是 `<BASE>/downloads/downloads/task_7`（不存在）→ 接口 200 success，几十 GB 瓦片纹丝不动。
2. 恢复该任务继续下载 → 新瓦片镜像到 `downloads/downloads/task_7/`，而 `/tiles/7/...` 去 `downloads/task_7/` 找 → 新下的全部 404，产物分裂成两处。

**改法**：收敛成一套 —— 让 `resolve_stored_output_dir` 采用 `core/database.py:529-532` 已认定的历史语义（等价于前缀剥离），`tiles_static`/`terrain_static` 改调它并删掉 `_resolve_config_path`；`dem_api`/`contour_api` 也改调它；在 `init_database()` 里对四张任务表的相对 output_path 做一次性幂等归一，此后下游只用绝对值。同时三个删除端点接住返回值，响应带 `files_removed:false` 与原因，前端区分「已清理」与「未通过安全校验、未删除」。**口径 D（local terrain）也要一并接住失败并上报**，它现在连失败都吞。

收敛目标自洽性已验证：`core/config.py:64` 是 `DOWNLOADS_DIR = BASE_DIR / 'downloads'`，所以口径 B（前缀剥离 + 其余相对值落 BASE_DIR）与 `core/database.py:543` 的 `_root.parent / _p` 在所有相对形态上等价，可以安全统一。

## M11. terrain 切片丢弃 build_terrain 的 failed 计数，缺瓦片的作业照样报 completed 且 layer.json 过度声明

**位置**：`services/terrain_tiling/dem_task_tiler.py:56`（调用后丢弃返回值，签名 `-> None`）、消费方 `services/dem_task_manager.py:399-418` 与 `services/local_terrain_task_manager.py:366-393` 均无条件置 completed
**对照组**：`services/contour_task_manager.py:757-795` 接了 `render_counts`、`rendered==0` 判 failed（`:778`）、`failed>0` 记 warning（`:790-795`）

**问题**：`cesium_terrain.py:586` 返回 `{"total","rendered","failed"}`，注释自己写着「调用方(dem_task_tiler/local_terrain_task_manager)此前忽略返回值,保持兼容」。上一轮给 terrain 补的逐瓦片容错（`:399-401` 异常只记 warning 返回 None）因此变成纯静默；进度 `done = rendered+failed`（`:494`）必然走到 100%。而 `available` 数组是按 `_tile_ranges` 的完整矩形算出来的（`:463`），与实际产出对不上，Cesium 按 availability 请求后拿到 404（parentUrl 也不兜底，因为子层声明了可用）。极端情况：所有瓦片都失败时 `terrain_tiles/` 一片没有，layer.json 照写、`dem_task_tiler.py:68` 的存在性检查照过、job 照标 completed。

数据库和前端完全无感知（job 行只有 `rendered_tiles`/`total_tiles`，且 `rendered_tiles` 实存的是 `rendered+failed`），只有散落的逐瓦片 `logger.warning`，没有任何聚合失败计数。

**触发场景**：切片途中磁盘写满，或某个 granule 是被截断的坏 tif。

**改法**：`tile_dem_task_dir` 返回 counts dict；两个 `_run_tiling_job` 参照 contour 收尾：`rendered==0` 判 failed、`failed>0` 记 warning 并写 `error_message`。进一步可按实际落盘瓦片重算 available。

**两个落地前提**（二次核对补充）：
1. **schema 现状**：`dem_terrain_jobs` 没有 `failed_tiles` 列（`core/database.py:336-352`、`:509-510`），而 `local_terrain_tasks`（`core/database.py:359-376`）**连 total/rendered/failed 三个瓦片计数列一个都没有**，只有 files 相关列。好在两张表都有 `error_message`，所以「rendered==0 判 failed + 写 error_message」这个最小修法**不需要动 schema**，只有想持久化 failed 计数才需要 ALTER。
2. **改返回类型会打断 6 处测试替身**：`tests/test_fix_dem_start_tiling_race.py:60`、`tests/test_fix_infra_e.py:235/275/391`、`tests/test_local_terrain_api.py:99/605` 都用返回 `None` 的 fake monkeypatch 掉 `tile_dem_task_dir`，管理器一旦 `counts.get(...)` 就会 AttributeError 把任务打成 failed。`dem_task_tiler.py:26-28` 的注释正好警告过这些契约测试钉住了调用形态 —— 必须连带更新。

## M12. 低 zoom 地形瓦片虚报 available，把 parentUrl 基础地形整个盖掉

**位置**：`services/terrain_tiling/cesium_terrain.py:449-450`（z≤4 无条件全球出图）、`:463`（整个索引矩形无差别写进 available）、`:206-211`（DemSampler 对 DEM 外采样点钳到边界像素）、`:163-164`（完全不相交时返回全 0）

**问题**（两条发现同一失效模式，合并）：
1. `_tile_ranges` 在 z≤4 时把范围强制改成全球，与 DEM bbox 的交集完全不参与；`dem_task_tiler.py` 又固定 `min_level=0`，于是每个任务固定生成 2+8+32+128+512=682 片瓦片。DEM 外的采样窗口为空 → 返回全 0 → 由 `encode_quantized_mesh:289-290` 兜成 hmin=0/hmax=1 的平面。
2. 部分相交的瓦片走另一条路：`np.clip(np.floor(lpx).astype(int), 0, arr.shape[1]-2)` 把越界采样点钳进读窗口，拿到 DEM 最外一行/列的高程；`fx/fy` 也被 clip 到 [0,1]，所以是「边缘剖面沿法向无限拉伸」（幅度有界，不是线性外插）。z0 那片瓦片覆盖半个地球，顶点几乎全部越界 → 一个 4000 m 的青藏 DEM 会让整个东半球在 z0 视角下变成几块高原台地。**实测**解本机产物 `0/1/0.terrain` 头部：hmin=913 m、hmax=2502 m —— 被外拉的是 DEM **边缘剖面**的高程而非内部峰值，所以幅度约 0.9–2.5 km（原稿写「4 km」高估约一倍，且与它自己上一句「幅度有界，不是线性外插」矛盾）。

两类瓦片**都被写进 available**，而 Cesium 1.143 的 `requestTileGeometry` 取「第一个 availability 声明可用的层」、父层是在子层之后才追加的 —— base_z8 的 z0-4 永远不会被请求。

**实测产物佐证**：`downloads/dem/dem_task_1/terrain_tiles/layer.json` 的 valid_bounds 是 [85,41,88,43]，但 available[0..4] 分别是全球 2×1 / 4×2 / 8×4 / 16×8 / 32×16，available[5] 才收缩成 [47,23,47,23]；解 `0/0/0.terrain` 头部得 hmin=0.00 hmax=1.00。

**触发条件收窄**：只在任务的 parentUrl 指向的全局 base（`downloads/terrain/base_z8/`）**真实存在**时才显形 —— 该 base 是用户手工离线构建的可选产物（`docs/terrain/global-base-build.md` 只有说明没有脚本）。未构建 / parent_url 为 null 时是单层 provider，不存在遮蔽。附带代价始终存在：682 片无用瓦片的时间与磁盘，以及 `h_min_global` 被 0 值污染 —— meta.json 的 minHeight 被 0 钳住、不反映真实最小高程（严格说是 `min(0, DEM 实际最小值)`，遇到死海/吐鲁番这类负高程 DEM 会是负数；本机实测为 0.0）。

> **勘误（2026-08-03 文档重构时发现）**：上一段括注里的「`docs/terrain/global-base-build.md` 只有说明没有脚本」**不确** —— `scripts/build_global_base_terrain.ps1` 一直存在（748 字节，2026-05-18 起入库），只是从未被任何文档引用过，所以本次审查没看见它。该脚本另有缺陷：漏传 `--tile-size`，走 CLI 默认 17 而应用侧单任务用 65。文档现位于 `docs/reference/terrain/global-base-build.md`，已补完整构建流程与该差异说明。本条其余结论（z0–4 遮蔽机理、实测佐证、改法）经复核仍然成立。

**改法**（原稿的兜底条件有严重问题，已重排优先级）：

1. **首选、风险最低**：`available` 只声明与 DEM 真正相交且有有效数据的瓦片。这一条单独就能消除遮蔽 —— 子层不声明可用，Cesium 自然回落到 parentUrl 的 base。
2. `DemSampler.sample` 对超出 `[0, cols)/[0, rows)` 的 px/py 生成掩码置 NaN 而非钳位。注意置 NaN 后仍会走 `:219` 的 `np.where(isnan, 0.0, v)` 落成 0，效果是「DEM 外恒为 0」而非「不出瓦片」，**必须配合第 1 条才完整**。
3. 去掉 z≤4 全球分支这条 **不要按原稿写的「仅在 parent_url 为空时启用」实现**：`parent_url` 实际恒非空（`dem_task_manager.py:298`、`local_terrain_task_manager.py:33-40` 都是 `config.get(...) or <硬编码 base URL>`，`layer_json.py:7` 又无条件写进 layer.json），该条件形同虚设，等于无条件删掉 z≤4。而 base_z8 **不存在时**（多数用户的状态）删掉 z≤4 会把现状从「低层被遮蔽」恶化成「地形完全不加载」：parent layer.json 404 → provider 退回单层 → Cesium 单层路径根本不查 availability（vendored `Cesium.js` 里 `if(s===1)r=o[0]`）→ 直接请求 `0/0/0.terrain` → 404 → 根瓦片 FAILED、无父可上采样。真要做，条件必须是「parentUrl 指向的 base 真实可解析」（切片时探测其 layer.json），不是「配置为空」。

**佐证产物的一处说明**：上面引的实测 `layer.json`/`meta.json` 出自 `tileSize: 17` 的产物，即 CLI/旧跑法而非应用路径（应用单任务固定 65）。`available` 数组与 tile_size 无关，结论不受影响。

## M13. 晕渲预览在 Flask 请求线程里按原分辨率做 MEM 晕渲

**位置**：`services/hillshade_preview.py:73`（`gdal.DEMProcessing("", vrt_path, "hillshade", format="MEM")` 无尺寸参数）、限宽 `_MAX_WIDTH=1600` 只在 `:81` 的 Translate 阶段生效

**问题**：先全尺寸算完再缩小。实测（GDAL 3.8.4，项目 .venv）：12000×12000 的 VRT → RSS +143MB / 5.41s；线性外推 36000×36000（10°×10° ASTER）≈ 1.24GB / 约 50s，而产物最终只是 1600px 宽的 PNG。上游没有面积/颗粒数上限（`geo_validation.validate_bbox` 只查取值范围），路由 `routes/terrain_static.py:255-271` 在请求线程里同步调用，无超时、无尺寸预检、不校验任务状态。

**更容易触发的一条**（原报告漏了）：BuildVRT 取的是输入**并集**范围 —— 实测两个相距 10° 的 30×30 小 tif 合出 1030×1030 的 VRT。所以任务跨度 10° 但只成功下了 2 个颗粒（部分失败/下载中）时，照样按满幅分配 1.2GB，绝大部分是 nodata。

**失败形态修正**：不是 Python MemoryError（MEM 驱动走 C 层 VSIMalloc，失败时 DEMProcessing 返回 None → RuntimeError → HTTP 500）；Linux overcommit 下更常见的是分配成功、写满时被 OOM killer 杀掉整个进程。也**不会**整站卡死 —— 项目是 threading 模式（无 eventlet/gevent），实测 GDAL 释放 GIL，只占住该请求线程数十秒。

**改法**：先 `gdal.Translate('', vrt_path, format='VRT', width=min(RasterXSize,_MAX_WIDTH))` 拿缩略 VRT，再对它 DEMProcessing，最后 CreateCopy 成 PNG；finally 里把 hs/out 置 None。

**两点需要写清**：
1. 缩略 VRT 上算 hillshade 与「全分辨率算完再缩」**不是同一张图** —— 坡度按已变粗的像元尺寸计算，起伏会明显更平缓、更平滑。对预览图可接受（甚至更干净），但这是视觉行为变化，不是纯性能优化。
2. 这个改法**不覆盖上面新加的那条** —— BuildVRT 取并集范围的问题依旧（相距 10° 的两片 tif 仍合出 1030×1030 的 VRT），只是从「按满幅分配 1.2 GB」变成「按满幅分配一张 1600px 缩略图」。内存问题解决了，预览图绝大部分仍是 nodata 空白。要治那条得另做（按各输入实际范围裁剪或分别出图）。

## M14. 回车连击穿透双重确认，直接删磁盘产物

**位置**：`static/js/ui.js:169`（Enter 一律 `cleanup(true)`，无 `e.repeat` 守卫、无节流）、`:151-165`（先 removeEventListener 再 resolve）、`:175`（executor 里同步挂 keydown）
**受害调用点**：`static/js/history.js:754/760`（删除任务 →「是否同时删除磁盘上的下载产物？」）

**问题**：`cleanup` 摘掉 A 的监听后 `resolve(true)`，续体是微任务，在同一轮事件循环末尾就同步调用第二个 `showConfirm` 并挂上监听 —— 必然早于下一个 keydown 宏任务。浏览器 Enter 自动重复（`e.repeat=true`，preventDefault 拦不住）或用户快速连按两下，第二个确认框在用户看清之前就被自动确认。全项目 `e.repeat` 零命中。

（原稿说「在淡入动画还没跑完时就被确认」略夸大：淡入是 200ms（`style.css:2597/2613`），而操作系统按键自动重复的**首次**延迟通常 250–660ms，多数情况下框已淡入完成才被穿透。「动画未跑完就被确认」只在用户手动快速连按两下时成立。风险不变。）

**实质危害限于 history.js:760**：这里第二个框问的是**另一个维度**的问题（默认答案 = 取消 = 保留产物），自动确认会替用户选中破坏性的那一边，发出 `?delete_files=true` 删掉任务目录下的瓦片/GeoTIFF/DEM —— 用户从未做出这个选择。**四条管线一视同仁**：`static/js/history.js:772` 无论如何都显式带上 `?delete_files=true/false`，而 `routes/local_terrain_api.py:122` 会尊重显式的 `false` —— 所以后端默认值在这条链路上根本用不到。（原稿以「local_terrain 后端默认本就是 true」豁免了它，不成立。）

**已修正的夸大**：`static/js/config.js:82-93` 的两道确认问的是**同一件事**，被穿透只是少了停顿思考的机会，清理范围仍由用户先前那次点击决定（只有点「全部缓存」时才是整个 cache/）。Escape 也会连穿，但两次都是取消，方向安全。

**改法**：`onKey` 里加 `if (e.repeat) return;`，并在 `showConfirm` 内记录 `openedAt = performance.now()`，Enter 时忽略挂载后 300ms 内（与淡入动画对齐）的按键。

**两半不可互换，`e.repeat` 是承重的那一半**：按键重复间隔约 30ms，而 Windows 最短首次延迟是 250ms —— 只加 300ms 宽限窗口的话，第一次重复被挡在 ~280ms，紧接着 ~310ms 那次照样穿透。300ms 窗口只能覆盖真人快速双击。

## M15. 连续编辑两个坐标时，浮层重建与委托点击互踩

**位置**：`static/js/map.js:877`（blur → `commit(true)`）、`_applyBoundsEdit` 的两条出口 `:884/896/901/906`（同步 `updateBoundsInfo`）与 `:912`（→ `:343` rAF 重写）、委托点击在 `:1584`

**问题**：真正的分野是「浮层重写发生在 click 派发之前还是之后」，不是「校验成功还是失败」。只要在编辑态下用鼠标点浮层里的任何东西（另一个 `.bounds-v`、下载按钮、删除按钮），mousedown 就先触发 blur → 提交 → 整层 `innerHTML` 重写：
- 重写在 click 之前（校验失败必然如此；校验成功时只要间隔跨过一帧也如此）→ 委托监听器挂在 `#boundsInfo` 上，而 innerHTML 重写后旧节点的事件传播路径已不再包含 `#boundsInfo`，处理器**根本不会被调用**（浏览器在 mousedown 目标脱离文档时通常也干脆不派发 click）→ **这一次点击完全没反应**，必须再点一次。（原稿把原因写成「`closest('.bounds-v')` 返回 null 或游离节点」，那不是起作用的机制 —— `closest()` 在脱离文档的节点上照样能匹配。）
- 重写在 click 之后（仅校验成功且点击极快）→ 第二个输入框刚建好就被抹掉，焦点丢失，之后敲的字全丢。

**改法**：让重渲染跳过正在编辑的格子（`updateBoundsInfo` 检测 `#boundsInfo` 内是否存在 `.bounds-edit-input`）—— **只有这一条是承重项**。另两条是配套的健壮性改进，单独实施不解决丢点击：把 blur 提交延到 rAF 之后再判断 `document.contains(input)` 挡的是重复提交，rAF 仍在 mouseup/click 之前触发；让失败分支也走 `_scheduleBoundsInfoUpdate` 单独实施反而把成功分支的偶发问题变成必现。

## M16. 工具条按钮的键盘焦点圈被容器 overflow:hidden 完整裁掉

**位置**：`static/css/style.css:567`（`.map-panel-triggers { overflow: hidden }`）、`:570-607`（`.map-panel-btn` 没有任何 `:focus-visible` 规则）、全站唯一命中裸 button 的焦点样式在 `:2358-2361`（`outline-offset: 2px`）

**问题**：容器无内边距、内容盒宽度正好等于按钮的 30px，`outline-offset:2px` 画出的整圈都落在 padding box 之外；outline 不计入 scrollable overflow region，所以是直接剪掉。Playwright 实测（等价最小复现）：单按钮分组（「框选」，`templates/index.html:51` 独占一组）聚焦后**屏幕上一条焦点线都没有**；三按钮分组的中间按钮只剩两截压在邻居身上的 2px 水平线，左右竖边永远不可见。`.map-panel-btn` 也没有任何替代焦点指示（JS 只切 `--active` 类）。

契约测试照不到：`test_focus_visible_has_a_visible_outline` 只遍历 `BUTTON_CONTEXTS` 那 11 个 `.btn` 上下文（`.map-panel-btn` 不在其中），**不建模祖先裁剪** —— 它对入册的上下文其实查得不浅（层叠后的 `outline-width ≥ 2px`、`outline-style` 非 none、焦点环对背衬 ≥ `BTN_RING_MIN_CONTRAST`），问题纯粹是「没入册 + 不管裁剪」。

**改法**：`.map-panel-btn:focus-visible { outline: 2px solid var(--color-accent); outline-offset: -2px; background-color: var(--color-control-hover); }`（负 offset 画进 padding box 内不会被裁），或去掉容器 `overflow:hidden` 改用首尾按钮各自的 border-radius。在 `test_css_contract.py` 补一条「焦点轮廓不得被任何 overflow!=visible 的祖先裁掉」的断言。

## M17. 未勾选的复选框/单选框边界对比度 1.35:1，比 Bootstrap 默认还差

**位置**：`static/css/style.css:2223-2224`（`background: var(--color-bg-tertiary)` + `border: 2px solid var(--color-border)`）

**问题**：暗色边框实算 rgb(44,48,54) 对卡片/弹窗底 #15171c 只有 **1.35:1**，填充 1.10:1；亮色边框 rgb(218,221,225) 对白底 **1.36:1**，填充 1.11:1。而项目对图形元素反复声明过 3:1 下限（`style.css:20-27`、`58-63`、`1313-1316` 的注释，以及 `PROGRESS_FILL_MIN_CONTRAST=3.0`、`ERROR_BORDER_MIN_CONTRAST=3.0`、`BTN_RING_MIN_CONTRAST=3.0` 三条已有断言）。更关键的是：Bootstrap 自己的 `--bs-border-color`(#495057) 能拿到 2.19:1，站内覆盖后**只剩它的 62%**（1.35/2.19，即恶化 38%；按「超出 1 的对比度」算则恶化 70%）。原稿写「改差了 62%」是把比值说反了。`grep -c "form-check" tests/test_css_contract.py` = **0**。

勾选态没问题（accent 暗色 8.37:1 / 亮色 7.56:1）。坏的是未勾选时「那个可点的方框在哪」—— 复选框除了这个方框什么都没有，正是 WCAG 1.4.11 要管的。同令牌问题也影响 `.form-control/.form-select`，但它们还有文字/箭头撑起可见性。

**触发场景**：「下载数据」弹窗切到「高程」时的两个未勾选复选框、配置页的「启用瓦片缓存」。

**改法**：新增专用令牌（如 `--color-control-border`）并在 light 块给出亮色值，保证边框对**周围表面**（`--color-bg-secondary`）≥3:1；按 `test_progress_bar_fill_has_sufficient_contrast` 的写法补一条 form-check 边界断言。

**取值必须实算，别照抄原稿**：暗色 `#6b7280` 可用（对 #15171c = 3.71:1）；但原稿给的亮色 `#9ca3af` 对白底只有 **2.54:1**、对控件填充 #f1f3f5 只有 **2.28:1**，比它要修的 3:1 门槛还低，照抄会写出一个通不过自己新断言的令牌。亮色档应取 `#858c97`（白底 3.39:1 / 填充 3.05:1）或直接复用亮色块已有的 `--color-neutral: #6b7280`（4.83:1 / 4.35:1）。

## M18. 三处正文文字用了被明令禁止当文字色的 --color-text-muted

**位置**：`static/css/style.css:2934`（`.statusbar-event` 状态栏最近事件）、`:2864`（`.bounds-hint`，由 `map.js:821` 渲染「拖拽角点调整 · 点击数值编辑」）、`:3064`（`.tile-estimate` 下载弹窗瓦片预估）

**问题**：该令牌的定义处（`:20-27`、`:210-213`）逐字写明它**不是文字色**、只许用于图形元素，并声称约束由 `test_every_text_context_meets_wcag_aa` 兜住。实算：#5f6670 压 #15171c = **3.09:1**（原稿写 3.07 偏低 0.02；style.css 自己的注释 `:21` 写的就是 3.09），#8f959f 压 #ffffff = **3.01:1**，字号 12px 够不上大字豁免，全部低于 AA 正文的 4.5:1。`git blame` 显示三处分别出自 c854e12fe4（2026-07-30）与 857cea4bd6（2026-07-31），均晚于确立禁令的 A7/Task 12 —— 是新增回归，不是历史豁免。

安全网缺口比「三条不在清单里」更大：`_text_contexts()`（`test_css_contract.py:5152-5258`，手写 17 条并 `assert len == 17`）覆盖的区域只有记录面板、详情弹窗、首页表单和 toast，**整条状态栏、整个地图浮层体系、下载弹窗的非表单内容**在模型里都没有入口。

**改法**：三处一律改 `var(--color-text-secondary)`（暗 6.81:1；亮色下这三处的实际背衬是白底，实测 **7.5:1**，原稿写的 6.6:1 是对 `--color-bg-primary` #eef0f3 的最坏档），层级差交给已有的 `font-size: var(--font-size-xs)` 承担（与 A7 对 `.form-text`/`.detail-k` 的处理同一套）；把这三条上下文补进 `_text_contexts()` 并把 17 改成 20 —— 但要意识到补完缺口依然在。

**注**：`:2185` 的 `.page-item.disabled .page-link` 也用 muted 当 color，但那是 disabled 控件（且叠了 opacity:.5），WCAG 1.4.3 明确豁免，不算违规。

## M19. 发布包里混进冒烟测试生成的数据库，默认保存路径写死成 CI 机器路径

**位置**：`.github/workflows/build.yml:136-148`（Package 直接对 `dist/terraforge/` 整目录打包）、`:115-134`（Smoke test 在 `cd dist/terraforge` 后真正启动 exe），两步之间无任何清理

**问题**：frozen 模式下 `Config.BASE_DIR = Path(sys.executable).parent`，那次启动会在 `dist/terraforge/` 里创建 `data/map_downloader.db`、`downloads/`、`cache/` 和 `smoke.log`，全部被 tar/7z 进 Release 资产。而那个 DB 不是空壳：`init_database()` 播种 `'./downloads'` 后在**同一次调用**里把它归一成**当时机器上的绝对路径**（`core/database.py:534-554`）。用户首次启动时该值已是绝对路径，归一化分支只处理相对值、不会再动它。

**触发场景**：三平台矩阵全中。Windows 包里是 `D:\a\map-download\map-download\dist\terraforge\downloads`，Linux `/home/runner/...`，macOS `/Users/runner/...`。用户解压运行 → 表单被预填成该路径 → 有可写 D 盘时几十 GB 瓦片写进一个想不到的位置（且不在 exe 旁边，与「便携」设计相悖），无 D 盘或非 root 时 mkdir 失败任务必败。DEM 管线的服务端兜底（`dem_task_manager.py:105`）更隐蔽 —— 请求不带 output_path 时静默用该值。

**改法**：Package 步骤前 `rm -rf dist/terraforge/{data,downloads,cache,smoke.log}`（或让冒烟测试在 dist 之外的拷贝里跑），并加一条断言：打包前 `dist/terraforge` 下不得存在 `data/`。

## M20. build.bat 不检查 nuitka_build.py 退出码，GDAL DLL 补拷失败仍报「Build successful!」

**位置**：`build.bat:62`（调用后不看 errorlevel）、`:65`（只用 `if exist "dist\terraforge"` 判定成败）
**对照组**：`build.sh:6` 有 `set -euo pipefail`，同样的调用失败即以非零码退出

**问题**：`nuitka_build.py:385-390` 在很早就把 `dist/app.dist` 重命名成 `dist/terraforge`，之后才依次执行产物自检 —— 所以这些自检一旦触发，目标目录已经存在了。`build.bat` 里每一个前置步骤（uv 存在性、依赖安装、GDAL pin、osgeo 可导入、版本比对）都检查了 errorlevel，唯独主构建调用没有。被吞掉的是**两处自检 + 一处可能抛意外异常的补拷步骤**：`:393-394`（exe 未生成，raise）、`:397 → :248-253`（OSGeo4W 等非 conda 布局下 GDAL DLL 闭包补拷失败时主动 raise，错误文案明说是为了防止交付「能构建能启动但每次 GDAL 调用都失败」的包）、`:398`（`alias_conda_tagged_extensions` —— 读全函数 293-326 确认它自己不 raise，只会因 `import osgeo` 或 `shutil.copy` 抛意外异常而失败；原稿把它算作「自检」不准确）。

**同一条路径上的第二种失因**（原报告未提）：`build.bat:49-52` 安装的 nuitka **未 pin 版本**，而 `nuitka_build.py:219` 导入的是 Nuitka 私有 API `nuitka.freezer.DllDependenciesWin32.detectBinaryPathDLLsWin32`，Nuitka 升级改签名就会在此抛异常 —— 同样发生在重命名之后，同样被判成「Build successful!」。（原稿称它「更易发生」不成立：`:219` 被 `:214-216` 的 `if _find_bundled_gdal_dll(dist_dir): return` 挡在同一道门后，与 `:248-253` 的触发人群完全相同，只是失因不同。）

**改法**：`:62` 之后立刻加 `if errorlevel 1 ( echo Build failed! & exit /b 1 )`，把 `if exist` 降级为附加断言；顺带给 `uv pip install nuitka` 也补检查。`tests/test_fix_build_scripts.py:39` 只断言 `build.sh` 有 pipefail，对 `build.bat` 没有等价断言 —— 两脚本的不对称从未被约束。

**注**：`verify_no_missing_libs`（ldd 自检）在 Windows 是 no-op（`:264-265` 直接 return），不在被吞之列。CI 直接跑 `python nuitka_build.py`，退出码正常传播，不受影响。

## M21. 四条管线的 start/pause/resume HTTP 端点几乎零覆盖

**位置**：`routes/api.py:207/240/273/306`、`routes/dem_api.py:114/128/142/156`、`routes/contour_api.py:183/197/211/225`、`routes/local_terrain_api.py:101`
**现状说明在**：`tests/test_fix_api_hardening.py:6` 的 docstring

**问题**：穷举全测试套件打过的 URL 字面量后确认，**没有任何一条**请求过四条管线的 `/start`、`/resume`，contour 的 `/pause`，local 的 `/cancel`，或 GET `/api/tasks/<id>`；地图管线的 pause/cancel 只有 ValueError→400 分支被打到，200 成功路径一行没跑过。也没有任何用例直接调用视图函数。这些端点里各自**手抄**了一份 8-10 行的 `except ValueError → 400`（contour/dem 的 delete/cancel 还手抄了 `404 if "not found" in msg else 400` 的分流）—— 复制粘贴最容易漏的地方，恰恰零覆盖。

上轮 review 的 MEDIUM #15 就是这类漏抄的实例（contour 的 cancel 漏了 `except ValueError`，用户点取消得到 500），靠人读代码发现，测试全绿。这些端点不是死代码：`static/js/tasks.js:790/805/819/837` 用 `apiPrefixForType` 拼四条管线共用的 start/pause/resume/cancel。

**当前不存在活 bug** —— 逐个读过四个模块的 handler，`except ValueError` 一个不缺，e3a5d82de 那次漏抄已修。这是回归检测能力的缺口。

**改法**：补一组对称的参数化用例 `(前缀, 表名)`：对不存在的 id、终态任务、状态不对的任务分别 POST start/pause/resume/cancel，断言状态码严格等于约定值（404 vs 400），并至少覆盖一次成功路径（200 + DB 状态翻转）。start 的成功分支难测有正当理由（会真拉起下载线程），但**错误分支零副作用、极易补**（`POST /api/tasks/9999/start` 等各断言一次即可）—— 恰恰是这批最廉价的用例一条都没有。

## M22. 浅路径删除守卫零有效覆盖，用例是碰巧通过的

**位置**：`tests/test_delete_files_cleanup.py:321`，守卫在 `services/task_cleanup.py:109`

**问题**：断言 `remove_task_dir_if_safe(Path(os.path.abspath(os.sep))) is False` 看起来在钉浅路径守卫，实际不具区分力：测试里 `DOWNLOADS_DIR` 被 patch 成 `tmp_path/'downloads'`，把 `:109` 删掉后 `:115`（`target in downloads_root.parents`）会等价地返回 False。变异实测：`mutant('/') → False`（照样绿），而只有 `:109` 能挡的形态 `'/contour_task_5'` → `mutant` 返回 True（rmtree 放行）、真实代码返回 False。用例名 `test_cleanup_refuses_shallow_and_home` 会让后来的人误以为已被钉住，这比没有用例更坏。

生产配置下 `DOWNLOADS_DIR` 在 exe 目录旁，一个一级目标路径不会命中 home/downloads/cache 中任何一条，浅路径守卫是唯一防线。

**缓解事实**（收窄暴露面，不推翻结论）：`services/geo_validation.py:100-127`（`require_absolute_output_dir`，守卫本体在 `:123`）在建任务时已按同口径拒绝 `parts<3`，且该层被 `tests/test_path_browser.py:68-74` 有效覆盖，所以浅 output_path 只能来自 0.2.3 前的存量行或手改 DB；可能拿到的入口是 `routes/dem_api.py:101` 和 `routes/api.py:406`（contour 的 output_path 硬编码，用户输入进不来）。

**改法**：补一条一级路径断言，例如 `assert tc.remove_task_dir_if_safe(Path(os.path.abspath(os.sep)) / 'tf_shallow_probe') is False`（该路径不存在，即使守卫失效也不会真删，但变异时会翻红）。

## M23.〔上轮已提出仍未修〕47 个测试文件各写一份 pop 清单，且 routes.* 的注入型全局无人恢复

**位置**：`tests/conftest.py:30-59`（`fresh_import` 只恢复 sys.modules 与父包属性）、`:81`（`isolated_app` 只传 `"app"`/`"core.database"`）

**问题**：上轮 MEDIUM #21 的结论是「新增 conftest.fresh_import 统一隔离工具」，但工具只被 conftest 自己和 3 个文件直接用（算上 `isolated_app` 的 4 个使用者，并集 6 个）；47 个测试文件仍各写一份互不相同的 `sys.modules.pop(...)` 清单（共 54 处调用），其中 **46 个从不恢复**（唯一恢复的是 `tests/test_fix_terrain_gdal_import.py`），**44 个**根本不 pop `routes.*`（只有 3 个 pop 了：`test_delete_files_cleanup.py:23-25`、`test_fix_api_hardening.py:36`、`test_fix_dem_delete_tiling_guard.py:154`）。conftest 的 docstring 自己也承认「Existing test files should migrate their ad-hoc pop loops to it」。

更关键的是：`create_app()` 通过 `init_task_manager(...)` 把 manager 注入到 `routes.*` 的**模块级全局**里，任何隔离工具都管不住。实测跑完用 `isolated_app` 的测试后，`sys.modules['app']` 已被恢复成原实例，但 `app.task_manager`（id …847488）与 `routes.api.task_manager`（id …769600）是两个不同对象，后者仍是 fixture 里那个绑定到已删除 `tmp_path` 的管理器。

`tests/test_fix_api_hardening.py:27-31` 的注释逐字描述过这个坑（「测试 patch 新模块、请求却打到旧模块」）并靠 `:32-38` 手抄 **11** 个模块名的 pop 清单绕开 —— 是踩出来的而非理论风险。当前全量正序/逆序都绿（全库仅一个不隔离地 import app 的文件 `tests/test_app_mp_worker_guard.py`，且它那句 `import app` 跑在 spawn 出来的**子进程**里、根本碰不到父进程的 `sys.modules`），属潜伏状态：任何人新增一个不做隔离的 app 用例就会引爆，失败模式是静默假绿而非报红。

**改法**：47 个清单统一迁到 `fresh_import`/`isolated_app`；给 `fresh_import` 补注入型全局的恢复（重导入前记录 `routes.api`/`dem_api`/`contour_api`/`local_terrain_api`/`terrain_api` 的 `*_task_manager` 原值，teardown 还原）；加一条自检用例钉住「teardown 后 `app.task_manager is routes.api.task_manager`」。

## M24.〔已修，残留一项〕README 描述的等高线入口和自动下载流程都不存在

> **状态**：主体已由提交 `1cd918598` 修掉（该提交 body 逐条复述了本发现），`README.md:130-134` 现已是上传驱动口径。**残留**：README 全文 grep「处理类型 / 数据处理」仍**零命中** —— 现文案只写「切换到等高线」，用户看完仍不知道该点哪个控件，本条点破的「正确入口完全缺失」尚未闭环。

**位置**：`README.md:130-134`

**问题**：两句话各错一半。(a)「下载类型切换为等高线」—— 前端「下载类型」单选组只有「瓦片」和「高程」两项（`templates/index.html:122-127`），等高线在另一个独立的「处理类型」下拉里（`:239-242`），切不过去；(b)「任务自动完成 DEM 下载 → 等高线渲染 → XYZ 瓦片输出全流程」—— 等高线自 0.2.4 起是纯上传驱动，`routes/contour_api.py:89-94` 无文件直接 400，`create_task_with_files` 把 dataset 写死 `'upload'`，全程不下载。CLAUDE.md:12 已经写对了，只有 README 没跟上 —— 而 README 恰恰是本次未提交改动里被整体重写的文件（383 增 345 删），等于新写了一段过时内容。

更糟的是 README 全文 grep「处理类型 / 数据处理」零命中：不只是入口写错，而是**正确入口在 README 里完全缺失**，用户没有自救线索。

**触发场景**：新用户按 README 操作，摸到「处理类型」选中等高线后只框选区域不上传就点创建，前端 `static/js/map.js:1101-1103` 弹「请先选择至少一个 .tif/.tiff 文件」并中止，卡在这一步。

**改法**：改成上传驱动口径（处理类型选等高线 → 上传 .tif → 远程高程请先跑 DEM 任务，产物在 `downloads/dem/dem_task_<id>/`），删掉「框选区域」和「自动完成 DEM 下载」。同批需修的还有 `README.md:34` 的「水体掩膜均可配置」（见 M26）。

## M25.〔已修〕README 称 DEM 必须 Earthdata 账号，实际默认数据源免认证

> **状态**：已由提交 `1cd918598` 全部修掉，四处现已是「默认 Copernicus GLO-30 免认证 / ASTER 需 Earthdata」口径，`:46` 按要求未动。

**位置**：`README.md:32`、`:125`、`:150`、`:373`（原稿写 `:371`；四处一致地说「必须 Earthdata」）；`:8` 与 `:219` 不完整（只提 ASTER）

**问题**：代码实际默认是 `COP-DEM-GLO-30`（`services/dem_task_manager.py:100`），走公开 AWS 桶（`dem_download_engine.py:58-60`），`_dataset_requires_auth` 明确 `return dataset != "COP-DEM-GLO-30"`，全链路不碰 Earthdata；界面下拉的默认选项文案就是「Copernicus GLO-30（30m，推荐，更干净，免认证）」（`templates/index.html:176-179`）。创建/启动 DEM 任务链路完全不检查账号。测试也钉死了免认证（`test_dem_task_manager_dataset.py:32-37`、`test_dem_download_engine_url.py:31-35`）。等高线更是完全不下载，谈不上需要账号。

**触发场景**：用户读 README 后认为必须先去 NASA 注册并等审批，要么放弃该功能要么白花时间；反过来 README 从头到尾没提「数据源」可选，用户也不知道有 Copernicus/ASTER 切换，更不知道 ASTER 覆盖只到 83S–83N 而 Copernicus 没这个限制。

**改法**：四处改为「DEM 默认使用 Copernicus GLO-30（AWS 公开桶，免认证，全球覆盖），界面可切到 ASTER GDEM v3（需 Earthdata，覆盖 83S–83N）」；`:150`/`:371` 的「必需」限定为「仅选 ASTER 时」，并删掉其中的「等高线」。**`:46`「配置页……Earthdata 账号等」是正确的，不要改**。

## M26.〔部分已修，CLAUDE.md 仍待修〕文档称等高线水体掩膜可配置，实际上传路径硬编码 water=0

> **状态**：`README.md:34`（已删「水体掩膜」）与 `docs/notes/geolibre-takeaways.md:68`（已加限定）由提交 `1cd918598` 修掉；**`CLAUDE.md:12` 的 `water mask` 三个字被漏掉，至今未改** —— 那次提交改的是同一行的另外半句。CLAUDE.md 是 AI 速查区，错误口径留在这里传染性最强，应优先补上。

**位置**：`CLAUDE.md:12`、`README.md:34`、`docs/notes/geolibre-takeaways.md:68`（原稿写 `docs/geolibre-takeaways.md:68`，`a439aa6e2` 的 docs 分层重构已将其移入 `notes/`，行号未变）

**问题**：当前唯一的创建入口 `create_task_with_files`（`services/contour_task_manager.py:263-373`）既不接收水体参数，也在 INSERT 的 water 列写死常量 0（`:368`），函数自己的 docstring `:277-278` 也承认。前端 `templates/index.html:252-301` 的等高线选项区只有上传、等高距、背景、地形着色、配色自定义五组控件，全仓 `grep water templates/ static/js/` 零命中。渲染期 `:572` 的 `want_water` 因此恒为 False，ASTWBD 枚举永远返回空。

这是**纯文档漂移，不是代码 bug** —— `tests/test_contour_api.py:52` 已把 `task["water"] == 0` 钉死。水体代码路径本身完整、对历史遗留的下载驱动行（DB 默认 `water INTEGER DEFAULT 1`）依然可运行，但今天无法新建这类任务。

**改法**：CLAUDE.md:12 改为「configurable interval, background, shading/hypsometric tint」并注明「水体掩膜仅遗留下载驱动任务可用，上传驱动的新任务恒为 water=0」；README.md:34 删掉「水体掩膜」或加同样限定。若打算保留该能力，则给 `create_task_with_files` 加 water 形参并补前端开关。

---

# LOW（已通过对抗性验证）

## L1. 等高线崩溃恢复漏掉 downloading 行，缺文件也报 completed

**位置**：`services/contour_task_manager.py:574-579`（待下载列表只取 `pending/failed`）、`:667-676`（`pending_count` 只统计 `status='pending'`）
**对照组**：`services/dem_task_manager.py:538-544`（C4 修复：`UPDATE dem_files SET status='pending' WHERE status='downloading'`）、`:678`（判据用 `NOT IN ('completed','skipped','failed')`）

**问题**：`'downloading'` 这个中间态在两处都被漏掉，残留行既不会被重下、也不会阻止任务置 completed，渲染在缺几块 1° DEM 的输入上出图，成品带缺口而任务 completed。

**触发条件三重叠加，故为 low**：(1) 当前版本**无法创建**下载驱动的等高线任务（唯一入口是纯上传，下载驱动的 `create_task` 已删除），只有从旧版本升级上来的存量 DB 行能跑进这条路；(2) `'downloading'` 残留只来自进程被**硬杀**（正常暂停时 `dem_download_engine.py:186-188/265-267` 会回写 `pending`；该状态本身是 `dem_download_engine.py:194` 上报、经 `contour_task_manager.py:597` 的回调写进 `contour_files` 的 —— contour 自身 grep `downloading` 零命中，这个反直觉点是真的）；(3) 必须「部分下完」—— 一颗都没下完时 tiler 返回 rendered=0，会被判 failed，用户看得到。

**改法**：`_execute` 取列表前补 `UPDATE contour_files SET status='pending' WHERE task_id=? AND status='downloading'`（dem/water 两种 kind 都要），并把 `pending_count` 改成 `status NOT IN ('completed','skipped','failed')`。

## L2. thread.start() 失败后 running 状态无回补，任务永久不可删不可重跑

**位置**：`services/dem_task_manager.py:351`（start_tiling 的裸 `th.start()`）、`services/local_terrain_task_manager.py:364`、`services/contour_task_manager.py:432`
**对照组**：`services/task_manager.py:481-501`、`services/dem_task_manager.py:201-217` 两条下载管线都写了完整回补，且 `tests/test_fix_dem_start_thread_failure.py` 用 `_BoomThread` 把这个契约钉死了 —— 但只覆盖 `start_task`

**问题**：锁内把 job 行 upsert 成 `running` 并 commit，锁外裸调 `th.start()`，没有 try 回补。线程创建失败（`RuntimeError: can't start new thread`）后 job 行永久停在 running：再次 start_tiling 被 `ON CONFLICT ... WHERE status != 'running'` 的 rowcount=0 判为「已在运行」而 ValueError，`delete_task` 又被 `:503-508` 的 DB 状态检查挡住（`is_alive()` 检查拦不住 —— tiling 线程根本不登记进 `active_tasks`，未 start 的线程 `is_alive()` 返回 False）。`routes/terrain_api.py` 整个 blueprint 没有任何 cancel/reset job 的端点，只有重启进程让孤儿恢复把 job 标 failed 才能解开。contour 那条所幸还能靠 pause 翻回 paused 自救。

**为何是 low**：触发条件不受任何 API 输入控制，必须 OS 真的耗尽线程/内存；无数据损坏、无静默错误产出；本地单用户桌面应用重启即自愈。

**范本的一处差异**：`dem_task_manager.py:203-217` 那份回补退到 `paused`，但切片 job 行没有 paused 态，三处 tiling 应置 `failed` + 写 `error_message`。

**改法**：三处 `th.start()` 包进 try，按 `dem_task_manager.py:203-217` 的既有范本回补（锁内按身份校验清 active_tasks/stop_flags，job 行置 failed 并写 error_message，contour/local 置回 paused/failed），再向上抛。回归测试也需横向同步。

## L3. total_running_seconds 在两个任务列表接口里都没返回

**位置**：`routes/api.py:576-592`（`/api/history_all` 的 map 分支 SELECT 列表没选它）、`models/task.py:200-244`（Task 数据类无该字段，`to_dict()` 自然不输出，导致 `/api/tasks?status=active` 也丢）

**问题**：`tasks` 表有这一列且由 pause/complete 累计，前端 `calculateTimeInfo`（`static/js/tasks.js:715-746`）的注释明确写着这两个接口「本来就是 tasks 列的累计值」，字段缺失时才回退按 `started_at` 算墙钟 —— 那个分支本来是给不写该列的 dem/contour/local 三条管线兜底的。

**持久错误只有两种场景**（正在下载的任务会被 socket 的 `task_progress` 载荷在 0.5 秒内修正）：① paused 任务在页面刷新后（暂停期间无 socket，且 `updateTimeDisplay` 只刷新 running 行，错误值一直挂着）；② 下载已完成、处于拼接/复制阶段的 running 任务在页面刷新后（该阶段只发 `task_stitch_progress`/`task_copy_progress`）。终态任务不受影响（`history.js:229-251` 只对 live 状态调 `calculateTimeInfo`）。ETA 被放大只发生在场景 ②（暂停行根本不算 ETA）。

**改法**：`history_all` 的 map 分支加该列（其余三分支补 `NULL AS total_running_seconds` 对齐），并给 `Task` 数据类补字段（`from_row` 读、`to_dict` 输出）。

## L4. 等高线串行/回退路径 rmtree 时 GDAL 仍握着 warp 产物

**位置**：`services/contour_engine.py:798-805`（finally 只 `plt.close`，然后 `shutil.rmtree(tmpdir, ignore_errors=True)`），ctx 在 `:345-396` 打开的 `ctx.ds`/`ctx.att_ds` 从未置 None

**问题**：Windows 上 GDAL 持有的文件删不掉，`ignore_errors=True` 静默吞掉 PermissionError，`:638` 的注释「tmpdir is removed at the end」在这些路径上不成立。凡是「ctx 建成后未被置 None 就走到 finally」的都中招：(a) `n_workers==1 or total<=4` 的串行；(b) BrokenProcessPool 回退里 `_render_serial` 重建的 ctx；(c) `:688` 之后、`:770` 的 `ctx=None` 之前抛出的任何异常。只有并行成功路径干净。对照 `download_engine.py:1001/1024/1058` —— 同项目 stitch 路径每个 dataset 在 rmtree 前都显式置 None，注释还自述了 Windows 文件锁的理由。

**影响面修正**：不是「永久残留、最终写满盘」。`app.py:205` 每次启动的清扫会删掉所有 `contour_warp_*`，进程退出后句柄已释放，重启即回收。真实危害是「单次运行期间每跑一次串行/回退任务泄漏一份（大区域数十 GB），无任何日志，直到下次重启」。

**改法**：finally 里 rmtree 之前加 `ctx = None`（或显式置 ctx.band/att_band/ds/att_ds 为 None）；rmtree 改用 `onerror` 回调记 warning，别静默吞。

## L5. 启动清扫漏扫三处临时目录，硬杀后 GB 级残留在本次运行内不回收

**位置**：`services/task_cleanup.py:192-206`（只扫系统临时目录的两个前缀 + `contour_warp_tmpdir` 配置目录）
**漏掉的创建点**：`services/download_engine.py:931`（配了 `stitch_tmpdir` 时的 work_dir）、`services/local_terrain_task_manager.py:141`（`local_upload_*`）、`services/contour_task_manager.py:331`（`contour_upload_*`）

**问题**：5 处 mkdtemp 型残留点，清扫只覆盖落在系统临时目录的部分 + `contour_warp_tmpdir`。`stitch_tmpdir` 与 `contour_warp_tmpdir` 处理不对称尤其刺眼 —— 这个键存在的意义恰恰是把 GB 级中间产物挪到空间充足的盘，配了它反而进清扫盲区，而配置页的「缓存管理」只覆盖 `Config.CACHE_DIR`，没有任何回收入口。两处上传暂存的保护比原报告说的更弱：是 `try/except: rmtree; raise` + 函数末尾一条 rmtree，**没有 finally**，所以 Ctrl-C / SystemExit 也会绕过。

**注**：docstring 没说谎 —— `:182` 原文就写着「**系统临时目录里的** stitch work_dir」，问题是覆盖面与创建点不对齐，不是文档与代码不符。

**改法**：读 `stitch_tmpdir` 配置扫 `_STITCH_TMP_PREFIX`；新增 `local_upload_`/`contour_upload_` 两个前缀常量，分别扫 `DOWNLOADS_DIR/terrain` 与 `DOWNLOADS_DIR/dem`；把「新增临时目录前缀必须同步登记到本模块常量」写进文件顶部注释。

## L6. deleteTask 不清 activeTasks，状态栏留幻影活动任务

**位置**：`static/js/history.js:753-800`（成功分支不 `activeTasks.delete()`，也不触发 `updateStatusTasks`）
**对照组**：`tasks.js:551`（handleTaskCompleted）、`:657`（dismissTask）、`:843`（cancelTask）全都记得清

**问题**：删除一个 pending/paused 任务（四个 DELETE 端点都只拒 running）后，底部状态栏「N 个活动任务（M 运行中） X%」继续把它算进去；`loadHistory` 又不调 `updateStatusTasks`，文本原地冻结。唯一纠正点是 `loadActiveTasks` 里的 `activeTasks.clear()` —— 只在新建任务、socket 断线重连或整页刷新时发生。

**两条次生结论已证伪**：无 rowid 复用风险（四张任务表都是 `INTEGER PRIMARY KEY AUTOINCREMENT`，且 key 带类型前缀，建任务路径又都先 `loadActiveTasks` 清表）；`updateTimeDisplay` 不产生每秒开销（`tasks.js:766` 对非 running 立即 return）。

**改法**：成功分支加两行（独立页 `/history` 不加载 tasks.js，需 typeof 守卫，与同函数 `stopTaskPreviewForTask`/`closeFailureToast` 的写法一致）：
```js
if (typeof activeTasks !== 'undefined') activeTasks.delete(`${taskType}:${taskId}`);
if (typeof updateStatusTasks === 'function') updateStatusTasks();
```

## L7. loadHistory 无请求序号守卫，旧响应覆盖新筛选结果

**位置**：`static/js/history.js:112-129`（await 前就写死 currentPage/statusParam，await 后无条件覆盖 `allTasks` 并重渲染）

**问题**：真正会产生可见错配的只有一条路径 —— 状态筛选 chip 连点（`:21-27` 无防抖、无禁用、无 in-flight 标志）。若先发的响应后返回，chip 高亮与 `currentStatusFilter` 已是新值，而表格和 `allTasks` 是旧筛选集合。

**已证伪的两条**：「重开面板 + 断线重连并发」不成立（`panels.js:64` 与 `tasks.js:30` 都同步读同一个全局 currentPage，两个请求的 page 与 status 完全相同）；分页高亮也基本不错（`renderPagination` 用的是响应体里的 `p.page`）。危害不含误删/崩溃 —— 晚返回的响应同时覆盖 `allTasks` 和 DOM 行，二者自洽。

**改法**：加模块级序号 `let _historyReqSeq = 0;`，进入时 `const seq = ++_historyReqSeq;`，await 后 `if (seq !== _historyReqSeq) return;` 再渲染，并把 `currentPage`/`allTasks` 的赋值也挪到守卫之后；catch 分支同样要守卫。

## L8. 0.2.4「全盘保存路径」改动后遗留 6 处过时注释/docstring

**位置**：
- `services/task_cleanup.py:4-8`（模块 docstring 称「only allowed for directories that resolve strictly inside Config.DOWNLOADS_DIR」）
- `services/task_manager.py:298-299` 与 `services/dem_task_manager.py:101-102`（建任务处注释称「必须落在 Config.DOWNLOADS_DIR 内」）
- `routes/api.py:349-350`、`routes/dem_api.py:99`、`routes/contour_api.py:170`（DELETE 端点 docstring 称「when it resolves inside DOWNLOADS_DIR」）

**问题**：`require_absolute_output_dir`（`services/geo_validation.py:100-127`）在 0.2.4 已放开 —— 其 docstring 明写「不再强制落在 DOWNLOADS_DIR 内」，函数体只剩绝对路径与 `parts>=3` 两条检查，`base_dir` 形参只为签名兼容保留、不参与校验。`remove_task_dir_if_safe` 同理（自己的 docstring `:91` 是对的，模块 docstring 掉队）。`tests/test_delete_files_cleanup.py:293-300` 已把「允许删 DOWNLOADS_DIR 之外」钉死。

**为何仍需修**：这是唯一执行 `shutil.rmtree` 的共用入口，错误的安全承诺出现在这里风险最高 —— 后续开发者可能据此省掉新管线的路径校验；`routes/api.py` 那处还是对外 API 文档。

**改法**：统一改成当前的五条边界（符号链接分量 / 不足两级深度 / 家目录 / DOWNLOADS_DIR 本身或祖先 / CACHE_DIR 相关一律拒绝）+「0.2.4 起保存路径全盘可选」。注意 `task_cleanup.py` 模块 docstring 里引用 local-terrain 惯例那半句**没错**（`local_terrain_task_manager.py:499-502` 至今保留 `DOWNLOADS_DIR/terrain` 边界）—— 改时要写清两者现在是两套口径。「文件写到校验范围外」那半句表达的理由（打包 exe 从快捷方式启动时按 CWD 解析导致落盘漂移）仍然有效，只改措辞、别删理由。

---

# LOW（原未经对抗性验证，已在二次核对中补验）

按流程 low 项不送验，原稿 14 条的行号与语义未经第二人复核。**二次核对已把这 14 条全部补验完毕**：13 条属实（行号无一漂移，质量高于「未复核」这个标签的暗示），**U9 被推翻为误报已移出**（见文末误报清单）。下表为补验后的 13 条，其中 5 条的建议做法经核对后作了补充。

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| U1 | `services/dem_task_manager.py:382`、`services/contour_task_manager.py:730` | 切片/渲染进度的 `socketio.emit` 未包 try，而它经回调被 `build_terrain` 在瓦片循环里同步调用；一次 emit 抛出会穿透整个作业被记为切片失败 | 各包一层 try/except 只记日志，与 `task_manager.py:1313-1321`、`1437-1445` 对齐 |
| U2 | `services/contour_task_manager.py:461` | cancel 对终态任务静默返回成功（rowcount==0 且行存在时 fall through），路由回 `{"success": true}`；map/dem 都改抛 ValueError 了，contour 没跟上 | 终态时抛 `ValueError(f"Cannot cancel contour task {id} with status '{status}'")`。**必须同步改 `tests/test_cancel_terminal_state.py:198,209`** —— 这两条用例当前直接调 `ctm.cancel_task(...)` 不包 `pytest.raises`，把现行为钉死了；改成 `pytest.raises(ValueError, match="Cannot cancel contour task")`，与同文件 dem 的两条对齐 |
| U3 | `core/database.py:55` | `history_retention_days` 有默认值、有校验、有配置页输入框、前端会提交，但**全项目无消费方**，历史表无限增长 | 要么实现启动时按天数清理四张任务表的终态行，要么整条删掉（比照 0.2.4 移除 `cache_max_size_mb`）；选择写进 CLAUDE.md |
| U4 | `routes/api.py:949` | `/api/fs/browse` 对 `~未知用户`（RuntimeError）、含空字节的 path（ValueError）抛未捕获异常返回 500；源码运行默认 DEBUG=1，Werkzeug 调试器会把完整堆栈回给浏览器 | 解析与存在性检查包进 `except (OSError, ValueError, RuntimeError)` 统一 400 |
| U5 | `static/js/history.js:494` | `_statusStrokeCache` 惰性缓存 6 个状态色令牌，前提是「调色板运行期不变」；主题切换落地后该前提已不成立，`TerraTheme.set` 无缓存失效钩子，小地图矩形留旧主题色 | theme 切换后广播事件置 `_statusStrokeCache = null` 并重跑 `renderHistoryMap`；或把缓存键改成 `resolved()+令牌名` |
| U6 | `static/js/map.js:1338` | 等高线预览面板作为普通流内块 append 到 `.index-map`（`overflow:hidden` + `#map` 高 100%），起始位置就在容器高度之下，被完整裁掉，永远不可见还白拉接口 | 补绝对定位样式，或承认已被记录面板的预览按钮取代、删掉这一整组函数与监听 |
| U7 | `static/css/style.css:2222` | `.form-check-input { margin-left: -1.75rem }` 是 (0,1,0)，被 Bootstrap 的 `.form-check .form-check-input`(0,2,0) 压掉 —— 注释描述的对齐修正从未生效 | 提到 `.form-check .form-check-input`；补一条按层叠求值 margin-left 的断言 |
| U8 | `static/css/style.css:3043` | `.btn-outline-danger` 只写了基态和 hover，缺 `:not(:disabled):focus-visible` 与 `:active`，键盘聚焦时变回 Bootstrap 红（段头注释 `:1467-1503` 已讲明每个变体五态都要写）。该类是活的：`_config_content.html:134,180`、`config.js:71,179` 四处在用 | 照 `.btn-outline-primary` 补两条；把它加进 `BUTTON_CONTEXTS`，并**同步把 `tests/test_css_contract.py:4235` 的 `assert len(BUTTON_CONTEXTS) == 11` 改成 `== 12`**（否则必红） |
| U10 | `static/css/style.css:1414` | 表格时代残留：`.history-table` 六条规则（止于 `:1449`）+ 专供它的 `--color-row-hover` 令牌（暗 `:124`/亮 `:254`，唯一消费点就是那条死规则）全部零引用；同批零引用的还有 `.action-buttons`（含 @media 两条）、**`.container, .container-fluid`（分组选择器，`.container` 同样零引用，别只删半边）**、`.input-group`、`.bg-white`、`.alert-dismissible .btn-close` | 按 `:2189-2197` 的既有做法整段删除；`.bg-white` 与 `.container/.container-fluid` 各带 1 处 `!important`，按项目棘轮惯例把 `tests/test_css_contract.py:439` 的 `<= 59` 降到 `<= 57`（实测当前 56，不降也不会红）。**不存在钉住令牌清单的测试**，删 `--color-row-hover` 不打红任何用例 |
| U11 | `core/logging_setup.py:70` | `root.setLevel(os.environ.get('LOG_LEVEL','INFO').upper())` 非法值直接抛 `ValueError: Unknown level`，用户看到裸 traceback 且提示不含变量名（原稿称「在 handler 装好之前」不确：`:69` 已 `addHandler`，`:70` 才崩；对用户所见无影响） | 白名单校验后回退 INFO 并 warning 打出变量名，与 `core/config.py:17-32` 的 `_parse_max_content_length` 一致 |
| U12 | `core/process_watchdog.py:79` | 〔上轮已提出仍未修〕PID 复用防护只在 Linux 生效（靠 `/proc/<pid>/cmdline`），Windows 上退化成纯 `pid_alive` 探活，而 Windows 的 PID 回收更激进 —— 看门狗可能永不退出，孤儿子进程占着 5000 端口。**触发范围有限**：`start_parent_watchdog()` 只在 `app.py:341-342` 的 `WERKZEUG_RUN_MAIN=='true'` 分支调用（即只在 reloader 子进程里跑），而打包 exe 默认 `DEBUG=0` 不开 reloader —— 实际只影响「Windows + 源码运行 + DEBUG=1」的开发场景 | Windows 用 `GetProcessTimes` 读创建时间做身份校验（创建时间+PID 才唯一），或改用 Job Object / 继承匿名事件句柄 |
| U13 | `tests/test_tiles_static.py:72` | `/tiles` 的路径穿越用例断言 `in (400, 404)`，而 404 是该路径的默认结果 —— 实测删掉 `terrain_static.py:103-104` 的守卫，本用例照旧通过（`test_fix_terrain_traversal.py` 已重写过 terrain 链路的同款空洞，tiles 被漏下） | 照 `test_fix_terrain_traversal.py` 改成正向可证：任务目录外放 canary，断言 400 且响应体不含 canary；再补一条直调 `_resolve_safe_file` 断言 `HTTPException.code == 400` |
| U14 | `CLAUDE.md:85` | 称「migrations/ 目录存在但不是主要机制」，实际该目录在上轮修 HIGH #8 时被清空，git 不追踪空目录，fresh clone 上根本不存在 | 改为「schema 演进只在 `init_database()` 内进行；migrations/ 已废弃，不存在旁路迁移执行器」 |

---

# 存疑待人工判断

**无。** 本轮所有送验发现都得到明确裁定（保留或下调），没有出现「验证者无法确定」的条目。

# 被推翻的误报清单

**首轮对抗性验证：0 条被整条推翻。** 45 条送验发现全部在代码里得到确认（22 条被下调级别、多条被改写描述）。

**二次核对（覆盖含未送验的 14 条 low）：推翻 1 条 —— U9。**

| 条目 | 原说法 | 实际情况 |
| --- | --- | --- |
| **U9** | 提交按钮转圈图标用了 `animation: spin`，但全仓 5 个 @keyframes + vendor 里都没有名为 `spin` 的，动画静默失效 | **`@keyframes spin` 就在 `static/js/map.js:1077`**，与 `fadeOut` 一起写在 `document.createElement('style')` 的 textContent 里，由同文件 `:1085` 在脚本求值时 `document.head.appendChild` 注入 —— 早于任何一次点击。map.js 由 `templates/index.html:400` 加载，`animation: spin` 全仓唯一使用点（`:1017`）就在同一个文件里，动画正常工作。「全仓 5 个 @keyframes」的计数也错：style.css 6 个 + map.js 2 个。根因是审查员只 grep 了 `static/css/` 与 vendor，漏了 JS 运行时注入的 `<style>`。**若照建议去 style.css 补一条 `@keyframes spin`，反而造成重复定义。** |

但验证过程中**有 16 处次生结论被证伪**。这些是审查员在正确的核心发现之上外推出的错误推论，记录在此以免下次重复走同一条弯路：

| # | 被证伪的说法 | 实际情况 |
| --- | --- | --- |
| 1 | 「建 DEM 任务不填 output_path 会回退到 `default_save_path` 再 400」 | `routes/dem_api.py:36-39` 把 output_path 列为**必填**，缺字段直接 400「Missing required fields」；`dem_task_manager.py:105` 的回退经 HTTP 不可达 |
| 2 | 「SQLite rowid 复用后新任务会命中前端 activeTasks 里的陈旧对象」 | 四张任务表全是 `INTEGER PRIMARY KEY AUTOINCREMENT`，id 单调不复用；key 还带类型前缀，建任务路径又都先 `loadActiveTasks` 清表 |
| 3 | 「`updateTimeDisplay` 每秒为已删任务做 getElementById」 | `tasks.js:765` 第一行 `if (task.status !== 'running') return`，陈旧的 pending/paused 项走不到 |
| 4 | 「启动清扫删掉 `.part` 会让瓦片记失败」 | 写缓存的整段被 except 吞（即 H2），replace 抛异常只 warning，瓦片仍返回 `completed`；且窗口只有毫秒 |
| 5 | 「第二实例删掉 work_dir = 几小时下载白干」 | 瓦片在共享 cache + 已镜像的输出目录里都在，重跑只需重做拼接/warp |
| 6 | 「`verify_no_missing_libs` 在 build.bat 路径上被吞掉」 | 该函数 `:264-265` 对 win32/darwin 直接 return，是 no-op；被吞的是另外三处自检 |
| 7 | 「`routes/contour_api.py:172` 会拿到用户提供的浅路径 output_path」 | `contour_task_manager.py:324` 硬编码 `Config.DOWNLOADS_DIR/"dem"`，用户输入进不来 |
| 8 | 「任务运行中点『清空缓存』会触发 cache 写盘失败分支」 | 每块瓦片都 `mkdir(parents=True, exist_ok=True)`，目录会被立刻重建，写盘仍成功；清缓存的危害是另一条独立路径（M8） |
| 9 | 「晕渲预览会把 Flask worker 占满、整站卡死」 | threading 模式（无 eventlet/gevent），实测 GDAL 释放 GIL，只占住该请求线程 |
| 10 | 「浅路径守卫在 `/` 的判定中根本不参与」 | 因果说反了：`:109` 确实先执行且确实拒了 `/`；问题是删掉它后 `:115` 会等价兜住，所以那条断言无区分力 |
| 11 | 「z≤4 全球瓦片遮蔽 base，每个 DEM 任务必然如此」 | 需要 `downloads/terrain/base_z8/` 真实存在（用户手工离线构建的可选产物）；parentUrl 为 null 时是单层 provider |
| 12 | 「按住回车会把整个 cache/ 目录清空」 | `clearCacheCategory` 只清用户点的那个分类；只有点「全部缓存」时才是整个 cache/ |
| 13 | 「`task_cleanup.py` 模块 docstring 引用 local-terrain 惯例是错的」 | 那半句没错，`local_terrain_task_manager.py:499-502` 至今保留 DOWNLOADS_DIR/terrain 边界；错的是把它当成 `remove_task_dir_if_safe` 自己的边界 |
| 14 | 「terrain 切片失败没有任何服务端记录可查」 | `cesium_terrain.py:400` 对每个失败瓦片都写了 logger.warning；真正缺的是聚合失败计数与 DB/前端感知 |
| 15 | 「hillshade 会抛 Python MemoryError」 | MEM 驱动走 C 层 VSIMalloc，失败时 DEMProcessing 返回 None → RuntimeError → 500；Linux overcommit 下更常见的是被 OOM killer 杀进程 |
| 16 | 「`_stream_copy_tile` 的 `made_dirs_lock` 跨线程争用是事件循环阻塞的主要放大器」 | 该锁只在目录未命中时包住 mkdir（每个唯一 `<z>/<x>` 一次），是二阶因素；主因始终是同步 copy2 链本身 |

另有两条**并非缺陷、不要再报**：`services/terrain_tiling/cesium_terrain.py:206` 的采样半像素偏移（上轮 M19 已实测否定，GDAL bilinear 降采样的精确逆映射即现行公式，且配了甄别测试）；`app.py` 对 Nuitka/multiprocessing 的三重进程守卫（读 Nuitka 4.1.3 的 `MainProgram.c` 与 `MultiprocessingPlugin.py` 确认成立，且比注释描述得更稳）。

---

# 覆盖度说明

| 维度 | 覆盖情况 |
| --- | --- |
| 并发与任务状态机 | 四个 manager 全文通读（task 1574 / dem 718 / contour 817 / local 514 行）+ socketio_events + dem_download_engine + 三个相关路由；download_engine 与 contour_engine 只读并发相关段。未覆盖：GDAL 拼接内部实现、cesium_terrain 全文 |
| 下载引擎与网络层 | download_engine(1431) / dem_download_engine(286) / tile_url_probe(380) / earthdata_client(123) / system_proxy(83) 全文通读 + task_manager 的复制与执行主干。实测排除 4 条假设（连接池死锁、重复复制竞态、回调击穿 gather、session 泄漏） |
| 数据库与数据完整性 | core/database(568) / models/task(298) / config_manager(362) / task_cleanup(343) / geo_validation(140) / task_manager(1573) 全文；其余 manager 与路由的 DB 路径精读。2 条结论实机验证 |
| REST API 路由层 | 7 个路由模块全文 + 支撑的 manager/cleanup/validation 段 + 前端调用点。第 1 条实跑复现 |
| 静态服务与文件系统安全 | 3 个静态路由 + task_cleanup + geo_validation + hillshade_preview 全文；delete/cache/browse 路径逐条核对；实机验证 `resolve_stored_output_dir` 的分叉 |
| 地理数据与渲染管线 | contour_engine(807) / cesium_terrain(639) / 4 个 tiler 模块 / hillshade_preview / dem_granules 全文通读 + GDAL 合成数据实测两项 |
| 前端 JavaScript | 8 个 js 文件全文通读（map 1603 / tasks 861 / history 801 / config 338 / ui 238 / panels 137 / theme 86 / path_browser 91）+ 后端契约交叉核对。上一轮 12 条前端项已逐条回验为已修 |
| CSS 与 UI 契约 | style.css 全 3071 行（剥注释后再定位/计数，避免注释干扰）+ 全部模板 + vendor bootstrap 针对性核对。**未做真实浏览器实测**，对比度与几何均按层叠规则手算（算法与 test_css_contract.py 的 `_contrast_ratio`/`_flatten` 一致）；工具条焦点圈那条做了 Playwright 等价复现 |
| 打包、启动与运行时环境 | app.py / core 五个模块 / nuitka_build(404) / 两个 build 脚本 / 两个 workflow / push-release 全文 + 读 .venv 里 Nuitka 源码验证进程守卫。**无法实际执行 Windows/macOS 构建与 CI**，相关结论由控制流推导 |
| 测试套件质量 | conftest + 13 个测试文件全文 + 7 个关键段落；test_css_contract.py(8187 行) 只读工具函数与用例清单。全量 900 passed 基线 + 逆序全量 + 101 个文件逐个单跑 + 17 处变异测试（在 /tmp 副本上做，仓库未改动）+ coverage 行覆盖统计（85%，routes/dem_api.py 55% 最低） |
| 文档与代码一致性 | CLAUDE.md / README.md / QUICKSTART / INSTALL / BUILD / 两份未跟踪设计稿全文；README API 章节与 9 个路由模块逐端点比对；DEFAULT_CONFIGS 42 项核对。未逐字精读 docs/ 下 7 份 2026-05 的历史归档、docs/packaging/(11 个)、docs/terrain/、ui-review-2026-07.md |

**测试基线**：全量 `uv run pytest tests/` = 900 passed / 0 failed / 0 skipped（186s）。

---

# 系统性结论

架构本身仍然是健康的：core 收敛配置/DB 唯一入口、manager/engine 分层、状态机全部「条件 UPDATE + rowcount」、四条管线各自的孤儿恢复、路径安全三重防护、900 个测试真实覆盖关键路径。**没有一条发现指向架构选型错误**，全部是同一批局部模式的重复实例。

上一轮（2026-07-31）总结的三个根因里，**#1「校验在一层、使用在另一层」和 #2「修复不横向同步」在本轮仍是最高频的模式**，且 0.2.4 的三项大改动（保存路径全盘可选、等高线改上传驱动、缓存不再自动淘汰）各自又生成了一批新实例。

## 模式 1：修复只做一条管线（本轮 6 个新实例，上轮根因 #2 未收敛）

四条平行管线共享同一份逻辑的复制粘贴，每次修 bug 只改被报告的那一条：

| 修复 | 已修 | 未修 |
| --- | --- | --- |
| 兜底 except 排除 `'completed'` | contour（含回归测试） | **map、dem** |
| `downloading` 残留行重新入队 + 完成判据 | dem（标为 C4） | **contour** |
| `th.start()` 失败回补状态 | map/dem 的 `start_task`（含 `_BoomThread` 测试） | **dem.start_tiling、local.start_tiling、contour.start_task** |
| 消费 `build_terrain` 的 failed 计数 | contour | **dem、local_terrain** |
| cancel 对终态任务抛 ValueError | map、dem | **contour**（U2） |
| 删除前检查后台线程 | map/dem/contour/local（上轮已修） | 缓存清理路径漏了（M8） |

**注意一个更深的现象**：这些修复往往**连回归测试一起**没有横向同步 —— `test_fix_dem_start_thread_failure.py` 用 `_BoomThread` 把契约钉死了，但只覆盖 `start_task`；`test_contour_execute.py` 的 emit-boom 测试只覆盖 contour。所以「已修」的那条管线永远是绿的，未修的三条永远没人测。**建议**：把这四条管线的共性行为（状态机转换、收尾 emit 隔离、线程启动回补、计数消费）抽成参数化测试表 `(manager_cls, table, prefix)`，新管线加一行；比抽共享基类风险小、收益立竿见影。

## 模式 2：失败被降级成日志，状态照报成功

本轮最普遍、也最危险的模式 —— 至少 9 处：

- 缓存写盘失败 → warning，瓦片仍报 completed（H2，且下游还有两次机会也只 warning）
- terrain 切片 failed 计数被丢弃 → job 无条件 completed，layer.json 还过度声明 available（M11）
- 等高线缺 granule → 渲染出缺口，任务 completed（L1）
- DEM skipped 不计数 → 终态 `downloaded+failed < total`（M7）
- `remove_task_dir_if_safe` 拒删 → 返回值被四个调用点全部丢弃，接口 200 success（M10）
- `rmtree(..., ignore_errors=True)` 吞掉 Windows 文件锁失败（L4）
- 清缓存后产物缺瓦片 → 三处 warning，任务 completed（M8）
- 进度 emit 抛异常反过来把成功任务改判 failed（M1，同一根因的反向表现：不隔离）

共同结构：**错误信息只进日志，DB 状态与 HTTP 响应一律「成功」**，用户没有任何察觉途径，且多数无法原地自愈（completed 任务不能重启）。**建议**建立一条硬约定：任何「本该产出而没产出」的事件必须至少落到 DB 的一个计数列或状态位上，日志只能是补充；review 时把 `except ... : logger.warning` 当成需要举证的写法。

## 模式 3：判据太弱 —— 用「存在且非空 / 名字前缀」当作完成或归属的证据

- 拼接短路：`exists() && size > 0` → 半截 tif 被当完成（M2）
- 缓存命中：`size > 0` → 200 的 HTML 错误页被当瓦片，永久污染（M5）
- 启动清扫：纯文件名前缀 → 删掉活进程的工作目录（H3）
- 删除守卫：靠路径形状（深度/是否等于某目录）而不是归属登记（M22、H4 的连带风险）

对照组就在同一个文件里：`_add_georeference` 用 `.part + os.replace` 解决过完全相同的问题两次并写了注释解释理由；瓦片复制短路用的是「dest 存在**且大小与源一致**」。**建议**统一到「原子写 + 归属标记」：写盘一律 `.part.<pid>` → `os.replace`；临时目录名里带 pid（现在其实已经带了，只是清扫没用）；清扫按 `pid_alive` + mtime 双条件。

## 模式 4：同一份数据三套解析口径（上轮根因 #1 的延续，且被 0.2.4 放大）

- `output_path`：写侧 `resolve_stored_output_dir`（拼 DOWNLOADS_DIR）/ 读侧 `_resolve_config_path`（前缀剥离）/ dem+contour 删除侧裸 `Path()`（按 CWD）—— **三套**（M10）
- `default_save_path`：`init_database` 归一 vs `reset_to_defaults` 不归一 vs `validate_config` 拒绝该值 —— 写入路径与校验路径互相矛盾（M6）
- 代理：验证/测速 bypass 内网，下载不 bypass（M4）
- `delete_files` 默认值：三条管线 false、local_terrain true
- `PUT /api/config`：路由的逐键过滤反转了 `set_many` 自己声明并测试过的「全或无」语义（M9）

上轮的建议「规范化后入库、下游只用规范值」对新建任务已经做到了（38e3e30fc 起 create_task 存绝对路径），但**存量行从未被归一**，于是解析歧义被永久保留在数据里。**建议**在 `init_database()` 里对四张任务表的相对 `output_path` 做一次性幂等归一（与 `default_save_path` 同样的做法，同样用 `PRAGMA user_version` 标记），此后全链路只允许绝对值，删掉所有相对路径解析分支。

## 模式 5：全项目缺进程级互斥

启动清扫、四条管线的孤儿恢复、SQLite 写入都隐式假设单实例，但 grep 全库没有任何 pid 文件 / mutex / flock / 启动前端口探测。`app.py:148-158` 的 docstring 自己描述过「第二次跑 create_app 会污染正在 running 的任务」这个危害，但只对 multiprocessing worker 加了守卫，没覆盖「用户双击第二个 exe」。而 `docs/packaging/DISTRIBUTION.md:59` 写的是「端口 5000 已被占用时应用将无法启动」—— 把第二次启动的后果描述成「起不来而已」，正是被 H3 推翻的那个假设。这一条修掉可以同时消掉 H3 的生产触发面和孤儿恢复误判两个问题。

## 模式 6：测试的「绿」不等于「守住了」

- 断言不具区分力：浅路径守卫删掉全绿（M22）、`/tiles` 穿越用例是空断言（U13）
- 整层零覆盖：四条管线的 start/resume HTTP 端点一次都没被请求过（M21）
- 契约测试靠手写白名单：`_text_contexts()` 17 条、`BUTTON_CONTEXTS` 11 条，都用 `assert len(...) == N` 钉死条数，**新增代码天然逃逸**（M16、M17、M18 三条无障碍问题全部是这样逃出去的，且都是上轮 A7/Task 12 确立规则之后的新增回归）
- 测试本身具破坏性：真删 HOME（H4）、删真实 `/tmp` 工作目录（H3）
- 隔离设施退化：47 份互不相同、46 份从不恢复的 pop 清单（M23）

**建议**把「断言必须能被变异测试翻红」写进测试规约；契约测试的上下文清单改成「扫描 CSS/模板自动发现 + 显式豁免名单」而不是手写白名单，否则每次新增 UI 都要靠人记得去改那个 17。

## 模式 7：三次行为变更都改了代码没改文档

0.2.4 的三项改动各留下一批过时描述：保存路径全盘可选 → 6 处注释仍说 DOWNLOADS_DIR 边界（L8）；等高线改上传驱动 → README 整节流程不存在 + 水体掩膜声称可配（M24、M26）；DEM 默认换 Copernicus → README 四处仍说必须 Earthdata（M25）。其中 README 是**本次未提交改动里被整体重写的文件**（383 增 345 删），等于新写了一段过时内容 —— 说明文档更新目前不在改动的检查清单里。**建议**把「行为变更必须同步 README/CLAUDE.md 对应段落」加进提交前检查，或至少在 CLAUDE.md 里为每条管线标注「事实源在哪个文件的哪一段」，让下次审查能机械比对。

---

# 修复优先级建议

1. **H1**（等高线坐标公式，一行改动 + 一条用例）—— 唯一在**静默产出错误地理数据**的问题，且修复成本最低
2. **H4**（测试真删家目录，一行 monkeypatch）与 **H3 的测试侧**（conftest patch gettempdir）—— 修复成本极低但当前每次跑 pytest 都在赌
3. **H2 + M5 + M8**（缓存这条链上的三个静默失败：写盘失败仍 completed、非图片被当瓦片、清缓存不检查活动任务）—— 同一子系统，一起改口径最省
4. **H3 的生产侧**（进程级互斥）—— 一并消掉孤儿恢复误判
5. **M1 / L1 / L2 / M11 / U2**（横向同步那五条）—— 建议连参数化测试表一起做，避免第三次重复
6. **M2**（拼接原子写）、**M6**（reset 配置）、**M19**（发布包卫生）—— 各自独立、改动小、用户可见
7. **M10 + L8**（路径口径收敛 + 注释对齐）—— 需要一次性存量归一，建议单独一个 PR
8. **M16-M18**（三条无障碍）与 **M21-M23**（测试设施）—— 建议连同「契约测试白名单改成自动发现」一起做。注意 M17 的令牌取值要实算（原稿给的亮色档不达标）、U8 与 M23 各自会打红一条 `assert len(...) == N`
9. 文档收尾：**M26 的 `CLAUDE.md:12` `water mask`**（M26 唯一未闭环处，AI 速查区传染性最强）与 **M24 残留的「处理类型」入口缺失**；M24/M25 主体已在 `1cd918598` 修完
10. 其余未列入上述分组的条目（M3/M4/M7/M9/M12-M15/M20、L3-L7、U1/U3-U8/U10-U14）按各自条目内的改法处理；**U9 已推翻，不要修**

---

# 修复闭环（2026-08-03/04）

**全部 51 条真问题已处理完毕。** 测试从 900 → **962 passed**（+62，全绿；正序与
逆序各跑一遍稳定）。以下只记录**偏离原建议**的地方和需要后人知情的取舍 ——
按原建议直接落地的条目不再重复。

## 关键修复的验证方式

高危项一律做了**变异验证**（把修复改回坏写法，确认新测试翻红），而不是只看「加了
断言且是绿的」：

| 条目 | 变异注入 | 结果 |
| --- | --- | --- |
| H1 等高线坐标 | 偏移项改回 `eff_px_w` | 新用例翻红 |
| H4 / M22 删除守卫 | 分别废掉浅路径守卫、家目录守卫 | 各自翻红，且 rmtree 被 spy 拦住、不再真删 |
| M1 终态保护 | 三条管线各自去掉 `'completed'` | 各自翻红 |
| M6 reset 归一 | 去掉 reset 里的归一调用 | 两条用例翻红 |
| M16/M17 无障碍 | 改回弱边框 / 正 outline-offset / 用审核指出的 `#9ca3af` | 三重变异全部翻红 |
| M18 muted 当文字色 | `.bounds-hint` 改回 muted | 新增上下文翻红 |
| U13 `/tiles` 穿越 | 拆掉 `_is_within` 守卫 | 旧断言会全绿，新断言翻红 |
| M23 隔离设施 | 去掉注入型全局的恢复 | 自检用例翻红 |

## 与原建议不同的三处

1. **U9 未修 —— 它是误报**（见误报清单）。照原建议往 style.css 补
   `@keyframes spin` 反而会与 `map.js:1077` 运行时注入的那份重复定义。

2. **U8 只做了一半，且是刻意的**：`.btn-outline-danger` 的五态已补全
   （focus-visible + active），但**没有**加进 `BUTTON_CONTEXTS`。入册会撞上一个
   数学上无解的约束 —— 暗色档启用墨 `--color-danger`(#f87171) 相对亮度 0.3296：
   - `BTN_DISABLED_MIN_INK_DIMMING` 要求禁用墨 L ≤ 0.3296 − 0.15 = **0.1796**；
   - `BTN_INK_MIN_CONTRAST` 要求禁用墨对按钮底 #1c2027 达 4.5:1，即 L ≥ **0.2392**。

   区间为空，选什么禁用墨都必红。要入册得先决定「调暗危险色本身」还是「放宽某条
   阈值」，那是产品/视觉决策，不该塞进一次 bug 修复。已把这段推导写进
   `tests/test_css_contract.py` 的 `TRANSPARENT_BTN_VARIANTS` 上方，避免后人
   照着 U8 的原文直接往元组里加。

3. **M12 只落地了「首选」那一条**（available 只声明与 DEM 真正相交的瓦片），
   没做「越界采样置 NaN」。理由见 M12 条目内的订正：后者单独实施不解决遮蔽，
   且置 NaN 后仍会被 `np.where(isnan, 0.0, v)` 落成 0；而收窄 available 单独就
   足以消除遮蔽，同时**不会**触发「base 不存在时地形完全不加载」那个新故障
   （低 zoom 照常出图，只是不再声明可用）。

## 顺带修掉的两处（测试逼出来的，不在原报告里）

- `local_terrain_task_manager._mark_failed` 的 UPDATE **没有任何状态守卫**。
  新增的管线一致性测试把它揪了出来；虽然唯一调用点在 create 阶段、当时任务
  必是 pending，仍补上了 `AND status='pending'`，让「置 failed 的 UPDATE 一律
  不得改写终态记录」这条约定在四条管线里没有例外。
- 同名方法遮蔽：L2 的回补方法起初也叫 `_mark_failed`，直接把上面那个覆盖掉了
  （测试立刻红）。已改名 `_mark_running_task_failed`。

## 已知未闭环

- **M13 的 BuildVRT 并集范围问题仍在**：内存已经从「按满幅分配 1.2 GB」降到
  「按 1600px 缩略图分配」，但预览图在输入稀疏时绝大部分仍是 nodata 空白。
  要治需按各输入实际范围裁剪或分别出图 —— 那是功能改动，不是本次修复范围。

> **M23 已于 2026-08-04 完全闭环**，存量清零，棘轮名单为空。见下节。

## 补记：M23 的账（2026-08-04）—— 已复现、已修、规模比原判小一个量级

首轮闭环把 M23 记为「潜伏，逐个改写 47 个文件属纯机械重构，留给单独提交」。
2026-08-04 复核推翻了「潜伏」这半句，也推翻了「要改 47 个文件」这半句。

**先说结论**：按**文件级逆序**跑全量（`ls tests/test_*.py | tac` 交给 pytest），
`test_download_engine.py` 的两条 stop_flag 用例**必失败**。已修，正序与文件级
逆序现在都是 **968 passed**。上文「正序/逆序各跑一遍稳定」指的是另一种逆序跑法，
不覆盖文件级逆序。这不是那一轮的新回归 —— 在 `aaeb8d3a9` 的 HEAD 上隔离验证过，
基线同样失败。

**根因**（诊断到类对象 id 层面，非推断）：`test_fix_release_hygiene.py` 裸 pop
`services.download_engine` 且从不恢复。`test_download_engine.py` 顶部的模块级
`from services.download_engine import DownloadEngine` 在 collect 期就把名字绑死
在**旧模块 A** 上，其方法 `download_tile` 的 `__globals__` 里 `DownloadCancelled`
也是 A 的那份；而测试函数体内 `from services.download_engine import
DownloadCancelled` 拿到的是 sys.modules 当前的**新模块 B**。实测
`raises() 捕获的类 id=1029653408` / `download_tile 实际 raise 的类 id=1027048528`
/ `is` 为 False —— `pytest.raises(B)` 捕不住 A 的异常，异常穿透。正序下三者同一，
所以只在特定文件顺序下显形。

> **取证方法记一笔**：第一次诊断用 `sys.modules[type(obj).__module__]` 取「实例
> 所属模块」，那是错的 —— `__module__` 只是字符串，查 sys.modules 拿到的仍是
> **当前**那份，三个 id 看着一致，把结论引向了错误方向。正确取法是
> `obj.method.__func__.__globals__['Name']`：函数体内的名字解析走的是它自己的
> `__globals__`，那才是「代码实际引用的那一份」。

**规模比原判小一个量级**。AST 扫全库（61 处裸 pop，其中 51 处是 `for mod in
[...]` 的清单式写法，正则会漏）后：被 pop 的模块共 14 个，但
`app`（45 个文件）和 `core.database`（46 个文件）—— 也就是那「47 份清单」的绝大
部分 —— **没有任何测试文件在模块级 from-import**（项目规约要求先 monkeypatch
Config 再在函数内 import），本来就无害。真正危险的组合只有 5 个模块，其中实际
引爆的只有 1 个（只有它涉及 `pytest.raises` 的类身份比较；另外 4 个实测跑过，
全绿）。**所以不需要改 47 个文件，改 1 个即可。**

**试过一个更"彻底"的方案，失败了，值得记下来别再走**：在 conftest 加 autouse
fixture，在每个测试 teardown 时把项目模块的 sys.modules 恢复成测试前的快照。
第一版打红 1 条（恢复了模块身份却没恢复注入型全局，造出
`app.task_manager is not routes.api.task_manager` 的新不一致）；补上注入型全局的
恢复后**打红 15 条**，集中在走 app 的 HTTP 用例。原因是根本性的：**现有 965 个
测试是建立在「裸 pop 不恢复」这个既成事实上的** —— 强制全局恢复等于改变了所有
测试的运行前提，回归面覆盖整个套件，正是原报告预警过的那个风险。收益（挡住 4 个
不引爆的潜伏项）与代价完全不匹配，已撤回。

**最终落地**：
1. `test_fix_release_hygiene.py` 的 3 处裸 pop 改用 `conftest.fresh_import`
   （`services.download_engine` ×2、`services.config_manager` ×1）。
2. `tests/test_conftest_isolation_contract.py` 新增三条契约：
   - **静态棘轮** `test_no_new_module_double_instance_risk`：AST 扫描全库，
     「裸 pop 的模块 ∩ 别处模块级 from-import 的模块」不得超出 KNOWN 名单
     （现存 4 个：`models.task` / `services.config_manager` /
     `services.contour_task_manager` / `services.dem_download_engine`）。
     新增一个就翻红，并在断言消息里直接给出改用 `fresh_import` 的指引。
   - **棘轮另一侧** `test_known_risk_list_has_no_stale_entries`：修掉存量后
     必须同步从 KNOWN 移除，否则棘轮会悄悄松掉。
   - **真实场景** `test_release_hygiene_followed_by_download_engine_stays_green`：
     子进程按「逆序会产生的那个顺序」跑那两个文件，断言退出码为 0。必须用子
     进程 —— 模块身份问题只在全新解释器 + 特定文件顺序下显形。
3. 变异验证：把 `test_fix_release_hygiene.py` 改回裸 pop，静态棘轮与真实场景
   两条**各自独立翻红**。

### 存量清零（同日续做）

剩下那 4 个模块（`models.task` / `services.config_manager` /
`services.contour_task_manager` / `services.dem_download_engine`）也清掉了，
**棘轮名单现在是空集**。正序与文件级逆序全量均 **968 passed**。

清法**不是**把 27 处裸 pop 迁到 `fresh_import`，而是**直接删掉多余的 pop 项**。
依据：AST 检查确认这四个模块的**模块级语句、类体直接语句、装饰器**都不捕获
`Config` 的值 —— 它们只有 `from core.config import Config`（引用类本身，而
monkeypatch 是 `setattr` 打在类上，对所有引用可见）。既然重导入不会改变任何
行为，pop 它们就是多余的：删掉之后，测试函数里的 `import_module(...)` 拿到
全局那一份，运行时照样读到 monkeypatch 后的 Config。实测 21 个文件、27 项
删除后，受影响文件 250 passed，全量两个方向都 968。

**为什么不用 `fresh_import`**：它会给这些文件新增「teardown 恢复」语义 ——
正是上面记的那个打红 15 条的机制。删多余项则完全不碰恢复语义，回归面最小。
判据可以一般化：**pop 一个模块只有在它「模块级捕获了会被 monkeypatch 改动的
状态」时才有意义**；否则那次 pop 除了制造第二份类对象之外不产生任何效果。

棘轮在空集状态下仍有区分力：变异注入一处
`sys.modules.pop("services.contour_task_manager")` 后
`test_no_new_module_double_instance_risk` 立即翻红并点名该模块。

## 补记：M3 的另一半（2026-08-04）

首轮闭环只挪走了 M3 的主要阻塞源（回调里的瓦片复制 → `asyncio.to_thread`），
**同一个回调里的 `flush_progress_counts` 仍在下载事件循环上同步做 sqlite
executemany + commit** —— M3 条目的「范围说明」点破过这一点（「改完 copy 后
事件循环仍不干净」），但改法只列了 copy，收尾时也没记进本节。现已补完：

- `core/database.py` 的 `get_connection()` 新增 `check_same_thread` 形参（默认
  `True` 不变）。唯一的 `False` 使用者是 `progress_conn`。
- flush 拆成三段：`_drain_progress_batch()`（**留在事件循环上**原子摘批）、
  `_write_progress_batch()`（纯 IO，走 `asyncio.to_thread`）、
  `_restore_progress_batch()`（写盘失败退回队列）。批次 flush 走新的
  `flush_progress_async()`，靠 `flush_in_flight` 标志串行化；下载循环收尾
  （finally）那次仍是同步版 —— 那时已无并发回调，异常路径上不该再引入挂起点。

**摘批为什么必须留在事件循环上**：若让工作线程直接读那三个待写列表，
`executemany` 执行期间 sqlite3 会释放 GIL，回调此刻 append 进来的新登记会被
随后的 `clear()` 一起抹掉 —— 失败瓦片静默丢记录，完成判定的 `failed_count>0`
就守不住（正是本报告「模式 2」那类静默失败）。

三条新用例（`tests/test_fix_progress_flush_offloading.py`）各自做过变异验证：

| 变异注入 | 结果 |
| --- | --- |
| 批次 flush 改回事件循环上同步调用 | 「flush 跑在工作线程」翻红（4 次全落在事件循环线程） |
| 摘批不原子（把活列表交给工作线程，写完再 clear） | 「并发不丢失败行」翻红 |
| 写盘失败不退回队列 | 「失败批次由后续 flush 补上」翻红（708 → 508 行） |

**第二条变异验证踩了一个坑，值得记下来**：最初的写法测不出东西 —— 708 个上报
协程之间没有 await 点，事件循环会在 `to_thread` 的调度间隙把它们一口气跑完，
`executemany` 迭代的是活列表、开始时早已拿到全量，`clear()` 抹掉的全是已落库
的行，坏实现碰巧无害。要让新登记落进 `executemany` 正在执行的那几毫秒，上报
必须**分散在时间轴上**（用递增 `asyncio.sleep` 复现真实的网络 IO 到达分布），
并且撑窗口的延迟要放在 SQL **执行之后**而非之前。这两点写进了测试文件的注释。

测试 962 → **965 passed**（正序）。`get_connection` 加形参连带打断了两处无参的
测试替身（`test_sparse_task_tiles.py`、`test_fix_map_low_review.py`），已改为
透传 `*args/**kwargs`。
