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
    # 「有缺块」筛选 chip（§13-3）。它是「已完成」的**子集**而不是并列的第五
    # 个生命周期档：completed_with_gaps 的任务确实完成了，只是成品带洞，所以
    # 「已完成」也筛得到它（服务端把 completed 展开成两态）。单独给它一枚 chip
    # 的理由是「哪几份成果有洞」是用户拿数据去做后续处理前必须回答的问题，
    # 而在这枚 chip 之前那个问题只能靠逐页翻行上的缺块角标来回答。
    #
    # 单词本身不自明（「有缺块」既可能指待决、也可能指已接受），所以这一枚
    # 破例带 title —— 其余四枚的词面已经说尽了它们的含义，不需要。
    'tpl.history.filter.gaps': {
        'zh': '有缺块',
        'en': 'With gaps',
    },
    'tpl.history.filter.gaps_title': {
        'zh': '已完成，但成品带缺块（缺块决策仍待处理的任务在「进行中」里）',
        'en': 'Finished, but the output has gaps (tasks still awaiting a gap '
              'decision are under "In progress")',
    },
}
