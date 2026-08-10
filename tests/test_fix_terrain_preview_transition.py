"""高程/地形预览的进场过渡。

## 症状

点「预览」一个高程切片任务，画面是硬切的：地面几何从椭球（或上一份地形）
**原地跳**成新地形，还要当着用户的面逐级细化。

## 为什么没法用常规办法淡入

Cesium 的地形是全球单一 `viewer.terrainProvider`，赋值即整块球面重建 ——
既没有影像层那种 `alpha`，也没有两份地形共存做交叉淡化的余地
（`static/js/map.js` previewTask 的 terrain 分支）。

## 做法

在地图上罩一层薄雾（`.map-transition-veil`，复用 `--color-backdrop`）：
换 provider **之前**淡入，视野瓦片落地（`globe.tilesLoaded`）之后淡出。
等待信号与首屏 splash 同款（postRender 轮询 + 上限计时器兜底）——
tilesLoaded 在 flyTo 飞行期间会一直是 false，没有上限就等于把用户关在雾里。

薄雾不拦交互（`pointer-events: none`）、层级压在地图工具条之下：过渡期间
工具条与状态栏仍要清晰可点。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node 不可用，跳过 JS 行为断言")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _strip_js_comments(src):
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'(^|[^:])//[^\n]*', r'\1', src)


def _fn_body(src, name):
    """按花括号配对提取 `function name(...)` 的整个函数体（含外层 {}）。"""
    i = src.index(f'function {name}(')
    j = src.index('{', i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    raise AssertionError(f'{name} 函数体花括号不配对')


def _rule(css, selector):
    m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert m, f'找不到 {selector} 规则'
    return m.group(1)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------


def test_veil_is_a_real_fade_and_never_eats_clicks():
    css = _read("static", "css", "style.css")
    base = _rule(css, ".map-transition-veil")

    assert re.search(r'opacity:\s*0\s*;', base), \
        '薄雾基态不是透明 —— 它是常驻元素，不透明就等于永久盖着地图'
    assert re.search(r'transition:[^;]*opacity', base), \
        '没有 opacity 过渡 —— 那就只是把「地形硬切」换成「薄雾硬切」'
    assert re.search(r'pointer-events:\s*none\s*;', base), \
        '薄雾会吃掉鼠标事件 —— 过渡期间地图拖不动、按钮点不了'

    inn = _rule(css, ".map-transition-veil--in")
    assert re.search(r'opacity:\s*1\s*;', inn), '--in 没有把薄雾亮出来'


def test_veil_stays_below_the_map_toolbar():
    """薄雾压在工具条之下：过渡是给地图的，不是给控件的。"""
    css = _read("static", "css", "style.css")
    veil_z = re.search(r'z-index:\s*(\d+)', _rule(css, ".map-transition-veil"))
    tool_z = re.search(r'z-index:\s*(\d+)', _rule(css, ".map-toolbar"))
    assert veil_z and tool_z, '两条规则都要显式 z-index，否则层序靠源码顺序碰运气'
    assert int(veil_z.group(1)) < int(tool_z.group(1)), \
        f'薄雾 z-index {veil_z.group(1)} 盖住了工具条 {tool_z.group(1)}'


# ---------------------------------------------------------------------------
# 接线：换 provider 前必须先起雾
# ---------------------------------------------------------------------------


def test_veil_is_raised_before_the_terrain_swap():
    """顺序是承重的：先起雾再换 provider。

    反过来写，几何跳变已经发生在屏幕上了，雾再起来只是给一次硬切加了个尾巴。
    """
    code = _strip_js_comments(_read("static", "js", "map.js"))
    body = _fn_body(code, "previewTask")

    show = body.find("_showMapVeil(")
    swap = body.find("viewer.terrainProvider = provider")
    assert show >= 0, 'previewTask 里没有起雾 —— 地形预览仍是硬切'
    assert swap >= 0, '找不到 terrainProvider 赋值 —— 本测试已失效'
    assert show < swap, '起雾写在换 provider 之后 —— 跳变照样露给用户'

    assert "_hideMapVeilWhenTilesSettle(" in body, \
        '没有按瓦片落地淡出 —— 雾要么早散（露出细化过程）要么不散'
    assert "_hideMapVeil()" in body, \
        '异常路径没有把雾撤掉 —— 报错之后地图会一直蒙着'


# ---------------------------------------------------------------------------
# 行为
# ---------------------------------------------------------------------------


_FAKE_DOM = """
const nodes = {};
const host = { appendChild(el) { nodes[el.id] = el; } };
function makeEl() {
  return {
    id: '', className: '',
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    remove() { delete nodes[this.id]; },
  };
}
const document = {
  querySelector: () => host,
  createElement: () => makeEl(),
  getElementById: (id) => nodes[id] || null,
};
const requestAnimationFrame = (fn) => fn();
const postRender = { _l: [],
  addEventListener(f) { this._l.push(f); },
  removeEventListener(f) { this._l = this._l.filter((x) => x !== f); },
  fire() { this._l.slice().forEach((f) => f()); } };
