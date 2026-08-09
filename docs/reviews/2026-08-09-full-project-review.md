# 全项目评审 · 2026-08-09

> 状态：本文是**快照**，代码为准。基线 commit `e9af408`（v0.2.14），工作区干净。
> 测试基线：`python -m pytest tests -q` → **1963 passed, 3 skipped, 105.81s**（本次实跑）。
> 方法：15 个并行子代理按文件归属分片（互不重叠）+ 主评审逐条复核 + **真实浏览器驱动**。
> 最后一项是上一份评审「本次未评审」里点名的缺口（`2026-08-08-full-project-review.md:512`
> 「所有 UI 结论来自源码 + 一次 Flask test client 渲染，未观察真实 tab 序列」），本次补上了。
> 带「实测」的条目是**跑出来**的；只读得出的条目在原始分片报告里标着 `verified_by: read-only`。

**基线对比**：上一份（2026-08-08）的 4 条 P0 + 17 条 P1 + 全部 P2 当日全部修完，本次逐条复核
**没有一条回归**（复核证据见「上一轮修复的复查」一节）。此后 51 个提交、约 7300 行源码改动，
本次是在那之上的新一轮。


## 修复进度（2026-08-09 当日）

**按本文末尾「建议修复顺序」的 7 项全部修完，另加 1 项。** 全量测试
**1963 → 2035 passed / 3 skipped**（净增 72 条行为用例）。每一条都做了变异校验 ——
把实现换回旧写法，新用例必须变红；本轮逐个跑过的变异 **40+ 个，零存活**。

| 顺序项 | 落点 | 变异校验 |
|---|---|---|
| 1. 两条 P0 | **T2**：`task_manager` 拼接段加「本层还有失败瓦片就整层不拼」闸门；短路判据从「文件在且非空」收紧为「文件在 **且本轮这一层没下到新瓦片**」（补齐的那块必然落在后者，于是旧版本留下的残缺 mosaic 会被重拼，而纯重跑仍然短路）。**T1**：新增 `well_covered_tile_range` / `_MIN_TILE_AXIS_COVERAGE`，`available` 从「与 DEM 相交」收紧为「单轴覆盖率 ≥ 25%」 | 5/5 抓住（T1 判据、T1 调用点、T2 失败闸门、T2 短路判据、flush 判定） |
| 2. 搁死补偿两个洞 | `_STRANDED_TASK_TABLES` 从 frozenset 改成带定位列的表，纳入 `dem_terrain_jobs`（按 `task_id`）与 `local_terrain_tasks`；两个 `_run_tiling_job` 的 `finally` 接上补偿。白名单那条被证伪的注释改写 | 7/7 抓住 |
| 3. `~` 展开三种解释 | `_validate_scratch_dir` 去掉 `.expanduser()`，与三个读取侧逐字同口径 | 抓住（回填 `.expanduser()` → 10 条红） |
| 4. 配置页那条反话 | `tpl.config.download.basemap_hint` 中英两版重写；`test_docs_claims.py` 加反向闸门，i18n 目录里不许再出现「底图 + 绕过代理」同句 | 2/2 抓住 |
| 5. 前端四至收敛 | 新增 `validateBoundsRules`，三个入口全部改走它；删掉 `_wrapLngEast/_wrapLngWest`、瓦片预估里不可达的「后端会拒绝」分支、两处静默 swap，以及三处已成假话的注释 | 11/11 抓住（含 9801 格与后端 `validate_bbox` 逐格对拍） |
| 6. 测试网四个洞 | `test_terrain_stage_progress.py` 删掉手抄副本、改从真管理器捕获真闭包；`/api/config/reset`、本地地形线程起不来、`/api/config/proxy_status` 各补用例 | 7/7 抓住（此前 4/4 存活） |
| 7. 治理四条 | `check_gdal.py` 接进两个 workflow 的每个构建 job；`fail-fast: false`；底图瓦片 URL 加源标识 `?v=<hash>` + 长缓存判据改用实时源；`CLAUDE.md` 三角化/法线与 WGS-84 两节重写，另补离线、CSS、日志三节 | 各自抓住 |
| **+1（顺序外）** | `panels.js` 的 Esc/Tab 让位判据提到分支之前，且改用 `body.modal-open`（Bootstrap 的 `hide()` 同步摘 `.show`，冒泡到 document 时它已不匹配 —— 用 `.modal.show` 实测无效） | 抓住 |

**三处子代理的判断被复核后采纳或推翻：**

1. **`~` 的拒绝文案没做，理由成立。** 这两个键在界面上根本没有输入框（`grep` 模板/静态/i18n 目录零命中），只能经 `PUT /api/config` 或改库设置 —— 没有「对用户说的话」这个落点。要挂按键定制提示得改 `_VALUE_RULES` 的 bool 协议（波及 25 个键），不划算。
2. **`Create Release` 没有挪进汇总 job，理由成立。** 紧挨发版做这个改动要重验三平台。残余风险写进了 workflow 注释：`fail-fast: false` 之后失败平台可以单独重跑补齐。
3. **`DocsInvariants` 推翻了本文对 `cesiumjs-loading.md` §3 的处方，且它是对的。** 本文让它照抄兄弟 README 的「默认路径已消解、兜底路径依旧」状态头；而 T1 修完之后 `build_terrain` 在**两条路径**上都按覆盖率收窄声明，兜底路径同样会回落到 `parentUrl`。它按代码现状重写，并顺手改掉了兄弟 README 里同一句假话。

**浏览器复验（改前改后各一遍，同一套无头 Chromium）：** 首页 102 个请求全部同源、55 张底图瓦片全部带 `?v=`、零远程请求、零 console error；`east=400` / 跨反经线 / 零面积三种输入改前全部被收下（状态栏还显示过 `-340.000°` 的负宽度），改后全部被中文文案拒绝；弹窗上按一次 Esc 改前连面板一起关，改后只关弹窗、第二次才关面板，而单独开着面板时第一次 Esc 照常关闭。

## 结论

**两条 P0，都是「静默产出错数据」——本仓反复栽的同一类，也是上一轮 P0 的同一类。**
两条都不是新写的代码写错了，而是**旧闸门的判据不够强**：一条把「有交集」当成「有数据」，
一条把「文件在」当成「文件对」。

| P0 | 一句话 | 用户看到什么 |
|---|---|---|
| **T1** 边界瓦片几乎全是外推假地形，却被声明 `available` | `intersecting_tile_range` 只保证瓦片与 DEM **有交集**，交集可以只有一个像素；采样落到 DEM 外走 `np.clip` 钳位成台地 | 小范围 AOI（本地地形上传的常见规模）在 z8 那层得到两块 78×78 km 的假台地，高差可达数千米。HTTP 全 200、作业 completed、日志零告警 |
| **T2** 部分下载失败仍照常拼接，恢复时「文件在就跳过」把残缺 mosaic 当成品 | 拼接段无「本层还有失败瓦片就别拼」的闸门；短路判据只看 `exists() and size>0` | GeoTIFF 打得开、看着正常，地理范围比选区小或内部有洞；任务 completed、`error_message` 为 NULL。唯一发现途径是自己拿 GIS 去量四至 |

**26 条 P1 全部落在六个模式上，没有一条是孤立的**（分节详情见下）：

| 模式 | 条数 | 最贵的那条 |
|---|---|---|
| 一份规则多处实现，其中一处没跟上 | 4 | `~` 展开：配置校验侧 `expanduser`，三个读取侧都不 —— 同一个存量值有三种解释，配 `contour_warp_tmpdir=~/warp` 保存时 200、每个等高线任务必炸 |
| 静默丢数据 / 静默错声明 | 6 | 404 颗粒计进「已下载」且永不重试：任务报 100% 成功，产物目录可以是空的 |
| 文档不变量已过期 | 5 | `CLAUDE.md` 的三角化/法线一节三条全反 —— 而它是本仓不变量的唯一事实源 |
| 界面对用户说的话是反的 | 4 | 配置页当面告诉用户「底图不经过代理设置」，而 0.2.12 起底图恰恰吃 `proxy_url`；「底图打不开」是本项目最高频的现场问题 |
| 测试网破洞 | 4 | 三条用例断言的是测试文件里手抄的副本，4/4 变异存活 |
| 搁死补偿网有洞，而注释断言洞不存在 | 3 | `task_cleanup._STRANDED_TASK_TABLES` 的注释说 `local_terrain_tasks` 与 `dem_terrain_jobs`「自己有兜底 except」——两个分片各自独立地把这句话证伪了 |

**测试套件第一次做了变异实测，结果不好看**：4 个针对生产闭包的变异 **4/4 全部存活**
（`tests/test_terrain_stage_progress.py` 断言的是测试文件里手抄的一份副本）。
另有三个端点/回补路径零测试，把实现整个掏空仍然全绿。

**安全姿态：本次零新发现，也未复审**——2026-08-08 用户裁定「不用考虑安全，系统会运行在
可信的环境中」，本次按该裁定把安全整体划出范围，只在「校验与读取口径不一致」这类
**功能正确性**问题上出报告。上一份记录的「部署前提待重签」仍然待办。

**两条产品硬约束实测完好，且是被机器守住的**：离线（浏览器实测 101 个请求**全部同源**，
零远程 fetch）与 WGS-84 底图（55 张瓦片全部走同源 `/basemap/{z}/{x}/{y}`）。

## P0

### T1 边界瓦片几乎全是外推假地形，却被声明 `available`

`src/services/terrain_tiling/cesiumlab_terrain.py:1432（available 取交集）+ :754-755（np.clip 钳位外推）+ src/services/terrain_tiling/dem_task_tiler.py:154（min_level=8）`

`intersecting_tile_range` 只保证瓦片与 DEM **有交集**，交集可以只有一个源像素。`DemSampler.sample` 对落在源栅格之外的采样点走 `ix = np.clip(np.floor(lpx), 0, arr.shape[1]-2)`（:754-755），即把最外一圈源像素向外无限延伸成台地——不是 nodata、不是 0。而 :1432 的 `a0,a1,b0,b1 = max(x0,ix0)...` 把这些瓦片原样写进 available（:1584）。生产 `min_level=8`，z8 一片跨 0.703°≈78 km，所以任何小于 78 km 的 AOI 在它最浅的那一层都会得到一张几乎全是假高原的瓦片，并被声明为可用。dem_task_tiler.py:149-150 的注释「底图独占 z0-z7，任务只出 z8+…也没有『半张瓦片是真数据、半张是采到 DEM 外的外推值』那种接缝」与实测相反。M12 收窄 z<=4 的 available 修的正是同一个机制（注释里就写着「被边缘像素钳位拉成台地」），但只修了零重叠与 z<=4，部分重叠这一半没修。

**后果**：用户上传/下载一小块 DEM（0.05°≈5.5 km 见方是本地地形上传的常见规模）后，z8 那张瓦片 95.4% 的顶点取的是 DEM 东/西边缘的钳位值，在 Cesium 里就是两块 78×78 km 的巨型假台地，高差可达数千米。底图只到 z7（docs/reference/terrain/global-base-build.md「为什么是 z0–7 而目录仍叫 base_z8」），z8 没有任何回落，这张假瓦片就是该层唯一数据源。全程 HTTP 200、作业 completed、日志零告警——本仓反复栽的那类静默错数据。

**修法**：两条路，都要动 available 与采样二选一：①（推荐，改动小）在 `_tile_ranges` 里额外算每张瓦片与 `sampler.bounds` 的**面积覆盖率**，低于阈值（例如 5%）的瓦片仍出图但不写进 available，让 Cesium 拿父层（底图 z7）上采样——注意 Cesium 的 availability 链必须连续，所以阈值要按层递减、保证每层至少留下覆盖 AOI 的那几张；②（彻底）浅层瓦片的 DEM 外区域改从已植入的底图瓦片取值再与真实 DEM 拼接，而不是钳位。无论哪条，都应在 counts 里回报「外推面积占比」并在切片结束日志/UI 上出声，因为今天这条链完全无信号。

> 验证：实跑（/tmp 合成 GeoTIFF + DemSampler）：DEM bounds=(100.0,38.95,100.05,39.0)（0.05° 见方，500→3500 m 东西向斜坡）；`intersecting_tile_range` z8 → (398,398,183,183)；tile 398/183 extent=(99.8438,38.6719,100.5469,39.3750)，**real-DEM area fraction = 0.51%**，4225 个顶点里只有 **16 个**落在真实 DEM 内；瓦片高程范围 810.9..2905.0 m，出现频次最高的两个值是 2905.0（3055 次，72.3%）与 810.9（975 次，23.1%）——即 95.4% 的顶点等于某一侧边缘的钳位值；中间行剖面 [810.9 810.9 1670. 2905. 2905. 2905. 2905. 2905. 2905.]。另一组 1°×1° DEM 的 z8 边界瓦片真实覆盖率分别为 41.5% / 57.3% / 34.4%。

### T2 部分下载失败仍照常拼接，恢复时「文件在就跳过」把残缺 mosaic 当成品

`src/services/task_manager.py:1487`

拼接段（:1464-1545）在下载结束后**无条件**执行，没有任何「本层还有失败瓦片就别拼」的闸门；:1468 `zoom_levels = sorted(set(tile.zoom for tile in completed_tiles))`、:1528 `tiles=completed_tiles`，而 completed_tiles 只含 cache 命中 + 本轮下载成功的瓦片 —— 失败瓦片直接不在里面，于是拼出来的 mosaic 覆盖范围比任务 bbox 小。第二轮（暂停/崩溃后恢复）走到 :1487 `if output_path.exists() and output_path.stat().st_size > 0:` → `stitched_zooms.append(zoom); continue`，把上一轮那张残缺图当成完成品保留。:1482-1486 的注释只承认「Translate 写盘中途被杀留下非空半成品」这一种风险，没有承认「用一个不完整的瓦片集合拼出来的完整文件」。引擎侧也拦不住：`_assert_vrt_covers_tile_grid` 的期望值由 :1343-1346 `min/max(t.x for t in tiles_at_zoom)` 推出，即**它收到的那批瓦片**，边缘瓦片缺失时 bbox 一起缩小，actual==expected，结构上不可能发现「任务网格少了瓦片」。

**后果**：选 both / image_only 的任务，拼接图是用户要的主产物。链路：一块瓦片瞬时 500 → 拼接照跑（少一块）→ 用户在拼接/拷贝阶段点暂停（或进程被杀后孤儿恢复翻 paused）→ 恢复 → 缺的瓦片这次下成功、失败行被清、拼接被短路跳过 → 任务 completed、error_message 为 NULL。产物是一张地理范围比用户选区小（或内部有洞）的 GeoTIFF，文件打得开、看着正常，任务侧零信号。唯一发现途径是自己拿 GIS 去量四至。

**修法**：短路判据要能证明「这张图是用完整瓦片集合拼的」，不能只看文件在不在：(a) 拼接前查 `task_tiles` 该 zoom 是否还有 failed 行，有就整层跳过拼接（推迟到某一轮该层齐全时再拼），这同时也堵住「拼出残缺图」的源头；(b) 短路时把期望网格（x_min/x_max/y_min/y_max 或瓦片数）与产物实际尺寸对一次，不匹配就重拼。(a) 更便宜且一处解决两个问题。

