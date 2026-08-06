"""底图预热的接线：create_app 必须触发它。

不接的话 Task 1 整个模块就是死代码，而现象是「启动后什么都没发生」—— 没有报错、
没有日志差异，只有第一次切片时又回到那段几分钟的无提示等待。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_create_app_kicks_off_the_warmup(monkeypatch, tmp_path):
    """create_app 走完之后 start_warmup 必须被调用过一次，且拿到的是真 socketio。"""
    from src.core import config as config_mod

    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)

    from src.services import base_terrain_warmup as w

    calls = []
    monkeypatch.setattr(w, "start_warmup", lambda sio: calls.append(sio))

    import src.app_factory as factory

    # create_app() 会经 _build_task_managers 把四个 manager 写进 src.routes.* 的
    # **模块级全局**。这条用例既不走 isolated_app 也不走 fresh_import，那两条路上
    # 的 _preserve_injected_globals 就不会执行 —— teardown 之后那些全局仍指向绑定
    # 在已删除 tmp_path 上的 manager，永不还原。这正是 conftest 与
    # test_conftest_isolation_contract.py 开篇整段注释描述的 M23：失败模式是
    # **静默假绿**（后面的用例 patch 新模块、请求却打到旧模块），不是报红。
    # 按文件名字母序本文件排在一大批用例之前，泄漏窗口不小。
    #
    # ⚠️ 这一调用必须排在上面 `import src.app_factory` 之【后】：
    # _preserve_injected_globals 只对**已经在 sys.modules 里**的模块记录原值
    # （`mod = sys.modules.get(name)`，取不到就跳过），而 src.routes.* 是被
    # app_factory 的模块级 `import src.routes` 才拉进来的。搬到 import 之前的话
    # 五个模块一个都还不在，undo 栈全空，这行代码看着在防护、实际一条都没记 ——
    # 实测过：teardown 之后 src.routes.api.task_manager 仍是用例里那个 manager
    # 实例，而用例照样全绿。放在 import 之后，此刻各全局的值是模块初值 None，
    # teardown 会老老实实还原成 None。
    from conftest import _preserve_injected_globals
    _preserve_injected_globals(monkeypatch)

    app, socketio = factory.create_app()[:2]

    assert len(calls) == 1, f"start_warmup 被调用 {len(calls)} 次，应当恰好 1 次"
    assert calls[0] is socketio, "传进去的必须是 create_app 构造的那个 socketio"


def test_app_factory_lists_the_module_for_nuitka():
    """预热 import 清单里要有新模块。

    那份清单同时是**打包的可达性清单**（模块 docstring 写明）：凡是只在函数体内
    import 的模块都要列出来，让 Nuitka 的静态分析看得见。漏了的话源码运行一切正常，
    打包产物启动即 ModuleNotFoundError —— 而这只有真去跑 exe 才会发现。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "src", "app_factory.py"), encoding="utf-8") as f:
        src = f.read()
    assert "import src.services.base_terrain_warmup" in src
