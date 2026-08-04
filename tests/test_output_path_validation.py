"""resolve_output_dir / sanitize_filename —— output_path 与任务名的入口校验。

防的是评审发现 C5:output_path 未校验时可经网络写任意目录;task.name 直接拼
文件名可含 ../../ 逃逸。相对路径必须相对 Config.DOWNLOADS_DIR 解析,不能依赖
进程 CWD(评审发现 16:冻结 exe 搬迁后落盘与静态服务分叉)。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    from src.core import config

    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    return tmp_path / "downloads"


def test_relative_path_resolves_under_downloads(downloads):
    from src.services.geo_validation import resolve_output_dir

    assert resolve_output_dir("sub/dir") == str(downloads / "sub" / "dir")


def test_absolute_path_inside_downloads_ok(downloads):
    from src.services.geo_validation import resolve_output_dir

    assert resolve_output_dir(str(downloads / "abs")) == str(downloads / "abs")


def test_dotdot_escape_rejected(downloads):
    from src.services.geo_validation import resolve_output_dir

    with pytest.raises(ValueError):
        resolve_output_dir("../outside")


def test_absolute_path_outside_downloads_rejected(downloads, tmp_path):
    from src.services.geo_validation import resolve_output_dir

    with pytest.raises(ValueError):
        resolve_output_dir(str(tmp_path / "elsewhere"))


def test_downloads_root_itself_allowed(downloads):
    from src.services.geo_validation import resolve_output_dir

    assert resolve_output_dir(str(downloads)) == str(downloads)


def test_sanitize_filename_strips_path_segments():
    from src.services.geo_validation import sanitize_filename

    assert sanitize_filename("a/b\\c") == "a_b_c"
    assert ".." not in sanitize_filename("..\\..\\evil")
    assert sanitize_filename(" normal name-1 ") == "normal name-1"


def test_sanitize_filename_blank_gets_default():
    from src.services.geo_validation import sanitize_filename

    assert sanitize_filename("///") == "task"
    assert sanitize_filename("") == "task"
