"""把两份 README 用到的**静态**徽章与国旗烤成仓库内的 PNG（docs/assets/badges/）。

徽章是**生成物而非手绘素材**：这个脚本就是它们的源文件，想改文字/配色/图标请改
下面的清单再重跑，不要用图像编辑器直接改 PNG —— 下一次重跑会把手改静默盖掉。

    uv run python scripts/build_readme_badges.py

四条取舍，别当成随手选的实现：

  1. **为什么落地成文件，而不是让 README 直接引 img.shields.io**。shields.io 与
     flagcdn 都是第三方服务：它们停服、限流、被墙，README 就变成一排碎图，而
     GitHub 的 camo 缓存过期后照样要回源。落地之后整份文档除两个动态徽章外零外链，
     离线 clone 里预览也齐全，与「前端第三方库全部本地 vendor」是同一条口径。

  2. **为什么用 Chrome 而不是 cairosvg 光栅化**。shields 的 SVG 用 `textLength`
     把文字拉到服务端算好的宽度，cairosvg 的 toy 字体 API 会逐字符挤压，中英文
     一律糊成墨团（实测「官网」两个汉字直接不可读）。Chrome 有完整字体栈与
     fontconfig，`--default-background-color=00000000` 还能给出真透明背景 ——
     徽章的圆角在 GitHub 深色主题下不会露白角。

  3. **动态徽章不在此列，故意的**。`github/v/release` 与 `github/actions/...` 的
     内容随仓库状态变，烤成 PNG 就冻在生成那一刻：发了新版 README 仍写着旧版本号，
     构建红了徽章还是绿的 —— 那不是「少一个外链」，那是文档在撒谎。这两个继续走
     远程，坏了最多是碎图，不会给出错的事实。

PNG 一律按 2× 生成（`--force-device-scale-factor=2`）再量化到 128 色，README 里用
`height=` 指定逻辑尺寸（flat 20、for-the-badge 28）：高分屏不糊，40 张合计不到
80 KB。量化是**目视比过**的 —— 徽章只有色块、白字与一层 10% 渐变，3× 放大对比
看不出与原图的差别，而不量化要 257 KB。

图标素材来自 shields.io 的 simple-icons 集合（素材本身 CC0，商标归各自所有者，
仅用于标识对应技术与数据源）。Windows 那枚四窗格图标是本脚本自绘的
（`WIN_LOGO`）—— simple-icons 已下架 Microsoft 全家，`logo=windows` 拿不到图标。
"""
import base64
import concurrent.futures as futures
import http.client
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'docs', 'assets', 'badges')

SHIELDS = 'https://img.shields.io/badge/'
# 2× 尺寸的国旗：README 里按 width="18" 显示。
FLAGCDN = 'https://flagcdn.com/w40/'

# 自绘的 Windows 四窗格标记（simple-icons 无 Windows/Microsoft 图标）。
WIN_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="white">'
    '<path d="M3 4.2l8.3-1.1v8.4H3V4.2zm9.7-1.3L21 1.8v9.7h-8.3V2.9zM3 12.5h8.3v8.4'
    'L3 19.8v-7.3zm9.7 0H21v9.7l-8.3-1.1v-8.6z"/></svg>'
)
WIN_LOGO = 'data:image/svg+xml;base64,' + base64.b64encode(WIN_LOGO_SVG.encode()).decode()


def badge(spec, **params):
    """`spec` 是 shields 的 `<label>-<message>-<color>` 段，已按 shields 规则转义。"""
    if 'logo' in params:
        params['logo'] = urllib.parse.quote(params['logo'], safe='')
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    return f'{SHIELDS}{spec}?{query}' if query else f'{SHIELDS}{spec}'


