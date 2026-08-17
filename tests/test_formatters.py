"""一个概念一个格式化器：字节 / 速度 / 时长 / 坐标（前端文本级 + node 行为级）。

改造前的实测事实（本文件就是为了让它们回潮时红）：

1. **三个字节格式化器、三套舍入规则。**
   - `static/js/ui.js` 的 `formatBytes`，注释自称「全站唯一一份」；
   - `static/js/task_center.js` 的 `formatSpeed`，自己一份 1024 进位循环，
     还写了一段「刻意分成两个函数」的理由；
   - `static/js/map.js` 的 `_fmtBytes`，**第三份、无文档**，舍入是
     `v.toFixed(v < 10 ? 1 : 0)`。
   同一个 `102400 B`，前两者读作 `100.0 KB`，第三者读作 `100 KB`。没有任何
   机制会报错 —— 缓存卡、产物清单、磁盘预算判决、TIF 信息卡各说各话。
   并且 `static/js/config.js` 的注释把 `formatBytes` 的家指到了
   **task_center.js**（错文件：/config 恰恰是不加载 task_center.js 的那一页，
   真按注释搬过去就是缓存卡一片 ReferenceError）。

2. **单位标签是 1000 进制的前缀，标在 1024 进制的数字上。** 代码除以 1024，
   标签写 `KB/MB/GB`；而产品里唯一的真限额 `api.region.too_large` 标的是
   `MiB`。用户拿界面读数去验算那条限额，每级偏 2.4%。计划的决定是**改标签**
   到 `KiB/MiB/GiB`，与后端那条限额对齐。单位词继续不翻译（中英通用），
   这一条原注释里就写着，仍然成立。

3. **坐标四档精度。** `task_list.js` 的 `toFixed(2)`（≈1.1 km —— 同一个选区在
   任务行和状态栏读出两个不同的框）、`map.js` 的 `toFixed(4)`、五处
   `toFixed(5)`、复制路径 `toFixed(6)`。位数散在调用点上，没有一处写着
   「为什么是这个位数」。收成两档：读数 5 位、详情 6 位，位数只由 ui.js 的
   `formatCoord` / `formatCoordExact` 持有。
   角度**跨度**（`east - west` 那种差值）不是坐标，是另一个概念，仍是 3 位 ——
   本文件把它当独立一类钉住数量，而不是放任。

4. **时长两套。** 90 分钟在下载面板读「1.5 小时」（`map.js` 内联
   `(count / 10 / 3600).toFixed(1)`，瓦片预估读数与大任务确认框各一处），
   在任务行读「1小时30分钟」（`task_center.js` 的 `formatDuration`）。
   内联那两处收口到 `formatDuration`。

为什么是文本级断言：本项目没有 JS 测试框架（无 package.json/vitest，且不打算
引入 —— 会破坏 PyInstaller 离线打包形态）。行为级的部分把函数源码抠出来交给
node 跑（`requires_node`，与 test_map_js_contract.py / test_fix_terrain_
preview_transition.py 同一套路），node 缺席时结构断言照样全跑。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用既有的花括号配对切函数体 / 剥注释工具，**不另写一份**（本计划的主题就是
# 「一个概念一个实现」，测试自己先做到）。
from test_css_contract import _js_function_body, _strip_js_comments  # noqa: E402

from src.i18n import catalog  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'static', 'js')

requires_node = pytest.mark.skipif(
    shutil.which('node') is None, reason='node 不可用，跳过 JS 行为断言')


def _js(name):
    with open(os.path.join(JS_DIR, name), encoding='utf-8') as f:
        return f.read()


def _js_names():
    """static/js/ 下的一手脚本（不含 static/vendor/，那不是我们的代码）。"""
    return sorted(n for n in os.listdir(JS_DIR) if n.endswith('.js'))


def _js_sources():
    """{文件名: 剥掉注释的源码}。

    必须剥注释：本次改动的每一处都配了一段解释「原来错在哪」的注释，里面
    必然复述 `KB`、`toFixed(4)`、`1024` 这些正被禁止的形态。拿原文匹配会把
    解释性注释当成回潮（test_css_contract.py 与 test_tasks_js_contract.py
    各踩过一次）。
    """
    return {n: _strip_js_comments(_js(n)) for n in _js_names()}


def _js_function_source(src, name):
    """连签名一起切出 `function <name>(...) { ... }`，用于喂给 node。

    `_js_function_body` 只给函数体；node 侧要的是一个可执行的声明。
    """
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\(', src)
    assert m, f'找不到 function {name}( —— 本测试已失效'
    body = _js_function_body(src, name)
    start = m.start()
    end = src.index(body, m.end()) + len(body)
    return src[start:end] + '}'


def _without_function(src, name):
    """挖掉 `function <name>` 的函数体，用于「除了它以外都不许有」的普查。"""
    body = _js_function_body(src, name)
    return src.replace(body, '\n')


def _sites(pattern, allow=()):
    """普查 static/js：返回命中 pattern 的 `文件: 片段` 列表。

    `allow` 是 `(文件名, 函数名)` 的白名单，这些函数体先挖掉再查 —— 有些形态
    在唯一实现内部是合法的（`/ 3600` 在 formatDuration 里就是它的活）。
    白名单是**具名**的：加一条就得写出理由，比把正则放宽一点点难糊弄。

    **不报行号**：剥注释会把整段块注释压掉，行号与源文件对不上；报一个错的
    行号比不报更浪费人。片段本身足够拿去 grep。
    """
    out = []
    for name, src in _js_sources().items():
        for allowed_file, func in allow:
            if allowed_file == name:
                src = _without_function(src, func)
        for m in pattern.finditer(src):
            out.append(f'{name}: {" ".join(m.group(0).split())}')
    return out


def _node_json(script):
    # encoding 必须显式给：Windows 上 text=True 按 locale（cp1252）解码，
    # 输出里的中文（如「1小时30分钟」）会让读取线程抛 UnicodeDecodeError，
    # stdout 静默变成 None，报错现场离真因十万八千里。
    try:
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=True, timeout=120,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        pytest.skip('node 启动超过 120 秒（CI runner 冷启动）；'
                    '同一批结构断言不依赖 node')
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. 字节：只剩一份 1024 进位循环
# ---------------------------------------------------------------------------

# 1024 进位循环的形态：循环头里出现 1024。三份实现全长这样
# （`while (value >= 1024 && i < units.length - 1)`）。
# 不用「出现 1024 就算」：map.js 的 TERRAIN_TILE_BYTES = 8.4 * 1024 是常量，
# 不是换算实现。
_SCALE_LOOP_RE = re.compile(r'\b(?:while|for)\s*\([^)]*\b1024\b')

# 字节单位数组的形态：以 'B' 起头的字符串数组。
_BYTE_UNIT_ARRAY_RE = re.compile(r"\[\s*'B'\s*,")

BYTE_FORMATTER_HOME = 'ui.js'


def test_exactly_one_byte_scaling_loop_in_static_js():
    """1024 进位循环全站只许有一处，且在 ui.js 的 formatBytes 里。

    这是本任务的主断言：三份实现的三套舍入规则不可能靠人盯着保持一致，
    唯一可靠的机制是**只有一份**。
    """
    found = _sites(_SCALE_LOOP_RE)
    assert len(found) == 1, (
        '1024 进位循环有 %d 处，只许 1 处（ui.js 的 formatBytes）：\n  %s'
        % (len(found), '\n  '.join(found)))
    assert found[0].startswith(BYTE_FORMATTER_HOME + ':'), (
        f'唯一的进位循环不在 {BYTE_FORMATTER_HOME} 里，而在 {found[0]}')

    body = _js_function_body(_strip_js_comments(_js('ui.js')), 'formatBytes')
    assert _SCALE_LOOP_RE.search(body), (
        'ui.js 里的进位循环不在 formatBytes 内 —— 换算又被搬到别处了')


def test_exactly_one_byte_unit_array_in_static_js():
    """单位表同理：两份单位表就是两套标签，改一处不改另一处不会有人发现。"""
    found = _sites(_BYTE_UNIT_ARRAY_RE)
    assert len(found) == 1, (
        f'字节单位数组有 {len(found)} 处，只许 1 处：{found}')
    assert found[0].startswith(BYTE_FORMATTER_HOME + ':'), found[0]


def test_the_third_byte_formatter_is_gone():
    """map.js 的 `_fmtBytes`（第三份、无文档）连名字都不许留下。

    名字留着就会有人再调它；调用点全部改成 window.formatBytes。
    """
    found = _sites(re.compile(r'_fmtBytes'))
    assert not found, (
        f'_fmtBytes（第三份字节格式化器或它的调用点）没删干净：{found}')


def test_map_js_calls_the_shared_byte_formatter():
    """删掉 `_fmtBytes` 之后，map.js 那 8 个调用点必须真的接上共享实现。

    只断言「_fmtBytes 消失」是不够的：把调用点连同读数一起删掉也能让上面
    那条过 —— 磁盘预算判决与 TIF 信息卡会静默失去数字。
    """
    src = _strip_js_comments(_js('map.js'))
    n = len(re.findall(r'window\.formatBytes\(', src))
    assert n >= 8, (
        f'map.js 只有 {n} 处 window.formatBytes( 调用，改造前 _fmtBytes 有 8 处 '
        '（磁盘预算 5 + TIF 信息卡 2 + 地形预估 1）')


def test_config_js_and_history_js_call_the_shared_byte_formatter():
    """另两条调用路径不许自己再长一份。"""
    for name in ('config.js', 'history.js'):
        src = _strip_js_comments(_js(name))
        assert 'formatBytes(' in src, f'{name} 不再调 formatBytes 了？'
        assert not _SCALE_LOOP_RE.search(src), f'{name} 长出了自己的进位循环'


# ---------------------------------------------------------------------------
# 2. formatSpeed 是显式包装，不是第二份实现
# ---------------------------------------------------------------------------

def test_format_speed_is_an_explicit_wrapper():
    """`formatSpeed` 必须调 formatBytes，且自己不许有进位循环 / 单位表。

    它与 formatBytes 的差别（`/s` 后缀、≥100 取整）是真的，但那是两个参数，
    不是一份复制。
    """
    body = _js_function_body(
        _strip_js_comments(_js('task_center.js')), 'formatSpeed')
    assert 'formatBytes(' in body, (
        'formatSpeed 没有调 formatBytes —— 它又是一份独立实现了')
    assert not _SCALE_LOOP_RE.search(body), 'formatSpeed 里还有自己的 1024 进位循环'
    assert not _BYTE_UNIT_ARRAY_RE.search(body), 'formatSpeed 里还有自己的单位表'
    assert 'roundAtHundred' in body, (
        'formatSpeed 没有把「≥100 取整」作为参数传下去 —— 那条显示规则丢了')
    assert "'/s'" in body or '"/s"' in body, (
        'formatSpeed 没有把 /s 后缀交给共享实现')


def test_shared_byte_formatter_takes_the_rounding_rule_as_a_parameter():
    """共享实现必须真的认那两个参数，否则包装是假的。"""
    body = _js_function_body(_strip_js_comments(_js('ui.js')), 'formatBytes')
    assert 'roundAtHundred' in body, 'formatBytes 不认 roundAtHundred'
    assert 'suffix' in body, 'formatBytes 不认 suffix'


# ---------------------------------------------------------------------------
# 3. 单位标签：1024 进制配 KiB/MiB/GiB
# ---------------------------------------------------------------------------

EXPECTED_BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB']

# SI 前缀（1000 进制）标在 1024 进制的数字上就是错的。只查**字符串字面量**：
# 注释与文档里说「省掉约 160 KB」是另一回事，剥注释已经处理掉了。
_SI_BYTE_LITERAL_RE = re.compile(r"""['"](?:[KMGT]B)(?:/s)?['"]""")


