"""配置页的表单必须能提交 —— 守 HTML5 约束校验这条静默失败路径。

真实故障：`map_center_lat` 写了 `step="0.1"`，而出厂默认值是 `29.56`。
HTML5 约束校验判这个值非法 → 整个 <form> 变成 :invalid → 点「保存」按钮
（type="submit" form="configForm"）**submit 事件根本不触发**。表现是配置页
完全存不进去，而且没有任何报错：浏览器只在那个可能滚出视口的纬度输入框上
弹一个原生气泡。改任何一项配置都存不了，不只是坐标。

这类 bug 用眼睛审查抓不到，也不会被任何后端断言看见（请求压根没发出去）。
所以这里不是钉死那两个字段，而是扫描**渲染后**的配置页：每一个带约束的
数字输入框，它自己渲染出来的默认值都必须过得了那条约束。
"""
import decimal
import importlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.config import Config  # noqa: E402

_INPUT_RE = re.compile(r'<input\b[^>]*type="number"[^>]*>', re.I | re.S)


@pytest.fixture
def client(monkeypatch, tmp_path):
    # 与 test_tile_url_config._load_app 同一路数：把数据目录指到 tmp_path，
    # 否则 app_factory 的单实例锁会撞上开发机上正在跑的实例。
    monkeypatch.setattr(Config, 'DATABASE_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    monkeypatch.setattr(Config, 'CACHE_DIR', tmp_path / 'cache')
    for mod in ('app', 'src.core.database'):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module('app')
    app_mod.app.config['TESTING'] = True
    return app_mod.app.test_client()


def _attr(tag, name):
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def _violations(html):
    """返回 [(id, value, 违反的约束)]，模拟浏览器的约束校验。"""
    bad = []
    for tag in _INPUT_RE.findall(html):
        el_id = _attr(tag, 'id') or '(no id)'
        raw = _attr(tag, 'value')
        if raw is None or raw == '':
            continue
        try:
            value = decimal.Decimal(raw)
        except decimal.InvalidOperation:
            bad.append((el_id, raw, 'value 不是合法数字'))
            continue

        for bound, cmp_fail, label in (('min', lambda v, b: v < b, 'min'),
                                       ('max', lambda v, b: v > b, 'max')):
            got = _attr(tag, bound)
            if got is not None and cmp_fail(value, decimal.Decimal(got)):
                bad.append((el_id, raw, f'{label}={got}'))

        step = _attr(tag, 'step')
        if step is None:
            step = '1'          # HTML 默认 step 就是 1（整数）
        if step.lower() == 'any':
            continue
        base = decimal.Decimal(_attr(tag, 'min') or '0')
        rem = (value - base) % decimal.Decimal(step)
        if rem != 0:
            bad.append((el_id, raw, f'step={step}（默认值不是它的整数倍）'))
    return bad


@pytest.mark.parametrize('path', ['/', '/config'])
def test_config_number_inputs_pass_their_own_constraints(client, path):
    html = client.get(path).get_data(as_text=True)
    bad = _violations(html)
    assert not bad, (
        '配置页存在自相矛盾的数字输入框 —— 渲染出的默认值过不了它自己声明的约束。\n'
        '后果不是这个字段红一下，而是**整个表单 :invalid，点「保存」submit 事件'
        '不触发，所有配置都存不进去**（浏览器只弹一个原生气泡，无控制台报错）。\n'
        + '\n'.join(f'  #{i}: value="{v}" 违反 {why}' for i, v, why in bad)
    )


# 配置渲染进 min/max 输入框的键里，有一部分**写入侧不校验**（登记在
# config_manager._UNCONSTRAINED_KEYS）：PUT /api/config 收得下
# terrain_local_maxzoom=99，而仓库自己的 tests/test_local_terrain_api.py 的
# test_out_of_range_maxzoom_config_falls_back
# 把「越界配置软退回 14 继续跑」当成受支持状态。这类值照直渲染进 value=""，
# 就把上面那条出厂配置扫不到的路径炸开了：受害的不是那个字段，而是它所在的
# **整张表单** —— #taskForm 变 :invalid，原生校验拦下 submit 事件，
# static/js/map.js 上挂在 #taskForm 的 submit 监听根本不触发。
#
# ⚠️ 2026-08-15 Task 5：爆炸半径**变大了**，不是只换了个 id。
# 改前首页是两张表单（#downloadForm 瓦片/高程、#processForm 地形切片/等高线），
# 越界的 terrain_local_maxzoom 只污染 #processForm，卡住的是那**两条**加工管线；
# 现在四条管线合并成**一张** #taskForm，同一个非法控件卡住的是**全部四条**
# （瓦片 / 高程 / 本地地形切片 / 等高线），首页整个建不了任何任务。
# 而 #localTerrainOptions 只用 hidden 属性隐藏、字段不 disable，非法控件仍参与
# 校验且不可聚焦：气泡弹不出来，连与地形无关的等高线任务也一起建不了
# （显隐现在只有一个消费者：map.js 的 applyPipeline()，即
# initDownloadTypeToggle + initProcessTypeToggle 合并后的那一个 —— 它只翻
# `hidden`，一个 required 都不摘。合并版把 required 那条腿改成了结构性的：
# 整张 #taskForm 只剩 #taskName 一个 required，而它对四条管线都可见，
# 所以「藏起来的 required 拦下 submit」这一形态不会再发生；本文件守的是
# 另一条腿 —— **可见**控件的 min/max/step 与渲染值自相矛盾）。
# 服务端因此有**两道**钳位站在配置与表单之间，模板记不了日志、被丢掉的值必须
# 在服务端留一条 warning：
#   · main._terrain_form_defaults()  —— terrain_local_maxzoom，那条日志由
#     tests/test_terrain_lighting_frontend.py 钉住；
#   · main._contour_form_default()   —— 2026-08-15 新增。#contourInterval 改成
#     由服务端按 contour_default_interval 渲染（改前模板写死 value="50" +
#     提交端 `|| 50` 兜底，配置页那个旋钮是假的），闸门与 warning 一并补上。
# 本表只管一件事：不管库里存的是什么，渲染出来的 value 都得过控件自己的约束。
_OUT_OF_RANGE_CASES = [
    ('99', '14'),     # 越上界：min/max 是 0-21
    ('-3', '14'),     # 越下界
    ('abc', '14'),    # 非数字
    ('16.5', '14'),   # 小数：层级是整数，半级切不出来（与 validate_zoom 同一口径）
    ('', '14'),       # 空串（键存在但值被清空）
    ('0', '0'),       # 合法边界值必须原样透出，别被兜底吃掉
    ('16', '16'),     # 合法值必须跟着配置走，否则这个控件又成了假旋钮
]


@pytest.mark.parametrize('raw,expected', _OUT_OF_RANGE_CASES)
def test_unvalidated_config_cannot_make_the_page_unsubmittable(client, monkeypatch,
                                                               raw, expected):
    """写入侧不校验的配置值，渲染出来也必须过得了控件自己的 min/max。"""
    from src.routes import main as main_route
    # stub 必须**叠在真实 get_all() 之上**，不能整个替成单键 dict：那样页面上
    # 其余由配置驱动的数字框全渲染成 value=""，被 _violations 的空值分支跳过，
    # 下面那句「将来新增的配置驱动数字框也会在这里红」就成了假话。
    real = main_route.config_manager.get_all()
    monkeypatch.setattr(main_route.config_manager, 'get_all',
                        lambda: {**real, 'terrain_local_maxzoom': {'value': raw}})

    html = client.get('/').get_data(as_text=True)
    tag = next(t for t in _INPUT_RE.findall(html)
               if _attr(t, 'id') == 'localTerrainMaxzoom')
    assert _attr(tag, 'value') == expected, (
        f'terrain_local_maxzoom={raw!r} 渲染成 {_attr(tag, "value")!r}，'
        f'应为 {expected!r}')
    # 顺带整页复扫：这一侧不针对某个 id，将来谁再往页面上加一个由配置驱动的
    # 数字输入框，它越界时也会在这里红。
    bad = _violations(html)
    assert not bad, (
        f'terrain_local_maxzoom={raw!r} 时页面上出现了自相矛盾的数字输入框 —— '
        f'整张表单 :invalid，创建按钮点了没反应：\n'
        + '\n'.join(f'  #{i}: value="{v}" 违反 {why}' for i, v, why in bad))


# #contourInterval 的初值（2026-08-15 Task 5 新增的第二道钳位）。
# 与上面那条同一类缺陷、不同一个字段：控件自己写着 `min="1" step="1"`，而初值
# 改成了由服务端按 contour_default_interval 渲染（改前是模板里写死的
# `value="50"` —— 写死值天然合法，所以这条路径以前压根不存在，它是本轮
# 「把假旋钮改成真旋钮」顺带开出来的）。渲染出一个控件自己收不下的数，
# 卡死的是**整张 #taskForm**，四条管线一起废。
#
# 闸门与控件约束逐字对齐：min="1" 同时是 step 的基点，合法集就是 1, 2, 3, …
# 所以 0.5 / 50.5 这种「正数但控件收不下」的值也必须退回出厂默认。
# 这两格是实测出来的：本轮初版闸门写的是 `value > 0`，两个值都漏了过去。
_CONTOUR_INTERVAL_CASES = [
    ('', '50'),        # 键存在但值被清空
    ('abc', '50'),     # 非数字
    ('-3', '50'),      # 负数
    ('0', '50'),       # 0：控件 min="1" 收不下
    ('0.5', '50'),     # 正数但小于 min
    ('50.5', '50'),    # 正数且够大，但不是 step="1" 的整数倍
    ('inf', '50'),     # 无穷：float() 认，控件不认
    ('nan', '50'),     # NaN：同上
    ('1e400', '50'),   # 溢出成 inf 的字面量
    ('1', '1'),        # 合法边界值必须原样透出，别被兜底吃掉
    ('25', '25'),      # 合法值必须跟着配置走，否则这个控件又成了假旋钮
    ('  25 ', '25'),   # 两侧空白要去掉：value=" 25 " 浏览器判非法数字
]


@pytest.mark.parametrize('raw,expected', _CONTOUR_INTERVAL_CASES)
def test_contour_interval_renders_a_value_its_own_min_accepts(client, monkeypatch,
                                                              raw, expected):
    """库里存什么，#contourInterval 渲染出来都得过它自己的 min="1"/step="1"。

    正常写入路径到不了这里（config_manager._is_valid_contour_interval 要求
    >= contour_task_manager._MIN_CONTOUR_INTERVAL = 1.0）；兜的是有人用
    sqlite3 直接改库 —— main._contour_form_default() 的 docstring 自己点名的
    那条路。模板是这条链路上唯一记不了日志的一环，所以钳位必须在服务端，
    被丢掉的脏值当场留一条 warning。
    """
    from src.routes import main as main_route
    # 同上：stub 叠在真实 get_all() 之上，否则整页复扫会退化成「全是空 value」，
    # 下面那句「将来新增的配置驱动数字框也会在这里红」就成了假话。
    real = main_route.config_manager.get_all()
    monkeypatch.setattr(main_route.config_manager, 'get_all',
                        lambda: {**real, 'contour_default_interval': {'value': raw}})

    html = client.get('/').get_data(as_text=True)
    tag = next(t for t in _INPUT_RE.findall(html)
               if _attr(t, 'id') == 'contourInterval')
    assert _attr(tag, 'value') == expected, (
        f'contour_default_interval={raw!r} 渲染成 {_attr(tag, "value")!r}，'
        f'应为 {expected!r}')
    bad = _violations(html)
    assert not bad, (
        f'contour_default_interval={raw!r} 时页面上出现了自相矛盾的数字输入框 —— '
        f'整张 #taskForm :invalid，四条管线的创建按钮全都点了没反应：\n'
        + '\n'.join(f'  #{i}: value="{v}" 违反 {why}' for i, v, why in bad))


def test_coordinate_inputs_use_step_any(client):
    """坐标是任意精度的量，不该被 step 量化。

    这条比上一条更具体：就算有人把默认值改成 29.6 让 step="0.1" 恰好通过，
    用户手输一个 29.56 照样会把整张表单卡死。范围校验由后端
    ConfigManager._is_valid_lat/_is_valid_lng 负责。
    """
    html = client.get('/config').get_data(as_text=True)
    for el_id in ('map_center_lat', 'map_center_lng'):
        tag = next(t for t in _INPUT_RE.findall(html) if _attr(t, 'id') == el_id)
        assert (_attr(tag, 'step') or '').lower() == 'any', (
            f'#{el_id} 必须 step="any"：坐标要能填任意小数位'
        )


def test_save_button_is_wired_to_the_form():
    """保存按钮在 <form> 外面，靠 form="configForm" 关联 —— 关联断了就点不动。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'templates', '_config_content.html'), encoding='utf-8') as f:
        src = f.read()
    assert 'id="configForm"' in src
    assert re.search(r'<button[^>]*type="submit"[^>]*form="configForm"', src), (
        '底部操作条的保存按钮必须带 form="configForm"（它在 <form> 之外）'
    )
