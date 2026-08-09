"""
Nuitka build driver for TerraForge (replaces PyInstaller build.spec).

Locates the GDAL/PROJ data directories, then invokes Nuitka in standalone
mode. Output layout matches the old PyInstaller COLLECT layout:

    dist/terraforge/
    ├── terraforge(.exe)    # 主程序
    ├── templates/          # Web UI 模板
    ├── static/             # 静态资源
    ├── gdal-data/          # GDAL 数据
    ├── proj-data/          # PROJ 数据
    └── [Python 运行时与依赖库]
"""
import glob
import os
import re
import shutil
import subprocess
import sys

APP_NAME = 'terraforge'
ENTRY = 'app.py'

# 产物里必须存在的「自有数据」哨兵。gdal-data / proj-data 各有构建期与运行期
# 两道硬失败,而 --include-data-dir=templates / static / assets 一道都没有:静默
# 漏收时 exe 照样能启动,CI 冒烟测试请求 `/` 也照样 200(它只用到 templates/),
# 用户拿到的是一张白地图,而且没有任何一处日志会报错。
# 用 glob 而不是写死路径:Cesium 在 static/vendor/cesium/<版本>/ 下。
#
# 全球 base 地形分卷是这里最隐蔽的一个:它漏收之后连白地图都算不上——切片能跑完、
# 服务照常 200,只是所有任务的 z0-7 都没有底,用户只看到「地形不对」而无从判断
# 少了什么(运行期按设计静默退回 parentUrl 级联,见 base_terrain.base_parts_dir)。
# 分卷有两个,少一个解压就会中途失败,所以哨兵匹配 part* 整组。
APP_DATA_SENTINELS = (
    'templates/index.html',
    'static/vendor/cesium/*/Cesium.js',
    'static/vendor/fonts/fonts.css',
    'assets/terrain/base_z8.tar.gz.part*',
)


def verify_app_data(dist_dir):
    """校验 templates/ / static/ / assets/ 的关键文件真的落进产物;缺的一次全列出来。"""
    missing = [rel for rel in APP_DATA_SENTINELS
               if not glob.glob(os.path.join(dist_dir, *rel.split('/')))]
    if missing:
        raise RuntimeError(
            'App data collection failed, these are missing from the bundle:\n'
            + '\n'.join('  ' + m for m in missing)
            + '\nCheck the --include-data-dir=templates / static / assets options — such '
              'a bundle starts up fine and then serves a blank map (or terrain with no '
              'global base) to the user.'
        )


def _first_existing_dir(candidates, required_file=None):
    for cand in candidates:
        cand = (cand or '').strip()
        if not cand:
            continue
        if os.path.isdir(cand) and (required_file is None or os.path.exists(os.path.join(cand, required_file))):
            return cand
    return ''


def _gdal_data_candidates():
    candidates = [os.environ.get('GDAL_DATA', '')]

    conda_prefix = os.environ.get('CONDA_PREFIX', '').strip()
    if conda_prefix:
        candidates.append(os.path.join(conda_prefix, 'share', 'gdal'))
        candidates.append(os.path.join(conda_prefix, 'Library', 'share', 'gdal'))

    candidates.append(os.path.join(os.path.dirname(sys.executable), 'Library', 'share', 'gdal'))

    # osgeo 包布局(Windows conda-forge 把数据放在包目录下)
    try:
        import osgeo
        candidates.append(os.path.join(os.path.dirname(osgeo.__file__), 'data', 'gdal'))
    except ImportError:
        pass

    if sys.platform != 'win32':
        gdal_config_dir = os.popen('gdal-config --datadir 2>/dev/null').read().strip()
        if gdal_config_dir:
            candidates.append(gdal_config_dir)
        candidates.extend([
            '/usr/share/gdal',
            '/usr/local/share/gdal',
        ])
        if sys.platform == 'darwin':
            # '/usr/local/share/gdal' 已在上方通用列表里,不重复添加
            candidates.append('/opt/homebrew/share/gdal')

    return candidates