def test_byte_unit_labels_are_binary_prefixes():
    src = _strip_js_comments(_js('ui.js'))
    m = re.search(r"\[\s*'B'\s*,[^\]]*\]", src)
    assert m, 'ui.js 里找不到字节单位数组 —— 本测试已失效'
    units = re.findall(r"'([^']+)'", m.group(0))
    assert units == EXPECTED_BYTE_UNITS, (
        f'单位表是 {units}，应为 {EXPECTED_BYTE_UNITS} —— 代码除以 1024，'
        '标签就必须是 1024 进制的前缀')


def test_no_si_prefixed_byte_unit_literals_remain():
    """`'KB'` / `'MB/s'` 这类字面量全站为 0。

    留一处就够让两条读数用两把尺 —— 而这正是改造前的状态。
    """
    found = _sites(_SI_BYTE_LITERAL_RE)
    assert not found, (
        'SI 前缀的字节单位字面量还在（1024 进制不能标 KB/MB）：\n  '
        + '\n  '.join(found))


def test_binary_prefix_matches_the_only_real_limit_in_the_product():
    """界面单位与 `api.region.too_large` 的 MiB 是同一把尺。

    这条限额是产品里唯一会拒绝用户文件的字节阈值。界面读数与它对不上时，
    用户没法验算「我这个文件到底超没超」。
    """
    msg = catalog.MESSAGES['api.region.too_large']
    for lang in ('zh', 'en'):
        assert 'MiB' in msg[lang], (
            f'api.region.too_large[{lang}] 不再标 MiB，界面单位的对齐目标没了：'
            f'{msg[lang]!r}')
    assert 'MiB' in EXPECTED_BYTE_UNITS


