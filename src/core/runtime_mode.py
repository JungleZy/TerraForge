"""启动身份判定 —— 同一份 app.py 会被五种进程执行,各自该做什么由这里决定。

| 身份                     | 模块名                        | 打横幅 | 打启动提示 | 跑 create_app |
|--------------------------|-------------------------------|--------|------------|---------------|
| 非 debug 单进程          | `__main__`                    | 是     | 是         | 是            |
| dev reloader watcher 父  | `__main__`(无 WERKZEUG_RUN_MAIN)| 是   | 否         | **否**        |
| dev reloader 服务子进程  | `__main__`(WERKZEUG_RUN_MAIN) | 否     | 是         | 是            |
| WSGI / 测试 import       | `app`                         | 否     | 否         | 是            |
| multiprocessing worker   | `__mp_main__`/`__parents_main__`| 否   | 否         | **否**        |

两条「否」都是踩出来的:
- reloader 的 watcher 父进程只是文件监听器,真正服务的是它 fork 的子进程。父进程
  若也跑 create_app(),其中的孤儿恢复会写库 —— 同库另有存活实例时会把对方的
  running 任务误标 paused。
- spawn 平台(Windows 打包 exe / macOS)启动 ProcessPoolExecutor 时会 re-import 主
  模块:CPython 用 runpy 以 `__mp_main__` 重跑,Nuitka 的 multiprocessing 插件以伪造
  的 `__parents_main__` 重跑。这两种情况下 parent_process() 尚未设置(返回 None),
  仅靠 parent_process() guard 拦不住 —— v0.1.1 Windows 打包 exe 的地形切片就死在这。

横幅和启动提示分给两个进程,是为了让控制台第一时间有东西:重量级 import 要数秒,
横幅必须由**先启动**的那个进程(debug 下是 watcher 父进程)打;而「组件加载完成」
只有**真正起服务**的那个进程才知道什么时候到。

本模块只依赖标准库 + src.core.bundle,可以在 app.py 顶部安全地 import。
"""

import multiprocessing
import os
import sys
from dataclasses import dataclass

from src.core.bundle import bundle_dir

# 服务监听地址 —— 启动横幅里的访问链接和 socketio.run() 必须用同一份,否则横幅
# 印的地址会和实际监听的对不上。
SERVER_HOST = '0.0.0.0'
SERVER_PORT = 5000

# spawn 平台 re-import 主模块时用的两个模块名(见模块 docstring)。
_MP_RERUN_NAMES = ('__mp_main__', '__parents_main__')


def is_frozen():
    """是否运行在打包产物里。Nuitka 不设 sys.frozen,所以要一并看 bundle_dir()。"""
    return getattr(sys, 'frozen', False) or bundle_dir() is not None


def debug_enabled():
    """DEBUG 开关:环境变量始终优先;打包 exe 默认关,源码运行默认开。

    热重载对最终用户没有意义,还会让进程模型更复杂(reloader 会重新执行 exe 本身)。
    """
    return os.environ.get('DEBUG', '0' if is_frozen() else '1') not in (
        '0', 'false', 'False')


def _is_werkzeug_child():
    """是不是 reloader fork 出来的那个真正提供服务的子进程。"""
    return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'


@dataclass(frozen=True)
class StartupRole:
    """当前进程的启动身份。由 detect_startup_role() 构造,字段含义见模块表格。"""

    debug: bool
    reloader_parent: bool
    print_banner: bool
    show_startup_output: bool
    should_create_app: bool


def detect_startup_role(module_name, debug=None):
    """判定 module_name(入口模块的 __name__)代表的启动身份。

    debug 省略时取 debug_enabled();显式传入便于测试穷举真值表。
    """
    if debug is None:
        debug = debug_enabled()

    is_entry = module_name == '__main__'
    is_main_process = multiprocessing.parent_process() is None
    reloader_parent = is_entry and debug and not _is_werkzeug_child()

    return StartupRole(
        debug=debug,
        reloader_parent=reloader_parent,
        # 非 debug 时单进程自己打;debug 时由先启动的 watcher 父进程打。
        print_banner=is_entry and is_main_process and (
            not debug or not _is_werkzeug_child()),
        # 与横幅相反:由真正起服务的那个进程打。
        show_startup_output=is_entry and is_main_process and (
            not debug or _is_werkzeug_child()),
        should_create_app=(
            module_name not in _MP_RERUN_NAMES
            and is_main_process
            and not reloader_parent
        ),
    )
