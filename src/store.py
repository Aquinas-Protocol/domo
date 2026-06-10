"""Persistent state: SessionStore + task_ledger + schema migrations.

One connection per call (open/close around each operation). SQLite is in WAL
mode with a 5s busy_timeout so concurrent readers/writers from the asyncio
loop and worker threads don't block on each other.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from src.config import BILLING_CYCLE_START_DAY, SESSIONS_DB

log = logging.getLogger(__name__)

CURRENT_VERSION = 19
BUSY_TIMEOUT_MS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_cycle_start(
    cycle_day: int = BILLING_CYCLE_START_DAY,
    today: date | None = None,
) -> str:
    """Most recent billing-cycle start date (ISO ``YYYY-MM-DD``).

    Day-of-month is clamped to 1..28 so February never falls off the calendar.
    If ``today.day >= cycle_day`` the current cycle began earlier this month;
    otherwise it began ``cycle_day`` of the prior month.
    """
    today = today or datetime.now(timezone.utc).date()
    cycle_day = max(1, min(28, int(cycle_day)))
    if today.day >= cycle_day:
        return today.replace(day=cycle_day).isoformat()
    if today.month == 1:
        return today.replace(year=today.year - 1, month=12, day=cycle_day).isoformat()
    return today.replace(month=today.month - 1, day=cycle_day).isoformat()


@contextmanager
def connect(db_path: Path = SESSIONS_DB) -> Iterator[sqlite3.Connection]:
    """One connection per call. Auto-commits on exit, rolls back on exception."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


# ----------------------------- Migrations -----------------------------

def _get_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(v)}")


def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            discord_key         TEXT PRIMARY KEY,
            claude_session_id   TEXT,
            cwd                 TEXT,
            parent_discord_key  TEXT,
            source              TEXT DEFAULT 'discord',
            created_at          TEXT,
            last_used_at        TEXT
        )
        """
    )


def _migration_v2(conn: sqlite3.Connection) -> None:
    legacy = conn.execute("SELECT 1 FROM sessions WHERE discord_key='operator'").fetchone()
    main_existing = conn.execute("SELECT 1 FROM sessions WHERE discord_key='main'").fetchone()
    if legacy and not main_existing:
        conn.execute("UPDATE sessions SET discord_key='main' WHERE discord_key='operator'")
        log.info("renamed legacy session key 'operator' -> 'main'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_ledger (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_key        TEXT NOT NULL,
            agent_name         TEXT NOT NULL,
            persona_version    TEXT,
            status             TEXT NOT NULL,
            summary            TEXT,
            error_summary      TEXT,
            triggered_by       TEXT,
            parent_ledger_id   INTEGER,
            discord_msg_url    TEXT,
            claude_session_id  TEXT,
            cost_usd           REAL,
            started_at         TEXT,
            completed_at       TEXT,
            created_at         TEXT NOT NULL,
            FOREIGN KEY(discord_key) REFERENCES sessions(discord_key),
            FOREIGN KEY(parent_ledger_id) REFERENCES task_ledger(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_agent_status "
        "ON task_ledger(agent_name, status, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_parent ON task_ledger(parent_ledger_id)"
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cron_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            description     TEXT,
            cron_expr       TEXT NOT NULL,
            target_agent    TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_by      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            last_run_at     TEXT,
            next_run_at     TEXT NOT NULL,
            run_count       INTEGER NOT NULL DEFAULT 0,
            fail_count      INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_due ON cron_jobs(enabled, next_run_at)"
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cron_jobs)")}
    if "destination_channel_id" not in cols:
        conn.execute("ALTER TABLE cron_jobs ADD COLUMN destination_channel_id TEXT")
    if "oneshot" not in cols:
        conn.execute("ALTER TABLE cron_jobs ADD COLUMN oneshot INTEGER NOT NULL DEFAULT 0")


def _migration_v5(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialist_threads'"
    ).fetchone()
    if not exists:
        conn.execute(
            """
            CREATE TABLE specialist_threads (
                label             TEXT PRIMARY KEY,
                provider          TEXT NOT NULL,
                session_id        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                last_used_at      TEXT NOT NULL,
                turn_count        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        return

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(specialist_threads)")}
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE specialist_threads ADD COLUMN provider TEXT NOT NULL DEFAULT 'gpt5'"
        )
    if "session_id" not in cols:
        conn.execute(
            "ALTER TABLE specialist_threads ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
        )
    if "created_at" not in cols:
        conn.execute(
            "ALTER TABLE specialist_threads ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
        )
    if "last_used_at" not in cols:
        conn.execute(
            "ALTER TABLE specialist_threads ADD COLUMN last_used_at TEXT NOT NULL DEFAULT ''"
        )
    if "turn_count" not in cols:
        conn.execute(
            "ALTER TABLE specialist_threads ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0"
        )