def test_unit_words_stay_untranslated():
    """单位词不进 i18n —— 这条原注释里就写着，改造不推翻它，但要留痕。

    B/KiB/MiB 中英通用；给它们建 i18n 键只会多出一批 zh 与 en 逐字相同的
    条目，外加一次运行期查表。
    """
    ui_src = _js('ui.js')
    tc_src = _js('task_center.js')
    assert '单位词不翻译' in ui_src or '单位不翻译' in ui_src, (
        'ui.js 里「单位词不翻译」的约定说明没了 —— 下一个人会给单位建 i18n 键')
    assert '单位词不翻译' in tc_src or '单位不翻译' in tc_src, (
        'task_center.js 里同一条约定说明没了')
    unit_words = set(EXPECTED_BYTE_UNITS) | {u + '/s' for u in EXPECTED_BYTE_UNITS}
    offenders = [key for key, val in catalog.MESSAGES.items()
                 if val.get('zh', '').strip() in unit_words]
    assert not offenders, f'单位词被搬进 i18n 了：{offenders}'


# ---------------------------------------------------------------------------
# 4. 三处过时注释
# ---------------------------------------------------------------------------

def test_ui_js_no_longer_claims_two_deliberate_byte_formatters():
    """ui.js 那段「与 formatSpeed 刻意分成两个函数」的理由已经不成立。

    它当年还多说了一句「合成一个函数再传标志位等于把这条理由藏进一个布尔
    参数里」—— 而现在传的就是标志位，理由写在共享实现的注释里，没被藏起来。
    """
    src = _js('ui.js')
    for stale in ('刻意分成两个函数', '合成一个函数再传标志位'):
        assert stale not in src, f'ui.js 里「{stale}」这段过时理由还在'
    assert '全站唯一一份' in src, (
        'ui.js 不再声明自己是唯一一份 —— 这句话现在是**真的**，要留着')