def _proj_data_candidates():
    candidates = [
        os.environ.get('PROJ_DATA', ''),
        os.environ.get('PROJ_LIB', ''),
    ]
    conda_prefix = os.environ.get('CONDA_PREFIX', '').strip()
    if conda_prefix:
        candidates.append(os.path.join(conda_prefix, 'share', 'proj'))
        candidates.append(os.path.join(conda_prefix, 'Library', 'share', 'proj'))

    candidates.append(os.path.join(os.path.dirname(sys.executable), 'Library', 'share', 'proj'))

    if sys.platform == 'darwin':
        candidates.extend([
            '/opt/homebrew/share/proj',
            '/usr/local/share/proj',
            '/usr/share/proj',
        ])
    else:
        candidates.extend([
            '/usr/share/proj',
            '/usr/local/share/proj',
        ])

    # pkg-config 是 Unix 工具,Windows 上不存在 —— 无守卫时 popen 静默失败
    # 虽无害,但与上方 gdal-config 同款守卫保持一致。
    if sys.platform != 'win32':
        pkg_config_datadir = os.popen('pkg-config --variable=datadir proj 2>/dev/null').read().strip()
        if pkg_config_datadir:
            candidates.append(os.path.join(pkg_config_datadir, 'proj'))

    return candidates


# ldd 闭包中需要排除的系统运行库(与 Nuitka 自身的 _linux_dll_ignore_list 对齐):
# glibc/libstdc++/zlib 在任何目标机器上都可假定存在,且打包它们反而会引发 ABI 冲突。
_SYSTEM_LIB_PREFIXES = (
    'linux-vdso', 'ld-linux', 'ld-musl',
    'libc.', 'libc-', 'libm.', 'libdl.', 'libpthread.', 'librt.', 'libutil.',
    'libresolv.', 'libnsl.', 'libnss_', 'libcrypt.', 'libmvec.', 'libanl.',
    'libBrokenLocale.', 'libSegFault.', 'libcidn.', 'libmemusage.', 'libpcprofile.',
    'libthread_db.', 'libstdc++.', 'libgcc', 'libz.', 'libdrm.',
)


def _ldd_paths(binary):
    """解析 ldd 输出中的绝对路径依赖。"""
    out = subprocess.run(
        ['ldd', binary], capture_output=True, text=True, check=False,
    ).stdout
    for line in out.splitlines():
        match = re.search(r'=>\s+(/\S+)', line)
        if match:
            yield match.group(1)
        elif line.strip().startswith('/'):
            yield line.split()[0]


def copy_extension_system_libs(dist_dir):
    """把 osgeo 扩展的系统级依赖库(ldd 闭包)拷进 dist 根目录。

    Nuitka 只复制 python 路径内的依赖,apt 安装的 libgdal/libproj/libgeos
    等在 /lib 下,默认不会进包——本机能跑、干净机器上 GDAL 全挂。
    扩展的 RPATH($ORIGIN/..)与 exe 的 RPATH($ORIGIN)都会搜索 dist 根目录,
    拷到那里即可被找到。仅 Linux(ELF/ldd)需要此步骤。
    """
    if sys.platform == 'win32' or sys.platform == 'darwin':
        return

    import osgeo
    osgeo_dir = os.path.dirname(osgeo.__file__)

    seen = {}
    queue = [
        os.path.join(osgeo_dir, name)
        for name in os.listdir(osgeo_dir)
        if name.endswith('.so')
    ]
    while queue:
        so = queue.pop()
        for path in _ldd_paths(so):
            base = os.path.basename(path)
            if base.startswith(_SYSTEM_LIB_PREFIXES) or base in seen:
                continue
            seen[base] = os.path.realpath(path)
            queue.append(path)

    copied = 0
    for base, path in sorted(seen.items()):
        dest = os.path.join(dist_dir, base)
        if os.path.exists(dest):
            continue
        # shutil.copy 跟随符号链接,dest 文件名保持 soname(libgdal.so.34),
        # 与扩展里 DT_NEEDED 记录的名字一致。
        shutil.copy(path, dest)
        copied += 1
    print(f'Copied {copied} system shared libraries into {dist_dir}')