let viewer = { scene: { postRender: postRender, globe: { tilesLoaded: false } } };
const veilIn = () => {
  const v = nodes['mapTransitionVeil'];
  return v ? v.classList.contains('map-transition-veil--in') : null;
};
"""


def _veil_script(tail):
    code = _read("static", "js", "map.js")
    fns = "\n".join(
        "function " + signature + " " + _fn_body(code, name)
        for name, signature in [
            ("_showMapVeil", "_showMapVeil()"),
            ("_hideMapVeil", "_hideMapVeil()"),
            ("_hideMapVeilWhenTilesSettle", "_hideMapVeilWhenTilesSettle(maxWaitMs)"),
        ])
    # 模块级状态（两个时长常量 + 移除计时器）在函数外，单独带上
    decls = re.findall(
        r'^(?:const|let) (?:_VEIL_[A-Z_]+|_veilRemoveTimer)\b[^\n]*$', code, re.M)
    assert len(decls) == 3, f"模块级声明抓到 {len(decls)} 条（期望 3）—— 测试已失效"
    return _FAKE_DOM + "\n".join(decls) + "\n" + fns + "\n" + tail


def _run_node(script):
    # encoding 必须显式给：Windows 上 text=True 默认按 locale（cp1252）解码，
    # node 输出里只要有一个中文字，读取线程就抛 UnicodeDecodeError，
    # stdout 静默变成 None，报错现场离真因十万八千里。
    return json.loads(subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
        timeout=60).stdout.strip())


@requires_node
def test_veil_fades_out_once_the_tiles_have_settled():
    """瓦片落地就散雾 —— 不必等上限计时器。"""
    out = _run_node(_veil_script(
        "const seen = [];\n"
        "_showMapVeil();\n"
        "seen.push(veilIn());\n"
        "_hideMapVeilWhenTilesSettle(5000);\n"
        "postRender.fire();          // 还没落地\n"
        "seen.push(veilIn());\n"
        "viewer.scene.globe.tilesLoaded = true;\n"
        "postRender.fire();\n"
        "seen.push(veilIn());\n"
        "seen.push(postRender._l.length);\n"
        "console.log(JSON.stringify(seen));\n"
    ))
    raised, waiting, settled, listeners = out
    assert raised is True, "起雾没生效"
    assert waiting is True, "瓦片没落地就散雾了 —— 用户会看到逐级细化的过程"
    assert settled is False, "瓦片落地了雾还不散"
    assert listeners == 0, "postRender 监听器没摘 —— 每次预览都会多挂一个，永久累积"


@requires_node
def test_veil_gives_up_waiting_after_the_cap():
    """瓦片永远落不齐（慢网络 / 飞行未停）也必须散雾。

    tilesLoaded 在 flyTo 飞行期间一直是 false；没有上限，用户就被关在雾里。
    """
    out = _run_node(_veil_script(
        "(async () => {\n"
        "  _showMapVeil();\n"
        "  _hideMapVeilWhenTilesSettle(60);\n"
        "  const before = veilIn();\n"
        "  await new Promise((r) => setTimeout(r, 120));\n"
        "  console.log(JSON.stringify([before, veilIn()]));\n"
        "})();\n"
    ))
    before, after = out
    assert before is True, "起雾没生效"
    assert after is False, "到了上限还没散雾 —— 慢网络下地图会一直蒙着"


@requires_node
def test_veil_element_is_removed_after_it_fades_out():
    """散雾之后要把元素撤掉，不留一个常驻的全屏 div。"""
    out = _run_node(_veil_script(
        "(async () => {\n"
        "  _showMapVeil();\n"
        "  const present = !!nodes['mapTransitionVeil'];\n"
        "  _hideMapVeil();\n"
        "  const rightAfter = !!nodes['mapTransitionVeil'];\n"
        "  await new Promise((r) => setTimeout(r, 600));\n"
        "  console.log(JSON.stringify([present, rightAfter,"
        " !!nodes['mapTransitionVeil']]));\n"
        "})();\n"
    ))
    present, right_after, later = out
    assert present is True, "没建出薄雾元素"
    assert right_after is True, \
        "淡出还没走完就把元素删了 —— 那是硬切，不是过渡"
    assert later is False, "淡出之后元素没被撤掉"
