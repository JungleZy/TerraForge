"""全窗口拖拽打开本地处理 / 导入下载区域的契约测试(2026-08-11 设计 §3.6 + §5.1)。

借鉴 GeoLibre 的窗口级 drag-drop:拖入时全屏遮罩提示,松手按后缀分流 ——
.tif/.tiff 喂给 #taskForm 的 #sourceFiles 并打开 #createPanel(预选
local_terrain 管线),区域矢量文件(.geojson/.json/.kml/.kmz/.zip/.shp)交给
map.js 的 importRegionFile 落成当前下载区域。

2026-08-15(设计 §2.5 入口收敛):两个弹窗 #downloadModal/#processModal 合并成
非模态的 #createPanel,两张表单合并成 #taskForm,两个文件框合并成 #sourceFiles。
本文件的锚点跟着换名,守的还是同一条链。
"""

import os
import re
import shutil
import subprocess

import pytest

# 2026-08-15 Task 3：层栈令牌化之后 `.drop-veil` 的 z-index 是
# `var(--z-drop-veil)`。复用 test_css_contract 的解析器而不是再写一个
# 「跟 var()」的小函数 —— 三份测试文件都要跟这一层，各写一份必然分叉。
from test_css_contract import _resolve_z_index

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DROP_JS = os.path.join(ROOT, 'static', 'js', 'drop_process.js')
INDEX_HTML = os.path.join(ROOT, 'templates', 'index.html')
CSS_PATH = os.path.join(ROOT, 'static', 'css', 'style.css')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_index_page_loads_drop_process():
    src = _read(INDEX_HTML)
    assert re.search(r'<script[^>]+src=[^>]*js/drop_process\.js', src), (
        'index.html 没有加载 js/drop_process.js'
    )


def test_drop_process_behavior_contract():
    """窗口级投放的行为契约:遮罩、两类文件的分流、失败提示、卡死自救。

    ⚠️ 登记(2026-08 §5.1 区域导入):拒绝文案的键从 `js.drop.no_tif` 换成
    `js.drop.unsupported`,不是改名而是**语义变了**。投放处理器现在收两类
    文件,「这里没有 tif」已经不再等于「这次投放没用」——拖一个 .geojson
    进来是完全合法的一条路径。只有两类都不匹配时才拒绝,文案要同时说清两类
    各接受什么后缀(catalog 里那句就是这么写的)。
    继续钉 `js.drop.no_tif` 的话:那条文案已经从 catalog 删掉,断言拦下的不是
    缺陷,而是这次改动本身。

    区域那条分支一并钉住(它就是本条翻面的原因):没有它,把 .geojson 分支整个
    删掉只会让用户回到「拖边界文件没反应」,而遮罩、tif 分支、i18n 键全绿。
    """
    src = _read(DROP_JS)
    # 首页守卫的判据从退场的 #processModal 换成 #createPanel:两者都是「这一页
    # 有没有新建任务的那张表」的唯一标志物,/config、/history 上都不存在,所以
    # 「无弹窗页空载」这条契约一字未变。
    assert "getElementById('createPanel')" in src, '缺首页守卫(无新建面板的页空载)'
    assert "'dragenter'" in src and "'dragleave'" in src and "'drop'" in src, (
        'dragenter/dragleave/drop 三个事件都要接'
    )
    assert 'DataTransfer' in src, '应用 DataTransfer 过滤构造 FileList(只留 .tif)'
    assert re.search(r'tiff?', src), '文件过滤必须认 .tif / .tiff'
    assert "getElementById('sourceFiles')" in src, '文件要喂给 #sourceFiles'
    assert 'showToast' in src, '失败/不支持的文件要走 showToast 提示'
    for key in ("'js.drop.hint'", "'js.drop.unsupported'", "'js.drop.failed'",
                "'js.region.drop.only_first'"):
        assert key in src, f'缺 i18n 键字面量 {key}(双向闭合按字面量扫)'
    assert "'js.drop.no_tif'" not in src, (
        "'js.drop.no_tif' 回来了 —— 它的语义(「这里没有 tif」= 失败)在区域导入"
        '落地后已经不成立,catalog 里也没有这条了'
    )
    # 区域矢量分流:后缀判定 + 单值语义 + 交给 map.js 的导入口。
    assert re.search(r'geojson[^\n]*kml[^\n]*(?:kmz|zip|shp)', src), (
        '认不出区域矢量后缀 —— 拖边界文件会掉进「不支持」提示'
    )
    assert 'importRegionFile' in src, (
        '区域文件没有交给 map.js 的 importRegionFile —— 投放彻底静默'
    )
    assert re.search(r"typeof importRegionFile === 'function'", src), (
        'importRegionFile 裸调没有 typeof 守卫 —— 加载顺序一变就是 ReferenceError,'
        '而它发生在投放处理器里,用户只看到「拖了没反应」'
    )
    assert 'blur' in src, '窗口失焦(blur)是遮罩卡死的自救路径'


