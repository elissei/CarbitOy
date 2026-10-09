"""Additive Web 4A migration, run inside WorkRepository.initialize's transaction.

Revision items are inserted BEFORE their revision in the same transaction.
Their deferred FK permits this; inserting the revision seals that item set.
No executescript() is used, since its implicit commit would split the migration.
This module exposes no approval or plan-saving service.
"""

import sqlite3
from datetime import datetime, timezone


def _decimal_text(column: str, *, positive: bool) -> str:
    """Validate ordinary decimal TEXT without lossy REAL/INTEGER conversions."""
    parts = [
        f"typeof({column}) = 'text'",
        f"{column} GLOB '[0-9]*'",
        f"{column} NOT GLOB '*[^0-9.]*'",
        f"{column} NOT LIKE '%.%.%'",
        f"{column} NOT LIKE '%.'",
    ]
    if positive:
        parts.append(f"{column} GLOB '*[1-9]*'")
    return " AND ".join(parts)


def _utc_text(column: str) -> str:
    return (
        f"julianday({column}) IS NOT NULL AND "
        f"(substr({column}, -6) = '+00:00' OR substr({column}, -1) = 'Z')"
    )


def _decimal_value_text(column: str) -> str:
    """Compare valid decimal TEXT exactly despite different trailing zeroes."""
    return (
        f"ltrim(CASE WHEN instr({column}, '.') > 0 "
        f"THEN rtrim(rtrim({column}, '0'), '.') ELSE {column} END, '0')"
    )


POSITIVE_ORIGINAL = _decimal_text("original_hours", positive=True)
POSITIVE_TOTAL = _decimal_text("original_total_hours", positive=True)
NONNEGATIVE_FINAL = _decimal_text("final_billable_hours", positive=False)


