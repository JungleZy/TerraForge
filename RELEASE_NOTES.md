## v0.4.0 —— 插件系统：四类扩展点、四个首发插件，缺省全部关闭

**先说结论：这一版加的是一套插件系统，不是某个具体功能。宿主开出四类扩展点（数据源 / 下载管线 / 导出器 / 任务钩子），并随包带四个首发插件各占一类：天地图数据源、MVT 矢量瓦片下载、GeoPackage 导出、产物元数据 sidecar。所有插件缺省关闭——不去插件面板开启的话，程序行为与 0.3.5 逐字一致。首次启动会做一次数据库迁移（`user_version` 6 → 7）：只新建三张空表（`plugins` / `plugin_tasks` / `plugin_task_tiles`），存量数据一列都不动，已下载的数据、配置、历史全部照旧。**

**插件面板在哪、怎么用**
- 左侧工具条多了一颗「插件」按钮，打开的面板列出全部插件：名称、版本、来源（in-tree / 外部目录）、声明的能力、开关。
- **加载失败的插件也在列表里**，带一条红色失败原因。这是有意的：坏插件不许打穿宿主，但也不许悄悄消失——一个装了却看不见的插件比一个报错的插件更难查。
- 每张插件卡片上有一颗「配置」：点开就是按插件自己声明的 `config_schema` 现渲染的表单（天地图那张只有一个 token 输入框），不合法的键名会**逐键**在对应字段下面报出来。凭据类字段是密码框，且**已保存的值永远不回显**——服务端下发的是哨兵，原样保存 = 不改，清空保存 = 清除。
- 开启带「管线」能力的插件后，面板里可以直接新建它的任务：任务表单按插件声明的参数 schema 现渲染（区域四至 + 插件自己的字段），不需要为每个插件写一份界面。
- 插件任务与四条核心管线的任务同处一个任务中心：同一份列表、同一套状态、同一套缺块决策、同一颗删除按钮。

**四个首发插件**
- **天地图数据源**：影像（`img_w`）与注记（`cia_w`）两个 WMTS 源，开启后出现在下载弹窗的数据源下拉里。需要在插件卡片的「配置」里填自己的 key（`https://console.tianditu.gov.cn/` 申请）。key **只存在插件配置里**，不进任务行、不进指纹、不进日志——换 key 不会让已下载的瓦片失效。填错键名不会静默吞掉：配置过插件自己的 schema 校验，错在哪个键就报哪个键。
- **MVT 矢量瓦片下载**：给一个 TileJSON 地址和层级范围，把矢量瓦片下成单个 `.mbtiles`（`metadata.format=pbf`，带描述图层的 `json` 键，tileserver-gl / QGIS / MapLibre 直接能读）。只下瓦片：style、字体、sprite 都不抓，也不把 pbf 解码成 GeoJSON。
- **GeoPackage 导出**：把已完成任务的栅格产物（GeoTIFF）导出成 `.gpkg`，落在源产物的同级目录。走的是**核心那条导出路由**——`POST /api/export/<pipeline>/<id>`，插件导出器只是往这条路由的格式表里加一行。任务中心的导出按钮会先问 `GET /api/export/<pipeline>/<id>/formats`：**这个任务**真正导得出什么就列什么（拿它的产物登记行对照导出器的 `accepts()` 算出来，不是全局格式表），只有一种就直接导、多于一种才弹选择框。注意 in-tree 只有地图管线登记 GeoTIFF 产物，所以今天能导 `gpkg` 的实际只有地图任务。
- **产物元数据 sidecar**：插件任务成功收尾后，在每个产物旁写一份 `<产物名>.tfmeta.json`（形态、格式、字节数、瓦片数、层级范围、有没有缺块、生成时间），下游工具不用开 SQLite 就能读到产物的关键属性。

**不启用插件时会发生什么：什么都不会**
- 插件缺省 `enabled = 0`。关闭状态下四个扩展点一齐熄灯：数据源列表为空、管线取不到、导出格式表为空、钩子一个都不分发。下载弹窗的数据源下拉、任务中心、导出对话框与 0.3.5 逐字相同。
- 这条由 `tests/test_plugin_acceptance.py::test_all_plugins_disabled_keeps_core_intact` 钉住，不是靠人记得。

---

**给排障和构建的人**