> 验证：驱动真实 _execute_task 两轮（probe1，tmp sqlite + 打桩 download/stitch）：`full grid at zoom 10 = 12 tiles` / `run1: status(after pause)=paused failed_rows=1 stitch_calls=[('run1', 10, 11, 'A_zoom_10.tif')]` / `run2: status=completed error_message=None failed_rows=0` / `run2: stitch_calls=[('run1', 10, 11, 'A_zoom_10.tif')]   <-- no second call` / `mosaic on disk: A_zoom_10.tif -> b'MOSAIC-FROM-11-TILES'`。即 12 块的网格，盘上留着一张 11 块拼的图，任务报 completed。


## P1（26 条，按模式归类）

### 一份规则多处实现，其中一处没跟上

**`~` 展开：校验侧展开，三个读取侧都不展开** — `src/services/config_manager.py:103`

`_validate_scratch_dir` 的判据是 `Path(raw).expanduser().is_absolute()`（config_manager.py:103），所以 `~/tf_warp` 被判合法并原样入库（`set()` 不做归一，存的就是 `'~/tf_warp'`）。三个消费者一个都不 expanduser：
1. `src/services/download_engine.py:1054` `os.makedirs(stitch_tmp_base, exist_ok=True)` + `:1055` `tempfile.mkdtemp(dir=stitch_tmp_base)` —— 字面量 `~/...` 按进程 CWD 解析；
2. `src/services/contour_engine.py:679` `tempfile.mkdtemp(prefix="contour_warp_", dir=warp_tmp_base)` —— 该行不在任何 try 内（try 只包 :673 的 ConfigManager().get），直接抛 FileNotFoundError；
3. 启动清扫 `src/services/task_cleanup.py:747-750`（warp）与 `:762-767`（stitch）用 `Path(warp_base)` / `Path(stitch_base)`，同样按 CWD 解析。
讽刺的是这个键要求绝对路径的**唯一理由**（docstring config_manager.py:94-101）就是「相对值会被 download_engine 按进程 CWD 解析，打包 exe 从快捷方式启动时中间产物会落到谁也想不到的地方」—— expanduser 恰好在 `~` 这一类上把这道闸打穿了。

后果：配 `contour_warp_tmpdir=~/warp`：每一个等高线任务在 warp 阶段抛 FileNotFoundError 任务失败，而配置页保存时是 200。配 `stitch_tmpdir=~/scratch`：GB 级拼接中间产物写进 `<CWD>/~/scratch/`（冻结 exe 的 CWD 是用户双击时所在的任意目录），而清扫根是第三种解释，残留回收不可靠。

修法：`_validate_scratch_dir` 去掉 `.expanduser()`（改成 `Path(raw).is_absolute()`），或者让三个读取侧统一 `Path(v).expanduser()`。两者选一，但必须一套 —— 现在是三套。建议前者：这个键的语义就是「用户明确指定的另一块盘」，不需要 `~` 支持。

> 验证：实跑（.venv python）：`validate_config('contour_warp_tmpdir','~/tf_scratch') -> True`；`validate_config('stitch_tmpdir','relative/dir') -> False`；`cm.set('contour_warp_tmpdir','~/tf_warp') -> True`，`cm.get(...) -> '~/tf_warp'`。读取侧复刻：在临时 CWD 下 `os.makedirs('~/tf_scratch')` 后 `listdir(cwd) == ['~']`、`isdir(cwd/'~'/'tf_scratch') == True`、`isdir(expanduser('~/tf_scratch')) == False`；`tempfile.mkdtemp(prefix='contour_warp_', dir='~/tf_scratch')` → `FileNotFoundError [Errno 2] '~/tf_scratch/contour_warp_47l3spoz'`；`Path('~/tf_scratch').resolve() == /home/zhang/workspace/map-download/~/tf_scratch`。

**`_wrap_lons` 的护栏在它唯一有害的调用点上失效** — `src/services/raster_probe.py:146-158（判据），src/services/raster_probe.py:492（唯一有害调用点）`

`_wrap_lons` 的护栏是「补 360 之后跨度 ≤180 才认跨界」（:158 `return shifted if max(shifted)-min(shifted) <= 180 else None`）。docstring :152-155 明写这条护栏就是为了让「真正的全球栅格（-180..180）」不被误判。**它不成立**：`[-180,180]` 补完是 `[180,180]`，跨度 0，稳稳落进 ≤180。护栏只在输入点里含中间经度时才有效（实测 `_wrap_lons([-180,-90,0,90,180]) = None`）——而 :492 的调用点 `_wrap_lons(wests + easts)` 每个文件只喂两个极值经度，正是护栏失效的输入形状。同一条规则的另一个调用点 `_bounds_from_points`（:174）喂的是 `_perimeter` 加密后的 84 个点，所以那条路碰巧是对的：一条规则两个调用点，可靠性不一样。

后果：多文件上传的「合并范围」行（static/js/map.js:948-950，count>1 时渲染）报出与真实并集无关的数字，并附一条假的 antimeridian 警告，全程无报错。实测两例：①全球栅格 + 一个 100°E 的小文件 → 合并范围报 `[100, -90, 180, 90]`，整个西半球消失；②一个覆盖 -100..100 的文件 + 100..101 的文件 → 合并范围报 `[100, -50, 101, 50]`，201° 宽的并集报成 1° 宽。触发条件是文件集的经度跨度 >180°（全球 DEM、或横跨东西半球的一批文件）。这块面板存在的唯一意义就是让用户在开跑几小时切片前确认「我选的文件覆盖对了」，而它在这里给的是一个自信的错数。不判 P0 是因为它只进显示：`recommended_maxzoom` 走 pixel_deg 不走 bounds，没有产物依赖这个值。

修法：合并侧不能只拿极值经度判跨界。两条都行：①`describe_headers` 把每个文件的 `[west,east]` 连同中点一起喂给 `_wrap_lons`（恢复护栏赖以生效的中间点）；②更稳妥的是给 `_wrap_lons` 补一条显式闸门——只有当所有输入经度都落在 `(-180,-90] ∪ [90,180)` 两端、中间带为空时才认跨界，`[-180,180]` 与 `[-100,100]` 都会被这条挡住。顺带把 :152-155 那段 docstring 改成真话。

> 验证：python 实跑（构造 header entry 直接调 describe_headers）：`_wrap_lons([-180.0,180.0]) = [180.0, 180.0]`；`_wrap_lons([-100.0,100.0]) = [260.0, 100.0]`；`_wrap_lons([-180,-90,0,90,180]) = None`。用 GDAL 造的真 GeoTIFF（720x360，EPSG:4326，gt=(-180,0.5,0,90,0,-0.5)）走完整链路：单文件卡片 `bounds_wgs84=[-180,-90,180,90]`（对），同一次调用的 summary `bounds_wgs84=[180.0,-90.0,180.0,90.0]` + `warnings=['antimeridian']`（零宽度）。多文件：`SUMMARY [100.0,-90.0,180.0,90.0] ['antimeridian']` 与 `SUMMARY [100.0,-50.0,101.0,50.0] ['antimeridian']`。

**`available` 的下标原点与 CesiumJS 的解析约定不一致** — `src/services/terrain_tiling/cesiumlab_terrain.py:1582-1584（"minzoom": min_level + available 从 min_level 起 append，见 :1400）`

vendored CesiumJS 1.143.0（static/vendor/cesium/1.143.0/Cesium.js 偏移 ~5413400）里 CesiumTerrainProvider 的 layer.json 解析原文：`let p=t.available; b=new If(e.tilingScheme,p.length); for(let y=0;y<p.length;++y){let _=p[y], S=e.tilingScheme.getNumberOfYTilesAtLevel(y); ... b.addAvailableTileRange(y,Z.startX,S-Z.endY-1,Z.endX,S-Z.startY-1)}` —— 层号恒等于**数组下标 y**，`minzoom` 根本没被读；TileAvailability 的最大层级还被钉成 `p.length`。而 build_terrain 在 min_level=8 时写出 minzoom=8、available[0] = z8 的矩形。默认生产路径之所以正确，纯粹是因为 `merge_base_availability`（layer_json.py:130-144）把它重排成以 0 为原点并硬置 minzoom=0；跳过 merge 的路径（停止提前 return、graft 抛异常导致任务失败但目录保留、CLI `--min-level>0`）拿到的就是这份错文件。

后果：错文件里 available[0] 被当成 z0：TMS→XYZ 的 Y 翻转用 `getNumberOfYTilesAtLevel(0)=1` 去减 startY=183/endY=184，算出 (-184,-183) 这种负区间；TileAvailability 上限被设成 3，z8 根本无法表达。结果是可用性声明全废，Cesium 要么不请求任何瓦片、要么请求不存在的坐标。今天没炸只是因为唯一的生产消费者先跑了 merge——这是一颗留给下一个消费者（或下一个跳过 merge 的分支）的雷。

修法：让 build_terrain 直接输出**绝对层号对齐**的数组：`available` 前面补 `min_level` 个 `[]`，`minzoom` 仍写 min_level（Cesium 不读它，人读）。同步把 `merge_base_availability` 里的 `task_min` 偏移逻辑删掉（改成纯并集），并把它 docstring 里「available[i] 的绝对层号是 minzoom + i」那句改成「= i」——那句现在描述的是本仓的私有约定，不是 quantized-mesh/Cesium 的行为。两处必须同一笔改，否则会二次偏移。

> 验证：① 从 vendored Cesium.js 直接读出上述解析代码（python 读文件 + 偏移定位，原文见 evidence）；② 实跑 build_terrain(min_level=8) 产出的 layer.json：minzoom=8、maxzoom=10、len(available)=3、available[0]=[{'startX':398,'startY':183,'endX':399,'endY':184}]。

**预览与建任务对同一个等高距给出不同答案** — `src/services/contour_engine.py:121-131（合成 DEM 的高程幅度）、171-175（同一道 _MAX_CONTOUR_LEVELS 闸门）`

预览用的合成场是 `z0=breaks[0]=0`、`z1=breaks[-1]*1.05=5250`，再叠 `hills=0.12*(z1-z0)*(sin+cos)`，实际值域约 [-1260, 6510]，跨度约 7770 m —— 比真实单张瓦片的起伏大一个量级。于是 `n_levels = 7770/interval` 很容易越过 200，第 175 行的 `if _MIN_CONTOUR_LEVELS <= n_levels <= _MAX_CONTOUR_LEVELS` 直接不成立，整段等高线绘制被跳过，**没有任何提示**，接口照样返回 200 + PNG。

后果：配色预览的全部意义就是「看线是什么样」。用户把等高距设成 10 m 或 20 m（比默认 50 m 更常见的大比例尺取值），预览里只剩分层设色和晕渲，看不到自己正在调的线色/线宽/计曲线/标注，会以为线色设置没生效。连带：此时预览路径根本不会碰 `color_intermediate/color_index`，一个非法线色（如 `#zzzzzz`）在这个 interval 下反而拿到 200，而建任务时会被 `validate_color` 判 400 —— 两个端点对同一个值给出不同答案，正是上一轮 P1#10 修掉的那类分歧。

修法：预览不该用「整条 0–5250 m 色带」当地形。把合成场的高程跨度按 interval 自适应（例如取 `min(z1-z0, interval*80)` 的窗口，保证 n_levels 落在 [10, 60]），并让色带随该窗口取子集；或者退一步——n_levels 超上限时按当前 interval 只画中间一段（预览与瓦片不需要跨图对齐，`interval_for_zoom` 那条「不许放粗」的理由在预览这边不成立）。

> 验证：实跑 `render_style_preview(ContourStyle(), iv)` 并统计线色像素：
```
interval=   5 n_levels=  1294 lines_drawn=False intermediate_color_px=24099
interval=  10 n_levels=   648 lines_drawn=False intermediate_color_px=24099
interval=  20 n_levels=   325 lines_drawn=False intermediate_color_px=24099
interval=  25 n_levels=   261 lines_drawn=False intermediate_color_px=24099
interval=  40 n_levels=   163 lines_drawn=True  intermediate_color_px=44086
interval=  50 n_levels=   131 lines_drawn=True  intermediate_color_px=38674
interval= 100 n_levels=    67 lines_drawn=True  intermediate_color_px=31153
```
前四行像素数**逐字相同**（24099，全部来自 hypsometric 色带自身）—— 说明这四张图与 interval 无关、确实一条线都没画。现有用例 test_contour_api.py:112 用 `interval=100000` 探参数，test_fix_contour_hardening.py:315 只做源码文本断言，都盖不到这一段。


### 搁死补偿网有洞，而注释断言洞不存在

**`dem_terrain_jobs` 不在补偿白名单里** — `src/services/dem_task_manager.py:641-655（_run_tiling_job 的兜底 except）、:616-621（stopped 提前 return）、:396-397（UPSERT 的 rowcount 闸）`

`_run_tiling_job` 的 `except Exception` 里自己又开连接写终态（:642 `conn = get_connection()`，:645 UPDATE，commit）—— 这段**没有再包一层保护**，`database is locked` / 建连接失败会从 except 里再抛出去，job 行留在 running。`finally` 只摘登记（:657-672），不写状态。`task_cleanup._STRANDED_TASK_TABLES = {'tasks','dem_tasks','contour_tasks'}`（:126）不含 `dem_terrain_jobs`，其注释「dem 的切片 job 同理（自己有兜底 except）」正是被这条路证伪的。第二道门：:616 `if stopped: return` 直接收工，注释假定「行连同 CASCADE 已经不在了」；但 `task_deletion.delete_task_row` 的 except 分支（task_deletion.py:333-348 的注释自己写明）在 commit 失败时**回滚删行但不回滚 stop flag**，于是行还在、flag 已置、切片线程走 :616 正常 return —— 没有异常，`_run_tiling_job` 的 except 也盖不住。`dem_tasks` 走 `_run_task` 的 `fail_stranded_running_task`（:785）有网，切片 job 没有。

后果：job 行永久 running：再点「开始切片」被 :396 的 `WHERE dem_terrain_jobs.status != 'running'` 判为 rowcount=0 → ValueError「已在运行」；`src/routes/terrain_api.py` 没有任何重置 job 的端点。唯一出路是删掉整个 DEM 任务重下，或重启进程靠 `_recover_orphan_running_tasks` 解开 —— 与 2026-08-08 评审 P1#1/#2 修掉的那个形态一模一样，只是这张表漏了。

修法：把 `dem_terrain_jobs` 纳入同一张网：`_run_tiling_job` 的 `finally` 里补一次「行还是 running 就判 failed」（表名白名单里加 `dem_terrain_jobs`，`fail_stranded_running_task` 的 UPDATE 需要按 `task_id` 而不是 `id` 匹配，或另加一个同形的 helper）；同时 :616 的 stopped 分支不要无条件 return，也走同一条补偿（行真被删了就是无害 no-op，正是该 helper 的既有约定）。

