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

# multiprocessing 的 resource_tracker 子进程以 `exe -c 'prog'` 形式拉起本 exe
# (CPython 对冻结应用同样用 -c,resource_tracker.ensure_running 没有 frozen 分支)。
# Nuitka 不识别 -c 参数,不处理会把本模块当主程序重跑(create_app → orphan
# recovery 误杀运行中的任务、重复占用端口)。代为执行该程序段后退出——
# 这只是 multiprocessing 自己构造的启动形式,等价于 python -c。
if '-c' in sys.argv[1:]:
    _c_idx = sys.argv.index('-c')
    if _c_idx + 1 >= len(sys.argv):
        # `-c` 后面没有程序段(畸形调用,resource_tracker 不会这么拉起)——
        # 不 exec 空气,也别往下走把整个 app 当主程序重跑。
        print("error: '-c' requires a program argument", file=sys.stderr)
        sys.exit(1)
    exec(sys.argv[_c_idx + 1], {'__name__': '__main__'})
    sys.exit()

# 打包模式(Nuitka standalone)下设置 GDAL_DATA/PROJ_DATA,必须赶在任何
# import osgeo 之前(routes/services 在下方 import 时会间接加载 GDAL)。
# 非打包环境为 no-op。src.core.bundle 是轻量模块,不引入重量级依赖。
from src.core.bundle import bundle_dir, setup_bundle_env
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
# (create_app 的初始化日志 —— 父进程被守卫跳过,正常情况下没有)和 run_simple 在
# 父进程侧打的启动行(Serving Flask app / Running on / Restarting with stat)都会
# 打一遍,子进程再打一遍,启动输出又乱又重复。这里赶在 import config/routes(会触发
# 日志)之前把父进程的根日志级别抬到 ERROR,让它保持安静 —— 启动输出只由子进程负责。
# werkzeug 的 _log 首次使用时会把 werkzeug logger 显式设为 INFO(盖过根级别),所以要
# 单独压它。
# 下方正式的 configure_logging 对已配置过 handler 的父进程是 no-op,不会把级别降回去。
if __name__ == '__main__' and _DEBUG and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Configure logging(按级别着色;核心模块 logging_setup 很轻,赶在重量级 import 前配置)
from src.core.logging_setup import configure_logging

configure_logging()
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
    from src.core.config import Config
    from src.core.startup_banner import print_banner

    print_banner(
        Config.APP_VERSION,
        host='0.0.0.0',
        port=5000,
        debug=_DEBUG,
        downloads_dir=Config.DOWNLOADS_DIR,
        database_path=Config.DATABASE_PATH,
    )

# 加载动画:重量级 import 阻塞主线程数秒,动画只能在后台线程里跑(Spinner 内部
# 处理;非 TTY 退化为一次性静态提示)。父进程(横幅后)和 reloader 子进程(不打
# 横幅,但也要等同样久的 import)各起一个,在下方 create_app 之前停掉。
_spinner = None
if _PRINT_BANNER or _SHOW_STARTUP_OUTPUT:
    from src.core.startup_banner import start_spinner
    _spinner = start_spinner('  正在加载组件,请稍候…')

from flask import Flask, request
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

from src.core.config import Config
from src.core.database import init_database
from src.routes import main_bp, api_bp, dem_api_bp, terrain_api_bp, terrain_static_bp, local_terrain_api_bp, contour_api_bp, contour_static_bp, tiles_static_bp
from src.routes.api import init_task_manager
from src.routes.socketio_events import register_socketio_events
from src.services.task_manager import TaskManager
from src.routes.dem_api import init_dem_task_manager
from src.routes.terrain_api import init_terrain_dem_task_manager
from src.services.dem_task_manager import DemTaskManager
from src.services.local_terrain_task_manager import LocalTerrainTaskManager
from src.routes.local_terrain_api import init_local_terrain_task_manager
from src.services.contour_task_manager import ContourTaskManager
from src.routes.contour_api import init_contour_task_manager
from src.services.system_proxy import apply_system_proxy
from src.services.task_cleanup import sweep_startup_residue
from src.core.single_instance import acquire_instance_lock, lock_path

