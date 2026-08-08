"""进程入口守卫 —— 必须在任何 app 初始化之前执行。

打包成 exe 之后同一个可执行文件会被 multiprocessing 以两种额外身份重新拉起,
两种都会把整个 app 重跑一遍(init_database、抢 SQLite 锁、重复占端口,以及四条
管线的孤儿恢复把正在 running 的任务误标成 paused —— 表现为刷新显示暂停、点开始
报已在运行、完成后仍留在活动列表):

1. ProcessPoolExecutor 的渲染 worker:exe 带 `--multiprocessing-fork` 重启;
2. multiprocessing 的 resource_tracker:以 `exe -c '<程序段>'` 形式拉起。

本模块只依赖标准库,import 开销可忽略,所以能排在 app.py 的最前面(紧跟着
setup_bundle_env,理由见那里的注释)—— 这正是它存在的意义:守卫必须赶在
flask/routes/services 那几秒重量级 import 之前生效。
"""

import multiprocessing
import sys


def install_entry_guards():
    """拦下 frozen worker 与 `-c` 形式的启动;正常主进程直接返回,继续走初始化。

    两个分支都以 sys.exit() 收尾(freeze_support 内部退出),调用方不需要判断返回值。
    """
    # freeze_support() 检测到 worker(sys.argv 带 --multiprocessing-fork)就直接运行
    # worker 逻辑并 sys.exit()。**但它不是本项目的主要拦截点**,别指望它:
    #   1. CPython 的 BaseContext.freeze_support 在 sys.platform != 'win32' 时整个
    #      短路;win32 上还要求 sys.frozen 为真 —— 那是 bundle_dir() 设的,所以
    #      app.py 必须先调 setup_bundle_env()(它就在本函数上一行,别再换回来)。
    #   2. Nuitka 打出来的 worker 根本走不到 Python 层的 __main__:它的 C bootstrap
    #      自己扫 argv 里的 --multiprocessing-fork,把主模块伪造成 __parents_main__
    #      重跑。真正拦住这条路的是 runtime_mode._MP_RERUN_NAMES 里那个名字
    #      (should_create_app 为假 → 不建 app、不做孤儿恢复)。
    # 留着它是为了 Windows 上 spawn 起 worker 的那条标准路径(sys.frozen 已就位时
    # 它会在这里直接跑完 worker 并退出,比走到模块级 create_app() 再靠身份判定
    # 空转一遍更早、更省)。
    multiprocessing.freeze_support()

    # CPython 对冻结应用同样用 `-c` 拉起 resource_tracker(resource_tracker.
    # ensure_running 没有 frozen 分支)。Nuitka 不识别 -c 参数,不处理会把 app 当
    # 主程序重跑。代为执行该程序段后退出 —— 这只是 multiprocessing 自己构造的启动
    # 形式,等价于 python -c。
    if '-c' in sys.argv[1:]:
        idx = sys.argv.index('-c')
        if idx + 1 >= len(sys.argv):
            # `-c` 后面没有程序段(畸形调用,resource_tracker 不会这么拉起)——
            # 不 exec 空气,也别往下走把整个 app 当主程序重跑。
            print("error: '-c' requires a program argument", file=sys.stderr)
            sys.exit(1)
        exec(sys.argv[idx + 1], {'__name__': '__main__'})
        sys.exit()