- 宿主全在 `src/plugins/`：`registry.py`（发现 / 启停 / 失败隔离 / 四类能力查询）、`task_manager.py`（**全部插件共用一份**任务管理器，模块级单例）、`task_context.py`（`TaskContext` 门面——插件拿不到任何核心 manager、`socketio` 或数据库连接，这是物理约束不是约定）、`protocols.py`（四类 Protocol + 共享数据类，`PLUGIN_API_VERSION = '1.0'`）、`manifest.py` / `params.py` / `credentials.py`。架构说明见 `CLAUDE.md` 的「Plugin system」一节。
- **插件任务只有一张表** `plugin_tasks`（外加稀疏的 `plugin_task_tiles`），不是每个插件一张；任务中心里的类型标识 `task_type='plugin'` 由 `/api/history_all` 的**第五段 UNION** 现给，表上没有这一列。每任务日志与产物走 `pipeline='plugin'`（已登记进 `src/contracts/artifact.py` 的 `PIPELINES`）。
- **切进核心的两道缝都是通用形状，不是给插件开的后门**：① `TaskManager.create_task` 接受调用方传进来的 `source_snapshot`（已冻结的源快照），没传才按 `style` 现算——核心只认这份合同，不认它出自谁；② `DownloadEngine.get_tile_url` 在发请求前那一瞬把 URL 模板里的 `{credential}` 占位符替换成插件配置里的值。快照、任务行、指纹、日志里永远只有**键名**。
- **外部插件放在 exe 旁的 `plugins/<id>/`**（源码运行时是仓库根），每个目录一份 `plugin.toml`；带二进制依赖的插件把它们放进 `plugins/<id>/vendor/` 并在清单里声明 `requires_abi`（如 `cp312-linux-x86_64`）。`api_version` 主版本或 ABI 不匹配一律拒载，原因显示在面板上。
- **外部插件 = 在本进程里执行任意代码，没有沙箱。** 信任边界是本机 / 局域网，面板顶部就这么写着。只启用你自己看过的插件。
- **打包零改动**：四个 in-tree 插件都是纯 Python（清单是模块级 dict），不带任何随包数据文件，所以 `nuitka_build.py` 的 `APP_DATA_SENTINELS` 与包数据参数一个字没动。可达性靠 `src/app_factory.py` 的预热清单，`tests/test_plugin_nuitka_reachability.py` 在**全新子进程**里断言 `src/plugins/` 下每个模块都真的进了 `sys.modules`（同进程里永远是绿的——每个插件的测试文件都在模块级 import 它）。新增 in-tree 插件要改两处：`registry._BUILTIN` 与那份预热清单。

**这一版有意留下的欠账（八条，按影响排序）**

1. **天地图插件未经真实服务验证。** 手上没有真 key、构建机不打外网，`LAYER` / `TILEMATRIXSET` / `FORMAT` / `max_zoom=18` 全部按天地图公开文档写。**发版前必须用真 key 手工验一张瓦片**（步骤见下）。
2. **插件任务没有「只重跑缺块」。** 瓦片管线可以只重跑可重试的格子，插件任务只能整趟重跑。要补这一条得让 `PipelinePlugin.run()` 接受「只跑这些格子」的入参，属于协议变更，不进 1.0。
3. **区域只能手填四至，接不上地图框选。** 插件任务表单里的 north/south/east/west 是四个文本框；计划里的「在地图上框选后切给插件」没做。
4. **插件的 `i18n.toml` 运行时合并推迟了。** 四个首发插件都没有自定义 UI 文案，参数 `label` 直接用纯字符串。第三方插件想让自己的表单跟界面语言走，得等这条落地。
5. **面板样式全部借 Bootstrap 通用类，`static/css/style.css` 一行没加。** 这是有意的：那份文件正在被前端信息架构改造动着，此时插进一段插件专属样式必然打架。代价是面板的几何与配置页不完全同款。
6. **产物 sidecar 不进 `artifacts` 表。** 删任务会留下孤儿 `*.tfmeta.json`。要不要把 sidecar 登记成产物（登记了就得考虑「元数据的元数据」）是产品决定，待定。
7. **MVT 落进 MBTiles 的是解压后的 pbf 字节。** 服务器带 `Content-Encoding: gzip` 时 aiohttp 会透明解压，落库的是未压缩 pbf。MBTiles 1.3 允许两者，读得动，代价只是库更大——不在这里重新 gzip 是为了不给每块瓦片付一次压缩。
8. **插件凭据缓存是 60 秒的进程内缓存。** 今天瓦片端口与主服务同进程同一个 Flask app（`src/core/tile_server.py` 起的是线程），改配置会立即 `invalidate`，所以换 token 是即时生效的。这条 TTL 只在将来把瓦片服务拆成独立进程时才会变成「最坏延迟 60 秒」。

**发版前补掉的三条（原清单十一条中的第 2、3、11 条）**

