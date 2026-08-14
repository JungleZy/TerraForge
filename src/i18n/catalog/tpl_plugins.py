"""插件管理面板的**模板**文案（templates/_plugins_content.html + index.html
的面板入口/面板头）。

这里只有两条 —— 面板里能看见的其余文字（启用/禁用/加载失败/表单）全部由
static/js/plugins.js 渲染，所以它们必须是 `js.plugins.*`：只有 `js.` 前缀那部分
文案会内联给浏览器（src/i18n/__init__.py 的 `client_catalog`），JS 里调
`t('tpl.…')` 拿不到值，会把键名原样显示给用户。

**插件自己的名字、描述、参数标签不在这里**：它们来自 plugin.toml / MANIFEST /
ParamSpec.label，是运行期数据，翻译由插件作者负责。宿主 catalog 只装宿主自己
的界面文案。
"""

MESSAGES = {
    'tpl.plugins.title': {
        'zh': '插件',
        'en': 'Plugins',
    },
    'tpl.plugins.full_privilege': {
        'zh': '插件以宿主的完整权限运行，只启用你信任的插件。',
        'en': 'Plugins run with the host\'s full privileges; enable only those '
              'you trust.',
    },
}
