"""src/plugins/ 的打包可达性：每个模块都必须在 app_factory 的预热清单里。"""


def test_every_plugin_module_is_warmed_for_nuitka():
    """src/plugins/ 下每个模块都必须在 app_factory 的可达性清单里。

    那份清单同时是打包的可达性清单（app_factory 模块 docstring 写明）。
    断言 import 后的 sys.modules 而不是 app_factory.py 的源码文本：真正要
    保证的是「模块被实际拉进来了」，不是「文件里有那个字符串」。
    """
    import pathlib
    import sys

    import src.app_factory  # noqa: F401

    root = pathlib.Path(__file__).resolve().parent.parent / 'src' / 'plugins'
    for py in sorted(root.glob('*.py')):
        name = 'src.plugins' + ('' if py.stem == '__init__' else f'.{py.stem}')
        assert name in sys.modules, f'{name} 不在 app_factory 预热清单里，打包会丢'
