"""统一流式列表的结构测试（模板文本级 + Flask 渲染级）。

2026-08 重设计定稿：记录面板的任务列表**整体重做**为统一流式列表——
废掉 9 列 .task-table 与表头（「太乱」三条根因之一：活动三行富行 +
历史 9 列网格行 + 不对应的 9 列表头，两种行语言硬拼），活动/历史任务
共用同一种行结构。面板结构变为：

    统计卡 → 任务列表卡（筛选行：搜索框 + 状态 chips →
        #activeTasksBody（活动/失败分组，tasks.js 渲染）→
        「历史」分组头 → #historyTableBody（历史流，分页））→ 历史区域地图

records_panel（index.html 在 include 前 set）仍决定两件事：
  1. 是否渲染 #activeTasksBody——独立页 /history 不加载 tasks.js，不能有它；
     history.js 也靠「文档里有没有 #activeTasksBody」判断要不要按
     activeTasks 精确去重（实时区里显示着的任务，历史流不重复显示）。
  2. 卡片顺序：首页记录面板列表是主体，统计卡 → 任务列表 → 历史区域地图；
     独立页 /history 布局不动，统计卡 → 历史区域地图 → 任务列表。

history.js 的去重逻辑依赖这个渲染差异，所以这里用 test_client 真实渲染
两页来钉住，而不只查模板源码。

登记：本文件前身是「双 tbody 统一任务表」的结构断言（test_index_records_
panel_has_active_tbody_above_history_tbody 等四条），2026-08 重设计后整体
重写为列表结构断言——锚点 #activeTasksBody / #historyTableBody 保留
（id 名留作稳定挂钩，元素已从 <tbody> 变为 <div>），「9 列表格」相关
断言翻面为「表格必须不存在」。
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


def test_index_records_panel_has_active_list_above_history_list(monkeypatch, tmp_path):
    """首页记录面板：两个列表容器都在，且活动实时区在历史流之上。"""
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'id="activeTasksBody"' in html, '首页缺 #activeTasksBody（活动/失败分组实时区）'
    assert 'id="historyTableBody"' in html, '首页缺 #historyTableBody（历史流）'
    assert html.index('id="activeTasksBody"') < html.index('id="historyTableBody"'), (
        '活动任务实时区必须在历史流之上'
    )


def test_index_records_panel_has_no_task_table(monkeypatch, tmp_path):
    """9 列 .task-table 与 9 列表头必须整体消失（重设计的核心动作）。

    「太乱」根因之一：活动三行富行 + 历史 9 列网格行 + 与富行不对应的
    9 列表头三种形态硬拼。定稿设计是废掉表格、活动/历史共用同一种行结构。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'task-table' not in html, '9 列 .task-table 还在——废表格是本次重设计的核心'
    assert '<thead' not in html, '9 列表头（ID/名称/状态/区域/…）还在'
    assert 'id="activeTasks"' not in html, (
        '更早期的 #activeTasks 卡片区还在——活动任务会出现两份'
    )
    # 面板内容顺序：统计卡 → 任务列表 → 历史区域地图（列表是主体，上移）。
    assert html.index('id="statsRow"') < html.index('id="activeTasksBody"'), (
        '统计卡必须在任务列表之前'
    )
    assert html.index('id="historyTableBody"') < html.index('id="historyMap"'), (
        '首页记录面板里任务列表必须在历史区域地图之前'
    )


def test_status_filter_chips_render_on_both_pages(monkeypatch, tmp_path):
    """状态筛选 chips（全部/已完成/失败/已取消）两个页面都要有。

    chips 只作用于历史流（history.js 把取值透传给 /api/history_all
    的 ?status= 参数）。四个取值与后端 TaskStatus 的终态一一对应，
    没有「活动」chip——活动任务在上方的实时区，不在历史流里。
    """
    client = _load_app(monkeypatch, tmp_path)
    for page in ('/', '/history'):
        html = client.get(page).get_data(as_text=True)
        assert 'id="statusChips"' in html, f'{page} 缺状态筛选 chips 容器'
        assert 'id="searchInput"' in html, f'{page} 缺搜索框'
        for status in ('', 'completed', 'failed', 'cancelled'):
            assert f'data-status="{status}"' in html, (
                f'{page} 的 chips 缺 data-status="{status}"'
            )


def test_history_page_has_no_active_list_and_keeps_its_layout(monkeypatch, tmp_path):
    """独立页 /history：没有实时区、没有活动/失败分组头，布局顺序不变
    （地图在列表之前）。

    这是 history.js 精确去重的前提：它靠「文档里有没有 #activeTasksBody」
    判断要不要排除 activeTasks 里的任务。/history 若渲染出这个容器，
    去重就会在独立页上误伤——实时区明明不存在，任务却被当成「已在上面
    显示着」而从历史流里消失。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/history").get_data(as_text=True)
    assert 'id="activeTasksBody"' not in html, (
        '独立页 /history 不应渲染 #activeTasksBody（它不加载 tasks.js，'
        '渲染出来也永远是空容器，还会触发 history.js 的误去重）'
    )
    assert 'id="historyTableBody"' in html
    assert html.index('id="historyMap"') < html.index('id="historyTableBody"'), (
        '独立页 /history 的布局不能变：历史区域地图在任务列表之前'
    )
    # 「历史」分组头只在首页记录面板出现（区分实时区与历史流）；
    # 独立页整页都是历史，不需要它。
    assert 'task-group-header' not in html, (
        '独立页 /history 不应有分组头（设计：无活动/失败分组、无「历史」分组头）'
    )


def test_active_list_is_behind_the_records_panel_flag():
    """模板源码级：#activeTasksBody 必须包在 records_panel 条件块里。

    渲染级断言（上面几条）守的是结果，这条守的是机制：如果有人把条件块
    拆掉、让容器无条件渲染，/history 的渲染级断言会红——但失败信息
    指向的是「结果错了」，这条让「机制被拆了」单独可见。
    """
    path = os.path.join(ROOT, 'templates', '_history_content.html')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert 'id="activeTasksBody"' in src, 'partial 里没有 #activeTasksBody——本测试已失效'
    # 条件块与容器必须在同一对 {% if %}...{% endif %} 内
    m = re.search(
        r'\{%-?\s*if\s+records_panel[^%]*%\}(.*?)\{%-?\s*endif\s*%\}', src, re.S)
    assert m and 'id="activeTasksBody"' in m.group(1), (
        '#activeTasksBody 不在 records_panel 条件块里——独立页 /history 也会渲染它'
    )
