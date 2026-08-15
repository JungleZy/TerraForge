"""
Configuration Manager Service

Provides centralized configuration management with validation and persistence.
Handles reading, updating, and validating application configuration stored in SQLite.
"""

import ipaddress
import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlsplit
import sqlite3
from src.core.database import (
    get_connection_context, DEFAULT_CONFIGS, normalize_default_save_path, utc_now_iso,
)
from src.services.geo_validation import TILING_QUALITY_OFFSETS
from src.services.system_proxy import mask_url_secrets

logger = logging.getLogger(__name__)

# 写入/更新 config 行的 upsert 语句，set 与 set_many 共用（抽常量避免两处漂移）
_CONFIG_UPSERT_SQL = '''INSERT INTO config (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at'''


def _mask_log_value(key: str, value: str) -> str:
    """日志用的脱敏值：凭据/代理 userinfo 不进日志（与 set 的口径一致）。"""
    if key in {'earthdata_username', 'earthdata_password'}:
        return '***'
    if key == 'proxy_url':
        # user:pass@ 形式的代理凭据不进日志（host 保留便于排查）
        return mask_url_secrets(value)
    return value


# 「已保存，未修改」哨兵。密码框回填真值等于把它交给页面 DOM 与
# GET /api/config 的任何读者（局域网上的任意主机、任何浏览器扩展）。改为回填这个
# 哨兵:用户看到密码已设置、可以改，而真值不出服务端;PUT /api/config 收到它就
# 跳过该键不覆盖。清空密码仍然可用 —— 把框清空提交，存的就是空串。
# 见 docs/reviews/2026-08-08-full-project-review.md 的「安全姿态」第 1 项。
SECRET_UNCHANGED = '__TF_UNCHANGED__'

# 只有「用户永远不需要读回」的键才适用哨兵。proxy_url **不在**其中:它是一个
# 用户必须看得见才能改的文本框，塞哨兵会让「改主机名」变成「先重打整条 URL」。
# 它的 userinfo 仍会随配置表单下发,这一条留在既定的「可信环境」部署前提里。
_SENTINEL_KEYS = frozenset({'earthdata_password'})


def redact_secret_value(key: str, value):
    """下发给浏览器前的脱敏:已设置的密钥类键换成哨兵值，未设置的原样(空串)。"""
    if key in _SENTINEL_KEYS and value:
        return SECRET_UNCHANGED
    return value


def is_unchanged_secret(key: str, value) -> bool:
    """这个键值对是不是「原样回传的哨兵」—— 是就该跳过，不能当新值写库。"""
    return key in _SENTINEL_KEYS and value == SECRET_UNCHANGED


# --------------------------------------------------------------------------
# 键 -> 校验规则表
#
# validate_config 过去是一串 if/elif，尾部一句「其余键一律 return True」——
# **全部路径类与 URL 类键都落在那个兜底里**。后果不是「少一道校验」这么轻：
#   - terrain_global_base_path 是 /terrain/base/<path> 的根
#     （routes/terrain_static.py）。设成 '/' 之后 _resolve_safe_file 的包含检查
#     恒真，两个未鉴权请求就能读走任意文件（2026-08-08 评审「安全姿态」第 3 项
#     有实测：PUT 配置 200 → GET /terrain/base/etc/passwd 拿到 1427 字节）。
#   - stitch_tmpdir 被 download_engine.py 直接 os.makedirs(..., exist_ok=True)，
#     写错就是在任意位置建目录。
#   - terrain_base_parent_url 被固化进 layer.json 的 parentUrl 交给浏览器，
#     javascript:/data:/带 userinfo 的值会原样发到客户端。
# 改成显式的表：新增配置键必须在这里登记一条（哪怕是「不加约束」），
# tests/test_fix_config_path_validation.py 的覆盖用例会钉住这一点，
# 不会再有键靠「什么都不写」就默默拿到 accept-anything 待遇。
# --------------------------------------------------------------------------


