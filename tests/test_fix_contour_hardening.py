"""2026-08-08 评审 · 等高线管线加固（P1#10 / P1#11 + 五条 contour P2）。

每条用例都对着一个**旧行为**：

* P1#10 颜色只查 `#` 前缀 —— `#zzzzzz` 创建时 201 收下，一路到 per-tile 渲染
  才在吞异常的 except 里把每一张瓦片记成 failed，任务最后报
  「No contour tiles rendered (check DEM coverage / interval / zoom range)」，
  指着三个都正确的参数，而且要等一整轮 warp + 全量瓦片之后。
* P1#11 per-tile 级数无上限 —— interval 只查 > 0，UI 的 min="1" 从不执行；
  0.1m 间距叠 1000m 起伏 ≈ 单瓦片 1 万条 trace，瓦片内部没有停止检查。
* P2 暂存目录清理是死代码 —— 成功路径从 try 内 `return`，每个成功的上传任务
  泄漏一个空 `contour_upload_*` 目录。
* P2 产物位置三个消费者两套根 —— 路由重算 `DOWNLOADS_DIR/dem`，writer 用存储值，
  deleter 用 `resolve_stored_output_dir`；今天巧合一致，搬动 frozen exe 后永久 404。
* P2 `zoom_max < detail_zoom` 时用户填的间距被静默无视（50m → 2500m）。
* P2 非栅格文件带 201 收下，失败要等 warp 之后以一句原始 GDAL 报错出现。

「反事实」注释说明为什么该断言不是空转（例如级数上限那条：只断言 <= 200 在
根本没调用 ax.contour 时也成立，所以同一用例必须证明粗间距那侧确实画了线）。
"""

import importlib
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from conftest import fresh_import, geotiff_bytes

pytest.importorskip("osgeo.gdal")
pytest.importorskip("matplotlib")


def _setup_db(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    db = fresh_import(monkeypatch, "src.core.database")
    db.init_database()
    ctm_mod = fresh_import(monkeypatch, "src.services.contour_task_manager")
    return db, ctm_mod


def _load_app(monkeypatch, tmp_path):
    from src.core import config
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "OUTPUT_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    app_mod = fresh_import(
        monkeypatch,
        "app", "src.core.database", "src.services.contour_task_manager",
        "src.routes", "src.routes.contour_api", "src.routes.contour_static",
    )[0]
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _post_task(client, name="t", files=None, **fields):
    if files is None:
        files = [("dem1.tif", geotiff_bytes())]
    data = dict(fields)
    data["name"] = name
    data["files"] = [(io.BytesIO(content), fname) for fname, content in files]
    return client.post("/api/contour/tasks", data=data,
                       content_type="multipart/form-data")


class _FakeUpload:
    def __init__(self, filename, content):
        self.filename = filename
        self._content = content

    def save(self, dst):
        with open(dst, "wb") as f:
            f.write(self._content)


# ---------------------------------------------------------------------------
# P1#10 颜色校验：创建时收下的必须恰好等于渲染时画得出的
# ---------------------------------------------------------------------------

# 判据表。渲染器对颜色的解析器就是 matplotlib.colors.to_rgba：ListedColormap
# （分层设色）与 ax.contour(colors=...) 最终都落到它。
_COLOR_CASES = [
    ("#112233", True),
    ("#abc", True),
    ("#AABBCCDD", True),
    ("red", True),
    ("#zzzzzz", False),
    ("#12345", False),
    ("112233", False),
    ("not-a-color", False),
]


def test_validate_color_matches_the_renderer_exactly():
    """创建时的判据必须与渲染器逐值一致。

    旧判据是 `value.startswith('#')`：`#zzzzzz` / `#12345` 通过（然后在渲染里
    炸掉），而 `red` 被拒（渲染器其实认）。两个方向都错。
    """
    from matplotlib.colors import to_rgba

    from src.services.contour_task_manager import validate_color

    for value, renderer_ok in _COLOR_CASES:
        try:
            to_rgba(value)
            actual_renderer_ok = True
        except (ValueError, TypeError):
            actual_renderer_ok = False
        assert actual_renderer_ok is renderer_ok, f"判据表与 matplotlib 不符: {value}"

        if renderer_ok:
            assert validate_color(value) == value
        else:
            with pytest.raises(ValueError):
                validate_color(value)


