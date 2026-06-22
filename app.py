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
import multiprocessing

# 必须赶在任何 app 初始化(下方 create_app -> init_database / ContourTaskManager 的
# orphan recovery)之前调用。Windows 打包 exe 的 ProcessPoolExecutor 渲染 worker 会
# 重启 exe 并重新执行本模块;freeze_support() 检测到 worker(sys.argv 带
# --multiprocessing-fork)就直接运行 worker 逻辑并 sys.exit(),根本到不了 create_app。
# 放在 __main__ 块里太晚——模块级 create_app() 会先跑,worker 重跑 orphan recovery 把
# 正在运行的任务误标成 paused;而 parent_process() guard 在 frozen worker 这一刻还没设
# 好父进程标记(返回 None),拦不住。这才是 frozen 下真正有效的拦截点。
multiprocessing.freeze_support()

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
from routes import main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp, local_terrain_api_bp, contour_api_bp, contour_static_bp
from routes.api import init_task_manager
from routes.socketio_events import register_socketio_events
from services.task_manager import TaskManager
from routes.dem_api import init_dem_task_manager
from routes.terrain_api import init_terrain_dem_task_manager
from services.dem_task_manager import DemTaskManager
from services.local_terrain_task_manager import LocalTerrainTaskManager
from routes.local_terrain_api import init_local_terrain_task_manager
from services.contour_task_manager import ContourTaskManager
from routes.contour_api import init_contour_task_manager
from services.system_proxy import apply_system_proxy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def create_app():
    """构造 Flask app + SocketIO + 全部 TaskManager + 蓝图,返回 (app, socketio)。

    仅在主进程调用。multiprocessing worker(spawn 平台 —— Windows 打包 exe / macOS ——
    会 re-import 本模块)绝不能重跑此函数:否则每个 worker 都会重新 init_database、抢
    SQLite 锁,并触发 ContourTaskManager 的 orphan recovery 把正在 running 的任务误标成
    paused(表现为刷新显示暂停、点开始报已在运行、完成后仍留在活动列表)。详见模块底部
    的 parent_process() guard。
    """
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

    # Create LocalTerrainTaskManager and inject into local terrain API routes
    local_terrain_task_manager = LocalTerrainTaskManager(socketio=socketio)
    init_local_terrain_task_manager(local_terrain_task_manager)
    logger.info("LocalTerrainTaskManager created and injected")

    # Create ContourTaskManager and inject into contour API routes
    contour_task_manager = ContourTaskManager(socketio=socketio)
    init_contour_task_manager(contour_task_manager)
    logger.info("ContourTaskManager created and injected")

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

    app.register_blueprint(local_terrain_api_bp)
    logger.info("Local terrain API blueprint registered")

    app.register_blueprint(contour_api_bp)
    logger.info("Contour API blueprint registered")

    app.register_blueprint(contour_static_bp)
    logger.info("Contour static blueprint registered")

    # Register SocketIO events
    register_socketio_events(socketio)
    logger.info("SocketIO events registered")

    logger.info("Application initialization complete")
    return (app, socketio, task_manager, dem_task_manager,
            local_terrain_task_manager, contour_task_manager)


# 仅主进程执行完整初始化。multiprocessing worker(spawn 平台 —— Windows 打包 exe /
# macOS —— 在启动 ProcessPoolExecutor 渲染瓦片时会 re-import 本模块)会命中 guard 并
# 跳过 create_app(),避免重跑 init_database / orphan recovery —— 那是任务被误标 paused、
# worker 环境不稳的根因。WSGI(gunicorn import app:app)和 Flask dev reloader 子进程都
# 不是 multiprocessing 子进程,parent_process() 返回 None,会正常初始化。
app = None
socketio = None
task_manager = None
dem_task_manager = None
local_terrain_task_manager = None
contour_task_manager = None
if multiprocessing.parent_process() is None:
    (app, socketio, task_manager, dem_task_manager,
     local_terrain_task_manager, contour_task_manager) = create_app()


if __name__ == '__main__':
    # freeze_support() 已在模块顶部调用(必须赶在 create_app 之前拦截 frozen worker)。
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
