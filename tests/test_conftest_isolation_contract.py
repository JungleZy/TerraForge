"""隔离设施自检（M23）：teardown 之后必须没有「模块双实例」残留。

`create_app()` 通过 init_*_task_manager(...) 把 manager 注入到 routes.* 的
**模块级全局**里。sys.modules 的恢复管不住这些属性 —— teardown 后
sys.modules['app'] 已经是原实例，而 routes.api.task_manager 仍指向 fixture 里
那个绑定到已删除 tmp_path 的管理器。失败模式是**静默假绿**（测试 patch 新模块、
请求却打到旧模块），不是报红，所以必须由自检用例守住。

2026-08-04：M23 的另一半（裸 pop 不恢复）已经**不再是潜伏状态** —— 见本文件
下半部分的模块身份契约。`test_fix_release_hygiene.py` 裸 pop
`services.download_engine` 且从不恢复，逆序下让 `test_download_engine.py` 的两条
stop_flag 用例实测翻红：`download_tile` 内 `raise DownloadCancelled()` 解析的是
**它自己模块的全局**（旧模块 A），而测试函数内 `from services.download_engine
import DownloadCancelled` 拿到的是 sys.modules 当前那份（新模块 B），
`pytest.raises(B)` 捕不住 A 的异常，异常穿透 → FAILED。
"""

import ast
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import _INJECTED_MANAGER_GLOBALS, fresh_import  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


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


# ---------- 模块身份契约：裸 pop 不得与「别处的模块级 from-import」相撞 ----------
#
# 全库有 61 处裸 `sys.modules.pop(...)`，但绝大多数无害：45/46 处 pop 的是
# `app` / `core.database`，而这两个**没有**任何测试文件在模块级 from-import
# （项目规约要求先 monkeypatch Config 再在函数内 import）。
#
# 真正会出事的是这个组合：模块 M 被 A 文件裸 pop（不恢复），同时被 B 文件在
# **模块级** `from M import Cls`。B 顶部的名字在 collect 期就绑死在旧模块对象
# 上，而函数体内 `raise` / `isinstance` 解析的是各自模块的全局 —— 两份类对象
# 一出现，`pytest.raises` / `except` / `isinstance` 全部失效，且是否引爆取决于
# 文件执行顺序（正序绿、逆序红）。
#
# 实测引爆过的实例：`test_fix_release_hygiene.py` 裸 pop
# `services.download_engine` → 文件级逆序下 `test_download_engine.py` 的两条
# stop_flag 用例翻红。已改用 `fresh_import` 修掉。
#
# 存量已于 2026-08-04 清零。清法不是把 27 处裸 pop 都迁到 `fresh_import`，而是
# **直接删掉多余的 pop 项** —— `models.task` / `services.config_manager` /
# `services.contour_task_manager` / `services.dem_download_engine` 这四个模块的
# 模块级、类体、装饰器都不捕获 `Config` 的值（只 `from core.config import
# Config`，引用类本身，monkeypatch 打在类上对所有引用可见），所以把它们从 pop
# 清单里删掉是零行为改变的：测试函数里的 `import_module(...)` 拿到全局那一份，
# 运行时照样读到 monkeypatch 后的 Config。
#
# 之所以不用 `fresh_import`：它会给这些文件新增「teardown 恢复」语义，而现有
# 测试是建立在「裸 pop 不恢复」这个既成事实上的 —— 上一轮试过在 conftest 里
# 全局施加恢复，打红 15 条（详见报告 M23 补记）。删多余项则完全不碰恢复语义。
#
# 下面两条是棘轮：名单为空 = 不许出现任何这种组合。

KNOWN_DOUBLE_INSTANCE_RISKS: set = set()


