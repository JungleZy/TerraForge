"""本地地形任务在**切片阶段**的进度读数（任务行 + 底部状态栏）。

## 症状

界面上「切片的进度一直显示 100%」。

## 根因

`local_terrain` 行的百分比只有一个口径 —— 上传的文件数：

    tasks.js  normalizeTask()      total_items = total_files
                                   downloaded_items = uploaded_files
    task_list.js  progress()       downloaded_items / total_items
    tasks.js  updateStatusTasks()  Σdownloaded_items / Σtotal_items

上传（或 dem_task 来源的零拷贝）在秒级结束，计数当场写满；而真正耗时的切片
（物化 → 逐瓦片，几十分钟起）只以**字符串** `tiling_text` 出现在行上，
从来没有进入百分比口径。于是整个切片过程进度条、行右侧的百分数、状态栏的
汇总读数三处全部恒为 100%。

后端不缺数据：`local_terrain_task_manager._run_tiling_job` 每秒最多一发
`terrain_job_progress`，带 `rendered_tiles/total_tiles`（逐瓦片）或
`stage_fraction`（物化/建金字塔）。缺的是前端把它接到百分比上。

修法与等高线管线同构 —— 等高线早就做了阶段切换（`contourPhaseCounts`：
渲染阶段一开始，进度口径从「下载的 DEM 文件」切到「渲染的瓦片」）。这里不
改计数字段（切片阶段的分母是瓦片、行上的单位是文件，混在一起会让
`countText` 说谎），而是单开一个百分比字段 `tiling_progress`：
`progress()` 优先取用，状态栏按各任务原有权重折算，切片结束时清空、
口径落回文件计数。
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_map_js_contract import _fn_body

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node 不可用，跳过 JS 行为断言")


def _js(name):
    with open(os.path.join(ROOT, "static", "js", name), encoding="utf-8") as f:
        return f.read()


def _method_body(src, name):
    """按花括号配对提取对象字面量里的 `name() { ... }`（Vue computed）。"""
    m = re.search(r'^\s*' + name + r'\(\)\s*\{', src, re.M)
    assert m, f"找不到 {name}() —— 测试已失效"
    j = src.index("{", m.start())
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    raise AssertionError(f"{name}() 花括号不配对")


def _run_node(script):
    # encoding 必须显式给：Windows 上 text=True 默认按 locale（cp1252）解码，
    # 脚本输出里的中文（如 stage_label '合并 DEM'）会让 subprocess 的读取线程
    # 抛 UnicodeDecodeError，stdout 静默变成 None，报错现场离真因十万八千里。
    return json.loads(subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
        timeout=60).stdout.strip())


# ---------------------------------------------------------------------------
# 事件 -> 百分比
# ---------------------------------------------------------------------------


@requires_node
def test_tiling_events_carry_a_percentage_for_the_row():
    """逐瓦片与物化两种事件都要给出百分比，收尾时清空。

    只发 `tiling_text` 是不够的：那串字在行上是**阶段提示**的位置，
    进度条和右侧的「NN%」读的是另一条路。
    """
    src = _js("tasks.js")
    fn = "function updateTerrainJobProgress(data) " + _fn_body(
        src, "updateTerrainJobProgress")
    script = (
        "const t = (k, v) => k + ':' + JSON.stringify(v || {});\n"
        "let committed = null;\n"
        "function commitTaskUpdate(key, patch) { committed = patch; return patch; }\n"
        "const window = { TaskStore: { has: () => true,"
        " get: () => null, getActive: () => null,"
        " patch: (k, p) => { committed = p; },"
        " commit: (k, p) => { committed = p; } } };\n"
        + fn + "\n"
        "const out = [];\n"
        "const call = (d) => { committed = null;"
        " updateTerrainJobProgress(Object.assign("
        "{task_type: 'local_terrain', task_id: 1, status: 'running'}, d));"
        " out.push(committed); };\n"
        "call({rendered_tiles: 30, total_tiles: 100});\n"
        "call({rendered_tiles: 0, total_tiles: 2000});\n"
        "call({stage_label: '合并 DEM', stage_fraction: 0.45});\n"
        "call({status: 'completed'});\n"
        "console.log(JSON.stringify(out));\n"
    )
    tiles, first, stage, done = _run_node(script)

    assert tiles.get("tiling_progress") == 30, \
        f"逐瓦片事件没给百分比：{tiles} —— 进度条仍停在上传阶段写满的 100%"
    assert first.get("tiling_progress") == 0, \
        f"切片刚开始（0/2000）应该是 0% 而不是 {first.get('tiling_progress')!r}"
    assert stage.get("tiling_progress") == 45, \
        f"物化阶段没给百分比：{stage} —— 这一段同样跑在文件计数写满之后"
    assert done.get("tiling_progress") is None, \
        f"切片收尾没清掉百分比：{done} —— 会永久顶掉行自己的文件计数口径"


@requires_node
def test_tiling_progress_reaches_the_active_set():
    """切片百分比必须同时写进**活动集**，否则状态栏读不到。

    store 里是两个集合：时间流（当前分页，负责渲染）与活动集（全量活动任务，
    负责底部状态栏的汇总读数）。`TaskStore.patch` 只写前者 —— 那样行上的百分比
    对了，状态栏仍然恒显 100%。而且切片任务完全可能落在第 2 页之后，那时时间流
    里根本没有它，只写时间流等于这一发全丢。
    """
    src = _js("tasks.js")
    fn = "function updateTerrainJobProgress(data) " + _fn_body(
        src, "updateTerrainJobProgress")
    # 真实语义的替身：patch 只写时间流，commit 两边都写（见 task_store.js）。
    script = (
        "const t = (k, v) => k + ':' + JSON.stringify(v || {});\n"
        "const rows = { 'local_terrain:1': {} };\n"
        "const active = { 'local_terrain:1': {}, 'local_terrain:2': {} };\n"
        "const window = { TaskStore: {\n"
        "  has: (k) => k in rows,\n"
        "  get: (k) => rows[k] || null,\n"
        "  getActive: (k) => active[k] || null,\n"
        "  patch: (k, p) => { if (rows[k]) Object.assign(rows[k], p); },\n"
        "  commit: (k, p) => { if (rows[k]) Object.assign(rows[k], p);"
        " if (active[k]) Object.assign(active[k], p); },\n"
        "} };\n"
        "function commitTaskUpdate(key, patch) {"
        " return window.TaskStore ? window.TaskStore.patch(key, patch) : null; }\n"
        + fn + "\n"
        "updateTerrainJobProgress({task_type: 'local_terrain', task_id: 1,"
        " status: 'running', rendered_tiles: 30, total_tiles: 100});\n"
        "updateTerrainJobProgress({task_type: 'local_terrain', task_id: 2,"
        " status: 'running', rendered_tiles: 10, total_tiles: 100});\n"
        "console.log(JSON.stringify([rows['local_terrain:1'],"
        " active['local_terrain:1'], active['local_terrain:2']]));\n"
    )
    row, act, offpage = _run_node(script)

    assert row.get("tiling_progress") == 30, f"时间流那一行没更新：{row}"
    assert act.get("tiling_progress") == 30, \
        f"活动集没更新：{act} —— 状态栏的汇总读数仍按写满的文件计数算"
    assert offpage.get("tiling_progress") == 10, \
        f"不在当前分页的切片任务被整发丢弃：{offpage} —— 状态栏里它恒为 100%"


# ---------------------------------------------------------------------------
# 百分比 -> 界面
# ---------------------------------------------------------------------------


@requires_node
def test_row_percentage_prefers_the_tiling_progress():
    """`progress()` 必须优先用切片百分比。

    行自己的文件计数在切片期间恒为「满」，是这条 bug 的源头；有切片百分比时
    必须让位，没有时（下载/上传阶段、终态）照旧按计数算。
    """
    body = _method_body(_js("task_list.js"), "progress")
    script = (
        "const progress = function() " + body + ";\n"
        "console.log(JSON.stringify([\n"
        "  progress.call({task: {tiling_progress: 30}, total: 3, downloaded: 3}),\n"
        "  progress.call({task: {tiling_progress: 0}, total: 3, downloaded: 3}),\n"
        "  progress.call({task: {}, total: 4, downloaded: 1}),\n"
        "  progress.call({task: {}, total: 0, downloaded: 0}),\n"
        "]));\n"
    )
    tiling, tiling_zero, counting, empty = _run_node(script)

    assert tiling == 30, f"切片百分比没被优先取用（得到 {tiling}）—— 行仍显示 100%"
    assert tiling_zero == 0, \
        f"切片百分比 0 被当成「没有值」（得到 {tiling_zero}）—— 0 是合法进度，不能用真值判断"
    assert counting == 25, f"没有切片百分比时应该退回计数口径，得到 {counting}"
    assert empty == 0, f"分母为 0 时应该是 0，得到 {empty}"


def _statusbar_script(live_tasks):
    """跑真实的 updateStatusTasks，返回它写进状态栏的百分比。

    t() 直接回吐参数：状态栏文案是 `{n} 个任务进行中 · {pct}%` 这种模板，
    把变量原样吐成 JSON 就能读出 pct，不必跟着文案走。
    """
    body = _fn_body(_js("tasks.js"), "updateStatusTasks")
    return (
        "const t = (k, v) => JSON.stringify(v || {});\n"
        "const els = {\n"
        "  statusTasksText: { textContent: '' },\n"
        "  statusTasksProgress: { hidden: true },\n"
        "  statusTasksBar: { style: { width: '' } },\n"
        "};\n"
        "const document = { getElementById: (id) => els[id] || null };\n"
        "const live = " + json.dumps(live_tasks) + ";\n"
        "const window = { TaskStore: { liveTasks: () => live } };\n"
        "function updateStatusTasks() " + body + "\n"
        "updateStatusTasks();\n"
        "console.log(JSON.stringify([els.statusTasksText.textContent,"
        " els.statusTasksBar.style.width]));\n"
    )


@requires_node
def test_statusbar_aggregate_honours_the_tiling_progress():
    """状态栏汇总：切片中的任务不能按「文件已上传满」计。

    这条与行上那条是同一个 bug 的两个出口 —— 只修行的话，一个人盯着状态栏
    看到的仍然是「1 个任务进行中 · 100%」跑一小时。
    """
    text, width = _run_node(_statusbar_script([
        {"status": "running", "task_type": "local_terrain",
         "total_items": 3, "downloaded_items": 3, "tiling_progress": 30},
    ]))
    assert json.loads(text)["pct"] == 30, \
        f"状态栏没用切片百分比：{text} —— 汇总读数在整个切片期间恒为 100%"
    assert width == "30%", f"状态栏进度条宽度没跟上：{width}"


@requires_node
def test_statusbar_keeps_weighting_tasks_by_their_own_item_counts():
    """折算必须保持各任务原有权重，不能让切片任务变成「一票 100 单位」。

    汇总是按条目数加权的（Σdone / Σtotal）。切片任务只有几个文件，若换成
    「0~100」的百分比单位，它会在汇总里盖过一个几万瓦片的下载任务。
    """
    text, _ = _run_node(_statusbar_script([
        {"status": "running", "task_type": "map",
         "total_items": 1000, "downloaded_items": 500},
        {"status": "running", "task_type": "local_terrain",
         "total_items": 4, "downloaded_items": 4, "tiling_progress": 50},
    ]))
    # done = 500 + 4*0.5 = 502，total = 1004 -> 50%
    assert json.loads(text)["pct"] == 50, \
        f"加权口径变了：{text}（期望 (500+2)/1004 = 50%）"


@requires_node
def test_statusbar_unchanged_without_tiling_progress():
    """没有切片百分比的任务照旧按计数算 —— 这条是回归看守。"""
    text, width = _run_node(_statusbar_script([
        {"status": "running", "task_type": "map",
         "total_items": 400, "downloaded_items": 100},
    ]))
    assert json.loads(text)["pct"] == 25, f"普通任务的汇总口径被改坏了：{text}"
    assert width == "25%"


# ---------------------------------------------------------------------------
# 切片阶段的「预计剩余」
# ---------------------------------------------------------------------------
#
# 行右侧的 ETA 由 calculateTimeInfo 按 downloaded_items/total_items 线性外推。
# 切片期间那两个数恒为满 -> progress = 1 -> 剩余 0 -> 整段不显示 ETA。
#
# 不能直接把切片百分比塞给它：elapsed 是**整个任务**的墙钟时长（含上传、物化），
# 而切片百分比从 0 重新开始，切片刚起步时会外推出一个荒唐的大数。所以按
# 「这一段自己的起点 + 起点处的百分比」外推，并且只在**逐瓦片**那一段给 ——
# 物化阶段给 ETA 等于对用户说「快好了」，而它后面还压着整个切片。


def _fake_store_script(seed_row, fn):
    """带状态的 store 替身：get 能读回上一发写进去的字段。"""
    return (
        "const t = (k, v) => k + ':' + JSON.stringify(v || {});\n"
        "const row = " + json.dumps(seed_row) + ";\n"
        "const window = { TaskStore: {\n"
        "  has: () => true,\n"
        "  get: () => row,\n"
        "  getActive: () => null,\n"
        "  patch: (k, p) => Object.assign(row, p),\n"
        "  commit: (k, p) => Object.assign(row, p),\n"
        "} };\n"
        "function commitTaskUpdate(key, patch) {"
        " return window.TaskStore.patch(key, patch); }\n"
        + fn + "\n"
    )


@requires_node
def test_tiling_phase_is_anchored_for_the_eta():
    """逐瓦片阶段要记下**这一段**的起点与起点处的百分比。

    - 首发：起点 = 此刻，锚 = 这一发的百分比（半途打开页面也不会算出天文数字）
    - 续发：起点与锚都不动，否则每发都重新计时，ETA 恒等于 0
    - 倒退：并行 worker 崩溃会回退串行、计数从 0 重来
      （cesiumlab_terrain.py 的 BrokenProcessPool 分支），此时必须重新锚定
    """
    fn = "function updateTerrainJobProgress(data) " + _fn_body(
        _js("tasks.js"), "updateTerrainJobProgress")
    ev = ("{task_type: 'local_terrain', task_id: 1, status: 'running',"
          " total_tiles: 100, rendered_tiles: %d}")
    script = _fake_store_script({}, fn) + (
        "const out = [];\n"
        "updateTerrainJobProgress(" + (ev % 20) + ");\n"
        "out.push(Object.assign({}, row));\n"
        "row.tiling_started_at = 1000;   // 假装这一段是很久以前开始的\n"
        "updateTerrainJobProgress(" + (ev % 40) + ");\n"
        "out.push(Object.assign({}, row));\n"
        "updateTerrainJobProgress(" + (ev % 5) + ");   // worker 崩溃后从头重来\n"
        "out.push(Object.assign({}, row));\n"
        "console.log(JSON.stringify(out));\n"
    )
    first, second, restarted = _run_node(script)

    assert first.get("tiling_phase") == "tiles", f"没标记阶段：{first}"
    assert first.get("tiling_anchor_pct") == 20, \
        f"锚点不是首发的百分比：{first} —— 半途接上的任务会外推出天文数字"
    assert first.get("tiling_started_at"), f"没记下这一段的起点：{first}"

    assert second.get("tiling_started_at") == 1000, \
        f"续发把起点重置了：{second} —— 每发都重新计时，ETA 恒为 0"
    assert second.get("tiling_anchor_pct") == 20, f"续发把锚点挪了：{second}"

    assert restarted.get("tiling_started_at") != 1000, \
        f"进度倒退后没有重新锚定：{restarted} —— ETA 会一直算不出来"
    assert restarted.get("tiling_anchor_pct") == 5, f"重锚后锚点不对：{restarted}"


@requires_node
def test_stage_and_finish_events_drop_the_anchor():
    """物化阶段与收尾都要清掉锚点。

    物化不给 ETA（它后面还压着整个切片）；收尾不清则锚点会跨作业残留，
    下一次切片的第一发会拿上一次的起点算出一个假的剩余时间。
    """
    fn = "function updateTerrainJobProgress(data) " + _fn_body(
        _js("tasks.js"), "updateTerrainJobProgress")
    seed = {"tiling_phase": "tiles", "tiling_started_at": 1000,
            "tiling_anchor_pct": 20}
    script = _fake_store_script(seed, fn) + (
        "const out = [];\n"
        "updateTerrainJobProgress({task_type: 'local_terrain', task_id: 1,"
        " status: 'running', stage_label: '合并 DEM', stage_fraction: 0.5});\n"
        "out.push(Object.assign({}, row));\n"
        "updateTerrainJobProgress({task_type: 'local_terrain', task_id: 1,"
        " status: 'completed'});\n"
        "out.push(Object.assign({}, row));\n"
        "console.log(JSON.stringify(out));\n"
    )
    stage, done = _run_node(script)

    assert stage.get("tiling_started_at") is None, f"物化阶段没清起点：{stage}"
    assert stage.get("tiling_anchor_pct") is None, f"物化阶段没清锚点：{stage}"
    assert done.get("tiling_phase") is None, f"收尾没清阶段标记：{done}"
    assert done.get("tiling_started_at") is None, f"收尾没清起点：{done}"


@requires_node
def test_tiling_eta_extrapolates_from_the_phase_anchor():
    """ETA 用「这一段的起点」和「相对锚点前进了多少」外推。

    stub 掉 calculateTimeInfo 只看喂进去的口径：公式本身已经有它自己的用例，
    这里要钉的是**喂对了数**（否则就是拿整任务的墙钟时长除以切片的百分比）。
    """
    body = _method_body(_js("task_list.js"), "tilingEstimated")
    script = (
        "const store = { state: { tick: 0 } };\n"
        "let seen = null;\n"
        "function calculateTimeInfo(task) { seen = task;"
        " return { show: true, elapsed: 'E', estimated: 'REMAIN' }; }\n"
        "const tilingEstimated = function() " + body + ";\n"
        "const call = (task) => { seen = null;"
        " return [tilingEstimated.call({ task: task }), seen]; };\n"
        "console.log(JSON.stringify([\n"
        "  call({tiling_phase: 'tiles', tiling_progress: 60,"
        " tiling_anchor_pct: 20, tiling_started_at: 1700000000000}),\n"
        "  call({tiling_phase: 'tiles', tiling_progress: 20,"
        " tiling_anchor_pct: 20, tiling_started_at: 1700000000000}),\n"
        "  call({tiling_phase: 'stage', tiling_progress: 60,"
        " tiling_anchor_pct: 20, tiling_started_at: 1700000000000}),\n"
        "  call({}),\n"
        "]));\n"
    )
    (eta, seen), (flat, _), (stage, _), (none, _) = _run_node(script)

    assert eta == "REMAIN", "逐瓦片阶段没有给出 ETA"
    assert seen["downloaded_items"] == 40, \
        f"喂的不是「相对锚点前进的百分比」：{seen}（60 - 20 = 40）"
    assert seen["total_items"] == 80, \
        f"分母不是「锚点到 100 还剩多少」：{seen}（100 - 20 = 80）"
    assert seen["started_at"], "没把这一段的起点喂进去 —— 会用整任务的墙钟时长外推"
    assert "2023-11-14" in seen["started_at"], \
        f"起点不是 tiling_started_at 那个时刻：{seen['started_at']}"
    assert flat == "", "进度没前进时不该给 ETA（除零/无穷大）"
    assert stage == "", "物化阶段不该给 ETA —— 它后面还压着整个切片"
    assert none == "", "普通任务不该走这条路"


@requires_node
def test_row_time_prefers_the_tiling_eta():
    """行上的「预计剩余」优先用切片 ETA，没有时退回计数口径的那个。"""
    body = _method_body(_js("task_list.js"), "timeText")
    script = (
        "const store = { state: { tick: 0 } };\n"
        "function formatShortDate() { return 'DATE'; }\n"
        "function calculateTimeInfo() {"
        " return { show: true, elapsed: 'ELAPSED', estimated: 'BY_COUNTS' }; }\n"
        "const timeText = function() " + body + ";\n"
        "const ctx = (tilingEstimated) => ({ isLive: true, task: {},"
        " downloaded: 3, total: 3, tilingEstimated: tilingEstimated,"
        " t: (k, v) => JSON.stringify(v || {}) });\n"
        "console.log(JSON.stringify([\n"
        "  timeText.call(ctx('BY_TILING')),\n"
        "  timeText.call(ctx('')),\n"
        "]));\n"
    )
    with_tiling, without = _run_node(script)

    assert "BY_TILING" in with_tiling, \
        f"行上没用切片 ETA：{with_tiling} —— 切片期间计数口径算出来的剩余恒为 0，整段不显示"
    assert "BY_COUNTS" not in with_tiling, f"两个 ETA 同时显示了：{with_tiling}"
    assert "BY_COUNTS" in without, f"没有切片 ETA 时应该退回计数口径：{without}"
