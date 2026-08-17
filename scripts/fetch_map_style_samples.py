"""重抓 `static/img/map-styles/` 的五张样式样例瓦片。

界面「地图样式」下拉旁的预览图（`static/js/map.js:initMapStylePreview`）与根
README 的样式表读的是同一批文件：`m/s/y/h/t.png`，各一张真实瓦片快照。

    uv run python scripts/fetch_map_style_samples.py                      # 默认重庆 z10
    uv run python scripts/fetch_map_style_samples.py --lat 29.56 --lon 106.55 --zoom 10
    uv run python scripts/fetch_map_style_samples.py --dry-run             # 只抓不写

四条约束，都是踩过的：

  1. **五张一起换，不许只补一张**。这五张并排出现在同一个下拉框和同一张表里，
     一张换了位置就成了「四张重庆、一张别处」——读者会把图源差异误读成样式差异。
     所以本脚本要么五张全部通过校验一起落盘，要么一张都不写。

  2. **走项目自己的出网路径，不写第二份**。样式码取 `source_registry.STYLE_CODES`
     （全仓唯一一份），URL 用 `tile_url_probe.expand_server_entry` 拼，代理用
     `proxy_autodetect.resolve_proxy_url` 解析，瓦片坐标用
     `region_tiles.lat_lon_to_tile`。这里任何一处自己算，就会出现「预览图取自
     A 图层、下载引擎取自 B 图层」的分叉。

  3. **落盘前过图片魔数**（`download_engine.looks_like_image`）。默认主机是明文
     http，运营商劫持返回 `200` + HTML 是教科书场景；不校验就会把一段 HTML
     存成 `.png`。

  4. **落盘前过亮度自检**。它挡的是「劫持页 / 错误响应被存成瓦片」那类事故 ——
     一张几乎全黑的图片能过魔数校验，但没有任何测试会因此变红。**但 `t` 是例外**：
     Google 的 `lyrs=t` 本身就是深色地形阴影叠加层（白底合成后平均亮度 7.4/255），
     那是它的真实样貌，不是坏图 —— 实测重抓与仓库里那张 MD5 逐字节相同，而同一格
     的 `lyrs=p`（完整地形底图）亮度 203.1。所以 `t` 列在 `DARK_BY_DESIGN` 里直接
     放行，其余样式低于阈值一律拒收（确认无误时用 `--allow-dark` 放行）。

`h.png` 是带透明的 PNG（道路叠加层），亮度按**白底合成后**计算 —— 直接读原始
像素会把透明区域算成黑色，把一张正常的叠加层判成黑图。
"""
import argparse
import io
import os
import sys
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.contracts.region_tiles import lat_lon_to_tile          # noqa: E402
from src.services.download_engine import looks_like_image       # noqa: E402
from src.services.proxy_autodetect import resolve_proxy_url     # noqa: E402
from src.services.source_registry import STYLE_CODES            # noqa: E402
from src.services.tile_url_probe import expand_server_entry     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'static', 'img', 'map-styles')

# 现有五张的取景：重庆一带（`static/js/map.js` 的注释写的就是这个位置）。
DEFAULT_LAT, DEFAULT_LON, DEFAULT_ZOOM = 29.56, 106.55, 10

# 抓哪些样式：`STYLE_CODES` 的四个码，外加 `h`（道路叠加层）—— 它在界面的样式
# 下拉里，但不在 STYLE_CODES 表里（那张表只登记会写进任务行的四种）。
STYLE_ORDER = list(STYLE_CODES.values()) + ['h']

# 默认服务器条目，与 `config.tile_servers` 的出厂值同形（别名，由
# expand_server_entry 展开成 mts0.googleapis.com）。
DEFAULT_SERVER = 'mts0'

# 白底合成后的平均亮度下限。正常的四张是 90~220，出事故的黑图会掉到个位数。
MIN_MEAN_LUMINANCE = 16.0