def _migration_v6(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT NOT NULL UNIQUE,
            description           TEXT,
            target_agent          TEXT NOT NULL,
            template              TEXT NOT NULL,
            cadence_cron          TEXT,
            default_topic         TEXT,
            timezone              TEXT NOT NULL DEFAULT 'America/Chicago',
            enabled               INTEGER NOT NULL DEFAULT 1,
            budget_usd            REAL NOT NULL DEFAULT 7.0,
            wall_clock_minutes    INTEGER NOT NULL DEFAULT 180,
            next_run_at           TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(enabled, next_run_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL REFERENCES tasks(id),
            status                TEXT NOT NULL,
            current_phase         TEXT,
            topic                 TEXT NOT NULL,
            scheduled_for         TEXT,
            started_at            TEXT,
            completed_at          TEXT,
            deadline_at           TEXT,
            budget_usd            REAL NOT NULL,
            spent_usd             REAL NOT NULL DEFAULT 0,
            stop_reason           TEXT,
            final_brief_path      TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            UNIQUE(task_id, scheduled_for)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mission_runs_status "
        "ON mission_runs(status, task_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_steps (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_run_id        INTEGER NOT NULL REFERENCES mission_runs(id),
            phase                 TEXT NOT NULL,
            iteration             INTEGER NOT NULL,
            ledger_id             INTEGER REFERENCES task_ledger(id),
            phase_status          TEXT,
            artifact_path         TEXT,
            summary               TEXT,
            open_questions        TEXT,
            started_at            TEXT NOT NULL,
            completed_at          TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mission_steps_run_phase "
        "ON mission_steps(mission_run_id, phase, iteration)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_artifacts (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_run_id        INTEGER NOT NULL REFERENCES mission_runs(id),
            phase                 TEXT NOT NULL,
            artifact_name         TEXT NOT NULL,
            relative_path         TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            UNIQUE(mission_run_id, phase, artifact_name)
        )
        """
    )

    ledger_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_ledger)")}
    if "mission_run_id" not in ledger_cols:
        conn.execute(
            "ALTER TABLE task_ledger ADD COLUMN mission_run_id INTEGER REFERENCES mission_runs(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_mission ON task_ledger(mission_run_id)"
    )


def _migration_v7(conn: sqlite3.Connection) -> None:
    ledger_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_ledger)")}
    if "total_tokens" not in ledger_cols:
        conn.execute("ALTER TABLE task_ledger ADD COLUMN total_tokens INTEGER")


def _migration_v8(conn: sqlite3.Connection) -> None:
    """Add fresh_session flag to cron_jobs.

    Cron-fired runs reuse a per-job runner key (cron:{id}), and the runner
    resumes its prior Claude session by default. For jobs where context bleed
    across firings is harmful (e.g. an email-cull scan that should judge each
    day independently), set fresh_session=1 so the scheduler suffixes the
    runner key with the firing timestamp and bypasses session resume.
    Existing rows default to 0 — daily-brief and other in-flight jobs are
    unaffected.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cron_jobs)")}
    if "fresh_session" not in cols:
        conn.execute(
            "ALTER TABLE cron_jobs ADD COLUMN fresh_session INTEGER NOT NULL DEFAULT 0"
        )


def _migration_v9(conn: sqlite3.Connection) -> None:
    """Reserved migration slot (no-op).

    Retained so the migration version chain stays contiguous; the tables this
    slot originally created belonged to a feature not included in this build.
    """
    return