> 验证：实跑（临时库，patch `tile_dem_task_dir` 抛错 + 让 except 里的第二次 `get_connection()` 抛 `sqlite3.OperationalError('database is locked')`）：
  _run_tiling_job propagated: OperationalError: database is locked
  job row now: running | error_message= None
  registered in active_tasks: False | stop_flags: False
  start_tiling -> ValueError: DEM tiling job for task 1 is already running
  fail_stranded_running_task table whitelist: ['contour_tasks','dem_tasks','tasks']
  after fresh manager, job status = failed   ← 只有重启进程才解得开

**`local_terrain_tasks` 同样不在，排除理由同样不成立** — `src/services/local_terrain_task_manager.py:594-599（stop 分支正常 return）、src/services/local_terrain_task_manager.py:628-629（兜底 except 自己会抛）、src/services/task_cleanup.py:124-126（白名单注释）`

`task_cleanup.py:124-126` 写着「没有 local_terrain_tasks：它没有 `_run_task`，切片线程 `_run_tiling_job` 自己有兜底 except 把行判 failed」。这句话对两条路都不成立：
① `:594` `if stop_flag is not None and stop_flag.is_set(): return` —— **正常 return，没有异常，兜底 except 盖不住**。它的注释（:595-599）断言「local_terrain_tasks 行此刻已经不在了」，而 `task_deletion.py:335-346` 自己就写明了反例：`delete_task_row` 先置停止标志再 DELETE，commit 失败时事务回滚而标志**不**回滚（有意的），行还在、标志已置。这正是 2026-08-08 评审 P1#2 的第 2 条路，另外三条管线为它加了 `fail_stranded_running_task`。
② `:628-629` `except Exception as e:` 的第一句就是 `conn = get_connection()`，**它自己在 try 之外**。建连接失败（库被锁/磁盘满）时新异常直接穿透线程，行留在 running。这是 P1#1 的第 1 条路，形状逐字相同。
另外 `:447` 与 `:493` 两处注释说卡住之后「delete 也被拒」——不对，`delete_task_row` 按 id 无条件 DELETE，删是通的。这个错误描述会把下一个维护者推向错的修法。

后果：任务行永久停在 running：界面上是一条永不结束的「运行中」，`start_tiling` 拒绝重启（实测抛 `Local terrain task 1 is already running`），唯一出路是重启进程让 `_recover_orphan_running_tasks` 捞。路①的用户视角是「点删除报 500，任务还在转」；路②更糟——切片其实已经失败了，但界面一直显示运行中且 error_message 为空，用户会一直等一个已经死掉的作业。

修法：把 `'local_terrain_tasks'` 加进 `task_cleanup._STRANDED_TASK_TABLES`，并在 `_run_tiling_job` 的 `finally`（:648-652）里调 `fail_stranded_running_task('local_terrain_tasks', task_id, ...)`——与另外三条管线同一处、同一形状。同时把 `task_cleanup.py:124-126` 的排除理由删掉，把 `:447`/`:493` 那句「delete 也被拒」改对。

> 验证：python 实跑（tmp 库 + init_database + 真 LocalTerrainTaskManager）：路② —— 置位 stop_flag 后调 `_run_tiling_job`，`row after worker returned via stop branch -> ('running', None)`；路① —— 让 `tile_dem_task_dir` 抛、`get_connection` 抛 OSError，`exception escaped the worker thread -> OSError: database is locked`，`row after thread died -> ('running', None)`。补偿网覆盖面：`whitelist = ['contour_tasks','dem_tasks','tasks']`，`fail_stranded_running_task('local_terrain_tasks', ...)` → `ValueError: 未知任务表 'local_terrain_tasks'`。卡住后 `start_tiling(1)` → `ValueError: Local terrain task 1 is already running`。旁证：`tests/test_fix_stranded_running_task.py` 的参数化恰好只有 `["tasks","dem_tasks","contour_tasks"]`。

**进度落库失败被降级成一条 log，完成判定却只信库** — `src/services/task_manager.py:1435`

下载循环收尾：`try: flush_progress_counts() except Exception as flush_error: logger.error(...)`（:1435-1440）—— 失败只记日志，批次随 :1441 `progress_conn.close()` 一起蒸发。而完成判定 :1673-1677 `SELECT COUNT(*) AS failed_count FROM task_tiles WHERE task_id=? AND status='failed'` 读的是**库**，不是内存里那份准确的 `progress_counts['failed']`（:1302 一直在累加）。小任务（<PROGRESS_DB_FLUSH_INTERVAL=200 块）只有这一次 flush，它失败就等于整轮进度全丢；大任务丢的是最后不满一批 + 之前所有 flush 失败退回队列（:1177-1189）攒下来的量。completed 是终态，start_task（:470）拒绝重启，用户没有原地自愈路径。

后果：一次 `database is locked` / 磁盘满，把「N 块瓦片失败」的任务写成「completed，无 error_message」。tasks.downloaded_tiles 同时停在对账值（首轮 = 0），前端进度条显示 0/N 却标完成。实测控制组与故障组只差这一个异常。

修法：完成判定不能只信库。要么在 :1673 之前先拿内存值把关（`if progress_counts['failed'] > 0 or failed_count > 0:` 判 failed），要么把 flush 失败记成任务级故障（置一个 nonlocal 标志，收尾时判 failed 并写明「进度落库失败，计数不可信」）。当前「只 logger.error」是把可观测的 bookkeeping 故障降级成了静默的错误状态。

> 验证：probe1 实跑同一条 _execute_task，唯一变量是 `_write_progress_batch` 抛 `database is locked`：control (flush works): `status=failed failed_rows=1 downloaded=11 failed=1`；flush raises: `status=completed failed_rows=0 downloaded=0 failed=0 error=None`（12 块瓦片里第 1 块失败）。


### 静默丢数据 / 静默错声明

**404 颗粒计进「已下载」且永不重试** — `src/services/dem_download_engine.py:236-243`

引擎把 HTTP 404 上报为 `skipped`（engine:241）。管理器 `_DONE_STATUSES = ("completed", "skipped")`（manager:53），`_status_count_deltas`（:57）于是把 skipped 计进 `downloaded_files`；收尾统计 `SUM(status NOT IN ('completed','skipped','failed'))`（:966）把 skipped 当已终结，任务直接置 `completed`（:987）。而恢复时的重入队查询是 `WHERE task_id=? AND status IN ('pending','failed')`（:812）—— **'skipped' 不在里面**，所以一颗被 404 判过的颗粒此后永远不会再被请求。`dem_tasks` 表没有 skipped_files 列（database.py:528-547），前端拿不到任何区分信号。manager:46-52 的注释断言「磁盘产物与后续切片都是对的 —— 纯计数/展示口径问题」，实测不成立。

后果：两种后果。(a) 正常海域选区：记录面板显示「已完成 10/10 文件」，实际只下了 4 个，用户无从知道 6 个格子是空的。(b) 一次**瞬时** 404（代理抖动、上游维护页、S3 一致性窗口、LP DAAC 路径改版）会把该颗粒永久钉成 skipped —— DEM 少一个 1°×1° 方格，任务照报成功，后续 terrain / 等高线在那块静默留洞，用户点「恢复」也修不好，只能删任务重建。全部 404 时任务报 completed 而目录为空，问题要到点「开始切片」才炸出来。不判 P0 只因为「404 = 真的没数据」在多数情况下成立；一旦不成立就是静默错数据。

修法：三处：(1) `dem_tasks` 加 `skipped_files` 列（或至少在 `_execute` 收尾把 skipped 数量写进 `error_message`/一条 warning），别再把 skipped 混进 downloaded_files；(2) 把 `skipped` 加进 :812 的重入队集合，让「恢复」能重试一次 —— 真没数据的颗粒再 404 一次代价是一个 HEAD 级请求；(3) 全部颗粒 skipped 时任务不应判 completed，应判 failed 并写明「所选区域没有任何可用颗粒」。

> 验证：实跑（临时库 + FakeSession，`python -c`）：
=== run 1: 全部 404 ===
  task=completed 2/2 ok, 0 failed | files=[('A_DEM.tif','skipped','retry=0'),('B_DEM.tif','skipped','retry=0')] | dir=[]
=== run 2: 上游恢复正常，用户点恢复 ===
  task=completed 2/2 ok, 0 failed | files=[('A_DEM.tif','skipped',...),('B_DEM.tif','skipped',...)] | dir=[]
另一轮（3 颗全 404 后走切片闸门）：
  task: status=completed downloaded=3 failed=0 total=3；task dir contents=[]
  start_tiling ACCEPTED → job status=failed err='No DEM tifs found under .../dem_task_1'

**`available` 写的是计划而不是事实** — `src/services/terrain_tiling/cesiumlab_terrain.py:1584（写 available）与 :1592（无条件写 layer.json）`

`available_per_level` 在 :1414-1438 的计数循环里算完，之后 `_worker_tile` 的逐瓦片容错（:1270-1272，异常只记 warning 返回 None）与 `stop_flag`（:1501-1502 / :1535-1536）都不会回头修正它。:1592 在两种情况下都照写。管理器侧 `failed>0` 只降级成 warning 文案（dem_task_manager.py:590 一带、local_terrain_task_manager.py:590），作业仍是 completed；停止时 `tile_dem_task_dir` 在 dem_task_tiler.py:195 提前 return，merge/patch 都不跑，那份 layer.json 原样留在盘上。`delete_files=false`（local_terrain_task_manager.delete_task:674，路由可传）时产物目录被保留交给用户。

后果：完成态作业：Cesium 对声明可用却不存在的瓦片发请求拿 404，而它不回落父层——地形上出现按瓦片对齐的空洞。停止态作业（用户选「保留文件」）：拿到一个声称有 69 张瓦片、实际 0 张的目录，拷到别处加载即整个地形不可见。这正是 M11 注释（dem_task_tiler.py:99-102）说要堵、但只堵了「计数回报」那一半的链。

修法：把 available 从「计划」改成「事实」：`_tally` 里累计实际落盘的 (z,x,y)，收尾时按层归并成 range 再写 available（最省事的版本：每层维护 min/max x/y 的实际外包，失败瓦片不计入）。停止分支额外把 maxzoom / minLevel 收到实际处理到的最深层。

> 验证：实跑 build_terrain（min_level=8, max_level=10, tile_size=65, workers=1, grid, 合成 1°×1° DEM）：①注入 1/5 瓦片失败 → counts={'total':69,'rendered':56,'failed':13}，layer.json 声明 69 张、磁盘 56 张，**MISSING-BUT-DECLARED=13**；②预置 stop_flag → counts={'total':69,'rendered':0,'failed':0}，layer.json 仍声明 69 张、磁盘 **0** 张，available=[[{398,183,399,184}],[{796,366,799,369}],[{1592,733,1598,739}]]。

**级数上限静默丢掉整张瓦片的等高线** — `src/services/contour_engine.py:461-468（上限判定与一次性 warning）`

`if n_levels > _MAX_CONTOUR_LEVELS: ... draw_lines = False`。被闸掉的瓦片仍然画了 hypsometric+晕渲，`_render_contour_tile_core` 返回 'rendered'，counts 里没有任何一格记录「这张瓦片的等高线被丢了」。唯一线索是 `ctx.levels_capped` latch 下的一条 warning——每个 ctx 只发一次，而并行模式每个 worker 一个 ctx、worker 是 spawn 出来的子进程，父进程配好的 file handler 不在那里。管理器侧 1048 行只看 `rendered == 0`，1060 行只看 `failed > 0`，两条都为假，于是走 1067 行 `status='completed'`。

后果：用户填一个完全合法的等高距（下限是 1 m），在陡峭地形上得到一张「山体内部没有等高线、只有边缘瓦片有线」的地图，任务显示成功完成，logs/terraforge.log 里一个字都没有。打包成 Windows GUI exe 时子进程 stderr 也没有落点，诊断彻底消失。

修法：把「因超上限而未画线」计成第五个 counts 键（worker 已经在回传字符串状态，加一个 'rendered_no_lines' 即可），管理器在收尾时把它写进 error_message／completed 的提示里；至少要让 warning 从 worker 传回父进程（把 capped 的 z 与 n_levels 附在 worker 的返回值上，由父进程 log）。

> 验证：实跑合成 DEM（600×600，lon[86.0,86.1] lat[27.9,28.0]，高程 3000→8000 m，即单张 z14 瓦片约 1100 m 起伏），interval=5、zoom 12-14：
- 复算引擎自身的级数公式：`z=13 eff=10 tiles=16 over_cap=8 max_n_levels=224` / `z=14 eff=5 tiles=36 over_cap=24 max_n_levels=227`（对照 interval=20：三层 over_cap 全 0）
- 像素统计与之吻合：z14 36 张瓦片中 24 张零等高线像素
- 串行跑：`counts={'total':58,'rendered':58,'failed':0,'skipped':0}`，只有一条 warning，且报的是 z=13 不是最严重的 z=14
- 并行跑（workers=4，即默认 `min(4,cpu_count)`）+ 根 logger 只挂 FileHandler：`counts={'total':58,'rendered':58,'failed':0}`；`log lines with level-cap warning in FILE sink: []`；三条 warning 只出现在子进程 stderr（logging.lastResort）

**晕渲预览把投影坐标当经纬度** — `src/services/hillshade_preview.py:67-70（west/north/east/south 直接取 gt）、125（`return [west, south, east, north]`）`

`gt = vrt.GetGeoTransform(); west, north = gt[0], gt[3]; east = west + RasterXSize*gt[1]; south = north + RasterYSize*gt[5]` —— 全程没有读 `GetProjection()`、没有 `CoordinateTransformation`。返回值经 `terrain_static._hillshade_json:251` 原样 `jsonify` 给前端，`static/js/map.js:2044` 直接 `Cesium.Rectangle.fromDegrees(bounds[0..3])`。而 local_terrain 的上传闸门（`local_terrain_task_manager.py:163-165`）只查扩展名，不查 CRS，UTM/兰伯特 的 GeoTIFF 一律收下并存成 `source/upload_N_dem.tif`。同一个仓里 `contour_task_manager._union_tif_extent_lonlat:163-175` 对同一件事做的是正确的：建 `osr.CoordinateTransformation(src, 4326)` 再转四角 —— 一条规则两份实现，已经漂了。

后果：「无切片任务 → 源 DEM 晕渲单图叠加」这条回退路径对任何投影坐标系的上传都给出米制的 rectangle。Cesium 拿到 `fromDegrees(400000, 3088000, ...)` 要么抛（debug 断言）要么放出一个荒谬矩形，前端 `.catch(()=>null)` 把它吞成「没有源文件」的提示。用户上传了正确的 DEM，预览要么空白要么错位，且没有任何错误信息。

修法：照抄 `_union_tif_extent_lonlat` 的做法：读 `vrt.GetProjection()`，非地理坐标系时用 `osr.CoordinateTransformation(src, EPSG:4326, OAMS_TRADITIONAL_GIS_ORDER)` 转四角取并集；`GetProjection()` 为空时返回 None（无参考栅格没有正当的经纬度矩形），让路由 404 而不是给一个错的矩形。

> 验证：实跑 `hillshade_preview.ensure_hillshade`：
- EPSG:4326 源（gt 起点 86.0/28.0，400×400 @0.01°）→ `bounds = [86.0, 24.0, 90.0, 28.0]` ✓
- EPSG:32645 源（gt 起点 400000/3100000，400×400 @30 m，同一片地面）→ `bounds = [400000.0, 3088000.0, 412000.0, 3100000.0]` ✗（米被当成度）

