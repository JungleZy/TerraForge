"""src/plugins/ 的打包可达性：每个模块都必须在 app_factory 的预热清单里。"""

import pathlib
import subprocess
import sys

_MARK = 'WARMED:'


def _expected_module_names(root: pathlib.Path):
    """src/plugins/ 下每个 .py 对应的完整模块名（含子包与其 __init__）。"""
    names = set()
    for py in sorted((root / 'src' / 'plugins').glob('**/*.py')):
        parts = list(py.relative_to(root).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()          # src/plugins/builtin/__init__.py → src.plugins.builtin
        names.add('.'.join(parts))
    return names


def test_every_plugin_module_is_warmed_for_nuitka():
    """src/plugins/ 下每个模块都必须在 app_factory 的可达性清单里。

    那份清单同时是打包的可达性清单（app_factory 模块 docstring 写明）：漏一行，
    源码运行一切正常，打包产物启动即 ModuleNotFoundError——只有真去跑 exe 才发现。

    断言 import 后的 sys.modules 而不是 app_factory.py 的源码文本：真正要保证的
    是「模块被实际拉进来了」，不是「文件里有那个字符串」。

    **必须在全新子进程里问**：本进程的 sys.modules 早被收集期污染了——每个插件
    模块的测试文件都在模块级 import 它，于是无论 app_factory 登记与否都「在」，
    在全量 `pytest tests/` 路径上这条闸门会变成假绿。子进程只 import
    src.app_factory，看到的就是打包时 Nuitka 看到的那张图。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    probe = ('import src.app_factory, sys\n'
             f'print("\\n".join({_MARK!r} + n for n in sorted(sys.modules)'
             ' if n == "src.plugins" or n.startswith("src.plugins.")))\n')
    proc = subprocess.run([sys.executable, '-c', probe], cwd=root,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f'子进程 import src.app_factory 失败：\n{proc.stderr}'

    warmed = {line[len(_MARK):] for line in proc.stdout.splitlines()
              if line.startswith(_MARK)}
    missing = sorted(_expected_module_names(root) - warmed)
    assert not missing, f'{missing} 不在 app_factory 预热清单里，打包会丢'
