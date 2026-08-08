"""测试不得往仓库的 assets/terrain/ 里解压。

CI 流水线里测试跑在 Nuitka 打包**之前**：有一条测试解压到仓库里，224 MB /
4.3 万个文件就会被打进三个平台的产物。这个文件是那个约束的守卫。

第一道防线是 conftest 的 `isolate_base_terrain`（把 `bundle_dir` 指到空沙箱，
`base_parts_dir()` 因此找不到分卷、`ensure_base_unpacked()` 返回 None）。这里是
第二道：万一有人绕过了它，跑完测试就能看见。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_UNPACKED = os.path.join(_REPO, "assets", "terrain", "base_z8")


def test_test_run_did_not_unpack_into_the_repo(repo_unpacked_base_at_session_start):
    """跑测试不得让仓库里凭空多出解压后的底图。

    比对的是会话起点而不是「目录不存在」：assets/terrain/base_z8 自
    user_version=3 起就是底图的正常落点，开发机上跑过一次切片之后它合法存在，
    那不是测试造成的污染（.gitignore 挡住 git status，nuitka_build.py 的
    --noinclude-data-files 挡住产物）。CI 是全新克隆，起点必然是「不存在」，
    这条就退化成严格断言。
    """
    if repo_unpacked_base_at_session_start:
        pytest.skip(
            f"会话开始前仓库里就有 {_UNPACKED}（正常跑过切片的开发机），"
            f"不是测试造成的；CI 全新克隆走严格分支")
    assert not os.path.exists(_UNPACKED), (
        f"跑测试期间仓库里出现了解压后的底图：{_UNPACKED}\n"
        f"某条测试把它解到了仓库 —— 必须改用 tmp_path。CI 里测试跑在 Nuitka "
        f"打包之前，这 224 MB / 4.3 万个文件会被打进三个平台的产物。")


def test_gitignore_blocks_the_unpacked_base():
    with open(os.path.join(_REPO, ".gitignore"), encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    assert "assets/terrain/base_z8/" in lines, (
        ".gitignore 要挡住解压后的 4.3 万个文件，否则 git status 会被淹掉")


def test_gitignore_blocks_the_unpack_runtime_artifacts():
    """解压的另外两样运行期产物也要挡住：跨进程锁与中转目录。

    三样东西是同一次解压留下的，只挡了最大的那个不够：
      - `.base_unpack.lock`（`base_terrain._CacheLock` 的跨进程互斥锁）——
        解压跑过一次就在，且**不会**被清掉；
      - `.base_unpack_<pid>_*/`（`UNPACK_TMP_PREFIX` + pid 的中转目录）——
        正常路径由 finally 清掉，但崩溃 / SIGKILL 之后会留在仓库里直到下次启动
        清扫，期间它装着几万个文件，`git status` 直接被淹掉。

    ⚠️ 中转目录那条的通配必须与代码里的前缀常量对得上，所以这里**从代码读**
    `UNPACK_TMP_PREFIX` 再拼，而不是手抄一个字面量 —— 手抄的话改了常量这里照绿，
    而 .gitignore 已经不匹配了。
    """
    from src.services.terrain_tiling.base_terrain import UNPACK_TMP_PREFIX

    with open(os.path.join(_REPO, ".gitignore"), encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]

    assert "assets/terrain/.base_unpack.lock" in lines, (
        ".gitignore 要挡住解压的跨进程锁文件（运行期产物，解压过一次就在且不会清掉）")
    assert f"assets/terrain/{UNPACK_TMP_PREFIX}*/" in lines, (
        f".gitignore 要挡住解压中转目录 assets/terrain/{UNPACK_TMP_PREFIX}*/ —— "
        "崩溃 / SIGKILL 之后它带着几万个文件留在仓库里，直到下次启动清扫")


def test_test_run_did_not_write_logs_into_the_repo(repo_logs_at_session_start):
    """跑测试不得让仓库根目录多出 logs/。

    日志落盘（logging_setup）写的是 `<Config.BASE_DIR>/logs`，而 BASE_DIR 默认
    就是仓库根。任何在子进程里把 app.py 当 `__main__` 跑的测试都必须把
    `Config.BASE_DIR` 指到 tmp_path —— 只改 DATABASE_PATH / DOWNLOADS_DIR /
    CACHE_DIR 是不够的，那是本条要拦的确切失败方式（tests/test_fix_infra_e.py
    的 `_RUN_AS_MAIN` 就漏过一次）。

    与上面那条底图断言同构：比对会话起点而不是「目录不存在」，因为开发机上
    真跑过一次程序之后 logs/ 合法存在。CI 全新克隆走严格分支。
    """
    from src.core.logging_setup import LOG_DIR_NAME

    logs_dir = os.path.join(_REPO, LOG_DIR_NAME)
    if repo_logs_at_session_start:
        pytest.skip(f"会话开始前仓库里就有 {logs_dir}（正常跑过程序的开发机），"
                    f"不是测试造成的；CI 全新克隆走严格分支")
    assert not os.path.exists(logs_dir), (
        f"跑测试期间仓库里出现了运行日志目录：{logs_dir}\n"
        f"某条测试把 app.py 当 __main__ 跑却没有把 Config.BASE_DIR 指到 tmp_path。")


def test_gitignore_blocks_the_log_dir():
    """`*.log` 挡不住轮转产物 —— 后缀在 .log **之后**（terraforge.log.2026-08-07）。

    目录名从代码读（LOG_DIR_NAME），手抄字面量的话改了常量这里照绿，
    而 .gitignore 已经不匹配了。
    """
    from src.core.logging_setup import LOG_DIR_NAME

    with open(os.path.join(_REPO, ".gitignore"), encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    assert f"{LOG_DIR_NAME}/" in lines, (
        f".gitignore 要整个挡住 {LOG_DIR_NAME}/：轮转后的文件叫 "
        f"terraforge.log.2026-08-07，`*.log` 那条盖不住它")
