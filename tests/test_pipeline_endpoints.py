"""四条管线 start/pause/resume/cancel 端点的对称覆盖（M21）。

穷举全测试套件打过的 URL 字面量后确认：改前**没有任何一条**请求过四条管线的
`/start`、`/resume`，contour 的 `/pause`，local 的 `/cancel`，或
`GET /api/tasks/<id>`；地图管线的 pause/cancel 也只打到过 ValueError→400 分支。

这些端点里各自**手抄**了一份 8-10 行的 `except ValueError → 400`（contour/dem
的 delete/cancel 还手抄了 `404 if "not found" in msg else 400` 的分流）——
复制粘贴最容易漏的地方，恰恰零覆盖。上一轮 review 的 MEDIUM #15 就是这类漏抄
的实例（contour 的 cancel 漏了 `except ValueError`，用户点取消得到 500），靠人
读代码发现，测试全绿。

本文件不追求覆盖成功路径（start 会真拉起下载线程），只钉住**错误分支**：
它们零副作用、极易补，而漏抄 except 的失败形态正是 500。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# (标签, URL 前缀, 支持的动作)
_PIPELINES = [
    ("map", "/api/tasks", ("start", "pause", "resume", "cancel")),
    ("dem", "/api/dem/tasks", ("start", "pause", "resume", "cancel")),
    ("contour", "/api/contour/tasks", ("start", "pause", "resume", "cancel")),
    # local terrain 的切片是一次性 build_terrain，没有 pause/resume 模型
    ("local_terrain", "/api/terrain/local/tasks", ("cancel",)),
]

_CASES = [(label, prefix, action)
          for label, prefix, actions in _PIPELINES
          for action in actions]


@pytest.mark.parametrize("label,prefix,action", _CASES,
                         ids=[f"{c[0]}-{c[2]}" for c in _CASES])
def test_action_on_missing_task_is_a_client_error_not_500(isolated_app, label, prefix, action):
    """对不存在的任务做 start/pause/resume/cancel 必须回 4xx，且带 JSON error。

    漏抄 `except ValueError` 的失败形态正是 500 —— 这条断言就是冲它去的。
    """
    client = isolated_app.app.test_client()

    resp = client.post(f"{prefix}/999999/{action}")

    assert resp.status_code in (400, 404), (
        f"{label} {action}: 不存在的任务应回 400/404，实际 {resp.status_code}"
        f"（500 通常意味着漏了 except ValueError）"
    )
    body = resp.get_json()
    assert isinstance(body, dict) and body.get("error"), (
        f"{label} {action}: 错误响应必须是带 error 的 JSON，实际 {resp.data[:200]!r}")


def _insert_map_task(status):
    from core.database import get_connection_context
    with get_connection_context() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_format, output_path) "
            "VALUES ('t', ?, 1, 0, 1, 0, 1, 2, 's', 'tiles_only', '/tmp/x')",
            (status,))
        conn.commit()
        return cur.lastrowid


def test_get_task_detail_returns_404_for_missing_task(isolated_app):
    """GET /api/tasks/<id> 改前一次都没被请求过。"""
    client = isolated_app.app.test_client()
    resp = client.get("/api/tasks/999999")
    assert resp.status_code == 404
    assert resp.get_json().get("error")


def test_get_task_detail_returns_the_task(isolated_app):
    client = isolated_app.app.test_client()
    task_id = _insert_map_task("pending")
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.get_json()["task"]["id"] == task_id


def test_pause_running_task_succeeds_and_flips_db_state(isolated_app):
    """成功路径至少覆盖一次：200 + DB 状态真的翻转。

    改前地图管线的 pause 只有 ValueError→400 分支被打到过，200 成功路径一行
    没跑过 —— 「端点能返回 200」和「它真的改了状态」是两件事。
    """
    from core.database import get_connection_context
    client = isolated_app.app.test_client()
    task_id = _insert_map_task("running")

    resp = client.post(f"/api/tasks/{task_id}/pause")

    assert resp.status_code == 200, resp.data
    assert resp.get_json()["success"] is True
    with get_connection_context() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "paused"


def test_cancel_pending_task_succeeds_and_flips_db_state(isolated_app):
    from core.database import get_connection_context
    client = isolated_app.app.test_client()
    task_id = _insert_map_task("pending")

    resp = client.post(f"/api/tasks/{task_id}/cancel")

    assert resp.status_code == 200, resp.data
    with get_connection_context() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == "cancelled"


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_cancel_on_terminal_task_is_rejected_uniformly(isolated_app, terminal):
    """终态任务 cancel：四条管线都必须拒绝，而不是静默回 success。

    contour 此前是唯一 fall through 的一条（U2），这里用 map 管线钉住约定，
    contour 侧的同款断言在 tests/test_cancel_terminal_state.py。
    """
    from core.database import get_connection_context
    client = isolated_app.app.test_client()
    task_id = _insert_map_task(terminal)

    resp = client.post(f"/api/tasks/{task_id}/cancel")

    assert resp.status_code in (400, 404), (
        f"终态({terminal})任务的 cancel 应被拒绝，实际 {resp.status_code}")
    with get_connection_context() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["status"] == terminal, "终态记录绝不可被 cancel 改写"
