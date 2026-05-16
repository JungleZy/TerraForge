"""
Flask routes package

Exports main_bp and api_bp blueprints for application routing.
"""

from routes.main import main_bp
from routes.api import api_bp
from routes.dem_api import dem_api_bp
from routes.terrain_api import terrain_api_bp
from routes.terrain_static import terrain_static_bp

__all__ = ['main_bp', 'api_bp', 'dem_api_bp', 'terrain_api_bp', 'terrain_static_bp']
