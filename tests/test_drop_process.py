"""全窗口拖拽打开本地处理 / 导入下载区域的契约测试(2026-08-11 设计 §3.6 + §5.1)。

借鉴 GeoLibre 的窗口级 drag-drop:拖入时全屏遮罩提示,松手按后缀分流 ——
.tif/.tiff 喂给 #processForm 的 #localTerrainFiles 并打开 #processModal,
区域矢量文件(.geojson/.json/.kml/.kmz/.zip/.shp)交给 map.js 的
importRegionFile 落成当前下载区域。
"""

import os
import re
import shutil
import subprocess

import pytest

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
    assert "getElementById('processModal')" in src, '缺首页守卫(无弹窗页空载)'
    assert "'dragenter'" in src and "'dragleave'" in src and "'drop'" in src, (
        'dragenter/dragleave/drop 三个事件都要接'
    )
    assert 'DataTransfer' in src, '应用 DataTransfer 过滤构造 FileList(只留 .tif)'
    assert re.search(r'tiff?', src), '文件过滤必须认 .tif / .tiff'
    assert "getElementById('localTerrainFiles')" in src, '文件要喂给 #localTerrainFiles'
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
    assert 'z-index: 13000' in body, '遮罩 z-index 应为 13000(命令面板 13100 之下)'
    m = re.search(r'\.drop-veil--in\s*\{([^}]*)\}', css)
    assert m and 'opacity: 1' in m.group(1), '缺 .drop-veil--in 的显示态'
