"""地名搜索与行政区查询 —— 机制完整交付，数据源**故意留空**。

为什么不内置任何 geocoder（这是本模块最重要的一条决定，不是偷懒）：

- `docs/notes/external-projects-takeaways.md` §11 明确写着「不内置未经政策
  审核的公共或商业批量下载源；不在源码里硬编码第三方 token」——那一条本来
  是冲着 GeoD 硬编码公共天地图 token（`config.rs:55`）去的，地名服务是同一
  类东西：内置一个 Nominatim / 高德 / 天地图地址，等于替使用者接受了对方的
  ToS 与调用配额。
- §13 收尾那段把「行政区与地名搜索的数据源与测绘合规」列为**尚未拍板**的
  产品问题，并直接点名这是本文的内部矛盾：§10 阶段 2 的门槛里写了这个功能，
  §5.1 正文却没给来源。在源码里硬编码一个地址，就是把这个还没做的产品决定
  悄悄替产品层做了，而且做在一个没人会 review 的常量里。

所以：能力做完整（取回、SSRF 防护、两种响应形态归一、几何校验、行政区
多边形贯通到 RegionSpec），出厂时 `geocoder_url` 为空、功能关闭。使用者把
自己的（自建的、或已确认合规的）服务地址填进配置页，功能才亮。配置校验在
`config_manager._validate_geocoder_url`：必须 http(s)、必须含 `{q}`。

取回一律走 `src.services.url_guard.guarded_request`，不在这里另写一个
fetcher —— 一个「使用者粘进配置页的任意 URL」正是 §11-3 所说的 SSRF 面，
而 url_guard 就是为它存在的。这里传 `allow_private=True`：自建的局域网
Nominatim（`http://192.168.x.x:8080/search?q={q}`）是本项目最可能的正经用法，
一刀切禁私网会把唯一合规的部署方式也堵死；`config_manager` 那边同样理由拒绝
在配置期做私网判定。威胁模型按 §13-5 已降级为「只面向可信本机/LAN」。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit

from src.contracts.region import RegionSpec, RegionValidationError
from src.services import url_guard
from src.services.config_manager import ConfigManager

logger = logging.getLogger(__name__)

__all__ = [
    'GeocodingDisabled',
    'MAX_RESPONSE_BYTES',
    'geocoder_configured',
    'search_places',
]

# 配置键。默认值 '' 在 src/core/database.py DEFAULT_CONFIGS 里，校验规则在
# src/services/config_manager.py。这里只读，不重复定义默认值。
CONFIG_KEY = 'geocoder_url'

# limit 的钳位区间。上界 50 不是随手取的：结果要连同行政区多边形一起回给
# 前端，一个省级边界解析后就是几万个顶点，50 条已经足以把响应撑到 MB 级；
# 下界 1 是为了让 limit=0 / 负数不静默变成「一条都不返回」。
MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 8

# 取回超时。地名搜索是交互式操作（用户盯着搜索框），比后台下载更不能久等；
# 自建服务正常应在 1s 内回，10s 已经是「它挂了」的判据。
REQUEST_TIMEOUT = 10

# 响应体上限。默认的 2 MB 对纯 bbox 响应绰绰有余，但一旦服务端开了
# polygon_geojson，单个国家级边界就能超过它 —— 截断后 json 解析失败，
# 用户看到的现象是「搜大区域永远搜不到」。给到 4 MiB，再大就是对方在返回
# 不适合做区域选择的东西了。
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# 名称字段的取值顺序。GeoJSON 属性表没有统一约定：Nominatim 风格用
# display_name，OSM 导出用 name，Esri / 国内行政区 shapefile 转出来的常是
# 大写 NAME。三个都试，取第一个非空的。
_NAME_KEYS = ('name', 'display_name', 'NAME', 'Name', 'title')

# 要素类别字段。给 UI 分组/加图标用，取不到就是空串 —— 不编一个 'place'
# 出来冒充，前端能区分「服务没给」与「服务说它是个 place」。
# `type` 排在 `class` 前面：Nominatim 的这一对是 (class=boundary, type=administrative)
# 或 (class=place, type=city)，两次都是 type 才带信息量，class 只是它的大类。
_KIND_KEYS = ('kind', 'addresstype', 'type', 'class', 'place_type', 'category')


class GeocodingDisabled(RuntimeError):
    """未配置 `geocoder_url` 就调用了搜索。

    刻意**不是** ValueError：路由层把 ValueError 映射成 400（「你的输入不
    对」），而这里的真相是「这台部署根本没开这个功能」，跟用户敲了什么无关。
    路由层应当据此回 501/409 一类的「功能未启用」，并把用户指向配置页。
    """


# ---- 配置读取 -----------------------------------------------------------


def _resolve_url(config_manager=None) -> str:
    """读出配置的地名服务地址；任何读不出来的情况都降级为「未配置」。

    ConfigManager.get 对 sqlite 错误是**有意重抛**的（见其注释：静默吞成默认
    值会造成莫名 401 且极难排查）。但对本功能而言，配置库读不出来的正确行为
    是「搜索框不可用」，而不是让整个页面 500 —— 一个可选的搜索功能没有资格
    因为数据库暂时锁住就打断用户正在做的下载任务。记 warning 后按未配置处理。
    """
    cfg = config_manager or ConfigManager()
    try:
        raw = cfg.get(CONFIG_KEY, '')
    except Exception as exc:
        logger.warning('读取 %s 失败，地名搜索按未配置处理: %s', CONFIG_KEY, exc)
        return ''
    return (raw or '').strip()


def geocoder_configured(config_manager=None) -> bool:
    """地名搜索是否可用。空地址 = 出厂状态 = 关闭（见模块 docstring）。"""
    return bool(_resolve_url(config_manager))


def _log_host(url: str) -> str:
    """日志里只出现主机名。

    地名服务地址常把 API key 写在 query 里（`...&key=xxxx&q={q}`），把整条
    URL 打进日志等于把凭据落盘到 logs/ 并可能随诊断包外发。主机名足以定位
    「是哪个服务在出问题」，这是日志唯一需要回答的问题。
    """
    try:
        return urlsplit(url).netloc or '<unparsable>'
    except ValueError:
        return '<unparsable>'


# ---- 归一化小工具 -------------------------------------------------------


def _clamp_limit(limit) -> int:
    """limit 钳到 [1, 50]；非数字按默认值处理，不抛错。

    limit 通常来自 query string，前端传个空串或 'abc' 是可预期的，为此回
    400 只会让搜索框在边缘输入上莫名其妙地坏掉。
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def _first_str(mapping, keys: Sequence[str]) -> str:
    """按 keys 顺序取第一个非空字符串值。"""
    if not isinstance(mapping, dict):
        return ''
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _kind_of(mapping) -> str:
    """要素类别。GeoJSON 的 `type` 在 properties 里才是类别。

    注意只在 properties 里找 `type`：Feature 顶层的 `type` 恒为 'Feature'，
    误取它会让每一条结果的 kind 都是 'Feature' 这种纯噪声。
    """
    return _first_str(mapping, _KIND_KEYS)


