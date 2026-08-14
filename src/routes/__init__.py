"""
Flask routes package

Exports main_bp and api_bp blueprints for application routing.
"""

from src.routes.main import main_bp
from src.routes.api import api_bp
from src.routes.dem_api import dem_api_bp
from src.routes.terrain_api import terrain_api_bp
from src.routes.terrain_static import terrain_static_bp
from src.routes.local_terrain_api import local_terrain_api_bp
from src.routes.contour_api import contour_api_bp
from src.routes.contour_static import contour_static_bp
from src.routes.tiles_static import tiles_static_bp
from src.routes.basemap_static import basemap_static_bp
from src.routes.mbtiles_static import mbtiles_static_bp
from src.routes.plugins_api import plugins_bp

__all__ = ['main_bp', 'api_bp', 'dem_api_bp', 'terrain_api_bp', 'terrain_static_bp', 'local_terrain_api_bp', 'contour_api_bp', 'contour_static_bp', 'tiles_static_bp', 'basemap_static_bp', 'mbtiles_static_bp', 'plugins_bp']
