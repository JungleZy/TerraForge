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
import shutil
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 测试进程自己也碰 GDAL(夹具直接建 tif/png),不表态照样吃一条 FutureWarning。
# 按生产同一套策略声明,顺带保证 `pytest -q` 的 warnings summary 是干净的。
# 缺 GDAL 的环境(纯前端/工具类用例)不该因此收集失败,所以吞掉 ImportError。
try:
    from src.core.gdal_mode import pin_gdal_exception_mode

    pin_gdal_exception_mode()
except ImportError:
    pass


def find_real_bash():
    """返回一个**真的** GNU bash 路径，找不到返回 None。

    Windows 的 `C:\\Windows\\System32\\bash.exe` 是 WSL 的安装占位 stub：
    `shutil.which('bash')` 找得到、也能执行，但没装发行版时它只打印
    「Windows Subsystem for Linux has no installed distributions」（**UTF-16**，
    所以 `text=True` 读出来是一串夹 NUL 的乱码）并以非零码退出。用它跑脚本的
    用例于是全红，而且报错完全看不出真正原因 —— 2026-08-08 的发版就是这么断的：
    一个新用例直接 `subprocess.run(['bash', ...])`，本地和 Linux CI 全绿，
    Windows runner 上炸。

    可靠的验明正身：`--version` 必须输出 `GNU bash`（WSL/git-bash 都是 GNU，
    stub 只会打印安装提示）。Windows 上 git-bash 常不在 pytest 的 PATH 里，
    额外探测 Git for Windows 的默认安装位置。

    这个知识点原来住在 tests/test_fix_l1_entry_build_misc.py 里，随那批用例一起
    被删掉了，紧接着就有新用例重新踩坑 —— 所以挪到 conftest：任何要跑 shell
    的用例都该从这里取 bash，配 `needs_bash` 跳过。
    """
    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    if os.name == "nt":
        candidates += [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "Git", "bin", "bash.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "Git", "bin", "bash.exe"),
        ]
    import subprocess

    for exe in candidates:
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, timeout=10)
        except Exception:
            continue
        if proc.returncode == 0 and b"GNU bash" in proc.stdout:
            return exe
    return None


REAL_BASH = find_real_bash()

needs_bash = pytest.mark.skipif(
    REAL_BASH is None,
    reason="需要可用的 GNU bash（Windows 的 WSL 占位 stub 不算）")


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


@pytest.fixture(autouse=True)
def isolate_task_logs(monkeypatch, tmp_path):
    """每任务日志（logs/tasks/<pipeline>_<id>.log）不得写进工作树。

    `task_logging.task_log_dir()` 解析成 `Config.BASE_DIR / 'logs' / 'tasks'`，
    而各测试的隔离 helper 只 patch 了 DATABASE_PATH / DOWNLOADS_DIR / CACHE_DIR，
    **没有 BASE_DIR** —— 于是任何跑到管线线程入口的用例都会往仓库里写日志。
    `tests/test_no_repo_pollution.py` 明令 `<repo>/logs` 在测试期间不得出现，
    它在开发机上之所以是绿的，只是因为 `logs/` 在会话开始前就已存在（那条用例
    拿会话基线做跳过判断）；CI 上是干净 clone，基线为假，直接红。

    为什么 patch `Config.BASE_DIR` 而不是 patch `task_logging.task_log_dir`：
    BASE_DIR 是所有派生路径的根，挡住它同时挡住了下一个从它推路径的新模块。
    只挡一个函数是在跟具体实现赛跑。

    ⚠️ 本 fixture **不**替代各用例自己 patch 那四个路径。BASE_DIR 只影响
    「没被单独 patch 过」的派生路径；DATABASE_PATH 等在 Config 上是独立的类
    属性，不跟着 BASE_DIR 走（它们在 import 时就算好了）—— 下一个 fixture
    `point_database_at_a_path_that_does_not_exist` 专门堵 DATABASE_PATH。
    """
    try:
        from src.core import config as _config
    except Exception:  # 环境缺依赖时不阻断收集
        return
    sandbox = tmp_path / "_base"
    sandbox.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_config.Config, "BASE_DIR", sandbox, raising=False)