# ---- Windows: GDAL DLL 补拷 ----

# 依赖闭包中需要排除的 Windows 系统 DLL(前缀匹配,小写):
# Windows 目录下的系统组件与 API 集在任何目标机器上都存在;
# MSVC 运行库由 Nuitka 自己的 VC Redist 机制处理,这里不重复拷贝。
_WIN_SYSTEM_DLL_PREFIXES = (
    'api-ms-win-', 'ext-ms-', 'msvcrt', 'msvcp', 'vcruntime', 'ucrtbase',
    'kernel32', 'kernelbase', 'ntdll', 'user32', 'gdi32', 'advapi32',
    'ole32', 'oleaut32', 'shell32', 'shlwapi', 'ws2_32', 'secur32',
    'crypt32', 'bcrypt', 'rpcrt4', 'comdlg32', 'comctl32', 'winmm',
    'version', 'imm32', 'setupapi', 'cfgmgr32', 'powrprof', 'dwmapi',
    'uxtheme', 'wldap32', 'normaliz', 'iphlpapi', 'dnsapi', 'netapi32',
    'userenv', 'profapi', 'mswsock', 'sspicli', 'dpapi', 'samcli',
)


def _is_windows_system_dll(path):
    # 纯字符串处理(不用 os.path):该函数虽只在 win32 上执行,
    # 但单元测试在 POSIX 上跑,os.path.basename 不认反斜杠。
    normalized = path.replace('/', '\\')
    base = normalized.rsplit('\\', 1)[-1].lower()
    if base.startswith(_WIN_SYSTEM_DLL_PREFIXES):
        return True
    windir = os.environ.get('SystemRoot', r'C:\Windows').replace('/', '\\').rstrip('\\').lower()
    return normalized.lower().startswith(windir + '\\')


def _find_bundled_gdal_dll(dist_dir):
    for root, _dirs, files in os.walk(dist_dir):
        for name in files:
            lowered = name.lower()
            if lowered.startswith('gdal') and lowered.endswith('.dll'):
                return True
    return False


def copy_extension_system_dlls_windows(dist_dir):
    """把 osgeo .pyd 依赖的 GDAL DLL 闭包补拷进 dist 根目录(仅 Windows)。

    Nuitka 只自动复制 python/conda 前缀内的 DLL——CI 的 conda 布局
    (Library/bin 在前缀内)无需处理;OSGeo4W 等前缀外的布局会被剥掉,
    产物在本机能跑、干净机器上 GDAL 全挂。这里复用 Nuitka 自带的
    依赖扫描器(depends/pefile,构建时已下载)求完整闭包并补拷;
    dist 根目录即 exe 所在目录,永远在 Windows DLL 搜索路径首位。
    """
    if sys.platform != 'win32':
        return

    if _find_bundled_gdal_dll(dist_dir):
        print('GDAL DLLs already bundled by Nuitka (inside-prefix layout)')
        return

    import osgeo
    from nuitka.freezer.DllDependenciesWin32 import detectBinaryPathDLLsWin32

    osgeo_dir = os.path.dirname(osgeo.__file__)
    source_dir = os.path.dirname(osgeo_dir)

    copied = 0
    for name in os.listdir(osgeo_dir):
        if not name.endswith('.pyd'):
            continue
        pyd = os.path.join(osgeo_dir, name)
        dlls = detectBinaryPathDLLsWin32(
            is_main_executable=False,
            source_dir=source_dir,
            original_dir=osgeo_dir,
            binary_filename=pyd,
            package_name='osgeo',
            use_path=True,
            use_cache=True,
            update_cache=True,
        )
        for dll in dlls:
            if _is_windows_system_dll(dll):
                continue
            dest = os.path.join(dist_dir, os.path.basename(dll))
            if not os.path.exists(dest):
                shutil.copy(dll, dest)
                copied += 1
    print(f'Copied {copied} system DLLs into {dist_dir}')

    if not _find_bundled_gdal_dll(dist_dir):
        raise RuntimeError(
            'GDAL DLL collection failed: no gdal*.dll found in the bundle even '
            'after dependency closure copy. Install GDAL (e.g. conda-forge) or '
            'put the GDAL DLL directory on PATH, then rebuild.'
        )


