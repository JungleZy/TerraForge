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

    # ---- /api/region（区域文件导入）----
    # 这几条都是**回给浏览器并原样显示**的，所以外壳必须是译文，不能整句甩
    # str(e)：region_import 抛的 RegionImportError 消息是英文的，整句回它就等于
    # 在中文界面上甩一句生英文。
    'api.region.import_failed': {
        'zh': '这个文件解析不出可用的下载区域，请换一个文件或检查它的坐标系（需要 WGS-84 经纬度）',
        'en': 'No usable download region could be parsed from this file — try another '
              'file, or check its CRS (WGS-84 longitude/latitude is required)',
    },
    # 有具体原因时用这条。**外壳译、原因不译**，这是刻意的取舍：
    # 上面那句不带原因的版本曾经是七种失败（不是 JSON / 空文件 / 传了个 Point /
    # 坐标越界 / 环退化 / KML 截断 / 假 zip）共用的唯一出口，于是一个上传了点
    # 要素的人被告知「检查你的坐标系」—— 那不是没帮上忙，是把人往错的方向指。
    # 原因句由服务层生成（它知道是第几个环、少了哪个成员、限额是多少），照抄进
    # 目录就是把上百条随时会变的英文句子钉死成译文，一定漂。所以原因保持英文
    # 原文，外壳这句中文负责告诉用户「这是导入失败」和「该往哪看」。
    #
    # `{reason}` 里可能带用户内容（region_import._echo 的 repr 片段，可能含
    # 花括号）。str.format 只扫模板不扫替换进去的值，所以不会二次解析 —— 别
    # 把这里改成 f-string 拼接后再 t()，那才会炸。
    'api.region.import_failed_detail': {
        'zh': '这个文件解析不出可用的下载区域：{reason}',
        'en': 'No usable download region could be parsed from this file: {reason}',
    },
    'api.region.no_file': {
        'zh': '没有收到文件',
        'en': 'No file received',
    },
    # 支持的扩展名清单**不写进文案**：它的事实源是 region_import.SUPPORTED_EXTENSIONS，
    # 抄一份进中英两条消息就是第三、第四份，加一种格式必漏改。前端把清单拼在
    # 这句后面显示。
    'api.region.unsupported': {
        'zh': '不支持这种文件格式',
        'en': 'Unsupported file format',
    },
    # 体积闸在**读文件之前**就拒（看 Content-Length，不等 werkzeug 把 2 GiB
    # spool 到临时盘），所以这句话必须自己带上限额数字：用户此刻手里只有文件
    # 属性里的那个大小，「太大了」不带数字等于没说。
    'api.region.too_large': {
        'zh': '这个文件太大（上限 {limit_mb} MiB），请先简化边界或裁到你需要的范围再导入',
        'en': 'This file is too large (the limit is {limit_mb} MiB) — simplify the '
              'boundary or clip it to the area you need, then import again',
    },

    # ---- /api/places/search（地名搜索）----
    # disabled 不是错误:出厂就没有配地名服务(不内置 provider 是产品决定,
    # 见 CLAUDE.md 的 geocoder_url 一行)。接口回 200 + enabled:false,
    # 这句是给「配置页里为什么这一栏是灰的」当解释用的,不是报错弹窗。
    'api.places.disabled': {
        'zh': '未配置地名服务地址，地点搜索不可用（在配置页填入一个 Nominatim 兼容的地址即可启用）',
        'en': 'No geocoder URL configured, place search is unavailable '
              '(set a Nominatim-compatible URL in Settings to enable it)',
    },
    # 上游是用户自己填的第三方地址,失败原因(超时、404、返回了 HTML)对用户
    # 没有可操作性,统一折成一句;真实原因进任务日志与服务端日志。
    'api.places.failed': {
        'zh': '地名服务没有返回结果，请检查配置里的地址是否可用',
        'en': 'The geocoder returned no result — check that the configured URL is reachable',
    },

    # ---- /api/tasks/<id>/gaps | refill | accept_gaps（缺块决策）----
    'api.gaps.not_found': {
        'zh': '任务不存在，或它没有缺块记录',
        'en': 'No such task, or it has no recorded gaps',
    },
    # 「当前状态不允许」而不是「操作失败」:补漏只在 已完成(有缺口) /
    # 待决策 / 失败 三个状态下成立,而用户看到按钮就会点 —— 说清楚是状态
    # 的问题,他才知道要先等任务跑完,而不是反复重试。
    'api.gaps.not_allowed': {
        'zh': '当前任务状态不允许这个操作',
        'en': 'This action is not allowed in the task\'s current state',
    },

    # ---- POST /api/export/<pipeline>/<id>（成果打包导出）----
    'api.export.unsupported_format': {
        'zh': '不支持这种导出格式',
        'en': 'Unsupported export format',
    },
    'api.export.no_tiles': {
        'zh': '这个任务还没有可导出的瓦片',
        'en': 'This task has no tiles to export yet',
    },
    # 与 api.logs.bad_pipeline 分开的两条,不是重复:那条是「这个管线名不存在」
    # (路径段拼错),这条是「管线名是对的,但这类任务根本没有瓦片金字塔可打包」
    # (dem / local_terrain)。合并成一条会让用户对着一个拼写正确的 URL 找错字。
    # 可打包的管线清单由路由在 body 里回 `supported_pipelines`,不抄进文案。
    'api.export.unsupported_pipeline': {
        'zh': '该类型的任务没有可打包的瓦片金字塔，无法导出',
        'en': 'This task type has no tile pyramid to package, so it cannot be exported',
    },

    # ---- /api/logs/<pipeline>/<id>（每任务日志）----
    # 管线名是路径的一段,拼错会走到这里。合法值来自 contracts.artifact.PIPELINES,
    # 同样不抄进文案。
    'api.logs.bad_pipeline': {
        'zh': '未知的管线名',
        'en': 'Unknown pipeline name',
    },

    # ---- /api/cache/sweep_orphans（孤儿缓存清扫）----
    # 成功回执,不是错误。清扫结果(清掉几个命名空间、释放多少字节)由前端
    # 拼在这句后面 —— 数字进文案就要带占位符,而两种语种的量词位置不同,
    # 拼在外面更不容易翻错。
    'api.cache.sweep_done': {
        'zh': '孤儿缓存清理完成',
        'en': 'Orphan cache sweep complete',
    },
}