def test_create_rejects_bad_tint_color_with_the_offending_value(monkeypatch, tmp_path):
    """`#zzzzzz` 在分层色带里必须创建时就 400，且错误信息带上那个值 ——
    否则用户只能看到「没渲染出瓦片，检查覆盖/间距/层级」。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, tint_breaks="0,1000",
                      tint_colors="#111111,#zzzzzz,#333333")
    assert resp.status_code == 400
    assert "#zzzzzz" in resp.get_json()["error"]
    # 400 的任务不许留在库里
    assert client.get("/api/contour/tasks").get_json()["count"] == 0


@pytest.mark.parametrize("field", ["line_color_intermediate", "line_color_index"])
def test_create_rejects_bad_line_color(monkeypatch, tmp_path, field):
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, **{field: "#zzzzzz"})
    assert resp.status_code == 400
    body = resp.get_json()["error"]
    assert "#zzzzzz" in body and field in body


def test_create_rejects_bad_background(monkeypatch, tmp_path):
    """背景色以前有两个错法：非 `#` 开头被静默换成默认色（用户的输入被吃掉），
    带 `#` 的垃圾值则放过 —— 然后在 _build_render_ctx 里（per-tile try 之外）
    炸掉整个任务。现在两种都在创建时 400。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    for bad in ("#zzzzzz", "banana"):
        resp = _post_task(client, background=bad)
        assert resp.status_code == 400, bad
        assert bad in resp.get_json()["error"]


def test_create_and_style_preview_agree_on_the_same_bad_color(monkeypatch, tmp_path):
    """同一个值两个端点必须给同一个答案。旧行为里 /style_preview 400、
    创建 201 —— 这个分歧本身就是 P1#10 的证据。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    preview = client.get("/api/contour/style_preview?background=%23zzzzzz")
    create = _post_task(client, background="#zzzzzz")
    assert preview.status_code == 400
    assert create.status_code == 400


def test_transparent_background_still_accepted(monkeypatch, tmp_path):
    """'transparent' 不是颜色但是合法特值（引擎按它出全透明底）——
    不能被新判据一起拒掉。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, background="transparent")
    assert resp.status_code == 201
    tid = resp.get_json()["task_id"]
    task = client.get(f"/api/contour/tasks/{tid}").get_json()["task"]
    assert task["background"] == "transparent"


def test_config_rejects_bad_contour_color(monkeypatch, tmp_path):
    """全局配色也走同一判据：以前 contour_color_* 这些键零校验，
    一个 `#zzzzzz` 会让此后**所有**等高线任务全瓦片失败。"""
    _db, _ctm = _setup_db(monkeypatch, tmp_path)
    from src.services.config_manager import ConfigManager

    cm = ConfigManager()
    assert cm.validate_config("contour_color_index", "#7A4F2A") is True
    assert cm.validate_config("contour_color_index", "#zzzzzz") is False
    assert cm.validate_config("contour_background", "transparent") is True
    assert cm.validate_config("contour_background", "#zzzzzz") is False
    assert cm.validate_config(
        "contour_hypsometric_colors", "#111111,#222222") is True
    assert cm.validate_config(
        "contour_hypsometric_colors", "#111111,#zzzzzz") is False
    with pytest.raises(ValueError):
        cm.set("contour_color_index", "#zzzzzz")


# ---------------------------------------------------------------------------
# P1#11 等高线级数上限
# ---------------------------------------------------------------------------

def test_create_rejects_interval_below_the_floor(monkeypatch, tmp_path):
    """UI 写着 min="1" 但提交路径从不 checkValidity（parseFloat 后直接
    FormData 提交），所以下限只能在服务端成立。旧行为只查 > 0：0.1 拿 201。"""
    app_mod, client = _load_app(monkeypatch, tmp_path)
    for bad in ("0.1", "0.001"):
        resp = _post_task(client, contour_interval=bad)
        assert resp.status_code == 400, bad
        assert "contour_interval" in resp.get_json()["error"]
    assert _post_task(client, contour_interval="1").status_code == 201


def test_config_rejects_default_interval_below_the_floor(monkeypatch, tmp_path):
    """contour_default_interval 是「建任务时 interval 留空」的取值来源：
    配一个 0.1 等于让默认任务在创建时被 400 拒掉，而报错指着另一个字段。"""
    _db, _ctm = _setup_db(monkeypatch, tmp_path)
    from src.services.config_manager import ConfigManager

    cm = ConfigManager()
    assert cm.validate_config("contour_default_interval", "50") is True
    assert cm.validate_config("contour_default_interval", "0.1") is False


