"""
Flask Application Entry Point for TerraForge

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

# 打包模式(Nuitka standalone)下设置 GDAL_DATA/PROJ_DATA,必须赶在任何
# import osgeo 之前(routes/services 在下方 import 时会间接加载 GDAL)。
# 非打包环境为 no-op。core.bundle 是轻量模块,不引入重量级依赖。
from core.bundle import bundle_dir, setup_bundle_env
setup_bundle_env()

# 打包 exe 默认关闭 debug/reloader:热重载对最终用户没有意义,还会让进程模型更复杂
# (reloader 会重新执行 exe 本身)。开发环境默认开启。DEBUG 环境变量始终优先。
# Nuitka 不设 sys.frozen,用 bundle_dir() 统一判断打包环境。
_FROZEN = getattr(sys, 'frozen', False) or bundle_dir() is not None
_DEBUG = os.environ.get(
    'DEBUG',
    '0' if _FROZEN else '1',
) not in ('0', 'false', 'False')

# debug 模式下 Werkzeug reloader 会先以"watcher 父进程"身份执行一遍本模块,再 fork
# 子进程(WERKZEUG_RUN_MAIN=true)重跑。父进程只是文件监听器,但它的 import 副作用
# (SECRET_KEY 提示、create_app 的初始化日志)和 run_simple 在父进程侧打的启动行
# (Serving Flask app / Running on / Restarting with stat)都会打一遍,子进程再打一遍,
# 启动输出又乱又重复。这里赶在 import config/routes(会触发日志)之前把父进程的
# 根日志级别抬到 ERROR,让它保持安静 —— 启动输出只由子进程负责。werkzeug 的 _log
# 首次使用时会把 werkzeug logger 显式设为 INFO(盖过根级别),所以要单独压它。
# 下方正式的 basicConfig 对已配置过 handler 的父进程是 no-op,不会把级别降回去。
if __name__ == '__main__' and _DEBUG and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Configure logging
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 启动输出分两半,由两个进程分别负责:
# - 横幅 / 加载提示:debug 模式下由 reloader 的 watcher 父进程打印。真正起服务的
#   子进程要等父进程跑完重量级 import(数秒)再 fork 出来,若等子进程打横幅,开头
#   那几秒控制台依旧一片空白 —— 这正是把横幅放在这里的意义。非 debug 单进程则由
#   它自己打印。
# - "组件加载完成"(见文件底部 __main__ 块):由真正起服务的进程(debug 子进程 /
#   非 debug 单进程)打印。
# 被 import(测试、gunicorn app:app)和 multiprocessing worker 两个条件都不满足,
# 不会输出。
_PRINT_BANNER = (
    __name__ == '__main__'
    and multiprocessing.parent_process() is None
    and (not _DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') != 'true')
)
_SHOW_STARTUP_OUTPUT = (
    __name__ == '__main__'
    and multiprocessing.parent_process() is None
    and (not _DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true')
)

# 横幅必须赶在下方重量级 import(flask / flask_socketio / routes / services,要数秒)
# 之前打印,否则这段时间控制台一片空白,看起来像卡死。config 和 startup_banner 都是
# 轻量模块,先加载它们即可。
if _PRINT_BANNER:
    from core.config import Config
    from core.startup_banner import print_banner, use_color

    _color = use_color()
    print_banner(
        Config.APP_VERSION,
        host='0.0.0.0',
        port=5000,
        debug=_DEBUG,
        downloads_dir=Config.DOWNLOADS_DIR,
        database_path=Config.DATABASE_PATH,
    )
    # SECRET_KEY 提示跟在横幅后面(config.py 在 import 时只置标记,不直接打日志)。
    # 用 print 而不用 logger.warning:父进程的日志已被压到 ERROR;且父进程在 reload
    # 时不会重跑,提示不会每次热重载都刷一遍。
    if Config.SECRET_KEY_WAS_GENERATED:
        _warn = ('  ⚠ SECRET_KEY 未配置,已为本会话生成随机密钥;'
                 '生产环境请设置 SECRET_KEY 环境变量')
        print(f'\033[33m{_warn}\033[0m' if _color else _warn, flush=True)

# 加载动画:重量级 import 阻塞主线程数秒,动画只能在后台线程里跑(Spinner 内部
# 处理;非 TTY 退化为一次性静态提示)。父进程(横幅后)和 reloader 子进程(不打
# 横幅,但也要等同样久的 import)各起一个,在下方 create_app 之前停掉。
_spinner = None
if _PRINT_BANNER or _SHOW_STARTUP_OUTPUT:
    from core.startup_banner import start_spinner
    _spinner = start_spinner('  正在加载组件,请稍候…')

from flask import Flask
from flask_socketio import SocketIO

# 打包模式(Nuitka standalone):templates/static 与可执行文件同目录
_bundle_dir = bundle_dir()
if _bundle_dir is not None:
    template_folder = os.path.join(_bundle_dir, 'templates')
    static_folder = os.path.join(_bundle_dir, 'static')
else:
    # Running in normal Python environment
    template_folder = 'templates'
    static_folder = 'static'

from core.config import Config
from core.database import init_database
from routes import main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp, local_terrain_api_bp, contour_api_bp, contour_static_bp, tiles_static_bp
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

    logger.debug("Flask application created")

    # Initialize SocketIO with CORS support
    socketio = SocketIO(app, cors_allowed_origins="*")
    logger.debug("SocketIO initialized with CORS enabled")

    # Initialize database
    init_database()
    logger.debug("Database initialized")

    # Create TaskManager instance with SocketIO
    task_manager = TaskManager(socketio=socketio)
    logger.debug("TaskManager created")

    # Inject TaskManager into API routes
    init_task_manager(task_manager)
    logger.debug("TaskManager injected into API routes")

    # Create DEM TaskManager instance with SocketIO
    dem_task_manager = DemTaskManager(socketio=socketio)
    logger.debug("DemTaskManager created")

    # Inject DemTaskManager into DEM API routes
    init_dem_task_manager(dem_task_manager)
    logger.debug("DemTaskManager injected into DEM API routes")

    # Inject DemTaskManager into terrain API routes
    init_terrain_dem_task_manager(dem_task_manager)
    logger.debug("DemTaskManager injected into terrain API routes")

    # Create LocalTerrainTaskManager and inject into local terrain API routes
    local_terrain_task_manager = LocalTerrainTaskManager(socketio=socketio)
    init_local_terrain_task_manager(local_terrain_task_manager)
    logger.debug("LocalTerrainTaskManager created and injected")

    # Create ContourTaskManager and inject into contour API routes
    contour_task_manager = ContourTaskManager(socketio=socketio)
    init_contour_task_manager(contour_task_manager)
    logger.debug("ContourTaskManager created and injected")

    # Register blueprints
    app.register_blueprint(main_bp)
    logger.debug("Main blueprint registered")

    app.register_blueprint(api_bp)
    logger.debug("API blueprint registered")

    app.register_blueprint(dem_api_bp)
    logger.debug("DEM API blueprint registered")

    app.register_blueprint(terrain_api_bp)
    logger.debug("Terrain API blueprint registered")

    app.register_blueprint(terrain_static_bp)
    logger.debug("Terrain static blueprint registered")

    app.register_blueprint(local_terrain_api_bp)
    logger.debug("Local terrain API blueprint registered")

    app.register_blueprint(contour_api_bp)
    logger.debug("Contour API blueprint registered")

    app.register_blueprint(contour_static_bp)
    logger.debug("Contour static blueprint registered")

    app.register_blueprint(tiles_static_bp)
    logger.debug("Tiles static blueprint registered")

    # Register SocketIO events
    register_socketio_events(socketio)
    logger.debug("SocketIO events registered")

    logger.debug("Application initialization complete")
    return (app, socketio, task_manager, dem_task_manager,
            local_terrain_task_manager, contour_task_manager)


# 重量级 import 到此结束 —— 停掉加载动画(create_app 里的数据库日志需要干净的行)。
if _spinner is not None:
    _spinner.stop()

# 仅主进程执行完整初始化。multiprocessing worker(spawn 平台 —— Windows 打包 exe /
# macOS —— 在启动 ProcessPoolExecutor 渲染瓦片时会 re-import 本模块)会命中 guard 并
# 跳过 create_app(),避免重跑 init_database / orphan recovery —— 那是任务被误标 paused、
# worker 环境不稳的根因。WSGI(gunicorn import app:app)和 Flask dev reloader 子进程都
# 不是 multiprocessing 子进程,parent_process() 返回 None,会正常初始化。
def _is_reloader_parent() -> bool:
    """dev 模式 reloader 的 watcher 父进程:__main__ + debug + 无 WERKZEUG_RUN_MAIN。

    父进程只是文件监听器,真正起服务的是它 fork 的子进程(WERKZEUG_RUN_MAIN=true)。
    若父进程也跑 create_app(),其中的 orphan recovery 会写库 —— 同库另有存活实例时
    会把对方的 running 任务误标 paused。冻结 exe(默认 debug off)和非 reloader 路径
    不受影响。
    """
    return (
        __name__ == '__main__'
        and _DEBUG
        and os.environ.get('WERKZEUG_RUN_MAIN') != 'true'
    )


app = None
socketio = None
task_manager = None
dem_task_manager = None
local_terrain_task_manager = None
contour_task_manager = None
if multiprocessing.parent_process() is None and not _is_reloader_parent():
    (app, socketio, task_manager, dem_task_manager,
     local_terrain_task_manager, contour_task_manager) = create_app()


if __name__ == '__main__':
    # freeze_support() 已在模块顶部调用(必须赶在 create_app 之前拦截 frozen worker)。

    # werkzeug 的启动行(Running on xxx / dev-server 警告 / debugger PIN)和横幅里的
    # 访问地址重复,用过滤器精确拦掉这几条;HTTP 请求访问日志(GET /... 200)保留。
    from core.startup_banner import WerkzeugStartupFilter
    logging.getLogger('werkzeug').addFilter(WerkzeugStartupFilter())

    # Flask 的 " * Serving Flask app / * Debug mode" 两行由 app.run -> flask.cli.
    # show_server_banner 用 click.echo 直接写 stdout,日志级别拦不住;内容与横幅
    # 重复,替换成 no-op。flask.app 里是通过 cli.show_server_banner 模块属性调用的,
    # 所以 patch flask.cli 即可生效。
    import flask.cli
    flask.cli.show_server_banner = lambda *args, **kwargs: None

    # reloader 的 watcher 父进程被强杀时,fork 出来的子进程会变孤儿继续占着 5000
    # 端口(werkzeug 不处理)。父进程先把 pid 写进环境变量传给子进程;子进程起
    # 看门狗线程轮询,父进程没了就退出。
    from core.process_watchdog import PARENT_PID_ENV, start_parent_watchdog
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_parent_watchdog()
    else:
        os.environ.setdefault(PARENT_PID_ENV, str(os.getpid()))

    # 组件加载完毕的反馈 —— werkzeug 的服务行已被压掉,没有这行的话用户不知道
    # "正在加载组件"已经结束。
    if _SHOW_STARTUP_OUTPUT:
        from core.startup_banner import use_color
        _ready = '  ✓ 组件加载完成,服务已启动'
        print(f'\033[32m{_ready}\033[0m' if use_color() else _ready, flush=True)

    # reloader 父进程里 create_app 被守卫跳过(app/socketio 为 None)。父进程并不
    # 真正服务 —— werkzeug run_simple 在父进程只把 application 包进一个从不调用的
    # lambda,然后 spawn 子进程(WERKZEUG_RUN_MAIN=true)重新执行本模块,由子进程的
    # 完整 app 提供服务。所以这里只需一个轻量占位 app 驱动 reloader 的文件监听。
    if app is None:
        app = Flask(__name__)
        socketio = SocketIO(app)

    # Run the application with SocketIO
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=_DEBUG,
        use_reloader=_DEBUG,
        allow_unsafe_werkzeug=True
    )