**投影栅格被当成经纬度解释，零警告** — `src/services/raster_probe.py:198-202（`_epsg_from_geokeys` 的分支），src/services/raster_probe.py:416（无量纲检查地写入 bounds_wgs84）`

`:199 for key in ("3072",) if model in (1, 3) else ("3072", "2048")`。docstring :189-196 把这条判据的全部理由讲清楚了：投影栅格上 2048 永远在，拿它当栅格 EPSG 会「凭空报出一个自信的错范围且不带任何警告」，并点名「国产 GIS 导出的自定义 Albers/兰勃特/高斯克吕格 DEM 正是 1024=1 + 3072=32767」。但保护只在 `model in (1,3)` 时生效。GeoTIFF 允许 GTModelTypeGeoKey 取 32767（用户自定义），也有写库干脆不写这个键——两种情况下 `model` 分别是 32767 和 None，都不在 `(1,3)` 里，于是回落到 2048，docstring 说要防的事原样发生。第二道网也没有：:416 `result["bounds_wgs84"] = [west, south, east, north]` 对经纬度量纲不做任何检查，唯一的过滤是 `transform_bounds`/`_bounds_from_points` 的 `math.isfinite`（:264-266、:171-172），而 CGCS2000→WGS84 近乎恒等变换，米级东北坐标转出来是有限值。

后果：一个 30 m 的高斯克吕格 DEM（东 500000 / 北 3300000）被报成：`epsg=4490`、`crs_name="China Geodetic Coordinate System 2000"`、`bounds_wgs84=[500000, 3294000, 506000, 3300000]`（经度 50 万度、纬度 329 万度）、`pixel_meters=3339600`（3339 公里/像素）、`recommended_maxzoom=0`、`warnings=[]`。面板判「致命」的那套码（static/js/map.js:797-799 的 header_unreadable/no_georeference/unknown_crs/some_unusable）一个都没触发，卡片是干净的。用户看到一个理直气壮的错答案，比 `unknown_crs` 糟得多——后者至少诚实。

修法：两处都补。①`_epsg_from_geokeys`：只有 `model == 2`（明确是地理栅格）才允许回落到 2048；`model` 为 None / 32767 / 其他值时与 `model in (1,3)` 同样只认 3072，解不出就是 `unknown_crs`。②`_describe_one` 在 :416 之前加一条量纲闸：`abs(lat) > 90` 或 `abs(lon) > 360` 就丢弃这次换算、按 `crs_unresolved` 处理。第二条不依赖任何 geokey 推断，能兜住这一类里我没枚举到的其余走法。

> 验证：python 实跑 `describe_headers`，三组 geo_keys 对照（geotransform 固定为 tie_point=(500000,3300000)、pixel_scale=30m）：`{'1024':32767,'3072':32767,'2048':4490}` → epsg=4490，bounds_wgs84=[500000.0, 3294000.0, 506000.0, 3300000.0]，pixel_meters=3339600.0，zoom=0，warn=[]；`{'3072':32767,'2048':4490}`（1024 缺席）→ 完全相同；`{'1024':1,'3072':32767,'2048':4490}`（docstring 点名的那组）→ epsg=None，warn=['unknown_crs']（这组是对的）。真实世界里 1024 缺席/取 32767 的写库占比我没有测量，标注为 [INFERENCE]。

**`finally` 里的 `gdal.Unlink` 反过来吃掉真正的错误** — `src/services/hillshade_preview.py:141（外层 `finally: gdal.Unlink(vrt_path)`）、139（内层 `gdal.Unlink(thumb_path)`）`

`vrt_path` 是 `/vsimem/hillshade_<pid>_<tid>.vrt`。`gdal.BuildVRT` 失败时该 vsimem 文件根本没被创建，第 66 行抛出的信息完整的 `RuntimeError("gdal.BuildVRT failed for <dir>")` 在退栈时撞上 `finally` 里对不存在路径的 `gdal.Unlink`。本模块从不设置 GDAL 的异常模式，而 `gdal.UseExceptions()` 是**进程全局**的、`contour_engine.py:659` 和 `:367` 无条件调它 —— 这件事本仓已经知道并写在 `download_engine.py:1216` 与 `cesiumlab_terrain.py:542-545` 的注释里（那两处为此显式钉死非异常模式），只有 hillshade_preview 没做。同样的坑在第 139 行再来一次：缩略图 `gdal.Translate` 返回 None 时 `thumb_path` 已赋值但文件不存在。

后果：只要本进程跑过任意一个等高线任务（或将来升到默认开异常的 GDAL 4），源目录里有一个坏 tif 就会让 `/terrain/{dem,local}/<id>/hillshade` 抛 `RuntimeError('unknown error occurred')` —— 500 到浏览器，日志里也是这句，真正的原因（BuildVRT 失败、哪个目录）被完全抹掉。前端 `map.js:2036-2038` 再把它降级成「没有源文件」的提示，排查时三层都看不到真相。

修法：两处 Unlink 各包一层 `try/except Exception: pass`（清理失败不该改变异常语义），或在 `_render` 入口按本仓既有约定显式钉住模式；顺带把第 65 行 `if vrt is None` 的判据补上 —— 异常模式下 BuildVRT 是抛而不是返 None，那道 None 检查已经半死。

> 验证：实跑：`gdal.UseExceptions()` 后 `gdal.Unlink('/vsimem/does_not_exist_<pid>.vrt')` → `RuntimeError('unknown error occurred')`。随后对一个内容为 `b'not a tiff at all'` 的 `x_dem.tif` 调 `ensure_hillshade` → `ensure_hillshade RAISES: RuntimeError unknown error occurred`（原本应有的 `gdal.BuildVRT failed for /tmp/hsfail/src` 消失）。


### 界面/文档对用户说的话是反的

**配置页说「底图不经过代理设置」，而底图恰恰吃 proxy_url** — `src/i18n/catalog/tpl_config.py:174-187（渲染点 templates/_config_content.html:237）`

`tpl.config.download.basemap_hint` zh: 「框选时看到的底图，与上面的下载源相互独立 —— 底图由浏览器直连加载，不经过代理设置；下载走后端，走代理。」en: 「the basemap is loaded by the browser directly and does NOT go through the proxy settings, while downloads go through the backend and do」。而 src/routes/basemap_static.py:185 每张底图瓦片都 `proxy = resolve_from_config(config_manager, wait_s=_PROXY_WAIT_S)`，同文件 :14-20 的模块 docstring 把「共用 proxy_autodetect 同一个入口」写成本路由存在的决定性理由。CLAUDE.md:127 已经预先裁定这类残留：「Comments claiming 「底图走浏览器直连」 predate it and are wrong … Treat any survivor as stale」，但它只点名了 database.py 与 _config_content.html 两处已修的，漏了这条 i18n 文案 —— 而这条是唯一**用户看得见**的。

后果：底图打不开（蓝球）是本项目最高频的现场问题，而配置页当场告诉用户「代理设置对底图无效」，用户于是不会去配 proxy_url —— 正是修好它的那一步。中英两版同错。

修法：改成「底图瓦片由服务端同源转发（/basemap/{z}/{x}/{y}），与下载走同一条出网路径，一样吃 proxy_url / 代理自动发现」。顺手给 tests/test_docs_claims.py 加一条反向断言：i18n catalog 里不许出现「直连」+「不经过代理」与 basemap 同现。

> 验证：Flask test client：GET / 的 HTML 中 '浏览器直连加载' -> True、'不经过代理' -> True（rsb_osm_probe 实测输出）

**换底图后一天之内看到的还是旧的那家** — `src/routes/basemap_static.py:267-269`

`response.headers["Cache-Control"] = ("public, max-age=86400" if candidate["source"] == configured["source"] else f"public, max-age={_FALLBACK_MAX_AGE_S}")`。同源路径是固定的 `BASEMAP_TILE_PATH = '/basemap/{z}/{x}/{y}'`（basemap_source.py:50），不含源标识、无 ETag、无 Last-Modified（send 的是 `Response(body, mimetype=...)`），所以浏览器命中缓存后不回源。同文件 :259-266 的注释已经精确描述了这个危害（「浏览器就会把另一家的影像烤进缓存一整天 … 它永远不会自己好 —— 用户在界面上也没有任何补救手段」）并对**回退**瓦片降到 60 秒，但对「配置被用户改掉」这条同构路径没做任何处理。叠加 :68 的 `_SOURCE_TTL_S = 5.0`：改完配置后 5 秒内取到的瓦片仍来自旧源，却因为 `candidate['source'] == configured['source']`（比较的是**缓存里那份**解析结果）拿到 86400 的长缓存。

后果：用户在配置页把底图从 Esri 换成 Google（或换掉一个偏移的自定义源）→ 保存 → 刷新，已浏览过的区域画面**完全不变**，长达 24 小时，且只能靠硬刷新/清缓存解决。表现为「这个设置项坏了」。

修法：让同源 URL 带上源标识，例如 `BASEMAP_TILE_PATH = '/basemap/{z}/{x}/{y}'` 改为服务端生成时附一个由配置源算出的版本串（`?v=<hash(configured_source)>`，路由忽略该参数），换源即换 URL 空间；或把长缓存降到分钟级并补 ETag。另外 86400 那一支应当用**当次请求实时解析**的配置源比较，而不是 5 秒 TTL 缓存里的旧值。

> 验证：Flask test client + 打桩 opener：正常出图 `200 Cache-Control='public, max-age=86400'`；回退出图 `200 Cache-Control='public, max-age=60'`（rsb_fallback_probe）。切源实测：`cm.set('basemap_source','google_satellite')` 后下一张瓦片仍打 server.arcgisonline.com，而同一时刻 `/api/basemap` 已报 source=google_satellite；强制 TTL 过期后才切到 mts0.googleapis.com（probe3 输出）

**状态栏永久停在「运行中」，与同一行的「失败」自相矛盾** — `static/js/task_store.js:76 (replaceAll) / static/js/task_store.js:162 (setActive)`

`replaceAll(list)` 只做 `state.tasks.splice(...)` + `reindex()`，**一个字都不碰 state.active**（76-88 行）。能让 state.active 变小的写入方只有三个：`setActive`（整体替换，唯一调用点 tasks.js:162，即 `loadActiveTasks`，而它只在 `initTasks` 与 socket `connect` 重连回调里跑）、`dropActive`（tasks.js:476，靠 `task_completed` 事件驱动）、`remove`（history.js:756，靠用户点删除）。于是任何「行已进终态、但前端没收到 task_completed / task_failed」的路径都会在 state.active 里留一条永久 live 的记录。这条路径是**确定存在**的：`src/services/task_cleanup.py:131` 的 `fail_stranded_running_task` 把行 UPDATE 成 failed 之后**不 emit 任何 socket 事件**（函数体 156-176 行只有 SQL + logger），前端无从得知。此外 `loadHistory` 全程也不调 `updateStatusTasks`（history.js:170-175 只有 renderHistoryTable / renderPagination / renderHistoryMap），状态栏文字连重算的机会都没有。

后果：用户看到的是自相矛盾的两处界面：时间流那一行已经翻红显示「失败」，底部状态栏同时写着「1 个活动任务（1 运行中）」，汇总进度条也不消失；`updateTimeDisplay`（tasks.js:646）因为 `liveTasks().length !== 0` 每秒继续 bumpTick 唤醒 Vue 调度器。翻页、切筛选 chip、删别的任务、关开面板都不能纠正 —— 唯一的出路是断线重连或整页刷新。

修法：在 `replaceAll` 里按服务端返回的**终态行**对账活动集（只删、不加，绝不能因为「本页没返回」就删 —— 活动任务可能落在别的分页窗口）：
```js
state.tasks.splice(0, state.tasks.length, ...rows);
reindex();
rows.forEach(r => { if (!isLive(r)) delete state.active[r._key]; });
```
另在 `deleteTask` / `loadHistory` 成功后补一次 `updateStatusTasks()`。

> 验证：node 实跑（Vue 3.5.13 vendor 真身 + task_store.js 原文，vm 沙箱）：`S.replaceAll([running]); S.setActive([running])` → `live=1`；模拟 fail_stranded 之后的 loadHistory `S.replaceAll([{...status:'failed'}])` → 输出 `after loadHistory: live= 1 rowStatus= failed activeStatus= running` / `status-bar would read: 1 active / 1 running`。另 `node --check` 全部 12 个目标文件通过。

**面板不为其上的 Bootstrap 弹窗让位** — `static/js/panels.js:144（Escape 分支）、:150（唯一的让位判据）、:159-162（Tab 分支）、:126-129（焦点归还判据）、:135-137（replaceState）`

onKey 的第一句就是 Escape 分支：`if (e.key === 'Escape') { closePanel(); return; }`（:144），而唯一的让位判据 `if (document.querySelector('.app-confirm-overlay')) return;` 排在它**下面**（:150）——所以那条让位从来只管 Tab，Esc 一次都没被它保护过。ui.js 的自定义 confirm 之所以没出事，是它自己那侧的功劳：ui.js:230 用 capture:true 注册并 stopImmediatePropagation，panels.js 根本收不到那个 ESC；不做这个捕获动作的浮层就全部暴露，而 Bootstrap 不做。全文件 grep `modal` 零命中。四个 Bootstrap 弹窗都在 body 直下、不在任何面板子树里（渲染 index.html 实测字节偏移：historyPanel@27129 / configPanel@31896，taskDetailModal@60095 / pathBrowserModal@68923），base.html:96-102 写明 taskDetailModal 必须留在面板外（面板恒带 transform，会成为 fixed 后代的包含块并自建层叠上下文），并由 test_task_detail_modal_is_not_trapped_inside_workbench_panel 钉住——所以位置是对的，问题在 panels.js。两条可达路径：首页任务面板里点任务名开 #taskDetailModal（task_list.js:64 的 .task-name 在 #historyTableBody 内）；首页配置面板里点「浏览」开 #pathBrowserModal（#defaultSavePathBrowse 在 #configPanel 内）。ESC：Bootstrap 5.3.0 把处理器绑在 modal 元素上且既不 preventDefault 也不停传播——vendored bundle 偏移 53304 抓到 `P.on(this._element,"keydown.dismiss.bs.modal",(t=>{"Escape"===t.key&&(this._config.keyboard?this.hide():this._triggerBackdropTransition())}))`；目标阶段先跑 hide，事件继续冒泡到 document，panels.js:144 把面板也关了。关的时候 :126 的 `el.contains(document.activeElement) && restoreFocus` 为假（activeElement 在 body 级弹窗里），restoreFocus 在 :129 被直接置空、从未被 focus，焦点落回 body；:135-137 又用 replaceState 抹掉 hash，后退键也回不来。TAB：Bootstrap 的 FocusTrap 注册在 document 冒泡阶段、且是弹窗 show 时才注册（偏移 49127 `P.on(document,"focusin.bs.focustrap",...)` 与 `P.on(document,"keydown.tab.bs.focustrap",...)`），而 panels.js 的监听在面板打开时就挂上了、跑在前面：:159-162 判定 `!el.contains(activeElement)` → preventDefault + 聚焦面板首个可聚焦元素，随后 Bootstrap 的 focusin 又把焦点拖回弹窗第一个元素。净效果不是「漏进面板」而是**每按一次 Tab 都复位到弹窗的第一个控件**。另有同源一条：两个面板的 `aria-modal="true"` 是静态属性（index.html:418,430），运行期无人摘挂，按 ARIA 1.2 弹窗整体落在模态边界之外。