def _relief_ctx(np, tmp_path, interval, relief=1000.0):
    """一个跨 relief 米高差的假渲染 ctx（纯线模式），用来驱动
    _render_contour_tile_core 的 level 计算。"""
    from types import SimpleNamespace

    from matplotlib.colors import to_rgba

    from src.services.contour_engine import ContourStyle, ORIGIN_SHIFT

    n = 1024
    arr = np.tile(np.linspace(0.0, relief, n).astype("float64"), (n, 1))

    class _Band:
        def ReadAsArray(self, xoff, yoff, xsize, ysize, **kwargs):
            win = arr[yoff:yoff + ysize, xoff:xoff + xsize]
            buf_x = kwargs.get("buf_xsize") or win.shape[1]
            buf_y = kwargs.get("buf_ysize") or win.shape[0]
            # 粗糙的降采样，够 level 计算用（zmin/zmax 仍横跨整段高差）
            ys = np.linspace(0, win.shape[0] - 1, buf_y).astype(int)
            xs = np.linspace(0, win.shape[1] - 1, buf_x).astype(int)
            return win[np.ix_(ys, xs)]

    return SimpleNamespace(
        originX=-ORIGIN_SHIFT, pxW=2 * ORIGIN_SHIFT / n,
        originY=ORIGIN_SHIFT, pxH=-2 * ORIGIN_SHIFT / n,
        nx=n, ny=n, band=_Band(), nodata=None,
        # detail_zoom=0 让 interval_for_zoom 在任何 z 上都返回 base（不放粗），
        # 这样被测的就只有级数上限本身。
        style=ContourStyle(detail_zoom=0), interval=interval,
        shade=False, water=False,
        att_band=None, transparent=False, bg_rgba=to_rgba("#FAF6EC"),
        out_dir=tmp_path,
    )


def test_tile_renderer_caps_the_level_count(monkeypatch, tmp_path, caplog):
    """瓦片渲染必须自带级数上限（与 render_style_preview 同一个常量）。

    旧行为：`levels = [lo + i*eff for i in range(round((hi-lo)/eff) + 1)]` 无上限，
    0.1m 间距叠 1000m 起伏 ≈ 1 万条 trace；瓦片**内部**没有停止检查（串行在瓦片
    之间查、并行在 512 张的批之间查），所以暂停和删除都打不断它。

    反事实：只断言「传给 ax.contour 的级数 <= 200」在根本没调用 ax.contour 时
    也成立 —— 所以同一用例必须证明粗间距那一侧确实画了线（levels 非空）。
    """
    import logging

    import matplotlib.axes
    np = pytest.importorskip("numpy")
    from src.services.contour_engine import _MAX_CONTOUR_LEVELS, _render_contour_tile_core

    seen_levels = []
    real_contour = matplotlib.axes.Axes.contour

    def spy_contour(self, *args, **kwargs):
        levels = kwargs.get("levels")
        if levels is not None:
            seen_levels.append(len(levels))
        return real_contour(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "contour", spy_contour)

    # 粗间距：正常出图，级数远低于上限 —— 证明这套观测确实能看到 ax.contour
    coarse = _relief_ctx(np, tmp_path / "coarse", interval=100.0)
    (tmp_path / "coarse").mkdir(parents=True, exist_ok=True)
    assert _render_contour_tile_core(2, 1, 1, coarse) == "rendered"
    assert seen_levels and sum(seen_levels) <= _MAX_CONTOUR_LEVELS
    coarse_total = sum(seen_levels)
    assert coarse_total >= 2

    # 细间距：1000m / 0.1m ≈ 10001 级，必须整段不画（而不是画一万条）
    seen_levels.clear()
    fine = _relief_ctx(np, tmp_path / "fine", interval=0.1)
    (tmp_path / "fine").mkdir(parents=True, exist_ok=True)
    with caplog.at_level(logging.WARNING, logger="src.services.contour_engine"):
        status = _render_contour_tile_core(2, 1, 1, fine)
    assert seen_levels == [], f"超过上限时不得画等高线: {seen_levels}"
    # 纯线模式下无线可画 = 留空档（与 featureless 瓦片同一语义）
    assert status == "skipped"
    # 必须留下线索，说清是 interval 太小而不是数据有问题
    assert any("contour_interval" in r.getMessage() for r in caplog.records)


