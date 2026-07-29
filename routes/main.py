"""
Main page routes

Handles rendering of HTML pages for the web interface.
"""

import logging
from flask import Blueprint, render_template
from services.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# Create main blueprint
main_bp = Blueprint('main', __name__)

# Initialize config manager
config_manager = ConfigManager()


@main_bp.route('/')
def index():
    """
    Render main index page with map interface

    Returns:
        Rendered index.html template with configuration data
    """
    try:
        # Get all configuration for the page（与 /config 相同的扁平化结构：
        # 配置面板作为 partial 嵌在首页里，需要全量键）
        config_raw = config_manager.get_all()
        template_config = {}
        for key, data in config_raw.items():
            template_config[key] = data.get('value', '')

        return render_template('index.html', config=template_config)

    except Exception as e:
        logger.error(f"Error rendering index page: {e}")
        return render_template('index.html', config={})


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
        # Get all configuration
        config_raw = config_manager.get_all()

        # Flatten the config structure for template
        config_data = {}
        for key, data in config_raw.items():
            config_data[key] = data.get('value', '')

        return render_template('config.html', config=config_data)

    except Exception as e:
        logger.error(f"Error rendering config page: {e}")
        return "Error loading config page", 500
