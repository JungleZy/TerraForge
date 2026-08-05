"""templates/_history_content.html + templates/history.html（历史/任务列表） 的界面文案。

key 命名：`tpl.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    'tpl.history.page_title': {
        'zh': '历史记录',
        'en': 'History',
    },
    'tpl.history.back_home': {
        'zh': '返回首页',
        'en': 'Back to home',
    },
    'tpl.history.stats.total': {
        'zh': '总任务',
        'en': 'Total tasks',
    },
    'tpl.history.stats.completed': {
        'zh': '已完成',
        'en': 'Completed',
    },
    'tpl.history.stats.failed': {
        'zh': '失败',
        'en': 'Failed',
    },
    'tpl.history.stats.downloaded': {
        'zh': '累计下载量',
        'en': 'Total downloaded',
    },
    'tpl.history.map.title': {
        'zh': '历史区域地图',
        'en': 'History area map',
    },
    'tpl.history.tasks.title': {
        'zh': '任务列表',
        'en': 'Task list',
    },
    'tpl.history.tasks.loading': {
        'zh': '加载中...',
        'en': 'Loading...',
    },
    'tpl.history.filter.search_placeholder': {
        'zh': '搜索任务...',
        'en': 'Search tasks...',
    },
    'tpl.history.filter.group_label': {
        'zh': '按状态筛选任务',
        'en': 'Filter tasks by status',
    },
    'tpl.history.filter.all': {
        'zh': '全部',
        'en': 'All',
    },
    'tpl.history.filter.active': {
        'zh': '进行中',
        'en': 'In progress',
    },
    'tpl.history.filter.failed': {
        'zh': '失败',
        'en': 'Failed',
    },
    'tpl.history.filter.completed': {
        'zh': '已完成',
        'en': 'Completed',
    },
    'tpl.history.filter.cancelled': {
        'zh': '已取消',
        'en': 'Cancelled',
    },
}