def test_ui_js_header_documents_the_options_argument():
    """文件头的导出清单是这个模块的对外契约，签名变了就得跟着变。"""
    header = _js('ui.js').split('*/', 1)[0]
    assert 'window.formatBytes' in header
    assert 'opts' in header or 'options' in header, (
        'ui.js 文件头还写着 formatBytes(bytes) 单参 —— 新增的舍入/后缀参数没登记')
    assert 'window.formatCoord' in header, (
        'ui.js 文件头没登记 formatCoord —— 坐标精度的唯一持有者不在对外清单里')


def test_config_js_comment_points_at_the_right_file():
    """`config.js` 的注释原来把 formatBytes 指到 task_center.js —— 错文件。

    /config 恰恰是把 base.html 的 vendor_task_list_js 块覆盖成空的那一页，
    task_center.js 在这一页根本不加载。真按注释把函数搬过去，缓存卡就是
    一片 ReferenceError。
    """
    src = _js('config.js')
    assert 'task_center.js 的 formatBytes' not in src, (
        'config.js 还在说 formatBytes 在 task_center.js 里 —— 指错了文件')
    assert 'ui.js 的 formatBytes' in src, (
        'config.js 的注释没有指向 ui.js —— 下一个人得自己全仓搜')


def test_task_center_cross_reference_comment_is_true():
    """task_center.js 的交叉引用注释要说出第三份的下场，并且不再自称「两个函数」。"""
    src = _js('task_center.js')
    assert '刻意是**两个**函数' not in src, (
        'task_center.js 还在说 formatSpeed 与 formatBytes 刻意是两个函数')
    assert 'ui.js' in src, 'task_center.js 的交叉引用丢了 formatBytes 的家'
    assert '_fmtBytes' in src, (
        'task_center.js 的注释没记下 map.js 那份 _fmtBytes 的下场 —— '
        '知识一丢，下一个人会再抄第三份')


# ---------------------------------------------------------------------------
# 5. 坐标精度两档
# ---------------------------------------------------------------------------

COORD_WORDS = r'(?:lng|lon|lat|north|south|east|west)'

# 角度**跨度**：`(a.east - a.west).toFixed(n)`。它不是坐标，是选区尺寸，
# 3 位（≈100 m）就够 —— 状态栏那一行读的是「这个框多大」，不是「它在哪」。
_SPAN_RE = re.compile(
    r'\(\s*[\w.]*' + COORD_WORDS + r'[\w.]*\s*-\s*[\w.]*' + COORD_WORDS
    + r'[\w.]*\s*\)\s*\.toFixed\((\d)\)', re.I)

# 坐标上的 toFixed：紧跟在经纬度标识符后面。
_COORD_TOFIXED_RE = re.compile(
    r'\b[\w.\[\]\'"]*' + COORD_WORDS + r'[\w.\[\]\'"]*\s*\)?\s*\.toFixed\((\d)\)',
    re.I)

