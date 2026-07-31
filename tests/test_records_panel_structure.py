"""统一任务表的结构测试（模板文本级 + Flask 渲染级）。

2026-07 改版：首页记录面板的「进行中」卡片区并入任务列表表格——
一张表、两个 tbody：#activeTasksBody（活动任务实时行，tasks.js 渲染）在上，
#historyTableBody（历史行，分页）在下。独立页 /history 复用同一个
partial 但**不**渲染 #activeTasksBody（它不加载 tasks.js），布局顺序也不变
（统计卡 → 历史区域地图 → 任务列表；首页是统计卡 → 任务列表 → 历史区域地图）。

history.js 的去重逻辑（有 #activeTasksBody 才跳过非终态行）依赖这个
渲染差异，所以这里用 test_client 真实渲染两页来钉住，而不只查模板源码。
"""

import importlib
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_app(monkeypatch, tmp_path):
    """与 tests/test_index_has_contour_option.py 同一个套路：Config 副作用
    全部重定向到 tmp_path，再新鲜 import app。"""
    from core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "core.database", "services.contour_task_manager"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_index_records_panel_has_active_tbody_above_history_tbody(monkeypatch, tmp_path):
    """首页记录面板：双 tbody 都在，且活动行区在历史行区之上。"""
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'id="activeTasksBody"' in html, '首页缺 #activeTasksBody（实时行区）'
    assert 'id="historyTableBody"' in html, '首页缺 #historyTableBody'
    assert html.index('id="activeTasksBody"') < html.index('id="historyTableBody"'), (
        '活动任务实时行区必须在历史行区之上'
    )


def test_index_records_panel_no_longer_has_the_active_tasks_card(monkeypatch, tmp_path):
    """旧的「进行中」卡片区（#activeTasks 容器）必须整体消失。"""
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'id="activeTasks"' not in html, (
        '旧的 #activeTasks 卡片区还在——活动任务会出现两份（卡片 + 表格行）'
    )
    # 面板内容顺序：统计卡 → 任务列表 → 历史区域地图（列表是主体，上移）。
    assert html.index('id="statsRow"') < html.index('id="activeTasksBody"'), (
        '统计卡必须在任务表之前'
    )
    assert html.index('id="historyTableBody"') < html.index('id="historyMap"'), (
        '首页记录面板里任务列表必须在历史区域地图之前'
    )


def test_history_page_has_no_active_tbody_and_keeps_its_layout(monkeypatch, tmp_path):
    """独立页 /history：没有实时行区，布局顺序不变（地图在列表之前）。

    这是 history.js 去重逻辑的前提：它靠「文档里有没有 #activeTasksBody」
    判断要不要跳过非终态行。/history 若渲染出这个空 tbody，去重就会
    在独立页上误伤——非终态任务从列表里消失而页面顶部并没有实时行。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/history").get_data(as_text=True)
    assert 'id="activeTasksBody"' not in html, (
        '独立页 /history 不应渲染 #activeTasksBody（它不加载 tasks.js，'
        '渲染出来也永远是空 tbody，还会触发 history.js 的误去重）'
    )
    assert 'id="historyTableBody"' in html
    assert html.index('id="historyMap"') < html.index('id="historyTableBody"'), (
        '独立页 /history 的布局不能变：历史区域地图在任务列表之前'
    )


def test_active_tbody_is_behind_the_records_panel_flag():
    """模板源码级：#activeTasksBody 必须包在 records_panel 条件块里。

    渲染级断言（上面三条）守的是结果，这条守的是机制：如果有人把条件块
    拆掉、让 tbody 无条件渲染，/history 的渲染级断言会红——但失败信息
    指向的是「结果错了」，这条让「机制被拆了」单独可见。
    """
    path = os.path.join(ROOT, 'templates', '_history_content.html')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert 'id="activeTasksBody"' in src, 'partial 里没有 #activeTasksBody——本测试已失效'
    # 条件块与 tbody 必须在同一对 {% if %}...{% endif %} 内
    m = re.search(
        r'\{%-?\s*if\s+records_panel[^%]*%\}(.*?)\{%-?\s*endif\s*%\}', src, re.S)
    assert m and 'id="activeTasksBody"' in m.group(1), (
        '#activeTasksBody 不在 records_panel 条件块里——独立页 /history 也会渲染它'
    )
