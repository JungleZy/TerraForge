"""应用图标的契约：一份 .ico 同时供网页标签页与打包 exe 使用。

为什么值得一个测试文件：图标缺失是**最安静的一类回归**。删掉
static/img/favicon.ico，开发机上页面照常渲染、`/` 照常 200，只是标签页回到
浏览器默认的白纸；打包侧更糟 —— `--windows-icon-from-ico` 一旦被人从命令行里
拿掉，构建照样成功、exe 照样能跑，只是拿到的是 Nuitka 的默认图标，而这件事
在 CI 的冒烟测试里一个字都不会体现。

这里钉四件事：文件在、是多尺寸 ICO、模板引用它、打包命令用的是同一个路径。
"""
import os
import re
import struct

import pytest

import nuitka_build

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_REL = 'static/img/favicon.ico'
ICON_PATH = os.path.join(PROJECT_ROOT, *ICON_REL.split('/'))

# 16 是任务栏/标签页的下限，256 是 Windows「大图标」与文件资源管理器预览用的。
# 少了 16，小尺寸处会由系统从大图缩放，糊；少了 256，大图标处会拉花。
REQUIRED_SIZES = {16, 32, 48, 256}


def _ico_entry_sizes(path):
    """读 ICO 目录区，返回每一帧的宽度集合（字节 0 在 ICO 里表示 256）。"""
    with open(path, 'rb') as f:
        header = f.read(6)
        assert header[:4] == b'\x00\x00\x01\x00', f'{path} 不是 ICO 文件'
        count = struct.unpack('<H', header[4:6])[0]
        return {(f.read(16)[0] or 256) for _ in range(count)}


def test_icon_file_exists_and_is_a_multi_size_ico():
    assert os.path.isfile(ICON_PATH), (
        f'{ICON_REL} 不在 —— 用 `uv run python scripts/make_icon.py` 生成'
    )
    sizes = _ico_entry_sizes(ICON_PATH)
    assert REQUIRED_SIZES <= sizes, (
        f'{ICON_REL} 里只有 {sorted(sizes)} 这些尺寸，缺 '
        f'{sorted(REQUIRED_SIZES - sizes)} —— 小尺寸缺失会让标签页图标糊掉，'
        '256 缺失会让 Windows 的大图标视图拉花'
    )


def test_generator_script_is_the_source_of_the_icon():
    """图标是生成物，生成脚本必须在，且写的就是这个路径。"""
    script = os.path.join(PROJECT_ROOT, 'scripts', 'make_icon.py')
    assert os.path.isfile(script), 'scripts/make_icon.py 不在 —— 图标就没有源文件了'
    with open(script, encoding='utf-8') as f:
        text = f.read()
    assert "'static', 'img', 'favicon.ico'" in text, (
        '生成脚本的输出路径和本测试对不上了'
    )


def test_base_template_links_the_icon():
    """标签页图标必须由 base.html 显式声明（四个页面都 extends 它）。

    不显式写就得靠浏览器去 GET /favicon.ico，而应用没有那条根路由 —— 每开一页
    控制台多一条 404，图标还是没有。
    """
    with open(os.path.join(PROJECT_ROOT, 'templates', 'base.html'), encoding='utf-8') as f:
        markup = f.read()
    links = re.findall(r'<link[^>]*rel=["\']icon["\'][^>]*>', markup)
    assert len(links) == 1, (
        f'base.html 里有 {len(links)} 处 <link rel="icon">，期望恰好 1 处'
    )
    assert "url_for('static', filename='img/favicon.ico')" in links[0], (
        f'<link rel="icon"> 没有指向 {ICON_REL}：{links[0]}'
    )


@pytest.mark.parametrize('platform, expected', [
    ('win32', [f'--windows-icon-from-ico={os.path.join("static", "img", "favicon.ico")}']),
    # Linux 的 --linux-icon 只对 onefile 有效，macOS 的 --macos-app-icon 需要
    # app bundle，本脚本两样都不是 —— 给了只会被 Nuitka 忽略，故不给。
    ('linux', []),
    ('darwin', []),
])
def test_icon_options_are_windows_only(monkeypatch, platform, expected):
    monkeypatch.setattr(nuitka_build.sys, 'platform', platform)
    assert nuitka_build.icon_options() == expected


def test_icon_options_fail_loudly_when_the_icon_is_gone(monkeypatch):
    """图标丢了要在构建期红，而不是产出一个带 Nuitka 默认图标的 exe。"""
    monkeypatch.setattr(nuitka_build.sys, 'platform', 'win32')
    monkeypatch.setattr(nuitka_build, 'APP_ICON', 'no/such/icon.ico')
    with pytest.raises(RuntimeError, match='App icon missing'):
        nuitka_build.icon_options()


def test_nuitka_build_points_at_the_same_file_as_the_template():
    """常量与模板引用的是同一份文件 —— 两边各自改一半是这类回归的典型形态。"""
    assert nuitka_build.APP_ICON.replace(os.sep, '/') == ICON_REL
