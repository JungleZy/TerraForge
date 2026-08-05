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


# ---------------------------------------------------------------------------
# 全球 base 地形的分卷与还原脚本
# ---------------------------------------------------------------------------

UNPACK = ROOT / "scripts" / "unpack_base_terrain.py"
ASSETS = ROOT / "assets" / "terrain"


def test_base_terrain_parts_exist_and_fit_github_limit():
    """分卷必须存在，且每一卷都在 GitHub 的 100 MB 单文件硬限制之内。

    合起来 167 MB —— 正因为超限才拆卷。哪天有人合并回单文件，push 会被
    GitHub 拒绝，而那时才发现就晚了（本地 commit 已经进历史）。
    """
    parts = sorted(ASSETS.glob("base_z8.tar.gz.part*"))
    assert parts, f"找不到 base 地形分卷：{ASSETS}"
    limit = 100 * 1024 * 1024
    for p in parts:
        size = p.stat().st_size
        assert size < limit, (
            f"{p.name} 有 {size/1048576:.1f} MB，超过 GitHub 单文件 100 MB 限制")
    total = sum(p.stat().st_size for p in parts)
    assert total > 50 * 1024 * 1024, f"分卷合计只有 {total/1048576:.1f} MB，像是残缺"


def test_unpack_script_targets_the_configured_base_path():
    """还原脚本的默认目标必须与 DEFAULT_CONFIGS 里的 terrain_global_base_path 一致。

    路由是拿配置值去磁盘找文件的，没有自动发现 —— 两者对不上就是 404，
    而且是静默的（地形不出来、控制台无报错）。
    """
    from src.core.database import DEFAULT_CONFIGS

    cfg = dict(DEFAULT_CONFIGS)["terrain_global_base_path"]
    text = UNPACK.read_text(encoding="utf-8")
    # 配置值形如 './downloads/terrain/base_z8'
    tail = cfg.strip("./").replace("\\", "/")
    assert tail.replace("/", '" / "') in text or 'DEFAULT_OUT' in text, "脚本没有默认目标"
    for seg in tail.split("/"):
        assert f'"{seg}"' in text, f"脚本默认目标缺少路径段 {seg!r}（配置是 {cfg}）"


def test_nuitka_packs_the_parts_not_the_expanded_dir():
    """打包必须收分卷，不能收解压后的目录。

    解压后是 44k 个小文件，让 Nuitka 逐个收集会把构建拖垮、装机体积也更大。
    """
    text = (ROOT / "nuitka_build.py").read_text(encoding="utf-8")
    assert "--include-data-dir=assets/terrain=assets/terrain" in text, \
        "nuitka_build.py 没有打包 base 地形分卷"
    # 只看**实际的 --include-data-dir 参数值**，不看注释 —— 注释里提到
    # downloads/terrain/base_z8 是在说明还原目标，不是打包配置。
    data_dirs = re.findall(r"--include-data-dir=([^'\"]+)", text)
    for d in data_dirs:
        assert "base_z8" not in d, (
            f"打包参数 {d!r} 收的是解压后的目录（44k 个文件）—— 应当收分卷")
