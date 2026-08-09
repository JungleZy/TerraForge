"""
Main page routes

Handles rendering of HTML pages for the web interface.
"""

import logging
from flask import Blueprint, render_template
from src.services.basemap_source import client_descriptor, resolve_basemap
from src.routes.basemap_static import active_basemap
from src.services.config_manager import ConfigManager, redact_secret_value
from src.services.geo_validation import (DEFAULT_TILING_QUALITY,
                                         TILING_QUALITY_OFFSETS, validate_zoom)

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


# 出厂默认，与 `database.DEFAULT_CONFIGS` 里的 terrain_local_maxzoom 逐字一致
# —— 兜底值和出厂默认不一致就会造出「改了没反应」的假旋钮
# （local_terrain_task_manager._default_quality 为另一个配置键定过同一条规矩）。
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
    等高线任务也一起建不了。

    空值与缺键都算「没配过」，不告警：config={} 的异常兜底渲染和被清空的字段都走
    这条路，每刷一次首页就刷一条 warning 只会把真正的脏值淹掉。

    Args:
        cfg: _flat_config() 的扁平配置（异常兜底路径传 {}）

    Returns:
        (maxzoom, preset)：maxzoom 保证落在 [MIN_ZOOM, MAX_ZOOM] 内，
        preset 保证是 TILING_QUALITY_OFFSETS 里的键。
    """
    maxzoom = _FACTORY_LOCAL_MAXZOOM
    raw_zoom = cfg.get('terrain_local_maxzoom') or ''
    if raw_zoom != '':
        try:
            # 与两个管理器同一把尺（validate_zoom），不在这里另抄一份 0/21 的上下界。
            maxzoom = validate_zoom(raw_zoom, 'terrain_local_maxzoom')
        except Exception as e:
            logger.warning(
                f"配置 terrain_local_maxzoom={raw_zoom!r} 不可用({e})，"
                f"处理表单初值改用出厂默认 {maxzoom}")

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

    return maxzoom, preset


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
        )))

        # 模板不再自己钳层级、自己兜档位：那两处静默修复在这里做完并留日志，
        # 详见 _terrain_form_defaults。
        maxzoom, quality = _terrain_form_defaults(template_config)

        return render_template('index.html', config=template_config,
                               map_config=map_config, basemap=basemap,
                               terrain_local_maxzoom=maxzoom,
                               terrain_quality_preset=quality)

    except Exception as e:
        logger.error(f"Error rendering index page: {e}")
        # 空配置走同一个函数拿出厂默认：这条路径上什么都没被丢弃，
        # 不该再刷一条「配置不可用」的假警报（上面那条 error 已经说清了病因）。
        maxzoom, quality = _terrain_form_defaults({})
        return render_template('index.html', config={}, map_config={},
                               basemap=client_descriptor(resolve_basemap(None)),
                               terrain_local_maxzoom=maxzoom,
                               terrain_quality_preset=quality)


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
