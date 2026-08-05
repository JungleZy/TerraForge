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