def _migration_v10(conn: sqlite3.Connection) -> None:
    """Admin-elevation requests: Discord-approved, broker-executed.

    Each row records one `request_admin_elevation` call from the operator
    agent. Status transitions are CAS-guarded in ElevationStore so a
    double-clicked button can only succeed once. The bot writes the row in
    `pending`; the persistent ElevationView flips it to `approved` or
    `denied`; the elevated broker service flips it to `executed` (or
    `broker_error`). Rows still in `pending` at bot startup are mass-flipped
    to `expired` — no resurrection of dead asyncio waits across restart.

    `command_sha256` is recomputed by the broker before execution as a
    defense-in-depth check against direct SQLite tampering.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS elevation_requests (
            uuid                TEXT PRIMARY KEY,
            reason              TEXT NOT NULL,
            command             TEXT NOT NULL,
            command_sha256      TEXT NOT NULL,
            cwd                 TEXT,
            requested_by        TEXT NOT NULL,
            requested_at        TEXT NOT NULL,
            channel_id          INTEGER,
            message_id          INTEGER,
            approval_timeout_s  INTEGER NOT NULL,
            command_timeout_s   INTEGER NOT NULL,
            status              TEXT NOT NULL,
            approved_by         TEXT,
            approved_at         TEXT,
            executed_at         TEXT,
            timed_out_at        TEXT,
            exit_code           INTEGER,
            stdout              TEXT,
            stderr              TEXT,
            error               TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_elevation_status "
        "ON elevation_requests(status, requested_at DESC)"
    )


def _migration_v11(conn: sqlite3.Connection) -> None:
    """Reserved migration slot.

    Creates two audit tables (``email_categorize_runs`` /
    ``email_categorize_log``) retained for schema-chain continuity; the
    feature that populated them is not included in this build.
    ``thread_id_opaque`` columns hold HMAC-signed opaque thread tokens.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_categorize_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at          TEXT NOT NULL,
            completed_at        TEXT,
            criteria_version    INTEGER,
            criteria_hash       TEXT,
            query_used          TEXT,
            scanned_count       INTEGER NOT NULL DEFAULT 0,
            applied_count       INTEGER NOT NULL DEFAULT 0,
            error_count         INTEGER NOT NULL DEFAULT 0,
            error_summary       TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_categorize_runs_started "
        "ON email_categorize_runs(started_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_categorize_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id              INTEGER NOT NULL REFERENCES email_categorize_runs(id),
            thread_id_opaque    TEXT NOT NULL,
            label_applied       TEXT,
            from_addr           TEXT,
            subject             TEXT,
            decided_by          TEXT NOT NULL CHECK(decided_by IN (
                                    'rule_sender',
                                    'rule_subject',
                                    'rule_amex_split',
                                    'ai_discovery'
                                )),
            reasoning           TEXT,
            applied_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_categorize_log_applied "
        "ON email_categorize_log(applied_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_categorize_log_run "
        "ON email_categorize_log(run_id)"
    )


def _migration_v12(conn: sqlite3.Connection) -> None:
    """Email-orders Phase 4a.1: Amazon order tracking.

    Categorizer extracts order events (placed / shipped / out_for_delivery /
    delivered) from Amazon emails, upserts into ``email_orders`` keyed on
    ``(vendor, order_id)``, then trashes the source email. Daily-brief reads
    a markdown mirror at ``data/email-orders.md`` for in-transit + recently
    delivered orders.

    Vendor scope is Amazon-only in v1. Schema accommodates broader vendors
    when Phase 4c expands.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor              TEXT NOT NULL,
            order_id            TEXT NOT NULL,
            status              TEXT NOT NULL CHECK(status IN (
                                    'placed',
                                    'shipped',
                                    'out_for_delivery',
                                    'delivered',
                                    'cancelled'
                                )),
            subject_last        TEXT,
            eta                 TEXT,
            tracking_number     TEXT,
            delivered_at        TEXT,
            created_at          TEXT NOT NULL,
            last_updated        TEXT NOT NULL,
            UNIQUE(vendor, order_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_orders_status_updated "
        "ON email_orders(status, last_updated DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_orders_delivered "
        "ON email_orders(delivered_at DESC) WHERE delivered_at IS NOT NULL"
    )


def _migration_v13(conn: sqlite3.Connection) -> None:
    """Reverse-recruiter Phase 1: opportunities table for Nike's job-hunt scans.

    Nike (career-intelligence agent) scans job boards via WebFetch, scores
    each listing against ``wiki/profile/job-target-criteria.md``, and writes
    rows here via ``save_opportunities``. The dashboard's Reverse Recruiter
    tab reads this table.

    Audit fields (``criteria_version``, ``criteria_hash``, ``score_version``,
    ``scored_at``) make scoring reproducible across criteria revisions and
    algorithm bumps. ``status`` is sticky for user-set values
    (``applied``/``passed``/``archived``) — re-scans never overwrite them
    (enforced in ``ReverseRecruiterStore.save_opportunities``).

    Phase 2 will add ``recruiters`` and ``applications`` tables alongside
    this one.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunities (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source              TEXT NOT NULL,
            url                 TEXT NOT NULL UNIQUE,
            company             TEXT,
            role                TEXT,
            location            TEXT,
            remote_type         TEXT,
            salary_min          INTEGER,
            salary_max          INTEGER,
            comp_band           TEXT,
            posted_at           TEXT,
            fit_score           INTEGER NOT NULL CHECK (fit_score BETWEEN 0 AND 100),
            fit_reasons_json    TEXT NOT NULL,
            score_version       INTEGER NOT NULL,
            criteria_version    INTEGER NOT NULL,
            criteria_hash       TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'new',
            status_reason       TEXT,
            status_changed_at   TEXT,
            scored_at           TEXT NOT NULL,
            found_at            TEXT NOT NULL,
            last_seen           TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunities_status_score "
        "ON opportunities(status, fit_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunities_last_seen "
        "ON opportunities(last_seen DESC)"
    )


