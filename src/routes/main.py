"""
Main page routes

Handles rendering of HTML pages for the web interface.
"""

import logging
from flask import Blueprint, render_template
from src.services.basemap_source import client_descriptor, resolve_basemap
from src.services.config_manager import ConfigManager, redact_secret_value

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
        basemap = client_descriptor(resolve_basemap(
            template_config.get('basemap_source'),
            tile_servers=template_config.get('tile_servers'),
            default_style=template_config.get('default_style', 'm'),
        ))

        return render_template('index.html', config=template_config,
                               map_config=map_config, basemap=basemap)

    except Exception as e:
        logger.error(f"Error rendering index page: {e}")
        return render_template('index.html', config={}, map_config={},
                               basemap=client_descriptor(resolve_basemap(None)))


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
