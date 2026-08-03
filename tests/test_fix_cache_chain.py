"""
缓存链上三个「静默失败」的回归测试（H2 / M5 / M8）。

共同结构：错误信息只进日志，DB 状态与 HTTP 响应一律「成功」，用户没有任何
察觉途径，且多数无法原地自愈（completed 任务不能重启）。

- H2：写 cache 失败被 except 吞成 warning，仍无条件上报 completed —— 磁盘上
      没有文件、task_tiles 里没有 failed 行、downloaded_tiles 却 +1。
- M5：HTTP 200 的非图片响应（劫持返回的 HTML / 自建服务返回的 JSON）被当成
      瓦片永久写进共享 cache，跨任务扩散且无自动淘汰。
- M8：清理缓存不检查运行中的任务，产物目录静默缺瓦片而任务仍报完成。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.task import Tile  # noqa: E402
from services.download_engine import (  # noqa: E402
    DownloadEngine,
    NotAnImageResponse,
    looks_like_image,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_HTML = b"<!DOCTYPE html><html><head><title>Portal</title></head></html>"


class _FakeResponse:
    def __init__(self, body, content_type):
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, body, content_type="image/png"):
        self._body = body
        self._content_type = content_type
        self.calls = 0

    def get(self, url, timeout=None, proxy=None):
        self.calls += 1
        return _FakeResponse(self._body, self._content_type)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    from core import config
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    # DownloadEngine() 构造时会建 ConfigManager 并读 tile_servers —— 必须把
    # DATABASE_PATH 也指到 tmp_path 并建库，否则用例会去读开发机上真实的
    # data/map_downloader.db（本地碰巧存在所以全绿，CI 的干净 runner 上直接
    # `sqlite3.OperationalError: unable to open database file`，而错误经
    # download_tile 的通用 except 变成一条与本用例无关的失败）。
    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    from core.database import init_database
    init_database()
    eng = DownloadEngine()
    # 不要在测试里真的做指数退避;并把重试次数钉成 1(共 2 次尝试)。
    monkeypatch.setattr(eng, "_interruptible_sleep",
                        lambda *a, **k: asyncio.sleep(0))
    eng._batch_retry_config = (1, 30)
    return eng


# ---------------------------------------------------------------------------
# M5：魔数校验
# ---------------------------------------------------------------------------

def test_looks_like_image_accepts_common_formats():
    assert looks_like_image(_PNG)
    assert looks_like_image(b"\xff\xd8\xff" + b"\x00" * 16)          # JPEG
    assert looks_like_image(b"GIF89a" + b"\x00" * 16)                # GIF
    assert looks_like_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)  # WebP


def test_looks_like_image_rejects_html_json_and_stubs():
    assert not looks_like_image(_HTML)
    assert not looks_like_image(b'{"error":"out of range"}')
    assert not looks_like_image(b"")
    assert not looks_like_image(b"short")


def test_html_error_page_is_not_written_to_cache(engine, monkeypatch):
    """200 + HTML 必须当成下载失败,且【绝不落进共享 cache】。

    否则 0.2.4 起没有自动淘汰 -> 永久命中,跨任务扩散,除手工清空整个缓存分类
    外没有恢复途径。
    """
    session = _FakeSession(_HTML, content_type="text/html")
    tile = Tile(task_id=1, zoom=3, x=2, y=1)

    with pytest.raises(NotAnImageResponse):
        asyncio.run(engine.download_tile(tile, "s", session))

    cache_path = engine._get_cache_path(tile, "s")
    assert not cache_path.exists(), "非图片响应绝不能落进共享缓存"


def test_non_image_response_is_retried_then_reported_failed(engine):
    """非图片当作【可重试】的失败:换服务器/重试仍不行才判 failed。"""
    session = _FakeSession(_HTML, content_type="text/html")
    tile = Tile(task_id=1, zoom=3, x=2, y=1)

    result = asyncio.run(engine._download_single_tile(
        tile, "s", session, cache_enabled=True, progress_callback=None,
    ))

    assert result["status"] == "failed"
    assert session.calls == 2, "应按 max_retries 重试后才放弃"


# ---------------------------------------------------------------------------
# H2：写缓存失败不得上报 completed
# ---------------------------------------------------------------------------

def test_cache_write_failure_reports_tile_as_failed(engine, monkeypatch):
    """写 cache 抛异常时,这块瓦片必须记 failed 并上报 failed。

    旧行为:只打一条 warning 然后无条件 progress_callback(tile,'completed')
    并 return status='completed' —— 磁盘上没有任何文件、task_tiles 里没有
    failed 行、downloaded_tiles 却 +1;完成判定只数 failed 行,任务照标
    completed,而 completed 任务不允许重启,用户无法原地续传自愈。
    """
    session = _FakeSession(_PNG)
    tile = Tile(task_id=1, zoom=3, x=2, y=1)

    def _boom(*a, **k):
        raise OSError(28, "No space left on device")

    # 写盘第一步就炸(等价于 ENOSPC / 只读目录 / Windows 上被占用)
    monkeypatch.setattr("aiofiles.open", _boom)

    reported = []

    async def _cb(t, status, err):
        reported.append((t.zoom, t.x, t.y, status, err))

    result = asyncio.run(engine._download_single_tile(
        tile, "s", session, cache_enabled=True, progress_callback=_cb,
    ))

    assert result["status"] == "failed", "写缓存失败绝不能上报 completed"
    assert "cache write failed" in result["error"]
    assert len(reported) == 1
    assert reported[0][3] == "failed"
    assert not engine._get_cache_path(tile, "s").exists()


def test_successful_download_still_reports_completed(engine):
    """对照:正常路径不受影响 —— 写盘成功仍报 completed 且缓存落盘。"""
    session = _FakeSession(_PNG)
    tile = Tile(task_id=1, zoom=3, x=2, y=1)

    reported = []

    async def _cb(t, status, err):
        reported.append(status)

    result = asyncio.run(engine._download_single_tile(
        tile, "s", session, cache_enabled=True, progress_callback=_cb,
    ))

    assert result["status"] == "completed"
    assert reported == ["completed"]
    cache_path = engine._get_cache_path(tile, "s")
    assert cache_path.exists() and cache_path.read_bytes() == _PNG


# ---------------------------------------------------------------------------
# M8：清缓存前检查未结束的任务
# ---------------------------------------------------------------------------

def _insert_task(app_mod, status):
    from core.database import get_connection_context
    with get_connection_context() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_format, output_path) "
            "VALUES ('t', ?, 1, 0, 1, 0, 1, 2, 's', 'tiles_only', '/tmp/x')",
            (status,),
        )
        conn.commit()
        return cur.lastrowid


def test_clear_cache_refuses_while_a_task_is_unfinished(isolated_app):
    """有 pending/running/paused 任务时清缓存必须 409 —— 而不是静默清掉,
    让运行中任务的产物缺瓦片却照报 completed。"""
    client = isolated_app.app.test_client()
    task_id = _insert_task(isolated_app, "running")

    resp = client.post("/api/cache/clear", json={"category": "__all__"})

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["active_tasks"], "响应必须点名是哪些任务挡住了清理"
    assert any(str(task_id) in label for label in body["active_tasks"])


@pytest.mark.parametrize("status", ["pending", "running", "paused"])
def test_clear_cache_blocked_for_every_live_status(isolated_app, status):
    client = isolated_app.app.test_client()
    _insert_task(isolated_app, status)
    resp = client.post("/api/cache/clear", json={"category": "__all__"})
    assert resp.status_code == 409


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_clear_cache_allowed_when_all_tasks_are_terminal(isolated_app, status):
    """终态任务不阻塞清理 —— 否则用户永远清不掉缓存。"""
    client = isolated_app.app.test_client()
    _insert_task(isolated_app, status)
    resp = client.post("/api/cache/clear", json={"category": "__all__"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_clear_cache_force_overrides_the_guard(isolated_app):
    """显式 force=true 仍可强清(用户已被前端二次询问过)。"""
    client = isolated_app.app.test_client()
    _insert_task(isolated_app, "running")

    resp = client.post("/api/cache/clear",
                       json={"category": "__all__", "force": True})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