def _migration_v14(conn: sqlite3.Connection) -> None:
    """Reverse-recruiter Phase 1.5: pipelines for "elect to deep-analyze"
    workflow.

    Each row links one opportunity to a structured gap analysis + bridge plan
    Nike generates on demand. Dylan elects an opportunity from the dashboard
    → ``awaiting_analysis`` row is created → Dylan DMs Nike to populate
    → Nike calls ``start_pipeline`` (WebFetches the JD, runs analysis, fills
    fields, flips status to ``active``) → Dylan works the bridges, appends
    notes via ``update_pipeline``, transitions to ``ready`` / ``paused`` /
    ``closed``.

    Status state machine:
      ``awaiting_analysis`` (skeleton from dashboard) →
      ``active`` (Nike populated the analysis) →
      ``paused`` | ``ready`` | ``closed`` (terminal user states).

    Multiple pipelines per opportunity are allowed (e.g. retry after closed)
    — no UNIQUE on opportunity_id. App-layer surfaces the most-recent open
    pipeline.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipelines (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id        INTEGER NOT NULL REFERENCES opportunities(id),
            status                TEXT NOT NULL DEFAULT 'awaiting_analysis',
            skill_match_summary   TEXT,
            gaps_json             TEXT,
            bridge_plan_md        TEXT,
            learning_log_md       TEXT,
            analyzed_at           TEXT,
            created_at            TEXT NOT NULL,
            last_updated          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipelines_status_updated "
        "ON pipelines(status, last_updated DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipelines_opp "
        "ON pipelines(opportunity_id, last_updated DESC)"
    )


def _migration_v15(conn: sqlite3.Connection) -> None:
    """Add style flag to cron_jobs ('verbatim' | 'reminder').

    style='verbatim' (default) keeps the strict literal-post directive — used
    for cron jobs whose payload is a structured artifact (standup posts,
    status updates, code blocks) that must reach Discord exactly as written.

    style='reminder' loosens the directive at fire time so Maine can lightly
    flavor short personal reminders ("take out the trash") with an emoji and
    a touch of warmth before posting. Existing rows default to 'verbatim' so
    in-flight jobs are unaffected.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cron_jobs)")}
    if "style" not in cols:
        conn.execute(
            "ALTER TABLE cron_jobs ADD COLUMN style TEXT NOT NULL DEFAULT 'verbatim'"
        )


