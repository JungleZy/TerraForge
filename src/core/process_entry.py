"""进程入口守卫 —— 必须在任何 app 初始化之前执行。

打包成 exe 之后同一个可执行文件会被 multiprocessing 以两种额外身份重新拉起,
两种都会把整个 app 重跑一遍(init_database、抢 SQLite 锁、重复占端口,以及四条
管线的孤儿恢复把正在 running 的任务误标成 paused —— 表现为刷新显示暂停、点开始
报已在运行、完成后仍留在活动列表):

1. ProcessPoolExecutor 的渲染 worker:exe 带 `--multiprocessing-fork` 重启;
2. multiprocessing 的 resource_tracker:以 `exe -c '<程序段>'` 形式拉起。

本模块只依赖标准库,import 开销可忽略,所以能放在 app.py 的第一行 —— 这正是它
存在的意义:守卫必须赶在 flask/routes/services 那几秒重量级 import 之前生效。
"""

import multiprocessing
import sys


def install_entry_guards():
    """拦下 frozen worker 与 `-c` 形式的启动;正常主进程直接返回,继续走初始化。

    两个分支都以 sys.exit() 收尾(freeze_support 内部退出),调用方不需要判断返回值。
    """
    # freeze_support() 检测到 worker(sys.argv 带 --multiprocessing-fork)就直接运行
    # worker 逻辑并 sys.exit(),根本到不了 create_app。放在 app.py 的 __main__ 块里
    # 太晚 —— 模块级 create_app() 会先跑,worker 重跑孤儿恢复就把运行中的任务误标了;
    # 而 parent_process() guard 在 frozen worker 这一刻还没设好父进程标记(返回
    # None),拦不住。这才是 frozen 下真正有效的拦截点。
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
