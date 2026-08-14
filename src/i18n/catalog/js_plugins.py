"""static/js/plugins.js（插件管理面板）的界面文案。

面板列表是 JS 渲染的，所以连「启用 / 禁用 / 加载失败」这种一眼像模板文案的短词
也必须落在 `js.` 前缀下：模板文案在服务端就渲染完了，不会内联给浏览器
（src/i18n/__init__.py 的 `client_catalog` 只挑 `js.`），JS 里取 `tpl.` 的键会
拿到键名本身。

**不含插件自带的字符串**：插件名/描述（plugin.toml）与参数标签
（`ParamSpec.label`）由插件作者提供，宿主只负责转义后渲染。
"""

MESSAGES = {
    # ---- 列表 ----
    'js.plugins.empty': {
        'zh': '未发现任何插件',
        'en': 'No plugins found',
    },
    'js.plugins.enable': {
        'zh': '启用',
        'en': 'Enable',
    },
    'js.plugins.disable': {
        'zh': '禁用',
        'en': 'Disable',
    },
    # 加载失败的插件**也在列表里**（隔离铁律的另一半：坏插件不许打穿宿主，
    # 但必须在界面上看得见），这一条是那条红带的标题。
    'js.plugins.load_error': {
        'zh': '加载失败',
        'en': 'Load failed',
    },
    'js.plugins.origin_builtin': {
        'zh': '内置',
        'en': 'Built-in',
    },
    'js.plugins.origin_external': {
        'zh': '外部',
        'en': 'External',
    },

    # ---- 插件配置 ----
    'js.plugins.config': {
        'zh': '配置',
        'en': 'Configure',
    },
    'js.plugins.config_save': {
        'zh': '保存配置',
        'en': 'Save configuration',
    },
    # 没声明 config_schema 的插件走 JSON 回落分支，这是那个框的标签。
    'js.plugins.config_json_label': {
        'zh': '配置（JSON 对象）',
        'en': 'Configuration (JSON object)',
    },
    # 凭据键的密码框下面那一行说明：服务端下发的是哨兵不是真值，
    # 原样提交 = 不改，清空提交 = 清除。不说清楚用户会以为框里是真 token。
    'js.plugins.config_secret_hint': {
        'zh': '已保存的凭据不回显；不改就直接保存，清空后保存即清除。',
        'en': 'A saved credential is never shown; save as-is to keep it, '
              'or clear the box and save to remove it.',
    },
    'js.plugins.config_load_failed': {
        'zh': '插件配置加载失败',
        'en': 'Failed to load the plugin configuration',
    },
    'js.plugins.config_saved': {
        'zh': '插件配置已保存',
        'en': 'Plugin configuration saved',
    },
    'js.plugins.config_failed': {
        'zh': '插件配置保存失败：{reason}',
        'en': 'Failed to save the plugin configuration: {reason}',
    },

    # ---- 声明式新建任务表单 ----
    'js.plugins.new_task': {
        'zh': '新建任务',
        'en': 'New task',
    },
    'js.plugins.form_name': {
        'zh': '任务名称',
        'en': 'Task name',
    },
    # 四至：v1 用四个数字输入，刻意不接地图框选（范围切割）。
    'js.plugins.form_north': {
        'zh': '北',
        'en': 'North',
    },
    'js.plugins.form_south': {
        'zh': '南',
        'en': 'South',
    },
    'js.plugins.form_east': {
        'zh': '东',
        'en': 'East',
    },
    'js.plugins.form_west': {
        'zh': '西',
        'en': 'West',
    },
    'js.plugins.form_submit': {
        'zh': '创建并启动',
        'en': 'Create and start',
    },

    # ---- 结果提示 ----
    'js.plugins.load_failed': {
        'zh': '插件列表加载失败',
        'en': 'Failed to load the plugin list',
    },
    'js.plugins.toggle_failed': {
        'zh': '切换插件启停状态失败',
        'en': 'Failed to change the plugin on/off state',
    },
    'js.plugins.schema_failed': {
        'zh': '插件参数表单加载失败',
        'en': 'Failed to load the plugin parameter form',
    },
    'js.plugins.create_failed': {
        'zh': '任务创建失败：{reason}',
        'en': 'Failed to create the task: {reason}',
    },
    'js.plugins.created_started': {
        'zh': '任务已创建并启动，可在任务面板查看进度',
        'en': 'Task created and started; watch its progress in the tasks panel',
    },
    # 建成了但没起来是**两件事**：任务行已经落库，提示不能说成「创建失败」
    # （见 src/routes/plugins_api.py 的 create_plugin_task）。
    'js.plugins.created_not_started': {
        'zh': '任务已创建，但启动失败：{reason}',
        'en': 'Task created, but it failed to start: {reason}',
    },
}
