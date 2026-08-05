"""跨页面共用的文案：站点标题、语言开关本身。

key 命名：`app.<区域>.<短名>`；zh 必须与改造前的原文逐字一致
（渲染结果的中文输出要保持不变，由 HTML 快照比对钉住）。
"""

MESSAGES = {
    'app.title': {
        'zh': 'TerraForge —— GIS 数据获取与加工',
        'en': 'TerraForge — GIS Data Acquisition & Processing',
    },
    'app.suffix': {
        'zh': 'TerraForge',
        'en': 'TerraForge',
    },
    'app.language.label': {
        'zh': '语言',
        'en': 'Language',
    },
    'app.language.zh': {
        'zh': '中文',
        'en': '中文',
    },
    'app.language.en': {
        'zh': 'English',
        'en': 'English',
    },
    'app.language.hint': {
        'zh': '切换语言后页面会重新加载；偏好存在本机浏览器里，不进配置表。',
        'en': 'Switching the language reloads the page. The preference is stored '
              'in this browser, not in the config table.',
    },
}