COORD_READOUT_DIGITS = 5
COORD_DETAIL_DIGITS = 6
SPAN_DIGITS = 3
# 沿革：2026-08-15 之前锚点是 4 —— 状态栏选区摘要 2（w/h）+ 面板里那句只读摘要
# `#createPanelBounds` 的 2（`updateCreatePanelBounds()` 里同名的 w/h）。工具条
# 瘦身把那句只读摘要连同 `js.map.download.bounds_summary` 一起退役（同一个四至
# 原本在面板里渲染两遍，一份可编辑一份只读），跨度读数跟着少两处。
# 实测（2026-08-17，拿 _SPAN_RE 普查 static/js/）：map.js:2981 / 2982，两处都在
# `updateBoundsInfo()` 里喂 `js.map.status.selection`，就是状态栏那一行的宽高。
SPAN_SITE_COUNT = 2        # 状态栏选区尺寸 w + h
# 跨度读数的唯一宿主：选区每次变化的出口。数量从 4 掉到 2 之后，光数个数已经
# 不够（见下面那条用例的 docstring），要连宿主一起钉。
SPAN_HOST_FN = 'updateBoundsInfo'


def test_coordinate_precision_lives_in_exactly_two_functions():
    """位数只由 ui.js 的两个函数持有，且就是 5 与 6。

    改造前四档位数散在 12 个调用点上，没有一处写着「为什么是这个位数」。
    """
    src = _strip_js_comments(_js('ui.js'))
    readout = _js_function_body(src, 'formatCoord')
    detail = _js_function_body(src, 'formatCoordExact')
    assert re.search(r'toFixed\(%d\)' % COORD_READOUT_DIGITS, readout), (
        f'formatCoord 不是 {COORD_READOUT_DIGITS} 位：{readout.strip()!r}')
    assert re.search(r'toFixed\(%d\)' % COORD_DETAIL_DIGITS, detail), (
        f'formatCoordExact 不是 {COORD_DETAIL_DIGITS} 位：{detail.strip()!r}')
    assert 'window.formatCoord' in src and 'window.formatCoordExact' in src, (
        '两个坐标格式化器没有全局导出 —— map.js / history.js / task_list.js '
        '拿不到它们，只能各自再写 toFixed')


def test_no_coordinate_tofixed_outside_the_two_formatters():
    """全仓经纬度上的 `toFixed(...)` 为 0（ui.js 的两个实现除外）。

    计划点名的是 `toFixed(2)` 与 `toFixed(4)`，但只禁这两个位数等于默许
    第五档 —— 位数在调用点上出现本身就是问题。
    """
    found = []
    for name, src in _js_sources().items():
        if name == BYTE_FORMATTER_HOME:
            continue
        stripped = _SPAN_RE.sub('SPAN', src)     # 跨度是另一类，下一条管
        for m in _COORD_TOFIXED_RE.finditer(stripped):
            found.append(f'{name}: {m.group(0).strip()}')
    assert not found, (
        '经纬度上还有裸 toFixed（位数必须只由 formatCoord/formatCoordExact 持有）：\n  '
        + '\n  '.join(found))


def test_no_coordinate_tofixed_2_or_4_anywhere():
    """计划点名的两档单独钉一条 —— 它们是可见缺陷，不只是风格问题。

    `toFixed(2)` 是 ≈1.1 km：同一个选区在任务行和状态栏读出两个不同的框。
    """
    found = _sites(re.compile(
        r'[\w.]*' + COORD_WORDS + r'[\w.]*\s*\)?\s*\.toFixed\([24]\)', re.I))
    assert not found, f'经纬度上还有 toFixed(2)/toFixed(4)：{found}'


def test_the_two_tiers_are_both_actually_used():
    """两档都要有调用点，否则「两档」只是纸面上的。"""
    users = {}
    for name, src in _js_sources().items():
        if name == BYTE_FORMATTER_HOME:
            continue
        users[name] = (
            # 引用而不是调用：几处调用点把它取成局部别名
            # （`const f = window.formatCoord;`），因为读数被塞进一段很长的
            # 模板字面量里，逐处内联只会让那几行更难读。别名照样让位数只有
            # 一个来源 —— 这里要守的是「位数不在调用点上」，不是调用写法。
            len(re.findall(r'\bformatCoord\b', src)),
            len(re.findall(r'\bformatCoordExact\b', src)),
        )
    total_readout = sum(v[0] for v in users.values())
    total_detail = sum(v[1] for v in users.values())
    assert total_readout > 0 and total_detail > 0, (
        f'两档坐标格式化器没有都被用上：'
        f'{ {k: v for k, v in users.items() if any(v)} }')
    for name in ('map.js', 'history.js', 'task_list.js'):
        assert sum(users[name]) > 0, (
            f'{name} 不再走共享的坐标格式化器 —— 它原本有坐标读数')


