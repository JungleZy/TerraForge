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