def test_preview_and_tile_share_one_level_cap():
    """预览与瓦片的级数闸门必须是同一个常量，而不是两处 200 字面量 ——
    旧代码里瓦片侧压根没有这道闸门。"""
    import inspect

    from src.services import contour_engine

    src = inspect.getsource(contour_engine.render_style_preview)
    assert "_MAX_CONTOUR_LEVELS" in src and "_MIN_CONTOUR_LEVELS" in src
    tile_src = inspect.getsource(contour_engine._render_contour_tile_core)
    assert "_MAX_CONTOUR_LEVELS" in tile_src


# ---------------------------------------------------------------------------
# P2 暂存目录清理
# ---------------------------------------------------------------------------

def test_successful_upload_leaves_no_staging_dir(monkeypatch, tmp_path):
    """成功路径的 rmtree 以前跟在 try/except **之后**，而成功是从 try 内
    `return task_id` 出去的 —— 那句永远执行不到，每个成功的上传任务泄漏一个
    空 contour_upload_* 目录。"""
    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)

    tif = geotiff_bytes()
    for i in range(3):
        mgr.create_task_with_files(
            name=f"t{i}", files=[_FakeUpload("a.tif", tif)],
            contour_interval=50, zoom_min=12, zoom_max=12)

    dem_root = Path(tmp_path) / "downloads" / "dem"
    leaked = sorted(p.name for p in dem_root.glob("contour_upload_*"))
    assert leaked == [], f"暂存目录泄漏: {leaked}"


def test_failed_upload_also_leaves_no_staging_dir(monkeypatch, tmp_path):
    """失败路径本来就清（走 except），改成 finally 之后不能回退。"""
    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)

    with pytest.raises(ValueError):
        mgr.create_task_with_files(
            name="bad", files=[_FakeUpload("a.tif", geotiff_bytes()),
                               _FakeUpload("b.tif", b"")],
            contour_interval=50, zoom_min=12, zoom_max=12)

    dem_root = Path(tmp_path) / "downloads" / "dem"
    assert sorted(p.name for p in dem_root.glob("contour_upload_*")) == []


# ---------------------------------------------------------------------------
# P2 产物位置：三个消费者一套口径
# ---------------------------------------------------------------------------

def _seed_task_with_output_path(db, output_path):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks (
                name, status, north, south, east, west, dataset,
                contour_interval, background, terrain_shade, water,
                zoom_min, zoom_max, output_path,
                total_files, downloaded_files, failed_files,
                total_tiles, rendered_tiles, failed_tiles
            )
            VALUES ('relocated', 'running', 1, 0, 1, 0, 'upload', 50,
                    '#FAF6EC', 1, 0, 12, 12, ?, 1, 1, 0, 0, 0, 0)
            """,
            (str(output_path),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count)"
            " VALUES (?, 'upload_1_dem.tif', 'dem', 'completed', 0)",
            (task_id,),
        )
        conn.commit()
        return task_id
    finally:
        conn.close()


def test_tile_route_serves_from_the_stored_output_path(monkeypatch, tmp_path):
    """路由必须按行里存的 output_path 找瓦片，而不是重算 DOWNLOADS_DIR/dem。

    旧行为下这个请求恒 404：瓦片在盘上、行也在，只是两边算出的根不同。
    真实触发场景是 frozen exe 被搬走（BASE_DIR 变了），老任务重跑写在旧的
    绝对路径下 —— terrain_static 已把这个场景记成真实存在。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")

    relocated = tmp_path / "moved_elsewhere" / "dem"
    task_id = _seed_task_with_output_path(db, relocated)
    tile = relocated / f"contour_task_{task_id}" / "contour_tiles" / "12" / "5" / "6.png"
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(b"\x89PNG\r\n\x1a\n")

    resp = client.get(f"/contour/{task_id}/12/5/6.png")
    assert resp.status_code == 200, resp.data[:200]
    assert resp.data.startswith(b"\x89PNG")

    # 同一个根下不存在的瓦片仍是 404（没有把闸门整个拆掉）
    assert client.get(f"/contour/{task_id}/12/5/7.png").status_code == 404
    # 不存在的任务照旧 404
    assert client.get(f"/contour/{task_id + 999}/12/5/6.png").status_code == 404