def _validate_scratch_dir(value) -> bool:
    """临时/中间产物目录：空 = 用系统临时目录；非空必须是**绝对**路径。

    这两个键（stitch_tmpdir / contour_warp_tmpdir）存在的意义就是把 GB 级中间
    产物挪到另一块盘，所以**不做根目录约束** —— 部署前提是可信环境（见
    docs/reviews/2026-07-31-code-only-review.md 的「部署前提」，2026-08-08 重新
    确认），用户想把 scratch 放哪就放哪，与 default_save_path 自 0.2.4 起的
    「全盘可选」口径一致。

    仍然要求绝对路径，理由与安全无关而是**正确性**：相对值会被
    `download_engine` 那侧按【进程 CWD】解析（`os.makedirs(stitch_tmp_base)`），
    打包 exe 从快捷方式启动时 CWD 不是安装目录，中间产物会落到一个谁也想不到
    的地方 —— 这正是 M10 给 output_path 修过的那一类坑。

    判据**有意不做 `expanduser()`**，别再加回来：三个读取侧
    （`download_engine.stitch_tiles_with_gdal` 的 `os.makedirs` +
    `tempfile.mkdtemp(dir=...)`、`contour_engine.build_contour_tiles` 的
    `tempfile.mkdtemp(dir=...)`、`task_cleanup.sweep_startup_residue` 的
    `Path(...)`）拿的都是库里的字面量，一个都不展开 `~`。校验侧一旦展开，
    `~/tf_warp` 就被判合法并原样入库，然后同一个值有三种解释：拼接把 GB 级
    中间产物写进 `<CWD>/~/tf_warp/`（真的建一个名叫 `~` 的目录）、每个等高线
    任务在 warp 阶段抛 FileNotFoundError（那行不在 try 内）、清扫扫的是第三个
    根 —— 而配置页保存时是 200。这个键的语义是「用户明确指定的另一块盘」，
    `~` 支持恰好把上面那道 CWD 闸门打穿，不需要。
    """
    raw = str(value or '').strip()
    if not raw:
        return True
    try:
        return Path(raw).is_absolute()
    except (OSError, ValueError, RuntimeError):
        return False


