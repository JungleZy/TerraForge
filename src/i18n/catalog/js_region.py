"""WAVE B 前端新增能力的界面文案（§5.1 区域导入与地名搜索、§6.2 瓦片源向导、
§13-3 缺口决策、§4.5 任务日志）。

一个模块对应一处来源是本目录的约定，而这五组前缀的消费者是**跨文件**的
（js.gaps.* 同时被 static/js/task_center.js、task_list.js、history.js、map.js 用，
js.region.* 同时被 map.js 与 drop_process.js 用），所以按**特性**而不是按文件
归拢：拆成五个模块的结果是每个都只有几条、且改一个特性要开五个文件。

命名口径：
  js.region.*   —— 导入的区域（多边形/孔洞/跨反经线）与它的估算读数
  js.search.*   —— 地名搜索
  js.wizard.*   —— 瓦片源向导
  js.gaps.*     —— 缺块与决策（补漏 / 接受并导出 / 导出 MBTiles）
  js.tasklog.*  —— 任务日志查看器与诊断包
"""

MESSAGES = {
    # ---- 区域导入（§5.1）-------------------------------------------------
    'js.region.unnamed': {
        'zh': '未命名区域',
        'en': 'Unnamed region',
    },
    # 三条几何事实。它们是「这不是一个矩形」的全部内容，而每一条都会让实际
    # 下载量与用户看四至读数得出的直觉差出一大截。
    'js.region.facts.polygon': {
        'zh': '多边形',
        'en': 'polygon',
    },
    'js.region.facts.polygons': {
        'zh': '多边形 ×{n}',
        'en': '{n} polygons',
    },
    'js.region.facts.holes': {
        'zh': '孔洞 {n}',
        'en': '{n} holes',
    },
    'js.region.facts.antimeridian': {
        'zh': '跨反经线',
        'en': 'crosses the antimeridian',
    },
    'js.region.import.applied': {
        'zh': '已用「{name}」作为下载区域（{facts}）',
        'en': 'Using "{name}" as the download region ({facts})',
    },
    'js.region.import.failed': {
        'zh': '区域导入失败：{error}',
        'en': 'Region import failed: {error}',
    },
    # 服务端回了 200 但 body 里没有可用的几何 —— 不静默当成「没导入」：
    # 那会让用户以为文件不被支持，而真正的问题在响应上。
    'js.region.import.bad_payload': {
        'zh': '服务端没有返回可用的区域几何',
        'en': 'The server did not return a usable region geometry',
    },
    # 一次只取一个：选区是单值，拖多个边界文件没有「合并」语义。
    'js.region.drop.only_first': {
        'zh': '一次只能用一个区域文件，已使用「{name}」',
        'en': 'Only one region file is used at a time — took "{name}"',
    },
    # 服务端只回机器码，文案在这里（它没有语种上下文）。每条都必须说出
    # **对用户意味着什么**，不能只复述发生了什么：这些警告的收件人正准备拿这
    # 个区域去下几十 GB 瓦片，「知道出了什么事」而不知道「会怎样」等于没警告。
    'js.region.warning.crosses_antimeridian': {
        'zh': '该区域跨过反经线，下载时会拆成东西两段分别枚举瓦片',
        'en': 'This region crosses the antimeridian; tiles are enumerated as two '
              'separate east/west spans',
    },
    # 下面两条是同一个失败模式的两半：坐标系不明 -> 按 WGS84 经纬度原样用。
    # 源数据若是投影坐标系（米制），区域会落在地球上完全不相干的地方，而界面
    # 上看起来一切正常 —— 用户只会在下完之后发现下错了地方。
    'js.region.warning.missing_crs': {
        'zh': '压缩包里没有 .prj，坐标已按 WGS84 经纬度原样使用；'
              '源数据若是投影坐标系，区域会落在错误的位置，请在地图上先核对',
        'en': 'No .prj in the archive — coordinates were used as-is, assuming '
              'WGS84 lon/lat. If the source is in a projected CRS the region will '
              'land in the wrong place; check it on the map first',
    },
    'js.region.warning.unreadable_crs': {
        'zh': '压缩包里的 .prj 解析不了，坐标已按 WGS84 经纬度原样使用；'
              '源数据若是投影坐标系，区域会落在错误的位置，请在地图上先核对',
        'en': 'The .prj in the archive could not be parsed — coordinates were used '
              'as-is, assuming WGS84 lon/lat. If the source is in a projected CRS '
              'the region will land in the wrong place; check it on the map first',
    },
    # 「少了东西」的一类：说清楚少的是什么，用户才判断得出这个区域还能不能用。
    'js.region.warning.skipped_non_polygon_features': {
        'zh': '文件里的点/线要素已跳过，只使用了面要素',
        'en': 'Point and line features were skipped; only polygon features were used',
    },
    'js.region.warning.encoding_fallback_gb18030': {
        'zh': '该 GeoJSON 不是 UTF-8，已按 GB18030 解码；名称若是乱码请另存为 UTF-8 再导入',
        'en': 'This GeoJSON is not UTF-8; it was decoded as GB18030. If names look '
              'garbled, re-save the file as UTF-8 and import again',
    },
    'js.region.warning.extension_content_mismatch': {
        'zh': '扩展名与文件实际内容不符，已按内容解析',
        'en': "The file extension does not match the file's actual content; it was "
              'parsed by content',
    },
    # 四至读数对导入区域是**派生值**（外接矩形），改它会造出几何与 bbox 不一致
    # 的选区：地图上还是原来那个多边形，服务端按几何算张数。
    'js.region.bbox_readonly': {
        'zh': '导入区域的四至由几何决定，不能直接改。要换范围请重新导入或改用矩形框选',
        'en': 'The bounds of an imported region come from its geometry and cannot be '
              'edited. Import another file or switch to rectangle selection',
    },
    'js.region.estimate.pending': {
        'zh': '正在按区域几何计算瓦片数…',
        'en': 'Counting tiles over the region geometry…',
    },
    'js.region.estimate.failed': {
        'zh': '瓦片数计算失败：{error}',
        'en': 'Tile count failed: {error}',
    },
    # 磁盘预算**永远带数字**（与后端 BudgetVerdict 同一条约定）：「空间不足」
    # 四个字对用户没有操作性，他要知道的是还差多少。
    'js.region.budget.ok': {
        'zh': '需要约 {required}，可用 {free}',
        'en': 'needs about {required}, {free} free',
    },
    'js.region.budget.short': {
        'zh': '磁盘不足：需要约 {required}，可用 {free}，还差 {shortfall}',
        'en': 'not enough disk: needs about {required}, {free} free, {shortfall} short',
    },

    # ---- 地名搜索（§5.1）-------------------------------------------------
    # 未配置时控件渲染成禁用 + 这句提示。**不静默隐藏**（用户会以为功能不存在，
    # 而它只是没配地址），也不内置默认服务商（那等于替用户决定把地名查询发给
    # 一个第三方，而这是个可离线部署的工具）。提示必须点名配置项**和它在哪一栏**：
    # 只写「请在配置页填写 geocoder_url」时配置页里压根没有这个输入框，用户被指
    # 到一个死胡同（用户实测反馈）。输入框已补上，落点由
    # tests/test_geocoder_config.py 钉住。
    'js.search.disabled_hint': {
        'zh': '地名搜索未启用：请到配置页的「地名搜索」一栏填写 geocoder_url（地理编码服务地址）',
        'en': 'Place search is off: set geocoder_url (geocoding service address) '
              'under "Place search" on the settings page',
    },
    'js.search.no_results': {
        'zh': '没有匹配的地点',
        'en': 'No matching places',
    },
    'js.search.failed': {
        'zh': '地名搜索失败：{error}',
        'en': 'Place search failed: {error}',
    },
    # 「搜索中…」。上游往返实测约 2 秒，改造前这段时间面板压根不显示，用户
    # 看到的是「敲完字什么都不发生」。现在敲第一个字面板就开、先摆这一句。
    'js.search.searching': {
        'zh': '搜索中…',
        'en': 'Searching…',
    },
    # 地理编码服务给的 bbox 未必满足本应用的四条选区规则（跨反经线的国家、
    # 退化成一个点的地名都真实存在）。说清是哪个地点、哪一条规则不过。
    'js.search.unusable_bbox': {
        'zh': '「{name}」的范围不能直接作为选区：{reason}',
        'en': 'The extent of "{name}" cannot be used as a selection: {reason}',
    },

    # ---- 坐标直达（2026-08）----------------------------------------------
    # 关键词先本地过一遍坐标识别，命中就不打上游。meta 那两条**必须**把「谁是
    # 纬度谁是经度」写出来：不带半球字母的两个数只能靠约定判序（默认纬度在前，
    # 与地图应用复制出来的一致），判反就是跑到地球另一边 —— 明写出来用户一眼
    # 能看见，静默猜测才是危险的那一种。
    'js.search.coord.point_title': {
        'zh': '跳到此坐标',
        'en': 'Jump to these coordinates',
    },
    'js.search.coord.point_meta': {
        'zh': '纬度 {lat} · 经度 {lon}（只移动视角，不改选区）',
        'en': 'Lat {lat} · Lon {lon} (moves the camera only, selection unchanged)',
    },
    'js.search.coord.bbox_title': {
        'zh': '用这组四至作为选区',
        'en': 'Use these bounds as the selection',
    },
    'js.search.coord.bbox_meta': {
        'zh': '西 {west} · 南 {south} · 东 {east} · 北 {north}',
        'en': 'W {west} · S {south} · E {east} · N {north}',
    },

    # ---- 结果类型筛选（2026-08）------------------------------------------
    # 片子按本次结果实际出现过的 kind 动态生成，所以这里只是**可能**用到的
    # 译名表；认不得的 kind 原样显示，不编一个「其它」把两种类型糊成一类。
    'js.search.filter.all': {
        'zh': '全部',
        'en': 'All',
    },
    'js.search.kind.country': {'zh': '国家', 'en': 'Country'},
    'js.search.kind.state': {'zh': '省 / 州', 'en': 'State'},
    'js.search.kind.county': {'zh': '地区 / 县', 'en': 'County'},
    'js.search.kind.city': {'zh': '城市', 'en': 'City'},
    'js.search.kind.district': {'zh': '城区', 'en': 'District'},
    'js.search.kind.locality': {'zh': '聚落', 'en': 'Locality'},
    'js.search.kind.street': {'zh': '道路', 'en': 'Street'},
    'js.search.kind.house': {'zh': '地点', 'en': 'Place'},
    'js.search.kind.administrative': {'zh': '行政区', 'en': 'Administrative'},
    'js.search.kind.place': {'zh': '地名', 'en': 'Place name'},
    'js.search.kind.other': {'zh': '其它', 'en': 'Other'},

    # ---- 最近搜索（2026-08）----------------------------------------------
    # 存 localStorage（tf-place-history，最多 10 条），与主题/强调色同一档。
    'js.search.history.title': {
        'zh': '最近搜索',
        'en': 'Recent searches',
    },
    'js.search.history.clear': {
        'zh': '清除',
        'en': 'Clear',
    },

    # ---- 瓦片源向导（§6.2）-----------------------------------------------
    'js.wizard.label': {
        'zh': '从真实瓦片 URL 生成模板',
        'en': 'Build a template from a real tile URL',
    },
    'js.wizard.placeholder': {
        'zh': '粘贴一条真实的瓦片地址，例如 https://example.com/tiles/10/842/389.png',
        'en': 'Paste one real tile URL, e.g. https://example.com/tiles/10/842/389.png',
    },
    'js.wizard.analyze': {
        'zh': '识别',
        'en': 'Detect',
    },
    # 刻意不写成 {z}/{x}/{y}：那是 t() 的占位符语法，调用处不传值时会原样漏出，
    # 而这句话里它们本来就该是字面的「z、x、y 三个槽位」。
    'js.wizard.analyzing': {
        'zh': '正在识别 z / x / y 三个槽位的位置…',
        'en': 'Detecting where the z / x / y slots are…',
    },
    'js.wizard.need_url': {
        'zh': '先粘贴一条真实的瓦片地址',
        'en': 'Paste a real tile URL first',
    },
    'js.wizard.detected': {
        'zh': '模板：{template}（方案 {scheme}，样例瓦片 z{z}/x{x}/y{y}）',
        'en': 'Template: {template} (scheme {scheme}, sample tile z{z}/x{x}/y{y})',
    },
    'js.wizard.failed': {
        'zh': '识别失败：{error}',
        'en': 'Detection failed: {error}',
    },
    # 有警告时模板不自动落进条目框：那三类警告（凭据外泄、x/y 顺序是猜的、
    # 疑似 TMS）判错都不会报错，只会让用户下出一整套废图或把密钥写进库。
    # 让他为这个决定按一下，是这些警告存在的全部意义。
    'js.wizard.apply_anyway': {
        'zh': '已读过上面的提醒，仍然使用此模板',
        'en': 'I read the warnings above — use this template anyway',
    },
    'js.wizard.applied': {
        'zh': '已填入模板：{template}',
        'en': 'Template filled in: {template}',
    },

    # ---- 缺块与决策（§13-3）----------------------------------------------
    # 徽章是**常驻**的：completed_with_gaps 的产物可用，用户会拿去做后续处理，
    # 几个月后回到列表时「已完成」与「已完成（有缺口）」必须一眼分得开。
    'js.gaps.chip': {
        'zh': '缺 {n}',
        'en': '{n} missing',
    },
    'js.gaps.chip_title': {
        'zh': '这份产物缺 {n} 张瓦片。可以补漏重跑，也可以接受缺块继续用',
        'en': 'This artifact is missing {n} tiles. You can refill them or accept '
              'the gaps',
    },
    'js.gaps.loading': {
        'zh': '正在读取缺块明细…',
        'en': 'Loading gap details…',
    },
    'js.gaps.none': {
        'zh': '没有缺块记录',
        'en': 'No recorded gaps',
    },
    'js.gaps.pair': {
        'zh': '{label} {count}',
        'en': '{label} {count}',
    },
    # 四个结局名对应后端 TileOutcome 的四个非成功值。
    'js.gaps.outcome.no_data': {
        'zh': '上游无数据',
        'en': 'No data upstream',
    },
    'js.gaps.outcome.retryable_failure': {
        'zh': '可重试失败',
        'en': 'Retryable failure',
    },
    'js.gaps.outcome.permanent_failure': {
        'zh': '永久失败',
        'en': 'Permanent failure',
    },
    'js.gaps.outcome.cache_failure': {
        'zh': '缓存写入失败',
        'en': 'Cache write failure',
    },
    # 这两句是**决定性**的：全是 no_data 时补漏一张也补不回来（no_data 不在
    # RETRYABLE_OUTCOMES 里），该点的是「接受并导出」。
    'js.gaps.explained': {
        'zh': '全部缺块都是上游无数据 —— 补漏不会有收获，再跑一遍还是没有',
        'en': 'Every gap is "No data upstream" — refilling cannot recover any of '
              'them',
    },
    'js.gaps.unexplained': {
        'zh': '含可重试或失败的缺块 —— 补漏有机会补回一部分',
        'en': 'Some gaps are retryable or failed — refilling can recover part of them',
    },
    'js.gaps.decided': {
        'zh': '已作出的决定：{decision}',
        'en': 'Decision on record: {decision}',
    },
    'js.gaps.sample': {
        'zh': 'z{zoom}/{x}/{y} · {outcome} {error}',
        'en': 'z{zoom}/{x}/{y} · {outcome} {error}',
    },
    'js.gaps.load_failed': {
        'zh': '缺块明细读取失败：{error}',
        'en': 'Failed to load gap details: {error}',
    },
    'js.gaps.action.refill': {
        'zh': '补漏',
        'en': 'Refill',
    },
    'js.gaps.action.refill_title': {
        'zh': '只重跑记录在案、且值得重试的那些缺块（上游无数据的不会重跑）',
        'en': 'Re-run only the recorded gaps worth retrying (skips "No data '
              'upstream")',
    },
    'js.gaps.action.accept': {
        'zh': '接受缺块并生成产物',
        'en': 'Accept gaps and produce the artifact',
    },
    'js.gaps.action.accept_title': {
        'zh': '按现状生成产物。产物与历史会永久带缺块标记',
        'en': 'Produce the artifact as-is. The artifact and its history keep a '
              'permanent gap marker',
    },
    # 「成品」而不是「MBTiles」：格式不再由这颗按钮决定 —— 点下去先问服务端这个
    # 任务导得出哪些格式，多于一种时弹选择框。写死格式名的那一版把插件注册的
    # 导出器挡在了界面之外（后端早就把 gpkg 接进同一条路由了）。
    'js.gaps.action.export': {
        'zh': '导出产物',
        'en': 'Export artifact',
    },
    # 缺块明细读取失败后的手动重试。存在的理由：GET /gaps 超时一次，行组件的
    # 三个自动触发点（mounted + status/gap_tiles 两条 watch）之后一个都不会
    # 再响，行会永久停在「正在读取缺块明细…」。这颗按钮是那条死路的唯一出口，
    # 而不装退避轮询是为了不给每一条带缺块的行挂一个后台定时请求。
    'js.gaps.action.retry': {
        'zh': '重试',
        'en': 'Retry',
    },
    'js.gaps.action.retry_title': {
        'zh': '重新读取这个任务的缺块分档明细',
        'en': 'Load this task\'s gap breakdown again',
    },
    # 不可撤销，所以要二次确认，且把数字摆出来。
    'js.gaps.confirm_accept': {
        'zh': '接受 {n} 张缺失瓦片并按现状生成产物？产物与历史会永久带缺块标记，这个决定不可撤销。',
        'en': 'Accept {n} missing tiles and produce the artifact as-is? The artifact '
              'and its history keep a permanent gap marker, and this cannot be undone.',
    },
    'js.gaps.toast.refill_started': {
        'zh': '任务 #{id} 开始补漏',
        'en': 'Task #{id} started refilling',
    },
    'js.gaps.toast.refill_failed': {
        'zh': '补漏失败：{error}',
        'en': 'Refill failed: {error}',
    },
    'js.gaps.toast.accepted': {
        'zh': '已接受 {n} 张缺失瓦片，正在生成产物',
        'en': 'Accepted {n} missing tiles; producing the artifact',
    },
    'js.gaps.toast.accept_failed': {
        'zh': '接受缺块失败：{error}',
        'en': 'Accepting the gaps failed: {error}',
    },
    'js.gaps.toast.exported': {
        'zh': '已导出 MBTiles（{count} 张瓦片）：{path}',
        'en': 'MBTiles exported ({count} tiles): {path}',
    },
    # 通用文案：这颗按钮现在可能在导 mbtiles，也可能在导插件注册的任何格式，
    # 而拉格式表本身失败时连格式都还不知道。原文是「导出 MBTiles 失败」。
    'js.gaps.toast.export_failed': {
        'zh': '导出失败：{error}',
        'en': 'Export failed: {error}',
    },
    'js.gaps.event.pending_decision': {
        'zh': '任务 #{id} 有缺块，等待你决定',
        'en': 'Task #{id} has gaps and is waiting for your decision',
    },

    # ---- 导出格式选择器（§5.3）--------------------------------------------
    # 后端把插件导出器并进了 `POST /api/export/<pipeline>/<id>`，但格式表原先
    # 只在 400 的响应体里出现，前端于是写死 `{format:'mbtiles'}` —— gpkg 有货
    # 也点不到。`GET /api/export/<pipeline>/<id>/formats` 补上读端点之后，
    # 一种格式直接导（手感不变），多种才弹这个框。
    'js.export.confirm.title': {
        'zh': '选择导出格式',
        'en': 'Choose an export format',
    },
    # 说清「导出是追加」是有理由的：用户看到「格式」两个字容易以为是在换输出
    # 格式（那会牵连瓦片目录与 /tiles 预览），而 §5.3 的决定恰恰相反 ——
    # 容器是多出来的一份产物，原料一个字节都不动。
    # 不带 markdown 记号：确认框的正文进的是 textContent，`**追加**` 会原样
    # 显示成一对星号。
    'js.export.confirm.message': {
        'zh': '这个任务有多种可用的导出格式。导出是追加一份产物：原有的瓦片目录与 GeoTIFF 一个字节都不会动。',
        'en': 'This task can be exported to more than one format. Exporting adds an '
              'artifact: the existing tile directory and GeoTIFFs are left byte-for-byte '
              'untouched.',
    },
    'js.export.confirm.format_label': {
        'zh': '导出格式',
        'en': 'Export format',
    },
    'js.export.confirm.ok': {
        'zh': '导出',
        'en': 'Export',
    },
    # 「没有可导出的产物」而不是「导出失败」：这不是一次失败，是这个任务压根
    # 没有任何导出器吃得下的东西（dem / local_terrain 一件产物都不登记）。
    'js.export.toast.nothing_to_export': {
        'zh': '这个任务没有可导出的产物',
        'en': 'This task has no exportable artifacts',
    },
    # 插件导出器分支的成功文案。与 js.gaps.toast.exported 分成两条是因为信息量
    # 不同：mbtiles 的响应带 tile_count（打包器数过每一块瓦片），插件导出协议里
    # 没有让第三方报块数的地方，套那条带 {count} 的文案只会显示「0 块瓦片」。
    'js.export.toast.exported': {
        'zh': '已导出 {format}：{path}',
        'en': 'Exported {format}: {path}',
    },

    # ---- 产物清单（§13-3 / §5.3）------------------------------------------
    # 只在任务详情弹窗里出现。六个形态名对应后端 ArtifactKind 的六个值，
    # 逐个写成完整键字面量、不做前缀拼接：tests/test_i18n.py 的双向闭合按
    # 字面量扫源码，拼出来的键会被判成无人引用而删掉文案（同一形态的先例是
    # 上面的 js.gaps.outcome.* 与 history.js 的 TERRAIN_QUALITY_KEYS）。
    'js.artifacts.loading': {
        'zh': '正在读取产物清单…',
        'en': 'Loading artifacts…',
    },
    # 「没有登记的产物」而不是「没有产物」：任务可能真的产出了文件，只是这一版
    # 之前的任务没有往 artifacts 表登记。说死了会让用户以为磁盘上什么都没有。
    'js.artifacts.none': {
        'zh': '没有登记的产物',
        'en': 'No registered artifacts',
    },
    'js.artifacts.load_failed': {
        'zh': '产物清单读取失败：{error}',
        'en': 'Failed to load artifacts: {error}',
    },
    'js.artifacts.kind.xyz_dir': {
        'zh': 'XYZ 瓦片目录',
        'en': 'XYZ tile directory',
    },
    'js.artifacts.kind.geotiff': {
        'zh': 'GeoTIFF',
        'en': 'GeoTIFF',
    },
    'js.artifacts.kind.mbtiles': {
        'zh': 'MBTiles 容器',
        'en': 'MBTiles container',
    },
    'js.artifacts.kind.terrain_dir': {
        'zh': '地形瓦片目录',
        'en': 'Terrain tile directory',
    },
    'js.artifacts.kind.contour_dir': {
        'zh': '等高线瓦片目录',
        'en': 'Contour tile directory',
    },
    'js.artifacts.kind.dem_dir': {
        'zh': '高程颗粒目录',
        'en': 'Elevation granule directory',
    },
    # 规模行。三段各自可缺（非瓦片产物没有层级，老行没统计过大小），
    # 由 JS 过滤掉空段再用 · 连起来，所以这里每段都是独立的键。
    'js.artifacts.tiles': {
        'zh': '{n} 张',
        'en': '{n} tiles',
    },
    'js.artifacts.zooms': {
        'zh': 'z{min}-{max}',
        'en': 'z{min}-{max}',
    },
    # 产物侧的缺块标记。文案与行上的徽章不同（那里有具体块数，这里只有布尔），
    # 但**共用同一个 .task-gap-chip 长相** —— 用户只需要认一个符号。
    'js.artifacts.gapped': {
        'zh': '带缺块',
        'en': 'Has gaps',
    },
    'js.artifacts.gapped_title': {
        'zh': '这件产物本身带缺块标记。标记跟着产物走，任务行删掉之后它依然在',
        'en': 'This artifact itself carries the gap marker. The marker follows the '
              'artifact and survives deletion of the task row',
    },
    # MBTiles 体检判决（artifact_export 收尾处强制跑一遍 validate_mbtiles，
    # 结论写进 meta.validation）。**判决不阻断导出** —— 绝大多数问题
    # （metadata 少一个可选键、声明 bounds 与实际外包框差一点）不影响文件能用，
    # 所以这里是一条陈述，不是一个错误。
    'js.artifacts.validation.ok': {
        'zh': '已通过 MBTiles 规范校验',
        'en': 'Passed MBTiles spec validation',
    },
    'js.artifacts.validation.problems': {
        'zh': 'MBTiles 规范校验发现 {n} 个问题（文件仍可使用，明细如下）',
        'en': 'MBTiles spec validation found {n} problem(s) — the file is still '
              'usable; details below',
    },

    # ---- 任务日志与诊断包（§4.5）------------------------------------------
    'js.tasklog.errors_only': {
        'zh': '只看错误与警告',
        'en': 'Errors and warnings only',
    },
    'js.tasklog.copy': {
        'zh': '复制诊断摘要',
        'en': 'Copy diagnostics',
    },
    'js.tasklog.copied': {
        'zh': '诊断摘要已复制（已脱敏）',
        'en': 'Diagnostics copied (redacted)',
    },
    # 剪贴板 API 在非安全上下文（http:// 且非 localhost）里直接抛。那时还有
    # 「下载」这条路，所以要说清失败原因而不是只说「复制失败」。
    'js.tasklog.copy_failed': {
        'zh': '复制失败：{error}。可以改用下面的「下载诊断包」',
        'en': 'Copy failed: {error}. Use "Download diagnostics" instead',
    },
    'js.tasklog.download': {
        'zh': '下载诊断包',
        'en': 'Download diagnostics',
    },
    'js.tasklog.line': {
        'zh': '{ts} {level} {message}',
        'en': '{ts} {level} {message}',
    },
    # 三种「空」要说三句不同的话：合成一句「暂无日志」会让第一种情况下的用户
    # 永远等不到日志，也永远不知道去哪儿把它打开。
    'js.tasklog.disabled': {
        'zh': '任务日志已关闭：在配置页打开 task_log_enabled 后新任务才会记录',
        'en': 'Task logging is off: enable task_log_enabled on the settings page and new '
              'tasks will be recorded',
    },
    'js.tasklog.no_file': {
        'zh': '暂无日志：这个任务还没跑过',
        'en': 'No log yet: this task has not run',
    },
    'js.tasklog.empty': {
        'zh': '日志是空的',
        'en': 'The log is empty',
    },
    'js.tasklog.no_errors': {
        'zh': '这一段没有错误或警告',
        'en': 'No errors or warnings in this range',
    },
    'js.tasklog.load_failed': {
        'zh': '日志读取失败：{error}',
        'en': 'Failed to read the log: {error}',
    },
}
