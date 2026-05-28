"""Shared fixtures for the domo test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import store


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.sqlite"
    store.migrate(db)
    return db