def test_degree_span_is_a_separate_named_class():
    """角度跨度仍是 3 位，数量钉住，且只住在状态栏那一行。

    它与坐标是**两个概念**：跨度回答「这个框多大」，坐标回答「它在哪」。
    不把它并进两档里，但也不放任 —— 数量一变就要有人来解释。

    2026-08-17 迁移：锚点数 4 -> 2（沿革写在 SPAN_SITE_COUNT 上方）。只把 4 改成
    2 会让这条断言松掉一半 —— 「有 2 处」不再等于「就是状态栏那 2 处」，跨度改天
    悄悄搬去别的读数、状态栏那行改走 formatCoord，个数照样是 2，本条照样绿。所以
    补一条宿主判据：两处必须都在 `updateBoundsInfo()` 里、喂的必须还是状态栏那个
    键。要守的不变量（度数跨度是与坐标不同的一档读数、3 位、不许被当坐标走
    formatCoord）一个字没放宽。
    """
    sites = []
    for name, src in _js_sources().items():
        for m in _SPAN_RE.finditer(src):
            sites.append((name, int(m.group(1))))
    assert len(sites) == SPAN_SITE_COUNT, (
        f'角度跨度读数有 {len(sites)} 处，锚点是 {SPAN_SITE_COUNT}：{sites}\n'
        '新增的跨度读数要么用 3 位，要么说明为什么不是')
    assert {s[1] for s in sites} == {SPAN_DIGITS}, (
        f'角度跨度出现了 {SPAN_DIGITS} 位之外的精度：{sites}')
    host = _js_function_body(_strip_js_comments(_js('map.js')), SPAN_HOST_FN)
    assert len(_SPAN_RE.findall(host)) == SPAN_SITE_COUNT, (
        f'{SPAN_SITE_COUNT} 处跨度读数不再都住在 {SPAN_HOST_FN}() 里 —— '
        '个数对不代表钉的还是原来那两处，搬家要么改这里的宿主要么说明为什么')
    assert 'js.map.status.selection' in host, (
        f'{SPAN_HOST_FN}() 里的跨度不再喂 js.map.status.selection —— '
        '状态栏那一行是选区尺寸唯一的常驻读数（浮层退场后更是唯一的）')


# ---------------------------------------------------------------------------
# 6. 时长只有一份实现
# ---------------------------------------------------------------------------

DURATION_KEYS = [
    'js.tasks.duration.seconds',
    'js.tasks.duration.min_sec',
    'js.tasks.duration.minutes',
    'js.tasks.duration.hour_min',
    'js.tasks.duration.hours',
]

# `/ 3600` 就是在自己算小时。两处例外是具名的，不放宽正则：
#   task_center.js formatDuration —— 唯一实现，除以 3600 是它的活；
#   map.js _parseCoordPart      —— 那个 3600 是**角秒**（DMS 解析），不是时长。
_DURATION_ARITHMETIC_RE = re.compile(r'/\s*3600\b')
_DURATION_ARITHMETIC_ALLOW = (
    ('task_center.js', 'formatDuration'),
    ('map.js', '_parseCoordPart'),
)


def test_exactly_one_duration_implementation():
    found = [n for n, src in _js_sources().items()
             if re.search(r'function\s+formatDuration\s*\(', src)]
    assert found == ['task_center.js'], (
        f'formatDuration 的实现出现在 {found}，只许 task_center.js 一处')


def test_no_inline_duration_arithmetic_in_static_js():
    """改造前 map.js 有两处内联秒→小时：瓦片预估读数与大任务确认框。

    两处都读「1.5 小时」，而同一个 90 分钟在任务行读「1小时30分钟」。
    """
    found = _sites(_DURATION_ARITHMETIC_RE, allow=_DURATION_ARITHMETIC_ALLOW)
    assert not found, (
        f'还有内联的秒→小时换算：{found}；时长只许走 formatDuration')


def test_map_js_routes_both_durations_through_format_duration():
    """两处调用点都要真的接上，不是把读数删掉了事。"""
    src = _strip_js_comments(_js('map.js'))
    n = len(re.findall(r'formatDuration\(', src))
    assert n >= 2, (
        f'map.js 只有 {n} 处 formatDuration( 调用，应至少 2 处'
        '（瓦片预估读数 + 大任务确认框）')


