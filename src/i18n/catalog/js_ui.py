"""static/js/ui.js / panels.js / theme.js / terrain_lighting.js / path_browser.js（通用组件） 的界面文案。

key 命名：`js.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    # ---------------------------------------------------------------- ui.js
    'js.ui.toast.close': {
        'zh': '关闭',
        'en': 'Close',
    },
    'js.ui.confirm.title': {
        'zh': '确认操作',
        'en': 'Confirm action',
    },
    'js.ui.confirm.ok': {
        'zh': '确定',
        'en': 'OK',
    },
    'js.ui.confirm.cancel': {
        'zh': '取消',
        'en': 'Cancel',
    },
    'js.ui.conn.connected': {
        'zh': '已连接',
        'en': 'Connected',
    },
    'js.ui.conn.disconnected': {
        'zh': '已断开',
        'en': 'Disconnected',
    },
    # 进度框声明自己不可关闭时给出的那句话。改前它在捕获阶段把 Esc **吞掉**、
    # 界面上什么都不发生 —— 与「这个键坏了」完全无法区分。
    'js.ui.progress.locked': {
        'zh': '这一步不能中断,请等它跑完',
        'en': 'This step cannot be interrupted — please wait for it to finish',
    },

    # -------------------------------------------------------- path_browser.js
    'js.path_browser.pick_drive': {
        'zh': '(选择盘符)',
        'en': '(pick a drive)',
    },
    'js.path_browser.parent_dir': {
        'zh': '.. (上一级)',
        'en': '.. (parent directory)',
    },
    'js.path_browser.no_subdirs': {
        'zh': '(没有子目录)',
        'en': '(no subdirectories)',
    },
    # 读目录期间的占位项（形态与「(没有子目录)」一致：同一个 muted 列表项）。
    # 慢盘上那几秒里列表原来停在上一个目录的内容上，看着像点了没反应。
    'js.path_browser.loading': {
        'zh': '(正在读取目录…)',
        'en': '(loading directories…)',
    },
    # 原文是字符串拼接，合并成一条带 {error} 占位符的文案
    'js.path_browser.start_unavailable': {
        'zh': '起点不可用({error}),已回到根目录',
        'en': 'Start path unavailable ({error}), returned to root',
    },
    'js.path_browser.load_failed': {
        'zh': '目录列表加载失败:{error}',
        'en': 'Failed to load directory list: {error}',
    },
}
