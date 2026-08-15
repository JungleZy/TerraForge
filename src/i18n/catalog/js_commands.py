"""static/js/command_palette.js 的界面文案。key 命名:`js.cmdk.<命令id>`,
与注册表条目的 id 一一对应(完整字面量写在 titleKey 里)。"""

MESSAGES = {
    'js.cmdk.open_palette': {
        'zh': '命令面板',
        'en': 'Command palette',
    },
    'js.cmdk.show_help': {
        'zh': '快捷键速查',
        'en': 'Keyboard shortcuts',
    },
    'js.cmdk.esc_close': {
        'zh': '关闭最上层浮层',
        'en': 'Close topmost overlay',
    },
    'js.cmdk.start_bounds': {
        'zh': '开始框选',
        'en': 'Draw a selection',
    },
    'js.cmdk.clear_bounds': {
        'zh': '清除选区',
        'en': 'Clear selection',
    },
    # 「新建下载任务」→「新建任务」（2026-08-15）：这一条打开的是四条管线共用的
    # 新建面板（预选瓦片），标签不该只提其中一条管线；也与面板标题同词。
    'js.cmdk.new_download': {
        'zh': '新建任务',
        'en': 'New task',
    },
    'js.cmdk.open_tasks': {
        'zh': '打开任务面板',
        'en': 'Open tasks panel',
    },
    'js.cmdk.open_config': {
        'zh': '打开配置面板',
        'en': 'Open settings panel',
    },
    # 「打开本地处理」→「新建地形切片任务」：改前它打开的是「数据处理」弹窗，
    # 标签描述的是那个容器；现在它打开新建面板并预选本地地形切片，标签描述的是
    # 用户要建的那个东西。
    'js.cmdk.open_process': {
        'zh': '新建地形切片任务',
        'en': 'New terrain tiling task',
    },
    'js.cmdk.copy_coords': {
        'zh': '复制当前坐标',
        'en': 'Copy current coordinates',
    },
    # goto_history / goto_config 两条已删（2026-08-15 入口收敛）：命令面板同时列
    # 「打开任务面板」+「前往历史记录页」是同一件事的两种形态。/history 与
    # /config 两条路由本身保留（深链与打包可达性），只是不再从命令面板露出。
    'js.cmdk.theme_dark': {
        'zh': '切换到暗黑主题',
        'en': 'Switch to dark theme',
    },
    'js.cmdk.theme_light': {
        'zh': '切换到明亮主题',
        'en': 'Switch to light theme',
    },
    'js.cmdk.lang_switch': {
        'zh': '切换界面语言',
        'en': 'Switch interface language',
    },
    'js.cmdk.empty': {
        'zh': '无匹配命令',
        'en': 'No matching commands',
    },
}