def test_duration_i18n_keys_are_intact():
    """formatDuration 的五个 i18n 键是这套读法的全部，zh/en 都不许空。"""
    for key in DURATION_KEYS:
        msg = catalog.MESSAGES[key]
        assert msg['zh'].strip(), f'{key} 的 zh 是空的'
        assert msg['en'].strip(), f'{key} 的 en 是空的'


DURATION_CONSUMER_KEYS = [
    'js.map.tile_estimate.over',
    'js.map.download.confirm_large',
]


def test_duration_consumer_keys_take_a_formatted_duration():
    """两条句子模板收的是**成品时长**，不是一个小时数。

    占位符必须叫 `{duration}`：`{hours}` 这个名字会引诱下一个人再算一次小时。
    """
    for key in DURATION_CONSUMER_KEYS:
        msg = catalog.MESSAGES[key]
        for lang in ('zh', 'en'):
            val = msg[lang]
            assert '{duration}' in val, f'{key}[{lang}] 没有 {{duration}}：{val!r}'
            assert '{hours}' not in val, f'{key}[{lang}] 还留着 {{hours}}：{val!r}'


def test_duration_consumer_keys_do_not_repeat_the_unit_word():
    """句子里不许再写「小时」/「h」—— formatDuration 的返回值自带单位。

    留着就是「约 1小时30分钟 小时」。
    """
    for key in DURATION_CONSUMER_KEYS:
        msg = catalog.MESSAGES[key]
        assert '小时' not in msg['zh'], (
            f"{key}[zh] 在 {{duration}} 之外还写了「小时」：{msg['zh']!r}")
        assert not re.search(r'\{duration\}\s*h\b', msg['en']), (
            f"{key}[en] 在 {{duration}} 之后还写了 h：{msg['en']!r}")


def test_duration_consumer_keys_have_matching_placeholders():
    """zh 与 en 的占位符集合必须一致 —— 改占位符名最容易只改一边。"""
    for key in DURATION_CONSUMER_KEYS:
        msg = catalog.MESSAGES[key]
        zh = set(re.findall(r'\{(\w+)\}', msg['zh']))
        en = set(re.findall(r'\{(\w+)\}', msg['en']))
        assert zh == en, f'{key} 的占位符 zh={sorted(zh)} en={sorted(en)}'


# ---------------------------------------------------------------------------
# 7. node 行为级：同一个字节数，每条路径读出同一个数
# ---------------------------------------------------------------------------

# 改造前三份实现在这些输入上分家。102400 是计划点名的那个：
#   ui.js formatBytes -> '100.0 KB'   map.js _fmtBytes -> '100 KB'
_WITNESSES = [0, 1, 1023, 1024, 1536, 102400, 10485760, 104857600, 1073741824]


def _byte_formatter_harness(calls):
    """把 formatBytes 与 formatSpeed 的真源码抠出来交给 node。

    不 require 整个 ui.js / task_center.js：前者是 IIFE 且要 document，
    后者在**解析期**就自举（socket.io + Vue mount）。抠函数是这批 JS 契约
    测试的既有套路（test_map_js_contract.py 同法）。
    """
    ui = _strip_js_comments(_js('ui.js'))
    tc = _strip_js_comments(_js('task_center.js'))
    units = re.search(r"const\s+\w+\s*=\s*\[\s*'B'\s*,[^\]]*\];", ui)
    assert units, 'ui.js 里找不到单位表声明 —— 本测试已失效'
    return '\n'.join([
        'const window = {};',
        units.group(0),
        _js_function_source(ui, 'formatBytes'),
        'window.formatBytes = formatBytes;',
        _js_function_source(tc, 'formatSpeed'),
        f'console.log(JSON.stringify({calls}));',
    ])


