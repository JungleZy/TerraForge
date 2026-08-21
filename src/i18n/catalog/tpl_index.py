"""templates/index.html（首页：加载动画、左上工具条、下载/处理弹窗、任务面板） 的界面文案。

key 命名：`tpl.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # 首屏加载动画
    'tpl.index.splash.loading': {
        'zh': '页面加载中',
        'en': 'Page loading',
    },
    'tpl.index.splash.stage_init': {
        'zh': '正在初始化地图引擎…',
        'en': 'Initializing map engine…',
    },

    # 左上工具条
    #
    # 2026-08-15 退役四个键（工具条瘦身，9 颗 -> 6 颗）：
    #   · toolbar.zoom_in / zoom_out（「放大」「缩小」）—— 两颗按钮删了，缩放改由
    #     `+` / `-` 快捷键与命令面板的 js.cmdk.zoom_in / zoom_out 承担，那两条
    #     命令自带文案，所以这两个键没有新的引用点。
    #   · toolbar.draw_rect / draw_rect_title（「框选」）—— 工具条那颗是第二个
    #     入口，面板选区段里的 tpl.index.create.draw_rect 才是现在唯一那颗。
    # 区域导入与地点搜索（阶段 2）。两条都放在「框选」旁边:它们是同一件事的
    # 另外两个入口 —— 手画一个矩形 / 导入一份别人给的边界 / 按地名跳过去,
    # 最终都落到同一个 RegionSpec 上。分散到菜单里会让人以为是三套东西。
    #
    # ⚠️ 可见标签**必须是两个汉字**,和旁边五个按钮一样。`.map-panel-btn` 是
    # 固定 32px 宽(连边框 34px)的竖胶囊,`white-space: nowrap` + 容器
    # `overflow: hidden`,而 style.css 的圆角注释就是按「12px 两字标签 = 24px 宽」
    # 算的余量。四个字 48px 装不下,会被**静默裁掉**后半截 —— 实测「导入区域」
    # 显示成「入区」、「地点搜索」显示成「点搜」,没有任何报错。英文那侧有
    # `html[lang="en"] .map-panel-btn { width: max-content }` 兜着不会裁,但这里
    # 仍然给短词,好让两种语种的按钮是同一个形状。
    # 完整说明放在下面的 *_title(即 <button title=...>)里,那才是解释该待的地方;
    # 这两条同时还当 aria-label 用 —— 短名 + 详述 title 正是另外五个按钮的现成体例。
    'tpl.index.toolbar.region_import': {
        'zh': '区域',
        'en': 'Region',
    },
    'tpl.index.toolbar.region_import_title': {
        'zh': '导入 GeoJSON / KML / KMZ / Shapefile / ZIP 作为下载区域（支持多边形与孔洞）',
        'en': 'Import GeoJSON / KML / KMZ / Shapefile / ZIP as the download region (polygons and holes supported)',
    },
    # 地点搜索。2026-08 从工具条挪到顶部居中之后，工具条那三条
    # （place_search / _title / _placeholder）随按钮一起删掉 —— i18n 的
    # 双向检查会把「定义了但没人引用」的键判红，留着就是死重量。
    'tpl.index.search.label': {
        'zh': '搜索地点',
        'en': 'Search places',
    },
    'tpl.index.search.placeholder': {
        'zh': '地名 / 经纬度 / 四至…',
        'en': 'Place, coordinates or bounds…',
    },
    'tpl.index.search.clear_input': {
        'zh': '清空搜索框',
        'en': 'Clear the search box',
    },
    # 2026-08-21 删除登记：'tpl.index.toolbar.lighting'（光照）与
    # 'tpl.index.toolbar.create'（新建）随工具条纯图标化删除——按钮上的
    # 文字标签取消了，名义由 aria-label/title（terrain_lighting /
    # create_title 等完整说法键）承担。
    'tpl.index.toolbar.terrain_lighting': {
        'zh': '地形光照',
        'en': 'Terrain lighting',
    },
    'tpl.index.toolbar.terrain_lighting_title': {
        'zh': '地形光照（需已加载地形预览）',
        'en': 'Terrain lighting (requires a loaded terrain preview)',
    },
    # 「新建」：左列工具条第一组，打开 #createPanel。四条管线（瓦片 / 高程 /
    # 本地地形切片 / 等高线）唯一的创建入口 —— 2026-08-15 起 tpl.index.toolbar
    # .process（任务筛选行右端那颗「处理」）随 #processOpenBtn 一起退役。
    # 工具条 2026-08-21 起纯图标化：按钮上的短文案键（create「新建」）已删，
    # 名义只剩 create_title（aria-label 与 title 的完整说法）。
    'tpl.index.toolbar.create_title': {
        'zh': '新建任务',
        'en': 'New task',
    },
    'tpl.index.toolbar.tasks': {
        'zh': '任务',
        'en': 'Tasks',
    },
    'tpl.index.toolbar.tasks_title': {
        'zh': '任务（活动 + 历史）',
        'en': 'Tasks (active + history)',
    },
    'tpl.index.toolbar.config': {
        'zh': '配置',
        'en': 'Settings',
    },

    # 表单结构文案。「关闭」原先是两个弹窗的 .btn-close 用的
    # （tpl.index.modal.close），弹窗退场后面板头部走 tpl.index.panel.close。
    'tpl.index.form.section_basic': {
        'zh': '基础',
        'en': 'Basics',
    },
    # 「范围与层级」原先是下载弹窗里盖着「地图样式 + 缩放范围」的组头。四条管线
    # 合并后这一段被拆成两组：tpl.index.create.section_source（数据范围 / 来源）
    # 与 tpl.index.create.section_params（按管线切换的参数），所以它退役。

    # 新建任务面板（#createPanel）自己的结构文案。
    'tpl.index.create.pipeline': {
        'zh': '任务类型',
        'en': 'Task type',
    },
    'tpl.index.create.section_source': {
        'zh': '数据范围',
        'en': 'Data extent',
    },
    'tpl.index.create.section_params': {
        'zh': '参数',
        'en': 'Parameters',
    },
    # 无选区时面板里给出的两个入口。两颗按钮都不新起路径：前者点的是工具条那颗
    # 「框选」，后者开的是选区浮层空态里那个手动四至面板。
    'tpl.index.create.draw_rect': {
        'zh': '去框选',
        'en': 'Draw a selection',
    },
    'tpl.index.create.manual_bounds': {
        'zh': '手动输入范围',
        'en': 'Enter bounds manually',
    },
    'tpl.index.form.task_name': {
        'zh': '任务名称',
        'en': 'Task name',
    },
    'tpl.index.form.task_name_placeholder': {
        'zh': '输入任务名称...',
        'en': 'Enter a task name...',
    },
    'tpl.index.form.zoom_min': {
        'zh': '最小缩放层级',
        'en': 'Min zoom level',
    },
    'tpl.index.form.zoom_max': {
        'zh': '最大缩放层级',
        'en': 'Max zoom level',
    },

    # 瓦片 / 高程两条下载管线的字段。tpl.index.download.title（弹窗标题「下载
    # 数据」）与 tpl.index.download.type（radio 组标签「下载类型」）已退役：
    # 标题现在是面板标题 tpl.index.panel.create，而两选一的「下载类型」被四选一
    # 的段控取代，标签是 tpl.index.create.pipeline（「任务类型」）。
    # 下面 type_map / type_dem 两个值仍在用 —— 它们是段控前两枚 chip 的文案。
    'tpl.index.download.type_map': {
        'zh': '地图瓦片',
        'en': 'Map tiles',
    },
    'tpl.index.download.type_dem': {
        'zh': '高程',
        'en': 'Elevation',
    },
    'tpl.index.download.output_format': {
        'zh': '输出格式',
        'en': 'Output format',
    },
    'tpl.index.download.format_tiles': {
        'zh': '瓦片',
        'en': 'Tiles',
    },
    # MBTiles 打包勾选框。**刻意不是「输出格式」的第四个单选值** —— 它与
    # output_format 正交：打包的原料就是那棵松散瓦片树，而同一棵树又是
    # /tiles/<id>/ 预览的数据源，做成单选值就等于选它即删掉原料。
    # hint 那句必须把「目录照常保留」说出来，否则用户会以为勾了就只剩一个文件。
    'tpl.index.download.export_mbtiles': {
        'zh': '同时导出 MBTiles',
        'en': 'Also export MBTiles',
    },
    'tpl.index.download.export_mbtiles_hint': {
        'zh': '任务完成后额外把瓦片打成单文件 .mbtiles 容器。松散瓦片目录照常保留 —— 预览和之后的手动导出都从它出。',
        'en': 'Also pack the tiles into a single .mbtiles container when the task '
              'finishes. The loose tile directory is kept as usual — preview and '
              'later manual exports both read from it.',
    },
    'tpl.index.download.map_style': {
        'zh': '地图样式',
        'en': 'Map style',
    },
    'tpl.index.download.style_standard': {
        'zh': '路网',
        'en': 'Roadmap',
    },
    'tpl.index.download.style_satellite': {
        'zh': '卫星影像',
        'en': 'Satellite imagery',
    },
    'tpl.index.download.style_satellite_labels': {
        'zh': '卫星影像+标注',
        'en': 'Satellite imagery + labels',
    },
    'tpl.index.download.style_roads': {
        'zh': '道路',
        'en': 'Roads',
    },
    'tpl.index.download.style_terrain': {
        'zh': '地形',
        'en': 'Terrain',
    },
    'tpl.index.download.style_preview_alt': {
        'zh': '样式预览',
        'en': 'Style preview',
    },
    'tpl.index.download.style_preview_title': {
        'zh': '当前样式瓦片预览',
        'en': 'Tile preview of the current style',
    },
    # 插件源下拉:默认「内置源」= 走上面那五个样式的存量路径。
    'tpl.index.download.plugin_source': {
        'zh': '数据源',
        'en': 'Data source',
    },
    'tpl.index.download.source_builtin': {
        'zh': '内置源',
        'en': 'Built-in sources',
    },
    # 选了插件源之后「地图样式」下拉被置灰，这一句解释为什么。
    # 不是措辞谨慎的「可能不适用」：带快照的任务取哪张瓦片完全由快照里的
    # url_template 决定（download_engine.get_tile_url），样式码是死参数。
    'tpl.index.download.style_locked_hint': {
        'zh': '插件源自带图层，上面的地图样式选择对它不起作用。',
        'en': 'A plugin source ships its own layer — the map style above has no '
              'effect on it.',
    },
    'tpl.index.download.dem_dataset': {
        'zh': '数据源',
        'en': 'Data source',
    },
    'tpl.index.download.dem_glo30': {
        'zh': 'Copernicus GLO-30（30m，推荐，更干净，免认证）',
        'en': 'Copernicus GLO-30 (30 m, recommended, cleaner, no login)',
    },
    'tpl.index.download.dem_aster': {
        'zh': 'ASTER GDEM v3（30m，需 Earthdata 账号）',
        'en': 'ASTER GDEM v3 (30 m, requires an Earthdata account)',
    },
    'tpl.index.download.dem_hint': {
        'zh': '下方 NUM/SWB 仅 ASTER 适用；选 GLO-30 时忽略。',
        'en': 'NUM/SWB below apply to ASTER only; ignored when GLO-30 is '
              'selected.',
    },
    'tpl.index.download.dem_num': {
        'zh': '同时下载 NUM 质量文件',
        'en': 'Also download NUM quality files',
    },
    'tpl.index.download.dem_swb': {
        'zh': '同时下载 SWB 水体掩膜文件',
        'en': 'Also download SWB water-body mask files',
    },
    'tpl.index.download.section_output': {
        'zh': '输出',
        'en': 'Output',
    },
    'tpl.index.download.output_path': {
        'zh': '保存路径',
        'en': 'Save path',
    },
    'tpl.index.download.output_path_placeholder': {
        'zh': '绝对路径,可点「浏览」选择',
        'en': 'Absolute path, or click Browse to pick one',
    },
    'tpl.index.download.browse': {
        'zh': '浏览',
        'en': 'Browse',
    },
    'tpl.index.download.submit': {
        'zh': '创建任务',
        'en': 'Create task',
    },

    # 本地地形切片 / 等高线两条处理管线的字段。tpl.index.process.title（弹窗标题）
    # 与 tpl.index.process.type（「处理类型」下拉标签）已退役，理由同上：四条管线
    # 现在是同一个段控的四枚 chip。type_local_terrain / type_contour 仍在用。
    'tpl.index.process.type_local_terrain': {
        'zh': '本地地形切片',
        'en': 'Local terrain tiling',
    },
    'tpl.index.process.type_contour': {
        'zh': '等高线瓦片',
        'en': 'Contour tiles',
    },
    'tpl.index.process.source': {
        'zh': '数据来源',
        'en': 'Data source',
    },
    'tpl.index.process.source_upload': {
        'zh': '上传文件',
        'en': 'Upload files',
    },
    'tpl.index.process.source_dem_task': {
        'zh': '已下载的高程任务',
        'en': 'Downloaded elevation task',
    },
    'tpl.index.process.dem_task': {
        'zh': '选择高程任务',
        'en': 'Select elevation task',
    },
    'tpl.index.process.dem_task_hint': {
        'zh': '直接使用该任务已下载的高程文件，无需上传。',
        'en': 'Uses the elevation files already downloaded by that task; no '
              'upload '
              'needed.',
    },
    'tpl.index.process.upload_dem': {
        'zh': '上传高程文件（可多选 .tif/.tiff）',
        'en': 'Upload elevation files (multiple .tif/.tiff allowed)',
    },
    'tpl.index.process.local_terrain_maxzoom': {
        'zh': '最大切片层级',
        'en': 'Max tiling zoom level',
    },
    # 文案要说清「按什么算」，不能只写「自动」：用户看不见基准层级是怎么来的，
    # 就无从判断这一挡和自己手填那个数差在哪里（估算口径见
    # terrain_tiling 的 GeographicTilingScheme.estimate_max_level）。
    'tpl.index.process.local_terrain_maxzoom_auto': {
        'zh': '自动（按源数据分辨率决定）',
        'en': 'Auto (from source resolution)',
    },

    # 档位三档的措辞与任务详情面板（`js_history.py` 的 `js.history.terrain.quality_*`
    # 那组键）保持同一套词：
    # 精细 / 均衡 / 快速，参照物一律写「基准层级」。参照物不能写「默认」——
    # 偏移表（`geo_validation.TILING_QUALITY_OFFSETS`）的 +1/0/-1 是相对基准层级算的，与
    # terrain_quality_preset 当前配成哪一档无关；两处措辞不一致，用户会以为
    # 表单里选的档位和详情里显示的档位是两回事。
    'tpl.index.process.terrain_quality': {
        'zh': '切片档位',
        'en': 'Tiling preset',
    },
    'tpl.index.process.terrain_quality_precision': {
        'zh': '精细（比基准层级多切一级，体积约 3.3 倍）',
        'en': 'Precision (one level above the base level, ~3.3x size)',
    },
    'tpl.index.process.terrain_quality_balanced': {
        'zh': '均衡（基准层级，推荐）',
        'en': 'Balanced (the base level, recommended)',
    },
    'tpl.index.process.terrain_quality_speed': {
        'zh': '快速（比基准层级少切一级，体积约 1/3.3）',
        'en': 'Fast (one level below the base level, ~1/3.3 size)',
    },
    # 首句交代基准层级的**两个来源**：勾着「自动」（出厂默认）时上面那个数字框
    # 是禁用的，基准层级要等切片时按源数据分辨率现算 —— 把基准说死成「上面填的
    # 那个数」，在默认设置下逐字都是假的。
    # 末句交代边界：build_terrain 把层级钳到 [0, 21]，maxzoom=21 选精细档切出来
    # 还是 21（maxzoom=0 配快速档同理）。概率极低，但不写清楚的话，边界上「选了
    # 档位、产物一模一样」看起来就是个 bug。
    'tpl.index.process.terrain_quality_hint': {
        'zh': '基准层级：勾了「自动」就按源数据分辨率现算，否则就是上面填的最大'
              '切片层级。每差一级约 3.3 倍体积换 2.8 倍精度；层级已在 0 或 21 '
              '上限时不再偏移。',
        'en': 'The base level is derived from the source resolution when "Auto" is '
              'checked, otherwise it is the max tiling zoom above. Each step is one '
              'zoom level: ~3.3x size for ~2.8x accuracy. At the 0 / 21 limits the '
              'offset is clamped and the preset changes nothing.',
    },
    'tpl.index.process.terrain_normals': {
        'zh': '生成地形光照法线',
        'en': 'Generate terrain lighting normals',
    },
    # 两条后果都必须写在界面上，缺一条用户就会在几小时的切片之后才发现：
    # 1) Cesium 的 hasVertexNormals 是 provider 级单一标志 —— 这份地形没有法线，
    #    地形光照按钮就对整幅场景失效，连随包底图自带的法线也一并作废；
    # 2) 法线烘焙进瓦片，事后改配置不影响已切完的产物，只能重切。
    'tpl.index.process.terrain_normals_hint': {
        'zh': '不勾选：地图上的地形光照按钮会失效，打开只剩全球日夜渐变，'
              '随包底图自带的法线也一并作废；法线烘焙进瓦片，切完想开只能'
              '重新切片。勾选：体积多 35%~100%、切片慢约一倍，地形精度不变。',
        'en': 'Unchecked: the terrain lighting button stops working — it then only '
              'yields the global day/night gradient, and the bundled base terrain '
              'loses its normals too; normals are baked into the tiles, so turning '
              'them on later requires re-tiling. Checked: 35-100% more size and '
              'about 2x tiling time, with no accuracy gain.',
    },
    # 改前这句把用户指到「下载数据」/ `Download data` —— 那是旧下载弹窗的标题
    # tpl.index.download.title，59459b1 把两个弹窗并成 #createPanel 时连键一起
    # 删了。全仓 grep 过：界面上现在没有任何东西叫这个名字，照它找一辈子也找
    # 不到。真正的入口是同一张面板上方那排段控（tpl.index.create.pipeline =
    # 「任务类型」）里的「高程」chip（tpl.index.download.type_dem）。
    # 引号里的两个词与那两个键的值逐字相同，改任一处会被
    # tests/test_terminology.py::test_quoted_ui_terms_name_a_real_label 拦下。
    'tpl.index.process.contour_source_hint': {
        'zh': '等高线从上传的高程文件渲染；要下载远程高程，请在上方「任务类型」里选「高程」。',
        'en': 'Contours are rendered from the uploaded elevation files; to '
              'download remote elevation, pick "Elevation" under "Task type" '
              'above.',
    },
    'tpl.index.process.contour_interval': {
        'zh': '基准等高距（米）',
        'en': 'Base contour interval (m)',
    },
    'tpl.index.process.contour_interval_hint': {
        'zh': '基准（最细）等高距；低层级会自动变粗以免线条拥挤。小范围 50m、大范围 100m。',
        'en': 'Base (finest) contour interval; lower zoom levels coarsen '
              'automatically to keep lines readable. 50 m for small areas, '
              '100 m for large ones.',
    },
    'tpl.index.process.background': {
        'zh': '背景',
        'en': 'Background',
    },
    'tpl.index.process.background_transparent': {
        'zh': '透明（叠加到底图用）',
        'en': 'Transparent (for overlaying on a base map)',
    },
    'tpl.index.process.background_hint': {
        'zh': '默认米白；勾选透明可把等高线叠加到卫星/标准底图上。',
        'en': 'Off-white by default; check Transparent to overlay contours on '
              'the satellite or standard base map.',
    },
    'tpl.index.process.terrain_shade': {
        'zh': '地形着色',
        'en': 'Terrain shading',
    },
    'tpl.index.process.terrain_shade_option': {
        'zh': '分层设色 + 晕渲（按高程上色 + 阳光阴影）',
        'en': 'Hypsometric tint + hillshade (color by elevation + sun shading)',
    },
    'tpl.index.process.style_custom': {
        'zh': '配色自定义（不改则用默认方案）',
        'en': 'Custom colors (defaults are used if left untouched)',
    },
    'tpl.index.process.line_color_intermediate': {
        'zh': '普通等高线',
        'en': 'Intermediate contours',
    },
    'tpl.index.process.line_color_index': {
        'zh': '计曲线 / 标签',
        'en': 'Index contours / labels',
    },
    'tpl.index.process.tint_breaks': {
        'zh': '分层断点（米，逗号分隔，递增）',
        'en': 'Tint breaks (m, comma-separated, ascending)',
    },
    'tpl.index.process.tint_colors': {
        'zh': '分层颜色（按高程带，断点数+1 个）',
        'en': 'Tint colors (one per elevation band, breaks + 1)',
    },
    'tpl.index.process.tint_reset': {
        'zh': '恢复默认配色',
        'en': 'Reset to default colors',
    },
    'tpl.index.process.style_preview_alt': {
        'zh': '配色预览（分层设色 + 晕渲 + 等高线）',
        'en': 'Color preview (hypsometric tint + hillshade + contours)',
    },
    # zoom_max_placeholder（「自动」）已退役：#processZoomMin/Max 与
    # #zoomMin/#zoomMax 归一成同一对字段，而那对字段服务端渲染出厂 10/15
    # （再由 initMap 按 default_zoom_min/max 覆盖），placeholder 永远不会显示。
    # 「留空 = 自动」这层语义由下面的 zoom_hint 承担，它挂在 #zoomAutoHint 上、
    # 只在等高线管线下出现。
    'tpl.index.process.zoom_hint': {
        'zh': '最大层级留空按高程文件分辨率自动计算；也可手动填更高层级（最高 21）。',
        'en': 'Leave the max zoom level empty to derive it from the elevation '
              'file resolution; '
              'you can also enter a higher level manually (up to 21).',
    },
    # tpl.index.process.submit（「创建处理任务」）已退役：底条只剩一颗提交钮，
    # 四条管线共用 tpl.index.download.submit（「创建任务」）。

    # 滑出面板
    'tpl.index.panel.create': {
        'zh': '新建任务',
        'en': 'New task',
    },
    'tpl.index.panel.tasks': {
        'zh': '任务',
        'en': 'Tasks',
    },
    'tpl.index.panel.config': {
        'zh': '配置',
        'en': 'Settings',
    },
    'tpl.index.panel.close': {
        'zh': '关闭面板',
        'en': 'Close panel',
    },

    # 底部状态栏
    'tpl.index.statusbar.tasks_title': {
        'zh': '活动任务（点击打开任务面板）',
        'en': 'Active tasks (click to open the tasks panel)',
    },
    'tpl.index.statusbar.no_active_task': {
        'zh': '无活动任务',
        'en': 'No active tasks',
    },
    'tpl.index.statusbar.coords_title': {
        'zh': '鼠标指向地图显示经纬度，点击复制坐标',
        'en': 'Point at the map to read lon/lat; click to copy the coordinates',
    },
    'tpl.index.statusbar.coords_empty': {
        'zh': '经度 — 纬度 —',
        'en': 'Lon — Lat —',
    },
    'tpl.index.statusbar.selection_title': {
        'zh': '框选后点击复制四至（W,S,E,N）',
        'en': 'Click after a selection to copy the bbox (W,S,E,N)',
    },
    'tpl.index.statusbar.no_selection': {
        'zh': '未选择区域',
        'en': 'No area selected',
    },
    'tpl.index.statusbar.clock_title': {
        'zh': '本地时间',
        'en': 'Local time',
    },
}
