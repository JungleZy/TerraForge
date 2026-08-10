"""
Main page routes

Handles rendering of HTML pages for the web interface.
"""

import logging
from flask import Blueprint, render_template
from src.core.tile_server import current_tile_port
from src.services.basemap_source import client_descriptor, resolve_basemap
from src.routes.basemap_static import active_basemap
from src.services.config_manager import ConfigManager, redact_secret_value
from src.services.geo_validation import (AUTO_MAXZOOM, DEFAULT_TILING_QUALITY,
                                         TILING_QUALITY_OFFSETS, coerce_maxzoom)

logger = logging.getLogger(__name__)

# Create main blueprint
main_bp = Blueprint('main', __name__)

# Initialize config manager
config_manager = ConfigManager()

# 首页 JS 全局 `config` 只需要这几个键 —— static/js/map.js 的**全部**读取点
# （initMap 的中心/缩放、表单默认 zoom、默认保存路径）。
#
# 不能把 get_all() 整个 tojson 下发:那 45 个键里有 earthdata_password 与
# proxy_url（含 user:pass@），等于把凭据放进一个任何脚本/扩展都读得到的页面级
# 全局；而 tile_servers 会把上游地址递回浏览器，正好绕开
# basemap_source.client_descriptor 特意剥掉 upstream 的那道门（那个 docstring 写着
# 「前端一旦拿到上游地址就会有人图省事直连回去」）。
# 见 docs/reviews/2026-08-08-full-project-review.md 的「安全姿态」第 1 项。
MAP_CONFIG_KEYS = ('default_save_path', 'default_zoom_min', 'default_zoom_max',
                   'map_center_lat', 'map_center_lng', 'map_initial_zoom')


def _flat_config():
    """config 表扁平化成 {key: value}，密钥类键换成哨兵（redact_secret_value）。

    配置**表单**需要全量键才能渲染，所以这里不做白名单 —— 白名单是给
    MAP_CONFIG_KEYS 那个 JS 全局用的。
    """
    return {key: redact_secret_value(key, data.get('value', ''))
            for key, data in config_manager.get_all().items()}


# 自动挡下数字框仍要渲染的那个数。它**不是**「没配过时的行为」——
# 缺配置/脏配置一律回落到自动挡（与 local_terrain_task_manager._default_maxzoom
# 同一口径），这里只负责给数字框一个合法初值：它是用户取消勾选后的起点。
# 空 value 本身**不会**让表单 :invalid —— min/max 只对有值的控件成立
# （rangeUnderflow/rangeOverflow 对空值不适用），空值要 required 才触发
# valueMissing，而这个控件没有 required（本仓的浏览器模拟器
# tests/test_config_form_submittable.py:53-54 对空 value 也是直接跳过）。
# 不渲染这个数的真实后果是静默的：用户取消勾选拿到一个空数字框，提交时送空串，
# 后端 coerce_maxzoom 把空串当「未表态」→ 回落到配置默认（也就是他刚取消掉的
# 自动挡），取消勾选看起来毫无效果。这个值同时是 test_config_form_submittable
# 那七个越界用例的锚。真会让表单 :invalid 的是越界值（value="99"），见下面的
# docstring。
_FACTORY_LOCAL_MAXZOOM = 14


def _terrain_form_defaults(cfg):
    """把处理表单的两个地形初值在**服务端**收干净，每改写一个键留一条 warning。

    为什么不放在模板里：模板是这条链路上唯一记不了日志的一环。它此前自己钳层级、
    自己拿「均衡」那条 option 当兜底档，于是库里的 99 渲染成 14、'ultra' 渲染成
    均衡，页面看起来一切正常，运维要等到真起了切片作业才由
    local_terrain_task_manager._default_maxzoom 的那条 warning 吭一声。
    档位那半边还两个入口互相打架：
    同一个脏值从历史页详情面板起切（不带 body、走 validate_tiling_quality）是当场
    400，从这张表单却被静默改写成 balanced 一路切完 —— 一个入口硬拒、另一个悄悄改。
    这里是配置进模板前最后一个还有 logger 的地方，所以收口放在这里。

    层级那次钳位本身必须保留（只是补上日志）：terrain_local_maxzoom 登记在
    config_manager._UNCONSTRAINED_KEYS，写入侧不校验，PUT /api/config 收得下 99；
    照直渲染成 value="99" 会违反控件自己的 min/max，让整张 #processForm 变
    :invalid —— 原生校验拦下 submit 事件，map.js 的监听根本不触发，「创建」点了
    没反应，而 #localTerrainOptions 只用 hidden 藏、字段不 disable，连与地形无关的
    等高线任务也一起建不了。'auto' 塞不进这个数字框，但坏法**不是**同理：
    type="number" 的 value sanitization 会把非数字 value 直接置空，而这个控件没有
    required，空值不触发任何 constraint violation —— value="auto" 在浏览器里等于
    一个空数字框，表单照样提交得了。它的后果是静默的：用户看到空框，取消勾选后
    提交送空串，后端 coerce_maxzoom 把空串当「未表态」→ 回落到配置默认（正是他
    刚取消掉的自动挡），取消勾选毫无效果。所以 'auto' 由复选框那一态表达，
    数字框仍渲染 _FACTORY_LOCAL_MAXZOOM。

    脏值退回**自动挡**而不是某个写死的层级：与
    local_terrain_task_manager._default_maxzoom 同一口径。同一份坏配置，从这张表单
    和从两个管理器走，切出来的层级必须一样，否则「表单建的任务」和「详情面板起切」
    又成了两套行为。

    空值与缺键都算「没配过」，不告警：config={} 的异常兜底渲染和被清空的字段都走
    这条路，每刷一次首页就刷一条 warning 只会把真正的脏值淹掉。

    Args:
        cfg: _flat_config() 的扁平配置（异常兜底路径传 {}）

    Returns:
        (maxzoom, maxzoom_auto, preset)：maxzoom 保证落在 [MIN_ZOOM, MAX_ZOOM] 内
        （自动挡下是数字框的起点值，不是要提交的值），maxzoom_auto 是「自动」
        复选框的勾选态，preset 保证是 TILING_QUALITY_OFFSETS 里的键。
    """
    maxzoom = _FACTORY_LOCAL_MAXZOOM
    maxzoom_auto = True
    raw_zoom = cfg.get('terrain_local_maxzoom') or ''
    if raw_zoom != '':
        try:
            # 与两个管理器同一把尺（coerce_maxzoom），不在这里另抄一份 0/21 的
            # 上下界，也不在这里另认一次 'auto' 字面量。
            value = coerce_maxzoom(raw_zoom, 'terrain_local_maxzoom')
        except Exception as e:
            logger.warning(
                f"配置 terrain_local_maxzoom={raw_zoom!r} 不可用({e})，"
                f"处理表单初值改用出厂默认 {AUTO_MAXZOOM!r}")
        else:
            # 数字框在自动挡下仍渲染 _FACTORY_LOCAL_MAXZOOM —— 出厂默认已经是
            # 'auto'，这个 14 只是用户取消勾选后的起点。
            maxzoom_auto = value == AUTO_MAXZOOM
            if not maxzoom_auto:
                maxzoom = value

    preset = DEFAULT_TILING_QUALITY
    raw_preset = cfg.get('terrain_quality_preset') or ''
    if raw_preset != '':
        # 白名单直接取 geo_validation 的取值表，不抄第二份三个档位名
        # （config_manager._VALUE_RULES 里 terrain_quality_preset 那条定的规矩）。
        if raw_preset in TILING_QUALITY_OFFSETS:
            preset = raw_preset
        else:
            logger.warning(
                f"配置 terrain_quality_preset={raw_preset!r} 不在取值表 "
                f"{sorted(TILING_QUALITY_OFFSETS)} 内，处理表单初值改用出厂默认 "
                f"{preset}（同一个值从历史页详情面板起切会直接 400）")

    return maxzoom, maxzoom_auto, preset


