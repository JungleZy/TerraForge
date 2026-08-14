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
_LOCK = threading.Lock()


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
    try:
        from src.core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute('SELECT config_json FROM plugins WHERE id = ?',
                               (plugin_id,)).fetchone()
        finally:
            conn.close()
        cfg = json.loads(row['config_json']) if row and row['config_json'] else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as e:
        # 日志只写插件 id 与异常类型/信息，绝不写 config 内容——凭据不进日志。
        logger.warning('插件凭据解析失败（%s）：%r', plugin_id, e)
        cfg = {}
    with _LOCK:
        _CACHE[plugin_id] = cfg
        _CACHE_AT[plugin_id] = now
    return str(cfg.get(key, '') or '')


def invalidate(plugin_id=None) -> None:
    """配置保存后调用。plugin_id=None 全清。"""
    with _LOCK:
        if plugin_id is None:
            _CACHE.clear()
            _CACHE_AT.clear()
        else:
            _CACHE.pop(plugin_id, None)
            _CACHE_AT.pop(plugin_id, None)
