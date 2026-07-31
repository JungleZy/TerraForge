"""
Tests for layer json related DB schema bits.
"""

import os
import sys

import pytest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import database
from core.config import Config


def test_dem_terrain_jobs_table_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    # Override database path for isolated test run
    monkeypatch.setattr(Config, "DATABASE_PATH", Path(str(db_path)))
    # Prevent init_database() from touching the real workspace
    monkeypatch.setattr(Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(Config, "CACHE_DIR", tmp_path / "cache")

    database.init_database()

    with database.get_connection_context() as conn:
        cur = conn.cursor()

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("dem_terrain_jobs",),
        )
        assert cur.fetchone() is not None

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_dem_terrain_jobs_status",),
        )
        assert cur.fetchone() is not None

        # Enforce spec-critical constraints: output_dir/maxzoom must be NOT NULL
        cur.execute("PRAGMA table_info(dem_terrain_jobs)")
        cols = {row[1]: row for row in cur.fetchall()}  # (cid,name,type,notnull,dflt_value,pk)
        assert cols["output_dir"][3] == 1
        assert cols["maxzoom"][3] == 1


def test_patch_layer_json_parent(tmp_path):
    import json

    from services.terrain_tiling.layer_json import patch_layer_json_parent

    layer_json_path = tmp_path / "layer.json"
    layer_json_path.write_text("{}", encoding="utf-8")

    patch_layer_json_parent(layer_json_path, "https://example.com/parent")

    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    assert data["parentUrl"] == "https://example.com/parent"