后果：首页两条核心流程对键盘用户直接不可用：目录选择弹窗里到不了目录列表与 #pathBrowserSelect，任务详情弹窗里到不了起切地形/刷新/关闭。鼠标用户同样中招且更常见——在详情弹窗里按 Esc 只想关弹窗，结果身后的任务面板一起滑走、URL hash 被 replaceState 抹掉、焦点掉回 body，列表位置/滚动位置/状态筛选全丢且没有恢复路径。

修法：把让位判据提到 onKey 的**第一句**（必须在 Escape 分支之上，否则又只管 Tab）：`if (document.querySelector('.app-confirm-overlay, .modal.show')) return;`。用 `.modal.show` 而不是 `document.body.classList.contains('modal-open')`——后者在 show 过渡里置位稍晚。aria-modal 那条另配：在 show.bs.modal / hidden.bs.modal 上临时摘挂面板的 aria-modal。

> 验证：我这侧：panels.js:126-137,144,150,159-162 逐行读；grep modal 于 panels.js 零命中；Jinja 渲染 index.html 实测四个弹窗与两个面板的 DOM 位置（偏移量见 evidence）。FrontendApp 复核并补齐了机制（panels.js 的 Esc 分支在让位判据之上因而该判据是 Tab-only；ui.js:230 的 capture+stopImmediatePropagation 才是 confirm 幸免的真正原因；从 vendored bootstrap.bundle.min.js 偏移 53304 / 49127 抽出 modal ESC 与 FocusTrap 的注册形态），并指出 Tab 的真实净效果是「每次复位到弹窗首控件」而非「漏进面板」。**两人都没有在真实浏览器里驱动**——全部结论来自源码与 vendored bundle 抽取。严重度上 FrontendApp 判 P2（交互/a11y 破损、无数据损失）；我按本次评审的判据保留 P1（「P1 = 可达路径上的真实缺陷」，而 P2 的定义是「会咬到维护者的健壮性/漂移」——这一条咬的是用户）。


### 文档不变量已过期

**CLAUDE.md 的三角化/法线一节三条全反** — `CLAUDE.md:141, CLAUDE.md:143, CLAUDE.md:144`

CLAUDE.md:141 写 `TileParams` defaults to `triangulator="auto"` / `build_terrain` defaults to `normals=True`；实际 src/services/terrain_tiling/dem_task_tiler.py:50 是 `triangulator: str = "grid"`，:59 是 `normals: bool = False`（build_terrain 签名默认仍是 True，但应用侧透传 False，dem_task_tiler.py:168 `normals=params.normals`）。CLAUDE.md:143 断言瓦片带 oct 法线且 layer.json 声明 `extensions: ["octvertexnormals"]`；cesiumlab_terrain.py:1590 是 `"extensions": ["octvertexnormals"] if normals else []`，应用侧默认走 `[]`。CLAUDE.md:144 断言 `triangulator`/`max_error_k`/`normals`「not exposed to UI / DB / API — nothing reads them from the config table, env, request body, or query string」；实际 src/core/database.py:95 `('terrain_quality_preset','balanced')`、:99 `('terrain_vertex_normals','false')` 是 config 表键，src/routes/terrain_api.py:43-51 从 JSON body/表单读 `quality` 与 `vertex_normals`，dem_task_manager.py:582-584 / local_terrain_task_manager.py:572-574 把它们构造进 `TileParams(normals=…, level_offset=TILING_QUALITY_OFFSETS[quality])`，两张地形表还各加了 `quality` / `vertex_normals` / `effective_maxzoom` 列。

后果：CLAUDE.md 是本仓架构不变量的唯一事实源。照 :144 写代码的人会认为改法线只能改代码/CLI，于是绕开 `validate_tiling_quality` / `coerce_vertex_normals` 这两个唯一把关点（geo_validation.py:92、:98 的 docstring 明确写着「这是 vertex_normals 唯一的把关点」，管理器只做 bool()）；照 :141/:143 排障的人会以为产物带法线而实际不带（v0.2.13 发版说明里那条「静默无效」正是这个失效形态）。

修法：重写 CLAUDE.md 「Triangulation & per-vertex normals」小节：应用侧 grid + normals=False（CLI/全球底图仍 auto，是有意分叉）；`terrain_quality_preset` / `terrain_vertex_normals` 是 config 键，两条起切路由收 `quality` / `vertex_normals`，取值表唯一住在 `geo_validation.TILING_QUALITY_OFFSETS`；把关点是 `validate_tiling_quality` / `coerce_vertex_normals`。

> 验证：read-only（read dem_task_tiler.py:28-72、cesiumlab_terrain.py:1580-1592、terrain_api.py:26-56、database.py:92-105；grep TileParams 全仓）

**CLAUDE.md 的 WGS-84 枚举过期，且没提真正执行它的机制** — `CLAUDE.md:124`

CLAUDE.md:124：「Only Esri World Imagery and Google `lyrs=s` / `lyrs=m` are WGS-84, and those three *are* the whole `BASEMAP_PRESETS` table.」实际 src/services/basemap_source.py:59-84 里 `BASEMAP_PRESETS` 至少有四个键：`esri`、google 卫星、google 路网、`osm`（:81-84，`https://tile.openstreetmap.org/{z}/{x}/{y}.png`）。约束的执行点现在是每个 preset 的 `wgs84` 字段（:57-58 注释「决定它能不能进自动回退链」）加上 `AUTO_FALLBACK_ORDER = ('esri','google_satellite','osm')`（:101）与 `fallback_candidates` 里的 `if name == resolved['source'] or not preset['wgs84']: continue`（:152-155）。CLAUDE.md 整段没有「回退」二字。

后果：CLAUDE.md 是这条硬约束的唯一文字载体（tests/test_docs_claims.py:173 专门钉它）。按「表里就三条」去审的人会漏掉 osm，更会漏掉真正的闸门：新增 preset 时忘了写 `wgs84: False` 就会被自动追加进回退链，用户在一张偏移 100–700 m 的图上框选而界面只提示「已自动切换」。

修法：把 :124 改成：`BASEMAP_PRESETS` 现有四条（esri / google 卫星 / google 路网 / osm）；每条必须声明 `wgs84`，自动回退链 `AUTO_FALLBACK_ORDER` 只收 `wgs84=True` 的源（用户显式选的非 WGS-84 源排第一是他自己的决定）；顺带补上回退的其余口径（404 透传、403/429/5xx 才判源挂、回退瓦片只缓存 60 秒）。

> 验证：read-only（grep BASEMAP_PRESETS/AUTO_FALLBACK_ORDER in src/services/basemap_source.py:57-155）

**reference 教用户踩 heightmap 陷阱** — `docs/reference/terrain/cesiumjs-loading.md:80`

该行写「默认 `http://localhost:5000/terrain/base/layer.json`（`src/core/database.py` 的 `DEFAULT_CONFIGS`）」。实际 src/core/database.py:105 是 `('terrain_base_parent_url', 'http://localhost:5000/terrain/base')`，且 :100-104 的注释写明「必须是**目录**…带 /layer.json 会让它请求 .../layer.json/layer.json 得 404…塞一个假 heightmap 图层并污染共享 builder（实测 4154 m 山峰解成 -744 m）」。CLAUDE.md:146 同一条以 ⚠️ 记录。同目录 README.md:30 已经写对（目录形式），只有这份没改。

后果：reference/ 是「照着做能信」的一层。用户按 §4 的说法（该节主题恰恰是「什么时候必须改这个键」）手改 config 或改任务 layer.json，会把 parentUrl 写成 `.../layer.json`，得到全程 HTTP 200、零报错、高程被当 heightmap 解错的地形。

修法：把 :80 的默认值改成 `http://localhost:5000/terrain/base`，并把 CLAUDE.md:146 / database.py:100-104 的「必须是目录」警告复述一句（或链过去）。

> 验证：read-only（grep terrain_base_parent_url in src/core/database.py → :105；read CLAUDE.md:146；read docs/reference/terrain/README.md:30）

**reference 正文停在底图植入之前** — `docs/reference/terrain/cesiumjs-loading.md:42, :50, :56, :57`

:42「单任务切片的 `layer.json` 里带 `parentUrl` 指向全球 base」；实际 dem_task_tiler.py:199-207 —— 底图可用时走 `graft_base_into` + `merge_base_availability`（CLAUDE.md:137：`parentUrl` 被**移除**），只有底图不可用的兜底分支才 `patch_layer_json_parent`。:50「本节描述的是**待修的代码缺陷**，不是设计。代码修复后应删除本节」，:56「两条管线都以 `min_level=0` 调切片器（`src/services/terrain_tiling/dem_task_tiler.py:59`）」；实际 dem_task_tiler.py:154 是 `min_level = 8 if base_dir is not None else 0`，而 :59 这一行现在是 `normals: bool = False`。docs/reference/terrain/README.md:16 已给这条加了「默认路径上已消解，兜底路径上依旧」的状态头，cesiumjs-loading.md 自己没有。

后果：reference/ 自称「与代码不一致就是缺陷」（docs/reference/README.md:3）。读者会照 §3 去查一个默认路径上根本不存在的 z0–4 遮蔽问题，甚至照 :76 的「临时规避」手改 layer.json 把 available[0..4] 清空——而现在那五层装的是植入进来的真底图瓦片，清空等于把底图弄没。

修法：给 §3 加上与 terrain/README.md:16 同款状态头（仅兜底路径成立），§2 改成「底图可用时目录自包含、无 parentUrl；不可用时才有级联」，:56 的行号引用换成符号名（`tile_dem_task_dir` 里的 `min_level`）。

> 验证：read-only（read dem_task_tiler.py:142-207、docs/reference/terrain/README.md:14-26）

**INSTALL.md 把 GDAL 钉死成 3.8.4，与三处矛盾** — `docs/guides/INSTALL.md:55`

INSTALL.md:55 `uv pip install --no-build-isolation GDAL==3.8.4`。requirements.txt:24 给的装法是 `pip install --no-build-isolation "GDAL==$(gdal-config --version)"`，:17-23 明说「绑定版本【跟随机器】…钉任何一个具体值都会在另外两处触发卸掉重编」；README.md:85 用的正是 `$(gdal-config --version)`；同一份 INSTALL.md:117 又写 `GDAL==$(gdal-config --version)`，:120 写「装什么具体版本都不必回填」。CLAUDE.md:43 记录开发机是 3.11.4、CI noble 是 3.8.4。

后果：开发机 libgdal 是 3.11.4，照 :55 装会用 3.8.4 的 sdist 去对 3.11 的头文件现编，属于文档自己反对的那种操作；即使编过，`scripts/check_gdal.py` 只查范围也不会拦。CLAUDE.md:45 与 docs/README.md:67 都把这个话题的「唯一主人」指给 INSTALL.md，而主人这一行是错的。

修法：把 :55 换成 `uv pip install --no-build-isolation "GDAL==$(gdal-config --version)"`（与 :117、requirements.txt:24、README.md:85 逐字一致）。

> 验证：read-only（read requirements.txt:17-28、README.md:77-90、INSTALL.md:49-57 与 :113-120）——未实际执行安装


### 测试网破洞（本次首次做变异实测）

**三条用例断言的是测试文件里手抄的副本，4/4 变异存活** — `tests/test_terrain_stage_progress.py:116（_make_stage_cb_from_manager），用例在 :149 / :169 / :185`

`_make_stage_cb_from_manager()` 的 docstring 逐字写着「复刻 dem_task_manager._run_tiling_job 里的 tiling_stage 闭包」，函数体（tests/test_terrain_stage_progress.py:129-146）把节流、edge 判定、`if not socketio: return`、`try/except Exception: pass` 全部重写了一遍。除 `_PROGRESS_EMIT_MIN_INTERVAL` 这一个常量外，三条用例不接触任何生产代码。

后果：生产闭包 src/services/dem_task_manager.py:551-572 完全无回归保护。四条各自对应一个已被 docstring 点名的故障形态：(1) `edge = fraction <= 0.0 or fraction >= 1.0` 被改坏 → 首帧/末帧被节流吃掉，界面在 merge/overview 阶段开始时零反馈、结束后停在中途（正是这个特性当初要修的症状）；(2) 节流被删 → GDAL 原生回调每层多次触发 emit 风暴；(3) `except Exception` 收窄 → emit 异常穿透进 GDAL 进度回调，GDAL 视为「用户请求中止」，物化失败、产物被删、作业记 failed；(4) `_STAGE_LABELS.get(phase, phase)` 被删 → 前端显示裸 'merge'/'overview'。

修法：删掉 `_make_stage_cb_from_manager`，改成从真实管理器上取闭包：用 `DemTaskManager.__new__` 构造实例、monkeypatch `tile_dem_task_dir` 捕获传进去的 `params.stage_cb`（tests/test_terrain_stage_progress.py:69 的 test_stage_cb_is_threaded_through_the_tiler 已经打通了这条取法），再对拿到的真 callback 跑现有三条断言。

> 验证：变异实测（沙箱副本 /tmp/tf_mut，仓库零改动）：对 src/services/dem_task_manager.py 的真 tiling_stage 打 4 个变异，跑 tests/test_terrain_stage_progress.py + test_terrain_api.py + test_fix_socketio_events.py(+test_fix_dem_terrain_hardening.py / test_i18n.py)，**4/4 全部 SURVIVED**：edge→False（42 passed）、删节流（42 passed）、except 收窄（58 passed）、stage_label→phase（64 passed）。

**`POST /api/config/reset` 可以完全不重置而全绿** — `src/routes/api.py:877-899（reset_config），调用点 src/routes/api.py:890`

全库 grep `config/reset|reset_config` 在 tests/ 下零命中。`config_manager.reset_to_defaults()` 本身有单测（tests/test_config_manager.py::test_reset_to_defaults，把它改名即红），但**路由到 manager 的接线**没有任何用例。

后果：用户在配置页点「恢复默认」→ 前端收到 `{"success": true, "message": "Configuration reset to defaults"}` 并弹成功提示，而配置一条都没变。这是静默错误反馈：唯一的现象是「点了没用」，日志里也没有任何东西（成功路径不记日志）。该端点还兼管清空 proxy_url / Earthdata 凭据（见 :881-883 的 docstring），失败同样无声。

修法：在 tests/test_config_manager.py 或 tests/test_pipeline_endpoints.py 加一条：改掉两个可观察的键（如 tile_servers + contour_default_interval）→ POST /api/config/reset → 断言 200 且 GET /api/config 两个键都回到 DEFAULT_CONFIGS 的值。

