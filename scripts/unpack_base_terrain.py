#!/usr/bin/env python3
"""把随包分卷还原成全球 base 地形（手工入口）。

⚠️ 正常情况下**不需要跑它** —— 地形切片会在开头自动解压（见
`src/services/terrain_tiling/base_terrain.py:ensure_base_unpacked`）。这个脚本留作
排障与预热用：想在第一次切片之前先把那几分钟花掉，或者怀疑缓存坏了要强制重解。

解压逻辑一份都不在这里：分卷定位、就位判据、跨进程锁、临时目录原子改名全部
委托给 base_terrain。默认目标同样取自 `base_cache_dir()` 而不是自己拼 ——
路由是拿 config 表的 `terrain_global_base_path` 去磁盘找文件的，两处路径对不上
就是静默 404（地形不出来、控制台无报错），所以只能有一处事实来源。

为什么是分卷：z0-7 的 base 打包后 167 MB，而 GitHub 的**单文件硬限制是 100 MB**，
所以拆成 `base_z8.tar.gz.partaa` / `partab`。按字母序拼接即可还原，不需要专用
工具 —— `cat part* > x.tar.gz` 在任何平台都成立。

为什么不直接把 4.3 万个瓦片提交进 git：git 存全量历史且二进制不增量，4.3 万个
小文件会让 clone / status / checkout 明显变慢（Windows 上尤其明显）。

用法：
    uv run python scripts/unpack_base_terrain.py            # 解到默认位置
    uv run python scripts/unpack_base_terrain.py --force    # 已存在也重解
    uv run python scripts/unpack_base_terrain.py -o <dir>   # 解到指定目录
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.terrain_tiling.base_terrain import (  # noqa: E402
    base_cache_dir,
    ensure_base_unpacked,
    is_base_ready,
)


def main() -> int:
    default_out = base_cache_dir()
    ap = argparse.ArgumentParser(description="还原全球 base 地形（分卷 → 目录）")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help=f"解压目标（默认 {default_out}）")
    ap.add_argument("--force", action="store_true", help="已存在也重新解压")
    args = ap.parse_args()

    out = args.output or default_out
    if args.force and out.exists():
        print(f"清理旧目录 {out} …")
        shutil.rmtree(out)
    elif is_base_ready(out):
        print(f"✅ base 地形已就位：{out}（--force 可重解）")
        return 0

    def progress(_phase, frac):
        print(f"\r  解压中 {frac * 100:5.1f}%（4.3 万个小文件，Windows 上可能要几分钟）",
              end="", flush=True)

    try:
        result = ensure_base_unpacked(cache_dir=out, stage_cb=progress)
    except RuntimeError as e:
        print()
        print(f"❌ {e}", file=sys.stderr)
        return 1
    print()

    if result is None:
        print("❌ 找不到分卷：仓库里应当自带 assets/terrain/base_z8.tar.gz.part*\n"
              "   若是浅克隆或手工删过，重新拉取仓库即可。", file=sys.stderr)
        return 1
    if not is_base_ready(result):
        print(f"❌ 解压后校验不通过：{result}", file=sys.stderr)
        return 1

    n = sum(1 for _ in (result / "7").rglob("*.terrain")) if (result / "7").is_dir() else 0
    print(f"✅ 完成：{result}")
    print(f"   z7 瓦片 {n} 张（应为 32768）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