def create_app():
    """构造 Flask app + SocketIO + 全部 TaskManager + 蓝图,返回
    (app, socketio, task_manager, dem_task_manager,
    local_terrain_task_manager, contour_task_manager) 六元组。

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

    # SECRET_KEY 提示统一在这里打:config.py 在 import 时只置标记(见
    # Config.SECRET_KEY_WAS_GENERATED),由真正起服务的进程在 create_app 里
    # logger.warning 一次。dev reloader 的 watcher 父进程 / multiprocessing
    # worker 都被守卫挡在 create_app 之外,不会重复;WSGI 部署
    # (gunicorn import app:app)也能看到 —— 挂在启动横幅分支后则 WSGI 看不到。
    if Config.SECRET_KEY_WAS_GENERATED:
        logger.warning(
            'SECRET_KEY 未配置,已为本会话生成随机密钥;'
            '生产环境请设置 SECRET_KEY 环境变量')

    # Create Flask application
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
    app.config.from_object(Config)

    # Initialize application directories
    Config.init_app()

    # 单实例互斥(H3)。下面的 init_database / sweep_startup_residue / 四个
    # manager 的孤儿恢复全是破坏性的,而且全都跑在 socketio.run() 绑 5000 端口
    # 【之前】—— 第二个实例即使最终因端口占用崩溃,也已经 rmtree 掉第一个实例
    # 正在写的拼接/warp 工作目录(GB 级中间产物,窗口数分钟到数十分钟),并把它
    # 正在 running 的任务改判成了 paused。所以必须在这里就拦住,而不是等端口。
    # 锁的粒度是数据目录(DATABASE_PATH 同级),因为上述破坏的作用域正是它。
    if not acquire_instance_lock():
        _msg = (
            "TerraForge 已经在运行（同一数据目录下检测到另一个实例）。\n"
            "  · 请切换到已打开的窗口，而不是再启动一个；\n"
            f"  · 若确认上一个实例已崩溃退出，删除 {lock_path()} 后重试；\n"
            "  · 确需并行运行多个实例，设置环境变量 TERRAFORGE_ALLOW_MULTI_INSTANCE=1"
            "（注意：会重新引入互删临时目录、误判任务状态的风险）。"
        )
        logger.error(_msg)
        print(_msg, file=sys.stderr)
        raise SystemExit(1)

    logger.debug("Flask application created")

    # /static/vendor/ 下的第三方库路径自带版本号(如 cesium/1.143.0/...),
    # 内容变了路径就变,所以可以安全地让浏览器 immutable 长缓存,免去每次启动
    # 重新拉取几十 MB 的 Cesium/Bootstrap。业务资源(/static/js/ 等)不加,
    # 保持默认协商缓存,改了能立即生效。
    @app.after_request
    def _vendor_immutable_cache(response):
        if request.path.startswith('/static/vendor/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    # Initialize SocketIO with CORS support
    socketio = SocketIO(app, cors_allowed_origins="*")
    logger.debug("SocketIO initialized with CORS enabled")

    # Initialize database
    init_database()
    logger.debug("Database initialized")

    # 启动一次性清扫:上次进程被 SIGKILL/关窗打断时,finally 盖不住的三类
    # 临时残留(stitch work_dir / contour warp tmpdir / cache .part)在这里
    # best-effort 清掉。必须在 init_database 之后(要读 contour_warp_tmpdir
    # 配置键);同步毫秒级,失败只记日志不拖慢/阻断启动。
    sweep_startup_residue()

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
# macOS —— 在启动 ProcessPoolExecutor 渲染瓦片时)会以两种方式重跑本模块,都必须拦住:
#   1. CPython 原生:runpy 以 '__mp_main__' 重跑主模块;
#   2. Nuitka multiprocessing 插件:以伪造的 '__parents_main__' 模块重跑主模块源码。
# 这两种情况下 parent_process() 尚未设置(返回 None),原有的 parent_process() guard
# 拦不住(create_app → init_database → orphan recovery 把运行中的任务误标失败/暂停,
# 实测 v0.1.1 Windows 打包 exe 的地形切片即死于此)。WSGI(gunicorn import app:app,
# __name__=='app')和 Flask dev reloader 子进程(__main__)不在排除名单,正常初始化。
_MP_RERUN_NAMES = ('__mp_main__', '__parents_main__')


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
if (
    __name__ not in _MP_RERUN_NAMES
    and multiprocessing.parent_process() is None
    and not _is_reloader_parent()
):
    (app, socketio, task_manager, dem_task_manager,
     local_terrain_task_manager, contour_task_manager) = create_app()


if __name__ == '__main__':
    # freeze_support() 已在模块顶部调用(必须赶在 create_app 之前拦截 frozen worker)。

    # werkzeug 的启动行(Running on xxx / dev-server 警告 / debugger PIN)和横幅里的
    # 访问地址重复,用过滤器精确拦掉这几条;HTTP 请求访问日志(GET /... 200)保留,
    # 但剥掉和 asctime 重复的内嵌日期,并把日志名 werkzeug 换成 http。
    from src.core.logging_setup import WerkzeugAccessLogFilter
    from src.core.startup_banner import WerkzeugStartupFilter
    logging.getLogger('werkzeug').addFilter(WerkzeugStartupFilter())
    logging.getLogger('werkzeug').addFilter(WerkzeugAccessLogFilter())

    # Flask 的 " * Serving Flask app / * Debug mode" 两行由 app.run -> flask.cli.
    # show_server_banner 用 click.echo 直接写 stdout,日志级别拦不住;内容与横幅
    # 重复,替换成 no-op。flask.app 里是通过 cli.show_server_banner 模块属性调用的,
    # 所以 patch flask.cli 即可生效。
    import flask.cli
    flask.cli.show_server_banner = lambda *args, **kwargs: None

    # reloader 的 watcher 父进程被强杀时,fork 出来的子进程会变孤儿继续占着 5000
    # 端口(werkzeug 不处理)。父进程先把 pid 写进环境变量传给子进程;子进程起
    # 看门狗线程轮询,父进程没了就退出。
    from src.core.process_watchdog import PARENT_PID_ENV, start_parent_watchdog
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_parent_watchdog()
    else:
        os.environ.setdefault(PARENT_PID_ENV, str(os.getpid()))

    # 组件加载完毕的反馈 —— werkzeug 的服务行已被压掉,没有这行的话用户不知道
    # "正在加载组件"已经结束。
    if _SHOW_STARTUP_OUTPUT:
        from src.core.startup_banner import safe_print, use_color
        _ready = '  ✓ 组件加载完成,服务已启动'
        safe_print(f'\033[32m{_ready}\033[0m' if use_color() else _ready)

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
