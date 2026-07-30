"""
回归测试:multiprocessing worker(spawn 平台)re-import app 模块时,绝不能重跑
app 初始化。

根因:Windows 打包 exe / macOS 用 spawn 启动 ProcessPoolExecutor worker,worker
子进程会 re-import 主模块 app.py。若 app 初始化(init_database、构造 5 个
TaskManager、ContourTaskManager 的 orphan recovery)写在模块级,worker 会重跑一遍,
把正在 running 的任务误标成 paused —— 表现为刷新显示暂停、点开始报已在运行、完成后
仍留在活动列表。修复:初始化放进 create_app(),仅主进程(parent_process() is None)调用。
"""
import multiprocessing as mp
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _import_app_in_worker(q):
    """在 spawn 出来的 mp worker 进程里 import app,回报 app/socketio 是否未初始化。"""
    sys.path.insert(0, PROJECT_ROOT)
    try:
        import app as appmod
        q.put({"ok": True, "app_is_none": appmod.app is None,
               "socketio_is_none": appmod.socketio is None})
    except Exception as e:  # pragma: no cover - 诊断用
        q.put({"ok": False, "error": repr(e)})


def test_app_init_skipped_in_mp_worker():
    """spawn worker re-import app 时,app 初始化必须被跳过(app/socketio 为 None)。

    修复前 app.py 在模块级直接构造 Flask app + 全部 manager,worker 里 app 非 None ->
    本测试失败。修复后由 parent_process() guard 跳过 -> app 为 None -> 通过。
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_import_app_in_worker, args=(q,))
    p.start()
    p.join(timeout=180)

    assert p.exitcode == 0, f"worker 进程异常退出: exitcode={p.exitcode}"
    result = q.get(timeout=5)
    assert result["ok"], f"worker 内 import app 失败: {result.get('error')}"
    assert result["app_is_none"] is True, "mp worker 不应构造 Flask app(说明 app 初始化被重跑)"
    assert result["socketio_is_none"] is True, "mp worker 不应构造 SocketIO"


def test_mp_main_rerun_skips_create_app():
    """spawn 子进程以 __mp_main__ 重跑主模块时,create_app 必须被跳过。

    CPython spawn 的 _fixup_main_from_path 会用 runpy.run_path(run_name='__mp_main__')
    重跑 app.py,此时 parent_process() 为 None,仅靠 parent_process guard 拦不住
    (v0.1.1 Windows 打包 exe 地形切片被 orphan recovery 误杀的根因)。
    """
    import subprocess
    import textwrap

    code = textwrap.dedent(f"""
        import runpy, sys
        sys.path.insert(0, {PROJECT_ROOT!r})
        ns = runpy.run_path({os.path.join(PROJECT_ROOT, 'app.py')!r}, run_name='__mp_main__')
        print('APP_NONE', ns['app'] is None)
    """)
    out = subprocess.check_output([sys.executable, '-c', code], text=True, timeout=300)
    assert 'APP_NONE True' in out


def test_dash_c_argv_executes_program_instead_of_app():
    """`exe -c 'prog'`(multiprocessing resource_tracker 的拉起形式)必须执行 prog。

    Nuitka 不识别 -c;不处理会把 app 当主程序重跑(create_app → orphan recovery)。
    """
    import subprocess

    out = subprocess.check_output(
        [sys.executable, os.path.join(PROJECT_ROOT, 'app.py'), '-c', "print('EXEC_OK')"],
        text=True, timeout=300, cwd=PROJECT_ROOT,
    )
    assert 'EXEC_OK' in out
    assert 'TerraForge' not in out  # 不应进入主程序(横幅)
