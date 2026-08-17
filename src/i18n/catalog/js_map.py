"""static/js/map.js（地图交互、框选、下载弹窗逻辑） 的界面文案。

key 命名：`js.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # --- 首屏加载动画 ---------------------------------------------------------
    'js.map.splash.stage_engine': {
        'zh': '正在初始化地图引擎…',
        'en': 'Initializing map engine…',
    },
    'js.map.splash.stage_imagery': {
        'zh': '加载影像服务…',
        'en': 'Loading imagery service…',
    },
    'js.map.splash.stage_workbench': {
        'zh': '准备工作台…',
        'en': 'Preparing workbench…',
    },
    'js.map.splash.error': {
        'zh': '加载出错：{message}',
        'en': 'Load failed: {message}',
    },
    'js.map.splash.unknown_error': {
        'zh': '未知错误',
        'en': 'Unknown error',
    },
    'js.map.splash.ready': {
        'zh': '就绪',
        'en': 'Ready',
    },

    # --- 瓦片数量预估（下载弹窗读数）------------------------------------------
    'js.map.tile_estimate.count': {
        'zh': '预计 {count} 张瓦片',
        'en': 'About {count} tiles',
    },
    'js.map.tile_estimate.over': {
        'zh': '预计 {count} 张瓦片 · 按 10 张/秒约需 {duration}（大任务，创建时将要求确认）',
        'en': 'About {count} tiles · roughly {duration} at 10 tiles/s '
              '(large task, confirmation required on create)',
    },

    # --- 选区浮层（#boundsInfo）-----------------------------------------------
    # sr_* 是只给读屏软件的方位词，视觉上被 .bounds-sr 藏起来；
    # 词后面那个空格留在模板里，不进文案。
    'js.map.bounds.sr_north': {
        'zh': '北纬',
        'en': 'North',
    },
    'js.map.bounds.sr_south': {
        'zh': '南纬',
        'en': 'South',
    },
    'js.map.bounds.sr_east': {
        'zh': '东经',
        'en': 'East',
    },
    'js.map.bounds.sr_west': {
        'zh': '西经',
        'en': 'West',
    },
    'js.map.bounds.edit_title': {
        'zh': '点击编辑',
        'en': 'Click to edit',
    },
    # 2026-08-15 退役 create_task / create_task_title（原「新建任务」主按钮）：
    # 四至读数从地图浮层搬进「新建任务」面板之后，那颗按钮成了面板里指向面板
    # 自己的入口，与按钮一起删。面板标题与 rail 入口那两个键仍在。
    # 清除钮：可见文案与 title **同一个键**。改前是「删除」+ title「清除选区」——
    # 两个动词说同一件事，而「删除」还暗示删掉的是数据（它只清掉那个框）。
    'js.map.bounds.clear': {
        'zh': '清除选区',
        'en': 'Clear selection',
    },
    'js.map.bounds.hint': {
        'zh': '拖拽角点调整 · 点击数值编辑',
        'en': 'Drag a corner to adjust · click a value to edit',
    },
    'js.map.bounds.empty': {
        'zh': '请在地图上框选下载区域',
        'en': 'Draw a selection on the map to download',
    },
    # 2026-08-15 退役 bounds.manual（原空态里那颗「手动输入范围」）：地图浮层
    # 退场，面板选区段的 tpl.index.create.manual_bounds 是现在唯一那颗入口，
    # 它**有没有选区都显示**，所以键盘用户的唯一选区入口没有变少。
    # 下面 manual_apply / manual_cancel 是手动输入面板自己的确定/取消，仍在用。
    'js.map.bounds.manual_apply': {
        'zh': '确定',
        'en': 'Apply',
    },
    'js.map.bounds.manual_cancel': {
        'zh': '取消',
        'en': 'Cancel',
    },
    # 读屏播报（#boundsAnnounce）：只在框选落定 / 手动改数校验通过后写一次。
    # 不复用浮层里的 sr_* 方位词——那些是「N」旁边的补充词，这里要的是一句
    # 能独立听懂的完整摘要。
    'js.map.bounds.announce': {
        'zh': '已选定下载范围：北纬 {north}，南纬 {south}，东经 {east}，西经 {west}',
        'en': 'Selection set: north {north}, south {south}, east {east}, west {west}',
    },

    # --- 状态栏读数 -----------------------------------------------------------
    'js.map.status.selection': {
        'zh': '已选区域 {w}° × {h}°',
        'en': 'Selection {w}° × {h}°',
    },
    'js.map.status.no_selection': {
        'zh': '未选择区域',
        'en': 'No selection',
    },
    'js.map.status.coords': {
        'zh': '经度 {lng}°  纬度 {lat}°',
        'en': 'Lon {lng}°  Lat {lat}°',
    },
    'js.map.status.zoom': {
        'zh': 'z{z}',
        'en': 'z{z}',
    },
    'js.map.status.alt': {
        'zh': ' · 高度 {h}',
        'en': ' · Alt {h}',
    },

    # --- 选区四至校验（validateBoundsRules，三个入口共用）----------------------
    # 一句一条规则，与 src/services/geo_validation.validate_bbox 一一对应。
    # 后端的报错是英文且带原始数值（`east (-170.0) must be greater than west
    # (170.0)`），改前它会原样弹进中文界面；现在前端在提交前就用这几条挡下，
    # 后端那句话到不了用户眼前。
    'js.map.bounds.edit_aria': {
        'zh': '编辑{field}',
        'en': 'Edit {field}',
    },
    'js.map.edit.invalid_number': {
        'zh': '坐标格式无效：{value}',
        'en': 'Invalid coordinate: {value}',
    },
    'js.map.edit.north_gt_south': {
        'zh': '北纬必须大于南纬',
        'en': 'North must be greater than south',
    },
    'js.map.edit.lat_range': {
        'zh': '纬度必须在 ±90° 之间',
        'en': 'Latitude must be within ±90°',
    },
    'js.map.edit.lon_range': {
        'zh': '经度必须在 ±180° 之间',
        'en': 'Longitude must be within ±180°',
    },
    # 零宽（东西经相同）与跨反经线（west=170/east=-170）在后端是同一条规则
    # `east > west`，所以这里也是同一句话——分两句说会让人以为后端有两条口子。
    'js.map.edit.east_gt_west': {
        'zh': '东边界必须大于西边界：东西经相同（零宽）或跨 180° 经线的选区都不支持',
        'en': 'East must be greater than west: zero-width and '
              'antimeridian-crossing selections are not supported',
    },
    # 框选落定时零面积（单击而非拖拽）不念规则名——用户要听的是「怎么做对」。
    'js.map.bounds.no_area': {
        'zh': '单击不构成选区：请按住左键在地图上拖出一个矩形',
        'en': 'A click is not a selection: hold the left button and drag a '
              'rectangle on the map',
    },

    # --- 复制到剪贴板 ---------------------------------------------------------
    'js.map.copy.coords_done': {
        'zh': '坐标已复制',
        'en': 'Coordinates copied',
    },
    'js.map.copy.bounds_done': {
        'zh': '选区四至已复制（W,S,E,N）',
        'en': 'Selection bounds copied (W,S,E,N)',
    },
    'js.map.copy.failed': {
        'zh': '复制失败，请手动选择复制',
        'en': 'Copy failed, select the text and copy manually',
    },

    # --- 下载弹窗 -------------------------------------------------------------
    # 下载提交按钮不再按选区禁用（disabled 元素键盘够不着，也读不到原因），
    # 缺选区时改由这条文案当场解释。所以它必须把**两条**入口都说出来：
    # 只写「框选」等于把键盘用户指回他们唯一做不到的那件事。
    # 2026-08-15：文案里的位置跟着搬。四至读数与「手动输入范围」都在「新建任务」
    # 面板的选区那一格里，不再有「范围浮层」这个东西 —— 指向一个已经不存在的
    # 界面位置，比不给位置更糟。
    'js.map.download.need_selection': {
        'zh': '请先在地图上框选下载区域，或用选区那一格的「手动输入范围」填写四至',
        'en': 'Draw a selection on the map first, or use "Enter bounds manually" '
              'in the selection section',
    },
    # 2026-08-15 退役 download.bounds_summary：那是面板里那句只读四至摘要，而
    # 可编辑的四至读数（.bounds-grid）已经搬进同一格 —— 同一个数字两处渲染。
    'js.map.download.confirm_large': {
        'zh': '预计 {count} 张瓦片，按 10 张/秒估算耗时约需 {duration}。确定创建吗？',
        'en': 'About {count} tiles, roughly {duration} at 10 tiles/s. Create it?',
    },
    'js.map.download.confirm_large_title': {
        'zh': '大任务确认',
        'en': 'Confirm large task',
    },
    'js.map.download.need_output_format': {
        'zh': '请至少勾选一种输出格式（瓦片 / GeoTIFF）',
        'en': 'Select at least one output format (tiles / GeoTIFF)',
    },
    # 选了「需要凭据但还没配置」的插件源时的拦截文案。**必须说清去哪填**：
    # 干巴巴一句「缺凭据」等于把用户丢回那一屏 401 瓦片里自己猜。
    # 面板名与按钮名逐字对齐界面上的 tpl.plugins.title 与 js.plugins.config。
    'js.map.download.credential_missing': {
        'zh': '「{name}」还没配置访问凭据（key / token），现在下载只会得到一屏'
              '失败的瓦片。请打开「插件」面板，找到该插件，点「配置」填入 key '
              '并保存，然后重新提交。',
        'en': '“{name}” has no access credential (key / token) yet — '
              'downloading now would only produce failed tiles. Open the '
              'Plugins panel, find the plugin, click Configure, save your key, '
              'then submit again.',
    },
    # 下拉里给未配置的源打的标记，让用户在选之前就看得见。括号与空格属于语种，
    # 所以整条文案（含源名）都由这一条模板出，JS 不许自己拼。
    'js.map.download.source_unconfigured_option': {
        'zh': '{name}（未配置）',
        'en': '{name} (not configured)',
    },
    # `js.map.download.creating`（「创建中...」）已退役：提交钮的在飞态改由
    # ui.js 的 guard() 统一渲染（spinner + 按钮自己的可见文字），三条装配不再
    # 各换一次文案。同批退役的还有 process.uploading / process.submitting。
    'js.map.download.created': {
        'zh': '任务创建成功！ID: {id}',
        'en': 'Task created. ID: {id}',
    },
    'js.map.download.create_failed': {
        'zh': '创建任务失败: {error}',
        'en': 'Failed to create task: {error}',
    },

    # --- 数据处理表单（等高线 / 本地高程切片）--------------------------------
    'js.map.process.need_files': {
        'zh': '请先选择至少一个 .tif/.tiff 文件',
        'en': 'Select at least one .tif/.tiff file first',
    },
    'js.map.process.need_dem_task': {
        'zh': '请先选择一个已完成的高程任务',
        'en': 'Select a completed elevation task first',
    },
    'js.map.process.no_completed_dem_task': {
        'zh': '暂无已完成的高程任务',
        'en': 'No completed elevation tasks yet',
    },
    'js.map.process.dem_task_load_failed': {
        'zh': '高程任务列表加载失败: {error}',
        'en': 'Failed to load elevation tasks: {error}',
    },
    'js.map.process.terrain_started_dem_task': {
        'zh': '切片任务已创建（零拷贝复用高程任务 #{id} 已下载的高程文件）',
        'en': 'Tiling task created (zero-copy reusing the elevation files '
              'downloaded by task #{id})',
    },
    'js.map.process.contour_default_name': {
        'zh': '等高线瓦片',
        'en': 'Contour tiles',
    },
    'js.map.process.local_terrain_default_name': {
        'zh': '本地地形切片',
        'en': 'Local terrain tiling',
    },
    'js.map.process.create_failed': {
        'zh': '创建失败: {error}',
        'en': 'Create failed: {error}',
    },
    'js.map.process.start_failed': {
        'zh': '任务已创建但启动失败: {error}',
        'en': 'Task created but failed to start: {error}',
    },
    'js.map.process.contour_started': {
        'zh': '等高线任务已开始（上传高程文件 → 渲染瓦片）',
        'en': 'Contour task started (upload elevation files → render tiles)',
    },
    'js.map.process.contour_started_dem_task': {
        'zh': '等高线任务已开始（复用高程任务 #{id} 已下载的高程文件 → 渲染瓦片）',
        'en': 'Contour task started (reusing the elevation files already '
              'downloaded by task '
              '#{id} → render tiles)',
    },
    # `js.map.process.uploading` / `js.map.process.submitting`（「上传中...」/
    # 「提交中...」）随上面 download.creating 一同退役，理由同上：一条动作配一条
    # 「正在…」等于多一条要维护的文案，而按钮上已经写着它在做什么。
    'js.map.process.upload_started': {
        'zh': '上传成功，已开始切片！ID: {id}',
        'en': 'Upload complete, tiling started. ID: {id}',
    },
    'js.map.process.upload_failed': {
        'zh': '上传失败: {error}',
        'en': 'Upload failed: {error}',
    },

    # --- 底图自动回退（map.js _watchBasemapFallback）-------------------------
    # src_* 是按后端返回的源名拼出来的，拼接点登记在 tests/test_i18n.py 的
    # _DYNAMIC_KEY_SITES；源名表在 src/services/basemap_source.py。
    'js.map.basemap.fallback': {
        'zh': '底图已自动切换到{source}：{configured}取不到瓦片。'
              '需要固定用某一张请到配置页「地图底图」里选。',
        'en': 'Basemap switched to {source}: {configured} could not be reached. '
              'Pick one explicitly under Map basemap in Settings.',
    },
    'js.map.basemap.restored': {
        'zh': '底图已恢复为{source}。',
        'en': 'Basemap restored to {source}.',
    },
    'js.map.basemap.src_esri': {
        'zh': 'Esri 卫星影像',
        'en': 'Esri satellite imagery',
    },
    'js.map.basemap.src_google_satellite': {
        'zh': 'Google 卫星影像',
        'en': 'Google satellite imagery',
    },
    'js.map.basemap.src_google_roadmap': {
        'zh': 'Google 路网',
        'en': 'Google roadmap',
    },
    'js.map.basemap.src_osm': {
        'zh': 'OpenStreetMap 路网',
        'en': 'OpenStreetMap roadmap',
    },
    'js.map.basemap.src_download_source': {
        'zh': '下载源的底图',
        'en': 'the download source basemap',
    },
    'js.map.basemap.src_custom': {
        'zh': '自定义底图',
        'en': 'the custom basemap',
    },

    # --- 高程切片：选完 tif 后的有效信息卡（map.js updateLocalTerrainTifInfo）---
    # warn_* 是运行时按后端返回的警告码拼出来的，拼接点登记在
    # tests/test_i18n.py 的 _DYNAMIC_KEY_SITES 里。
    'js.map.tifinfo.reading': {
        'zh': '正在读取文件信息...',
        'en': 'Reading file info...',
    },
    'js.map.tifinfo.failed': {
        'zh': '读取文件信息失败: {error}',
        'en': 'Failed to read file info: {error}',
    },
    'js.map.tifinfo.dimensions': {
        'zh': '尺寸',
        'en': 'Dimensions',
    },
    'js.map.tifinfo.resolution': {
        'zh': '分辨率',
        'en': 'Resolution',
    },
    'js.map.tifinfo.crs': {
        'zh': '坐标系',
        'en': 'CRS',
    },
    'js.map.tifinfo.bounds': {
        'zh': '范围（WGS84）',
        'en': 'Extent (WGS84)',
    },
    'js.map.tifinfo.bounds_native': {
        'zh': '范围（原生坐标）',
        'en': 'Extent (native CRS)',
    },
    'js.map.tifinfo.data': {
        'zh': '数据',
        'en': 'Data',
    },
    'js.map.tifinfo.bands': {
        'zh': '{n} 波段',
        'en': '{n} band(s)',
    },
    'js.map.tifinfo.elevation': {
        'zh': '高程范围',
        'en': 'Elevation range',
    },
    'js.map.tifinfo.recommended_maxzoom': {
        'zh': '建议最大层级',
        'en': 'Suggested max zoom level',
    },
    # 起切前的规模预告（renderTerrainTileEstimate）。两个层级都要报：base 是按
    # 源数据像素估的基准层级，level 是叠上档位偏移、再钳进 [0, 21] 之后真正会切
    # 到的那一级 —— 只写一个数的话，用户在「自动」挡下无从判断档位改了什么。
    # 张数措辞用「预计生成」：这是**打算生成**的瓦片数，不是 Cesium 认得的可用
    # 瓦片数（后者还要过覆盖率闸门，只会更少）。
    'js.map.terrain.estimate': {
        'zh': '预计切片：基准 z{base} → 实际 z{level} · 约 {tiles} 张 · 约 {size}',
        'en': 'Estimated: base z{base} → actual z{level} · ~{tiles} tiles · ~{size}',
    },
    # 挂在预告行的 title 上。必须说清这是估算：真实基准层级由 build_terrain 用
    # 物化后的合并栅格现算，与这里按头部像素估的可能差一级。
    'js.map.terrain.estimate_hint': {
        'zh': '估算值。实际基准层级在切片时按合并后的源栅格现算，产物层级见任务详情。',
        'en': 'An estimate. The real base level is computed from the merged source '
              'raster at tiling time; see the task detail for the level the '
              'artifact actually got.',
    },
    'js.map.tifinfo.summary': {
        'zh': '合计 {n} 个文件',
        'en': '{n} files in total',
    },
    'js.map.tifinfo.finest_resolution': {
        'zh': '最细分辨率',
        'en': 'Finest resolution',
    },
    'js.map.tifinfo.merged_bounds': {
        'zh': '合并范围（WGS84）',
        'en': 'Merged extent (WGS84)',
    },
    'js.map.tifinfo.warn_header_unreadable': {
        'zh': '读不出 TIFF 头部：可能不是 GeoTIFF，或文件已损坏',
        'en': 'Cannot read the TIFF header: not a GeoTIFF, or the file is '
              'corrupt',
    },
    'js.map.tifinfo.warn_no_georeference': {
        'zh': '缺少地理参考（没有像元大小/绑定点），无法切片',
        'en': 'No georeference (missing pixel scale / tie point); cannot be '
              'tiled',
    },
    'js.map.tifinfo.warn_unknown_crs': {
        'zh': '坐标系缺失或是自定义投影，无法切片',
        'en': 'CRS is missing or user-defined; cannot be tiled',
    },
    'js.map.tifinfo.warn_gdal_unavailable': {
        'zh': '服务端缺少 GDAL，只能显示原生坐标下的范围',
        'en': 'GDAL is unavailable on the server; only native-CRS extent is '
              'shown',
    },
    'js.map.tifinfo.warn_reprojected': {
        'zh': '不是 WGS84 经纬度，切片前会自动重投影',
        'en': 'Not WGS84 lon/lat; it will be reprojected before tiling',
    },
    'js.map.tifinfo.warn_rotated': {
        'zh': '栅格带旋转，切片前会重采样',
        'en': 'Raster is rotated; it will be resampled before tiling',
    },
    'js.map.tifinfo.warn_multi_band': {
        'zh': '多波段文件，切片只使用第 1 波段',
        'en': 'Multi-band file; only band 1 is used for tiling',
    },
    'js.map.tifinfo.warn_mixed_crs': {
        'zh': '所选文件的坐标系不一致，请确认没有选错文件',
        'en': 'The selected files use different CRSs; check your selection',
    },
    'js.map.tifinfo.warn_some_unusable': {
        'zh': '有文件缺少可用的地理信息，切片会失败',
        'en': 'Some files lack usable georeference; tiling will fail',
    },
    'js.map.tifinfo.warn_crs_unresolved': {
        'zh': '服务端无法解析该坐标系（EPSG 码未知或不受支持），只能显示原生坐标下的范围',
        'en': 'The server cannot resolve this CRS (unknown or unsupported EPSG '
              'code); only the native-CRS extent is shown',
    },
    'js.map.tifinfo.warn_antimeridian': {
        'zh': '数据横跨 180° 经线，东边界已按 +360 展开（如 180.4 即 -179.6）',
        'en': 'The data crosses the 180° meridian; the eastern bound is '
              'unwrapped past 180 (e.g. 180.4 means -179.6)',
    },

    # --- 等高线配色自定义 -----------------------------------------------------
    'js.map.tint.band_all': {
        'zh': '全部',
        'en': 'All',
    },

    # --- 任务预览（主视图叠加）-----------------------------------------------
    'js.map.preview.hillshade_fallback': {
        'zh': '该任务还没有地形切片，显示源高程数据的晕渲预览',
        'en': 'This task has no terrain tiles yet; showing a hillshade preview '
              'of the source elevation data',
    },
    'js.map.preview.dem_no_tiles': {
        'zh': '该任务没有地形切片、也没有可渲染的高程源文件，仅定位到区域',
        'en': 'This task has neither terrain tiles nor renderable source '
              'elevation data; '
              'flying to the area only',
    },
    'js.map.preview.no_tiles_no_source': {
        'zh': '切片与源文件都不存在，仅定位到区域',
        'en': 'Neither tiles nor source files exist; flying to the area only',
    },
    'js.map.preview.failed': {
        'zh': '预览失败: {error}',
        'en': 'Preview failed: {error}',
    },
    'js.map.preview.chip': {
        'zh': '预览中：{name}（#{id}）',
        'en': 'Previewing: {name} (#{id})',
    },
    'js.map.preview.stop': {
        'zh': '关闭预览',
        'en': 'Close preview',
    },

    # --- 等高线预览面板 -------------------------------------------------------
    'js.map.contour.panel_title': {
        'zh': '已完成的等高线瓦片',
        'en': 'Completed contour tiles',
    },
    'js.map.contour.show_preview': {
        'zh': '在地图上预览：{name}',
        'en': 'Preview on map: {name}',
    },
    'js.map.contour.hide_preview': {
        'zh': '隐藏预览：{name}',
        'en': 'Hide preview: {name}',
    },
    'js.map.contour.default_name': {
        'zh': '等高线 #{id}',
        'en': 'Contour #{id}',
    },
}
