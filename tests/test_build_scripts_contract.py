"""scripts/ 下构建脚本的文本契约。

这些脚本不在 CI 的执行路径上（PowerShell、需要自备全球 DEM），所以只能钉文本。
不钉的代价是真实发生过的：src-layout 迁移把代码从 `services/` 挪到
`src/services/` 之后，`build_global_base_terrain.ps1` 的 `python -m
services.terrain_tiling...` 一直没跟着改，脚本直接 ModuleNotFoundError；
而 docs/reference/terrain/global-base-build.md 引用它时给的却是带 `src.` 的
正确形态，读者照文档看只会以为脚本是好的。
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "scripts" / "build_global_base_terrain.ps1"
DOC = ROOT / "docs" / "reference" / "terrain" / "global-base-build.md"


def _ps1() -> str:
    assert PS1.is_file(), f"{PS1} 不存在"
    return PS1.read_text(encoding="utf-8")


def test_module_path_matches_the_src_layout():
    """`python -m` 的模块路径必须带 src. 前缀，且真的可导入。

    这条是照着已经发生过的失效写的：迁移后脚本停在旧路径上整整没人发现，
    因为没有任何测试执行或检查它。
    """
    text = _ps1()
    mods = re.findall(r"-m\s+([A-Za-z_][\w.]*)", text)
    assert mods, "脚本里找不到 `-m <module>` 调用"
    for mod in mods:
        assert mod.startswith("src."), (
            f"模块路径 {mod!r} 缺 src. 前缀 —— src-layout 迁移后会 ModuleNotFoundError")
        # 真的导得进来，而不只是字符串长得对
        parts = mod.split(".")
        target = ROOT.joinpath(*parts).with_suffix(".py")
        assert target.is_file(), f"{mod} 解析到 {target}，文件不存在"


def test_tile_size_matches_the_application_side():
    """必须显式传 --tile-size，且默认值与 TileParams.tile_size 一致。

    CLI 自己的默认是 17，应用侧是 65。不传的话 base 的顶点网格每轴比子层
    稀疏 4 倍，级联切换时几何精度跳变。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams

    text = _ps1()
    assert "--tile-size" in text, "脚本没传 --tile-size，会走 CLI 默认值 17"
    m = re.search(r"\[int\]\$TileSize\s*=\s*(\d+)", text)
    assert m, "找不到 $TileSize 的默认值"
    assert int(m.group(1)) == TileParams.tile_size, (
        f"脚本默认 tile-size {m.group(1)} 与应用侧 TileParams.tile_size "
        f"{TileParams.tile_size} 不一致")


def test_doc_quotes_the_script_as_it_actually_is():
    """文档引用的那行命令必须与脚本实际内容一致。

    文档此前引用的是「正确形态」而脚本是坏的 —— 这种不一致比整篇过时更坏，
    因为读者拿文档去核对脚本时会得到「一切正常」的结论。
    """
    doc = DOC.read_text(encoding="utf-8")
    script_mods = set(re.findall(r"-m\s+([A-Za-z_][\w.]*)", _ps1()))
    doc_mods = set(re.findall(r"-m\s+([A-Za-z_][\w.]*)", doc))
    assert script_mods <= doc_mods, (
        f"脚本用的模块路径 {script_mods - doc_mods} 在文档里找不到 —— 两者已脱节")


@pytest.mark.parametrize("flag", ["-i", "-o", "--max-level"])
def test_required_flags_are_still_passed(flag):
    """三个必传参数不能在重构中掉队。"""
    assert flag in _ps1(), f"脚本不再传 {flag}"
