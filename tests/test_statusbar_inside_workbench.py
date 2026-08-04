"""底部状态栏必须留在 .workbench 外壳内（渲染级结构断言）。

事故登记（2026-08，用户报告「底部状态工具栏消失了」）：
  _history_content.html 在「废三区」重排时丢掉了外层容器的开标签，
  留下一颗多余的 </div>。HTML5 解析器对不匹配 </div> 的恢复策略是
  「一直弹栈到匹配元素」，于是这颗 </div> 把 .workbench-panel__body、
  section、main、乃至 .workbench 全部提前关闭——footer.workbench-statusbar
  掉出 .workbench，成为 body 的最后一个子元素，排在 100vh 高的
  .workbench 之后（y = 视口高），被 body{overflow:hidden} 裁出视口。
  首页和 /history 两页共用这份 partial，所以两页的状态栏一起消失。

浏览器 DOM 里 footer 必须是 .workbench 的直接子元素（base.html 的结构），
这个测试用 html.parser 按 HTML5 的弹栈语义模拟解析渲染结果，
断言 footer 的父链里有 .workbench——多余/缺失闭合标签会让它立即翻红。
"""

import importlib
import os
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 参与配对追踪的标签（多余/缺失这些标签的闭合才会撼动外壳结构）
_STRUCT_TAGS = {"div", "main", "footer", "section", "header", "form", "nav", "ul", "li"}


def _load_app(monkeypatch, tmp_path):
    """与 tests/test_records_panel_structure.py 同一个套路。"""
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


class _WorkbenchTree(HTMLParser):
    """按 HTML5 弹栈语义维护开放元素栈，记录 footer.workbench-statusbar
    出现时它的祖先链里有没有 .workbench。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # [(tag, class_attr)]
        self.footer_ancestors = None

    def handle_starttag(self, tag, attrs):
        if tag not in _STRUCT_TAGS:
            return
        cls = dict(attrs).get("class", "")
        if tag == "footer" and "workbench-statusbar" in cls.split():
            self.footer_ancestors = list(self.stack)
        self.stack.append((tag, cls))

    def handle_endtag(self, tag):
        if tag not in _STRUCT_TAGS:
            return
        names = [t for t, _ in self.stack]
        if tag in names:
            # HTML5：不匹配的闭合标签会隐式关闭其间所有元素
            idx = len(names) - 1 - names[::-1].index(tag)
            del self.stack[idx:]
        # 栈里根本没有该标签 → 浏览器直接忽略这颗闭合标签

    def footer_is_inside_workbench(self):
        if self.footer_ancestors is None:
            return False
        return any(
            tag == "div" and "workbench" in cls.split()
            for tag, cls in self.footer_ancestors
        )


def _assert_statusbar_inside_workbench(html, page):
    parser = _WorkbenchTree()
    parser.feed(html)
    assert parser.footer_ancestors is not None, (
        f"{page} 渲染结果里没有 footer.workbench-statusbar"
    )
    assert parser.footer_is_inside_workbench(), (
        f"{page} 的 footer.workbench-statusbar 掉出了 .workbench——"
        "多半是某个模板多了/少了闭合标签（见本文件 docstring 的事故登记）"
    )


def test_statusbar_stays_inside_workbench_on_index(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    _assert_statusbar_inside_workbench(resp.get_data(as_text=True), "首页 /")


def test_statusbar_stays_inside_workbench_on_history(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/history")
    assert resp.status_code == 200
    _assert_statusbar_inside_workbench(resp.get_data(as_text=True), "历史页 /history")


def test_statusbar_stays_inside_workbench_on_config(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    _assert_statusbar_inside_workbench(resp.get_data(as_text=True), "配置页 /config")