def verify_no_missing_libs(dist_dir):
    """打包后自检:dist 内主程序 exe 与所有 .so 的 ldd 不得有 'not found'。

    LD_LIBRARY_PATH 指向 dist 根目录,模拟 exe 的传递性 RPATH($ORIGIN)——
    Nuitka 把 numpy/PIL 等 wheel 自带库放在 dist 根,靠 exe 的 RPATH 解析,
    直接对单个 .so 跑 ldd 会误报。exe 本体也要查:它是 ELF 可执行文件,
    文件名不含 .so,只按文件名过滤会漏掉它自身的缺库问题。
    """
    if sys.platform == 'win32' or sys.platform == 'darwin':
        return
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = os.path.abspath(dist_dir)
    targets = []
    exe = os.path.join(dist_dir, APP_NAME)
    if os.path.isfile(exe):
        targets.append(exe)
    for root, _dirs, files in os.walk(dist_dir):
        for name in files:
            if '.so' not in name:
                continue
            targets.append(os.path.join(root, name))
    missing = []
    for path in targets:
        out = subprocess.run(
            ['ldd', path], capture_output=True, text=True, check=False, env=env,
        ).stdout
        for line in out.splitlines():
            if 'not found' in line:
                missing.append(f'{path}: {line.strip()}')
    if missing:
        raise RuntimeError(
            'Bundle has unresolved shared library dependencies:\n'
            + '\n'.join(missing)
        )
    print('Shared library check OK (no unresolved dependencies)')


