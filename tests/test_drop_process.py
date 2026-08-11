"""全窗口拖拽 .tif 打开本地处理的契约测试(2026-08-11 设计 §3.6,P2)。

借鉴 GeoLibre 的窗口级 drag-drop:拖入时全屏遮罩提示,松手把过滤后的
.tif/.tiff 喂给 #processForm 的 #localTerrainFiles 并打开 #processModal。
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
    src = _read(DROP_JS)
    assert "getElementById('processModal')" in src, '缺首页守卫(无弹窗页空载)'
    assert "'dragenter'" in src and "'dragleave'" in src and "'drop'" in src, (
        'dragenter/dragleave/drop 三个事件都要接'
    )
    assert 'DataTransfer' in src, '应用 DataTransfer 过滤构造 FileList(只留 .tif)'
    assert re.search(r'tiff?', src), '文件过滤必须认 .tif / .tiff'
    assert "getElementById('localTerrainFiles')" in src, '文件要喂给 #localTerrainFiles'
    assert 'showToast' in src, '失败/无 tif 路径要走 showToast 提示'
    for key in ("'js.drop.hint'", "'js.drop.no_tif'", "'js.drop.failed'"):
        assert key in src, f'缺 i18n 键字面量 {key}(双向闭合按字面量扫)'
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