@pytest.fixture(autouse=True)
def point_database_at_a_path_that_does_not_exist(monkeypatch, tmp_path):
    """没自己隔离数据库的用例，必须在开发机上以**和干净 clone 一样**的方式失败。

    `Config.DATABASE_PATH` 是 import 期算好的类属性，指向 `<repo>/data/
    map_downloader.db`。开发机上这个文件存在（跑过程序），CI 上是干净 clone、
    连 `data/` 目录都没有 —— 于是「忘了隔离数据库」这一类用例在本地全绿、在
    CI 上以 `sqlite3.OperationalError: unable to open database file` 变红。
    v0.3.3 首次发版构建就是这么红的（`test_disk_recheck_inflight.py` 的一条
    用例构造 `DemDownloadEngine()`，构造函数读配置）。

    这里把它指向一个**父目录不存在**的沙箱路径，让两边行为一致：
    - 干净 clone 的语义被搬到开发机上，这类用例本地就红，不必等 CI；
    - 顺带堵死「测试写进用户真实任务库」这条更糟的路 —— 今天任何一条没隔离的
      用例都能往开发者正在用的 `data/map_downloader.db` 里写。

    父目录**故意不建**：建了的话 sqlite 会当场造一个空库，连接成功、查表失败，
    用例拿着一个静默的空库继续跑，与 CI 又对不上了。要的就是连不上。

    需要真数据库的用例照旧自己 patch `DATABASE_PATH`（各文件的 `isolated_config`
    / `client` 等 helper 都这么做），在本 fixture 之后生效，互不冲突。
    """
    try:
        from src.core import config as _config
    except Exception:  # 环境缺依赖时不阻断收集
        return
    monkeypatch.setattr(_config.Config, "DATABASE_PATH",
                        tmp_path / "_nodb" / "map_downloader.db", raising=False)


@pytest.fixture(scope="session", autouse=True)
def repo_unpacked_base_at_session_start():
    """会话开始时仓库里有没有解压后的底图 —— test_no_repo_pollution 的基线。

    要守的约束是「**测试运行**不得往仓库的 assets/terrain/ 解压」，不是「那个目录
    不许存在」：自 user_version=3 起 assets/terrain/base_z8 就是底图的正常落点，
    开发机上跑过一次切片（或让 init_database 把旧缓存搬过来）之后它合法存在。
    直接断言「不存在」会在正常开发机上误红，而红的原因与测试无关 —— 那种噪音
    只会训练人无视它。所以记下会话起点的状态，由用例比对是不是**测试**造成的。

    autouse + session 作用域：在第一个用例之前求值，晚于任何测试可能的污染就没
    意义了。CI 是全新克隆，起点必然是 False，走的是严格分支。
    """
    return os.path.exists(os.path.join(PROJECT_ROOT, "assets", "terrain", "base_z8"))


@pytest.fixture(scope="session", autouse=True)
def repo_logs_at_session_start():
    """会话开始时仓库根目录有没有 logs/ —— test_no_repo_pollution 的基线。

    与上面那条同理:logs/ 是运行日志的正常落点(logging_setup),开发机上真跑过
    一次程序之后它合法存在。要守的是「**测试运行**不得往仓库写运行日志」——
    任何在子进程里把 app.py 当 __main__ 跑的测试都必须把 Config.BASE_DIR
    指到 tmp_path,否则日志会写进真仓库。
    """
    from src.core.logging_setup import LOG_DIR_NAME
    return os.path.exists(os.path.join(PROJECT_ROOT, LOG_DIR_NAME))


