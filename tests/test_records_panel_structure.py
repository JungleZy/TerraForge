"""单一时间流的结构测试（模板文本级 + Flask 渲染级）。

2026-08 定稿（用户明确不要「活动/失败/历史」三个分区）：记录面板的
任务列表是**按创建时间倒序的单一时间流 + 顶部状态筛选**——

    统计卡 → 任务列表卡（筛选行：搜索框 + 状态 chips（五枚）→
        #historyTableBody（单一时间流，分页；活动任务也在流里））→ 历史区域地图

无分组头、无失败组折叠、无 #activeTasksBody 实时区、无 activeTasks 去重
——四表 UNION 里每个任务只有一行，天然只出现一次。

records_panel（index.html 在 include 前 set）现在只决定一件事：
  卡片顺序。首页记录面板列表是主体，统计卡 → 任务列表 → 历史区域地图；
  独立页 /history 布局不动，统计卡 → 历史区域地图 → 任务列表。
（它原先还控制 #activeTasksBody 实时区的有无，那个容器随三分区删除。）

登记：本文件历经两轮重写——
  1. 「双 tbody 统一任务表」结构断言 → 2026-08 统一流式列表（废 9 列表格，
     锚点 #activeTasksBody/#historyTableBody 从 <tbody> 变 <div>）；
  2. 本轮：活动/失败/历史三分区 → 单一时间流。#activeTasksBody 与
     task-group-header 的断言从「必须存在/按条件存在」翻面为「必须不存在」，
     chips 从四枚（全部/已完成/失败/已取消）变五枚（新增「进行中」= active）。
history.js 的单一时间流语义依赖这个渲染结果，所以用 test_client 真实渲染
两页来钉住，而不只查模板源码。
"""

import importlib
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 五枚状态筛选 chip 的取值（顺序即模板里的顺序）：
#   全部 / 进行中 / 失败 / 已完成 / 已取消。
# 'active' 是特殊值，后端 /api/history_all 把它展开成
# status IN ('pending','running','paused')——活动任务在时间流里，
# 靠这枚 chip 单独滤出来（不再有独立的活动分区）。
EXPECTED_CHIP_STATUSES = ('', 'active', 'failed', 'completed', 'cancelled')


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


def test_both_pages_have_a_single_stream_and_no_sections(monkeypatch, tmp_path):
    """两个页面都只有 #historyTableBody 一个列表容器，没有三分区的任何遗迹。

    单一时间流的核心结构断言：
      - #historyTableBody 必须在（稳定挂钩 id，history.js 渲染进这里）；
      - #activeTasksBody 必须不在（实时区随分区删除——首页也不再渲染它，
        tasks.js 不再承担列表渲染，只做 socket 实时更新）；
      - task-group-header 必须不在（无「活动 (N)」「失败 (N)」「历史」分组头）。
    """
    client = _load_app(monkeypatch, tmp_path)
    for page in ('/', '/history'):
        html = client.get(page).get_data(as_text=True)
        assert 'id="historyTableBody"' in html, f'{page} 缺 #historyTableBody（单一时间流）'
        assert 'id="activeTasksBody"' not in html, (
            f'{page} 还有 #activeTasksBody——活动实时区已随「活动/失败/历史」'
            '三分区删除，活动任务就在时间流里'
        )
        assert 'task-group-header' not in html, (
            f'{page} 还有分组头——单一时间流没有「活动 (N)」「失败 (N)」「历史」分区'
        )


def test_index_records_panel_has_no_task_table(monkeypatch, tmp_path):
    """9 列 .task-table 与 9 列表头必须整体消失（上一轮重设计的核心动作，沿用）。

    「太乱」根因之一：活动三行富行 + 历史 9 列网格行 + 与富行不对应的
    9 列表头三种形态硬拼。定稿设计是废掉表格、全状态共用同一种行结构。
    """
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'task-table' not in html, '9 列 .task-table 还在——废表格是重设计的核心'
    assert '<thead' not in html, '9 列表头（ID/名称/状态/区域/…）还在'
    assert 'id="activeTasks"' not in html, (
        '更早期的 #activeTasks 卡片区还在——活动任务会出现两份'
    )


def test_status_filter_chips_render_on_both_pages(monkeypatch, tmp_path):
    """状态筛选 chips（全部/进行中/失败/已完成/已取消，五枚）两个页面都要有。

    chips 作用于整个时间流（history.js 把取值透传给 /api/history_all
    的 ?status= 参数）。「进行中」是本轮新增：活动任务进了时间流之后，
    原来「活动分区」的查看入口由这枚 chip 接替（data-status="active"）。
    """
    client = _load_app(monkeypatch, tmp_path)
    for page in ('/', '/history'):
        html = client.get(page).get_data(as_text=True)
        assert 'id="statusChips"' in html, f'{page} 缺状态筛选 chips 容器'
        assert 'id="searchInput"' in html, f'{page} 缺搜索框'
        for status in EXPECTED_CHIP_STATUSES:
            assert f'data-status="{status}"' in html, (
                f'{page} 的 chips 缺 data-status="{status}"（五枚：'
                '全部/进行中/失败/已完成/已取消）'
            )


