"""conftest.fresh_import 工具自身的行为测试(遗留项②)。

fresh_import 对 dotted 模块(如 core.database)做 pop + 重导入时,importlib
会把新实例同时绑到父包属性上,而 monkeypatch 的 sys.modules 撤销并不覆盖
这个属性——不额外处理的话 teardown 后父包属性仍指向被弹出的实例
(split-brain 双实例泄漏)。fresh_import 通过 monkeypatch.setattr 把父包
属性的重绑也纳入 teardown 恢复范围,本文件钉住这个行为。
"""

import sys

import pytest


def test_fresh_import_restores_parent_package_attribute():
    import core
    import core.database  # noqa: F401  (确保父包属性已建立)

    from conftest import fresh_import

    original = sys.modules['core.database']
    assert core.database is original

    with pytest.MonkeyPatch.context() as mp:
        fresh = fresh_import(mp, 'core.database')
        assert fresh is not original
        assert sys.modules['core.database'] is fresh
        assert core.database is fresh, '重导入期间父包属性应指向新实例'

    assert sys.modules['core.database'] is original, 'teardown 后 sys.modules 必须恢复'
    assert core.database is original, 'teardown 后父包属性必须恢复原实例(否则 split-brain)'


def test_fresh_import_plain_module_still_works():
    """无点号的模块名不受父包恢复逻辑影响,行为与之前一致。"""
    from conftest import fresh_import

    original = sys.modules.get('nuitka_build')
    with pytest.MonkeyPatch.context() as mp:
        fresh = fresh_import(mp, 'nuitka_build')
        assert sys.modules['nuitka_build'] is fresh
    if original is not None:
        assert sys.modules['nuitka_build'] is original