@pytest.fixture(scope="session")
def _base_terrain_sandbox(tmp_path_factory):
    """空沙箱 + 会话级【永久】替换 bundle_dir(不走 monkeypatch)。

    永久替换是为了堵住一个用例边界管不住的窗口:`create_app()` 现在会调
    `start_warmup()`,底图缺失时它起一个**后台线程**去解压。那个线程什么时候读
    `bundle_dir` 不受用例生命周期约束 —— 实测 8 次里有 2 次,`create_app()` 都
    返回了它还没读到。只要这一读落在下面 `isolate_base_terrain` 的 teardown 之后,
    线程看到的就是真实仓库路径,于是把 assets/terrain 里 167 MB 的分卷解进
    assets/terrain/base_z8 —— 正是本文件与 test_no_repo_pollution.py 合力要挡的事,
    而且 CI 里测试跑在 Nuitka 打包之前,解出来的会被打进三个平台的产物。

    永久替换之后,用例之外的时刻 bundle_dir 同样指向沙箱,窗口不复存在。下面那个
    函数级 fixture 保留不动:它的 monkeypatch undo 现在还原到本替身而非真身,
    「用例自己 patch 一层来覆盖」的既有语义完全不变。
    """
    sandbox = tmp_path_factory.mktemp("base_terrain_sandbox")
    try:
        from src.services.terrain_tiling import base_terrain as bt
    except Exception:  # 环境缺依赖时不阻断收集
        return sandbox
    bt.bundle_dir = lambda: str(sandbox)
    return sandbox


@pytest.fixture(autouse=True)
def isolate_base_terrain(monkeypatch, _base_terrain_sandbox):
    """测试侧防护:不让任何测试把随包底图解压进仓库。

    `tile_dem_task_dir` 现在开头就调 `ensure_base_unpacked()`,而仓库里
    `assets/terrain/*.part` 是真实存在的 167 MB —— 任何调用真 tiler 的测试都会
    解出 224 MB / 4.3 万个文件到 `assets/terrain/base_z8`。两个后果:跑一次测试
    多等几分钟(现象不是报错,是「怎么这么慢」,极难归因),以及 CI 里测试跑在
    Nuitka 打包**之前**,解出来的东西会被打进三个平台的产物。

    做法是把分卷目录指到一个空沙箱:`base_parts_dir()` 找不到分卷就返回 None,
    `ensure_base_unpacked()` 随之返回 None,调用方走 parentUrl 兜底 —— 正是
    这些既有测试原本断言的路径,所以它们一行都不用改。

    打在 `bundle_dir` 而不是 `_assets_terrain_dir` 上:后者本身是两条用例的**被测
    对象**(`test_base_parts_dir_returns_none_without_parts` /
    `test_base_cache_dir_sits_next_to_the_parts` 验证的正是「打包目录 → assets/
    terrain」这层解析),把它整个换掉就等于把被测对象抽走。`bundle_dir` 低一层,
    那两条用例自己也 patch 它,setattr 打在后面覆盖本 fixture;要测真解压的用例
    (`test_base_terrain.py` 其余部分)patch 更上层的 `_assets_terrain_dir`,
    同样覆盖本 fixture。
    """
    try:
        from src.services.terrain_tiling import base_terrain as bt
    except Exception:  # 环境缺依赖时不阻断收集
        return
    monkeypatch.setattr(bt, "bundle_dir", lambda: str(_base_terrain_sandbox))


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


def geotiff_bytes(pixel_deg=30.0 / 111320.0, lon0=116.0, lat0=39.0,
                  width=8, height=8):
    """一小块真 GeoTIFF 的字节串（EPSG:4326，Float32）。

    等高线的创建路径会用 GDAL 打开每个上传文件求范围并集，并且（2026-08-08
    评审的 contour P2）在 GDAL 在位却打不开文件时直接 400 —— 所以「上传成功」
    类用例不能再喂 b"fake-tif-bytes"，那是在测一个已经被修掉的漏收。

    pixel_deg=0 会写出一个可读但地理变换退化的栅格：范围读得出、分辨率读不出，
    正好用来验证自动层级的兜底分支。
    """
    from osgeo import gdal, osr

    tmpdir = tempfile.mkdtemp(prefix="test_geotiff_")
    try:
        path = os.path.join(tmpdir, "dem.tif")
        ds = gdal.GetDriverByName("GTiff").Create(
            path, width, height, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((lon0, pixel_deg, 0, lat0, 0, -pixel_deg))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        ds.FlushCache()
        ds = None
        with open(path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