# ---------------------------------------------------------------- 徽章清单
# 键即产物文件名（无扩展名）。中英两版共用的只烤一份；带语言后缀的那几个是
# README.md / README.en.md 里文案不同的同一枚徽章。
BADGES = {
    # 页头
    'website-zh': badge('%E5%AE%98%E7%BD%91-terraforge--gis.pages.dev-38bdf8',
                        logo='cloudflarepages', logoColor='white'),
    'website-en': badge('Website-terraforge--gis.pages.dev-38bdf8',
                        logo='cloudflarepages', logoColor='white'),
    'python': badge('Python-3.12+-3776AB', logo='python', logoColor='white'),
    'license-mit': badge('License-MIT-3DA639', logo='opensourceinitiative', logoColor='white'),

    # 页头的三个下载按钮（for-the-badge，28 px 高）
    'download-windows-zh': badge('Windows-%E4%B8%8B%E8%BD%BD-0078D4',
                                 style='for-the-badge', logo=WIN_LOGO),
    'download-macos-zh': badge('macOS-%E4%B8%8B%E8%BD%BD-000000',
                               style='for-the-badge', logo='apple', logoColor='white'),
    'download-linux-zh': badge('Linux-%E4%B8%8B%E8%BD%BD-FCC624',
                               style='for-the-badge', logo='linux', logoColor='black'),
    'download-windows-en': badge('Windows-Download-0078D4',
                                 style='for-the-badge', logo=WIN_LOGO),
    'download-macos-en': badge('macOS-Download-000000',
                               style='for-the-badge', logo='apple', logoColor='white'),
    'download-linux-en': badge('Linux-Download-FCC624',
                               style='for-the-badge', logo='linux', logoColor='black'),

    # 「快速开始」平台表格里的小徽章
    'os-windows': badge('Windows-0078D4', logo=WIN_LOGO, logoColor='white'),
    'os-macos': badge('macOS-000000', logo='apple', logoColor='white'),
    'os-linux': badge('Linux-FCC624', logo='linux', logoColor='black'),

    # 四条管线的数据源与产物
    'google-maps': badge('Google%20Maps-4285F4', logo='googlemaps', logoColor='white'),
    'geotiff': badge('GeoTIFF-5CAE58', logo='gdal', logoColor='white'),
    'mbtiles': badge('MBTiles-003B57', logo='sqlite', logoColor='white'),
    'copernicus-glo30': badge('Copernicus%20GLO--30-003399',
                              logo='europeanunion', logoColor='white'),
    'aster-gdem-v3': badge('ASTER%20GDEM%20v3-0B3D91', logo='nasa', logoColor='white'),
    'quantized-mesh': badge('quantized--mesh-6CADDF', logo='cesium', logoColor='white'),
    'gebco-2024': badge('GEBCO%202024-006D8F'),
    'xyz-tiles-zh': badge('XYZ%20%E7%93%A6%E7%89%87-5CAE58', logo='gdal', logoColor='white'),
    'xyz-tiles-en': badge('XYZ%20tiles-5CAE58', logo='gdal', logoColor='white'),
    'leaflet': badge('Leaflet-199900', logo='leaflet', logoColor='white'),
    'openlayers': badge('OpenLayers-1F6B75', logo='openlayers', logoColor='white'),

    # 技术栈
    'flask': badge('Flask-000000', logo='flask', logoColor='white'),
    'flask-socketio': badge('Flask--SocketIO-010101', logo='socketdotio', logoColor='white'),
    'aiohttp': badge('aiohttp-2C5BB4', logo='aiohttp', logoColor='white'),
    'gdal': badge('GDAL-5CAE58', logo='gdal', logoColor='white'),
    'sqlite': badge('SQLite-003B57', logo='sqlite', logoColor='white'),
    'cesiumjs': badge('CesiumJS%201.143-6CADDF', logo='cesium', logoColor='white'),
    'bootstrap': badge('Bootstrap%205.3-7952B3', logo='bootstrap', logoColor='white'),
    'socketio': badge('Socket.IO-010101', logo='socketdotio', logoColor='white'),
    'vue': badge('Vue-4FC08D', logo='vuedotjs', logoColor='white'),
    'nuitka': badge('Nuitka-3776AB', logo='python', logoColor='white'),
    'pytest': badge('pytest-0A9EDC', logo='pytest', logoColor='white'),
    'uv': badge('uv-DE5FE9', logo='uv', logoColor='white'),

    # 「注意事项」的上游表格
    'esri': badge('Esri-005E95', logo='esri', logoColor='white'),
    'openstreetmap': badge('OpenStreetMap-7EBC6F', logo='openstreetmap', logoColor='white'),
}

# 语言切换用的国旗。flagcdn 直接给 PNG，不必光栅化。
FLAGS = {'flag-cn': FLAGCDN + 'cn.png', 'flag-gb': FLAGCDN + 'gb.png'}

UA = {'User-Agent': 'Mozilla/5.0 (TerraForge badge builder)'}
CHROME_CANDIDATES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')


