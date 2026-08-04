"""
Shared pytest fixtures for the test suite.

Review I21: many test files each duplicate the same sys.path.insert +
Config monkey-patch + module-pop dance, and a bare `sys.modules.pop(...)`
never restores the popped module — the fresh instance stays registered, so
a later test can import a different module object than the one the Flask
app's blueprints are actually using (module double-instance, test results
depend on execution order). `fresh_import()` below is the unified tool:
pop + re-import + automatic restore via monkeypatch teardown. Existing
test files should migrate their ad-hoc pop loops to it; new tests must use
it (or the `isolated_app` fixture) instead of hand-rolled pop lists.

Importing this module also guarantees the project root is on sys.path before
any test module is collected (some existing files rely on another test's
sys.path.insert having run first — fragile when tests run in isolation).
"""

import importlib
import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class _SandboxTempfile:
    """gettempdir 指向沙箱,其余属性透传给真实 tempfile 模块。"""

    def __init__(self, path: str):
        self._path = path

    def gettempdir(self) -> str:
        return self._path

    def __getattr__(self, name):
        return getattr(tempfile, name)


@pytest.fixture(scope="session")
def _startup_sweep_sandbox(tmp_path_factory):
    return tmp_path_factory.mktemp("startup_sweep_sandbox")


@pytest.fixture(autouse=True)
def isolate_startup_sweep(monkeypatch, _startup_sweep_sandbox):
    """H3 测试侧防护:让启动清扫打不到真实的系统临时目录。

    `sweep_startup_residue()` 按【纯文件名前缀】rmtree 掉 gettempdir() 下所有
    `map_dl_stitch_*` / `contour_warp_*` 目录 —— 没有 PID 归属、没有 mtime 年龄
    门槛。而 `create_app()` 在模块导入期就无条件调用它,所以**任何 import app 的
    测试**都会执行这段:已实测手建 /tmp/map_dl_stitch_PROOF 后跑一个只有 4 个
    用例的静态路由测试,该目录即被删除;并发跑两份测试会互删,是复现过的真实
    flaky(GDAL 报 "failed: No such file or directory")。生产侧的进程互斥另行
    修复,本 fixture 只负责让测试套件不再破坏本机 /tmp。

    显式验证清扫行为的 `test_startup_residue_sweep.py` 会在用例内再 patch 一次
    `task_cleanup.tempfile.gettempdir`,覆盖本 fixture(setattr 打在替身上),
    行为不变。
    """
    try:
        import src.services.task_cleanup as tc
    except Exception:  # 环境缺依赖时不阻断收集
        return
    monkeypatch.setattr(tc, "tempfile", _SandboxTempfile(str(_startup_sweep_sandbox)))


# create_app() 通过 init_*_task_manager(...) 把 manager 注入到这些模块的**模块级
# 全局**里。sys.modules 的恢复管不住它们：teardown 后 sys.modules['app'] 已是原
# 实例，而 src.routes.api.task_manager 仍指向 fixture 里那个绑定到已删除 tmp_path 的
# 管理器 —— 「测试 patch 新模块、请求却打到旧模块」（M23，
# tests/test_fix_api_hardening.py 的注释逐字描述过这个坑，是踩出来的）。
_INJECTED_MANAGER_GLOBALS = (
    ("src.routes.api", "task_manager"),
    ("src.routes.dem_api", "dem_task_manager"),
    ("src.routes.terrain_api", "dem_task_manager"),
    ("src.routes.local_terrain_api", "local_terrain_task_manager"),
    ("src.routes.contour_api", "contour_task_manager"),
)


def _preserve_injected_globals(monkeypatch):
    """让 monkeypatch 记住注入型全局的当前值，teardown 时自动还原。

    `monkeypatch.setattr(mod, attr, <当前值>)` 是值上的 no-op，唯一目的就是让
    monkeypatch 把原值记进 undo 栈 —— 与 fresh_import 里处理父包属性同一手法。
    """
    for mod_name, attr in _INJECTED_MANAGER_GLOBALS:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, getattr(mod, attr))


def fresh_import(monkeypatch, *names):
    """
    Pop each module from sys.modules, re-import it, and let monkeypatch
    restore the original sys.modules entries at test teardown
    (monkeypatch.delitem records the evicted module object and puts it
    back on undo), so no double instance leaks into later tests.

    All names are removed before any re-import so the whole set is reloaded
    consistently. Returns the module for a single name, otherwise a list in
    the order given.

    Dotted names ("src.core.database"): importlib also rebinds the submodule
    attribute on the parent package, which monkeypatch's sys.modules undo
    does NOT cover — without handling it, the parent package keeps pointing
    at the fresh instance after teardown (split-brain: sys.modules and the
    package attribute name different module objects). The pre-import
    `setattr(parent, attr, current_value)` below is a value-no-op whose
    only purpose is to make monkeypatch record the original attribute, so
    teardown restores it after the re-import rebinding.
    """
    _preserve_injected_globals(monkeypatch)
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in names:
        if "." in name:
            parent_name, attr = name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None and hasattr(parent, attr):
                monkeypatch.setattr(parent, attr, getattr(parent, attr))
    modules = [importlib.import_module(name) for name in names]
    return modules[0] if len(modules) == 1 else modules


@pytest.fixture
def isolated_app(monkeypatch, tmp_path):
    """
    Import (fresh) the Flask app with all Config side effects redirected to
    tmp_path: DATABASE_PATH / DOWNLOADS_DIR / OUTPUT_DIR / CACHE_DIR.

    Returns the imported `app` module (app_mod.app is the Flask instance,
    already in TESTING mode). Modules that bind Config at import time
    ("app", "src.core.database") are re-imported via fresh_import so the
    re-import picks up the patched values and the originals are restored
    at teardown.
    """
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    app_mod = fresh_import(monkeypatch, "app", "src.core.database")[0]
    app_mod.app.config["TESTING"] = True
    return app_mod
