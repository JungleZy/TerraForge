"""隔离设施自检（M23）：teardown 之后必须没有「模块双实例」残留。

`create_app()` 通过 init_*_task_manager(...) 把 manager 注入到 routes.* 的
**模块级全局**里。sys.modules 的恢复管不住这些属性 —— teardown 后
sys.modules['app'] 已经是原实例，而 routes.api.task_manager 仍指向 fixture 里
那个绑定到已删除 tmp_path 的管理器。失败模式是**静默假绿**（测试 patch 新模块、
请求却打到旧模块），不是报红，所以必须由自检用例守住。

全量正序/逆序当前都绿，属潜伏状态：任何人新增一个不做隔离的 app 用例就会引爆。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import _INJECTED_MANAGER_GLOBALS, fresh_import  # noqa: E402


def test_injected_globals_are_restored_after_teardown(tmp_path):
    """走完一次 isolated_app 的完整生命周期后，app 与 routes.* 必须仍指向同一批
    manager 对象。"""
    import importlib
    from core import config

    # 先确保基线：app 已加载，两侧一致
    app_mod = importlib.import_module("app")
    routes_api = importlib.import_module("routes.api")
    assert app_mod.task_manager is routes_api.task_manager, "基线就不一致，用例失效"
    baseline = {name: getattr(sys.modules[name], attr)
                for name, attr in _INJECTED_MANAGER_GLOBALS
                if name in sys.modules and hasattr(sys.modules[name], attr)}

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(config.Config, "DATABASE_PATH", tmp_path / "t.db")
        mp.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
        mp.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
        mp.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
        fresh = fresh_import(mp, "app", "core.database")[0]
        # fixture 生命周期内：注入的是新 manager
        assert fresh.task_manager is not baseline["routes.api"]
    finally:
        mp.undo()

    for name, attr in _INJECTED_MANAGER_GLOBALS:
        if name not in baseline:
            continue
        assert getattr(sys.modules[name], attr) is baseline[name], (
            f"{name}.{attr} 在 teardown 后没有还原 —— 后续用例会 patch 新模块、"
            f"请求却打到这个绑定了已删除 tmp_path 的旧 manager（静默假绿）"
        )

    app_mod = sys.modules["app"]
    routes_api = sys.modules["routes.api"]
    assert app_mod.task_manager is routes_api.task_manager, (
        "teardown 后 app 与 routes.api 指向了两个不同的 manager 实例")