def fetch_all(urls):
    """按主机复用一条 keep-alive 连接顺序取回。

    上一版是 8 线程并发 urlopen，本机（WSL）上 40 个 TLS 握手同时发起会集体
    卡死在 handshake 上 —— 同样的 URL 单条取只要 1 秒。徽章一共几十个、总量
    不到 100 KB，为它开并发本来就不成比例：一台主机一条连接、顺序发请求，
    握手只做一次，也不会给 shields 一串突发流量。
    """
    out = {}
    by_host = {}
    for name, url in urls.items():
        parts = urllib.parse.urlsplit(url)
        by_host.setdefault(parts.netloc, []).append(
            (name, parts.path + (('?' + parts.query) if parts.query else '')))

    for host, items in by_host.items():
        conn = http.client.HTTPSConnection(host, timeout=60)
        try:
            for name, path in items:
                for attempt in range(3):
                    try:
                        conn.request('GET', path, headers=UA)
                        resp = conn.getresponse()
                        body = resp.read()
                        if resp.status == 200:
                            out[name] = body
                            break
                        raise OSError(f'{host}{path[:60]} → HTTP {resp.status}')
                    except (http.client.HTTPException, OSError) as exc:
                        conn.close()
                        if attempt == 2:
                            sys.exit(f'取 {name} 失败：{exc}')
                        time.sleep(2 ** attempt)
                        conn = http.client.HTTPSConnection(host, timeout=60)
        finally:
            conn.close()
    return out


def find_chrome():
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit('找不到 Chrome/Chromium —— 徽章光栅化需要它，装一个再重跑：'
             + ' / '.join(CHROME_CANDIDATES))


def rasterise(chrome, name, svg_bytes, tmp_dir):
    """SVG → 2× 透明底、128 色 PNG。返回 (逻辑宽, 逻辑高)。"""
    head = svg_bytes[:400].decode('utf-8', 'replace')
    # 宽度可以是小数（`width="139.5"`），窗口尺寸只能给整数：向上取整，
    # 多出来的不到一像素是透明的，视觉上看不出来。
    def dim(attr):
        m = re.search(rf'{attr}="([\d.]+)"', head)
        if not m:
            sys.exit(f'{name}：SVG 头部没有 {attr} —— shields 换格式了？\n{head[:200]}')
        return math.ceil(float(m.group(1)))

    width, height = dim('width'), dim('height')

    svg_path = os.path.join(tmp_dir, name + '.svg')
    with open(svg_path, 'wb') as fh:
        fh.write(svg_bytes)
    shot_path = os.path.join(tmp_dir, name + '.shot.png')

    # 每次截图一个独立 user-data-dir：并行跑同一个 profile 会互相抢锁。
    subprocess.run([
        chrome, '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
        '--force-device-scale-factor=2', '--default-background-color=00000000',
        f'--user-data-dir={os.path.join(tmp_dir, "profile-" + name)}',
        f'--window-size={width},{height}', f'--screenshot={shot_path}',
        'file://' + svg_path,
    ], check=True, capture_output=True, timeout=180)

    # Chrome 出的是 32 位真彩 PNG（一张 17 KB）。徽章的实际颜色数是两位数，
    # FASTOCTREE 连半透明的抗锯齿边缘一起量化，圆角不会退化成硬边。
    with Image.open(shot_path) as shot:
        shot.load()
        quantised = shot.convert('RGBA').quantize(
            colors=128, method=Image.Quantize.FASTOCTREE)
    quantised.save(os.path.join(OUT_DIR, name + '.png'), optimize=True)
    return width, height


def main():
    chrome = find_chrome()
    os.makedirs(OUT_DIR, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        svgs = fetch_all(BADGES)
        flags = fetch_all(FLAGS)

        def one(item):
            name, svg = item
            return name, rasterise(chrome, name, svg, tmp_dir)

        # 光栅化是纯本地 CPU 活，这里并行才有意义。
        with futures.ThreadPoolExecutor(4) as pool:
            sizes = dict(pool.map(one, svgs.items()))

    for name, blob in flags.items():
        with open(os.path.join(OUT_DIR, name + '.png'), 'wb') as fh:
            fh.write(blob)

    total = 0
    for name in list(BADGES) + list(FLAGS):
        path = os.path.join(OUT_DIR, name + '.png')
        size = os.path.getsize(path)
        total += size
        logical = sizes.get(name)
        shape = f'{logical[0]}×{logical[1]} @2x' if logical else 'flagcdn w40'
        print(f'  {name+".png":<26} {size/1024:6.1f} KB  {shape}')
    print(f'\n{len(BADGES) + len(FLAGS)} 个产物，共 {total/1024:.0f} KB → {OUT_DIR}')


if __name__ == '__main__':
    main()
