"""src/routes/*.py（接口返回给界面的提示与错误文案） 的界面文案。

key 命名：`api.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # ---- /api/config ----
    'api.config.invalid_values_not_saved': {
        'zh': '存在非法值，本次未保存任何设置',
        'en': 'Invalid values found — nothing was saved',
    },
    'api.config.speedtest_fallback_note': {
        'zh': '测速流程异常，给出保守值',
        'en': 'Speed test failed — falling back to a conservative value',
    },
    'api.config.proxy_autodetect_disabled': {
        'zh': '代理自动检测已关闭，请先勾选「自动检测代理」并保存',
        'en': 'Proxy autodetect is disabled — enable "Auto-detect proxy" and '
              'save first',
    },

    # ---- /api/tasks（含四条管线共用的删除响应与缓存清理拦截）----
    # files_removed=False 有两种成因，文案必须都盖住：产物目录没通过删除护栏
    # （越界，永远删不掉），或者文件正被占用导致 rmtree 静默失败（Windows 上
    # 资源管理器预览、看图软件、杀软扫描都会造成）。后者已经登记进
    # pending_deletions，下次启动会自动补删 —— 只说「未通过安全校验」会把这一
    # 半说成另一半（2026-08-08 评审 P1#6 之后 files_removed 才有第二种成因）。
    'api.tasks.files_kept_unsafe_dir': {
        'zh': '任务记录已删除；产物目录未能删除（未通过安全校验，或文件正被占用），'
              '磁盘文件已保留。占用导致的失败会在下次启动时自动重试',
        'en': 'Task record deleted, but the output directory could not be removed '
              '(it failed the safety check, or its files are in use), so files on '
              'disk were kept. In-use failures are retried on the next startup',
    },
    'api.tasks.cache_clear_blocked': {
        'zh': '有任务尚未结束，清空缓存会让它们的产物静默缺瓦片。'
              '请先暂停或删除：{tasks}',
        'en': 'Some tasks are still unfinished; clearing the cache would '
              'silently leave their output missing tiles. Pause or delete '
              'them first: {tasks}',
    },
    # 上面那条消息里任务标签之间的分隔符（英文用逗号，中文用顿号）
    'api.tasks.label_separator': {
        'zh': '、',
        'en': ', ',
    },
    # 四条管线在「未结束任务」列表里的可读名
    'api.tasks.pipeline.map': {
        'zh': '地图瓦片',
        'en': 'Map tiles',
    },
    'api.tasks.pipeline.dem': {
        'zh': 'DEM',
        'en': 'DEM',
    },
    'api.tasks.pipeline.contour': {
        'zh': '等高线',
        'en': 'Contour',
    },
    'api.tasks.pipeline.local_terrain': {
        'zh': '本地地形',
        'en': 'Local terrain',
    },

    # ---- /api/fs/browse（目录选择弹窗）----
    'api.fs.invalid_path': {
        'zh': '路径无效：{error}',
        'en': 'Invalid path: {error}',
    },
    'api.fs.dir_not_found': {
        'zh': '目录不存在',
        'en': 'Directory does not exist',
    },
    'api.fs.not_a_dir': {
        'zh': '不是目录',
        'en': 'Not a directory',
    },
    'api.fs.read_dir_failed': {
        'zh': '读取目录失败：{error}',
        'en': 'Failed to read directory: {error}',
    },

    # ---- /api/raster/inspect（选完 tif 的信息卡）----
    # 这几条会被前端原样写进信息卡，所以必须是译文：服务层用 InspectError 带
    # 键抛出，路由翻译后才回给浏览器（回 str(e) 就是中文界面上一句生英文）。
    # 服务端自身的异常一律折成 inspect_failed，不把内部细节回显出去。
    'api.raster.unknown_mode': {
        'zh': '未知的解析模式',
        'en': 'Unknown inspection mode',
    },
    'api.raster.files_not_a_list': {
        'zh': 'files 必须是一个列表',
        'en': 'files must be a list',
    },
    'api.raster.no_files': {
        'zh': '没有需要解析的文件',
        'en': 'No files to inspect',
    },
    'api.raster.too_many_files': {
        'zh': '一次最多解析 {max} 个文件',
        'en': 'Too many files (max {max} per request)',
    },
    'api.raster.entry_not_object': {
        'zh': '每个文件条目必须是一个对象',
        'en': 'Each file entry must be an object',
    },
    'api.raster.inspect_failed': {
        'zh': '解析源文件失败',
        'en': 'Failed to inspect source files',
    },
    'api.raster.body_too_large': {
        'zh': '请求体过大：这条接口只接收文件头部标签，不接收文件本身',
        'en': 'Request body too large: this endpoint accepts header tags only, '
              'not the files themselves',
    },

    # ---- /api/contour ----
    'api.contour.default_task_name': {
        'zh': '等高线瓦片',
        'en': 'Contour tiles',
    },
}
