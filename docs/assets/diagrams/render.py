#!/usr/bin/env python3
"""把本目录（含中文版根目录与英文版 `en/`）的图源 HTML 渲染成 README 引用的 PNG
（@2x，仅图本身，不含标题与页脚）。

用法：`python3 docs/assets/diagrams/render.py [slug ...]`（省略 slug = 全部重渲）。
给了 slug 就把两种语言的同名图一起渲（`architecture` → zh + en，深浅共 4 张）。
为什么不直接截 HTML 页面：页面上还有标题、副标题与页脚，那些话 README 正文已经
说过一遍了，图里再来一遍就是重复。所以先把第一个 `<svg>` 抠出来单独包一页，尺寸
取 `viewBox`，`--force-device-scale-factor=2` 出 2 倍图。

字体：图源里的 `@font-face` 指向 `static/vendor/fonts/` 的相对路径（本仓的离线不
变量：不碰 CDN）。渲染时改写成绝对 `file://` 并加 `--allow-file-access-from-files`，
否则 Chrome 按不同源拒绝加载，落到系统回退字体。中文字形本来就走系统回退
（Inter / JetBrains Mono 都不含 CJK），装了 Noto Sans CJK 就行。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FONT_DIR = ROOT / 'static/vendor/fonts'
CHROME = ('google-chrome', 'chromium', 'chromium-browser', 'google-chrome-stable')
SCALE = 2

PAGE = """<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
*{{margin:0;padding:0}}
{faces}
html,body{{background:{paper}}}
:root{{--fs:'Inter','Noto Sans CJK SC','Noto Sans SC',sans-serif;
      --fm:'JetBrains Mono','Noto Sans Mono CJK SC','DejaVu Sans Mono',monospace}}
svg{{display:block;width:{w}px;height:{h}px}}
</style></head><body>{svg}</body></html>"""


def font_faces() -> str:
    base = f'file://{FONT_DIR}/'
    return ''.join(
        "@font-face{font-family:'%s';font-style:normal;font-weight:400 700;"
        "font-display:swap;src:url(%s%s-%s.woff2) format('woff2');}" % (fam, base, stem, sub)
        for fam, stem in (('Inter', 'inter'), ('JetBrains Mono', 'jetbrains-mono'))
        for sub in ('latin', 'latin-ext'))


def chrome() -> str:
    for name in CHROME:
        path = subprocess.run(['which', name], capture_output=True, text=True).stdout.strip()
        if path:
            return path
    raise SystemExit('找不到 Chrome / Chromium —— 装一个，或用别的无头浏览器截图')


def render(src: Path, browser: str, tmp: Path) -> str:
    html = src.read_text(encoding='utf-8')
    svg = re.search(r'<svg\b.*?</svg>', html, re.S)
    if not svg:
        raise SystemExit(f'{src.name}: 没找到 <svg>')
    body = re.sub(r'(?:\.\./)+static/vendor/fonts/', f'file://{FONT_DIR}/', svg.group(0))
    box = re.search(r'viewBox="0 0 (\d+) (\d+)"', body)
    if not box:
        raise SystemExit(f'{src.name}: <svg> 缺 viewBox，无法定尺寸')
    w, h = box.group(1), box.group(2)
    paper = re.search(r'--paper:([^;]+);', html)
    page = tmp / f'{src.parent.name}-{src.stem}.html'
    page.write_text(PAGE.format(faces=font_faces(), svg=body, w=w, h=h,
                                paper=paper.group(1).strip() if paper else '#ffffff'),
                    encoding='utf-8')
    png = src.with_suffix('.png')
    subprocess.run([browser, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-sandbox',
                    '--allow-file-access-from-files', f'--force-device-scale-factor={SCALE}',
                    f'--window-size={w},{h}', f'--screenshot={png}', f'file://{page}'],
                   check=True, capture_output=True, timeout=180)
    return f'{png.relative_to(ROOT)}  {int(w) * SCALE}x{int(h) * SCALE}  {png.stat().st_size // 1024} KB'


def main() -> None:
    wanted = sys.argv[1:]
    srcs = sorted(p for p in HERE.rglob('*.html')
                  if not wanted or p.stem in wanted or p.stem.removesuffix('-dark') in wanted)
    if not srcs:
        raise SystemExit(f'没有匹配的图源：{wanted}')
    browser = chrome()
    with tempfile.TemporaryDirectory(prefix='tf-diagrams-') as tmp:
        for src in srcs:
            print(render(src, browser, Path(tmp)))


if __name__ == '__main__':
    main()