def _migration_v16(conn: sqlite3.Connection) -> None:
    """Create the legacy events table for a removed subsystem. Retained verbatim
    to keep the user_version chain intact for existing databases; the table is
    created and then left unused (the feature that wrote to it is gone)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dictation_events (
            id                INTEGER PRIMARY KEY,
            created_at        TEXT NOT NULL,
            mode              TEXT NOT NULL,
            agent_key         TEXT,
            transcript_raw    TEXT,
            transcript_clean  TEXT,
            audio_bytes       INTEGER NOT NULL,
            latency_ms_stt    INTEGER,
            latency_ms_clean  INTEGER,
            latency_ms_total  INTEGER NOT NULL,
            error             TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dictation_events_created "
        "ON dictation_events(created_at DESC)"
    )


def _migration_v17(conn: sqlite3.Connection) -> None:
    """Reverse Recruiter cockpit: profiles + scan_runs + scan_run_opportunities.

    Three tables in one migration so the cockpit's launch panel + Recent Runs
    surface lands atomically. Server owns the scan_runs lifecycle (route or
    cron scheduler creates the row); Nike threads ``scan_run_id`` through her
    ``save_opportunities`` calls and closes the row via ``complete_scan_run``.
    Provenance lives in the ``scan_run_opportunities`` join so ``+ ALL`` runs
    and re-matches don't lose audit (per the plan's review feedback).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            key            TEXT PRIMARY KEY,
            label          TEXT NOT NULL,
            description    TEXT,
            criteria_path  TEXT NOT NULL,
            is_default     INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            last_updated   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_runs (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at               TEXT NOT NULL,
            ended_at                 TEXT,
            trigger                  TEXT NOT NULL,
            triggered_by             TEXT NOT NULL,
            profile_key              TEXT,
            boards_filter            TEXT,
            min_fit                  INTEGER,
            depth                    TEXT,
            status                   TEXT NOT NULL DEFAULT 'running',
            runner_key               TEXT,
            opportunities_saved      INTEGER NOT NULL DEFAULT 0,
            opportunities_viable     INTEGER NOT NULL DEFAULT 0,
            opportunities_pipelined  INTEGER NOT NULL DEFAULT 0,
            error                    TEXT,
            summary                  TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_runs_started "
        "ON scan_runs(started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_runs_status "
        "ON scan_runs(status, started_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_run_opportunities (
            scan_run_id             INTEGER NOT NULL REFERENCES scan_runs(id),
            opportunity_id          INTEGER NOT NULL REFERENCES opportunities(id),
            profile_key             TEXT,
            fit_score_at_save       INTEGER NOT NULL,
            profile_criteria_path   TEXT,
            profile_criteria_hash   TEXT,
            created_at              TEXT NOT NULL,
            UNIQUE(scan_run_id, opportunity_id, profile_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sro_scan "
        "ON scan_run_opportunities(scan_run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sro_opp "
        "ON scan_run_opportunities(opportunity_id)"
    )

    # Seed the profile registry. ``default`` points to the existing master
    # criteria file; the five named profiles match the design's PROFILES
    # array (view-cockpit-v2.jsx:164–170) and live at namespaced paths under
    # ``wiki/profile/job-target-criteria-<key>.md``.
    seeds = [
        ("default", "DEFAULT",
         "Master criteria — used for + ALL runs and as the floor for every profile.",
         "wiki/profile/job-target-criteria.md", 1),
        ("ai-security", "AI SECURITY",
         "agent-safety, prompt-injection, MCP threat modeling",
         "wiki/profile/job-target-criteria-ai-security.md", 0),
        ("ai-platform", "AI PLATFORM",
         "inference infra, vLLM, k8s, observability",
         "wiki/profile/job-target-criteria-ai-platform.md", 0),
        ("sre-reliability", "SRE / RELIABILITY",
         "k8s, terraform, on-call mature shops",
         "wiki/profile/job-target-criteria-sre-reliability.md", 0),
        ("ops-eng-hybrid", "OPS-ENG HYBRID",
         "both halves: regional-ops + backend",
         "wiki/profile/job-target-criteria-ops-eng-hybrid.md", 0),
        ("staff-backend", "STAFF BACKEND",
         "distributed systems, late-stage scaleup",
         "wiki/profile/job-target-criteria-staff-backend.md", 0),
    ]
    now = _now()
    for key, label, desc, path, is_default in seeds:
        conn.execute(
            """
            INSERT INTO profiles (key, label, description, criteria_path,
                                  is_default, created_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, label, desc, path, is_default, now, now),
        )


def _migration_v18(conn: sqlite3.Connection) -> None:
    """Credit-governor ledger fields: provider / runtime / billing bucket + split token counts.

    Pre-v18, ``task_ledger.total_tokens`` collapses every usage field the SDK
    surfaced (input, output, cache_read, cache_creation) into one integer. That
    was enough for "how many tokens did we move" but useless for cost modeling
    once Anthropic's Agent SDK credit ($100/mo on Max 5x, starts 2026-06-15)
    splits programmatic usage onto its own meter.

    This migration adds:

    - ``provider`` / ``runtime`` / ``billing_bucket`` so Codex specialist rows
      and Claude SDK rows can be aggregated separately (the credit governor's
      "remaining credit" reading is per-bucket).
    - ``billing_cycle_start`` (ISO date) frozen at write-time so historical
      rows don't re-bucket if ``BILLING_CYCLE_START_DAY`` is changed later.
    - Split token columns so cache-hit ratio can actually be computed
      (Phase 0a verification).

    Existing rows keep ``total_tokens`` untouched and get NULL for the new
    fields; read paths treat NULL ``billing_bucket`` as ``claude_agent_sdk``
    for legacy-row interpretation (everything pre-v18 was Claude SDK).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_ledger)")}
    additions = [
        ("provider", "TEXT"),
        ("runtime", "TEXT"),
        ("billing_bucket", "TEXT"),
        ("billing_cycle_start", "TEXT"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cache_creation_tokens", "INTEGER"),
        ("cache_read_tokens", "INTEGER"),
    ]
    for name, type_decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE task_ledger ADD COLUMN {name} {type_decl}")

    # Index supports the governor's hot-path query: cycle-to-date sum filtered
    # by billing_bucket. Partial index keeps it cheap on legacy NULL rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_cycle_bucket "
        "ON task_ledger(billing_cycle_start, billing_bucket) "
        "WHERE billing_cycle_start IS NOT NULL"
    )


def _migration_v19(conn: sqlite3.Connection) -> None:
    """Per-day ElevenLabs TTS character ledger (voice-notes budget gate).

    One row per UTC date; src/voice/budget.py consumes via a conditional
    UPDATE so the daily ceiling holds atomically. Deliberately separate
    accounting from task_ledger/credit_governor — that's Anthropic spend,
    this is ElevenLabs subscription credits.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_tts_usage (
            date        TEXT PRIMARY KEY,
            chars_used  INTEGER NOT NULL DEFAULT 0
        )
        """
    )