MIGRATION_4A = (
    f"""
    CREATE TABLE inquiry_work_items (
        inquiry_id TEXT NOT NULL REFERENCES work_inquiries(id),
        work_item_id TEXT NOT NULL CHECK (trim(work_item_id) <> ''),
        created_at TEXT NOT NULL CHECK ({_utc_text('created_at')}),
        PRIMARY KEY (inquiry_id, work_item_id)
    )
    """,
    f"""
    CREATE TABLE labour_plan_revisions (
        revision_id TEXT PRIMARY KEY NOT NULL CHECK (trim(revision_id) <> ''),
        inquiry_id TEXT NOT NULL REFERENCES work_inquiries(id),
        revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),
        content_id TEXT NOT NULL CHECK (
            length(content_id) = 64 AND content_id NOT GLOB '*[^0-9a-f]*'
        ),
        rules_version TEXT NOT NULL CHECK (trim(rules_version) <> ''),
        coverage_status TEXT NOT NULL CHECK (coverage_status IN ('READY', 'REVIEW_REQUIRED')),
        original_total_hours TEXT,
        work_item_count INTEGER NOT NULL CHECK (
            typeof(work_item_count) = 'integer' AND work_item_count >= 1
        ),
        snapshot_json TEXT NOT NULL CHECK (
            json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'
        ),
        created_at TEXT NOT NULL CHECK ({_utc_text('created_at')}),
        CHECK (
            (coverage_status = 'READY' AND original_total_hours IS NOT NULL
             AND {POSITIVE_TOTAL})
            OR (coverage_status = 'REVIEW_REQUIRED' AND original_total_hours IS NULL)
        ),
        UNIQUE (inquiry_id, revision),
        UNIQUE (revision_id, inquiry_id),
        UNIQUE (revision_id, inquiry_id, content_id, rules_version,
                original_total_hours, coverage_status)
    )
    """,
    f"""
    CREATE TABLE labour_plan_revision_items (
        revision_id TEXT NOT NULL,
        inquiry_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 1),
        operation_identifier TEXT NOT NULL CHECK (trim(operation_identifier) <> ''),
        axle TEXT NOT NULL CHECK (axle IN ('FRONT', 'REAR', 'BOTH', 'UNKNOWN')),
        side TEXT NOT NULL CHECK (side IN ('LEFT', 'RIGHT', 'BOTH', 'NOT_APPLICABLE', 'UNKNOWN')),
        original_hours TEXT,
        source TEXT CHECK (source IN ('AUTODATA', 'MANUAL_AUTODATA')),
        work_description TEXT,
        autodata_reference TEXT,
        recorded_by TEXT,
        PRIMARY KEY (revision_id, work_item_id),
        UNIQUE (revision_id, position),
        FOREIGN KEY (inquiry_id, work_item_id)
            REFERENCES inquiry_work_items(inquiry_id, work_item_id),
        FOREIGN KEY (revision_id, inquiry_id)
            REFERENCES labour_plan_revisions(revision_id, inquiry_id)
            DEFERRABLE INITIALLY DEFERRED,
        CHECK (
            (original_hours IS NULL AND source IS NULL)
            OR (original_hours IS NOT NULL AND source IS NOT NULL AND {POSITIVE_ORIGINAL})
        ),
        CHECK (source IS NULL OR source <> 'MANUAL_AUTODATA' OR (
            work_description IS NOT NULL AND trim(work_description) <> ''
            AND recorded_by IS NOT NULL AND trim(recorded_by) <> ''
        ))
    )
    """,
    "CREATE UNIQUE INDEX idx_work_events_id_inquiry ON work_events(id, work_id)",
    f"""
    CREATE TABLE labour_review_decisions (
        decision_id TEXT PRIMARY KEY NOT NULL CHECK (trim(decision_id) <> ''),
        inquiry_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        content_id TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        revision_status TEXT NOT NULL DEFAULT 'READY' CHECK (revision_status = 'READY'),
        status TEXT NOT NULL CHECK (status IN ('APPROVED_UNCHANGED', 'APPROVED_ADJUSTED')),
        original_total_hours TEXT NOT NULL CHECK ({POSITIVE_TOTAL}),
        final_billable_hours TEXT NOT NULL CHECK ({NONNEGATIVE_FINAL}),
        reason TEXT,
        reviewer_id TEXT NOT NULL CHECK (trim(reviewer_id) <> ''),
        reviewed_at TEXT NOT NULL CHECK ({_utc_text('reviewed_at')}),
        event_id INTEGER NOT NULL UNIQUE,
        FOREIGN KEY (revision_id, inquiry_id, content_id, rules_version,
                     original_total_hours, revision_status)
            REFERENCES labour_plan_revisions(revision_id, inquiry_id, content_id,
                                             rules_version, original_total_hours,
                                             coverage_status),
        FOREIGN KEY (event_id, inquiry_id) REFERENCES work_events(id, work_id),
        CHECK (
            (status = 'APPROVED_UNCHANGED'
             AND {_decimal_value_text('final_billable_hours')} = {_decimal_value_text('original_total_hours')})
            OR (status = 'APPROVED_ADJUSTED' AND reason IS NOT NULL AND trim(reason) <> '')
        ),
        UNIQUE (decision_id, revision_id, inquiry_id)
    )
    """,
    """
    CREATE TABLE inquiry_labour_plan_state (
        inquiry_id TEXT PRIMARY KEY NOT NULL REFERENCES work_inquiries(id),
        active_revision_id TEXT NOT NULL,
        active_decision_id TEXT,
        FOREIGN KEY (active_revision_id, inquiry_id)
            REFERENCES labour_plan_revisions(revision_id, inquiry_id),
        FOREIGN KEY (active_decision_id, active_revision_id, inquiry_id)
            REFERENCES labour_review_decisions(decision_id, revision_id, inquiry_id)
    )
    """,
    "CREATE INDEX idx_labour_decisions_history ON labour_review_decisions(inquiry_id, reviewed_at, decision_id)",
    f"""
    CREATE TABLE labour_plan_requests (
        inquiry_id TEXT NOT NULL,
        actor_id TEXT NOT NULL CHECK (trim(actor_id) <> ''),
        operation TEXT NOT NULL CHECK (operation IN ('SAVE_PLAN', 'APPROVE_LABOUR')),
        idempotency_key TEXT NOT NULL CHECK (trim(idempotency_key) <> ''),
        request_json TEXT NOT NULL CHECK (json_valid(request_json) AND json_type(request_json) = 'object'),
        response_json TEXT NOT NULL CHECK (json_valid(response_json) AND json_type(response_json) = 'object'),
        revision_id TEXT NOT NULL,
        decision_id TEXT,
        result_work_version INTEGER NOT NULL CHECK (
            typeof(result_work_version) = 'integer' AND result_work_version >= 1
        ),
        created_at TEXT NOT NULL CHECK ({_utc_text('created_at')}),
        PRIMARY KEY (inquiry_id, actor_id, operation, idempotency_key),
        FOREIGN KEY (revision_id, inquiry_id)
            REFERENCES labour_plan_revisions(revision_id, inquiry_id),
        FOREIGN KEY (decision_id, revision_id, inquiry_id)
            REFERENCES labour_review_decisions(decision_id, revision_id, inquiry_id),
        CHECK (
            (operation = 'APPROVE_LABOUR' AND decision_id IS NOT NULL)
            OR (operation = 'SAVE_PLAN' AND decision_id IS NULL)
        )
    )
    """,
    """
    CREATE TRIGGER seal_revision_items BEFORE INSERT ON labour_plan_revision_items
    WHEN EXISTS (SELECT 1 FROM labour_plan_revisions WHERE revision_id = NEW.revision_id)
    BEGIN
        SELECT RAISE(ABORT, 'revision item set is immutable');
    END
    """,
    """
    CREATE TRIGGER validate_revision_items BEFORE INSERT ON labour_plan_revisions
    BEGIN
        SELECT CASE WHEN (
            SELECT count(*) FROM labour_plan_revision_items
            WHERE revision_id = NEW.revision_id AND inquiry_id = NEW.inquiry_id
        ) <> NEW.work_item_count THEN RAISE(ABORT, 'revision item count mismatch') END;
        SELECT CASE WHEN NEW.coverage_status = 'READY' AND EXISTS (
            SELECT 1 FROM labour_plan_revision_items
            WHERE revision_id = NEW.revision_id AND original_hours IS NULL
        ) THEN RAISE(ABORT, 'READY revision requires all original times') END;
    END
    """,
)


