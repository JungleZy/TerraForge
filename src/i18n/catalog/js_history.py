"""static/js/history.js（历史记录、任务详情） 的界面文案。

key 命名：`js.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # 时间流列表本体
    'js.history.load_failed': {
        'zh': '加载失败',
        'en': 'Failed to load',
    },
    'js.history.unknown_error': {
        'zh': '任务失败，但没有记录失败原因。',
        'en': 'The task failed, but no failure reason was recorded.',
    },
    'js.history.empty': {
        'zh': '暂无任务',
        'en': 'No tasks yet',
    },

    # 行1 元信息（#类型:id 之后的那段）
    'js.history.meta.dem': {
        'zh': '高程',
        'en': 'DEM',
    },
    'js.history.meta.local_terrain': {
        'zh': '本地高程切片',
        'en': 'Local terrain tiling',
    },

    # 计数单位（行内，跟在数字后面）
    'js.history.unit.tile': {
        'zh': '瓦片',
        'en': 'tiles',
    },
    'js.history.unit.file': {
        'zh': '文件',
        'en': 'files',
    },
    'js.history.progress_verb.downloaded': {
        'zh': '已下载',
        'en': 'Downloaded',
    },

    # 行1 右侧时间 / 行2 摘要
    'js.history.row.elapsed': {
        'zh': '已运行: {time}',
        'en': 'Elapsed: {time}',
    },
    'js.history.row.estimated': {
        'zh': '预计剩余: {time}',
        'en': 'Remaining: {time}',
    },
    'js.history.row.speed': {
        'zh': '速度: {speed}',
        'en': 'Speed: {speed}',
    },
    'js.history.row.bbox': {
        'zh': '区域 {north}, {south}, {east}, {west}',
        'en': 'Area {north}, {south}, {east}, {west}',
    },
    'js.history.row.count': {
        'zh': '{verb}: {downloaded} / {total} {unit}',
        'en': '{verb}: {downloaded} / {total} {unit}',
    },
    'js.history.row.failed': {
        'zh': '失败: {count}',
        'en': 'Failed: {count}',
    },
    'js.history.row.summary': {
        'zh': '{status} · {downloaded} / {total} {unit}',
        'en': '{status} · {downloaded} / {total} {unit}',
    },
    'js.history.row.view_details': {
        'zh': '查看详情',
        'en': 'View details',
    },

    # 行1 右侧动作组（title / aria-label）
    'js.history.action.start': {
        'zh': '启动任务',
        'en': 'Start task',
    },
    'js.history.action.pause': {
        'zh': '暂停任务',
        'en': 'Pause task',
    },
    'js.history.action.resume': {
        'zh': '恢复任务',
        'en': 'Resume task',
    },
    'js.history.action.preview': {
        'zh': '在地图上预览',
        'en': 'Preview on map',
    },
    'js.history.action.delete': {
        'zh': '删除任务',
        'en': 'Delete task',
    },

    # 分页
    'js.history.pagination.prev': {
        'zh': '上一页',
        'en': 'Previous',
    },
    'js.history.pagination.next': {
        'zh': '下一页',
        'en': 'Next',
    },

    # 小地图矩形的信息框（标签位，句首大写）
    'js.history.map.status_label': {
        'zh': '状态:',
        'en': 'Status:',
    },
    'js.history.map.tiles_label': {
        'zh': '瓦片',
        'en': 'Tiles',
    },
    'js.history.map.files_label': {
        'zh': '文件',
        'en': 'Files',
    },

    # 任务状态词表在 js_tasks.py（js.tasks.status.*）—— 全站唯一一份。
    # 这里曾有一份逐字相同的 js.history.status.*，因为 getStatusText 在
    # tasks.js / history.js 各有一份实现、各查一个前缀。两份实现已收口到
    # static/js/task_status.js，文案也随之只留一处：一个状态一处文案，
    # 改中文不必想「改的是哪个页面看到的那份」。

    # 地图样式词表
    'js.history.style.roadmap': {
        'zh': '路线图',
        'en': 'Roadmap',
    },
    'js.history.style.satellite': {
        'zh': '卫星图',
        'en': 'Satellite',
    },
    'js.history.style.hybrid': {
        'zh': '混合图',
        'en': 'Hybrid',
    },
    'js.history.style.terrain': {
        'zh': '地形图',
        'en': 'Terrain',
    },
    'js.history.style.m': {
        'zh': '标准',
        'en': 'Standard',
    },
    'js.history.style.s': {
        'zh': '卫星',
        'en': 'Satellite',
    },
    'js.history.style.y': {
        'zh': '卫星+标注',
        'en': 'Satellite + labels',
    },
    'js.history.style.h': {
        'zh': '道路',
        'en': 'Roads',
    },
    'js.history.style.t': {
        'zh': '地形',
        'en': 'Terrain',
    },
    'js.history.style.contour': {
        'zh': '等高线',
        'en': 'Contour',
    },

    # 详情模态里由 history.js 写入的值（标签在 templates/base.html）
    'js.history.detail.contour_tiles': {
        'zh': '等高线瓦片',
        'en': 'Contour tiles',
    },
    'js.history.detail.load_failed': {
        'zh': '获取任务详情失败',
        'en': 'Failed to load task details',
    },

    # 详情模态的地形切片区
    'js.history.terrain.start_failed': {
        'zh': '启动切片失败',
        'en': 'Failed to start tiling',
    },
    'js.history.terrain.not_started': {
        'zh': '未开始',
        'en': 'Not started',
    },
    'js.history.terrain.status_unknown': {
        'zh': '状态未知',
        'en': 'Unknown status',
    },
    'js.history.terrain.load_failed': {
        'zh': '加载失败',
        'en': 'Failed to load',
    },

    # 详情面板里那个起切按钮不带任何参数，走的是配置默认档位 —— 用户在这里
    # 没有选择权，那就至少得让他看见实际用的是哪一档。档位名不能直吐后端的
    # precision/balanced/speed，那三个词说不清「和什么比、差在哪」。
    #
    # 参照物写「基准层级」而不是「默认档位」：TILING_QUALITY_OFFSETS
    # （src/services/geo_validation.py:77-81）的 +1/0/-1 是相对**基准层级**算的，
    # 与 terrain_quality_preset 当前配成哪一档无关。写成「比默认多切一级」的话，
    # 运维把默认改成 speed 之后，一个存成 balanced 的作业仍会被标成「默认」——
    # 那时它其实比默认多切了一级。用偏移表自己的词汇才无条件为真。
    'js.history.terrain.quality_label': {
        'zh': '切片档位',
        'en': 'Tiling preset',
    },
    'js.history.terrain.quality_precision': {
        'zh': '精细（比基准层级多切一级）',
        'en': 'Precision (one level above the base level)',
    },
    'js.history.terrain.quality_balanced': {
        'zh': '均衡（基准层级）',
        'en': 'Balanced (the base level)',
    },
    'js.history.terrain.quality_speed': {
        'zh': '快速（比基准层级少切一级）',
        'en': 'Fast (one level below the base level)',
    },
    'js.history.terrain.normals_label': {
        'zh': '顶点法线',
        'en': 'Vertex normals',
    },
    'js.history.terrain.normals_on': {
        'zh': '已开启',
        'en': 'On',
    },
    # 「未开启」后面那句括号不是补充说明，是这一档唯一要紧的信息：
    # Cesium 的 hasVertexNormals 是 provider 级的单一标志，这份地形没有法线，
    # 光照开关就对整幅场景失效，连随包底图自带的法线也一起作废。悬停另有全文。
    'js.history.terrain.normals_off': {
        'zh': '未开启（无光照数据）',
        'en': 'Off (no lighting data)',
    },
    'js.history.terrain.normals_off_hint': {
        'zh': '这份地形不含法线，开启光照只会得到全球日夜渐变，随包底图自带的法线'
              '也一并失效。法线是烘焙进瓦片的，改配置不影响已经切完的产物。',
        'en': 'This terrain carries no normals: enabling lighting only yields the '
              'global day/night gradient, and the bundled base terrain loses its '
              'normals too. Normals are baked into the tiles, so changing the '
              'setting later does not affect output that has already been tiled.',
    },

    # 「层级」那一格。两种标签不是措辞变体，是两种不同的事实：
    # 实际层级 = 作业切完后 build_terrain 回报的最深层级（= layer.json 的
    # maxzoom）；基准层级 = 用户填的那个数，精细/快速两档下它比实际值差一级。
    # 拿不到实际值时（存量行 / 还没切完）必须换标签 + 挂说明，不能让基准值
    # 顶着「实际」的名头显示 —— 那正是这次要修的错数字。
    'js.history.terrain.maxzoom_actual_label': {
        'zh': '实际层级',
        'en': 'Actual max level',
    },
    'js.history.terrain.maxzoom_base_label': {
        'zh': '基准层级',
        'en': 'Base level',
    },
    'js.history.terrain.maxzoom_base_hint': {
        'zh': '这是提交时填的基准层级，不是产物实际切到的最深层级：精细档会多切'
              '一级、快速档少切一级。作业切完后这里会换成实际层级（与 layer.json '
              '一致）。',
        'en': 'This is the base level submitted with the job, not the deepest '
              'level actually tiled: the precision preset goes one level deeper '
              'and the fast preset one level shallower. Once the job finishes '
              'this switches to the actual level (matching layer.json).',
    },

    # 删除任务的单一确认框与结果提示。
    # 三个活动状态各有各的措辞：pending 还没开始跑，写「正在运行」就是撒谎；
    # paused 停在半路，说「立即停止」也不准 —— 用户要判断「删了会失去什么」，
    # 靠的正是这句里对当前处境的描述，含糊一句话通用反而帮不上忙。
    'js.history.confirm.delete_task_title': {
        'zh': '删除任务',
        'en': 'Delete task',
    },
    'js.history.confirm.delete_task': {
        'zh': '确定要删除这个任务吗？记录不可恢复。',
        'en': 'Delete this task? The record cannot be recovered.',
    },
    'js.history.confirm.delete_task_running': {
        'zh': '该任务正在运行，删除会立即停止它，已下载的进度不保留。确定删除吗？',
        'en': 'This task is running. Deleting it stops the task immediately and the '
              'progress so far is lost. Delete it anyway?',
    },
    'js.history.confirm.delete_task_pending': {
        'zh': '该任务还在排队、尚未开始，删除会把它移出队列。确定删除吗？',
        'en': 'This task is queued and has not started yet. Deleting it removes it '
              'from the queue. Delete it anyway?',
    },
    'js.history.confirm.delete_task_paused': {
        'zh': '该任务已暂停但还没结束，删除会直接终止它，已下载的进度不保留。确定删除吗？',
        'en': 'This task is paused but not finished. Deleting it terminates the task '
              'and the progress so far is lost. Delete it anyway?',
    },
    'js.history.confirm.delete_files_checkbox': {
        'zh': '同时删除磁盘上的下载产物',
        'en': 'Also delete the downloaded output on disk',
    },
    'js.history.toast.deleted': {
        'zh': '任务已删除',
        'en': 'Task deleted',
    },
    'js.history.toast.deleted_files_deferred': {
        'zh': '任务已删除，磁盘产物正在后台清理',
        'en': 'Task deleted; the disk output is being cleaned up in the background',
    },
    'js.history.toast.delete_failed': {
        'zh': '删除失败',
        'en': 'Delete failed',
    },
    'js.history.toast.delete_failed_reason': {
        'zh': '删除失败: {error}',
        'en': 'Delete failed: {error}',
    },
}