@main_bp.route('/')
def index():
    """
    Render main index page with map interface

    Returns:
        Rendered index.html template with configuration data
    """
    try:
        # 配置面板作为 partial 嵌在首页里，表单需要全量键（密钥已换哨兵）。
        template_config = _flat_config()
        # JS 全局只拿 map.js 真正读的那几个键，见 MAP_CONFIG_KEYS 的注释。
        map_config = {key: template_config.get(key, '') for key in MAP_CONFIG_KEYS}

        # 底图在**服务端**解析（src/services/basemap_source.py），下发给前端的
        # 只有同源路径 /basemap/{z}/{x}/{y} —— 真实上游地址不出服务端。
        # 瓦片由 routes/basemap_static.py 转发：浏览器直连上游会撞 CORS，
        # 而且不吃项目的 proxy_url。
        # 报的是**实际在出图**的那个源（active_basemap）：配置的源取不到瓦片时
        # 后端会自动回退到链上的下一张（见 basemap_static.fallback_candidates），
        # 描述符还报配置值的话，max_level / 署名 / 界面提示全是错的。
        basemap = client_descriptor(active_basemap(resolve_basemap(
            template_config.get('basemap_source'),
            tile_servers=template_config.get('tile_servers'),
            default_style=template_config.get('default_style', 'm'),
        )), tile_port=current_tile_port())

        # 模板不再自己钳层级、自己兜档位：那两处静默修复在这里做完并留日志，
        # 详见 _terrain_form_defaults。
        maxzoom, maxzoom_auto, quality = _terrain_form_defaults(template_config)

        # 偏移表原样下发给模板，渲染进 <option data-offset>：起切前的规模预告
        # 要按「基准 + 偏移」算实际层级，而 map.js 里不许有第二份取值表。
        return render_template('index.html', config=template_config,
                               map_config=map_config, basemap=basemap,
                               terrain_local_maxzoom=maxzoom,
                               terrain_local_maxzoom_auto=maxzoom_auto,
                               terrain_quality_preset=quality,
                               terrain_quality_offsets=TILING_QUALITY_OFFSETS)

    except Exception as e:
        logger.error(f"Error rendering index page: {e}")
        # 空配置走同一个函数拿出厂默认：这条路径上什么都没被丢弃，
        # 不该再刷一条「配置不可用」的假警报（上面那条 error 已经说清了病因）。
        maxzoom, maxzoom_auto, quality = _terrain_form_defaults({})
        return render_template('index.html', config={}, map_config={},
                               basemap=client_descriptor(
                                   resolve_basemap(None),
                                   tile_port=current_tile_port()),
                               terrain_local_maxzoom=maxzoom,
                               terrain_local_maxzoom_auto=maxzoom_auto,
                               terrain_quality_preset=quality,
                               terrain_quality_offsets=TILING_QUALITY_OFFSETS)


@main_bp.route('/history')
def history():
    """
    Render task history page

    Returns:
        Rendered history.html template
    """
    try:
        return render_template('history.html')
    except Exception as e:
        logger.error(f"Error rendering history page: {e}")
        return "Error loading history page", 500


@main_bp.route('/config')
def config():
    """
    Render configuration page with current settings

    Returns:
        Rendered config.html template with configuration data
    """
    try:
        return render_template('config.html', config=_flat_config())

    except Exception as e:
        logger.error(f"Error rendering config page: {e}")
        return "Error loading config page", 500
