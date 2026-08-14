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
import src.plugins.manifest  # noqa: F401
import src.plugins.params  # noqa: F401
import src.plugins.protocols  # noqa: F401
import src.routes  # noqa: F401
import src.i18n  # noqa: F401
import src.routes.socketio_events  # noqa: F401
import src.services.base_terrain_warmup  # noqa: F401
import src.services.contour_task_manager  # noqa: F401
import src.services.dem_task_manager  # noqa: F401
import src.services.local_terrain_task_manager  # noqa: F401
import src.services.proxy_autodetect  # noqa: F401
import src.services.resource_scheduler  # noqa: F401
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

    # 这里【不能】建议用户删锁文件。锁锁的是「已打开句柄对应的 inode」
    # (fcntl.flock / msvcrt.locking),不是路径:POSIX 上 unlink 一个被锁的文件
    # 永远成功,下一次启动走 single_instance 的 `path.touch()` 建出【新 inode】
    # 再锁住它 —— 两个实例同时认为自己持锁,而第二个实例的 sweep_startup_residue()
    # 会 rmtree 掉第一个实例正在写的 GB 级拼接/warp 工作目录,四轮孤儿恢复还会把
    # 它 running 的任务改判 paused。这正是本函数存在的理由,不能由错误提示引导用户
    # 去触发。而且前提也不成立:进程死了(含崩溃、强杀)OS 就已经释放了锁,
    # 不存在需要手工清理的陈旧锁文件。详见 docs/reviews/2026-08-08-full-project-review.md 的 B2。
    msg = (
        "TerraForge 已经在运行（同一数据目录下检测到另一个实例）。\n"
        "  · 请切换到已打开的窗口，而不是再启动一个；\n"
        "  · 锁由操作系统在进程退出时自动释放（崩溃、强杀也一样），"
        f"不需要、也不要手动删除 {lock_path()}\n"
        "    —— 删掉它不会解锁，只会让两个实例同时认为自己持锁，"
        "第二个实例的启动清扫会删掉第一个实例正在写的中间产物；\n"
        "  · 确需并行运行多个实例，设置环境变量 TERRAFORGE_ALLOW_MULTI_INSTANCE=1"
        "（注意：会重新引入互删临时目录、误判任务状态的风险）。"
    )
    logger.error(msg)
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def probe_url_from_config(config_manager):
    """用配置里 tile_servers 的第一条构造一张样例瓦片 URL，给代理验证用。

    验证要打的必须是**用户真正要下的源**：默认 mts0.googleapis.com 需要代理，
    但换成国内自建镜像的用户根本不该因为"连不上 Google"被判定成没有可用代理。
    读不到配置/条目非法就回退到 proxy_autodetect 的默认 Google 瓦片。
    """
    from src.services.proxy_autodetect import DEFAULT_PROBE_URL
    from src.services.tile_url_probe import (
        build_probe_url, expand_server_entry, parse_server_list,
        validate_server_entry,
    )
    try:
        entry = parse_server_list(config_manager.get('tile_servers', '') or '')[0]
        ok, _err = validate_server_entry(entry)
        if not ok:
            return DEFAULT_PROBE_URL
        url, _tile = build_probe_url(expand_server_entry(entry, style='m'),
                                     float(config_manager.get('map_center_lng', '106.55')),
                                     float(config_manager.get('map_center_lat', '29.56')))
        return url
    except (IndexError, TypeError, ValueError) as e:
        logger.debug(f"Falling back to default proxy probe URL: {e}")
        return DEFAULT_PROBE_URL


def _start_proxy_autodetect():
    """没手动配代理且开关开着时，后台探一轮可用代理。返回是否起了线程。"""
    from src.services.config_manager import ConfigManager
    from src.services.proxy_autodetect import (
        auto_detect_enabled, start_background_autodetect,
    )

    config_manager = ConfigManager()
    try:
        manual = (config_manager.get('proxy_url', '') or '').strip()
        if manual:
            logger.info("Proxy autodetect skipped: proxy_url is configured manually")
            return False
        if not auto_detect_enabled(config_manager):
            logger.info("Proxy autodetect disabled by config (proxy_auto_detect=false)")
            return False
        return start_background_autodetect(probe_url=probe_url_from_config(config_manager))
    except Exception as e:
        # 探测是锦上添花,任何故障都不能挡住启动
        logger.warning(f"Failed to start proxy autodetect: {e!r}")
        return False


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
    from src.routes import (api_bp, basemap_static_bp, contour_api_bp,
                            contour_static_bp, dem_api_bp,
                            local_terrain_api_bp, main_bp, mbtiles_static_bp,
                            terrain_api_bp, terrain_static_bp, tiles_static_bp)

    for blueprint in (main_bp, api_bp, dem_api_bp, terrain_api_bp,
                      terrain_static_bp, local_terrain_api_bp,
                      contour_api_bp, contour_static_bp, tiles_static_bp,
                      basemap_static_bp, mbtiles_static_bp):
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
    from src.services.resource_scheduler import get_scheduler
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

    # 启动一次性清扫:上次进程被 SIGKILL/关窗打断时,finally 盖不住的七类
    # 临时残留(stitch work_dir / contour warp tmpdir / cache .part / 过期任务
    # 日志 / 超额缓存 ...)在这里 best-effort 清掉。必须在 init_database 之后
    # (要读 contour_warp_tmpdir 等配置键);同步毫秒级,失败只记日志不拖慢/阻断启动。
    sweep_startup_residue()

    # 全局资源上界打一条日志。这几个数(并发任务数 / 网络连接数 / CPU 工作进程 /
    # GDAL 槽)决定了「为什么第三个任务点了开始却在排队」，而它们来自配置库、
    # 用户改得动、脏值还会静默退回出厂默认(_coerce_limit 只 warn 一次)。不打
    # 这一行的话，排查一次「任务卡在 pending」就得先让用户去翻配置页 —— 而
    # 配置页显示的是**用户写进去的值**，不是调度器实际采信的值。
    # get_scheduler() 每次调用都重读配置，所以这里拿到的就是运行时口径。
    # 单独套 try：调度器初始化失败绝不该挡住启动，日志缺一行而已。
    try:
        limits = get_scheduler().limits()
        logger.info("Resource scheduler limits: %s",
                    ', '.join(f'{kind.value}={value}'
                              for kind, value in sorted(
                                  limits.items(), key=lambda kv: kv[0].value)))
    except Exception as e:
        logger.warning(f"Cannot read resource scheduler limits (ignored): {e}")

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

    # 代理自动发现的后台探测。必须在 init_database 之后（要读 proxy_url /
    # proxy_auto_detect 两个配置键），且只在"没手动配代理 + 开关没关"时才起
    # 线程 —— 手动配了就以手动值为准,探测纯属浪费。
    # 后台跑:枚举候选(端口扫描 ~0.4s + PAC 下载 ~3s)加逐个真实瓦片验证
    # (每个最坏 6s)合计可到二十几秒,同步做就是一个卡死的启动。下载任务若在
    # 探测完成前提交,resolve_proxy_url 会等这一轮出结果再走。
    _start_proxy_autodetect()

    logger.debug("Application initialization complete")
    return (app, socketio) + managers