@requires_node
def test_every_byte_path_formats_the_same_number_identically():
    """核心回归：一个字节数 → 一个读数。

    改造前 102400 B 在缓存卡读 `100.0 KB`、在磁盘预算判决读 `100 KB`。
    现在只有一份实现，所以「每条路径」在数值与单位上必然一致 —— 这条用
    node 真跑一遍把它变成可观测事实，而不是靠结构断言推论。
    """
    script = _byte_formatter_harness(
        '%s.map(function (n) { return {'
        ' n: n,'
        ' bytes: formatBytes(n),'
        ' viaWindow: window.formatBytes(n),'
        ' speed: formatSpeed(n) }; })' % json.dumps(_WITNESSES))
    rows = _node_json(script)

    for row in rows:
        assert row['bytes'] == row['viaWindow'], (
            f"{row['n']} B：直调与 window.formatBytes 读数不同 "
            f"({row['bytes']!r} vs {row['viaWindow']!r})")

    by_n = {row['n']: row for row in rows}
    assert by_n[102400]['bytes'] == '100.0 KiB', by_n[102400]
    assert by_n[1023]['bytes'] == '1023 B', by_n[1023]
    assert by_n[1024]['bytes'] == '1.0 KiB', by_n[1024]
    assert by_n[1536]['bytes'] == '1.5 KiB', by_n[1536]
    assert by_n[10485760]['bytes'] == '10.0 MiB', by_n[10485760]
    assert by_n[1073741824]['bytes'] == '1.0 GiB', by_n[1073741824]
    assert by_n[0]['bytes'] == '0 B', by_n[0]


@requires_node
def test_speed_rides_the_same_unit_ladder_as_size():
    """速度只在两点上与文件大小不同：`/s` 后缀、≥100 取整。其余必须逐字相同。"""
    script = _byte_formatter_harness(
        '%s.map(function (n) { return {'
        ' n: n, bytes: formatBytes(n), speed: formatSpeed(n) }; })'
        % json.dumps(_WITNESSES))
    rows = _node_json(script)

    for row in rows:
        if row['n'] <= 0:
            assert row['speed'] == '0 B/s', row
            continue
        assert row['speed'].endswith('/s'), row
        # 单位阶梯同一条：去掉 /s 之后单位词必须与文件大小一致。
        assert row['speed'][:-2].split(' ')[1] == row['bytes'].split(' ')[1], (
            f"{row['n']}：速度与大小落在不同单位上 {row!r}")

    by_n = {row['n']: row for row in rows}
    # < 100 时两条读数除后缀外逐字相同 —— 这才是「同一份实现」的可观测证据。
    assert by_n[1536]['speed'] == by_n[1536]['bytes'] + '/s' == '1.5 KiB/s'
    assert by_n[10485760]['speed'] == '10.0 MiB/s'
    # ≥100 才分家，且分家的是**取整**、不是单位或进制。
    assert by_n[102400]['speed'] == '100 KiB/s', by_n[102400]
    assert by_n[102400]['bytes'] == '100.0 KiB', by_n[102400]


def _duration_harness(calls, keys):
    """formatDuration 的真源码 + 从真 i18n 目录取的 zh 值。

    文案不在测试里手抄 —— 手抄的会在有人改文案时静默过期。
    """
    tc = _strip_js_comments(_js('task_center.js'))
    messages = {k: catalog.MESSAGES[k]['zh'] for k in keys}
    return '\n'.join([
        'const MSG = %s;' % json.dumps(messages, ensure_ascii=False),
        'function t(key, params) {',
        '  let s = MSG[key];',
        '  if (s === undefined) throw new Error("missing i18n key: " + key);',
        '  for (const k in (params || {})) s = s.split("{" + k + "}").join(params[k]);',
        '  return s;',
        '}',
        _js_function_source(tc, 'formatDuration'),
        f'console.log(JSON.stringify({calls}));',
    ])


@requires_node
def test_ninety_minutes_reads_the_same_everywhere():
    """90 分钟只有一种读法。

    改造前：下载面板「1.5 小时」，任务行「1小时30分钟」。
    """
    got = _node_json(_duration_harness(
        '[formatDuration(30), formatDuration(90), formatDuration(3600),'
        ' formatDuration(5400), formatDuration(12.3)]', DURATION_KEYS))
    assert got == ['30秒', '1分30秒', '1小时', '1小时30分钟', '12秒'], got


@requires_node
def test_tile_estimate_sentence_has_no_doubled_unit_word():
    """瓦片预估那句话渲染出来不许有「小时小时」这类重复单位。

    这是把内联时长换成 formatDuration 时最容易留下的伤：句子模板里那个
    「小时」原本是给 `{hours}` 配的量词。
    """
    calls = ('%s.map(function (key) { return t(key, '
             '{ count: "120,000", duration: formatDuration(12000) }); })'
             % json.dumps(DURATION_CONSUMER_KEYS))
    rendered = _node_json(_duration_harness(
        calls, DURATION_KEYS + DURATION_CONSUMER_KEYS))
    for line in rendered:
        assert '小时小时' not in line, line
        assert '分钟小时' not in line, line
        assert '3小时20分钟' in line, (
            f'时长没有原样出现在句子里（占位符名对不上？）：{line!r}')
        assert '{' not in line and '}' not in line, (
            f'句子里还有没填上的占位符：{line!r}')
