"""
打包运行环境支持(Nuitka standalone)

Nuitka standalone 模式下,`--include-data-dir` 收集的数据目录与可执行文件
同目录存放;`sys.executable` 指向真实 exe,其所在目录即 bundle 目录。
是否处于打包环境通过 Nuitka 注入模块命名空间的 `__compiled__` 判断。
"""
import os
import sys


def bundle_dir():
    """返回打包产物的资源目录;非打包环境返回 None。"""
    if '__compiled__' in globals():
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


def setup_bundle_env():
    """打包模式下设置 GDAL/PROJ 数据路径(替代原 PyInstaller runtime hook)。

    数据目录缺失时立即报错——没有它们的包能启动但每次 GDAL 调用都会失败,
    nuitka_build.py 已拒绝打出这种包,走到这里说明产物确实损坏。
    非打包环境为 no-op。
    """
    base = bundle_dir()
    if base is None:
        return

    gdal_data_path = os.path.join(base, 'gdal-data')
    proj_data_path = os.path.join(base, 'proj-data')

    missing = [p for p in (gdal_data_path, proj_data_path) if not os.path.isdir(p)]
    if missing:
        raise RuntimeError(
            'Corrupt bundle: missing GDAL/PROJ data directories: '
            + ', '.join(missing)
            + '. Rebuild the executable (nuitka_build.py refuses to bundle without them).'
        )

    os.environ['GDAL_DATA'] = gdal_data_path
    os.environ['PROJ_LIB'] = proj_data_path
    os.environ['PROJ_DATA'] = proj_data_path

    # Windows:Nuitka 用 LOAD_WITH_ALTERED_SEARCH_PATH 加载 .pyd,依赖 DLL 只搜
    # .pyd 所在目录和 PATH 等,搜不到 dist 根目录的 gdal.dll 及其闭包(conda 版
    # osgeo 自带的 add_dll_directory 在 ALTERED 搜索路径下不生效,实测 err=126)。
    # 把 dist 根加入 PATH(ALTERED 路径会搜 PATH),并注册 AddDllDirectory
    # 兜底(兼容 CPython 原生 DEFAULT_DIRS|USER_DIRS 加载路径)。
    if sys.platform == 'win32':
        os.environ['PATH'] = base + os.pathsep + os.environ.get('PATH', '')
        try:
            os.add_dll_directory(base)
        except (AttributeError, OSError):
            pass