def _validate_base_terrain_path(value) -> bool:
    """随包底图目录：非空且可解析即可，允许相对（出厂值就是相对的）。

    不做根目录约束：224 MB / 4.3 万个文件，用户把它放到另一块盘是正常需求。
    只拦空值 —— 空值会让 `resolve_stored_output_dir('')` 落到 BASE_DIR 本身，
    于是 /terrain/base 把整个安装目录（含 data/map_downloader.db）挂上静态服务，
    而且底图判定也必然失败。这是功能性错误，不是安全考虑。
    """
    from src.services.task_cleanup import resolve_stored_output_dir

    raw = str(value or '').strip()
    if not raw:
        return False
    try:
        resolve_stored_output_dir(raw).resolve()
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _is_link_local_host(host: str) -> bool:
    """169.254.0.0/16 与 fe80::/10 —— 云厂商的实例元数据端点就住在这里。

    有意**不**复用 tile_url_probe.should_bypass_proxy：它把回环与私网也算进去，
    那是「该不该走代理」的路由判断，不是安全边界。而存量库里的 parentUrl 就是
    旧出厂值 http://localhost:5000/terrain/base（新默认值是相对路径
    /terrain/base），指到内网 IP 上的另一套地形服务更是
    docs/reference/terrain/global-base-build.md 明写的正常用法 —— 照搬那个谓词
    会把存量值和文档化的部署方式一起判非法。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False        # 域名：解析结果随部署而变，这里判不了
    return ip.is_link_local


def _validate_browser_url(value) -> bool:
    """交给浏览器去取的 URL：同源相对路径，或 http(s) 绝对地址。"""
    raw = str(value or '').strip()
    if not raw:
        return True         # 空 = 不写该字段（layer_json.normalize_parent_url 返回 None）
    if any(ch in raw for ch in ' \t\r\n'):
        return False        # 换行会把值拆进 layer.json 的其它位置
    if raw.startswith('//'):
        return False        # 协议相对地址继承页面协议，绕开下面的 scheme 白名单
    if raw.startswith('/'):
        return '\\' not in raw          # 同源相对路径
    parts = urlsplit(raw)
    if parts.scheme not in ('http', 'https'):
        return False        # javascript:/data:/file: 一律挡在写库之前
    if parts.username or parts.password:
        return False        # userinfo 会随 layer.json 原样发到浏览器
    host = parts.hostname or ''
    if not host:
        return False
    port = parts.port       # 端口越界在这里抛 ValueError -> False
    if port is not None and not 0 < port <= 65535:
        return False
    return not _is_link_local_host(host)


def _validate_proxy_url(value) -> bool:
    """代理地址：空 = 自动探测/直连；非空必须带 http(s) scheme 与主机。

    判据与 proxy_autodetect._normalize_proxy_url 一致（aiohttp 只吃 http(s)）。
    少写 scheme 的 '127.0.0.1:7890' 过去能存进库，之后每一张瓦片都在
    aiohttp 里抛 InvalidURL —— 报错离配置页十万八千里。
    回环/私网**不拦**：代理正常就住在本机或局域网。
    """
    raw = str(value or '').strip()
    if not raw:
        return True
    parts = urlsplit(raw)
    if parts.scheme not in ('http', 'https'):
        return False
    if not parts.hostname:
        return False
    port = parts.port       # 端口越界 -> ValueError -> False
    return port is None or 0 < port <= 65535


def _validate_int_range(value, low: int, high: int) -> bool:
    return low <= int(value) <= high


def _is_valid_lat(value) -> bool:
    return -90 <= float(value) <= 90


def _is_valid_lng(value) -> bool:
    return -180 <= float(value) <= 180


def _validate_save_path(value) -> bool:
    # 绝对路径 + 至少两级深度(与建任务同一口径,0.2.4 起全盘可选);
    # 相对/浅层都拒绝 —— 存一个建任务时必被 400 的值没有意义。
    # 这个键**不受**上面的根集合约束:用户可以把产物存到任意一块盘。
    from src.services.geo_validation import require_absolute_output_dir
    try:
        require_absolute_output_dir(str(value))
        return True
    except ValueError:
        return False


def _validate_tile_servers(value) -> bool:
    # 逗号分隔的瓦片服务器列表：每个条目必须是 Google 别名/主机
    # 或含 {x}/{y}/{z} 的完整 XYZ 模板（tile_url_probe 统一语义）
    from src.services.tile_url_probe import validate_server_list
    ok, _err = validate_server_list(str(value))
    return ok


def _validate_basemap_source(value) -> bool:
    # 预设别名 / download_source / 完整 XYZ 模板
    from src.services.basemap_source import validate_basemap_source
    ok, _err = validate_basemap_source(str(value))
    return ok


def _is_valid_color(value) -> bool:
    # 判据用渲染器自己的解析器（见 contour_task_manager.validate_color）：
    # 「配置里收得下的」必须恰好等于「渲染时画得出的」。以前这里不校验，
    # '#zzzzzz' 一路通到 per-tile 渲染，在那个吞异常的 except 里把**每一张**
    # 瓦片记成 failed，任务最后报「No contour tiles rendered」——指着三个都
    # 正确的参数（评审 P1#10）。惰性导入：contour_task_manager 在模块级
    # import 本模块，顶层 import 会成环。
    from src.services.contour_task_manager import validate_color
    try:
        validate_color(str(value))
        return True
    except ValueError:
        return False


def _is_valid_color_or_transparent(value) -> bool:
    # 背景色多一个合法特值 'transparent'（引擎按它出全透明底图）。
    return str(value).strip().lower() == 'transparent' or _is_valid_color(value)


def _is_valid_color_csv(value) -> bool:
    # 分层设色色带：逗号分隔逐个校验。空值在这里不合法 —— 读取侧拿它建
    # ListedColormap，空色表构造即抛。
    parts = [p.strip() for p in str(value).split(',') if p.strip()]
    return bool(parts) and all(_is_valid_color(p) for p in parts)


def _is_valid_contour_interval(value) -> bool:
    # 与建任务同一道闸门（contour_task_manager._MIN_CONTOUR_INTERVAL）。这个键
    # 就是建任务时 contour_interval 留空的取值来源：配成 0.1，等高线级数在单张
    # 瓦片里会炸到上万条，而瓦片内部没有停止检查，暂停/删除都打不断（P1#11）。
    from src.services.contour_task_manager import _MIN_CONTOUR_INTERVAL
    return float(value) >= _MIN_CONTOUR_INTERVAL


def _validate_float_range(value, low: float, high: float) -> bool:
    return low <= float(value) <= high


def _validate_geocoder_url(value) -> bool:
    """地名搜索服务地址：空 = 关闭；非空必须是 http(s) 且带 {q} 占位符。

    要求 `{q}` 是硬约束而不是善意提示：没有它的地址永远只会返回同一个结果，
    而用户会以为是「搜不到」。缺占位符在配置页就拒绝，比在搜索框里静默失败好。
    SSRF / allowlist 的检查不在这里做 —— 那是取用时刻的事（服务地址可以指向
    局域网内的自建服务，配置期一刀切拒绝私网是错的）。
    """
    raw = str(value or '').strip()
    if not raw:
        return True
    if '{q}' not in raw:
        return False
    parts = urlsplit(raw)
    return parts.scheme in ('http', 'https') and bool(parts.hostname)


_VALUE_RULES = {
    # --- 数值 / 坐标 / 枚举（判据与改造前逐字相同）---
    'concurrent_downloads': lambda v: _validate_int_range(v, 1, 100),
    'request_timeout': lambda v: _validate_int_range(v, 1, 300),
    'max_retries': lambda v: _validate_int_range(v, 0, 10),
    'map_initial_zoom': lambda v: _validate_int_range(v, 0, 21),
    'default_zoom_min': lambda v: _validate_int_range(v, 0, 21),
    'default_zoom_max': lambda v: _validate_int_range(v, 0, 21),
    'map_center_lat': _is_valid_lat,
    'map_center_lng': _is_valid_lng,
    'tile_servers': _validate_tile_servers,
    'basemap_source': _validate_basemap_source,
    # --- 路径类 ---
    'default_save_path': _validate_save_path,
    'terrain_global_base_path': _validate_base_terrain_path,
    'stitch_tmpdir': _validate_scratch_dir,
    'contour_warp_tmpdir': _validate_scratch_dir,
    # --- URL 类 ---
    'terrain_base_parent_url': _validate_browser_url,
    'proxy_url': _validate_proxy_url,
    # --- 等高线颜色与间距（评审 P1#10 / P1#11）---
    'contour_color_intermediate': _is_valid_color,
    'contour_color_index': _is_valid_color,
    'contour_color_label': _is_valid_color,
    'contour_water_color_ocean': _is_valid_color,
    'contour_water_color_inland': _is_valid_color,
    'contour_background': _is_valid_color_or_transparent,
    'contour_hypsometric_colors': _is_valid_color_csv,
    'contour_default_interval': _is_valid_contour_interval,
    # 档位是小而稳定的枚举，白名单直接从 geo_validation 的取值表取 ——
    # 不在这里抄第二份（那正是 _UNCONSTRAINED_KEYS 注释里说的第二处事实来源）。
    'terrain_quality_preset': lambda v: v in TILING_QUALITY_OFFSETS,
    # --- 全局资源调度上界（§4.1）。这些数字是**天花板**，任务内的限流由
    # ResourceScheduler 在天花板下分配，所以上限可以放宽 —— 拧到 512 条连接
    # 是用户的选择，而改造前那种「四个任务各自 50 条、无人知道总数」不是。---
    'max_concurrent_tasks': lambda v: _validate_int_range(v, 1, 16),
    'max_network_connections': lambda v: _validate_int_range(v, 1, 512),
    # 0 = 自动（min(4, cpu_count)）。0 必须是合法值，否则出厂默认非法。
    'max_cpu_workers': lambda v: _validate_int_range(v, 0, 64),
    'max_gdal_slots': lambda v: _validate_int_range(v, 1, 16),
    # --- 磁盘预算（§4.2）---
    'disk_reserve_mb': lambda v: _validate_int_range(v, 0, 1024 * 1024),
    # 下界 1.0 = 不加安全余量；上界 3.0 已经离谱到该改估算器而不是拧系数。
    'disk_safety_factor': lambda v: _validate_float_range(v, 1.0, 3.0),
    # --- 缓存容量（§4.6）。0 = 不限，与 GeoD settings.rs:141 的 0 语义一致。---
    'cache_max_mb': lambda v: _validate_int_range(v, 0, 64 * 1024 * 1024),
    # --- 每任务日志（§4.5）---
    'task_log_max_kb': lambda v: _validate_int_range(v, 64, 1024 * 1024),
    'task_log_retain_days': lambda v: _validate_int_range(v, 1, 3650),
    # --- 地名搜索（§5.1）---
    'geocoder_url': _validate_geocoder_url,
}

# 有意不加约束的键。留在这里不是「忘了」，每一条都有理由：
#   - 布尔开关：读取侧一律 `!= 'false'` / `== 'true'`，脏值等价于取默认，
#     没有可被利用的失败模式；
#   - 凭据与枚举：earthdata_* 是自由文本；default_style / default_output_format /
#     gdal_* / contour_zoom_scaling / contour_hillshade_blend 的合法取值表住在
#     各自的引擎里，在这里再抄一份就是第二处事实来源；
#   - contour_* 里剩下的数值（线宽 / index_step / detail_zoom / hillshade_*）：
#     取值表住在 contour_engine 的 ContourStyle 里，在这里再抄一份就是第二处
#     事实来源；颜色与间距已按 P1#10 / P1#11 登记到 _VALUE_RULES。
#   - terrain_global_base_maxzoom：纯数值上限，同上不在本条范围。
#     terrain_local_maxzoom 自 2026-08-10 起**不再是纯数值**：合法取值是三态
#     （数字 / 字面量 'auto' / 空串=没配过），那张取值表住在
#     geo_validation.coerce_maxzoom 里，在这里抄一份就是第二处事实来源 ——
#     而且抄漏 'auto' 就等于把出厂默认判成非法。读取侧三处（两个管理器 +
#     main._terrain_form_defaults）一律过 coerce_maxzoom，脏值退回自动挡并留
#     warning，所以这里放行不构成静默失败。
#   - terrain_vertex_normals：布尔开关，按本表第一条理由；
#     terrain_quality_preset 反过来登记在 _VALUE_RULES —— 它的取值表只有三个
#     值且住在 geo_validation 里，直接 import 白名单不构成第二处事实来源。
#   - disk_budget_enabled / task_log_enabled：布尔开关，按第一条理由。
#     同批新增的数值键（max_* / disk_* / cache_max_mb / task_log_*）全部
#     登记在 _VALUE_RULES —— 它们的合法区间只此一份，不住在别的模块里。
_UNCONSTRAINED_KEYS = frozenset({
    # default_output_format 随 DEFAULT_CONFIGS 里那一行一起删（2026-08-15）：
    # 零消费者的键不需要「已知无校验」这条豁免。
    'default_style',
    'proxy_auto_detect', 'cache_enabled', 'dem_cache_enabled',
    'terrain_vertex_normals',
    'gdal_compression', 'gdal_resampling',
    'disk_budget_enabled', 'task_log_enabled',
    'earthdata_username', 'earthdata_password',
    'terrain_global_base_maxzoom', 'terrain_local_maxzoom',
    'contour_width_intermediate', 'contour_width_index',
    'contour_index_step', 'contour_detail_zoom',
    'contour_zoom_scaling', 'contour_hypsometric_breaks',
    'contour_hillshade_azimuth', 'contour_hillshade_altitude',
    'contour_hillshade_vert_exag', 'contour_hillshade_blend',
})


class ConfigManager:
    """
    Configuration management service with validation

    Manages application configuration stored in the database config table.
    Provides methods to get, set, validate, and reset configuration values.

    Validation Rules:
        - concurrent_downloads: 1-100
        - request_timeout: 1-300 seconds
        - max_retries: 0-10
        - map_center_lat: -90 to 90
        - map_center_lng: -180 to 180
        - map_initial_zoom: 0-21
        - default_zoom_min: 0-21
        - default_zoom_max: 0-21
    """

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Get configuration value by key

        Args:
            key: Configuration key to retrieve
            default: Default value to return if key not found

        Returns:
            Configuration value as string, or default if not found
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    'SELECT value FROM config WHERE key = ?',
                    (key,)
                )

                row = cursor.fetchone()

                if row:
                    return row['value']
                return default

        except sqlite3.Error as e:
            # 区分“无行”（上面返回 default）与“出错”：锁/IO 异常若静默吞成默认值，
            # earthdata_username 会被读成 '' 导致莫名 401，极难排查。记 error 后抛出。
            logger.error(f'Failed to get config {key}: {e}')
            raise

    def get_many(self, keys) -> Dict[str, Optional[str]]:
        """
        Get multiple configuration values in one connection / one query

        Args:
            keys: Iterable of configuration keys to retrieve

        Returns:
            Dict mapping each requested key to its stored value; keys without a
            config row map to None (mirrors get(key, default=None) semantics).
        """
        keys = list(keys)
        if not keys:
            return {}
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                placeholders = ','.join('?' * len(keys))
                cursor.execute(
                    f'SELECT key, value FROM config WHERE key IN ({placeholders})',
                    keys
                )

                result = {key: None for key in keys}
                for row in cursor.fetchall():
                    result[row['key']] = row['value']
                return result

        except sqlite3.Error as e:
            # 与 get 同口径：锁/IO 异常不静默吞成默认值，记 error 后抛出
            logger.error(f'Failed to get configs {keys}: {e}')
            raise

    def set(self, key: str, value: str) -> bool:
        """
        Set configuration value with validation and timestamp update

        Args:
            key: Configuration key to set
            value: Configuration value to set

        Returns:
            True if successful

        Raises:
            ValueError: If validation fails for the given key-value pair
            sqlite3.Error: If database operation fails
        """
        # Validate the configuration value
        if not self.validate_config(key, value):
            raise ValueError(f'Invalid value for config key {key}: {value}')

        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute(_CONFIG_UPSERT_SQL, (key, value, utc_now_iso()))

                conn.commit()

                # Avoid leaking secrets into logs (still allow saving them normally).
                logger.info(f'Config updated: {key} = {_mask_log_value(key, value)}')
                return True

        except sqlite3.Error as e:
            logger.error(f'Failed to set config {key}: {e}')
            raise

    def set_many(self, items: Dict[str, str]) -> bool:
        """
        Set multiple configuration values in a single transaction

        Validates every item up front, then writes all rows with one executemany
        and a single commit — a failure rolls the whole batch back, so callers
        never observe a half-updated configuration (unlike looping over set()).

        Args:
            items: Mapping of configuration key -> value

        Returns:
            True if successful (empty mapping is a no-op success)

        Raises:
            ValueError: If validation fails for any key-value pair
            sqlite3.Error: If database operation fails
        """
        # 全部键先校验完再碰库：任一键非法就整体拒绝，不产生半更新状态
        for key, value in items.items():
            if not self.validate_config(key, value):
                raise ValueError(f'Invalid value for config key {key}: {value}')

        if not items:
            return True

        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                try:
                    now = utc_now_iso()
                    cursor.executemany(
                        _CONFIG_UPSERT_SQL,
                        [(key, value, now) for key, value in items.items()]
                    )
                    conn.commit()
                except sqlite3.Error:
                    # 显式回滚，保证整批原子性（单连接单事务，要么全成要么全不成）
                    conn.rollback()
                    raise

                for key, value in items.items():
                    logger.info(f'Config updated: {key} = {_mask_log_value(key, value)}')
                return True

        except sqlite3.Error as e:
            logger.error(f'Failed to set configs {sorted(items)}: {e}')
            raise

    def get_all(self) -> Dict[str, Any]:
        """
        Get all configuration values as dictionary

        Returns:
            Dictionary with all configuration key-value pairs
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                cursor.execute('SELECT key, value, updated_at FROM config')
                rows = cursor.fetchall()

                result = {}
                for row in rows:
                    result[row['key']] = {
                        'value': row['value'],
                        'updated_at': row['updated_at']
                    }

                return result

        except sqlite3.Error as e:
            logger.error(f'Failed to get all configs: {e}')
            raise

    def reset_to_defaults(self) -> bool:
        """
        Reset all configuration to default values

        Deletes all existing configuration and re-inserts the DEFAULT_CONFIGS
        rows (47 as of 0.2.12). Uses explicit transaction with rollback on error
        to ensure data safety.

        M6：重插之后必须跑一次 default_save_path 归一化。DEFAULT_CONFIGS 里
        那一项是相对值 './downloads'，而 validate_config 自己会判它非法 ——
        本方法绕过了 set/set_many 的校验，不归一的话 reset 会把一个非法值写
        回库，之后地图/DEM 建任务全部 400「保存路径必须是绝对路径」（重启时
        init_database 会静默修好，所以现象很不自明）。

        Returns:
            True if successful

        Raises:
            sqlite3.Error: If database operation fails
        """
        try:
            with get_connection_context() as conn:
                cursor = conn.cursor()

                try:
                    # Delete all existing config
                    cursor.execute('DELETE FROM config')

                    # Insert default configurations
                    cursor.executemany(
                        'INSERT INTO config (key, value) VALUES (?, ?)',
                        DEFAULT_CONFIGS
                    )

                    normalize_default_save_path(cursor)

                    conn.commit()
                    logger.info('Configuration reset to defaults')
                    return True

                except sqlite3.Error as e:
                    conn.rollback()
                    logger.error(f'Failed to reset config to defaults: {e}')
                    raise

        except sqlite3.Error:
            raise

    def validate_config(self, key: str, value: str) -> bool:
        """按 _VALUE_RULES 里登记的规则校验单个配置值。

        协议不变：返回 bool，False 由 set / set_many / routes.api.update_config
        转成带键名的 ValueError / 400。

        未登记的键返回 True —— PUT /api/config 只放行 DEFAULT_CONFIGS 里的键，
        而 DEFAULT_CONFIGS 的每一个键都必须在 _VALUE_RULES 或
        _UNCONSTRAINED_KEYS 中出现（覆盖用例钉住）。这里保留宽松兜底是为了
        内部代码写临时键时不被拦下。
        """
        rule = _VALUE_RULES.get(key)
        if rule is None:
            return True
        try:
            return bool(rule(value))
        except (ValueError, TypeError):
            return False

    def _is_valid_lat(self, value: str) -> bool:
        """Validate latitude value (-90..90)."""
        try:
            return _is_valid_lat(value)
        except (ValueError, TypeError):
            return False

    def _is_valid_lng(self, value: str) -> bool:
        """Validate longitude value (-180..180)."""
        try:
            return _is_valid_lng(value)
        except (ValueError, TypeError):
            return False
