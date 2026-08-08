"""
TerraForge 进程入口。

这个文件只做编排,实现都在别处:

- 进程入口守卫(frozen worker / `-c` 转发)   src/core/process_entry.py
- 启动身份判定(谁打横幅、谁做初始化)        src/core/runtime_mode.py
- 启动横幅与加载动画                          src/core/startup_banner.py
- Flask/SocketIO/manager/蓝图的装配           src/app_factory.py
- socketio.run 与启动收尾                     src/core/server_runner.py

顺序是有讲究的,每一步的注释说明了为什么必须排在这个位置。
"""

# 入口守卫必须是第一件事:frozen worker / resource_tracker 会重新执行本模块,让它们
# 走到下面任何一步都意味着重跑 app 初始化(改写运行中任务的状态、抢库、占端口)。
from src.core.process_entry import install_entry_guards

install_entry_guards()

# 打包模式下设置 GDAL_DATA/PROJ_DATA,必须赶在任何 import osgeo 之前(下面的
# app_factory 会间接加载 GDAL)。非打包环境为 no-op。
from src.core.bundle import setup_bundle_env

setup_bundle_env()

from src.core.runtime_mode import SERVER_HOST, SERVER_PORT, detect_startup_role

_role = detect_startup_role(__name__)

# 日志配置要赶在 import config/routes(会触发日志)之前;watcher 父进程则要先闭嘴。
#
# log_to_file 只给**真正提供服务**的那个进程开(show_startup_output 的判定恰好
# 就是它,见 runtime_mode 的身份表)。按天轮转要重命名文件,多进程同时持有必然
# 打架 —— 而 reloader 的 watcher 父进程、multiprocessing 的 worker 都会把本模块
# 重跑一遍。它们只写控制台。
from src.core.logging_setup import configure_logging, quiet_reloader_parent

if _role.reloader_parent:
    quiet_reloader_parent()
configure_logging(log_to_file=_role.show_startup_output)

# 横幅必须赶在下面 app_factory 那几秒重量级 import 之前打印,否则这段时间控制台
# 一片空白,看起来像卡死。config 和 startup_banner 都是轻量模块,先加载它们即可。
if _role.print_banner:
    from src.core.config import Config
    from src.core.startup_banner import print_banner

    print_banner(
        Config.APP_VERSION,
        host=SERVER_HOST,
        port=SERVER_PORT,
        debug=_role.debug,
        downloads_dir=Config.DOWNLOADS_DIR,
    )

# 加载动画:重量级 import 阻塞主线程数秒,动画只能在后台线程里跑(Spinner 内部
# 处理;非 TTY 退化为一次性静态提示)。父进程(横幅后)和 reloader 子进程(不打
# 横幅,但也要等同样久的 import)各起一个。
_spinner = None
if _role.print_banner or _role.show_startup_output:
    from src.core.startup_banner import start_spinner

    _spinner = start_spinner('  正在加载组件,请稍候…')

# 重量级 import 集中在这一行(flask / routes / services → GDAL,数秒),由上面的
# 加载动画覆盖;停掉动画之后再 create_app,让数据库日志落在干净的行上。
from src.app_factory import create_app

if _spinner is not None:
    _spinner.stop()

# 模块级全局:WSGI(gunicorn app:app)和测试都直接取这几个名字。非主进程身份下
# 保持为 None —— 判定规则与理由见 src/core/runtime_mode.py。
app = None
socketio = None
task_manager = None
dem_task_manager = None
local_terrain_task_manager = None
contour_task_manager = None
if _role.should_create_app:
    (app, socketio, task_manager, dem_task_manager,
     local_terrain_task_manager, contour_task_manager) = create_app()


if __name__ == '__main__':
    from src.core.server_runner import run_server

    run_server(app, socketio, debug=_role.debug,
               show_startup_output=_role.show_startup_output)
