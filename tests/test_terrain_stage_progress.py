"""瓦片循环**之前**那些耗时阶段的进度上报。

## 为什么需要一条独立的通道

多幅 DEM 要先物化成单文件（实测 6 幅 13.6 秒、大任务分钟级），等高线要先
warp 到 EPSG:3857 —— 这两段都跑在 `total` 算出来**之前**：total 要靠 sampler，
sampler 要靠物化产物，物理上没有分母。所以复用不了 `progress_cb(done, total)`，
另开 `stage_cb(phase, fraction)`。

## 最容易踩的那个坑

`stage_cb` 最终接的是 **GDAL 的原生进度回调**，而 GDAL 把「回调抛异常」当成
**用户请求中止**：实测（GDAL 3.8.4）回调抛出时打印 `ERROR 9: User terminated
CreateCopy()`、`gdal.Translate` 返回 **None**、产物文件被删掉。也就是说一次
emit 故障（客户端断开）就能把整个切片作业搞成失败。所以从 GDAL 回调到 socketio
的每一层都必须吞掉异常。
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.terrain_tiling.cesium_terrain import _gdal_stage_callback


# ---------------------------------------------------------------------------
# GDAL 回调适配层
# ---------------------------------------------------------------------------


def test_gdal_stage_callback_forwards_phase_and_fraction():
    seen = []
    cb = _gdal_stage_callback(lambda phase, frac: seen.append((phase, frac)), "merge")

    assert cb(0.0, "", None) == 1
    assert cb(0.5, "msg", None) == 1
    assert cb(1.0, "", None) == 1

    assert seen == [("merge", 0.0), ("merge", 0.5), ("merge", 1.0)]


def test_gdal_stage_callback_returns_none_when_no_consumer():
    """没有 stage_cb 时不要给 GDAL 塞一个空回调 —— 白白多几百次 Python 调用。"""
    assert _gdal_stage_callback(None, "merge") is None


def test_gdal_stage_callback_swallows_exceptions_and_keeps_going():
    """回调抛异常 = GDAL 认为用户要中止 = 物化失败、产物被删。必须吞掉。

    实测 GDAL 3.8.4：回调抛出 → `ERROR 9: User terminated CreateCopy()` →
    gdal.Translate 返回 None。返回值必须恒为 1（继续），不能是 0/None/False。
    """
    def boom(phase, fraction):
        raise RuntimeError("socketio client went away")

    cb = _gdal_stage_callback(boom, "merge")
    assert cb(0.5, "", None) == 1, "回调必须返回 1（继续），否则 GDAL 会中止并删产物"


# ---------------------------------------------------------------------------
# 穿透：TileParams -> tile_dem_task_dir -> build_terrain
# ---------------------------------------------------------------------------


def test_stage_cb_is_threaded_through_the_tiler(tmp_path):
    """TileParams.stage_cb 必须原样传到 build_terrain。

    stage_cb 放在 params 而不是 tile_dem_task_dir 的独立参数：多个契约测试用
    `(task_dir, out_dir, params)` 三参替身钉住管理器到 tiler 的调用形态。
    """
    from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir

    task_dir = tmp_path / "src"
    task_dir.mkdir()
    (task_dir / "a_dem.tif").write_bytes(b"fake")
    out_dir = tmp_path / "tiles"

    captured = {}

    def fake_build_terrain(**kwargs):
        captured.update(kwargs)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "layer.json").write_text('{"available":[]}\n', encoding="utf-8")
        return {"total": 1, "rendered": 1, "failed": 0}

    marker = lambda phase, frac: None  # noqa: E731
    tile_dem_task_dir(
        task_dir, out_dir,
        TileParams(maxzoom=8, parent_url="", stage_cb=marker),
        build_terrain_fn=fake_build_terrain)

    assert captured.get("stage_cb") is marker, \
        "stage_cb 没传到 build_terrain —— 物化阶段的进度会整段丢失"


# ---------------------------------------------------------------------------
# DEM 管理器：节流与异常保护
# ---------------------------------------------------------------------------


class _RecordingSocket:
    def __init__(self, explode=False):
        self.events = []
        self.explode = explode

    def emit(self, name, payload=None):
        self.events.append((name, payload))
        if self.explode:
            raise RuntimeError("client disconnected")


def _real_stage_cb(monkeypatch, tmp_path, socketio, task_id=7):
    """从真实的 DemTaskManager._run_tiling_job 里截下 tiling_stage 闭包。

    这里原来手抄了一份闭包副本，三条用例于是对生产代码零覆盖：实测把真闭包的
    edge 判定、节流、except 宽度、stage_label 兜底各打一个变异，4/4 全部存活。
    换成把切片器替身塞进去、截 params.stage_cb —— 取法同上面的
    test_stage_cb_is_threaded_through_the_tiler。

    截取那一趟把 socketio 挂 None：作业本身的 tiling_progress 与
    _emit_tiling_finished 也发 terrain_job_progress，混进来会污染调用方
    对事件条数的断言。
    """
    from src.core import config

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core import database as db
    from src.services import dem_task_manager as dtm

    db.init_database()

    captured = {}

    def fake_tiler(task_dir, out_dir, params):
        captured["stage_cb"] = params.stage_cb
        return {"total": 0, "rendered": 0, "failed": 0}

    monkeypatch.setattr(dtm, "tile_dem_task_dir", fake_tiler)

    mgr = dtm.DemTaskManager(socketio=None)
    mgr._run_tiling_job(task_id, tmp_path / "src", tmp_path / "tiles", 8, "")

    assert "stage_cb" in captured, "切片器替身没被调用 —— 闭包没截到，下面的断言全是空的"
    mgr.socketio = socketio
    return mgr, captured["stage_cb"]


def test_stage_emit_is_throttled_but_always_sends_the_edges(monkeypatch, tmp_path):
    """中间帧节流，首帧与末帧必发。

    首帧不发 = 界面在整个阶段开始时没有任何反馈（就是这次要修的症状）；
    末帧不发 = 进度永远停在 87% 之类的地方。
    """
    sock = _RecordingSocket()
    _, stage = _real_stage_cb(monkeypatch, tmp_path, sock)

    stage("merge", 0.0)
    for f in (0.1, 0.2, 0.3, 0.4, 0.5):   # 同一节流窗口内的中间帧
        stage("merge", f)
    stage("merge", 1.0)

    fractions = [p["stage_fraction"] for _, p in sock.events]
    assert fractions[0] == 0.0, "首帧被节流掉了 —— 阶段开始时界面仍然没反馈"
    assert fractions[-1] == 1.0, "末帧被节流掉了 —— 进度会停在中间不动"
    assert len(sock.events) < 7, f"中间帧没有节流：发了 {len(sock.events)} 次"


def test_stage_label_falls_back_to_the_raw_phase(monkeypatch, tmp_path):
    """stage_label 是界面直接显示的那串字。

    没有映射就等于把内部标识 'merge'/'overview' 摆给用户看；而映射表里没有的
    新 phase 必须原样兜底，否则新增阶段会在界面上显示成空白。
    """
    sock = _RecordingSocket()
    _, stage = _real_stage_cb(monkeypatch, tmp_path, sock)

    stage("merge", 0.0)
    stage("overview", 1.0)
    stage("brand_new_phase", 1.0)   # 三帧都是 edge，不会被节流吃掉

    labels = [p["stage_label"] for _, p in sock.events]
    assert labels == ["合并 DEM", "建金字塔", "brand_new_phase"], \
        f"stage_label 没走映射表兜底：{labels}"


def test_stage_emit_failure_never_escapes(monkeypatch, tmp_path):
    """emit 抛异常绝不能穿透。

    这发 emit 被 GDAL 的进度回调**同步**调用，而 GDAL 把回调抛异常当成
    「用户请求中止」—— 穿透出去不只是丢一次进度，是整个物化失败、产物被删、
    作业记 failed。
    """
    sock = _RecordingSocket(explode=True)
    _, stage = _real_stage_cb(monkeypatch, tmp_path, sock)

    stage("merge", 0.0)     # 不抛即过
    stage("overview", 1.0)

    assert len(sock.events) == 2, "emit 被调用了，但异常必须被吞掉"


def test_stage_cb_tolerates_missing_socketio(monkeypatch, tmp_path):
    """契约测试会用 __new__ 构造管理器（压根没有 socketio 属性）。

    闭包必须 getattr 兜底：写成 self.socketio 就是一发 AttributeError 穿透进
    GDAL 的进度回调，与上一条同样的后果。
    """
    mgr, stage = _real_stage_cb(monkeypatch, tmp_path, None)
    del mgr.socketio

    stage("merge", 0.5)     # 不抛即过


# ---------------------------------------------------------------------------
# 等高线：warp 阶段用**新的** phase 值
# ---------------------------------------------------------------------------


def test_contour_prepare_phase_is_not_render():
    """准备阶段的 phase 不能复用 'render'。

    多个测试按 `p.get('phase') == 'render'` 过滤事件并数个数
    （tests/test_contour_execute.py），混进来会把它们连带打挂 —— 而且语义上
    也不对：那些计数指的是已渲染的瓦片数。
    """
    import re

    src = open(
        os.path.join(os.path.dirname(__file__), "..",
                     "src", "services", "contour_task_manager.py"),
        encoding="utf-8").read()

    # render_stage 闭包里赋的 phase 值
    m = re.search(r'def render_stage\(.*?\n(.*?)\n\s*render_counts = ', src, re.S)
    assert m, "找不到 render_stage 闭包 —— 测试已失效"
    phases = re.findall(r'payload\["phase"\]\s*=\s*"([^"]+)"', m.group(1))
    assert phases, "render_stage 没有设置 phase"
    assert "render" not in phases, \
        f"准备阶段复用了 phase='render'：{phases} —— 会打挂按 phase 过滤计数的测试"


def test_contour_engine_reports_total_before_first_tile():
    """total 一算出来就要上报，不能等第一块瓦片渲染完。

    上传任务的 total_tiles 在建任务时写死为 0，而 _emit 此前只被逐瓦片的
    _tally 调用 —— 于是界面在整个 warp + 建 ctx + worker 启动期间显示
    「0 / 0 瓦片 · 0%」不动。
    """
    import re

    src = open(
        os.path.join(os.path.dirname(__file__), "..",
                     "src", "services", "contour_engine.py"),
        encoding="utf-8").read()

    # counts["total"] = total 之后、_iter_tiles 之前必须有一次裸 _emit()
    m = re.search(r'counts\["total"\]\s*=\s*total(.*?)def _iter_tiles', src, re.S)
    assert m, "找不到 total 赋值与 _iter_tiles 之间的代码 —— 测试已失效"
    assert re.search(r'^\s*_emit\(\)\s*$', m.group(1), re.M), \
        "total 算完后没有立刻上报 —— 界面会在整个 warp 期间显示 0/0"


# ---------------------------------------------------------------------------
# 前端接线：后端广播的事件必须真的有人听
# ---------------------------------------------------------------------------


def _js(name):
    path = os.path.join(os.path.dirname(__file__), "..", "static", "js", name)
    return open(path, encoding="utf-8").read()


def _js_code(name):
    """剥掉注释后的 JS 源码。

    裸 grep 会被注释骗过 —— 本文件的断言就踩过一次：注释里逐字写着
    「原先写死 `map:${taskId}`」，于是「不许再出现这个字面量」的断言恒红。
    复用 test_tasks_js_contract 的剥离器，别再手写一个。
    """
    from tests.test_tasks_js_contract import _strip_js_comments
    return _strip_js_comments(_js(name))


def test_terrain_job_progress_has_a_frontend_listener():
    """后端广播的 terrain_job_progress 必须有前端监听者。

    这条不是假设性防护：改动前它**一个监听者都没有** —— 后端算好、落库、
    广播了整套逐瓦片进度，前端却只能靠详情弹窗手动点「刷新」才看得到，
    任务列表在整个切片期间毫无动静。emit 与 listener 分处前后端，脱节了
    不会有任何报错。
    """
    backend = open(
        os.path.join(os.path.dirname(__file__), "..",
                     "src", "services", "dem_task_manager.py"), encoding="utf-8").read()
    assert 'emit("terrain_job_progress"' in backend, "后端不再广播该事件？"

    js = _js("tasks.js")
    assert "socket.on('terrain_job_progress'" in js, \
        "terrain_job_progress 没有前端监听者 —— 切片进度在界面上不存在"


def test_stage_text_helper_is_not_hardcoded_to_map():
    """updateTaskStageText 的 key 前缀必须参数化。

    原先写死 `map:${taskId}`，地形/等高线管线复用不了 —— 它们的行 key 分别是
    `dem:` / `local_terrain:` / `contour:`。
    """
    code = _js_code("tasks.js")
    assert "`map:${taskId}`" not in code, \
        "updateTaskStageText 仍写死 map: 前缀，别的管线复用不了"
    assert re.search(r"function updateTaskStageText\([^)]*taskType[^)]*\)", code), \
        "updateTaskStageText 没有 taskType 参数"


def test_stage_text_has_a_clear_path():
    """stage_text 必须能被清除，否则会变成幽灵文本。

    进度更新只覆盖计数、不动 stage_text；于是任何后续的状态跃迁（含等高线的
    phaseChanged）都会让过期的阶段文字复活并**永久顶掉**计数。地图管线侥幸
    无事只是因为它的拼接/复制发生在下载彻底结束之后，两类事件永不交错；
    新阶段夹在两个会发 task_progress 的阶段中间，不清就必然显形。

    Vue 化后清除的形态从 `delete task.stage_text` 变成写空串：store 里的行是
    响应式对象，`delete` 属性在 Vue 3 的 reactive 下虽然能触发更新，但组件里
    这两个字段都参与 `||` 取值，写 '' 语义更直白也更难写错。
    """
    code = _js_code("tasks.js")
    assert re.search(r"stage_text:\s*stageText\s*\|\|\s*''", code), \
        "stage_text 只写不清 —— 过期的阶段文字会复活并顶掉计数"
    assert re.search(r"tilingText\s*=\s*''", code), \
        "tiling_text 只写不清 —— 切片结束后文字会一直挂在行上"


def test_tiling_text_renders_in_the_completed_branch():
    """已完成任务行也要能显示切片进度。

    DEM 的切片作业跑在**下载任务已经 completed 之后** —— 行落在组件的终态
    分支，那里原本既没有进度条也没有阶段位置，于是几十分钟的切片在界面上
    完全没有痕迹。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "js", "task_list.js"), encoding="utf-8") as f:
        code = f.read()
    assert "tiling_text" in code, "TaskRow 组件不渲染 tiling_text"
    # 活动态分支：tiling_text 优先于 stage_text（本地地形切片期间仍是 running，
    # 进度条一上传完就写满，行上必须显示切片进度才看得出还在跑）
    assert re.search(r"task\.tiling_text\s*\|\|\s*this\.task\.stage_text", code), \
        "stageText computed 没有让 tiling_text 优先于 stage_text"
    # 终态分支：DEM 切片时任务已 completed，走的正是那条
    assert 'class="task-line2 task-tiling-line" v-if="task.tiling_text"' in code, \
        "终态分支没有渲染 tiling_text —— DEM 切片时任务已 completed，走的正是那条"