> 验证：变异实测：`config_manager.reset_to_defaults()` → `pass`，跑 tests/test_config_manager.py + test_config_form_submittable.py + test_fix_config_path_validation.py + test_fix_config_secrets_to_browser.py + test_basemap_source.py + test_tile_url_config.py + test_contour_style_overrides.py → **SURVIVED，181 passed in 24.44s**。对照：把 `ConfigManager.reset_to_defaults` 本体改名 → CAUGHT（tests/test_config_manager.py::test_reset_to_defaults 变红）。

**本地地形「线程起不来」的回补路径零测试** — `src/services/local_terrain_task_manager.py:503-521（_mark_running_task_failed），SQL 在 :513-514`

`UPDATE local_terrain_tasks SET status='failed' ... WHERE id=? AND status='running'`。把 WHERE 里的 `'running'` 改成一个永不匹配的值后，7 个最相关的测试文件全绿。对照：同一形态的 DEM 路径有 tests/test_fix_dem_start_thread_failure.py 专门守，map 路径有 tests/test_fix_ghost_row_on_delete.py 守 —— 三条管线里只有 local 这条没有。

后果：`th.start()` 抛异常（线程数耗尽 / 内存压力）时任务行停在 running 且永不恢复。源码注释自己写明后果：「再次 start 被状态检查拒、delete 也被拒，只能重启进程靠孤儿恢复解开」。UI 上是一条永远转圈、点什么都被拒的幽灵任务。

修法：照 tests/test_fix_dem_start_thread_failure.py 的写法加一条：monkeypatch `threading.Thread.start` 抛异常 → 调 `start_tiling` → 断言 (a) 抛出，(b) 行 status == 'failed' 且 error_message 含 'tiling thread failed to start'，(c) active_tasks / stop_flags 里没有残留 key。

> 验证：变异实测：`AND status='running'` → `AND status='__mut__'`，跑 tests/test_local_terrain_api.py + test_fix_ghost_row_on_delete.py + test_fix_stranded_running_task.py + test_fix_infra_e.py + test_local_terrain_schema.py + test_orphan_recovery.py + test_task_deletion.py → **SURVIVED，111 passed in 23.62s**。

**`/api/config/proxy_status` 整个端点零测试** — `src/routes/api.py:1215-1257（proxy_status），返回体拼装在 :1249-1256`

grep `proxy_status` 在 tests/ 下零命中。该端点是 `proxy_autodetect.get_state()` / `reset_state()` 的唯一消费者（两者在 tests/ 里也从未按名出现）。把返回体里的 `'auto_enabled': auto_enabled` 反转后，5 个代理相关测试文件全绿。

后果：配置页的代理面板会说反话而无人发现：`auto_enabled` 决定开关的显示状态与「立即检测」按钮是否可点；`effective_source` 的三元式（:1253-1254）在 manual 为空且 effective 为空时要给 ''，写错就会显示一个不存在的来源。POST 分支还是同步的强制重探测（最坏二十几秒），它对 `auto_detect_enabled == False` 必须回 400（:1237）——这条分支同样没有任何用例。

修法：用 isolated_app 加三条：(1) auto_detect 关闭时 POST → 400；(2) GET 在 proxy_url 已配置时 `effective_source == 'manual'` 且 `manual` 已脱敏；(3) GET 在什么都没配时 `effective == ''` 且 `effective_source == ''`。三条都不需要真出网（`resolve_from_config(wait_s=0)` 不等后台探测）。

> 验证：变异实测：`'auto_enabled': auto_enabled` → `not auto_enabled`，跑 tests/test_proxy_autodetect.py + test_basemap_proxy_route.py + test_system_proxy.py + test_config_form_submittable.py + test_i18n.py → **SURVIVED，135 passed in 13.03s**。


## 主评审自己跑的：真实浏览器

上一份评审把「真实浏览器驱动」列进了「本次未评审」。本次用无头 Chromium 驱动了跑起来的
服务端（`python app.py`，dev server），下面每一条都是**观察到的**，不是读码推出来的。

### 两条产品硬约束在运行期成立

| 约束 | 实测 |
|---|---|
| 离线 | 首页完整加载共 **101 个请求，全部同源** —— 12 个 `/static/vendor`、16 个 `/static/js`、1 个 css、4 个 socket.io、4 个管线 API、55 张底图瓦片、6 个 Cesium worker 的 `blob:`。远程请求 **0**，`requestfailed` **0**，console error/warning **0** |
| WGS-84 底图 | 55 张瓦片全部打 `/basemap/{z}/{x}/{y}`，浏览器侧看不到任何上游地址；地球在 z3 正常出图（Esri 影像） |

### 焦点与键盘：面板本身是对的，弹窗一层是坏的

- 面板本身合格：打开配置面板后连按 **25 次 Tab，零次逃出面板**；关闭的 `#historyPanel` 是
  `display:none`（不可聚焦）；Esc 关闭后焦点正确回到触发它的 `.map-panel-btn`；
  `aria-pressed` 与视觉态在 11 个控件上全部一致（面板按钮用 `map-panel-btn--active`、
  chip 用 `.active`，两套命名各自成对）。
- **但弹窗一层没让位**（= `TemplatesCssI18n` 与 `FrontendApp` 那条 P1，两人都声明「没在真实
  浏览器里驱动过」，这里补上）：打开配置面板 → 点「浏览」开 `#pathBrowserModal` → 按一次
  Esc，实测 `modalOpen: ["pathBrowserModal"] → []`、`panelOpen: ["configPanel"] → []`，
  `.modal-backdrop` 归 0、`body.className` 清空 —— **两层一起关掉**。
- 顺带修正了那条 P1 原文的三处未经验证的断言：面板内**已输入但未保存的值不丢**
  （把 `UNSAVED-EDIT-PROBE` 写进 `#default_save_path`，Esc 塌陷后重开仍在）；
  **滚动位置也不丢**（`#configForm` 设 `scrollTop=300`，塌陷后重开仍是 300 —— 但这是
  **Chromium 的实测行为**，CSSOM 并不保证 `display:none` 的盒子保留滚动位置，别当成产品属性）；
  **状态筛选同理不丢**（chip 的 class 与 `aria-pressed` 都在 DOM 里）。真正丢的是：面板被关、
  hash 被 `replaceState` 抹掉（后退键回不来）、焦点掉回 `<body>`。Tab 那一半（键盘用户根本
  到不了目录列表与 `#pathBrowserSelect`）不依赖以上任何一条，是这条 P1 更重的那一半。

### 选区四至：一条规则，三个入口，三种答案（+ 后端第四种）

前端刻意允许跨反经线矩形（`static/js/map.js:1566-1567` 的注释写明「west=170/east=-170
这类跨 180° 经线的矩形照样放行」），后端 `validate_bbox` 则**永远**拒绝它。实测：

| 输入 | 前端 | 状态栏 | 后端 |
|---|---|---|---|
| north=200 | 拒（纬度必须在 ±90° 之间） | — | — |
| south=-200 | 拒 | — | — |
| north == south | 拒（零高） | — | — |
| **east=400** | **收** | **「已选区域 300.000° × 20.000°」** | 400 `east (400.0) must be between -180 and 180` |
| **east=-170 / west=170** | **收** | **「已选区域 -340.000° × 1.000°」（负数）** | 400 `east (-170.0) must be greater than west (170.0)` |

纬度校了、经度没校（`map.js:1476` 与 `:1586` 只判 `> 90 / < -90`，两处都没有经度量级判据）。
跨反经线那一格更完整地展示了这条链的分裂：

1. 绘制层**故意**放行（有注释背书）；
2. 状态栏算出一个**负宽度** `-340.000°` 并当成正常读数显示；
3. 瓦片预估框知道它必挂，写着「选区跨反经线，后端会拒绝该四至，无法预估瓦片数」——
   但这句话对 `east=400` 也照说，而 400 不是「跨反经线」，是越界；
4. 「创建下载任务」按钮**不禁用**（`disabled: false`）；
5. 点下去后端 400，前端把后端的**英文**原文直接弹进中文界面：
   `创建任务失败: east (-170.0) must be greater than west (170.0)`。

`FrontendMap` 分片独立发现了同一处并补上了我没测到的两条：鼠标在地图上单击（不拖动）
能造出零面积选区且预估框照报「约 6 张瓦片」；提交前的 `_wrapLngEast(east)` 会把用户填的
190 改写成 -170，于是**报错里的数字用户从来没输入过**。

### 三条管线共用的「已运行」读数，对其中两条是错的

`total_running_seconds` 只有 tile 管线在写（`src/routes/api.py:579,597,615` 给另外三条一律
`NULL AS total_running_seconds`），前端 `static/js/tasks.js:614-620` 的兜底分支是
`elapsedSeconds = Date.now() - started_at`，**不看 status、不看 completed_at**。
而 dem 与 contour 都支持暂停/恢复（`task_list.js:154` 只把 `local_terrain` 排除在外），
且两者的 resume 都会**重写 `started_at`**（`dem_task_manager.py:214`、
`contour_task_manager.py:659` 的 UPDATE 同时覆盖 pending 与 paused）。于是：

- 暂停中的 DEM/等高线任务，「已运行」**包含全部暂停时长且一直在涨**；
- 恢复之后，前面所有段的耗时**全部丢失**，ETA 跟着一起偏。

终态行不受影响（`task_list.js:229` 对非 live 行显示创建日期而不是耗时）。
实测：造一条 `started_at` 为三天前的 paused DEM 任务，`GET /api/history_all` 回
`{"task_type":"dem","status":"paused","started_at":"2026-08-06T15:28:11+00:00","total_running_seconds":null}`
—— 前端据此显示「已运行 3 天」。

`api.py:556-560` 的注释已经描述了这个兜底的存在，但把它写成「否则恒显示 0 秒」，
没有记下它对 paused 恰好是错的。

## 上一轮修复的复查（没有一条回归）

上一份评审当日修完的 4 条 P0 与 17 条 P1，本次由对应分片逐条回看，全部仍然成立，其中五条
做了实测复验：

| 上一轮条目 | 本次复验 |
|---|---|
| **T1** 瓦片索引上界改半开区间 | `intersecting_tile_range` 的修复**两个轴都完整且是唯一调用点**。实测 DEM 四至精确落在瓦片边界时，z2/z8/z11 三档下范围内零不相交瓦片、范围外零遗漏 |
| **T2** 底图迁移 `.part` + `os.replace` | 构造 `user_version=2` 的旧库，在 `os.replace` 成功之后、`conn.commit()` 之前注入异常模拟强杀：落地 `user_version=2`、config 仍是旧路径、目录已物理搬走 —— 重启后重新收敛，无半棵树 |
| **B1** GDAL 闸门 | `python scripts/check_gdal.py` → `GDAL check OK (spec GDAL>=3.8,<4, installed 3.11.4, _gdal_array present)`，exit 0。**但闸门仍然不在任何 CI 路径上**（见 P2） |
| **P1#1/#2** 搁死补偿 | `tasks` / `dem_tasks` / `contour_tasks` 三张表的补偿确实接上了。**漏了两张**（见 P1） |
| **监听器泄漏** `map.js:150` | 确已修好且修得对：处理器提成具名 `_onSplashError`，摘除那一行**排在** `if (!splash) return` 之前 |

另外两条产品硬约束的**测试防线**也做了变异实测：往 `templates/base.html` 塞一个
`fonts.googleapis.com` 的 `<link>` → 被抓；往 `style.css` 顶部塞 `@import url(fonts.googleapis…)`
→ 被抓。这两道是真的在咬人。

## 做得好的部分（不是客套）

