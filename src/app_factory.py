"""Flask 应用组装(composition root)。

`create_app()` 构造 socketio + 四条管线的 TaskManager,把它们注入到蓝图的模块级
全局里,再注册蓝图与 socketio 事件。装配顺序是有约束的,见函数内注释。

**为什么业务 import 全都写在函数体内**:测试的通用套路是 monkeypatch 掉
Config 的目录字段,再 `sys.modules.pop('app' / 'src.routes.*' / 'src.services.*')`
后重新 `import app`(见 tests/conftest.py 的 fresh_import 与其注释)。本模块不会被
一起 pop,若在模块级 `from src.routes import api_bp`,这里就会把上一次 import 的旧
模块对象钉死:蓝图闭包用旧 manager、init_*_task_manager 写进旧模块的全局,而测试
patch 的是新模块 —— 「测试 patch 新模块、请求却打到旧模块」的静默假绿。函数体内
import 每次调用都按名字重新解析 sys.modules,与测试拿到的是同一份实例。
"""

import logging

from flask import Flask, request
from flask_socketio import SocketIO

# 预热:重量级依赖(routes / 四个 manager → GDAL、numpy,合计数秒)在 import 本模块
# 时就装进 sys.modules,让 app.py 的加载动画覆盖这段等待;上面说的函数体内 import
# 随后只是一次字典命中。刻意用 `import 包.模块` 形式而非 from-import —— 不在本模块
# 留下任何会变陈旧的对象引用。
#
# 这里同时是打包的可达性清单:凡是只在函数体内 import 的模块都要在这列一行,
# 让 Nuitka 的静态分析一眼看见,不至于漏进 dist。
import src.core.database  # noqa: F401
import src.core.single_instance  # noqa: F401
import src.routes  # noqa: F401
import src.i18n  # noqa: F401
import src.routes.socketio_events  # noqa: F401
import src.services.base_terrain_warmup  # noqa: F401
import src.services.contour_task_manager  # noqa: F401
import src.services.dem_task_manager  # noqa: F401
import src.services.local_terrain_task_manager  # noqa: F401
import src.services.system_proxy  # noqa: F401
import src.services.task_cleanup  # noqa: F401
import src.services.task_manager  # noqa: F401

logger = logging.getLogger(__name__)


def _asset_dirs():
    """templates/static 的绝对路径。

    打包模式(Nuitka standalone)下它们与可执行文件同目录;源码运行取项目根目录。
    必须给绝对路径:Flask 的相对路径是相对 root_path 解析的,而本模块在 src/ 下,
    root_path 会落到 src/ 而不是项目根。
    """
    from pathlib import Path

    from src.core.bundle import bundle_dir

    base = bundle_dir()
    base = Path(base) if base is not None else Path(__file__).resolve().parent.parent
    return str(base / 'templates'), str(base / 'static'), str(base)


