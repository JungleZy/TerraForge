"""static/js/tasks.js（任务列表与实时进度） 的界面文案。

key 命名：`js.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # --- 底部状态栏「最近事件」单行读数 ---
    'js.tasks.event.completed': {
        'zh': '任务 #{id} 已完成',
        'en': 'Task #{id} completed',
    },
    'js.tasks.event.failed': {
        'zh': '任务 #{id} 失败',
        'en': 'Task #{id} failed',
    },
    'js.tasks.event.stitching': {
        'zh': '任务 #{id} 拼接瓦片中…',
        'en': 'Task #{id} stitching tiles…',
    },
    'js.tasks.event.copying': {
        'zh': '任务 #{id} 复制瓦片中…',
        'en': 'Task #{id} copying tiles…',
    },

    # --- 行内阶段提示（下载 100% 之后的拼接 / 复制阶段） ---
    'js.tasks.stage.stitching': {
        'zh': '拼接中（zoom {zoom}）…',
        'en': 'Stitching (zoom {zoom})…',
    },
    'js.tasks.stage.copying': {
        'zh': '复制瓦片中 {done} / {total} …',
        'en': 'Copying tiles {done} / {total} …',
    },
    # 地形切片。terrain_stage 覆盖瓦片循环**之前**的阶段（多幅 DEM 物化成单
    # 文件、建金字塔）—— 那一段跑在 total 算出来之前，没有分母，只能报比例。
    'js.tasks.terrain_stage': {
        'zh': '{stage} {pct}% …',
        'en': '{stage} {pct}% …',
    },
    'js.tasks.terrain_tiling': {
        'zh': '切片中 {done} / {total} …',
        'en': 'Tiling {done} / {total} …',
    },

    # --- 列表加载 ---
    'js.tasks.load.http_error': {
        'zh': '任务列表接口返回 HTTP {status}',
        'en': 'Task list API returned HTTP {status}',
    },
    'js.tasks.load.failed': {
        'zh': '加载任务列表失败: {error}',
        'en': 'Failed to load task list: {error}',
    },

    # --- 进度计数的单位与动词 ---
    'js.tasks.unit.tile': {
        'zh': '瓦片',
        'en': 'tiles',
    },
    'js.tasks.unit.file': {
        'zh': '文件',
        'en': 'files',
    },
    'js.tasks.verb.render_contour_tiles': {
        'zh': '渲染等高线瓦片',
        'en': 'Rendering contour tiles',
    },
    'js.tasks.verb.download_dem': {
        'zh': '下载 DEM',
        'en': 'Downloading DEM',
    },
    'js.tasks.verb.downloaded': {
        'zh': '已下载',
        'en': 'Downloaded',
    },
    'js.tasks.progress_detail': {
        'zh': '{verb}: {done} / {total} {unit}',
        'en': '{verb}: {done} / {total} {unit}',
    },
    'js.tasks.failed_count': {
        'zh': '| 失败: {n}',
        'en': '| Failed: {n}',
    },

    # --- 底部状态栏「活动任务」聚合读数 ---
    'js.tasks.status_bar.idle': {
        'zh': '无活动任务',
        'en': 'No active tasks',
    },
    'js.tasks.status_bar.active': {
        'zh': '{n} 个活动任务（{running} 运行中） {pct}%',
        'en': '{n} active tasks ({running} running) {pct}%',
    },

    # --- 终态提示 ---
    'js.tasks.toast.completed_with_warning': {
        'zh': '任务完成，但有警告：{warning}',
        'en': 'Task completed with a warning: {warning}',
    },
    'js.tasks.toast.failed': {
        'zh': '任务失败：{message}',
        'en': 'Task failed: {message}',
    },
    'js.tasks.unknown_error': {
        'zh': '任务失败，但后端没有返回失败原因。请查看服务端日志。',
        'en': 'The task failed, but the backend returned no reason. '
              'Check the server logs.',
    },

    # --- 状态名（键是后端 TaskStatus 的协议值，这里只翻显示文案） ---
    'js.tasks.status.pending': {
        'zh': '等待中',
        'en': 'Pending',
    },
    'js.tasks.status.running': {
        'zh': '运行中',
        'en': 'Running',
    },
    'js.tasks.status.paused': {
        'zh': '已暂停',
        'en': 'Paused',
    },
    'js.tasks.status.completed': {
        'zh': '已完成',
        'en': 'Completed',
    },
    'js.tasks.status.failed': {
        'zh': '失败',
        'en': 'Failed',
    },
    'js.tasks.status.unknown': {
        'zh': '未知',
        'en': 'Unknown',
    },

    # --- 时长格式化 ---
    'js.tasks.duration.seconds': {
        'zh': '{s}秒',
        'en': '{s}s',
    },
    'js.tasks.duration.min_sec': {
        'zh': '{m}分{s}秒',
        'en': '{m}m {s}s',
    },
    'js.tasks.duration.minutes': {
        'zh': '{m}分钟',
        'en': '{m}m',
    },
    'js.tasks.duration.hour_min': {
        'zh': '{h}小时{m}分钟',
        'en': '{h}h {m}m',
    },
    'js.tasks.duration.hours': {
        'zh': '{h}小时',
        'en': '{h}h',
    },
    'js.tasks.time.elapsed': {
        'zh': '已运行: {value}',
        'en': 'Elapsed: {value}',
    },
    'js.tasks.time.remaining': {
        'zh': '预计剩余: {value}',
        'en': 'ETA: {value}',
    },

    # --- 任务操作 ---
    'js.tasks.toast.start_failed': {
        'zh': '启动任务失败: {error}',
        'en': 'Failed to start task: {error}',
    },
    'js.tasks.toast.pause_failed': {
        'zh': '暂停任务失败: {error}',
        'en': 'Failed to pause task: {error}',
    },
    'js.tasks.toast.resume_failed': {
        'zh': '恢复任务失败: {error}',
        'en': 'Failed to resume task: {error}',
    },
}
