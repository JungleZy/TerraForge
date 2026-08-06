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
