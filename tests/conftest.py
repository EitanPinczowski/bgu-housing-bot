"""Shared test fixtures. Adds the project root to sys.path so tests can import
the flat modules (fit, storage, zones, …) and points storage at a throwaway DB."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point storage at a fresh empty SQLite file for the duration of one test.
    storage reads config.DB_PATH on every call, so patching the attribute is enough."""
    db = tmp_path / "test_listings.sqlite"
    monkeypatch.setattr(config, "DB_PATH", db)
    return db