class _PopFinder(ast.NodeVisitor):
    """收集本文件里 sys.modules.pop(...) 掉的模块名（含 for 循环的清单式写法）。"""

    def __init__(self):
        self.mods = set()

    @staticmethod
    def _is_modules_pop(node):
        f = node.func
        return (isinstance(f, ast.Attribute) and f.attr == "pop"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "modules")

    def visit_Call(self, node):
        if self._is_modules_pop(node) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self.mods.add(arg.value)
            elif isinstance(arg, ast.Name):
                self.mods.add(("VAR", arg.id))
        self.generic_visit(node)

    def visit_For(self, node):
        # for mod in ("app", "core.database"): sys.modules.pop(mod, None)
        if isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.List, ast.Tuple)):
            names = [e.value for e in node.iter.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            inner = _PopFinder()
            for stmt in node.body:
                inner.visit(stmt)
            if ("VAR", node.target.id) in inner.mods:
                self.mods.update(names)
                return
        self.generic_visit(node)


def _scan_double_instance_risks():
    """返回 {模块名: (pop 它的文件集, 模块级 from-import 它的其它文件集)}。"""
    import collections

    popped = collections.defaultdict(set)
    modlevel = collections.defaultdict(set)
    for path in sorted(pathlib.Path(PROJECT_ROOT, "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        finder = _PopFinder()
        finder.visit(tree)
        for mod in finder.mods:
            if isinstance(mod, str):
                popped[mod].add(path.name)
        for node in tree.body:      # 只看模块级，函数内 import 不绑死名字
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modlevel[node.module].add(path.name)

    risks = {}
    for mod in set(popped) & set(modlevel):
        importers = modlevel[mod] - popped[mod]     # 同一文件内自洽，不算风险
        if importers:
            risks[mod] = (popped[mod], importers)
    return risks


def test_no_new_module_double_instance_risk():
    """棘轮：不得新增「裸 pop 的模块 + 别处模块级 from-import」这种组合。

    会让本用例翻红的改动：给某个测试文件新加一句裸
    `sys.modules.pop("services.xxx")`，而 services.xxx 恰被另一个测试文件在文件
    顶部 `from services.xxx import ...`。改用 `conftest.fresh_import(monkeypatch,
    ...)` 即可 —— 它 pop 之后会由 monkeypatch 在 teardown 还原。
    """
    risks = _scan_double_instance_risks()
    new = set(risks) - KNOWN_DOUBLE_INSTANCE_RISKS
    assert not new, "\n".join(
        [f"新增了 {len(new)} 个模块双实例风险，请改用 conftest.fresh_import："]
        + [f"  {m}\n     被裸 pop 于  : {sorted(risks[m][0])}"
           f"\n     模块级 import 于: {sorted(risks[m][1])}" for m in sorted(new)]
    )


def test_known_risk_list_has_no_stale_entries():
    """棘轮的另一侧：KNOWN 里已经修掉的条目必须及时删除，否则棘轮会松掉。"""
    risks = _scan_double_instance_risks()
    stale = KNOWN_DOUBLE_INSTANCE_RISKS - set(risks)
    assert not stale, (
        f"这些模块已经不再有双实例风险，请从 KNOWN_DOUBLE_INSTANCE_RISKS 移除："
        f"{sorted(stale)}")


def test_release_hygiene_followed_by_download_engine_stays_green():
    """真实场景钉死：这两个文件按此顺序跑必须全绿（文件级逆序下就是这个顺序）。

    会让本用例翻红的改动：把 test_fix_release_hygiene.py 改回裸 pop
    `services.download_engine`。实测届时 test_download_engine.py 的
    test_download_tile_does_not_request_when_stop_flag_already_set 与
    test_download_tile_stops_retrying_after_stop_flag_set 两条失败 ——
    `download_tile` 抛旧模块的 DownloadCancelled，测试 catch 的是新模块那份。

    用子进程跑是必须的：模块身份问题只在**全新解释器 + 特定文件顺序**下显形，
    在当前进程内 import 一遍复现不出来。
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_fix_release_hygiene.py",
         "tests/test_download_engine.py",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    assert result.returncode == 0, (
        "模块双实例回归：\n" + (result.stdout or "")[-3000:]
    )