- **地形切片**：`intersecting_tile_range`（:192-216）的半开区间修复**两个轴都完整、且是唯一调用点**。实测：DEM 四至精确落在瓦片边界（45..46 × 44..45，对 z≥2 恒为整数分割）时，z2/z8/z11 三档下范围内**零**不相交瓦片、范围外**零**遗漏相交瓦片；全球 (-180,-90,180,90) 在 z3 正确给出 (0,15,0,7)。`_tile_ranges`（:1400-1403）是模块内唯一消费者，无第二份实现。
- **瓦片管线**：「两个 worker 抢同一行」和「暂停的任务被恢复两次」都是关的：start_task 把「读状态 → UPDATE ... WHERE status IN ('pending','paused') 且 rowcount!=1 就抛 → 登记线程」整段放在同一个 _state_lock 临界区里（:438-498），并发第二发要么撞 thread_alive 门、要么撞状态门。task_deletion.delete_task_row 也复用同一把锁并额外用 `thread.ident is None` 补上「已登记但还没 start()」那段窗口。
- **DEM 管线**：颗粒命名数学本身是对的。实跑覆盖 N/S/E/W 零填充、0 与 ±180/±90 边界、南半球西南角约定、全球 bbox（64800 块）：`(-1,-1)→S01W001`、`(0,-1)→N00W001`、`(-90,-180)→S90W180`、`(89,179)→N89E179` 全部符合 SRTM/Copernicus 的西南角命名；`tiles_for_bbox` 的 `north-eps` / `east-eps` 半开区间处理让整度边界不会多算一行一列（`north=45,south=44,east=46,west=45` → 恰好 1 块 N44E045）。
- **等高线管线**：临时目录纪律实测过关：成功、并行、以及「进入前就已置停止标志」三条路径跑完，系统临时目录里 `contour_warp_*` 净增均为 0。清理在 `finally` 里，且 rmtree 前先把 `ctx.band/att_band/ds/att_ds` 显式置 None（Windows 文件锁），`onexc`/`onerror` 按运行时版本分派、失败留 warning 不静默吞。warp 自身那一段另有独立的 `except: rmtree; raise`。
- **栅格探测**：`raster_probe` 根本不打开 GDAL Dataset，也不读一个像素——它只解释浏览器用 File.slice 读出的几 KB 头部标签（模块 docstring :1-9 把分工写清楚了）。所以「10 GB 栅格会不会被全分辨率读一遍」这个担心在这个模块上不成立，`MAX_INSPECT_BODY = 1 << 20`（:28）还专门把请求体压到 1 MiB，理由写在注释里（全局 2 GiB 上限套在这条接口上等于允许对方先让服务端缓存 2 GiB 再被文件数上限拒掉）。`osr` 惰性 import、缺 GDAL 时降级成「只报原生坐标范围」而不是报错。
- **删除/墓碑**：**任务 id 不会被复用，所以陈旧 pending_deletions / retained_outputs 行不可能命中新任务。** 四张任务表全是 `INTEGER PRIMARY KEY AUTOINCREMENT`（database.py:421/529/666/723），sqlite 的 AUTOINCREMENT 语义保证单调。实测：删掉最大 id=3 后再插入，新行拿到 id=4（no reuse）。这条是本次要证伪的假设之一，证伪成立。
- **库与入口**：T2（底图迁移半拷贝）的修复经实测确认完整。构造 user_version=2 的旧库 + 完整旧底图目录，在 os.replace 成功之后、conn.commit() 之前注入异常模拟强杀：RUN1 落地 user_version=2 / cfg 仍是旧路径 / 目录已物理搬到 assets/；RUN2（下次启动）识别出 old_dir 不在、new_dir 在，跳过搬运、补上 config 与 user_version=3，瓦片 7/0/0.terrain 完好。半应用状态可自愈，零数据丢失。
- **api 路由**：MAP_CONFIG_KEYS 与 map.js 的读取点**精确相等**，不多不少。逐点核对：map_center_lat/lng（map.js:290-291）、map_initial_zoom（:292）、default_zoom_min/max（:356-357）、default_save_path（:686）；全仓 static/js 再无第二处读页面级全局 config（config.js/task_list.js 里的 `config.` 分别是 i18n 键与 Vue app.config，不是这个全局）。无缺键、无多余键。
- **底图路由**：z/x/y 在任何网络动作之前校验，实测过硬：/basemap/25/0/0、/basemap/3/8/0、/basemap/3/0/8、/basemap/0/0/1、/basemap/-1/0/0、/basemap/99/1/1 六种越界形态全部 404，累计上游调用数 0（basemap_static.py:176-180）。
- **前端 map**：上次评审记的唯一一处监听器泄漏（map.js:150 的 splash window error）**确已修好且修得对**：处理器提成具名 _onSplashError(:131)，:164 挂上、:175 摘掉，而且摘除那一行**排在** `if (!splash) return`(:181) 之前——正是原缺陷的那条泄漏路径。本轮把 map.js 全部 28 处 addEventListener 逐个过了一遍（node 计数 28 add / 3 remove），其余全部是一次性 init 挂在常驻元素上，或挂在随元素一起被 remove 的临时 input 上（_beginBoundsEdit :1446/:1454），无新增泄漏。
- **前端状态层**：**单一数据源成立**：12 个文件里没有第二份任务数组/Map。tasks.js 的 `latestContourTasks`（:14）是唯一的例外，但它是 `loadActiveTasks` 内部与 `syncContourPreviewFromLatest` 原子刷新的传递缓冲——它的两个读取点（map.js:2232 经首屏 Promise、tasks.js:141 在赋值之后）都不可能读到过期值，实测无漂移路径。
- **模板/CSS**：离线不变量完好且是被机器守住的：模板与 style.css 里零 @import、零 CDN、零远程字体/脚本/样式引用；static/vendor/fonts/fonts.css 的 12 个 @font-face 全部 src: url(相对文件名.woff2)；字体一律走 --font-display / --font-mono 令牌，全文件没有一处裸写 'Inter' 或 'JetBrains Mono'。style.css 第 1-4 行还专门记下「刻意不写 @import」以及它原先指向 fonts.googleapis.com 这段历史。
- **构建**：版本链**此刻是一致的**（实测）：`src/core/config.py:41` APP_VERSION='0.2.14' == RELEASE_NOTES.md 首行 `## v0.2.14`；CHANGELOG 顶部 v0.2.13 正是设计中的「归档上一版」；v0.2.14 标签尚未创建，push-release.sh 的「标签已存在则中止」不会误伤。0.2.14 这次 bump 没有引入任何不一致。
- **文档**：README「API 端点」一节与 `src/routes/*.py` 双向一致：逐条比对 12 个蓝图共 40 条路由（main 3 / api 19 / dem 7 / terrain 2 / local_terrain 4 / contour 8 / 四个静态蓝图 9），README 既没有幽灵条目也没有漏记，含 `/api/config/verify_tile_url`、`GET|POST /api/config/proxy_status`、四条 hillshade 与 `/basemap/<z>/<x>/<y>`。这不是运气：tests/test_docs_claims.py:139 与 :149 是双向棘轮。
- **测试套件**：两条 P0 产品不变量都有真正咬人的防线，已用变异实测确认：往 templates/base.html 塞一个 fonts.googleapis.com 的 <link> → CAUGHT；往 static/css/style.css 顶部塞 @import url(fonts.googleapis.com) → CAUGHT（tests/test_css_contract.py::test_no_css_under_static_reaches_out_to_the_network，专门一条）；往 BASEMAP_PRESETS 加一个标着 wgs84=True 的高德卫星预设 → CAUGHT（tests/test_basemap_source.py::test_no_gcj02_source_is_preset）；把 client_descriptor 的 url 换成真上游地址 → CAUGHT（test_client_descriptor_never_leaks_the_upstream_url[esri]）。WGS-84 回退链另有两条棘轮（tests/test_basemap_proxy_route.py:395 与 :682，后者对每一个可配置源各钉一遍）。

## 本次未评审

- `static/vendor/` 下的第三方库（Cesium 1.143.0 / Vue 3.5.13 / Bootstrap 5.3.0 / socket.io）本身；
  本次只在需要判定行为时从 vendored bundle 里抽过三段实现（Bootstrap 的 modal ESC 处理器与
  FocusTrap 注册、Cesium 的 `requestRenderAfterFrame` 订阅点）。
- `CHANGELOG.md` 顶部约 80 行之外的历史条目；`docs/{superpowers,archive,notes}` 正文
  （`docs/README.md` 已声明为非权威快照）。
- 一次完整 Nuitka 构建，以及三平台产物的冒烟。
- 真实存量库上的迁移：仓内 `data/map_downloader.db` 的任务表仍为空，迁移结论来自在 /tmp 构造的
  旧库，不是真实用户库。
- 端到端真跑一条下载/切片作业：本次浏览器驱动只做了只读交互与被拒的创建请求，**没有**真的
  建过任务、没有落过产物（避免污染用户的 `downloads/` 与库）。因此四条管线的**产物正确性**
  仍然只有单测与分片实测背书，没有一次真实全流程。
- 安全姿态：按 2026-08-08 用户裁定整体划出范围，未复审。

## 建议修复顺序

1. **两条 P0**。T2（拼接短路）更便宜：拼接前查该 zoom 还有没有 failed 行，一处同时堵住
   「拼出残缺图」与「把残缺图当成品保留」。T1（外推假地形）要动 `available` 的判据，
   建议先落「覆盖率低于阈值不写进 available」，并在收尾日志/UI 上把外推面积占比说出来 ——
   今天这条链完全无信号。
2. **搁死补偿的两个洞**（`dem_terrain_jobs`、`local_terrain_tasks`）。同一张网补齐即可，
   并把 `task_cleanup.py:124-126` 那句已被证伪的注释改掉。这一类是「用户只能重启程序」，
   在桌面工具上等于卡死。
3. **`~` 展开的三种解释**。一行改动（校验侧去掉 `.expanduser()`），消掉一个「保存时 200、
   运行时必炸」的配置键。
4. **配置页那条反话**（底图与代理）。改一句文案，直接影响本项目最高频的现场问题；
   顺手给 `tests/test_docs_claims.py` 加反向断言。
5. **前端四至收敛成一个函数**（north>south、|lat|≤90、|lon|≤180、east>west），三个入口共用，
   并把「后端会拒」的状态改成禁用提交按钮 + 本地化文案。
6. **测试网的四个洞**，尤其 `test_terrain_stage_progress.py` 那三条 —— 它们今天是负资产：
   看起来有防线，实际 4/4 变异存活。
7. **P2 里的四条治理项**：`check_gdal.py` 接进 CI、`build.yml` 补 `fail-fast: false`、
   底图瓦片缓存加源标识、`CLAUDE.md` 的三角化/法线与 WGS-84 两节重写。

## 附录 A：P2 全表（66 条）

| 区 | 问题 | 位置 |
|---|---|---|
| 地形切片 | 两个三角化后端的 u/v 量化口径不一致（截断 vs 四舍五入），auto 下相邻瓦片公共边顶点位置对不齐 | `src/services/terrain_tiling/cesiumlab_terrain.py:821-822` |
| 地形切片 | _tile_normals 的 docstring 把一个已经修掉的缺陷写成「现存、本轮不修」，并给出与现实相反的实测数字 | `src/services/terrain_tiling/cesiumlab_terrain.py:1160-1172` |
| 地形切片 | ensure_base_unpacked 的「失败一律抛 RuntimeError」契约漏掉了分卷 glob + stat 这一段 | `src/services/terrain_tiling/base_terrain.py:327-328` |
| 瓦片管线 | _write_progress_batch 异常路径没有 rollback：失败批次的语句留在未提交事务里，退回队列后下一次 flush 再执行一遍，计数被重复累加 | `src/services/task_manager.py:851` |
| 瓦片管线 | pause_task 在 commit 之后才去查 stop_flags，会把停止标志设到**新一轮**执行的 flag 上；新 worker 立刻停、行留在 running、fail_stranded 把它判 failed | `src/services/task_manager.py:628` |
| 瓦片管线 | 搁死补偿写给用户的错误信息说「可以重新开始这个任务」，而 start/resume/pause 三个入口都拒绝 failed —— 死胡同 | `src/services/task_cleanup.py:128` |
| 瓦片管线 | 只有 completed 分支累计运行时长：失败的任务「运行时长」显示 0，整轮实际耗时被吞 | `src/services/task_manager.py:1744` |
| 瓦片管线 | 拼接阶段把整层网格物化三份（tiles_at_zoom 列表 + 全部 Future/队列项 + georef_paths），抵消下载路径刻意做的惰性化 | `src/services/download_engine.py:1111` |
| DEM 管线 | Earthdata 凭据缺失/错误的可操作提示到不了用户手里：任务级只写一句纯计数，dem_files 没有任何路由暴露，也没有前置检查 | `src/services/dem_task_manager.py:977` |
| DEM 管线 | 最后一次重试退避期间暂停，颗粒被判 failed 而不是 pending —— 直接违反引擎自己写的 C4 约定 | `src/services/dem_download_engine.py:309-333` |
| DEM 管线 | ASTGTM 覆盖范围守卫南北不对称：N83 瓦片（83°–84°N，整块在 83°N 上界之外）仍然生成颗粒名 | `src/services/dem_granules.py:98-107` |
| DEM 管线 | start_task 缺少 TaskStillStoppingError 契约：暂停后最长约 30 秒内点「恢复」会被告知「已在运行」 | `src/services/dem_task_manager.py:203-204` |
| 等高线管线 | spawn 出来的渲染 worker 没有任何 logging 配置，渲染热路径的全部 warning 都进不了应用日志 | `src/services/contour_engine.py:846-849` |
| 等高线管线 | contour 的 start_task 在「上一轮线程还在收尾」时报「已在运行」，与界面显示的「已暂停」直接矛盾——tile 管线为此专门加的 TaskStillStoppingError 没有同步过来 | `src/services/contour_task_manager.py:649-651` |
| 等高线管线 | warp 与建金字塔阶段完全没有协作停止点，stop_flag 要等预处理跑完才被看见 | `src/services/contour_engine.py:704-706` |
| 等高线管线 | 晕渲预览每渲染一次就在任务产物目录留下一个 preview_hillshade.tmp.png.aux.xml，而代码注释断言「PNG 驱动不写 .aux.xml 边车」 | `src/services/hillshade_preview.py:130-137` |
| 等高线管线 | _MAX_WIDTH 只封宽不封高，稀疏 VRT 会产出 1200×37200 的预览 PNG | `src/services/hillshade_preview.py:94-98` |
| 等高线管线 | 整条 ASTWBD 水体图层是不可达代码：两个构造器把 water 硬编码成 0，而表的默认值是 1 | `src/services/contour_task_manager.py:491 与 612` |
| 等高线管线 | contour_workers 是一个用户无法写入的配置键，而引擎注释把它写成 4 worker 上限的官方逃生口 | `src/services/contour_engine.py:801` |
| 本地地形/栅格探测 | `geo_validation.resolve_output_dir` 生产侧零调用，而它的 docstring 自称是「读历史数据的兼容入口」——正是全仓明令禁止它承担的角色 | `src/services/geo_validation.py:137,148-149` |
| 本地地形/栅格探测 | 底图预热的解压/就位目标写死 assets/，而所有消费方读的是 `terrain_global_base_path` 配置键 | `src/services/base_terrain_warmup.py:89,139` |
| 本地地形/栅格探测 | 可选子系统的预热失败会把整个服务端启动带崩，与同一子系统「底图不可用绝不让作业失败」的既定约定相反 | `src/services/base_terrain_warmup.py:114-120` |
| 本地地形/栅格探测 | 本地地形上传只查扩展名，等高线在建任务时就用 GDAL 打开——同一条上传规则两条管线已分叉 | `src/services/local_terrain_task_manager.py:163-168` |
| 清理/删除/配置 | delete_files=true 被护栏拦下时，产物目录失去全部 DB 引用（pending 行先插后删，retained_outputs 只在 delete_files=false 那支写） | `src/services/task_deletion.py:141` |
| 清理/删除/配置 | 「目录不存在」被当成「已删除」：盘没挂载时 pending_deletions 与 retained_outputs 的行一次启动就被销掉 | `src/services/task_cleanup.py:321` |
| 清理/删除/配置 | 「要求删文件但没删」被降级成「调用方没要求删文件」，接口对此一字不提 | `src/services/local_terrain_task_manager.py:697` |
| 清理/删除/配置 | terrain_static 的注释断言了一条已经被撤销的配置校验（terrain_global_base_path 的根目录约束） | `src/routes/terrain_static.py:88` |
| core/库/入口 | 底图迁移的文件搬运跑在 init_database 的写事务里，也跑在启动主线程上 | `src/core/database.py:333-354` |
| core/库/入口 | contour_files 每个新建库都多出一条完全重复的唯一索引，兼容分支也在新库上白跑一遍 | `src/core/database.py:789-801` |
| api/main 路由 | POST /api/config/proxy_status 的 reset_state() 绕开了 autodetect 的单轮守卫 —— 实测两轮探测并发跑，最后收工的那轮覆盖状态 | `src/routes/api.py:1238` |
| api/main 路由 | PUT /api/config 用 str(value) 收 JSON 值 —— 布尔 true 落库成 'True'，而 terrain_vertex_normals 的读取侧是大小写敏感的 == 'true'，法线被静默关掉 | `src/routes/api.py:827` |
| api/main 路由 | 超大整数（?page= / <int:task_id>）直达 SQLite，OverflowError 被通用 except 吞成 500，该是 400/404 | `src/routes/api.py:413` |
| 管线路由/底图 | /basemap 硬关环境变量代理，而下载引擎与配置页探测都靠 trust_env 读它 —— 关掉自动探测的用户会得到「下载能用、底图是蓝球」，正是本路由要消灭的分叉 | `src/routes/basemap_static.py:117-125 vs src/services/proxy_autodetect…` |
| 管线路由/底图 | download_source 分支替用户编造 credit='© Google' 与 max_level=21，而同一函数的自定义模板分支对同一个未知量如实报 None/'' | `src/services/basemap_source.py:126-132` |
| 管线路由/底图 | active_basemap 的「配置被改过就别报回退」防线在 custom / download_source 上失效，界面会挂一条从未发生的回退提示并按错误的 max_level 建图层 | `src/routes/basemap_static.py:167` |
| 管线路由/底图 | 替补源返回 404 会中止整条回退链 —— 配置的源明明有这块图也不再尝试 | `src/routes/basemap_static.py:231-233` |
| 管线路由/底图 | 为消除「每瓦片三条 sqlite 连接」加的 5 秒 TTL 缓存只解决了 3/5：每张底图瓦片仍固定新开 2 条连接读代理配置 | `src/routes/basemap_static.py:185 → src/services/proxy_autodetect.py:517` |
| 管线路由/底图 | 地形静态路由声称支持 Range 却忽略 Range，并且比另外三条同类路由少了 Content-Length / ETag / Last-Modified | `src/routes/terrain_static.py:48` |
| 管线路由/底图 | osm 是可写入、可服务的第 5 个 basemap_source 取值，但配置页 <select> 没有它 —— 页面渲染出「一个都没选中」，下次保存会静默把用户的选择改回 esri；CLAUDE.md 关于预设表的断言也已过期 | `src/services/basemap_source.py:83-88` |
| 管线路由/底图 | 代理自动发现串行验证候选，候选数量无上限、整轮无时间预算：PAC 环境下第一个下载任务会白等 25 秒再放弃，而结果 30 秒后才出来 | `src/services/proxy_autodetect.py:410-412` |
| 前端 map | 删除选区 / 建任务后复位：移除 Cesium 实体没有 requestRender()，蓝框和四个角点手柄会继续画在屏幕上 | `static/js/map.js:508` |
| 前端 map | contourPreviewTasks 是 TaskStore 之外的第二份状态，只增不删：删掉的等高线任务在左下角预览面板里永远留着一颗幽灵按钮 | `static/js/map.js:2139` |
| 前端 map | 三个选区入口的校验口径互不相同、且都比后端 validate_bbox 宽：鼠标单击能造出零面积选区，手输能造出 丨经度丨>180 | `static/js/map.js:578-600` |
| 前端 map | 被隐藏的 number 输入框仍带 min/max，切换类型后浏览器原生校验会静默拦下 submit——同款陷阱已经为 required 修过一次，min/max 没跟上 | `static/js/map.js:676` |
| 前端 map | _applyManualBounds 精心交接的焦点，会被一帧之后的 rAF 重渲染打回 <body>——而这条路正是键盘用户唯一的选区入口 | `static/js/map.js:1597-1616` |
| 前端应用层 | 幽灵行：DELETE 返回后才送达的 task_progress 会把已删任务整行插回时间流并重建活动集条目，且此后没有任何路径能清掉它 | `static/js/tasks.js:437` |
| 构建/发版 | GDAL 闸门 scripts/check_gdal.py 不在任何一条 CI 路径上；Windows/macOS 的 `gdal=3.8` 硬编码是版本策略的第二处实现 | `.github/workflows/build.yml:110` |
| 构建/发版 | build.yml 的 matrix 没写 `fail-fast: false`，默认 true —— 与文件顶部为「不留半份产物的 Release」而特意设的 `cancel-in-progress: false` 直接冲突 | `.github/workflows/build.yml:22-35` |
| 构建/发版 | certifi 是死依赖：src/ 无任何引用，`--include-package-data=certifi` 因此是 no-op，而注释声称「certifi CA 证书」已打进产物 | `requirements.txt:16` |
| 构建/发版 | Werkzeug 是 src/ 的直接 import，却既没进 requirements.txt 也没有任何上界 —— 与该文件自己写下的「必须钉版本否则同一 commit 打出的包不同」政策矛盾 | `requirements.txt` |
| 构建/发版 | 产物数据哨兵 APP_DATA_SENTINELS 漏掉全球底图分卷 —— 恰恰是唯一一个「丢了也照常 200、用户只看到地形不对」的资产 | `nuitka_build.py:30-34` |
| 文档真值 | CLAUDE.md 完全没有记录两条产品硬约束里的「离线」这一条，也没有 CSS 与日志两节——而三者都有测试在强制 | `CLAUDE.md` |
| 文档真值 | CLAUDE.md 没有记录三档预设这套新机制的单一事实源与 effective_maxzoom 语义 | `CLAUDE.md:140-147` |
| 文档真值 | docs/reference/README.md 的 file:line 引用已失效（差 88 行） | `docs/reference/README.md:19` |
| 文档真值 | global-base-build.md 两处 cesiumlab_terrain.py 行号引用完全错位（差 1000+ 行） | `docs/reference/terrain/global-base-build.md:57, :77` |
| 文档真值 | cesiumjs-loading.md 的 terrain_static 路由行号已过期 | `docs/reference/terrain/cesiumjs-loading.md:40` |
| 文档真值 | tiling-presets-measured.md 三处行号偏移 9~14 行 | `docs/reference/terrain/tiling-presets-measured.md:209, :227, :370` |
| 文档真值 | README 的 /api/terrain/dem/<id>/start 只写了 maxzoom，漏掉 quality 与 vertex_normals 两个入参 | `README.md:212` |
| 测试套件 | conftest 的两道沙箱护栏在正常路径成立，但被一次模块重导入就整个失效——且没有棘轮防止有人这么写 | `tests/conftest.py:99-119` |
| 测试套件 | 进度攒批的「同一时刻只允许一个 flush」串行化不变量零测试 | `src/services/task_manager.py:1218` |
| 测试套件 | 栅格范围的「非有限值」守卫零测试——它挡的正是「响应体裸 Infinity、卡片一片空白且不报错」 | `src/services/raster_probe.py:170` |
| 测试套件 | 两条无断言的看门狗用例：注释承诺「不起线程」，代码里那个 `parent_pid <= 0` 守卫删掉照样全绿 | `tests/test_process_watchdog.py:29` |
| 测试套件 | test_style_preview_lru_cache_hits_on_repeat 在同一进程里跑第二遍必红——依赖一个从不清理的进程级 lru_cache | `tests/test_contour_api.py:127` |
| 测试套件 | 唯一一处未加守卫的环境依赖断言：无 git 工作树时它变成空断言，而它的全部意义就是不让豁免名单变成万能通行证 | `tests/test_docs_claims.py:369-373` |
| 测试套件 | test_fix_* 沉积比上次评审更重了：67/155（43.2%），六个合并组可把 30 个文件收成 6 个且零断言损失 | `tests/ 全目录` |
| 模板/CSS/i18n | basemap_source='osm' 是校验层放行的一等预设，配置表单却没有对应 option——面板显示成「Esri」，按一次保存就把它静默改写掉 | `templates/_config_content.html:231-233` |

