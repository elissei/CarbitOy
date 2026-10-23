"""Additive Autodata 1B schema, run inside WorkRepository.initialize's transaction.

Lookups, decisions, receipts and redaction events are immutable evidence. The
only permitted changes are:
- redaction: write autodata_redaction_events first, then NULL the personal
  inputs and the two lookup digests;
- closure: write autodata_inquiry_closures, then delete that inquiry's rows in
  dependency order. A closure also blocks every later INSERT for the inquiry,
  so a permanent closure can never authorize deleting newer evidence.
The 24-hour acceptance age is checked in Python only; SQLite's julianday uses
floating point, so the schema checks timestamp format and ordering only.
"""

import sqlite3
from datetime import datetime, timezone


OUTCOMES = (
    "FOUND", "VEHICLE_AMBIGUOUS", "VEHICLE_NOT_FOUND", "WORK_AMBIGUOUS", "WORK_NOT_FOUND",
    "VARIANT_AMBIGUOUS", "TIME_MISSING", "UNPARSEABLE", "SELECTION_INVALID",
    "SELECTION_NOT_UNIQUE", "LOGIN_REQUIRED", "BLOCKED", "SITE_CHANGED", "TIMEOUT", "ERROR",
)
MAX_RESULT_JSON_BYTES = 65536
CONFIRMATION = "VEHICLE_OPERATION_VARIANT_TIME"


def _micro_utc(column: str) -> str:
    """Fixed-width 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00', so text order is time order."""
    digit = "[0-9]"
    pattern = (f"{digit * 4}-{digit * 2}-{digit * 2}T{digit * 2}:{digit * 2}:{digit * 2}"
               f".{digit * 6}+00:00")
    return f"(length({column}) = 32 AND {column} GLOB '{pattern}' AND julianday({column}) IS NOT NULL)"


def _uuid(column: str) -> str:
    return f"(length({column}) = 36 AND {column} GLOB '{'[0-9a-f]' * 8}-*')"


def _sha(column: str) -> str:
    return f"(length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*')"


def _positive_decimal(column: str) -> str:
    return (f"(typeof({column}) = 'text' AND {column} GLOB '[0-9]*' AND {column} NOT GLOB '*[^0-9.]*'"
            f" AND {column} NOT LIKE '%.%.%' AND {column} NOT LIKE '%.' AND {column} GLOB '*[1-9]*')")


def _closed(inquiry: str) -> str:
    return f"EXISTS (SELECT 1 FROM autodata_inquiry_closures c WHERE c.inquiry_id = {inquiry})"


_outcomes = ",".join(f"'{outcome}'" for outcome in OUTCOMES)

