"""
Real path-traversal coverage for the terrain static route (review I21).

The pre-existing traversal tests were structurally void: the route uses an
<int:task_id> converter, so a task_id containing slashes never matches the
route at all. The realistic attack vector is a TAMPERED DATABASE ROW: if
local_terrain_tasks.output_path/output_dir points outside DOWNLOADS_DIR
(e.g. /etc or an attacker-controlled dir), serving must still refuse.

These tests dogfood the shared `isolated_app` fixture from tests/conftest.py.
"""

import importlib
import sys
from pathlib import Path

import pytest


def _insert_tampered_task(db, output_path, output_dir):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO local_terrain_tasks
              (name, status, output_path, source_dir, output_dir, maxzoom)
            VALUES ('evil', 'completed', ?, ?, ?, 14)
            """,
            (output_path, output_path, output_dir),
        )
        task_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return task_id


def test_tampered_output_path_outside_downloads_is_not_served(isolated_app, tmp_path):
    """DB row points at an attacker dir OUTSIDE downloads holding a canary
    file. The route must never serve it (404 — the canonical recomputed
    location simply has no such file)."""
    client = isolated_app.app.test_client()
    db = importlib.import_module("src.core.database")

    outside = tmp_path / "attacker_controlled"  # NOT under tmp_path/downloads
    outside.mkdir()
    (outside / "layer.json").write_text('{"canary":"SERVED"}', encoding="utf-8")

    task_id = _insert_tampered_task(db, str(outside), str(outside))

    resp = client.get(f"/terrain/local/{task_id}/layer.json")
    assert resp.status_code == 404
    assert b"canary" not in resp.data


def test_tampered_output_path_pointing_at_etc_is_not_served(isolated_app):
    """Classic /etc target: even though /etc/passwd exists on disk, a row
    whose output_path says /etc must not make it reachable."""
    client = isolated_app.app.test_client()
    db = importlib.import_module("src.core.database")

    task_id = _insert_tampered_task(db, "/etc", "/etc")

    resp = client.get(f"/terrain/local/{task_id}/passwd")
    assert resp.status_code in (400, 403, 404)
    if resp.status_code == 200:  # pragma: no cover - defense in depth
        raise AssertionError("tampered output_path served /etc/passwd!")


def test_resolve_safe_file_guards_traversal_only(isolated_app, tmp_path):
    """0.2.4 起 _resolve_safe_file 不再要求 base_dir 在 DOWNLOADS_DIR 内
    (base_dir 来自任务行/配置,不是请求输入),但仍须硬拒 subpath 穿越。"""
    from werkzeug.exceptions import HTTPException

    from src.routes.terrain_static import _resolve_safe_file

    # base_dir 在 downloads 之外不再拒绝 —— 全盘保存路径的任务就靠它服务
    # (macOS /etc 是符号链接,期望按 resolve 后的形态断言)
    with isolated_app.app.test_request_context("/"):
        target = _resolve_safe_file(Path("/etc"), "passwd")
    assert str(target) == str((Path("/etc") / "passwd").resolve())

    # subpath 穿越仍然硬拒
    with isolated_app.app.test_request_context("/"):
        with pytest.raises(HTTPException) as excinfo:
            _resolve_safe_file(tmp_path / "downloads" / "ok", "../../outside")
    assert excinfo.value.code == 400