## 附录 B：P3 全表（49 条）

| 区 | 问题 | 位置 |
|---|---|---|
| 地形切片 | ungraft_base_from 的调用被 base_dir 是否可解析挡住，而它要防的「写穿共享底图」并不依赖这个条件 | `src/services/terrain_tiling/dem_task_tiler.py:146-147` |
| 地形切片 | 同一个 progress_cb 在 build_terrain 内部有两套异常契约 | `src/services/terrain_tiling/cesiumlab_terrain.py:1443-1446` |
| 地形切片 | 多幅输入的物化阶段完全不看 stop_flag | `src/services/terrain_tiling/cesiumlab_terrain.py:1356-1357` |
| 瓦片管线 | _run_task 在调用搁死补偿之前就把自己从 active_tasks 摘掉，留下一个新 worker 可以合法登记的窗口 | `src/services/task_manager.py:795` |
| 瓦片管线 | 共享 DownloadEngine 实例上的 _collect_batch_results / _batch_retry_config 只写不还原，__init__ 里声明的默认契约在首个地图任务之后就再也回不去 | `src/services/download_engine.py:195` |
| DEM 管线 | coverage_bbox 全项目无生产调用方，且在退化输入上与 tiles_for_bbox 不一致（docstring 声称「逐字一致」） | `src/services/dem_granules.py:64-87` |
| DEM 管线 | ASTWBD 水体链路整条不可达：颗粒名函数与 ASTWBD.001 的 base URL 分支都没有调用方，list_att_tifs 恒返回空 | `src/services/dem_granules.py:129-136` |
| DEM 管线 | dem_files.retry_count 恒为 0、local_path 无读取方 | `src/services/dem_task_manager.py:183-187` |
| 等高线管线 | 渲染路径全程走 pyplot 的全局状态，而它在多线程/多任务下没有任何必要 | `src/services/contour_engine.py:417、486` |
| 等高线管线 | style_for_task 对 background 用直接下标，与自己 docstring 承诺的「缺列视为未覆盖」矛盾，缺键时 500 而不是 400 | `src/services/contour_task_manager.py:253` |
| 本地地形/栅格探测 | `start_tiling` 从库里读回的 quality 非法时，报错点名的是一个用户根本没提交过的请求字段 | `src/services/local_terrain_task_manager.py:439` |
| 本地地形/栅格探测 | raster_probe 把客户端没送的字段当成已确定的事实回报 | `src/services/raster_probe.py:346` |
| 清理/删除/配置 | _sweep_cache_part_files 会在启动路径上枚举整个瓦片缓存；限深注释与「毫秒级」的说法都不成立 | `src/services/task_cleanup.py:594` |
| 清理/删除/配置 | remove_task_dir_if_safe 把「越界」和「检查时抛了异常」压成同一个 False，而消费者据此永久销账 | `src/services/task_cleanup.py:286` |
| core/库/入口 | idx_local_terrain_files_status 全项目零消费，且与三张兄弟表的索引形制相反 | `src/core/database.py:715-718` |
| core/库/入口 | release_instance_lock() 没有任何生产调用点，docstring 却声称「正常退出路径用」 | `src/core/single_instance.py:123-141` |
| core/库/入口 | 重量级 import 抛异常时 spinner 线程不会停，错误信息被 \r 动画覆盖 | `app.py:66-77` |
| core/库/入口 | M10 存量归一只覆盖 output_path，两张表的 output_dir 落在外面，而它的消费者没有 output_path 消费者那道绝对路径闸 | `src/core/database.py:234` |
| api/main 路由 | 「任务不存在」在 GET/DELETE 是 404，在 start/pause/resume 是 400 | `src/routes/api.py:238` |
| api/main 路由 | 服务端 connect 时 emit 的 'connected' 事件全站没有任何监听方 | `src/routes/socketio_events.py:43` |
| api/main 路由 | GET /api/history 已无任何前端调用方，且它的 ?status=active 与两个兄弟端点语义相反 | `src/routes/api.py:385` |
| api/main 路由 | GET /api/tasks 的响应形状随 ?status=active 变化，同一字段两种时间格式 | `src/routes/api.py:142` |
| api/main 路由 | 错误响应体没有统一信封，且 /api/* 的框架级错误（404/405/413）回的是 HTML | `src/routes/api.py:1043` |
| api/main 路由 | index() 的异常兜底路径丢掉了 active_basemap()，会报配置源而不是实际出图的源 —— 与它上面 6 行的注释直接矛盾 | `src/routes/main.py:150` |
| 管线路由/底图 | 每个源声明的 max_level 只下发给浏览器，路由侧不执行 —— 越级请求照样打到上游 | `src/routes/basemap_static.py:176-180` |
| 管线路由/底图 | /api/terrain/dem/<id> 把「任务不存在」和「任务在但从没切过片」压成同一个 200 {"job": null}，与同一 id 在 /api/dem/tasks 上的 404 口径相反；它的 400 分支是死代码 | `src/routes/terrain_api.py:64-71` |
| 管线路由/底图 | local terrain 详情无条件回一个 layer_url，无论切片有没有产出；且全项目零消费者 | `src/routes/local_terrain_api.py:109-110` |
| 前端 map | 注释自相矛盾：TASK_TILE_LIMIT 被说成「后端硬上限、超限提交前就拦下」，同文件另两处和后端都说它只是软阈值 | `static/js/map.js:31-35` |
| 前端 map | resetForm() 把 #mapStyle 拨回默认值，却不刷新 #mapStylePreview 缩略图，两者从此不一致 | `static/js/map.js:1243-1268` |
| 前端应用层 | 同一个计数单位有两套 i18n key，且同一行任务在生命周期中途会从一套切到另一套 | `static/js/task_list.js:174 vs static/js/tasks.js:249` |
| 前端应用层 | 创建/启动/删除按钮无 in-flight 守卫：双击「开始」在任务真的启动之后弹一条红色「启动失败」，双击删除会叠出两个确认框并在删除成功后弹「删除失败」 | `static/js/task_list.js:71 / static/js/task_list.js:100` |
| 前端应用层 | 小地图信息框读的是 `task.downloaded` / `task.total`，这两个字段只有 /api/history_all 的行才有；socket 插进来的新任务点开是 undefined/undefined | `static/js/history.js:294` |
| 前端应用层 | task_store 里两处死代码，其中 clear() 与 remove() 的语义不一致，长得像能用 | `static/js/task_store.js:154 (clear) / static/js/task_store.js:125 (up…` |
| 前端应用层 | 注释指向一个已经不成立、且照做会引入 bug 的转义约定 | `static/js/history.js:200` |
| 前端应用层 | 分页条仍在生成内联 onclick，与同目录两处显式记录过的「内联处理器已删」决定相矛盾 | `static/js/history.js:244` |
| 构建/发版 | 两个构建脚本里的 Nuitka「补装」分支永远不会执行 —— 一个看起来在兜底、实际不可达的 fallback，还有一条测试在守它 | `build.sh:31-34` |
| 构建/发版 | 本地构建会把 `assets/terrain/.base_unpack_*` 与 `.base_unpack.lock` 打进产物：排除项只挡了 base_z8，而 CI 的清理只在 build.yml 里、build.sh/build.bat 没有对应步骤 | `nuitka_build.py:392` |
| 构建/发版 | CHANGELOG 归档没有任何机器校验，BUILD.md 的发版步骤里也没有这一步 —— 而版本链已经漂过一次（CHANGELOG 有 v0.2.9，git 里没有 v0.2.9 标签） | `CHANGELOG.md:3` |
| 文档真值 | README 使用指南没提「数据处理」表单里的切片档位与地形光照两个控件 | `README.md:126` |
| 文档真值 | README 列举的 LOG_LEVEL 取值少一个合法值 | `README.md:337` |
| 测试套件 | conftest 的护栏说明与 src 现状已经对不上：它说生产侧没有 mtime 门槛，而生产侧半年前就加了 | `tests/conftest.py:103-105` |
| 测试套件 | test_watchdog_starts_daemon_thread 起了一条永不停止的看门狗守护线程，泄漏到整个会话 | `tests/test_process_watchdog.py:40-47` |
| 测试套件 | 两处 catch-and-pass 会把被测代码的崩溃变成不可见 | `tests/test_task_lifecycle_state.py:232-234` |
| 模板/CSS/i18n | 四个真实表单控件没有可访问名称（只有 placeholder，或只有一个没绑定的组标题） | `templates/_config_content.html:206+216` |
| 模板/CSS/i18n | --statusbar-clearance 的令牌注释把「滑出面板」列为消费者——正好与面板必须全高 bottom:0 这条不变量相反 | `static/css/style.css:116-118` |
| 模板/CSS/i18n | [hidden] 覆盖规则是死代码，而且它的注释把层叠机制讲错了 | `static/css/style.css:1002-1006` |
| 模板/CSS/i18n | 承重注释里残留 Leaflet 时代与 PyInstaller 的旧事实 | `templates/base.html:39` |
| 模板/CSS/i18n | 两条英文 placeholder 绕过文案目录；目录自校验只扫中文，英文方向没人管 | `templates/_config_content.html:377,383` |
| 模板/CSS/i18n | 三个模板 id 钩子无任何消费者；一个未使用令牌；一处该收成类的内联 style | `templates/index.html:40(#mapToolbar)、:76(#mapPanelTriggers)、:77(#proc…` |
