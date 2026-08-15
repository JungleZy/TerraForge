import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    for mod in ("app", "src.core.database"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_index_has_contour_controls(monkeypatch, tmp_path):
    client = _load_app(monkeypatch, tmp_path)
    html = client.get("/").get_data(as_text=True)
    # 2026-08-15：`value="contour"` 指的是 #processType 下拉里那个
    # `<option value="contour">`。两个弹窗合并成 #createPanel 之后，处理类型
    # 下拉与下载类型单选一起换成了 #createPipeline 里的四枚段控按钮，等高线
    # 那一枚是 `<button ... data-pipeline="contour">`。锚点换成它 —— 守的还是
    # 同一件事：首页必须有「选等高线管线」这个入口，没有它下面两个容器永远
    # 显不出来。
    assert 'data-pipeline="contour"' in html
    assert 'id="contourOptions"' in html
    assert 'id="contourInterval"' in html