MIGRATIONS = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
    7: _migration_v7,
    8: _migration_v8,
    9: _migration_v9,
    10: _migration_v10,
    11: _migration_v11,
    12: _migration_v12,
    13: _migration_v13,
    14: _migration_v14,
    15: _migration_v15,
    16: _migration_v16,
    17: _migration_v17,
    18: _migration_v18,
    19: _migration_v19,
}


def migrate(db_path: Path = SESSIONS_DB) -> int:
    """Apply pending migrations. Returns the new version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        current = _get_version(conn)
        if current >= CURRENT_VERSION:
            return current

        if db_path.exists() and db_path.stat().st_size > 0 and current > 0:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup = db_path.with_suffix(f".sqlite.bak.{ts}")
            shutil.copy2(db_path, backup)
            log.info("backed up %s -> %s before migration", db_path.name, backup.name)

        for v in range(current + 1, CURRENT_VERSION + 1):
            log.info("applying migration v%d", v)
            MIGRATIONS[v](conn)
            _set_version(conn, v)
        return CURRENT_VERSION


# ----------------------------- SessionStore -----------------------------

class SessionStore:
    """Per-call connection wrapper around the sessions table."""

    def __init__(self, db_path: Path = SESSIONS_DB):
        self.db_path = db_path

    def get_session_id(self, key: str) -> str | None:
        with connect(self.db_path) as c:
            row = c.execute(
                "SELECT claude_session_id FROM sessions WHERE discord_key=?", (key,)
            ).fetchone()
        return row["claude_session_id"] if row and row["claude_session_id"] else None

    def exists(self, key: str) -> bool:
        with connect(self.db_path) as c:
            row = c.execute("SELECT 1 FROM sessions WHERE discord_key=?", (key,)).fetchone()
        return row is not None

    def upsert(
        self,
        key: str,
        session_id: str | None = None,
        cwd: str | None = None,
        parent_key: str | None = None,
        source: str = "discord",
    ) -> None:
        now = _now()
        with connect(self.db_path) as c:
            c.execute(
                """
                INSERT INTO sessions(discord_key, claude_session_id, cwd, parent_discord_key, source, created_at, last_used_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_key) DO UPDATE SET
                    claude_session_id = COALESCE(excluded.claude_session_id, sessions.claude_session_id),
                    cwd = COALESCE(excluded.cwd, sessions.cwd),
                    parent_discord_key = COALESCE(excluded.parent_discord_key, sessions.parent_discord_key),
                    source = excluded.source,
                    last_used_at = excluded.last_used_at
                """,
                (key, session_id, cwd, parent_key, source, now, now),
            )

    def touch(self, key: str) -> None:
        with connect(self.db_path) as c:
            c.execute("UPDATE sessions SET last_used_at=? WHERE discord_key=?", (_now(), key))


# ----------------------------- Ledger -----------------------------

class Ledger:
    """task_ledger with lifecycle (queued -> running -> completed|failed|cancelled)."""

    def __init__(self, db_path: Path = SESSIONS_DB):
        self.db_path = db_path

    def enqueue(
        self,
        discord_key: str,
        agent_name: str,
        persona_version: str | None,
        triggered_by: str = "user",
        parent_ledger_id: int | None = None,
        discord_msg_url: str | None = None,
        mission_run_id: int | None = None,
    ) -> int:
        with connect(self.db_path) as c:
            cur = c.execute(
                """
                INSERT INTO task_ledger(
                    discord_key, agent_name, persona_version, status,
                    triggered_by, parent_ledger_id, discord_msg_url,
                    mission_run_id, created_at
                )
                VALUES(?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    discord_key, agent_name, persona_version,
                    triggered_by, parent_ledger_id, discord_msg_url,
                    mission_run_id, _now(),
                ),
            )
            return cur.lastrowid

    def mark_running(self, ledger_id: int) -> None:
        with connect(self.db_path) as c:
            c.execute(
                "UPDATE task_ledger SET status='running', started_at=? WHERE id=?",
                (_now(), ledger_id),
            )

    def mark_completed(
        self,
        ledger_id: int,
        summary: str,
        claude_session_id: str | None,
        cost_usd: float | None,
        total_tokens: int | None = None,
        *,
        provider: str | None = None,
        runtime: str | None = None,
        billing_bucket: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        cache_read_tokens: int | None = None,
    ) -> None:
        # billing_cycle_start is frozen at write-time so historical rows
        # don't re-bucket if BILLING_CYCLE_START_DAY changes later. Only set
        # when a billing_bucket is given — legacy/unknown calls leave it NULL.
        cycle_start = _current_cycle_start() if billing_bucket else None
        with connect(self.db_path) as c:
            c.execute(
                """
                UPDATE task_ledger
                SET status='completed',
                    summary=?,
                    claude_session_id=?,
                    cost_usd=?,
                    total_tokens=?,
                    provider=?,
                    runtime=?,
                    billing_bucket=?,
                    billing_cycle_start=?,
                    input_tokens=?,
                    output_tokens=?,
                    cache_creation_tokens=?,
                    cache_read_tokens=?,
                    completed_at=?
                WHERE id=?
                """,
                (
                    summary,
                    claude_session_id,
                    cost_usd,
                    total_tokens,
                    provider,
                    runtime,
                    billing_bucket,
                    cycle_start,
                    input_tokens,
                    output_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                    _now(),
                    ledger_id,
                ),
            )

    def mark_failed(self, ledger_id: int, error_summary: str) -> None:
        with connect(self.db_path) as c:
            c.execute(
                """
                UPDATE task_ledger
                SET status='failed', error_summary=?, completed_at=?
                WHERE id=?
                """,
                (error_summary[:1000], _now(), ledger_id),
            )

    def mark_cancelled(self, ledger_id: int, reason: str = "cancelled") -> None:
        with connect(self.db_path) as c:
            c.execute(
                """
                UPDATE task_ledger
                SET status='cancelled', error_summary=?, completed_at=?
                WHERE id=?
                """,
                (reason[:1000], _now(), ledger_id),
            )

    def cancel_active_for_mission(self, mission_run_id: int, reason: str) -> int:
        with connect(self.db_path) as c:
            cur = c.execute(
                """
                UPDATE task_ledger
                SET status='cancelled', error_summary=?, completed_at=?
                WHERE mission_run_id=? AND status IN ('queued', 'running')
                """,
                (reason[:1000], _now(), int(mission_run_id)),
            )
            return cur.rowcount

    def by_mission(self, mission_run_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        with connect(self.db_path) as c:
            rows = c.execute(
                "SELECT * FROM task_ledger WHERE mission_run_id=? ORDER BY id ASC LIMIT ?",
                (int(mission_run_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def query(
        self,
        agent: str | None = None,
        status: str | None = None,
        since_minutes: int = 240,
        limit: int = 20,
        triggered_by_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["created_at >= datetime('now', ?)"]
        params: list[Any] = [f"-{int(since_minutes)} minutes"]
        if agent:
            clauses.append("agent_name = ?")
            params.append(agent)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if triggered_by_prefix:
            clauses.append("triggered_by LIKE ?")
            params.append(f"{triggered_by_prefix}%")
        sql = (
            "SELECT id, agent_name, persona_version, status, summary, error_summary, "
            "triggered_by, parent_ledger_id, discord_msg_url, cost_usd, total_tokens, "
            "mission_run_id, started_at, completed_at, created_at "
            "FROM task_ledger WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with connect(self.db_path) as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def billing_cycle_summary(
        self,
        bucket: str = "claude_agent_sdk",
        cycle_day: int | None = None,
    ) -> dict[str, Any]:
        """Cycle-to-date usage for one billing bucket.

        Pre-v18 rows (``billing_bucket IS NULL``) are folded into
        ``claude_agent_sdk`` when that bucket is queried — every legacy row
        was a Claude SDK call. ``cache_hit_ratio`` is ``cache_read /
        (cache_read + input_tokens)``: the share of input the model served
        from cache. Token totals exclude legacy rows because pre-v18 captures
        the wrong granularity (collapsed total_tokens only).
        """
        cycle_day = cycle_day if cycle_day is not None else BILLING_CYCLE_START_DAY
        cycle_start = _current_cycle_start(cycle_day)
        cycle_start_iso = f"{cycle_start}T00:00:00+00:00"

        params: list[Any] = [bucket, cycle_start]
        legacy_clause = ""
        if bucket == "claude_agent_sdk":
            legacy_clause = "OR (billing_bucket IS NULL AND created_at >= ?)"
            params.append(cycle_start_iso)

        sql = f"""
            SELECT
                COUNT(*) AS run_count,
                COALESCE(SUM(cost_usd), 0) AS cost_usd,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
            FROM task_ledger
            WHERE (
                (billing_bucket = ? AND billing_cycle_start = ?)
                {legacy_clause}
            )
        """

        with connect(self.db_path) as c:
            row = c.execute(sql, params).fetchone()

        cost_usd = float(row["cost_usd"] or 0)
        input_t = int(row["input_tokens"] or 0)
        output_t = int(row["output_tokens"] or 0)
        cache_create = int(row["cache_creation_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        denom = cache_read + input_t
        cache_hit_ratio = (cache_read / denom) if denom else 0.0

        return {
            "bucket": bucket,
            "cycle_start": cycle_start,
            "run_count": int(row["run_count"] or 0),
            "cost_usd": cost_usd,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_creation_tokens": cache_create,
            "cache_read_tokens": cache_read,
            "total_tokens": input_t + output_t + cache_create + cache_read,
            "cache_hit_ratio": cache_hit_ratio,
        }

    def usage_summary(self, timezone_name: str = "America/Chicago") -> dict[str, Any]:
        tz = ZoneInfo(timezone_name)
        start_of_today = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_utc = start_of_today.astimezone(timezone.utc).isoformat()

        with connect(self.db_path) as c:
            daily = c.execute(
                """
                SELECT COUNT(*) AS run_count,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM task_ledger
                WHERE created_at >= ?
                """,
                (start_utc,),
            ).fetchone()
            lifetime = c.execute(
                """
                SELECT COUNT(*) AS run_count,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COUNT(total_tokens) AS token_rows
                FROM task_ledger
                """
            ).fetchone()

        return {
            "daily_cost_usd": float(daily["cost_usd"] or 0),
            "daily_runs": int(daily["run_count"] or 0),
            "daily_reset_timezone": timezone_name,
            "daily_reset_label": "12AM CT",
            "lifetime_cost_usd": float(lifetime["cost_usd"] or 0),
            "lifetime_runs": int(lifetime["run_count"] or 0),
            "lifetime_tokens": int(lifetime["total_tokens"] or 0),
            "lifetime_token_rows": int(lifetime["token_rows"] or 0),
        }


def count_active_background_runs(ledger: Ledger) -> int:
    """Count queued/running cron and mission ledger rows."""
    total = 0
    for prefix in ("cron:", "mission:"):
        for status in ("queued", "running"):
            total += len(ledger.query(
                triggered_by_prefix=prefix,
                status=status,
                since_minutes=10080,
                limit=1000,
            ))
    return total
