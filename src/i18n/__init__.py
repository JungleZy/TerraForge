"""界面语言（中文 / English）。

**为什么是自己写的而不是 Flask-Babel**：这是 Nuitka 打包的离线桌面工具,
gettext 那套要在构建期编译 .mo 并把它们当数据文件塞进 dist,还得处理
locale 目录查找 —— 换来的是翻译工具链,而本项目只有两种语言、几百条文案,
用不上。这里的目录是**纯 Python 模块**（`src/i18n/catalog/*.py`）,Nuitka 静态
分析直接把它们编译进产物,打包脚本一行都不用改。

口径:
- 语种存 cookie `tf-lang`（不是 localStorage）：模板是服务端渲染的,渲染那一刻
  就得知道语种,localStorage 送不到服务端。缺省 zh。
- 每条文案在目录里同时给 zh 和 en 两份,缺一边在合并时就报错（见 catalog）。
- `t()` 查不到 key 时原样返回 key —— 界面上会直接看到 `js.foo.bar` 这种东西,
  比静默回退成中文更容易发现漏翻。
"""

import json

from src.i18n.catalog import MESSAGES

LOCALES = ('zh', 'en')
DEFAULT_LOCALE = 'zh'
COOKIE_NAME = 'tf-lang'
COOKIE_MAX_AGE = 365 * 24 * 3600

# <html lang> 用的 BCP 47 标签,与内部语种码不是一回事。
HTML_LANG = {'zh': 'zh-CN', 'en': 'en'}

# 只有 `js.` 前缀的文案会随页面内联给浏览器。模板文案在服务端就渲染完了,
# 再塞给客户端是白付几十 KB。
CLIENT_PREFIX = 'js.'


def normalize_locale(raw):
    """把任意输入收敛成合法语种码;不认识的一律回落缺省。"""
    if isinstance(raw, str):
        code = raw.strip().lower()
        if code in LOCALES:
            return code
        # 'zh-CN' / 'en-US' 这类带地区的写法取主语言
        primary = code.split('-')[0]
        if primary in LOCALES:
            return primary
    return DEFAULT_LOCALE


def get_locale():
    """当前请求的语种。无请求上下文（后台线程、CLI）时返回缺省。"""
    try:
        from flask import has_request_context, request
    except ImportError:  # pragma: no cover - flask 必然装着,防御性
        return DEFAULT_LOCALE
    if not has_request_context():
        return DEFAULT_LOCALE
    return normalize_locale(request.cookies.get(COOKIE_NAME))


def t(key, locale=None, **params):
    """取文案。params 走 str.format,占位符写成 `{name}`。

    key 不存在时返回 key 本身（见模块 docstring）。
    """
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    text = entry.get(locale or get_locale()) or entry[DEFAULT_LOCALE]
    return text.format(**params) if params else text


def client_catalog(locale):
    """浏览器侧要用的那部分文案（`js.` 前缀），已按语种拍平。"""
    return {
        key: entry.get(locale) or entry[DEFAULT_LOCALE]
        for key, entry in MESSAGES.items()
        if key.startswith(CLIENT_PREFIX)
    }


def register(app):
    """挂到 Flask：模板里可以直接用 `t()`、`locale`、`html_lang`。"""
    app.jinja_env.globals['t'] = t

    @app.context_processor
    def _inject_locale():
        locale = get_locale()
        return {
            'locale': locale,
            'html_lang': HTML_LANG[locale],
            # 内联进页面的客户端文案表。ensure_ascii=True 让它在任何响应编码下
            # 都是纯 ASCII,不依赖 <meta charset> 的解析时机。
            'i18n_client_json': json.dumps(client_catalog(locale), sort_keys=True),
        }