def test_writer_and_route_resolve_the_same_root_for_a_legacy_relative_value(
        monkeypatch, tmp_path):
    """writer / 路由 / deleter 必须走同一条解析规则。

    存量行可能存着相对值（旧版本入库形态）。裸 `Path(stored)` 会按进程 CWD 解析，
    于是 writer 写一处、路由找另一处。这里同时验证两侧：writer 交给 tiler 的
    out_dir 是绝对路径且等于 resolve_stored_output_dir 的结果，路由算出的根与它
    同源（差一层 contour_tiles）。
    """
    import asyncio

    app_mod, client = _load_app(monkeypatch, tmp_path)
    db = importlib.import_module("src.core.database")
    ctm_mod = importlib.import_module("src.services.contour_task_manager")
    from src.services.task_cleanup import resolve_stored_output_dir

    task_id = _seed_task_with_output_path(db, "./downloads/legacy_dem")

    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        seen["out_dir"] = Path(out_dir)
        return {"total": 1, "rendered": 1, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    mgr = ctm_mod.ContourTaskManager(socketio=None)
    # __init__ 的孤儿回收会把 running 改成 paused，重新置回来再执行
    conn = db.get_connection()
    try:
        conn.execute("UPDATE contour_tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    asyncio.run(mgr._execute(task_id, None))

    expected_root = (resolve_stored_output_dir("./downloads/legacy_dem")
                     / f"contour_task_{task_id}")
    assert seen["out_dir"].is_absolute()
    assert seen["out_dir"] == expected_root / "contour_tiles"

    from src.routes import contour_static
    with app_mod.app.test_request_context():
        assert contour_static._tile_root(task_id) == seen["out_dir"]


# ---------------------------------------------------------------------------
# P2 zoom_max < detail_zoom 时用户的间距被静默无视
# ---------------------------------------------------------------------------

def test_detail_zoom_clamped_to_task_zoom_max():
    """detail_zoom 必须夹到本任务真正产出的最高层级。

    旧行为：detail_zoom 固定 14（配置默认），而 zoom_max 是按 DEM 分辨率自动算的。
    粗源算出 zoom_max=9 时，interval_for_zoom(50, 9, 14) = 2500 —— 用户填的 50m
    在**最细**的那一层就已经被放粗 50 倍，而 API 响应与界面都不报告这件事。
    """
    from src.services.contour_engine import interval_for_zoom
    from src.services.contour_task_manager import style_for_task

    class _Cfg:
        def get(self, key, default=None):
            return default

    task = {
        "background": "#FAF6EC",
        "line_color_intermediate": "", "line_color_index": "",
        "tint_breaks": "", "tint_colors": "",
        "zoom_max": 9,
    }
    style = style_for_task(_Cfg(), task)
    assert style.detail_zoom == 9
    # 最细层级上生效的间距 == 用户填的间距（旧行为是 2500）
    assert interval_for_zoom(50.0, 9, style.detail_zoom, style.zoom_scaling) == 50.0
    # 更低层级照旧沿 1-2-5 阶梯放粗（夹住不等于关掉分级）
    assert interval_for_zoom(50.0, 8, style.detail_zoom, style.zoom_scaling) == 100.0


def test_detail_zoom_not_raised_when_zoom_max_is_higher():
    """夹是单向的：zoom_max 高于 detail_zoom 时不能把 detail_zoom 抬上去 ——
    那会让 z=14..18 全用同一密度，与配置意图相反。"""
    from src.services.contour_task_manager import style_for_task

    class _Cfg:
        def get(self, key, default=None):
            return default

    base = {
        "background": "#FAF6EC",
        "line_color_intermediate": "", "line_color_index": "",
        "tint_breaks": "", "tint_colors": "",
    }
    assert style_for_task(_Cfg(), dict(base, zoom_max=18)).detail_zoom == 14
    # 缺列（如 /style_preview 传进来的字典）保持配置值
    assert style_for_task(_Cfg(), base).detail_zoom == 14


def test_execute_effective_interval_at_finest_zoom_equals_the_request(
        monkeypatch, tmp_path):
    """端到端：一条 zoom_max=9 的任务，交给引擎的 style 必须让最细层级上的
    有效间距等于任务行里的 contour_interval。"""
    import asyncio

    from src.services.contour_engine import interval_for_zoom

    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    # manager 必须先建：__init__ 的孤儿回收会把已有的 running 行改判 paused
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contour_tasks (
                name, status, north, south, east, west, dataset,
                contour_interval, background, terrain_shade, water,
                zoom_min, zoom_max, output_path,
                total_files, downloaded_files, failed_files,
                total_tiles, rendered_tiles, failed_tiles
            )
            VALUES ('coarse', 'running', 1, 0, 1, 0, 'upload', 50,
                    '#FAF6EC', 1, 0, 5, 9, ?, 1, 1, 0, 0, 0, 0)
            """,
            (str(Path(tmp_path) / "downloads" / "dem"),),
        )
        task_id = cur.lastrowid
        cur.execute(
            "INSERT INTO contour_files (task_id, granule_id, kind, status, retry_count)"
            " VALUES (?, 'upload_1_dem.tif', 'dem', 'completed', 0)",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    seen = {}

    def fake_tiler(task_dir, out_dir, params, build_contour_fn=None, progress_cb=None,
                   stage_cb=None, stop_flag=None):
        seen["style"] = params.style
        seen["interval"] = params.interval
        seen["zoom_max"] = params.zoom_max
        return {"total": 1, "rendered": 1, "failed": 0}

    import src.services.contour_task_tiler as tiler_mod
    monkeypatch.setattr(tiler_mod, "tile_contour_task_dir", fake_tiler)

    asyncio.run(mgr._execute(task_id, None))

    style = seen["style"]
    eff = interval_for_zoom(seen["interval"], seen["zoom_max"],
                            style.detail_zoom, style.zoom_scaling)
    assert eff == seen["interval"] == 50.0, (
        f"最细层级 z={seen['zoom_max']} 的有效间距应为 50，实际 {eff}")


# ---------------------------------------------------------------------------
# P2 非栅格上传
# ---------------------------------------------------------------------------

def test_non_raster_upload_rejected_with_400(monkeypatch, tmp_path):
    """.tif 后缀 + 非栅格内容必须创建时 400 并点名文件。

    旧行为：创建路径本来就为 bbox 用 GDAL 打开了每个文件，但读失败被有意吞成
    一条 warning、bbox 留 (0,0,0,0)、任务 201 建成；真正的失败要等 warp 之后，
    以一句原始 GDAL 报错落在一个 failed 任务上。
    """
    app_mod, client = _load_app(monkeypatch, tmp_path)
    resp = _post_task(client, files=[("dem1.tif", b"definitely not a geotiff")])
    assert resp.status_code == 400
    assert "dem1.tif" in resp.get_json()["error"]
    assert client.get("/api/contour/tasks").get_json()["count"] == 0
    # 残留清理：不许留下任务目录或暂存目录
    dem_root = Path(tmp_path) / "downloads" / "dem"
    if dem_root.exists():
        assert sorted(p.name for p in dem_root.iterdir()) == []


def test_manager_rejects_non_raster_with_valueerror(monkeypatch, tmp_path):
    db, ctm_mod = _setup_db(monkeypatch, tmp_path)
    mgr = ctm_mod.ContourTaskManager(socketio=None)
    with pytest.raises(ValueError, match="a.tif"):
        mgr.create_task_with_files(
            name="x", files=[_FakeUpload("a.tif", b"nope")],
            contour_interval=50, zoom_min=12, zoom_max=12)


def test_missing_gdal_stays_lenient(monkeypatch, tmp_path):
    """GDAL 装不上时必须维持宽容：此时任何本地栅格校验都做不了，卡住用户没有
    意义（渲染阶段自会因为缺 GDAL 失败）。区分「GDAL 不在」与「GDAL 在但这个
    文件不是栅格」正是这一条的全部内容。"""
    from src.services import contour_task_manager as ctm

    bad = tmp_path / "junk.tif"
    bad.write_bytes(b"nope")

    # GDAL 在位：抛
    with pytest.raises(ValueError):
        ctm._union_tif_extent_lonlat([bad])

    # sys.modules['osgeo'] = None 让 `from osgeo import ...` 抛 ImportError
    monkeypatch.setitem(sys.modules, "osgeo", None)
    assert ctm._union_tif_extent_lonlat([bad]) is None
