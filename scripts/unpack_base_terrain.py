#!/usr/bin/env python3
"""把 assets/terrain/ 里的分卷还原成全球 base 地形。

为什么是分卷：z0-7 的 base 打包后 167 MB，而 GitHub 的**单文件硬限制是 100 MB**，
所以拆成 90 MB 的分卷（`base_z8.tar.gz.partaa` / `partab`）。按字母序拼接即可还原，
不需要专用工具 —— `cat part* > x.tar.gz` 在任何平台都成立。

为什么不直接把 44k 个瓦片文件提交进 git：git 存全量历史且二进制不增量，
44k 个小文件会让 clone / status / checkout 明显变慢（Windows 上尤其明显）。

用法：
    uv run python scripts/unpack_base_terrain.py            # 解到默认位置
    uv run python scripts/unpack_base_terrain.py --force    # 已存在也重解
    uv run python scripts/unpack_base_terrain.py -o <dir>   # 解到指定目录

解压目标默认是 `downloads/terrain/base_z8`，必须与 config 表的
`terrain_global_base_path` 一致（默认值就是它）—— 路由是拿配置值去磁盘找文件的，
目录对不上就是 404，没有自动发现。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARTS_DIR = ROOT / "assets" / "terrain"
PART_GLOB = "base_z8.tar.gz.part*"
DEFAULT_OUT = ROOT / "downloads" / "terrain" / "base_z8"


def find_parts() -> list[Path]:
    parts = sorted(PARTS_DIR.glob(PART_GLOB))
    if not parts:
        raise SystemExit(
            f"找不到分卷：{PARTS_DIR / PART_GLOB}\n"
            f"仓库里应当自带它们；若是浅克隆或手工删过，重新拉取仓库即可。")
    return parts


def already_ok(out: Path) -> bool:
    """已解压且看起来完整 —— 判据是 layer.json 在、且瓦片数量对得上。

    只看 layer.json 存在是不够的：解压中途被打断也会留下它，而一个
    layer.json 齐全但瓦片残缺的 base 会让 Cesium 拿到 404 瓦片。
    """
    lj = out / "layer.json"
    if not lj.is_file():
        return False
    # 抽查每一层的顶层 x 目录数，比全量 walk 快得多（44k 文件在 Windows 上很慢）
    for z, expect_x in ((0, 2), (4, 32), (7, 256)):
        d = out / str(z)
        if not d.is_dir() or len(list(d.iterdir())) < expect_x:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="还原全球 base 地形（分卷 → 目录）")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT,
                    help=f"解压目标（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    ap.add_argument("--force", action="store_true", help="已存在也重新解压")
    args = ap.parse_args()

    out: Path = args.output
    if out.exists() and already_ok(out) and not args.force:
        print(f"✅ base 地形已就位：{out}（--force 可重解）")
        return 0

    parts = find_parts()
    total = sum(p.stat().st_size for p in parts)
    print(f"分卷 {len(parts)} 个，合计 {total / 1048576:.1f} MB")
    for p in parts:
        print(f"  {p.name}  {p.stat().st_size / 1048576:.1f} MB")

    if out.exists():
        print(f"清理旧目录 {out} …")
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 先拼接到临时文件再解压：流式 gzip 解 tar 时若某一卷缺失，会在解到一半才炸，
    # 留下半个目录；先拼好能提前发现缺卷。
    with tempfile.TemporaryDirectory(dir=str(out.parent)) as td:
        merged = Path(td) / "base.tar.gz"
        print("拼接分卷 …")
        with open(merged, "wb") as dst:
            for p in parts:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, dst, length=1 << 24)

        print("解压中（44k 个小文件，Windows 上可能要几分钟）…")
        with gzip.open(merged, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tf:
            # tar 里的顶层目录就叫 base_z8；解到 out 的父目录再核对
            tf.extractall(out.parent)

    extracted = out.parent / "base_z8"
    if extracted != out:
        if out.exists():
            shutil.rmtree(out)
        extracted.rename(out)

    if not (out / "layer.json").is_file():
        print(f"❌ 解压后没有 layer.json，产物异常：{out}", file=sys.stderr)
        return 1

    n = sum(1 for _ in (out / "7").rglob("*.terrain")) if (out / "7").is_dir() else 0
    print(f"✅ 完成：{out}")
    print(f"   z7 瓦片 {n} 张（应为 32768）")
    print(f"   确认 config 表的 terrain_global_base_path 指向 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
