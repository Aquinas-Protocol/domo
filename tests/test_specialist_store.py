"""Unit tests for Codex specialist thread persistence."""

from __future__ import annotations

from pathlib import Path

from src import store
from src.specialist_store import SpecialistStore


def test_specialist_store_upsert_get_record_turn(tmp_db: Path):
    specialist = SpecialistStore(tmp_db)

    specialist.upsert("tax-liens", "gpt5", "session-1")
    row = specialist.get("tax-liens")
    assert row is not None
    assert row["provider"] == "gpt5"
    assert row["session_id"] == "session-1"
    assert row["turn_count"] == 0

    specialist.record_turn("tax-liens")
    row = specialist.get("tax-liens")
    assert row is not None
    assert row["turn_count"] == 1

    specialist.upsert("tax-liens", "gpt5", "session-2")
    row = specialist.get("tax-liens")
    assert row is not None
    assert row["session_id"] == "session-2"
    assert row["turn_count"] == 1


def test_migration_v5_adds_specialist_threads_and_is_idempotent(tmp_db: Path):
    assert store.migrate(tmp_db) == store.CURRENT_VERSION
    with store.connect(tmp_db) as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(specialist_threads)")
        }
    assert {
        "label",
        "provider",
        "session_id",
        "created_at",
        "last_used_at",
        "turn_count",
    }.issubset(cols)


def test_migration_v5_recovers_partial_table(tmp_path: Path):
    db = tmp_path / "partial.sqlite"
    with store.connect(db) as conn:
        for version in range(1, 5):
            store.MIGRATIONS[version](conn)
            store._set_version(conn, version)
        conn.execute("CREATE TABLE specialist_threads (label TEXT PRIMARY KEY)")

    store.migrate(db)

    with store.connect(db) as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(specialist_threads)")
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == store.CURRENT_VERSION
    assert {
        "provider",
        "session_id",
        "created_at",
        "last_used_at",
        "turn_count",
    }.issubset(cols)
