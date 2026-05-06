"""
Flask routes package

Exports main_bp and api_bp blueprints for application routing.
"""

from routes.main import main_bp
from routes.api import api_bp

__all__ = ['main_bp', 'api_bp']
