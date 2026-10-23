"""Additive Autodata 1B schema, run inside WorkRepository.initialize's transaction.

Migration 1 (1B-1) is kept verbatim. Migration 2 (1B-2.0) rebuilds the
Autodata tables for structural removability:
- a fully immutable lookup body holding only closed values that CARBIT parsed
  or validated itself, and the schema checks their grammar;
- one deletable payload row per lookup holding every external or staff text
  and every digest computed from them;
- decisions without identities or digests, and receipts of closed fields only.
Permitted removals:
- redaction: write autodata_redaction_events first, then delete that lookup's
  payload; the body, decision and receipt stay;
- closure: write autodata_inquiry_closures, then delete that inquiry's
  decisions, redaction events, payloads and bodies. Receipts are never
  deleted by Autodata procedures (P7). A closure blocks every later INSERT for
  the inquiry, so it can never authorize deleting newer evidence.
Migration 2 stops without changes when version-1 evidence exists; it never
empties or converts old evidence.
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

# -- migration 2: removable payload, closed body (1B-2.0) ---------------------
# Literal copies, never imported: a migration must not change when the code
# later changes. Tests assert they equal the current enums and parser units.

ADAPTERS = (("fake", "fake-1"), ("synthetic-page", "synthetic-1"))
TIME_UNITS = ("h", "hour", "hours", "hr", "hrs", "tunti", "tuntia")
TIME_REJECTIONS = ("EMPTY", "TOO_LONG", "CLOCK_FORMAT", "RANGE", "MISSING_UNIT", "UNKNOWN_UNIT",
                   "AMBIGUOUS_SEPARATOR", "NOT_POSITIVE", "MALFORMED")
LEVELS = ("VEHICLE", "OPERATION", "VARIANT")
REDACTION_REASONS = ("IDENTIFIER_IN_SOURCE_TEXT", "DATA_SUBJECT_REQUEST", "ENTERED_IN_ERROR", "RETENTION")
CLOSURE_REASONS = ("DATA_SUBJECT_REQUEST", "RETENTION", "INQUIRY_DELETION")
CLOSE_CONFIRMATION = "CLOSE_AUTODATA_PERMANENTLY"
V1_TABLES = ("autodata_decision_receipts", "autodata_labour_decisions", "autodata_redaction_events",
             "autodata_lookup_vehicle_inputs", "autodata_lookups", "autodata_inquiry_closures")
V2_EVENT_TYPE = "AUTODATA_HAKU_HYVAKSYTTY"


def _quoted(values) -> str:
    return ",".join(f"'{value}'" for value in values)


def _nonblank(column: str, limit: int = 200) -> str:
    return f"(typeof({column}) = 'text' AND length(trim({column})) BETWEEN 1 AND {limit})"


def _time_text(column: str) -> str:
    """Exactly '<number> <unit>': one space, lower-case known unit, at most one separator."""
    number = f"substr({column}, 1, instr({column}, ' ') - 1)"
    unit = f"substr({column}, instr({column}, ' ') + 1)"
    return (f"(typeof({column}) = 'text' AND instr({column}, ' ') > 1 AND {unit} IN ({_quoted(TIME_UNITS)})"
            f" AND {number} GLOB '[0-9]*' AND {number} NOT GLOB '*[^0-9.,]*' AND {number} NOT GLOB '*[.,]'"
            f" AND length({number}) - length(replace(replace({number}, '.', ''), ',', '')) <= 1)")


def _adapter(name: str, version: str) -> str:
    return "(" + " OR ".join(f"({name} = '{adapter}' AND {version} = '{adapter_version}')"
                             for adapter, adapter_version in ADAPTERS) + ")"


# The labour snapshot an acceptance writes (webapp/labour_snapshot.py), sorted.
SNAPSHOT_KEYS = ("autodata_reference", "currency", "hourly_rate", "hours", "net_price", "recorded_by",
                 "source", "total_price", "vat_price", "vat_rate", "work_description")


def _decision_snapshot(column: str) -> str:
    """A closed acceptance snapshot: exactly SNAPSHOT_KEYS, every value a JSON string.

    NULL-safe throughout (IS, never =), because SQLite treats a NULL CHECK
    result as passing. Rebuilding the object from its own required keys and
    comparing it to the stored canonical text rejects missing keys, JSON null,
    other types and any extra key.
    """
    texts = " AND ".join(f"json_type({column}, '$.{key}') IS 'text'" for key in SNAPSHOT_KEYS)
    rebuilt = "json_object(" + ", ".join(
        f"'{key}', json_extract({column}, '$.{key}')" for key in SNAPSHOT_KEYS) + ")"
    return (f"CASE WHEN typeof({column}) = 'text' AND json_valid({column}) THEN ("
            f"json_type({column}) IS 'object' AND {texts} AND {column} IS {rebuilt}"
            f" AND json_extract({column}, '$.work_description') IS ''"
            f" AND json_extract({column}, '$.source') IS 'AUTODATA'"
            f" AND json_extract({column}, '$.autodata_reference') IS 'lookup:' || lookup_id"
            f" AND json_extract({column}, '$.hours') IS hours"
            f" AND json_extract({column}, '$.recorded_by') IS decided_by) ELSE 0 END")


def _redacted(lookup: str) -> str:
    return f"EXISTS (SELECT 1 FROM autodata_redaction_events r WHERE r.lookup_id = {lookup})"


def _has_payload(lookup: str) -> str:
    return f"EXISTS (SELECT 1 FROM autodata_lookup_payloads p WHERE p.lookup_id = {lookup})"


V2_TABLES = (
    f"""CREATE TABLE autodata_inquiry_closures (
        inquiry_id TEXT PRIMARY KEY NOT NULL REFERENCES work_inquiries(id),
        organization_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        closed_by TEXT NOT NULL CHECK ({_nonblank('closed_by')}),
        closed_by_attribution_json TEXT NOT NULL CHECK (json_valid(closed_by_attribution_json)),
        closed_at TEXT NOT NULL CHECK ({_micro_utc('closed_at')}),
        reason_code TEXT NOT NULL CHECK (reason_code IN ({_quoted(CLOSURE_REASONS)})),
        row_counts_json TEXT NOT NULL CHECK (json_valid(row_counts_json)),
        FOREIGN KEY (inquiry_id, workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id, workshop_id)
    )""",
    # The body: closed values only, no text from the adapter or the browser.
    f"""CREATE TABLE autodata_lookups (
        lookup_id TEXT PRIMARY KEY NOT NULL CHECK ({_uuid('lookup_id')}),
        organization_id TEXT NOT NULL CHECK ({_nonblank('organization_id')}),
        workshop_id TEXT NOT NULL CHECK ({_nonblank('workshop_id')}),
        inquiry_id TEXT NOT NULL REFERENCES work_inquiries(id),
        auth_store_id TEXT NOT NULL CHECK ({_nonblank('auth_store_id')}),
        requested_by TEXT NOT NULL CHECK ({_nonblank('requested_by')}),
        requested_by_attribution_json TEXT NOT NULL CHECK (json_valid(requested_by_attribution_json)),
        request_id TEXT NOT NULL CHECK ({_uuid('request_id')}),
        registration_source TEXT NOT NULL CHECK (registration_source = 'INQUIRY'),
        source_work_version INTEGER NOT NULL
            CHECK (typeof(source_work_version) = 'integer' AND source_work_version >= 1),
        requested_at TEXT NOT NULL CHECK ({_micro_utc('requested_at')}),
        retrieved_at TEXT NOT NULL CHECK ({_micro_utc('retrieved_at')}),
        outcome TEXT NOT NULL CHECK (outcome IN ({_outcomes})),
        level TEXT CHECK (level IS NULL OR level IN ({_quoted(LEVELS)})),
        candidate_count INTEGER NOT NULL
            CHECK (typeof(candidate_count) = 'integer' AND candidate_count >= 0),
        hours TEXT CHECK (hours IS NULL OR {_positive_decimal('hours')}),
        time_text TEXT CHECK (time_text IS NULL OR {_time_text('time_text')}),
        rejection_code TEXT CHECK (rejection_code IS NULL OR rejection_code IN ({_quoted(TIME_REJECTIONS)})),
        adapter_name TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
        CHECK ({_adapter('adapter_name', 'adapter_version')}),
        CHECK (retrieved_at >= requested_at),
        CHECK ((outcome = 'FOUND') = (hours IS NOT NULL)),
        CHECK ((outcome = 'FOUND') = (time_text IS NOT NULL)),
        CHECK ((outcome = 'UNPARSEABLE') = (rejection_code IS NOT NULL)),
        UNIQUE (auth_store_id, requested_by, workshop_id, request_id),
        UNIQUE (lookup_id, inquiry_id, workshop_id),
        FOREIGN KEY (inquiry_id, workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id, workshop_id)
    )""",
    "CREATE INDEX idx_autodata_lookups_inquiry ON autodata_lookups(inquiry_id, retrieved_at, lookup_id)",
    # The payload: every open text and every digest of it; deleted, never updated.
    f"""CREATE TABLE autodata_lookup_payloads (
        lookup_id TEXT PRIMARY KEY NOT NULL,
        inquiry_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        registration TEXT NOT NULL CHECK (length(trim(registration)) > 0),
        vin TEXT,
        search_text TEXT NOT NULL,
        request_snapshot_json TEXT NOT NULL CHECK (json_valid(request_snapshot_json)),
        result_json TEXT NOT NULL
            CHECK (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= {MAX_RESULT_JSON_BYTES}),
        vehicle_identity_json TEXT CHECK (vehicle_identity_json IS NULL OR json_valid(vehicle_identity_json)),
        operation_identity_json TEXT
            CHECK (operation_identity_json IS NULL OR json_valid(operation_identity_json)),
        variant_identity_json TEXT CHECK (variant_identity_json IS NULL OR json_valid(variant_identity_json)),
        raw_time_text TEXT,
        result_sha256 TEXT NOT NULL CHECK ({_sha('result_sha256')}),
        command_sha256 TEXT NOT NULL CHECK ({_sha('command_sha256')}),
        snapshot_sha256 TEXT NOT NULL CHECK ({_sha('snapshot_sha256')}),
        FOREIGN KEY (lookup_id, inquiry_id, workshop_id)
            REFERENCES autodata_lookups(lookup_id, inquiry_id, workshop_id)
    )""",
    f"""CREATE TABLE autodata_redaction_events (
        redaction_id TEXT PRIMARY KEY NOT NULL CHECK ({_uuid('redaction_id')}),
        lookup_id TEXT NOT NULL UNIQUE,
        organization_id TEXT NOT NULL,
        inquiry_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        redacted_by TEXT NOT NULL CHECK ({_nonblank('redacted_by')}),
        redacted_by_attribution_json TEXT NOT NULL CHECK (json_valid(redacted_by_attribution_json)),
        redacted_at TEXT NOT NULL CHECK ({_micro_utc('redacted_at')}),
        reason_code TEXT NOT NULL CHECK (reason_code IN ({_quoted(REDACTION_REASONS)})),
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
        hours TEXT NOT NULL CHECK ({_positive_decimal('hours')}),
        time_text TEXT NOT NULL CHECK ({_time_text('time_text')}),
        confirmation TEXT NOT NULL CHECK (confirmation = '{CONFIRMATION}'),
        source_work_version INTEGER NOT NULL
            CHECK (typeof(source_work_version) = 'integer' AND source_work_version >= 1),
        resulting_work_version INTEGER NOT NULL
            CHECK (typeof(resulting_work_version) = 'integer'
                   AND resulting_work_version = source_work_version + 1),
        lookup_retrieved_at TEXT NOT NULL CHECK ({_micro_utc('lookup_retrieved_at')}),
        decided_at TEXT NOT NULL CHECK ({_micro_utc('decided_at')}),
        auth_store_id TEXT NOT NULL CHECK ({_nonblank('auth_store_id')}),
        decided_by TEXT NOT NULL CHECK ({_nonblank('decided_by')}),
        decided_by_attribution_json TEXT NOT NULL CHECK (json_valid(decided_by_attribution_json)),
        labour_snapshot_json TEXT NOT NULL,
        work_event_id INTEGER NOT NULL UNIQUE REFERENCES work_events(id),
        CONSTRAINT decision_snapshot_closed CHECK ({_decision_snapshot('labour_snapshot_json')}),
        CHECK (decided_at >= lookup_retrieved_at),
        FOREIGN KEY (lookup_id, inquiry_id, workshop_id)
            REFERENCES autodata_lookups(lookup_id, inquiry_id, workshop_id)
    )""",
    "CREATE INDEX idx_autodata_decisions_inquiry ON autodata_labour_decisions(inquiry_id, decided_at, decision_id)",
    # Closed fields only; no FK to the decision, which a closure deletes (P7).
    f"""CREATE TABLE autodata_decision_receipts (
        auth_store_id TEXT NOT NULL CHECK ({_nonblank('auth_store_id')}),
        employee_id TEXT NOT NULL CHECK ({_nonblank('employee_id')}),
        workshop_id TEXT NOT NULL CHECK ({_nonblank('workshop_id')}),
        request_id TEXT NOT NULL CHECK ({_uuid('request_id')}),
        organization_id TEXT NOT NULL CHECK ({_nonblank('organization_id')}),
        inquiry_id TEXT NOT NULL CHECK ({_nonblank('inquiry_id')}),
        lookup_id TEXT NOT NULL CHECK ({_uuid('lookup_id')}),
        expected_version INTEGER NOT NULL
            CHECK (typeof(expected_version) = 'integer' AND expected_version >= 1),
        confirmation TEXT NOT NULL CHECK (confirmation = '{CONFIRMATION}'),
        decision_id TEXT NOT NULL CHECK ({_uuid('decision_id')}),
        resulting_work_version INTEGER NOT NULL
            CHECK (typeof(resulting_work_version) = 'integer'
                   AND resulting_work_version = expected_version + 1),
        decided_at TEXT NOT NULL CHECK ({_micro_utc('decided_at')}),
        created_at TEXT NOT NULL CHECK ({_micro_utc('created_at')}),
        PRIMARY KEY (auth_store_id, employee_id, workshop_id, request_id)
    )""",
)

_CLOSED_INSERT = "SELECT CASE WHEN {closed} THEN RAISE(ABORT, 'autodata evidence of this inquiry is closed') END;"

V2_TRIGGERS = (
    f"""CREATE TRIGGER autodata_closure_insert BEFORE INSERT ON autodata_inquiry_closures BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM inquiry_workshop_assignments a
                WHERE a.inquiry_id = NEW.inquiry_id AND a.workshop_id = NEW.workshop_id
                AND a.organization_id = NEW.organization_id)
            THEN RAISE(ABORT, 'closure requires its inquiry workshop scope') END;
        END""",
    """CREATE TRIGGER autodata_closure_update BEFORE UPDATE ON autodata_inquiry_closures
        BEGIN SELECT RAISE(ABORT, 'autodata closure is immutable'); END""",
    """CREATE TRIGGER autodata_closure_delete BEFORE DELETE ON autodata_inquiry_closures
        BEGIN SELECT RAISE(ABORT, 'autodata closure is immutable'); END""",
    # Body: fully immutable; deleted only by a closure.
    f"""CREATE TRIGGER autodata_lookup_insert BEFORE INSERT ON autodata_lookups BEGIN
        {_CLOSED_INSERT.format(closed=_closed('NEW.inquiry_id'))}
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM inquiry_workshop_assignments a
                WHERE a.inquiry_id = NEW.inquiry_id AND a.workshop_id = NEW.workshop_id
                AND a.organization_id = NEW.organization_id)
            THEN RAISE(ABORT, 'lookup requires its inquiry workshop scope') END;
        END""",
    """CREATE TRIGGER autodata_lookup_update BEFORE UPDATE ON autodata_lookups
        BEGIN SELECT RAISE(ABORT, 'lookup body is immutable'); END""",
    f"""CREATE TRIGGER autodata_lookup_delete BEFORE DELETE ON autodata_lookups
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'lookup deletion requires an inquiry closure'); END""",
    # Payload: never updated; deleted by a logged redaction or a closure.
    f"""CREATE TRIGGER autodata_payload_insert BEFORE INSERT ON autodata_lookup_payloads BEGIN
        {_CLOSED_INSERT.format(closed=_closed('NEW.inquiry_id'))}
        SELECT CASE WHEN {_redacted('NEW.lookup_id')}
            THEN RAISE(ABORT, 'a redacted lookup cannot regain its payload') END;
        SELECT CASE WHEN EXISTS (SELECT 1 FROM autodata_lookups l
                WHERE l.lookup_id = NEW.lookup_id AND l.outcome = 'FOUND')
            AND (NEW.raw_time_text IS NULL OR NEW.vehicle_identity_json IS NULL
                 OR NEW.operation_identity_json IS NULL OR NEW.variant_identity_json IS NULL)
            THEN RAISE(ABORT, 'a FOUND payload requires its identities and time text') END;
        END""",
    """CREATE TRIGGER autodata_payload_update BEFORE UPDATE ON autodata_lookup_payloads
        BEGIN SELECT RAISE(ABORT, 'lookup payload is never updated'); END""",
    f"""CREATE TRIGGER autodata_payload_delete BEFORE DELETE ON autodata_lookup_payloads
        WHEN NOT ({_redacted('OLD.lookup_id')} OR {_closed('OLD.inquiry_id')})
        BEGIN SELECT RAISE(ABORT, 'payload deletion requires a logged redaction or a closure'); END""",
    # Redaction events: logged before the payload is deleted.
    f"""CREATE TRIGGER autodata_redaction_insert BEFORE INSERT ON autodata_redaction_events BEGIN
        {_CLOSED_INSERT.format(closed=_closed('NEW.inquiry_id'))}
        SELECT CASE WHEN NOT {_has_payload('NEW.lookup_id')}
            THEN RAISE(ABORT, 'redaction requires the payload') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM autodata_lookups l
                WHERE l.lookup_id = NEW.lookup_id AND l.organization_id = NEW.organization_id)
            THEN RAISE(ABORT, 'redaction requires its lookup scope') END;
        END""",
    """CREATE TRIGGER autodata_redaction_update BEFORE UPDATE ON autodata_redaction_events
        BEGIN SELECT RAISE(ABORT, 'redaction event is immutable'); END""",
    f"""CREATE TRIGGER autodata_redaction_delete BEFORE DELETE ON autodata_redaction_events
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'redaction event deletion requires an inquiry closure'); END""",
    # Decisions: bound to the FOUND body, its payload and its own event.
    f"""CREATE TRIGGER autodata_decision_insert BEFORE INSERT ON autodata_labour_decisions BEGIN
        {_CLOSED_INSERT.format(closed=_closed('NEW.inquiry_id'))}
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM autodata_lookups l
                WHERE l.lookup_id = NEW.lookup_id AND l.inquiry_id = NEW.inquiry_id
                AND l.workshop_id = NEW.workshop_id AND l.organization_id = NEW.organization_id
                AND l.outcome = 'FOUND' AND l.hours = NEW.hours AND l.time_text = NEW.time_text
                AND l.source_work_version = NEW.source_work_version
                AND l.retrieved_at = NEW.lookup_retrieved_at)
            THEN RAISE(ABORT, 'decision must match its FOUND lookup') END;
        SELECT CASE WHEN NOT {_has_payload('NEW.lookup_id')}
            THEN RAISE(ABORT, 'decision requires the unredacted payload') END;
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM work_events e
                WHERE e.id = NEW.work_event_id AND e.work_id = NEW.inquiry_id
                AND e.work_version = NEW.resulting_work_version
                AND e.event_type = '{V2_EVENT_TYPE}')
            THEN RAISE(ABORT, 'decision requires its acceptance event') END;
        END""",
    """CREATE TRIGGER autodata_decision_update BEFORE UPDATE ON autodata_labour_decisions
        BEGIN SELECT RAISE(ABORT, 'decision is immutable'); END""",
    f"""CREATE TRIGGER autodata_decision_delete BEFORE DELETE ON autodata_labour_decisions
        WHEN NOT {_closed('OLD.inquiry_id')}
        BEGIN SELECT RAISE(ABORT, 'decision deletion requires an inquiry closure'); END""",
    # Receipts: every field bound to the decision at INSERT; never updated or deleted.
    f"""CREATE TRIGGER autodata_receipt_insert BEFORE INSERT ON autodata_decision_receipts BEGIN
        {_CLOSED_INSERT.format(closed=_closed('NEW.inquiry_id'))}
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM autodata_labour_decisions d
                WHERE d.decision_id = NEW.decision_id AND d.lookup_id = NEW.lookup_id
                AND d.inquiry_id = NEW.inquiry_id AND d.workshop_id = NEW.workshop_id
                AND d.organization_id = NEW.organization_id AND d.auth_store_id = NEW.auth_store_id
                AND d.decided_by = NEW.employee_id AND d.confirmation = NEW.confirmation
                AND d.decided_at = NEW.decided_at AND d.source_work_version = NEW.expected_version
                AND d.resulting_work_version = NEW.resulting_work_version)
            THEN RAISE(ABORT, 'receipt must match its decision') END;
        END""",
    """CREATE TRIGGER autodata_receipt_update BEFORE UPDATE ON autodata_decision_receipts
        BEGIN SELECT RAISE(ABORT, 'receipt is immutable'); END""",
    """CREATE TRIGGER autodata_receipt_delete BEFORE DELETE ON autodata_decision_receipts
        BEGIN SELECT RAISE(ABORT, 'receipt is never deleted by autodata procedures'); END""",
)

# Children before parents; dropping a table also drops its triggers and indexes.
V2_STATEMENTS = tuple(f"DROP TABLE {table}" for table in V1_TABLES) + V2_TABLES + V2_TRIGGERS


def _require_no_v1_evidence(connection: sqlite3.Connection) -> None:
    """Stop, changing nothing, if any version-1 Autodata evidence exists.

    A stop signal only, not an inventory of later copies (e.g. a manual entry
    saved from prefilled text). Old evidence is never emptied or converted.
    """
    for table in V1_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise RuntimeError(f"autodata migration 2 stopped: version-1 rows exist in {table}")
    if connection.execute("SELECT 1 FROM work_events WHERE event_type = ? LIMIT 1",
                          (V2_EVENT_TYPE,)).fetchone() is not None:
        raise RuntimeError("autodata migration 2 stopped: version-1 acceptance events exist")


MIGRATIONS = ((1, TABLES + TRIGGERS), (2, V2_STATEMENTS))
PRECONDITIONS = {2: _require_no_v1_evidence}


def apply_autodata_migrations(connection: sqlite3.Connection) -> None:
    """Require the caller's transaction so schema and ledger commit together.

    A failing precondition raises inside that transaction; the caller rolls
    back the whole initialization and the database stays unchanged.
    """
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
        precondition = PRECONDITIONS.get(version)
        if precondition is not None:
            precondition(connection)
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO autodata_schema_migrations VALUES (?, ?)",
                           (version, datetime.now(timezone.utc).isoformat()))