def _geojson_bbox(raw) -> Optional[Tuple[float, float, float, float]]:
    """GeoJSON `bbox` 成员 → (west, south, east, north)。

    RFC 7946 的 bbox 是「所有轴的最小值，再所有轴的最大值」：二维是 4 个数
    `[w, s, e, n]`，三维是 6 个数 `[w, s, minz, e, n, maxz]`。三维形态按 4 个
    数硬取会把高程当成经度，产出一个 bbox 校验勉强能过、位置完全错的区域，
    所以这里显式分支。其它长度视为不可用。
    """
    if not isinstance(raw, (list, tuple)):
        return None
    try:
        if len(raw) == 4:
            w, s, e, n = (float(v) for v in raw)
            return (w, s, e, n)
        if len(raw) == 6:
            w, s, _minz, e, n, _maxz = (float(v) for v in raw)
            return (w, s, e, n)
    except (TypeError, ValueError):
        return None
    return None


def _photon_extent(raw) -> Optional[Tuple[float, float, float, float]]:
    """Photon 的 `properties.extent` → (west, south, east, north)。

    ⚠️ 轴序是 `[minLon, maxLat, maxLon, minLat]` —— **西、北、东、南**，与
    RFC 7946 的 `[w, s, e, n]` 中间两位正好对调。照 GeoJSON 的顺序读会把南北
    互换，`RegionSpec.from_bbox` 拿到 north < south 直接判非法 —— 现象是「搜到了
    但一条结果都不显示」，而日志里只有一句 bbox 非法，很难指回轴序。
    两处独立取证：官方 api-v1.md 的柏林奥林匹克体育场示例
    `[13.23727, 52.5157151, 13.241757, 52.5135972]`（第 2、4 位是纬度，且
    第 2 位更大），以及实测「重庆」的 `[105.29, 32.20, 110.19, 28.16]`。

    为什么要单独认这个非标准字段：Photon 是少数免注册、免 key 的公共地名服务，
    而它**从不给** GeoJSON 的标准 `bbox` 成员，几何又恒为 Point。不认 extent
    的话它每次都返回「搜不到」——响应 200、features 非空，结果全被丢弃。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        west, north, east, south = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (west, south, east, north)


def _validated_bbox(bounds: Tuple[float, float, float, float],
                    label: str) -> Optional[List[float]]:
    """用 RegionSpec.from_bbox 过一遍，返回 [west, south, east, north]。

    校验放在**产出结果之前**而不是交给下游：第三方返回一个 north == south 的
    退化 bbox（点要素常见）或 lat/lon 写反的框，不校验的话它会一路传到任务
    创建才炸，届时错误信息指向的是任务表而不是「那条搜索结果是坏的」。
    这里顺带吃掉 from_bbox 的反经线归一（east < west → east + 360），下游拿
    到的 east 与 RegionSpec 的约定一致。
    """
    west, south, east, north = bounds
    try:
        spec = RegionSpec.from_bbox(north, south, east, west)
    except (RegionValidationError, TypeError, ValueError) as exc:
        logger.debug('丢弃地名结果 %r：bbox 非法 %r（%s）', label, bounds, exc)
        return None
    w, s, e, n = spec.bounds
    return [w, s, e, n]


def _make_region(geojson_obj, name: str) -> Optional[RegionSpec]:
    """GeoJSON 片段 → RegionSpec；没有面几何或几何非法时返回 None。

    `source='administrative'` 是 RegionSpec 合同里就为这条路径留的取值：区域
    带着「我是从行政区搜索来的」这个出身进入任务，历史与诊断里才分得清它和
    用户手绘的框。

    只吞 RegionValidationError 一类的几何问题 —— 一个畸形边界不该让整次搜索
    失败，退化成「这条结果只有 bbox」是可用的降级；而 bbox 都没有的那条会在
    调用处被丢掉。
    """
    if not isinstance(geojson_obj, dict):
        return None
    try:
        return RegionSpec.from_geojson(geojson_obj, source='administrative',
                                       display_name=name)
    except (RegionValidationError, TypeError, ValueError, KeyError) as exc:
        logger.debug('地名结果 %r 的几何不可用，降级为纯 bbox: %s', name, exc)
        return None


def _result(name: str, kind: str, bbox: List[float],
            region: Optional[RegionSpec]) -> Dict[str, Any]:
    return {
        'name': name,
        'bbox': bbox,
        'kind': kind,
        # region 非 None 才是真正的「行政区选择」：有多边形，下游掩膜能按边界
        # 挖洞；只有 bbox 时给 None，前端据此知道选中的是个矩形近似而不是
        # 一个假装成边界的方框。
        'region': region.to_dict() if region is not None else None,
    }


# ---- 两种响应形态 -------------------------------------------------------


def _parse_feature_collection(features, limit: int) -> List[Dict[str, Any]]:
    """形态一：GeoJSON FeatureCollection。"""
    out: List[Dict[str, Any]] = []
    for feature in features:
        if len(out) >= limit:
            break
        if not isinstance(feature, dict):
            continue
        props = feature.get('properties')
        props = props if isinstance(props, dict) else {}
        name = _first_str(props, _NAME_KEYS) or _first_str(feature, _NAME_KEYS)
        kind = _kind_of(props)

        # 单要素包装是必须的，不是多此一举：RegionSpec.from_geojson 会把传进去
        # 的 FeatureCollection 里**所有**要素合并成一个 MultiPolygon（「用户选了
        # 一个文件就是想下整个文件的范围」）。整包传进去，十条搜索结果会糊成
        # 一个横跨半个国家的区域。逐要素包一层，隔离出这一条的几何。
        region = _make_region(
            {'type': 'FeatureCollection', 'features': [feature]}, name)

        # bbox 取值顺序：要素自带的标准 bbox 优先（服务端算的才是它对这条要素的
        # 权威范围），其次 Photon 的非标准 properties.extent（同样是服务端算的），
        # 最后才从几何反推。
        bounds = _geojson_bbox(feature.get('bbox'))
        if bounds is None:
            bounds = _photon_extent(props.get('extent'))
        if bounds is None and region is not None:
            bounds = region.bounds
        if bounds is None:
            logger.debug('丢弃地名结果 %r：既无 bbox / extent 也无面几何', name)
            continue

        bbox = _validated_bbox(bounds, name)
        if bbox is None:
            continue
        out.append(_result(name, kind, bbox, region))
    return out


def _parse_nominatim(items, limit: int) -> List[Dict[str, Any]]:
    """形态二：Nominatim 风格的 JSON 数组。"""
    out: List[Dict[str, Any]] = []
    for item in items:
        if len(out) >= limit:
            break
        if not isinstance(item, dict):
            continue
        raw_box = item.get('boundingbox')
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            logger.debug('丢弃地名结果：boundingbox 形态不对 %r', raw_box)
            continue
        try:
            # Nominatim 的顺序是 [south, north, west, east]，**不是** GeoJSON 的
            # [w, s, e, n]，而且元素是字符串。按 GeoJSON 顺序读会得到一个
            # 经纬互换的区域：它多半还能通过取值域校验（都在 ±90 内的地方尤其
            # 危险），于是静默地下载了地球另一边的一块地。
            south, north, west, east = (float(v) for v in raw_box)
        except (TypeError, ValueError):
            logger.debug('丢弃地名结果：boundingbox 非数值 %r', raw_box)
            continue

        name = _first_str(item, _NAME_KEYS)
        kind = _kind_of(item)
        # 服务端带了 polygon_geojson=1 时才有 `geojson` 成员，是一个裸 Geometry。
        # 有就用 —— 这一条决定了返回的是「行政区」还是「一个方框」。
        region = _make_region(item.get('geojson'), name)

        bbox = _validated_bbox((west, south, east, north), name)
        if bbox is None:
            continue
        out.append(_result(name, kind, bbox, region))
    return out


def _shape_name(data) -> str:
    """给日志用的响应形态描述，只说结构不说内容（内容可能含地址/凭据）。"""
    if isinstance(data, dict):
        keys = ','.join(sorted(data)[:6]) or '<empty>'
        return f'object(type={data.get("type")!r}, keys={keys})'
    if isinstance(data, list):
        return f'array(len={len(data)}, first={type(data[0]).__name__ if data else "-"})'
    return type(data).__name__


# ---- 对外入口 -----------------------------------------------------------


def search_places(query: str, *, limit: int = DEFAULT_LIMIT,
                  config_manager=None) -> List[Dict[str, Any]]:
    """地名 / 行政区搜索。返回归一化结果列表，最多 limit 条。

    结果形态：`{'name', 'bbox': [west, south, east, north], 'kind', 'region'}`。
    `region` 是 `RegionSpec.to_dict()`（响应带面几何时）或 None（只有 bbox）。

    异常边界（有意划在这里）：
    - 未配置 → GeocodingDisabled，功能没开，不是搜不到；
    - 响应体畸形 / 形态不认识 / 非 2xx → 记日志返回 []，**绝不抛**。第三方返回
      一坨垃圾不该让本应用 500，用户该看到的是「没有结果」。非 2xx 走这条是
      因为 guarded_request 有意把 403/429 当作有效信息回传而不是抛；
    - 取回本身失败（超时、连接拒绝、被 url_guard 拦下、响应体超过 max_bytes）
      → 异常原样上抛。这类失败**不能**吞成空列表：吞了之后自建服务没起来的
      现象是「搜什么都搜不到」，使用者会去怀疑自己的关键词，而真正该做的是去
      看服务。路由层能据类型区分：UrlNotAllowed 是 ValueError（地址配错了 /
      响应过大，400），其余是服务不可达。
    """
    url_template = _resolve_url(config_manager)
    if not url_template:
        raise GeocodingDisabled(
            f'geocoding is disabled: config key {CONFIG_KEY!r} is empty; '
            f'set it to a place-search endpoint containing the {{q}} placeholder')

    text = (query or '').strip()
    if not text:
        # 空关键词不发请求：既省一次往返，也避免某些服务对空 q 返回「全世界」。
        return []

    limit = _clamp_limit(limit)
    # safe='' 让 `/`、`&`、`?` 也被编码 —— 关键词里的斜杠若原样拼进 path 形态的
    # 模板（`.../search/{q}`），会凭空多出一级路径；`&` 则能往 query 里注入参数。
    url = url_template.replace('{q}', quote(text, safe=''))

    status, _headers, body = url_guard.guarded_request(
        url,
        timeout=REQUEST_TIMEOUT,
        # 超限是抛异常而不是截断（见 url_guard），所以这里永远拿不到半截 JSON：
        # 要么完整，要么明确失败，不会退化成「解析失败 → 搜不到」这种假象。
        max_bytes=MAX_RESPONSE_BYTES,
        # 自建的局域网 Nominatim 是本功能最现实的合规部署方式，必须放行私网。
        allow_private=True,
        # 透传而不是让 url_guard 自己 new 一个：它拿这个解析 proxy_url。调用方
        # 指定了配置来源，代理决策却读另一份配置，就是同一次请求两套事实。
        config_manager=config_manager,
    )

    if not 200 <= int(status) < 300:
        logger.warning('地名服务 %s 返回 HTTP %s，本次搜索无结果',
                       _log_host(url), status)
        return []

    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning('地名服务 %s 的响应不是 JSON（%d 字节）: %s',
                       _log_host(url), len(body or b''), exc)
        return []

    # FeatureCollection 判据放宽到「有 features 数组」：不少服务省略了 type，
    # 但结构完全合规，为一个缺失的字面量把可用的响应判死不值当。
    if isinstance(data, dict) and isinstance(data.get('features'), list):
        return _parse_feature_collection(data['features'], limit)
    if isinstance(data, list):
        return _parse_nominatim(data, limit)

    logger.warning(
        '地名服务 %s 的响应形态不认识：%s；本模块只认 GeoJSON FeatureCollection '
        '与 Nominatim 风格数组', _log_host(url), _shape_name(data))
    return []