def _open_local_process_body():
    """openLocalProcess() 的函数体(含外层 {})。按花括号配对切,不按行缩进 ——
    这个 IIFE 里所有顶层函数都缩进 4 空格,「顶格的 }」那一招在这里不成立。"""
    src = _read(DROP_JS)
    start = src.index('function openLocalProcess(')
    open_at = src.index('{', src.index(')', start))
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError('openLocalProcess 花括号不配对 —— 本测试已失效')


def test_the_dropped_tif_lands_in_a_visible_control():
    """投放必须**先**把管线切到 local_terrain,再喂文件输入。

    这是 2026-08-15 入口收敛引入的新不变量,不是换名:弹窗时代 #localTerrainFiles
    装在 #processModal 里,打开弹窗与预选处理类型是同一个动作的两半,喂文件的
    先后无所谓。现在 #sourceFiles 装在 #sourceUploadRow 里,而那一行的可见性由
    map.js 的 PIPELINE_FIELDS 显隐表按「管线 × 来源」算 —— 段控默认停在「瓦片」,
    那时整个 #sourceField 是 hidden 的。

    顺序反了的后果是静默的:DataTransfer 照样能把 FileList 塞进一个 hidden 的
    input,change 也照样派发,但用户看不见自己拖进来的文件名;而
    updateSourceTifInfo 读的 mode 来自 _currentPipeline(),瓦片管线下它算出的不是
    'terrain',信息卡讲的是另一条管线的层级。openCreatePanel('local_terrain')
    一次把管线、面板、显隐三件事办齐,所以它必须排在喂文件之前。
    """
    body = _open_local_process_body()
    switch = body.find("openCreatePanel('local_terrain')")
    feed = body.find('input.files =')
    assert switch != -1, (
        "openLocalProcess 没有 openCreatePanel('local_terrain') —— 拖进来的 .tif "
        '会落在默认的瓦片管线下,那条管线根本没有文件输入'
    )
    assert feed != -1, '本测试失效:openLocalProcess 不再往 input.files 写 FileList'
    assert switch < feed, (
        '先喂文件后切管线 —— 文件被塞进一个 hidden 的 #sourceFiles,用户看不见'
        '自己拖进来的文件名,信息卡也会按错的 mode 算层级'
    )


@pytest.mark.skipif(shutil.which('node') is None, reason='node 不可用')
def test_drop_process_js_passes_node_syntax_check():
    subprocess.run(['node', '--check', DROP_JS],
                   capture_output=True, text=True, check=True, timeout=120)


def test_drop_veil_css():
    css = re.sub(r'/\*.*?\*/', '', _read(CSS_PATH), flags=re.S)
    m = re.search(r'\.drop-veil\s*\{([^}]*)\}', css)
    assert m, '缺 .drop-veil 规则'
    body = m.group(1)
    assert 'pointer-events: none' in body, '遮罩必须纯展示(事件始终落 window)'
    # 13000 从字面量变成 var(--z-drop-veil)：跟一层 var() 再比，
    # 契约本身没有放宽（仍然必须**恰好**是 13000，且在命令面板 13100 之下）。
    z = re.search(r'z-index:\s*([^;]+);', body)
    assert z, '遮罩必须显式声明 z-index，否则层序靠源码顺序碰运气'
    got = _resolve_z_index(css, z.group(1))
    assert got == 13000, (
        f'遮罩 z-index 应为 13000(命令面板 13100 之下)，'
        f'实际 {z.group(1).strip()!r} -> {got}'
    )
    m = re.search(r'\.drop-veil--in\s*\{([^}]*)\}', css)
    assert m and 'opacity: 1' in m.group(1), '缺 .drop-veil--in 的显示态'
