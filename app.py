"""
Flask Application Entry Point for Google Maps Downloader

This module initializes and configures the Flask application with:
- Flask-SocketIO for real-time WebSocket communication
- Database initialization
- Task manager for download orchestration
- Blueprint registration for routes
- SocketIO event handlers
"""

import sys
import os
import logging
from flask import Flask
from flask_socketio import SocketIO

# Support PyInstaller bundled mode
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # Running in PyInstaller bundle - add templates and static paths
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
else:
    # Running in normal Python environment
    template_folder = 'templates'
    static_folder = 'static'

from config import Config
from database import init_database
from routes import main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp
from routes.api import init_task_manager
from routes.socketio_events import register_socketio_events
from services.task_manager import TaskManager
from routes.dem_api import init_dem_task_manager
from routes.terrain_api import init_terrain_dem_task_manager
from services.dem_task_manager import DemTaskManager
from services.system_proxy import apply_system_proxy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Pick up Windows/macOS system proxy (read from registry / scutil) and export
# it into HTTP_PROXY/HTTPS_PROXY so aiohttp(trust_env=True) can use it. Must
# run before TaskManager/DemTaskManager are constructed.
apply_system_proxy()

# Create Flask application
app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
app.config.from_object(Config)

# Initialize application directories
Config.init_app()

logger.info("Flask application created")

# Initialize SocketIO with CORS support
socketio = SocketIO(app, cors_allowed_origins="*")
logger.info("SocketIO initialized with CORS enabled")

# Initialize database
init_database()
logger.info("Database initialized")

# Create TaskManager instance with SocketIO
task_manager = TaskManager(socketio=socketio)
logger.info("TaskManager created")

# Inject TaskManager into API routes
init_task_manager(task_manager)
logger.info("TaskManager injected into API routes")

# Create DEM TaskManager instance with SocketIO
dem_task_manager = DemTaskManager(socketio=socketio)
logger.info("DemTaskManager created")

# Inject DemTaskManager into DEM API routes
init_dem_task_manager(dem_task_manager)
logger.info("DemTaskManager injected into DEM API routes")

# Inject DemTaskManager into terrain API routes
init_terrain_dem_task_manager(dem_task_manager)
logger.info("DemTaskManager injected into terrain API routes")

# Register blueprints
app.register_blueprint(main_bp)
logger.info("Main blueprint registered")

app.register_blueprint(api_bp)
logger.info("API blueprint registered")

app.register_blueprint(dem_api_bp)
logger.info("DEM API blueprint registered")

app.register_blueprint(terrain_api_bp)
logger.info("Terrain API blueprint registered")

app.register_blueprint(terrain_static_bp)
logger.info("Terrain static blueprint registered")

# Register SocketIO events
register_socketio_events(socketio)
logger.info("SocketIO events registered")

logger.info("Application initialization complete")


if __name__ == '__main__':
    logger.info("Starting Google Maps Downloader server...")
    logger.info("Server will be available at http://0.0.0.0:5000")

    debug = os.environ.get('DEBUG', '1') not in ('0', 'false', 'False')

    # Run the application with SocketIO
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=debug,
        use_reloader=debug,
        allow_unsafe_werkzeug=True
    )