TABLES = (
    f"""CREATE TABLE autodata_inquiry_closures (
        inquiry_id TEXT PRIMARY KEY NOT NULL REFERENCES work_inquiries(id),
        workshop_id TEXT NOT NULL,
        closed_by TEXT NOT NULL CHECK ({_uuid('closed_by')}),
        closed_by_attribution_json TEXT NOT NULL CHECK (json_valid(closed_by_attribution_json)),
        closed_at TEXT NOT NULL CHECK ({_micro_utc('closed_at')}),
        reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
        row_counts_json TEXT NOT NULL CHECK (json_valid(row_counts_json)),
        FOREIGN KEY (inquiry_id, workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id, workshop_id)
    )""",
    f"""CREATE TABLE autodata_lookups (
        lookup_id TEXT PRIMARY KEY NOT NULL CHECK ({_uuid('lookup_id')}),
        organization_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        inquiry_id TEXT NOT NULL REFERENCES work_inquiries(id),
        auth_store_id TEXT NOT NULL CHECK (length(trim(auth_store_id)) > 0),
        requested_by TEXT NOT NULL CHECK (length(trim(requested_by)) > 0),
        requested_by_attribution_json TEXT NOT NULL CHECK (json_valid(requested_by_attribution_json)),
        request_id TEXT NOT NULL CHECK ({_uuid('request_id')}),
        command_sha256 TEXT CHECK (command_sha256 IS NULL OR {_sha('command_sha256')}),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        snapshot_sha256 TEXT CHECK (snapshot_sha256 IS NULL OR {_sha('snapshot_sha256')}),
        registration_source TEXT NOT NULL CHECK (registration_source = 'INQUIRY'),
        source_work_version INTEGER NOT NULL
            CHECK (typeof(source_work_version) = 'integer' AND source_work_version >= 1),
        requested_at TEXT NOT NULL CHECK ({_micro_utc('requested_at')}),
        retrieved_at TEXT NOT NULL CHECK ({_micro_utc('retrieved_at')}),
        outcome TEXT NOT NULL CHECK (outcome IN ({_outcomes})),
        result_json TEXT NOT NULL
            CHECK (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= {MAX_RESULT_JSON_BYTES}),
        result_sha256 TEXT NOT NULL CHECK ({_sha('result_sha256')}),
        hours TEXT CHECK (hours IS NULL OR {_positive_decimal('hours')}),
        raw_time_text TEXT,
        vehicle_identity_json TEXT CHECK (vehicle_identity_json IS NULL OR json_valid(vehicle_identity_json)),
        operation_identity_json TEXT
            CHECK (operation_identity_json IS NULL OR json_valid(operation_identity_json)),
        variant_identity_json TEXT CHECK (variant_identity_json IS NULL OR json_valid(variant_identity_json)),
        adapter_name TEXT NOT NULL CHECK (length(trim(adapter_name)) BETWEEN 1 AND 100),
        adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) BETWEEN 1 AND 40),
        synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
        CHECK (retrieved_at >= requested_at),
        CHECK ((outcome = 'FOUND') = (hours IS NOT NULL)),
        CHECK ((outcome = 'FOUND') = (raw_time_text IS NOT NULL)),
        CHECK (outcome <> 'FOUND' OR (vehicle_identity_json IS NOT NULL
               AND operation_identity_json IS NOT NULL AND variant_identity_json IS NOT NULL)),
        UNIQUE (auth_store_id, requested_by, workshop_id, request_id),
        UNIQUE (lookup_id, inquiry_id, workshop_id),
        FOREIGN KEY (inquiry_id, workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id, workshop_id)
    )""",
    "CREATE INDEX idx_autodata_lookups_inquiry ON autodata_lookups(inquiry_id, retrieved_at, lookup_id)",
    f"""CREATE TABLE autodata_lookup_vehicle_inputs (
        lookup_id TEXT PRIMARY KEY NOT NULL,
        inquiry_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        registration TEXT,
        vin TEXT,
        search_text TEXT,
        redacted_at TEXT CHECK (redacted_at IS NULL OR {_micro_utc('redacted_at')}),
        redacted_by TEXT,
        redaction_reason TEXT,
        CHECK ((redacted_at IS NULL AND registration IS NOT NULL AND search_text IS NOT NULL
                AND redacted_by IS NULL AND redaction_reason IS NULL)
            OR (redacted_at IS NOT NULL AND registration IS NULL AND vin IS NULL AND search_text IS NULL
                AND redacted_by IS NOT NULL AND length(trim(redacted_by)) > 0
                AND redaction_reason IS NOT NULL AND length(trim(redaction_reason)) BETWEEN 1 AND 500)),
        FOREIGN KEY (lookup_id, inquiry_id, workshop_id)
            REFERENCES autodata_lookups(lookup_id, inquiry_id, workshop_id)
    )""",
    f"""CREATE TABLE autodata_redaction_events (
        redaction_id TEXT PRIMARY KEY NOT NULL CHECK ({_uuid('redaction_id')}),
        lookup_id TEXT NOT NULL UNIQUE,
        inquiry_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        redacted_by TEXT NOT NULL CHECK (length(trim(redacted_by)) > 0),
        redacted_by_attribution_json TEXT NOT NULL CHECK (json_valid(redacted_by_attribution_json)),
        redacted_at TEXT NOT NULL CHECK ({_micro_utc('redacted_at')}),
        reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
        FOREIGN KEY (lookup_id, inquiry_id, workshop_id)
            REFERENCES autodata_lookups(lookup_id, inquiry_id, workshop_id)
    )""",
    f"""CREATE TABLE autodata_labour_decisions (
        decision_id TEXT PRIMARY KEY NOT NULL CHECK ({_uuid('decision_id')}),
        lookup_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        inquiry_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision = 'ACCEPTED'),
        vehicle_identity_json TEXT NOT NULL CHECK (json_valid(vehicle_identity_json)),
        operation_identity_json TEXT NOT NULL CHECK (json_valid(operation_identity_json)),
        variant_identity_json TEXT NOT NULL CHECK (json_valid(variant_identity_json)),
        hours TEXT NOT NULL CHECK ({_positive_decimal('hours')}),
        raw_time_text TEXT NOT NULL,
        result_sha256 TEXT NOT NULL CHECK ({_sha('result_sha256')}),
        confirmation TEXT NOT NULL CHECK (confirmation = '{CONFIRMATION}'),
        source_work_version INTEGER NOT NULL
            CHECK (typeof(source_work_version) = 'integer' AND source_work_version >= 1),
        resulting_work_version INTEGER NOT NULL
            CHECK (typeof(resulting_work_version) = 'integer'
                   AND resulting_work_version = source_work_version + 1),
        lookup_retrieved_at TEXT NOT NULL CHECK ({_micro_utc('lookup_retrieved_at')}),
        decided_at TEXT NOT NULL CHECK ({_micro_utc('decided_at')}),
        decided_by TEXT NOT NULL CHECK (length(trim(decided_by)) > 0),
        decided_by_attribution_json TEXT NOT NULL CHECK (json_valid(decided_by_attribution_json)),
        labour_snapshot_json TEXT NOT NULL CHECK (json_valid(labour_snapshot_json)),
        work_event_id INTEGER NOT NULL UNIQUE REFERENCES work_events(id),
        CHECK (decided_at >= lookup_retrieved_at),
        FOREIGN KEY (lookup_id, inquiry_id, workshop_id)
            REFERENCES autodata_lookups(lookup_id, inquiry_id, workshop_id)
    )""",
    "CREATE INDEX idx_autodata_decisions_inquiry ON autodata_labour_decisions(inquiry_id, decided_at, decision_id)",
    f"""CREATE TABLE autodata_decision_receipts (
        auth_store_id TEXT NOT NULL CHECK (length(trim(auth_store_id)) > 0),
        employee_id TEXT NOT NULL CHECK (length(trim(employee_id)) > 0),
        workshop_id TEXT NOT NULL,
        request_id TEXT NOT NULL CHECK ({_uuid('request_id')}),
        inquiry_id TEXT NOT NULL,
        lookup_id TEXT NOT NULL,
        command_sha256 TEXT NOT NULL CHECK ({_sha('command_sha256')}),
        decision_id TEXT NOT NULL REFERENCES autodata_labour_decisions(decision_id),
        response_json TEXT NOT NULL CHECK (json_valid(response_json)),
        created_at TEXT NOT NULL CHECK ({_micro_utc('created_at')}),
        PRIMARY KEY (auth_store_id, employee_id, workshop_id, request_id)
    )""",
)

