"""生成应用图标 static/img/favicon.ico（多尺寸：16/24/32/48/64/128/256）。

图标是**生成物而非手绘素材**：这个脚本就是它的源文件，改配色/形状请改这里
再重跑，不要用图像编辑器直接改 .ico —— 否则下一次重跑会把手改静默盖掉。

    uv run python scripts/make_icon.py

配色取自 static/css/style.css 的设计令牌（--color-accent #38bdf8 /
--color-accent-strong #0ea5e9 / --color-on-accent #041e2b）：亮色底板 + 深色
山形，在浅色与深色的任务栏、标签页上都看得见（深底板图标在深色任务栏上会糊）。

三条构图上的取舍，是逐版渲染 16/32/48/64 px 缩略图比出来的，别当成随手写的数字：
  1. **主体四周要留白**。山脚顶到底板边缘那一版（左右留白 0，山高占满）在
     256 px 上就是两个撑满画框的三角形，廉价插画感全从这儿来。现在主体框是
     x 0.145-0.855、y 0.255-0.715。
  2. **深山浅底，不是浅山浅底**。白山配蓝底那一版在 16 px 上明暗几乎糊成一块；
     深色山体对亮底板的对比度足够，16 px 缩略图里山形和雪顶都还认得出。
  3. **明暗只切一刀**。按每座峰各自切亮/暗面，读出来是「白蓝相间的竖条」像柱状图；
     现在整体只在主峰垂线处分一次，左坡受光、右侧（含次峰）统一为暗面。

产物同时供两处使用，只此一份：
  - 网页标签页图标（templates/base.html 的 <link rel="icon">）
  - 打包 exe 的图标（nuitka_build.py 的 --windows-icon-from-ico）
"""
import os

from PIL import Image, ImageDraw

# 母版按 8 倍于最大输出尺寸绘制，缩放时的抗锯齿远好于直接画小图。
MASTER = 2048
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'img', 'favicon.ico',
)

PLATE_A = (93, 208, 252)        # 底板渐变的左上端
PLATE_B = (3, 121, 190)         # 底板渐变的右下端
SLOPE_LIT = (11, 56, 80)        # 受光的左坡
SLOPE_SHADE = (6, 34, 50)       # 背光的右侧（含次峰），近 --color-on-accent
SNOW = (240, 250, 255)

# 山形的控制点，全部是 0-1 的相对坐标。底边 BASE 水平，左右脚对称留白。
BASE = 0.715
L_FOOT, R_FOOT = 0.145, 0.855
PEAK = (0.395, 0.255)           # 主峰
SADDLE = (0.560, 0.470)         # 鞍部
PEAK2 = (0.665, 0.372)          # 次峰
RIDGE = [(L_FOOT, BASE), PEAK, SADDLE, PEAK2, (R_FOOT, BASE)]
# 雪顶：首尾两点**落在山轮廓线上**算出来的（左坡 y=0.360 处 x=0.3379，右坡
# 同高处 x=0.4756），中间是锯齿雪线。随手取值会让白色溢出山体外挂在天上。
SNOW_CAP = [
    PEAK, (0.4756, 0.360), (0.4455, 0.3315), (0.4175, 0.3805),
    (0.3880, 0.3235), (0.3620, 0.3675), (0.3379, 0.360),
]


def _plate(size):
    """带圆角的对角渐变底板。

    渐变先在 64×64 上算再放大：线性渐变放大无损，而在 2048² 上逐像素跑
    Python 循环要几秒。
    """
    n = 64
    gradient = Image.new('RGB', (n, n))
    px = gradient.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1))
            px[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(PLATE_A, PLATE_B)
            )
    gradient = gradient.resize((size, size), Image.BICUBIC)

    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=round(size * 0.225), fill=255,
    )
    plate = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    plate.paste(gradient, (0, 0), mask)
    return plate, mask


def render_master(size=MASTER):
    """画出母版图：圆角渐变底板 + 双峰山脊 + 雪顶。"""
    img, mask = _plate(size)
    draw = ImageDraw.Draw(img, 'RGBA')

    def poly(points):
        """把 0-1 的相对坐标铺开成像素坐标，改尺寸不用改上面那堆数字。"""
        return [(x * size, y * size) for x, y in points]

    # 整座山（受光面的底色）
    draw.polygon(poly(RIDGE), fill=SLOPE_LIT)
    # 主峰垂线以右统一为暗面：光源在左上，与底板渐变的亮端同侧
    draw.polygon(
        poly([PEAK, SADDLE, PEAK2, (R_FOOT, BASE), (PEAK[0], BASE)]),
        fill=SLOPE_SHADE,
    )
    draw.polygon(poly(SNOW_CAP), fill=SNOW)

    # 用底板蒙版重新切一次圆角：当前的控制点都在圆角内侧，这一步是给以后
    # 调坐标的人兜底 —— 画出界的部分会被裁掉，而不是长出四个方角。
    img.putalpha(Image.composite(img.getchannel('A'), Image.new('L', (size, size), 0), mask))
    return img


def main():
    master = render_master()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    # 每个尺寸都自己用 LANCZOS 从母版缩好、整套塞进 append_images，一个都不能省：
    # Pillow 的 ICO 编码器只在 append_images 里找不到对应尺寸时才自己缩放，而那条
    # 回退分支复用了上一个循环泄漏出来的变量（provided_im，即列表最后一张），
    # 缺哪个尺寸就会写进一张**别的尺寸**的图 —— 实测缺 256 时目录里出现两个 128。
    master.save(
        OUT_PATH, format='ICO',
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[master.resize((s, s), Image.LANCZOS) for s in ICO_SIZES],
    )
    print(f'Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH)} bytes, sizes: '
          + ', '.join(str(s) for s in ICO_SIZES) + ')')


if __name__ == '__main__':
    main()
