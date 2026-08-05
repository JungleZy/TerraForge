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
    'js.history.action.cancel': {
        'zh': '取消任务',
        'en': 'Cancel task',
    },
    'js.history.action.preview': {
        'zh': '在地图上预览',
        'en': 'Preview on map',
    },
    'js.history.action.delete': {
        'zh': '删除任务',
        'en': 'Delete task',
    },
    'js.history.action.dismiss_title': {
        'zh': '从列表中移除这条失败记录',
        'en': 'Remove this failed record from the list',
    },
    'js.history.action.dismiss_label': {
        'zh': '移除失败任务行',
        'en': 'Remove failed task row',
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

    # 任务状态词表（models/task.py TaskStatus 六态 + 兜底）
    'js.history.status.pending': {
        'zh': '等待中',
        'en': 'Pending',
    },
    'js.history.status.running': {
        'zh': '运行中',
        'en': 'Running',
    },
    'js.history.status.paused': {
        'zh': '已暂停',
        'en': 'Paused',
    },
    'js.history.status.completed': {
        'zh': '已完成',
        'en': 'Completed',
    },
    'js.history.status.failed': {
        'zh': '失败',
        'en': 'Failed',
    },
    'js.history.status.cancelled': {
        'zh': '已取消',
        'en': 'Cancelled',
    },
    'js.history.status.unknown': {
        'zh': '未知',
        'en': 'Unknown',
    },

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

    # 删除任务的两步确认与结果提示
    'js.history.confirm.delete_task': {
        'zh': '确定要删除这个任务吗？',
        'en': 'Delete this task?',
    },
    'js.history.confirm.delete_task_title': {
        'zh': '删除任务',
        'en': 'Delete task',
    },
    'js.history.confirm.delete_files': {
        'zh': '是否同时删除磁盘上的下载产物？',
        'en': 'Also delete the downloaded output on disk?',
    },
    'js.history.confirm.delete_files_title': {
        'zh': '清理下载产物',
        'en': 'Clean up output',
    },
    'js.history.confirm.delete_files_confirm': {
        'zh': '删除产物',
        'en': 'Delete output',
    },
    'js.history.confirm.delete_files_cancel': {
        'zh': '保留产物',
        'en': 'Keep output',
    },
    'js.history.toast.deleted': {
        'zh': '任务已删除',
        'en': 'Task deleted',
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