_LOOKUP_COLUMNS = (
    "lookup_id", "organization_id", "workshop_id", "inquiry_id", "auth_store_id", "requested_by",
    "requested_by_attribution_json", "request_id", "snapshot_json", "registration_source",
    "source_work_version", "requested_at", "retrieved_at", "outcome", "result_json",
    "result_sha256", "hours", "raw_time_text", "vehicle_identity_json", "operation_identity_json",
    "variant_identity_json", "adapter_name", "adapter_version", "synthetic",
)
_INPUT_KEYS = ("lookup_id", "inquiry_id", "workshop_id")
_unchanged = " AND ".join(f"NEW.{column} IS OLD.{column}" for column in _LOOKUP_COLUMNS)
_inputs_unchanged = " AND ".join(f"NEW.{column} IS OLD.{column}" for column in _INPUT_KEYS)
_redaction_logged = ("EXISTS (SELECT 1 FROM autodata_redaction_events r "
                     "WHERE r.lookup_id = NEW.lookup_id)")

TRIGGERS = (
    # Closure: immutable, and only for an assigned inquiry of the same workshop.
    """CREATE TRIGGER autodata_closure_update BEFORE UPDATE ON autodata_inquiry_closures
        BEGIN SELECT RAISE(ABORT, 'autodata closure is immutable'); END""",
    """CREATE TRIGGER autodata_closure_delete BEFORE DELETE ON autodata_inquiry_closures
        BEGIN SELECT RAISE(ABORT, 'autodata closure is immutable'); END""",
    # Lookups.
    f"""CREATE TRIGGER autodata_lookup_insert BEFORE INSERT ON autodata_lookups BEGIN
        SELECT CASE WHEN {_closed('NEW.inquiry_id')}
            THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM inquiry_workshop_assignments a
                WHERE a.inquiry_id = NEW.inquiry_id AND a.workshop_id = NEW.workshop_id
                AND a.organization_id = NEW.organization_id)
            THEN RAISE(ABORT, 'lookup requires its inquiry workshop scope') END;
        SELECT CASE WHEN NEW.command_sha256 IS NULL OR NEW.snapshot_sha256 IS NULL
            THEN RAISE(ABORT, 'a new lookup requires both digests') END;
        END""",
    f"""CREATE TRIGGER autodata_lookup_update BEFORE UPDATE ON autodata_lookups
        WHEN NOT (NEW.command_sha256 IS NULL AND NEW.snapshot_sha256 IS NULL
                  AND {_unchanged} AND {_redaction_logged})
        BEGIN SELECT RAISE(ABORT, 'lookup permits only a logged redaction'); END""",
    f"""CREATE TRIGGER autodata_lookup_delete BEFORE DELETE ON autodata_lookups
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'lookup deletion requires an inquiry closure'); END""",
    # Personal inputs.
    f"""CREATE TRIGGER autodata_inputs_insert BEFORE INSERT ON autodata_lookup_vehicle_inputs BEGIN
        SELECT CASE WHEN {_closed('NEW.inquiry_id')}
            THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;
        SELECT CASE WHEN NEW.redacted_at IS NOT NULL
            THEN RAISE(ABORT, 'inputs cannot be inserted redacted') END;
        END""",
    f"""CREATE TRIGGER autodata_inputs_update BEFORE UPDATE ON autodata_lookup_vehicle_inputs
        WHEN NOT (OLD.redacted_at IS NULL AND NEW.redacted_at IS NOT NULL
                  AND NEW.redacted_by IS NOT NULL AND NEW.redaction_reason IS NOT NULL
                  AND {_inputs_unchanged} AND {_redaction_logged})
        BEGIN SELECT RAISE(ABORT, 'inputs permit only one logged redaction'); END""",
    f"""CREATE TRIGGER autodata_inputs_delete BEFORE DELETE ON autodata_lookup_vehicle_inputs
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'input deletion requires an inquiry closure'); END""",
    # Redaction events.
    f"""CREATE TRIGGER autodata_redaction_insert BEFORE INSERT ON autodata_redaction_events BEGIN
        SELECT CASE WHEN {_closed('NEW.inquiry_id')}
            THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;
        END""",
    """CREATE TRIGGER autodata_redaction_update BEFORE UPDATE ON autodata_redaction_events
        BEGIN SELECT RAISE(ABORT, 'redaction event is immutable'); END""",
    f"""CREATE TRIGGER autodata_redaction_delete BEFORE DELETE ON autodata_redaction_events
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'redaction event deletion requires an inquiry closure'); END""",
    # Decisions: must copy the FOUND lookup exactly and reference its own event.
    f"""CREATE TRIGGER autodata_decision_insert BEFORE INSERT ON autodata_labour_decisions BEGIN
        SELECT CASE WHEN {_closed('NEW.inquiry_id')}
            THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM autodata_lookups l
                WHERE l.lookup_id = NEW.lookup_id AND l.inquiry_id = NEW.inquiry_id
                AND l.workshop_id = NEW.workshop_id AND l.organization_id = NEW.organization_id
                AND l.outcome = 'FOUND' AND l.hours = NEW.hours AND l.raw_time_text = NEW.raw_time_text
                AND l.result_sha256 = NEW.result_sha256
                AND l.vehicle_identity_json = NEW.vehicle_identity_json
                AND l.operation_identity_json = NEW.operation_identity_json
                AND l.variant_identity_json = NEW.variant_identity_json
                AND l.source_work_version = NEW.source_work_version
                AND l.retrieved_at = NEW.lookup_retrieved_at)
            THEN RAISE(ABORT, 'decision must copy its FOUND lookup') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM work_events e
                WHERE e.id = NEW.work_event_id AND e.work_id = NEW.inquiry_id
                AND e.work_version = NEW.resulting_work_version
                AND e.event_type = 'AUTODATA_HAKU_HYVAKSYTTY')
            THEN RAISE(ABORT, 'decision requires its acceptance event') END;
        END""",
    """CREATE TRIGGER autodata_decision_update BEFORE UPDATE ON autodata_labour_decisions
        BEGIN SELECT RAISE(ABORT, 'decision is immutable'); END""",
    f"""CREATE TRIGGER autodata_decision_delete BEFORE DELETE ON autodata_labour_decisions
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'decision deletion requires an inquiry closure'); END""",
    # Receipts.
    f"""CREATE TRIGGER autodata_receipt_insert BEFORE INSERT ON autodata_decision_receipts BEGIN
        SELECT CASE WHEN {_closed('NEW.inquiry_id')}
            THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM autodata_labour_decisions d
                WHERE d.decision_id = NEW.decision_id AND d.inquiry_id = NEW.inquiry_id
                AND d.lookup_id = NEW.lookup_id AND d.workshop_id = NEW.workshop_id)
            THEN RAISE(ABORT, 'receipt requires its decision') END;
        END""",
    """CREATE TRIGGER autodata_receipt_update BEFORE UPDATE ON autodata_decision_receipts
        BEGIN SELECT RAISE(ABORT, 'receipt is immutable'); END""",
    f"""CREATE TRIGGER autodata_receipt_delete BEFORE DELETE ON autodata_decision_receipts
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'receipt deletion requires an inquiry closure'); END""",
)

MIGRATIONS = ((1, TABLES + TRIGGERS),)


def apply_autodata_migrations(connection: sqlite3.Connection) -> None:
    """Require the caller's transaction so schema and ledger commit together."""
    if not connection.in_transaction:
        raise RuntimeError("autodata migration requires an active transaction")
    connection.execute("""CREATE TABLE IF NOT EXISTS autodata_schema_migrations (
        version INTEGER PRIMARY KEY NOT NULL CHECK (version >= 1), applied_at TEXT NOT NULL
    )""")
    applied = {row[0] for row in connection.execute("SELECT version FROM autodata_schema_migrations")}
    if applied - {version for version, _ in MIGRATIONS}:
        raise RuntimeError("unknown autodata schema version")
    for version, statements in MIGRATIONS:
        if version in applied:
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO autodata_schema_migrations VALUES (?, ?)",
                           (version, datetime.now(timezone.utc).isoformat()))
