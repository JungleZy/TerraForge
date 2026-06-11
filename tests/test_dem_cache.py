"""
Unit tests for the DEM global cache helpers on DemDownloadEngine.

We don't exercise the full async download path here — that needs a live
Earthdata Login session. Instead we cover the two synchronous helpers
that gate the cache behavior: _try_promote_from_cache and _save_to_cache.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.dem_download_engine import DemDownloadEngine


GRANULE = "ASTGTMV003_N29E106_dem.tif"
PAYLOAD = b"fake-dem-bytes"


@pytest.fixture
def engine():
    return DemDownloadEngine()


def test_promote_returns_false_when_cache_dir_is_none(tmp_path, engine):
    dest = tmp_path / GRANULE
    assert engine._try_promote_from_cache(GRANULE, dest, None) is False
    assert not dest.exists()


def test_promote_returns_false_on_miss(tmp_path, engine):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    dest = tmp_path / "task" / GRANULE
    dest.parent.mkdir()

    assert engine._try_promote_from_cache(GRANULE, dest, cache_dir) is False
    assert not dest.exists()


def test_promote_ignores_zero_byte_cached_file(tmp_path, engine):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / GRANULE).write_bytes(b"")
    dest = tmp_path / "task" / GRANULE
    dest.parent.mkdir()

    assert engine._try_promote_from_cache(GRANULE, dest, cache_dir) is False
    assert not dest.exists()


def test_promote_hits_and_links_to_dest(tmp_path, engine):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / GRANULE
    cached.write_bytes(PAYLOAD)

    dest = tmp_path / "task" / GRANULE
    dest.parent.mkdir()

    assert engine._try_promote_from_cache(GRANULE, dest, cache_dir) is True
    assert dest.read_bytes() == PAYLOAD


def test_promote_overwrites_stale_dest(tmp_path, engine):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / GRANULE).write_bytes(PAYLOAD)

    dest = tmp_path / "task" / GRANULE
    dest.parent.mkdir()
    dest.write_bytes(b"stale-or-truncated")

    assert engine._try_promote_from_cache(GRANULE, dest, cache_dir) is True
    assert dest.read_bytes() == PAYLOAD


def test_save_no_op_when_cache_dir_is_none(tmp_path, engine):
    src = tmp_path / GRANULE
    src.write_bytes(PAYLOAD)
    # Should not raise.
    engine._save_to_cache(src, GRANULE, None)


def test_save_creates_cache_entry(tmp_path, engine):
    src = tmp_path / "task" / GRANULE
    src.parent.mkdir()
    src.write_bytes(PAYLOAD)

    cache_dir = tmp_path / "cache"  # intentionally missing
    engine._save_to_cache(src, GRANULE, cache_dir)

    cached = cache_dir / GRANULE
    assert cached.exists()
    assert cached.read_bytes() == PAYLOAD


def test_save_skips_existing_cache_entry(tmp_path, engine):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / GRANULE
    cached.write_bytes(b"previously-cached")

    src = tmp_path / "task" / GRANULE
    src.parent.mkdir()
    src.write_bytes(PAYLOAD)

    engine._save_to_cache(src, GRANULE, cache_dir)

    # Existing cache entry must not be overwritten.
    assert cached.read_bytes() == b"previously-cached"


def test_link_or_copy_falls_back_to_copy_when_link_unsupported(tmp_path, engine, monkeypatch):
    src = tmp_path / "src.bin"
    src.write_bytes(PAYLOAD)
    dst = tmp_path / "dst.bin"

    def fake_link(_a, _b):
        raise OSError("simulated EXDEV")

    monkeypatch.setattr("services.dem_download_engine.os.link", fake_link)

    DemDownloadEngine._link_or_copy(src, dst)

    assert dst.read_bytes() == PAYLOAD


def test_save_to_cache_copy_fallback_leaves_no_part_file(tmp_path, engine, monkeypatch):
    src = tmp_path / "task" / GRANULE
    src.parent.mkdir()
    src.write_bytes(PAYLOAD)
    cache_dir = tmp_path / "cache"

    def fake_link(_a, _b):
        raise OSError("simulated EXDEV")

    monkeypatch.setattr("services.dem_download_engine.os.link", fake_link)

    engine._save_to_cache(src, GRANULE, cache_dir)

    assert (cache_dir / GRANULE).read_bytes() == PAYLOAD
    assert list(cache_dir.glob("*.part.*")) == []