def test_card_order_differs_between_pages_via_records_panel(monkeypatch, tmp_path):
    """records_panel 机制：卡片顺序在两页相反——首页列表在前，独立页地图在前。

    这是 records_panel 变量现在唯一控制的东西。首页记录面板里列表是主体
    （统计卡 → 任务列表 → 历史区域地图）；独立页 /history 布局不动
    （统计卡 → 历史区域地图 → 任务列表）。
    """
    client = _load_app(monkeypatch, tmp_path)

    index_html = client.get("/").get_data(as_text=True)
    assert index_html.index('id="statsRow"') < index_html.index('id="historyTableBody"'), (
        '首页：统计卡必须在任务列表之前'
    )
    assert index_html.index('id="historyTableBody"') < index_html.index('id="historyMap"'), (
        '首页记录面板里任务列表必须在历史区域地图之前'
    )

    history_html = client.get("/history").get_data(as_text=True)
    assert history_html.index('id="statsRow"') < history_html.index('id="historyMap"'), (
        '独立页 /history：统计卡必须在历史区域地图之前'
    )
    assert history_html.index('id="historyMap"') < history_html.index('id="historyTableBody"'), (
        '独立页 /history 的布局不能变：历史区域地图在任务列表之前'
    )


def test_task_detail_modal_is_not_trapped_inside_workbench_panel(monkeypatch, tmp_path):
    """#taskDetailModal 不能是 .workbench-panel 的后代（2026-08 遮罩事故）。

    .workbench-panel 永远带非 none 的 transform（滑入 translateX(0)，
    滑出 translateX(100%)），transform 让它成为 fixed 后代的包含块
    并自建层叠上下文（z-index 1401）。Bootstrap 的 .modal-backdrop
    挂在 <body> 下、z-index 1450 > 1401 —— 弹窗若嵌在面板里，
    遮罩会整个盖住详情弹窗（headless Chrome 实测：elementFromPoint
    命中 modal-backdrop，弹窗被压暗且点不动），且 position:fixed
    相对面板布局，1193px 高的对话框被裁出 761px 视口。

    修法：弹窗标记放 base.html 的 <body> 直下（Bootstrap 官方推荐位），
    两个页面（首页记录面板 / 独立页 /history）共享一份。
    """
    client = _load_app(monkeypatch, tmp_path)
    for page in ('/', '/history'):
        html = client.get(page).get_data(as_text=True)
        assert html.count('id="taskDetailModal"') == 1, (
            f'{page} 应恰好有一个 #taskDetailModal'
        )
        # section 不嵌套，id="historyPanel" 之后第一个 </section> 即其收尾
        panel_start = html.index('id="historyPanel"') if page == '/' else None
        if panel_start is not None:
            panel_end = html.index('</section>', panel_start)
            modal_pos = html.index('id="taskDetailModal"')
            assert not (panel_start < modal_pos < panel_end), (
                '#taskDetailModal 又嵌回 .workbench-panel 里了——transform '
                '包含块 + 层叠上下文会让 body 级遮罩盖住弹窗（见本测试 docstring）'
            )


def test_records_panel_flag_only_controls_card_order():
    """模板源码级：records_panel 条件块只包裹卡片顺序分支，不再包任何列表容器。

    渲染级断言（上面几条）守的是结果，这条守的是机制：records_panel 在
    _history_content.html 里唯一的用处是 task_list_card / history_map_card
    的顺序分支。如果有人把 #activeTasksBody 之类的实时区容器加回条件块，
    上面的渲染级断言会红；如果有人把条件块整个拆掉，本条红——
    「机制被拆了」与「结果错了」分开可见。
    """
    path = os.path.join(ROOT, 'templates', '_history_content.html')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert 'id="activeTasksBody"' not in src, (
        'partial 里又出现了 #activeTasksBody——实时区已随三分区删除'
    )
    # 卡片顺序分支：if/else 两个分支分别包含两张卡的宏调用，顺序相反
    m = re.search(
        r'\{%-?\s*if\s+records_panel[^%]*%\}(.*?)\{%-?\s*else\s*%\}(.*?)\{%-?\s*endif\s*%\}',
        src, re.S)
    assert m, 'partial 里找不到 records_panel 的 if/else 卡片顺序分支——本测试已失效'
    assert 'task_list_card()' in m.group(1) and 'history_map_card()' in m.group(1), (
        'records_panel 为真的分支（首页）应渲染任务列表与历史区域地图两张卡'
    )
    assert m.group(1).index('task_list_card()') < m.group(1).index('history_map_card()'), (
        '首页分支：任务列表卡必须在历史区域地图卡之前'
    )
    assert m.group(2).index('history_map_card()') < m.group(2).index('task_list_card()'), (
        '独立页分支：历史区域地图卡必须在任务列表卡之前'
    )
