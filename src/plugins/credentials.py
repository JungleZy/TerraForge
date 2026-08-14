"""插件凭据的运行期解析。

SourceSnapshot.credential_reference 存的是「键名」不是值（指纹只含键名，
凭据永不进哈希、不进日志、不进任务行）。瓦片 URL 模板里的 {credential}
占位符由 download_engine.get_tile_url 在请求时替换成这里解析出的值。

引用格式：'plugin:<plugin_id>:<config_key>'。
"""

from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_AT: dict = {}
_TTL_SECONDS = 60.0
#: invalidate 的世代号。锁外读 DB 期间发生的失效要能被察觉，见 resolve_reference。
_GENERATION = 0
_LOCK = threading.Lock()


def _as_text(value) -> str:
    """配置值 → 凭据文本。归一化只在这一处做（写缓存之前）。

    两条 return 各写一份归一化是这个模块最贵的坑：命中缓存返回 JSON 原始
    类型、未命中返回 str，下游 `url.replace('{credential}', v)` 会「第一张
    瓦片成功、TTL 内之后全部 TypeError」。所以缓存里存的就是最终形态，
    命中与未命中共用同一句 `cfg.get(key, '')`。

    bool 与结构化值（dict/list）一律 ''：它们不可能是凭据，把 'False' 或
    "{'a': 1}" 拼进 URL 比空串更难查。
    """
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ''


def resolve_reference(reference: str) -> str:
    """'plugin:<id>:<key>' → plugins.config_json 里的值；任何失败返回 ''。

    返回空串而不是抛：下载循环里一个凭据缺失应落成瓦片失败（401），
    在瓦片 outcome 里如实记账，而不是把整条管线打死。
    """
    parts = (reference or '').split(':')
    if len(parts) != 3 or parts[0] != 'plugin':
        return ''
    _, plugin_id, key = parts
    now = time.monotonic()
    with _LOCK:
        if plugin_id in _CACHE and now - _CACHE_AT.get(plugin_id, 0) < _TTL_SECONDS:
            return _CACHE[plugin_id].get(key, '')
        generation = _GENERATION
    try:
        from src.core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute('SELECT config_json FROM plugins WHERE id = ?',
                               (plugin_id,)).fetchone()
        finally:
            conn.close()
        raw = json.loads(row['config_json']) if row and row['config_json'] else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception as e:
        # 日志只写插件 id 与异常类型/信息，绝不写 config 内容——凭据不进日志。
        logger.warning('插件凭据解析失败（%s）：%r', plugin_id, e)
        raw = {}
    cfg = {str(k): _as_text(v) for k, v in raw.items()}
    with _LOCK:
        # 读 DB 是在锁外做的：期间若有 invalidate（用户刚换了 token），这份
        # 已经过期的读法不许回填缓存，否则旧凭据会再活满一个 TTL。
        if generation == _GENERATION:
            _CACHE[plugin_id] = cfg
            _CACHE_AT[plugin_id] = now
    return cfg.get(key, '')


def invalidate(plugin_id=None) -> None:
    """配置保存后调用。plugin_id=None 全清。

    只对本进程有效：缓存是模块级的，跨进程（若瓦片服务独立起进程）唯一的
    保证是 _TTL_SECONDS。
    """
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        if plugin_id is None:
            _CACHE.clear()
            _CACHE_AT.clear()
        else:
            _CACHE.pop(plugin_id, None)
            _CACHE_AT.pop(plugin_id, None)
