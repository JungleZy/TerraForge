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
