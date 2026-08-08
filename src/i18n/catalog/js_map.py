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
    'js.map.tile_estimate.antimeridian': {
        'zh': '选区跨反经线，后端会拒绝该四至，无法预估瓦片数',
        'en': 'Selection crosses the antimeridian; the server rejects these '
              'bounds, so the tile count cannot be estimated',
    },
    'js.map.tile_estimate.count': {
        'zh': '预计 {count} 块瓦片',
        'en': 'About {count} tiles',
    },
    'js.map.tile_estimate.over': {
        'zh': '预计 {count} 块瓦片 · 按 10 张/秒约 {hours} 小时（大任务，创建时将要求确认）',
        'en': 'About {count} tiles · roughly {hours} h at 10 tiles/s '
              '(large job, confirmation required on create)',
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
    'js.map.bounds.download': {
        'zh': '下载',
        'en': 'Download',
    },
    'js.map.bounds.delete': {
        'zh': '删除',
        'en': 'Delete',
    },
    'js.map.bounds.clear_title': {
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
    # 手动输入范围：空态里那颗入口按钮 + 面板的确定/取消。这是键盘用户唯一的
    # 选区入口，文案要说清「输入的是范围」，不能只写「手动输入」。
    'js.map.bounds.manual': {
        'zh': '手动输入范围',
        'en': 'Enter bounds manually',
    },
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

    # --- 选区数值点击编辑 -----------------------------------------------------
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
    'js.map.edit.zero_width': {
        'zh': '东西经不能相同（选区宽度为 0）',
        'en': 'East and west cannot be equal (selection width is 0)',
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
    'js.map.download.need_selection': {
        'zh': '请先在地图上框选下载区域，或用范围浮层的「手动输入范围」填写四至',
        'en': 'Draw a selection on the map first, or use "Enter bounds manually" '
              'in the bounds overlay',
    },
    'js.map.download.bounds_summary': {
        'zh': '选区 N {north} · S {south} · E {east} · W {west}（{width}° × {height}°）',
        'en': 'Selection N {north} · S {south} · E {east} · W {west} '
              '({width}° × {height}°)',
    },
    'js.map.download.confirm_large': {
        'zh': '预计 {count} 块瓦片，按 10 张/秒估算耗时约 {hours} 小时。确定创建吗？',
        'en': 'About {count} tiles, roughly {hours} h at 10 tiles/s. Create it?',
    },
    'js.map.download.confirm_large_title': {
        'zh': '大任务确认',
        'en': 'Confirm large job',
    },
    'js.map.download.need_output_format': {
        'zh': '请至少勾选一种输出格式（瓦片 / GeoTIFF）',
        'en': 'Select at least one output format (tiles / GeoTIFF)',
    },
    'js.map.download.creating': {
        'zh': '创建中...',
        'en': 'Creating...',
    },
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
        'en': 'Select a completed DEM task first',
    },
    'js.map.process.no_completed_dem_task': {
        'zh': '暂无已完成的高程任务',
        'en': 'No completed DEM tasks yet',
    },
    'js.map.process.dem_task_load_failed': {
        'zh': '高程任务列表加载失败: {error}',
        'en': 'Failed to load DEM tasks: {error}',
    },
    'js.map.process.dem_tiling_started': {
        'zh': '已开始对高程任务 #{id} 做地形切片',
        'en': 'Terrain tiling started for DEM task #{id}',
    },
    'js.map.process.contour_default_name': {
        'zh': '等高线瓦片',
        'en': 'Contour tiles',
    },
    'js.map.process.local_terrain_default_name': {
        'zh': '本地高程切片',
        'en': 'Local terrain tiling',
    },
    'js.map.process.uploading': {
        'zh': '上传中...',
        'en': 'Uploading...',
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
        'zh': '等高线任务已开始（上传 DEM → 渲染瓦片）',
        'en': 'Contour task started (upload DEM → render tiles)',
    },
    'js.map.process.contour_started_dem_task': {
        'zh': '等高线任务已开始（复用高程任务 #{id} 已下载的 DEM → 渲染瓦片）',
        'en': 'Contour task started (reusing DEM already downloaded by task '
              '#{id} → render tiles)',
    },
    # 来源是已下载的高程任务时按钮不能写「上传中」——这条分支一个字节都不上传。
    'js.map.process.submitting': {
        'zh': '提交中...',
        'en': 'Submitting...',
    },
    'js.map.process.upload_started': {
        'zh': '上传成功，已开始切片！ID: {id}',
        'en': 'Upload complete, tiling started. ID: {id}',
    },
    'js.map.process.upload_failed': {
        'zh': '上传失败: {error}',
        'en': 'Upload failed: {error}',
    },

    # --- 等高线配色自定义 -----------------------------------------------------
    'js.map.tint.band_all': {
        'zh': '全部',
        'en': 'All',
    },

    # --- 任务预览（主视图叠加）-----------------------------------------------
    'js.map.preview.hillshade_fallback': {
        'zh': '该任务还没有地形切片，显示源 DEM 的晕渲预览',
        'en': 'This task has no terrain tiles yet; showing a hillshade preview '
              'of the source DEM',
    },
    'js.map.preview.dem_no_tiles': {
        'zh': '该任务没有地形切片、也没有可渲染的 DEM 源文件，仅定位到区域',
        'en': 'This task has neither terrain tiles nor a renderable source DEM; '
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
