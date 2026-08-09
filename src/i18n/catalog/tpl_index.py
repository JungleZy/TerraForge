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
    'tpl.index.toolbar.zoom_in': {
        'zh': '放大',
        'en': 'Zoom in',
    },
    'tpl.index.toolbar.zoom_out': {
        'zh': '缩小',
        'en': 'Zoom out',
    },
    'tpl.index.toolbar.draw_rect': {
        'zh': '框选',
        'en': 'Select',
    },
    'tpl.index.toolbar.draw_rect_title': {
        'zh': '框选下载区域',
        'en': 'Select download area',
    },
    'tpl.index.toolbar.lighting': {
        'zh': '光照',
        'en': 'Lighting',
    },
    'tpl.index.toolbar.terrain_lighting': {
        'zh': '地形光照',
        'en': 'Terrain lighting',
    },
    'tpl.index.toolbar.terrain_lighting_title': {
        'zh': '地形光照（需已加载地形预览）',
        'en': 'Terrain lighting (requires a loaded terrain preview)',
    },
    'tpl.index.toolbar.process': {
        'zh': '处理',
        'en': 'Process',
    },
    'tpl.index.toolbar.process_title': {
        'zh': '数据处理',
        'en': 'Data processing',
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

    # 两个弹窗共用的表单结构文案
    'tpl.index.modal.close': {
        'zh': '关闭',
        'en': 'Close',
    },
    'tpl.index.form.section_basic': {
        'zh': '基础',
        'en': 'Basics',
    },
    'tpl.index.form.section_range': {
        'zh': '范围与层级',
        'en': 'Extent & zoom levels',
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
        'zh': '最小缩放级别',
        'en': 'Min zoom level',
    },
    'tpl.index.form.zoom_max': {
        'zh': '最大缩放级别',
        'en': 'Max zoom level',
    },

    # 下载参数弹窗
    'tpl.index.download.title': {
        'zh': '下载数据',
        'en': 'Download data',
    },
    'tpl.index.download.type': {
        'zh': '下载类型',
        'en': 'Download type',
    },
    'tpl.index.download.type_map': {
        'zh': '瓦片',
        'en': 'Tiles',
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
    'tpl.index.download.map_style': {
        'zh': '地图样式',
        'en': 'Map style',
    },
    'tpl.index.download.style_standard': {
        'zh': '标准地图',
        'en': 'Standard map',
    },
    'tpl.index.download.style_satellite': {
        'zh': '卫星图',
        'en': 'Satellite',
    },
    'tpl.index.download.style_satellite_labels': {
        'zh': '卫星图+标注',
        'en': 'Satellite + labels',
    },
    'tpl.index.download.style_roads': {
        'zh': '道路图',
        'en': 'Roads',
    },
    'tpl.index.download.style_terrain': {
        'zh': '地形图',
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
        'zh': '创建下载任务',
        'en': 'Create download task',
    },

    # 数据处理弹窗
    'tpl.index.process.title': {
        'zh': '数据处理',
        'en': 'Data processing',
    },
    'tpl.index.process.type': {
        'zh': '处理类型',
        'en': 'Processing type',
    },
    'tpl.index.process.type_local_terrain': {
        'zh': '本地高程切片',
        'en': 'Local DEM tiling',
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
        'en': 'Downloaded DEM task',
    },
    'tpl.index.process.dem_task': {
        'zh': '选择高程任务',
        'en': 'Select DEM task',
    },
    'tpl.index.process.dem_task_hint': {
        'zh': '直接使用该任务已下载的 DEM 文件，无需上传。',
        'en': 'Uses the DEM files already downloaded by that task; no upload '
              'needed.',
    },
    'tpl.index.process.upload_dem': {
        'zh': '上传高程文件（可多选 .tif/.tiff）',
        'en': 'Upload DEM files (multiple .tif/.tiff allowed)',
    },
    'tpl.index.process.local_terrain_maxzoom': {
        'zh': '最大切片层级',
        'en': 'Max tiling zoom level',
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
    # 末句交代边界：build_terrain 把层级钳到 [0, 21]，maxzoom=21 选精细档切出来
    # 还是 21（maxzoom=0 配快速档同理）。概率极低，但不写清楚的话，边界上「选了
    # 档位、产物一模一样」看起来就是个 bug。
    'tpl.index.process.terrain_quality_hint': {
        'zh': '基准层级就是上面填的最大切片层级。每差一级约 3.3 倍体积换 2.8 倍'
              '精度；层级已在 0 或 21 上限时不再偏移。',
        'en': 'The base level is the max tiling zoom above. Each step is one zoom '
              'level: ~3.3x size for ~2.8x accuracy. At the 0 / 21 limits the '
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
    'tpl.index.process.contour_source_hint': {
        'zh': '等高线从上传的 DEM 渲染；远程高程下载在「下载数据」里做。',
        'en': 'Contours are rendered from the uploaded DEM; remote elevation '
              'downloads are done in Download data.',
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
        'zh': '分层设色 + 晕渲（按海拔上色 + 阳光阴影）',
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
        'zh': '分层颜色（按海拔带，断点数+1 个）',
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
    'tpl.index.process.zoom_max_placeholder': {
        'zh': '自动',
        'en': 'Auto',
    },
    'tpl.index.process.zoom_hint': {
        'zh': '最大级别留空按高程文件分辨率自动计算；也可手动填更高层级（最高 21）。',
        'en': 'Leave the max level empty to derive it from the DEM resolution; '
              'you can also enter a higher level manually (up to 21).',
    },
    'tpl.index.process.submit': {
        'zh': '创建处理任务',
        'en': 'Create processing task',
    },

    # 滑出面板
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