def alias_conda_tagged_extensions(dist_dir):
    """为 conda 版 osgeo 的 SWIG shim 补带 ABI 标签的扩展别名(Windows/macOS)。

    conda 的 osgeo/_gdal.py 用 pkg_resources.resource_filename 按带 ABI 标签的
    原名('_gdal.cp312-win_amd64.pyd' / '_gdal.cpython-312-darwin.so' 等,
    标签随 Python 版本变)定位扩展文件,
    而 Nuitka 把扩展重命名为无标签名(_gdal.pyd/_gdal.so)——resource_filename
    找不到文件,LoadLibrary 报 err 126「找不到指定的模块」。为每个带标签的
    源文件名在 dist 里补一份别名拷贝。
    """
    if sys.platform not in ('win32', 'darwin'):
        return

    import osgeo
    src_dir = os.path.dirname(osgeo.__file__)
    dst_dir = os.path.join(dist_dir, 'osgeo')
    if not os.path.isdir(dst_dir):
        return

    ext = '.pyd' if sys.platform == 'win32' else '.so'
    count = 0
    for name in os.listdir(src_dir):
        if not name.endswith(ext):
            continue
        plain = name.split('.', 1)[0] + ext
        if name == plain:
            continue
        src = os.path.join(dst_dir, plain)
        dst = os.path.join(dst_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy(src, dst)
            count += 1
    if count:
        print(f'Aliased {count} conda-tagged extension names in {dst_dir}')


def main():
    gdal_data = _first_existing_dir(_gdal_data_candidates(), required_file='epsg.wkt')
    proj_data = _first_existing_dir(_proj_data_candidates(), required_file='proj.db')

    # 缺数据目录的包能构建能启动,但每个 GDAL 调用都会在运行时失败——
    # "builds and runs but is broken"。在这里带清晰信息直接失败(I20b)。
    if not gdal_data:
        raise RuntimeError(
            'gdal-data collection failed: no GDAL data directory found. '
            'Install system GDAL (gdal-config) or set the GDAL_DATA environment '
            'variable, then rebuild.'
        )
    if not proj_data:
        raise RuntimeError(
            'proj-data collection failed: no PROJ data directory containing '
            'proj.db found. Set PROJ_DATA/PROJ_LIB or install the proj data '
            'package, then rebuild.'
        )

    print(f'GDAL data: {gdal_data}')
    print(f'PROJ data: {proj_data}')

    cmd = [
        sys.executable, '-m', 'nuitka',
        '--standalone',
        '--assume-yes-for-downloads',
        '--output-dir=dist',
        f'--output-filename={APP_NAME}',
        '--remove-output',
        # 应用资源
        '--include-data-dir=templates=templates',
        '--include-data-dir=static=static',
        # 全球 base 地形的分卷（2 个文件，约 167 MB）。打的是**分卷而非解压后的
        # 目录**：解压后是 44k 个小文件，让 Nuitka 逐个收集会把构建拖垮，装机体积
        # 也更大。首次切片时 base_terrain.ensure_base_unpacked() 会自动还原到
        # assets/terrain/base_z8（scripts/unpack_base_terrain.py 只是手工预热入口）。
        # 分卷缺失【运行期】不会坏 —— parentUrl 闸门检测到 base 不可用就不写该字段
        # （见 layer_json.parent_url_if_base_available）。但那是给「用户自己删了
        # 分卷」准备的退路,不是发布产物可以少东西的许可:漏收的包照常 200、切片
        # 照常完成,用户只看到地形不对 —— 所以 APP_DATA_SENTINELS 在构建期挡住它。
        '--include-data-dir=assets/terrain=assets/terrain',
        # 排除解压后的目录：上面那行是整目录收，而运行期解压的落点就在它里面。
        # 不排除的话，任何在本机跑过一次切片的人再构建，产物就平白多 224 MB /
        # 4.3 万个文件 —— 正是上面刻意避开的那件事。按**产物内路径**匹配。
        '--noinclude-data-files=assets/terrain/base_z8/**',
        f'--include-data-dir={gdal_data}=gdal-data',
        f'--include-data-dir={proj_data}=proj-data',
        # 动态加载、静态分析跟不住的包
        '--include-package=flask_socketio',
        '--include-package=socketio',
        '--include-package=engineio',
        # engineio 在 _websocket_wsgi 里运行时 import simple_websocket
        # (threading 模式的 WebSocket 升级),静态分析跟不住就退回 long-polling
        '--include-package=simple_websocket',
        '--include-package=aiohttp',
        '--include-package=aiofiles',
        '--include-package=osgeo',
        '--include-package=PIL',
        # 包内数据文件(matplotlib mpl-data、certifi CA 证书)
        '--include-package-data=matplotlib',
        '--include-package-data=certifi',
        '--nofollow-import-to=pytest',
        f'--jobs={os.cpu_count() or 4}',
        ENTRY,
    ]
    print('Running:', ' '.join(cmd), flush=True)
    subprocess.check_call(cmd)

    # Nuitka 输出在 dist/app.dist,重命名为 dist/terraforge 以保持原发布布局
    src = os.path.join('dist', 'app.dist')
    dst = os.path.join('dist', APP_NAME)
    if os.path.abspath(src) != os.path.abspath(dst):
        if os.path.exists(dst):
            shutil.rmtree(dst)
        os.rename(src, dst)

    exe = os.path.join(dst, APP_NAME + ('.exe' if sys.platform == 'win32' else ''))
    if not os.path.isfile(exe):
        raise RuntimeError(f'Build finished but executable not found: {exe}')
    verify_app_data(dst)

    copy_extension_system_libs(dst)
    copy_extension_system_dlls_windows(dst)
    alias_conda_tagged_extensions(dst)
    verify_no_missing_libs(dst)
    print(f'Build successful: {exe}')


if __name__ == '__main__':
    main()
