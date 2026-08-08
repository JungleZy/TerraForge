#!/usr/bin/env python3
"""校验当前环境的 GDAL Python 绑定能不能拿来构建。build.sh / build.bat 共用。

为什么不查 `GDAL==` 精确钉:requirements.txt 顶部写明了理由 —— 绑定是 sdist 现编,
编译时要对上系统 libgdal 的头文件,版本【跟随机器】,所以那里给的是范围。
2026-08-08 之前两个构建脚本都在查 `^GDAL==`,于是两条文档化的构建命令都不可用:

  · build.sh 有 `set -euo pipefail`,无命中的 grep 让脚本在【赋值那一行】就退出,
    紧随其后那句友好的「缺少 GDAL== pin」永远打不出来 —— 表现是 exit 1 + 零输出;
  · build.bat 每次都命中「缺少 GDAL== pin」分支,响亮地拒绝构建。

CI 走 `python nuitka_build.py` 绕开了这两个脚本,所以发版一直是绿的,没人发现。

这里改成检查两件真正决定「构建产物能不能用」的事:

  1. 装出来的版本落在 requirements.txt 声明的范围内;
  2. `_gdal_array` 在位。带 PEP 517 build isolation 装 GDAL 时 numpy 不可见,
     编出来的绑定会静默缺掉这个扩展,而 `gdal.__version__` 照样读得出 ——
     旧的 major.minor 比对检不出来。后果是 exe 能构建、能启动、能服务首页,
     而所有走 ReadAsArray/WriteArray 的 DEM/地形/等高线作业全炸
     (contour_engine / cesiumlab_terrain / download_engine 的拼接都在其中)。

解析放在 Python 而不是两个 shell 里,是因为「一份规则两处实现」正是上面那个
缺陷的成因:requirements.txt 的政策改了,而两个脚本里的正则没跟上。

用法:
    python scripts/check_gdal.py [requirements.txt 路径]
退出码 0 通过,非 0 附带可操作的修复指引。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# requirements.txt 里的依赖行形如 `GDAL>=3.8,<4`;行首锚定,避免命中注释里的示例
# (该文件的注释里就有 `pip install --no-build-isolation "GDAL==$(gdal-config --version)"`)。
_DEP_LINE = re.compile(r"^GDAL\s*(?P<spec>[^\s#]*)", re.MULTILINE)

_INSTALL_HINT = (
    "  正确装法(本地与 CI 一致,顺序不能调换):\n"
    "    uv pip install setuptools wheel\n"
    "    uv pip install numpy==1.26.4\n"
    '    uv pip install --no-build-isolation "GDAL==$(gdal-config --version)"\n'
    "  详见 docs/guides/BUILD.md 与 docs/guides/INSTALL.md。"
)


def _parse_version(text: str) -> tuple[int, ...]:
    """`3.11.4` -> (3, 11, 4)。取前三段数字,忽略 `3.8.4-1` 这类后缀。"""
    parts = re.findall(r"\d+", text)[:3]
    if not parts:
        raise ValueError(f"无法解析版本号: {text!r}")
    return tuple(int(p) for p in parts)


# 顺序有讲究:两字符运算符必须排在单字符前面,否则 `>=` 会被 `>` 抢先匹配。
_OPS: tuple[tuple[str, object], ...] = (
    ("<=", lambda got, want: got <= want),
    (">=", lambda got, want: got >= want),
    ("==", lambda got, want: got == want),
    ("!=", lambda got, want: got != want),
    ("<", lambda got, want: got < want),
    (">", lambda got, want: got > want),
)


def check_spec(spec: str, installed: str) -> str | None:
    """installed 不满足 spec 时返回一句人读的原因,满足返回 None。

    比较按【声明的精度】截断:`<4` 只比 major,`>=3.8` 比 major.minor。这正是
    requirements.txt 想表达的语义 —— 上限 `<4` 是「GDAL 4 默认开异常会让
    _raise_on_gdal_error 退化成空转」,与补丁号无关。
    """
    got_full = _parse_version(installed)
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        for symbol, compare in _OPS:
            if part.startswith(symbol):
                want = _parse_version(part[len(symbol):])
                if not compare(got_full[:len(want)], want):  # type: ignore[operator]
                    return f"GDAL {installed} 不满足 requirements.txt 的约束 {part}"
                break
        else:
            return f"无法解析 GDAL 版本约束 {part!r}(requirements.txt 里写的是 {spec!r})"
    return None


def main(argv: list[str]) -> int:
    req_path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "requirements.txt"
    try:
        text = req_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: 读不到 {req_path}: {exc}", file=sys.stderr)
        return 1

    match = _DEP_LINE.search(text)
    if match is None:
        print(f"Error: {req_path} 里找不到 GDAL 依赖行(应形如 `GDAL>=3.8,<4`)",
              file=sys.stderr)
        return 1
    spec = match.group("spec")

    try:
        from osgeo import gdal
        from osgeo import gdal_array  # noqa: F401  缺 _gdal_array 时就是这一行炸
    except ImportError as exc:
        print(
            f"Error: GDAL Python 绑定不可用: {exc}\n"
            "  · 若报的是 _gdal_array / gdal_array,说明装的时候带了 build isolation\n"
            "    (numpy 不可见),绑定缺 ReadAsArray/WriteArray —— exe 能构建能启动,\n"
            "    但所有 DEM / 地形 / 等高线作业都会在运行时炸。\n"
            "  · 若报的是 osgeo 本身,先装系统 GDAL\n"
            "    (apt-get install gdal-bin libgdal-dev / conda install -c conda-forge gdal)。\n"
            + _INSTALL_HINT,
            file=sys.stderr)
        return 1

    installed = gdal.__version__
    reason = check_spec(spec, installed)
    if reason is not None:
        print(f"Error: {reason}\n"
              "  Fix: 装一个落在该范围内的系统 GDAL,或改 requirements.txt 的范围\n"
              "  (改上限前先看该文件里 `<4` 那条注释 —— 有实测依据,不是随手写的)。\n"
              + _INSTALL_HINT,
              file=sys.stderr)
        return 1

    print(f"GDAL check OK (spec GDAL{spec}, installed {installed}, _gdal_array present)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