IMMUTABLE_TABLES = (
    "inquiry_work_items", "labour_plan_revisions", "labour_plan_revision_items",
    "labour_review_decisions", "labour_plan_requests",
)


IMMUTABILITY_TRIGGERS = tuple(
    f"""
    CREATE TRIGGER immutable_{table}_{action.lower()}
    BEFORE {action} ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{table} is immutable');
    END
    """
    for table in IMMUTABLE_TABLES
    for action in ("UPDATE", "DELETE")
)

# Only events already referenced by a decision are protected. Ordinary history
# INSERTs are unaffected. recursive_triggers=ON also protects REPLACE's deletion.
REVIEW_EVENT_TRIGGERS = tuple(
    f"""
    CREATE TRIGGER immutable_review_event_{action.lower()}
    BEFORE {action} ON work_events
    WHEN EXISTS (SELECT 1 FROM labour_review_decisions WHERE event_id = OLD.id)
    BEGIN
        SELECT RAISE(ABORT, 'review event is immutable');
    END
    """
    for action in ("UPDATE", "DELETE")
)

# Keep version 1 intact so databases already initialized during Web 4A upgrade
# additively. Both versions share the caller's transaction, including the ledger.
MIGRATIONS = (
    (1, MIGRATION_4A + IMMUTABILITY_TRIGGERS),
    (2, REVIEW_EVENT_TRIGGERS),
)


def apply_labour_migrations(connection: sqlite3.Connection) -> None:
    """Require the caller's transaction so schema and ledger commit together."""
    if not connection.in_transaction:
        raise RuntimeError("labour migration requires an active transaction")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS webapp_schema_migrations (
            version INTEGER PRIMARY KEY NOT NULL CHECK (version >= 1),
            applied_at TEXT NOT NULL
        )
    """)
    applied = {row[0] for row in connection.execute(
        "SELECT version FROM webapp_schema_migrations"
    )}
    for version, statements in MIGRATIONS:
        if version in applied:
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO webapp_schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
