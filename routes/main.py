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
        # Get all configuration for the page
        config = config_manager.get_all()

        # Extract values for template
        template_config = {
            'map_center_lat': config.get('map_center_lat', {}).get('value', '39.9'),
            'map_center_lng': config.get('map_center_lng', {}).get('value', '116.4'),
            'map_initial_zoom': config.get('map_initial_zoom', {}).get('value', '10'),
            'default_style': config.get('default_style', {}).get('value', 'm'),
            'default_zoom_min': config.get('default_zoom_min', {}).get('value', '10'),
            'default_zoom_max': config.get('default_zoom_max', {}).get('value', '15'),
            'default_output_format': config.get('default_output_format', {}).get('value', 'both'),
            'default_save_path': config.get('default_save_path', {}).get('value', './downloads'),
        }

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
        config_data = config_manager.get_all()

        return render_template('config.html', config=config_data)

    except Exception as e:
        logger.error(f"Error rendering config page: {e}")
        return "Error loading config page", 500