def _create_flask_app():
    """Flask 实例 + 配置 + 目录初始化 + 静态资源缓存策略。"""
    from src.core.config import Config

    template_folder, static_folder, root_path = _asset_dirs()
    # import_name 固定为 'app' 而不是本模块名:WSGI(gunicorn app:app)与测试
    # import 的就是 app 模块,保持 app.name 稳定。root_path 显式给出,Flask 就不会
    # 再按 import_name 去反推目录。
    app = Flask('app', root_path=root_path,
                template_folder=template_folder, static_folder=static_folder)
    app.config.from_object(Config)

    # Initialize application directories
    Config.init_app()

    # 界面语言（zh / en，cookie tf-lang）：注册 `t()` 与 locale 上下文，模板才能
    # 在服务端就把文案渲染成对应语种。
    from src.i18n import register as register_i18n

    register_i18n(app)

    # SECRET_KEY 提示统一在这里打:config.py 在 import 时只置标记(见
    # Config.SECRET_KEY_WAS_GENERATED),由真正起服务的进程在 create_app 里
    # logger.warning 一次。dev reloader 的 watcher 父进程 / multiprocessing
    # worker 都被守卫挡在 create_app 之外,不会重复;WSGI 部署
    # (gunicorn import app:app)也能看到 —— 挂在启动横幅分支后则 WSGI 看不到。
    if Config.SECRET_KEY_WAS_GENERATED:
        logger.warning(
            'SECRET_KEY 未配置,已为本会话生成随机密钥;'
            '生产环境请设置 SECRET_KEY 环境变量')

    # /static/vendor/ 下的第三方库路径自带版本号(如 cesium/1.143.0/...),
    # 内容变了路径就变,所以可以安全地让浏览器 immutable 长缓存,免去每次启动
    # 重新拉取几十 MB 的 Cesium/Bootstrap。业务资源(/static/js/ 等)不加,
    # 保持默认协商缓存,改了能立即生效。
    @app.after_request
    def _vendor_immutable_cache(response):
        if request.path.startswith('/static/vendor/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    logger.debug("Flask application created")
    return app


def _enforce_single_instance():
    """单实例互斥(H3):同一数据目录已有实例在跑就直接退出。

    create_app 后面的 init_database / sweep_startup_residue / 四个 manager 的孤儿
    恢复全是破坏性的,而且全都跑在 socketio.run() 绑端口【之前】—— 第二个实例即使
    最终因端口占用崩溃,也已经 rmtree 掉第一个实例正在写的拼接/warp 工作目录
    (GB 级中间产物,窗口数分钟到数十分钟),并把它正在 running 的任务改判成了
    paused。所以必须在这里就拦住,而不是等端口。锁的粒度是数据目录
    (DATABASE_PATH 同级),因为上述破坏的作用域正是它。
    """
    import sys

    from src.core.single_instance import acquire_instance_lock, lock_path

    if acquire_instance_lock():
        return

    msg = (
        "TerraForge 已经在运行（同一数据目录下检测到另一个实例）。\n"
        "  · 请切换到已打开的窗口，而不是再启动一个；\n"
        f"  · 若确认上一个实例已崩溃退出，删除 {lock_path()} 后重试；\n"
        "  · 确需并行运行多个实例，设置环境变量 TERRAFORGE_ALLOW_MULTI_INSTANCE=1"
        "（注意：会重新引入互删临时目录、误判任务状态的风险）。"
    )
    logger.error(msg)
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _build_task_managers(socketio):
    """构造四条管线的 manager 并注入到对应蓝图模块的全局里。

    蓝图的视图函数依赖这些模块级全局在第一个请求到达前被设置好,所以注入必须在
    注册蓝图之前完成。返回 (task_manager, dem_task_manager,
    local_terrain_task_manager, contour_task_manager)。
    """
    from src.routes.api import init_task_manager
    from src.routes.contour_api import init_contour_task_manager
    from src.routes.dem_api import init_dem_task_manager
    from src.routes.local_terrain_api import init_local_terrain_task_manager
    from src.routes.terrain_api import init_terrain_dem_task_manager
    from src.services.contour_task_manager import ContourTaskManager
    from src.services.dem_task_manager import DemTaskManager
    from src.services.local_terrain_task_manager import LocalTerrainTaskManager
    from src.services.task_manager import TaskManager

    task_manager = TaskManager(socketio=socketio)
    init_task_manager(task_manager)

    # DEM manager 同时服务 /api/dem 与 /api/terrain 两组路由。
    dem_task_manager = DemTaskManager(socketio=socketio)
    init_dem_task_manager(dem_task_manager)
    init_terrain_dem_task_manager(dem_task_manager)

    local_terrain_task_manager = LocalTerrainTaskManager(socketio=socketio)
    init_local_terrain_task_manager(local_terrain_task_manager)

    contour_task_manager = ContourTaskManager(socketio=socketio)
    init_contour_task_manager(contour_task_manager)

    logger.debug("Task managers created and injected into routes")
    return (task_manager, dem_task_manager,
            local_terrain_task_manager, contour_task_manager)


def _register_blueprints(app):
    """注册全部蓝图。必须在 manager 注入之后。"""
    from src.routes import (api_bp, contour_api_bp, contour_static_bp,
                            dem_api_bp, local_terrain_api_bp, main_bp,
                            terrain_api_bp, terrain_static_bp, tiles_static_bp)

    for blueprint in (main_bp, api_bp, dem_api_bp, terrain_api_bp,
                      terrain_static_bp, local_terrain_api_bp,
                      contour_api_bp, contour_static_bp, tiles_static_bp):
        app.register_blueprint(blueprint)

    logger.debug("Blueprints registered")


def create_app():
    """构造 Flask app + SocketIO + 全部 TaskManager + 蓝图,返回
    (app, socketio, task_manager, dem_task_manager,
    local_terrain_task_manager, contour_task_manager) 六元组。

    仅在主进程调用。multiprocessing worker(spawn 平台 —— Windows 打包 exe / macOS ——
    会 re-import 入口模块)绝不能重跑此函数:否则每个 worker 都会重新 init_database、抢
    SQLite 锁,并触发 ContourTaskManager 的 orphan recovery 把正在 running 的任务误标成
    paused(表现为刷新显示暂停、点开始报已在运行、完成后仍留在活动列表)。守卫见
    src/core/runtime_mode.py。
    """
    from src.core.database import init_database
    from src.routes.socketio_events import register_socketio_events
    from src.services.base_terrain_warmup import start_warmup
    from src.services.system_proxy import apply_system_proxy
    from src.services.task_cleanup import sweep_startup_residue

    # 读取 Windows/macOS 系统代理(注册表 / scutil)并导出到 HTTP_PROXY/HTTPS_PROXY,
    # 供 aiohttp(trust_env=True)使用。必须早于任何 manager 构造。
    apply_system_proxy()

    app = _create_flask_app()
    _enforce_single_instance()

    socketio = SocketIO(app, cors_allowed_origins="*")
    logger.debug("SocketIO initialized with CORS enabled")

    init_database()
    logger.debug("Database initialized")

    # 启动一次性清扫:上次进程被 SIGKILL/关窗打断时,finally 盖不住的三类
    # 临时残留(stitch work_dir / contour warp tmpdir / cache .part)在这里
    # best-effort 清掉。必须在 init_database 之后(要读 contour_warp_tmpdir
    # 配置键);同步毫秒级,失败只记日志不拖慢/阻断启动。
    sweep_startup_residue()

    managers = _build_task_managers(socketio)
    _register_blueprints(app)

    register_socketio_events(socketio)

    # 随包底图的后台预热。放在 socketio 与蓝图都就绪之后:它会立刻 emit 一次
    # 初始状态,而 connect handler 要能拿到同一个模块的快照。
    # 已就位时这里连线程都不起(三次 iterdir 计数就返回),是 99% 的启动路径;
    # 缺失时起一个后台线程,绝不阻塞启动 —— 解压 4.3 万个小文件在 Windows 上
    # 要几分钟,同步做的话用户看到的就是一个卡死的启动。
    # create_app 只在 StartupRole.should_create_app 时被调用,所以 dev reloader
    # 的观察者父进程、multiprocessing worker 都不会重复解压。
    start_warmup(socketio)

    logger.debug("Application initialization complete")
    return (app, socketio) + managers