# 「本来就暗」的图层：`t` 是地形阴影叠加层，7.4 是它的正常值（见 docstring 第 4 条）。
DARK_BY_DESIGN = frozenset({'t'})

UA = {'User-Agent': 'Mozilla/5.0 (TerraForge sample tile fetcher)'}


def fetch(url, proxy_url, timeout=30):
    handlers = [urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})] \
        if proxy_url else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=UA)
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def mean_luminance_on_white(data):
    """白底合成后的平均亮度（0–255）。透明叠加层必须先合成再算，见模块 docstring。"""
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert('RGBA')
        white = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        grey = Image.alpha_composite(white, rgba).convert('L')
        pixels = list(grey.getdata())
    return sum(pixels) / len(pixels)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--lat', type=float, default=DEFAULT_LAT)
    ap.add_argument('--lon', type=float, default=DEFAULT_LON)
    ap.add_argument('--zoom', type=int, default=DEFAULT_ZOOM)
    ap.add_argument('--server', default=DEFAULT_SERVER,
                    help='tile_servers 条目形态：别名 / 主机名 / 完整 {z}{x}{y} 模板')
    ap.add_argument('--proxy', default='',
                    help='手动代理；留空则走项目的代理自动探测')
    ap.add_argument('--allow-dark', action='store_true',
                    help='放行平均亮度低于阈值的瓦片（DARK_BY_DESIGN 里的样式无需此项）')
    ap.add_argument('--dry-run', action='store_true', help='只抓取与校验，不写文件')
    args = ap.parse_args()

    x, y = lat_lon_to_tile(args.lat, args.lon, args.zoom)
    proxy_url = args.proxy or resolve_proxy_url('', auto_enabled=True)
    print(f'取景 lat={args.lat} lon={args.lon} zoom={args.zoom} → 瓦片 {args.zoom}/{x}/{y}')
    print(f'代理：{proxy_url or "直连（未探测到可用代理）"}\n')

    fetched, problems = {}, []
    for code in STYLE_ORDER:
        url = expand_server_entry(args.server, code).format(x=x, y=y, z=args.zoom)
        try:
            status, body = fetch(url, proxy_url)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            problems.append(f'{code}: 取不到（{type(exc).__name__}: {exc}）')
            continue
        if status != 200:
            problems.append(f'{code}: HTTP {status}')
            continue
        if not looks_like_image(body):
            problems.append(f'{code}: 响应体不是图片（前 8 字节 {body[:8]!r}）—— 疑似劫持页')
            continue
        luma = mean_luminance_on_white(body)
        flag = ''
        if luma < MIN_MEAN_LUMINANCE:
            if code in DARK_BY_DESIGN:
                flag = '  ← 地形阴影叠加层，暗是正常的'
            elif args.allow_dark:
                flag = '  ← 很暗，已按 --allow-dark 放行'
            else:
                problems.append(f'{code}: 白底平均亮度 {luma:.1f} < {MIN_MEAN_LUMINANCE}'
                                '，判为黑图（确认无误请加 --allow-dark）')
                continue
        fetched[code] = body
        print(f'  {code}.png  {len(body)/1024:6.1f} KB  平均亮度 {luma:5.1f}{flag}')

    if problems:
        print('\n一张都没写 —— 五张必须整套替换（见模块 docstring 第 1 条）。问题：')
        for p in problems:
            print('  -', p)
        return 1

    if args.dry_run:
        print(f'\n--dry-run：{len(fetched)} 张全部通过校验，未写文件。')
        return 0

    for code, body in fetched.items():
        with open(os.path.join(OUT_DIR, f'{code}.png'), 'wb') as fh:
            fh.write(body)
    print(f'\n{len(fetched)} 张已写入 {OUT_DIR}'
          f'\n记得同步 {os.path.join("static", "img", "map-styles", "README.md")} 里的取景与日期。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
