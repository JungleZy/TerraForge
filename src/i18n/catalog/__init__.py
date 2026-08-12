"""文案目录：按来源分成若干模块，这里合并成一张 key -> {zh, en} 的表。

**一个模块对应一处来源**（模板 / 一个 JS 文件 / 一组接口），这样多人同时加文案
不会挤在同一个文件里。加新来源要在下面 `_DOMAINS` 里显式列出 —— 刻意不用
pkgutil 自动发现：Nuitka 是静态分析，扫不到动态 import 的模块，打包后会整块丢失。

不变量（在合并时就报错，不留到运行期）：
1. key 全局唯一；
2. 每条都必须同时有 zh 和 en，缺一边即报错 —— 漏翻在开发期就炸，而不是让用户
   在英文界面上看到一句中文。
"""

from src.i18n.catalog import (api, app, js_base_terrain, js_commands,
                              js_config, js_drop_process, js_history, js_map,
                              js_region, js_tasks, js_ui, tpl_base, tpl_config,
                              tpl_history, tpl_index, tpl_path_browser,
                              validation)

_DOMAINS = (
    app,
    tpl_base,
    tpl_index,
    tpl_config,
    tpl_history,
    tpl_path_browser,
    js_map,
    js_tasks,
    js_history,
    js_config,
    js_base_terrain,
    js_commands,
    js_drop_process,
    # 阶段 2/3 的界面文案，一个模块装五组前缀（js.region / js.search / js.wizard /
    # js.gaps / js.tasklog）：它们服务的是同一条链路 —— 导入或搜出一个区域、
    # 建任务、盯缺口、翻任务日志。拆成五个文件只会让改一处文案要开五个。
    js_region,
    js_ui,
    api,
    validation,
)

_REQUIRED_LOCALES = ('zh', 'en')


def _merge():
    merged = {}
    for module in _DOMAINS:
        name = module.__name__.rsplit('.', 1)[-1]
        for key, entry in module.MESSAGES.items():
            if key in merged:
                raise RuntimeError(
                    f'i18n key 重复: {key!r}（{name} 与之前的模块撞了）')
            missing = [loc for loc in _REQUIRED_LOCALES if not entry.get(loc)]
            if missing:
                raise RuntimeError(
                    f'i18n key {key!r}（{name}）缺少语种 {missing}')
            merged[key] = entry
    return merged


MESSAGES = _merge()