- **缺 key 现在说人话，不再是一屏 401。** `GET /api/plugins/sources` 每个源多一个 `credential_ready`——它读的是配置真值而不是「声明过凭据键」这个静态事实（无需凭据的源恒为 `true`，前端一个判据到底，真值不下发）。下拉里未配置的源直接标「（未配置）」，提交时拦下并点名去插件面板的哪个插件填。
- **选插件源后样式下拉不再装作有效。** 选中非内置源时 `#mapStyle` 置灰并给出说明、样式缩略图收起，切回内置源全部还原。历史列表也不再把天地图任务叫「路线图」：`history_all` 的地图段下发快照里的 `source_id`（其余四段补 NULL 对齐），插件源任务显示成「插件源 tianditu:img」。注意置灰的 `<select>` 不进表单提交，`style` 字段仍按内置默认值送出，不是 `undefined`。
- **导出格式选择器做了。** 新增 `GET /api/export/<pipeline>/<int:task_id>/formats`，回的是**这个任务**的事实（`mbtiles` 看管线是否有松散瓦片金字塔，插件格式拿它的产物登记行对照 `accepts()`），不是全局格式表。只有一种格式就直接导、多于一种才弹选择框。全局函数 `exportTaskMbtiles` 随之改名 `exportTask`（干净切换，不留别名）。

**一条行为变更，装卸插件前请知悉**

**卸载插件会连同它保存的配置一起清掉。** 从 `plugins/` 目录移走一个插件、下次启动时，它在数据库里那一行（启停开关 + 配置，**含你填的 token**）会被一并删除；重新装回来是干净的初始状态，需要重填。这样做是有必要的：不清的话，删掉插件 A 再装一个恰好同 id 的插件 B，B 会**直接继承 A 的开关与配置**——包括那个 token。两道保险：`plugins/` 目录整个读不动时（权限、盘没挂上）不清；本轮有任何插件加载失败时也整轮不清（加载失败时程序认不出它的真实 id，照清会误伤）。清理会在日志里逐条报出被删的 id，删到带配置的行时另有一条 warning。

**发版前手工验证（两条，本机跑不了，必须在出包与真 key 到手之后做）**

1. **frozen 插件冒烟**（规格 §15 第 5 条）：`./build.sh` 出包后，
   - 启动 `dist/terraforge/terraforge`，`GET http://localhost:5000/api/plugins` 应列出 4 个 in-tree 插件、`load_error` 全为空 —— 证明 Nuitka 把 `src/plugins/**` 收全了；
   - 把 `tests/` 里那种最小假插件目录（一份 `plugin.toml` + 一份 `plugin.py`，`register()` 返回空 `PluginDefinition()`）拷进 `dist/terraforge/plugins/<id>/`，重启 exe，`GET /api/plugins` 应多出这一条且 `origin` 为 `external` —— 证明 exe 旁的外部插件目录真的被扫到；
   - 再把该目录的 `api_version` 改成 `"2"` 重启，这一条应仍在列表里但带非空 `load_error` —— 证明拒载理由在界面上看得见（§15 第 6 条的 frozen 版）。
2. **天地图真 key 验一张瓦片**：插件面板找到 `tianditu` 卡片 → 点「配置」→ 在 token 框里填真 key → 保存 → 点「启用」，然后框一小块区域按天地图影像源下一个 z10 任务。要确认的是：瓦片真的下下来（不是 401、不是配额错误）、图面对得上位置（`TILEROW`/`TILECOL` 没写反）、日志里**没有** token 明文，且重新打开配置时 token 框里显示的不是真值。

**验证**

- 全量测试 **2921 项通过 / 1 项跳过**（干净树，开发机 Linux 3 分 25 秒；工作树上另有一批与本版无关的在途改动，不计入）。插件系统自带 18 个测试文件（`tests/test_plugin_*.py`、`tests/test_plugins_*.py`、`tests/test_docs_plugin_example.py`），其中 `tests/test_plugin_acceptance.py` 是规格 §15 六条验收标准里能测试化的那四条；`tests/test_docs_plugin_example.py` 钉住 `docs/examples/plugin-hello/` 这个「拷进去就能跑」的承诺不腐烂（走宿主真实装载路径，变异验证过六种腐烂形态都抓得到）。
- 未做真实服务验证的部分已在上面「发版前手工验证」逐条列明。天地图源的参数正确性、frozen 产物的插件可达性两项**尚未**在本机取得证据。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **下载体积**：每个平台仍包含 167 MB 的全球底图分卷（自 v0.2.8 起）。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。程序另会监听 5001 出瓦片，不放行也能用。
- **许可证与第三方声明**：程序目录下的 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`。MIT 只覆盖软件代码，**不授予**任何数据与在线服务的使用权。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/TerraForge/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/TerraForge/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/TerraForge/blob/master/docs/guides/QUICKSTART.md)。
