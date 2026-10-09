import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from webapp.database import WorkRepository
from webapp.labour_migrations import apply_labour_migrations
from webapp.labour_models import (
    CoverageStatus, LabourPlanRevisionRecord, OriginalWorkTimeRecord, decimal_storage_text,
)
from webapp.main import create_app


NOW = "2026-10-09T12:00:00+00:00"
NEW_TABLES = {
    "webapp_schema_migrations", "inquiry_work_items", "labour_plan_revisions",
    "labour_plan_revision_items", "labour_review_decisions",
    "inquiry_labour_plan_state", "labour_plan_requests",
}


@pytest.fixture
def database(tmp_path):
    repository = WorkRepository(tmp_path / "test.db")
    repository.initialize()
    work = repository.create_work(
        registration="TEST-123", customer_description="Original inquiry",
        actor="Recorder", idempotency_key=str(uuid4()),
    )
    with repository._connect() as connection:
        yield repository, connection, work["id"]


def make_revision(inquiry_id, revision=1, hours=("1.60", "2.00"), ready=True):
    items = tuple(OriginalWorkTimeRecord(
        work_item_id=f"work-{position}", operation_identifier="SHOCK_ABSORBER_REPLACEMENT",
        axle="FRONT", side="LEFT" if position == 1 else "RIGHT",
        original_hours=value, source="MANUAL_AUTODATA" if value is not None else None,
        work_description="Original operation", autodata_reference=f"REF-{position}",
        recorded_by="Recorder",
    ) for position, value in enumerate(hours, 1))
    total = sum((Decimal(value) for value in hours), Decimal("0")) if ready else None
    return LabourPlanRevisionRecord(
        revision_id=f"{inquiry_id}-revision-{revision}", inquiry_id=inquiry_id,
        revision=revision, content_id=str(revision % 10) * 64, rules_version="3B-v1",
        coverage_status="READY" if ready else "REVIEW_REQUIRED",
        original_total_hours=total, items=items, created_at=datetime.now(timezone.utc),
    )


def insert_revision(connection, record):
    """Schema fixture only; no production save/approval service is implemented."""
    for position, item in enumerate(record.items, 1):
        connection.execute(
            "INSERT OR IGNORE INTO inquiry_work_items VALUES (?, ?, ?)",
            (record.inquiry_id, item.work_item_id, NOW),
        )
        connection.execute("""
            INSERT INTO labour_plan_revision_items (
                revision_id, inquiry_id, work_item_id, position, operation_identifier,
                axle, side, original_hours, source, work_description,
                autodata_reference, recorded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.revision_id, record.inquiry_id, item.work_item_id, position,
            item.operation_identifier, item.axle, item.side,
            decimal_storage_text(item.original_hours) if item.original_hours is not None else None,
            item.source.value if item.source is not None else None,
            item.work_description, item.autodata_reference, item.recorded_by,
        ))
    connection.execute("""
        INSERT INTO labour_plan_revisions (
            revision_id, inquiry_id, revision, content_id, rules_version,
            coverage_status, original_total_hours, work_item_count, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.revision_id, record.inquiry_id, record.revision, record.content_id,
        record.rules_version, record.coverage_status.value,
        decimal_storage_text(record.original_total_hours) if record.original_total_hours is not None else None,
        len(record.items), record.model_dump_json(), NOW,
    ))


def insert_decision(connection, record, decision_id="decision-1", final="0.00", **changes):
    values = dict(
        decision_id=decision_id, inquiry_id=record.inquiry_id, revision_id=record.revision_id,
        content_id=record.content_id, rules_version=record.rules_version,
        status="APPROVED_ADJUSTED", original_total_hours=(
            decimal_storage_text(record.original_total_hours)
            if record.original_total_hours is not None else None
        ),
        final_billable_hours=final, reason="Explicit workshop decision",
        reviewer_id="employee-stable-id", reviewed_at=NOW,
    )
    values.update(changes)
    event = connection.execute("""
        INSERT INTO work_events (work_id, occurred_at, event_type, actor, details_json, work_version)
        VALUES (?, ?, 'LABOUR_REVIEW_DECIDED', ?, ?, 1)
    """, (record.inquiry_id, NOW, "employee-stable-id", json.dumps(values))).lastrowid
    values["event_id"] = event
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO labour_review_decisions ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def insert_request(connection, record, key="request-1", **changes):
    values = dict(
        inquiry_id=record.inquiry_id, actor_id="employee-stable-id", operation="SAVE_PLAN",
        idempotency_key=key, request_json='{"intent":"save"}', response_json='{"saved":true}',
        revision_id=record.revision_id, decision_id=None, result_work_version=7, created_at=NOW,
    )
    values.update(changes)
    connection.execute(
        f"INSERT INTO labour_plan_requests ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()),
    )


def test_new_database_has_additive_schema_and_foreign_keys(database):
    _, connection, _ = database
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert NEW_TABLES <= tables
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert [row[0] for row in connection.execute("SELECT version FROM webapp_schema_migrations ORDER BY version")] == [1, 2]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_database_is_upgraded_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    # Independent pre-4A schema, including the earliest event layout.
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE work_inquiries (
                id TEXT PRIMARY KEY, registration TEXT NOT NULL,
                customer_description TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('KESKEN')),
                version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                labour_hours TEXT, labour_source TEXT, labour_work_description TEXT,
                labour_recorded_by TEXT, autodata_reference TEXT, hourly_rate TEXT,
                total_price TEXT, net_price TEXT, vat_price TEXT, vat_rate TEXT, currency TEXT
            );
            CREATE TABLE work_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL REFERENCES work_inquiries(id),
                occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE inquiry_creation_requests (
                idempotency_key TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                work_id TEXT NOT NULL REFERENCES work_inquiries(id), response_json TEXT NOT NULL
            );
            INSERT INTO work_inquiries VALUES ('legacy','TEST-OLD','Original description','KESKEN',7,
                '2026-10-01','2026-10-01','1.60','MANUAL_AUTODATA','Original work','Recorder','REF',
                '90.00','144.00','114.74','29.26','0.255','EUR');
            INSERT INTO work_events VALUES (7,'legacy','2026-10-01','AUTODATA_TYOAIKA_KIRJATTU',
                'Recorder','{"hours":"1.60"}');
            INSERT INTO inquiry_creation_requests VALUES ('creation-key','{"original":true}','legacy','{"id":"legacy"}');
        """)
        before_work = connection.execute("SELECT * FROM work_inquiries").fetchall()
        before_events = connection.execute("SELECT * FROM work_events").fetchall()
        before_requests = connection.execute("SELECT * FROM inquiry_creation_requests").fetchall()
        before_columns = connection.execute("PRAGMA table_info(work_inquiries)").fetchall()

    repository = WorkRepository(path)
    repository.initialize()
    repository.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM work_inquiries").fetchall() == before_work
        assert connection.execute("SELECT * FROM work_events").fetchall() == [(*before_events[0], None)]
        assert connection.execute("SELECT * FROM inquiry_creation_requests").fetchall() == before_requests
        assert connection.execute("PRAGMA table_info(work_inquiries)").fetchall() == before_columns
        for table in NEW_TABLES - {"webapp_schema_migrations"}:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert repository.get_work("legacy")["version"] == 7
    assert repository.get_work("legacy")["status"] == "KESKEN"


def test_reinitialization_preserves_new_schema_data(database):
    repository, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
    before = [tuple(row) for row in connection.execute("SELECT * FROM labour_review_decisions")]

    repository.initialize()
    repository.initialize()

    assert [tuple(row) for row in connection.execute("SELECT * FROM labour_review_decisions")] == before
    assert connection.execute("SELECT count(*) FROM webapp_schema_migrations").fetchone()[0] == 2


def test_final_zero_is_text_and_original_time_is_preserved(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
    row = connection.execute("SELECT original_total_hours, final_billable_hours, typeof(final_billable_hours) FROM labour_review_decisions").fetchone()
    assert tuple(row) == ("3.60", "0.00", "text")
    assert [row[0] for row in connection.execute("SELECT original_hours FROM labour_plan_revision_items ORDER BY position")] == ["1.60", "2.00"]


def test_small_original_and_large_final_hours_are_exact_text(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id, hours=("1E-7", "1.60"))
    final = "12345678901234567890.123456789"
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record, final=final)
    assert connection.execute("SELECT original_hours FROM labour_plan_revision_items WHERE position=1").fetchone()[0] == "0.0000001"
    assert connection.execute("SELECT original_total_hours FROM labour_plan_revisions").fetchone()[0] == "1.6000001"
    assert connection.execute("SELECT final_billable_hours FROM labour_review_decisions").fetchone()[0] == final


@pytest.mark.parametrize("final", ["3.60", "3.6", "03.6000"])
def test_unchanged_decision_compares_decimal_value_without_float_conversion(database, final):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record, final=final, status="APPROVED_UNCHANGED", reason=None)
    assert connection.execute("SELECT final_billable_hours FROM labour_review_decisions").fetchone()[0] == final


def test_revisions_and_decision_history_are_separate_from_inquiry_version(database):
    repository, connection, inquiry_id = database
    first, second = make_revision(inquiry_id), make_revision(inquiry_id, 2, ("1.70", "2.00"))
    with connection:
        insert_revision(connection, first)
        insert_decision(connection, first)
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, ?)", (inquiry_id, first.revision_id, "decision-1"))
    with connection:
        insert_revision(connection, second)
        connection.execute("UPDATE inquiry_labour_plan_state SET active_revision_id=?, active_decision_id=NULL WHERE inquiry_id=?", (second.revision_id, inquiry_id))
        insert_decision(connection, second, "decision-2", final="2.75")
    assert connection.execute("SELECT count(*) FROM labour_plan_revisions").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM labour_review_decisions").fetchone()[0] == 2
    assert connection.execute("SELECT active_revision_id, active_decision_id FROM inquiry_labour_plan_state").fetchone()[1] is None
    assert repository.get_work(inquiry_id)["version"] == 1
    assert connection.execute("SELECT final_billable_hours FROM labour_review_decisions WHERE decision_id='decision-1'").fetchone()[0] == "0.00"


@pytest.mark.parametrize("table", ["inquiry_work_items", "labour_plan_revisions", "labour_plan_revision_items", "labour_review_decisions", "labour_plan_requests"])
@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_revision_and_decision_records_are_append_only(database, table, action):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
        insert_request(connection, record)
    columns = {"inquiry_work_items": "work_item_id", "labour_plan_revisions": "revision_id", "labour_plan_revision_items": "operation_identifier", "labour_review_decisions": "reviewer_id", "labour_plan_requests": "actor_id"}
    sql = f"UPDATE {table} SET {columns[table]}={columns[table]}" if action == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), connection:
        connection.execute(sql)


def test_revision_cannot_add_work_after_sealing(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), connection:
        connection.execute("INSERT INTO labour_plan_revision_items SELECT revision_id,inquiry_id,'extra',3,operation_identifier,axle,side,original_hours,source,work_description,autodata_reference,recorded_by FROM labour_plan_revision_items WHERE position=1")


@pytest.mark.parametrize("changes", [
    {"content_id": "f" * 64}, {"rules_version": "wrong-version"},
    {"original_total_hours": "99.00"}, {"inquiry_id": "missing-inquiry"},
    {"final_billable_hours": "-0.01"}, {"final_billable_hours": "NaN"},
    {"final_billable_hours": "Infinity"}, {"final_billable_hours": "1.2.3"},
    {"status": "APPROVED_UNCHANGED", "final_billable_hours": "3.6000000000000000000000001"},
    {"reason": " "}, {"reviewer_id": " "},
    {"reviewed_at": "2026-10-09T12:00:00"}, {"status": "PENDING"},
])
def test_decision_integrity_constraints(database, changes):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
    before_events = connection.execute("SELECT count(*) FROM work_events").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError), connection:
        insert_decision(connection, record, **changes)
    assert connection.execute("SELECT count(*) FROM labour_review_decisions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM work_events").fetchone()[0] == before_events


def test_incomplete_revision_cannot_have_a_decision(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id, hours=("1.60", None), ready=False)
    with connection:
        insert_revision(connection, record)
    with pytest.raises(sqlite3.IntegrityError), connection:
        insert_decision(connection, record, original_total_hours="1.60")


def test_database_rejects_ready_revision_with_missing_original_time(database):
    _, connection, inquiry_id = database
    incomplete = make_revision(inquiry_id, hours=("1.60", None), ready=False)
    # Bypass model validation deliberately to exercise the database constraint.
    invalid = incomplete.model_copy(update={
        "coverage_status": CoverageStatus.READY, "original_total_hours": Decimal("1.60"),
    })
    with pytest.raises(sqlite3.IntegrityError, match="all original times"), connection:
        insert_revision(connection, invalid)


def test_revision_without_its_declared_work_items_is_rejected(database):
    _, connection, inquiry_id = database
    with pytest.raises(sqlite3.IntegrityError, match="item count"), connection:
        connection.execute("""
            INSERT INTO labour_plan_revisions VALUES (
                'empty', ?, 1, ?, '3B-v1', 'READY', '1.60', 1, '{}', ?
            )
        """, (inquiry_id, "a" * 64, NOW))


def test_revision_numbers_are_unique_per_inquiry(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
    duplicate = record.model_copy(update={"revision_id": "another-revision-id"})
    with pytest.raises(sqlite3.IntegrityError), connection:
        insert_revision(connection, duplicate)


def test_revision_item_must_reference_registered_work(database):
    _, connection, inquiry_id = database
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("""
            INSERT INTO labour_plan_revision_items VALUES (
                'absent',?,'unregistered',1,'BRAKE_REPLACEMENT','FRONT','UNKNOWN',
                '1.60','AUTODATA',NULL,NULL,NULL
            )
        """, (inquiry_id,))


def test_active_pointer_cannot_reference_old_revision_decision(database):
    _, connection, inquiry_id = database
    first, second = make_revision(inquiry_id), make_revision(inquiry_id, 2)
    with connection:
        insert_revision(connection, first)
        insert_revision(connection, second)
        insert_decision(connection, first)
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, ?)", (inquiry_id, second.revision_id, "decision-1"))


def test_active_revision_must_belong_to_same_inquiry(database):
    repository, connection, inquiry_id = database
    other = repository.create_work(registration="OTHER", customer_description="Other", actor="Recorder", idempotency_key=str(uuid4()))["id"]
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, NULL)", (other, record.revision_id))


def test_item_without_parent_revision_fails_at_commit(database):
    _, connection, inquiry_id = database
    with connection:
        connection.execute("INSERT INTO inquiry_work_items VALUES (?, 'orphan', ?)", (inquiry_id, NOW))
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("INSERT INTO labour_plan_revision_items VALUES ('absent',?,'orphan',1,'BRAKE_REPLACEMENT','FRONT','UNKNOWN','1.60','AUTODATA',NULL,NULL,NULL)", (inquiry_id,))


def test_idempotency_keys_are_scoped_and_duplicates_rejected(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
        insert_request(connection, record)
        insert_request(connection, record, actor_id="other-employee")
        insert_request(connection, record, operation="APPROVE_LABOUR", decision_id="decision-1")
    with pytest.raises(sqlite3.IntegrityError), connection:
        insert_request(connection, record)
    assert connection.execute("SELECT count(*) FROM labour_plan_requests").fetchone()[0] == 3


def test_revision_decision_event_and_request_can_share_one_transaction(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        insert_revision(connection, record)
        insert_decision(connection, record)
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, ?)", (inquiry_id, record.revision_id, "decision-1"))
        insert_request(connection, record, operation="APPROVE_LABOUR", decision_id="decision-1")
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT active_decision_id FROM inquiry_labour_plan_state").fetchone()[0] == "decision-1"


def test_failed_future_transaction_rolls_back_revision_decision_and_event(database):
    _, connection, inquiry_id = database
    record = make_revision(inquiry_id)
    before_events = connection.execute("SELECT count(*) FROM work_events").fetchone()[0]
    with pytest.raises(RuntimeError), connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
        raise RuntimeError("simulated last write failure")
    for table in ("inquiry_work_items", "labour_plan_revision_items", "labour_plan_revisions", "labour_review_decisions"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM work_events").fetchone()[0] == before_events


def test_migration_requires_transaction_and_rolls_back_on_failure(tmp_path):
    path = tmp_path / "migration.db"
    # Minimal legacy parent tables; migration failure must not change these.
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE work_inquiries(id TEXT PRIMARY KEY); CREATE TABLE work_events(id INTEGER PRIMARY KEY,work_id TEXT);")
    with sqlite3.connect(path) as connection:
        with pytest.raises(RuntimeError, match="transaction"):
            apply_labour_migrations(connection)

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "CREATE TABLE labour_review_decisions" in sql:
                raise RuntimeError("simulated schema failure")
            return super().execute(sql, parameters)

    with sqlite3.connect(path, factory=FailingConnection) as connection:
        with pytest.raises(RuntimeError), connection:
            connection.execute("BEGIN IMMEDIATE")
            apply_labour_migrations(connection)
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"work_inquiries", "work_events"}
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            apply_labour_migrations(connection)
        assert connection.execute("SELECT count(*) FROM webapp_schema_migrations").fetchone()[0] == 2


def seed_review_history(connection, inquiry_id):
    record = make_revision(inquiry_id)
    with connection:
        insert_revision(connection, record)
        insert_decision(connection, record)
        insert_request(connection, record)
        connection.execute("INSERT INTO inquiry_labour_plan_state VALUES (?, ?, ?)",
                           (inquiry_id, record.revision_id, "decision-1"))
    return record


def history_snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in NEW_TABLES | {"work_inquiries", "work_events"}
    }


def replace_existing(connection, table, changes, *, where=""):
    # Table/column names and predicates are trusted test constants.
    values = dict(connection.execute(f"SELECT * FROM {table} {where} LIMIT 1").fetchone())
    values.update(changes)
    connection.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(values)}) "
        f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()),
    )


def pending_writes(connection, inquiry_id):
    """Earlier successful writes must roll back with the rejected replacement."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE work_inquiries SET version=version+1 WHERE id=?", (inquiry_id,))
    connection.execute("""
        INSERT INTO work_events (work_id, occurred_at, event_type, actor, details_json, work_version)
        VALUES (?, ?, 'PENDING_TEST_EVENT', 'Recorder', '{}', 2)
    """, (inquiry_id, NOW))


@pytest.mark.parametrize("table, changes", [
    ("inquiry_work_items", {"created_at": "2026-10-10T12:00:00+00:00"}),
    ("labour_plan_revisions", {"snapshot_json": '{"changed":true}'}),
    ("labour_plan_revision_items", {"original_hours": "9.99"}),
    ("labour_review_decisions", {"final_billable_hours": "9.99", "reason": "Changed decision"}),
    ("labour_plan_requests", {"response_json": '{"changed":true}'}),
    # A UNIQUE event_id collision must also preserve the original decision.
    ("labour_review_decisions", {"decision_id": "replacement-id", "final_billable_hours": "9.99"}),
])
def test_replace_cannot_change_immutable_history_and_rolls_back(database, table, changes):
    _, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    before = history_snapshot(connection)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), connection:
        pending_writes(connection, inquiry_id)
        replace_existing(connection, table, changes)
    assert not connection.in_transaction
    assert history_snapshot(connection) == before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("column, value", [
    ("id", 9999), ("work_id", "another-inquiry"),
    ("occurred_at", "2026-10-10T12:00:00+00:00"),
    ("event_type", "CHANGED"), ("actor", "Other recorder"),
    ("details_json", '{"changed":true}'), ("work_version", 9999),
])
def test_linked_review_event_cannot_be_updated(database, column, value):
    _, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    before = history_snapshot(connection)
    with pytest.raises(sqlite3.IntegrityError, match="review event is immutable"), connection:
        pending_writes(connection, inquiry_id)
        connection.execute(
            f"UPDATE work_events SET {column}=? "
            "WHERE id=(SELECT event_id FROM labour_review_decisions WHERE decision_id='decision-1')",
            (value,),
        )
    assert history_snapshot(connection) == before


@pytest.mark.parametrize("operation", ["DELETE", "REPLACE"])
def test_linked_review_event_cannot_be_deleted_or_replaced(database, operation):
    _, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    before = history_snapshot(connection)
    where = "WHERE id=(SELECT event_id FROM labour_review_decisions WHERE decision_id='decision-1')"
    with pytest.raises(sqlite3.IntegrityError, match="review event is immutable"), connection:
        pending_writes(connection, inquiry_id)
        if operation == "DELETE":
            connection.execute(f"DELETE FROM work_events {where}")
        else:
            replace_existing(connection, "work_events", {"details_json": '{"changed":true}'}, where=where)
    assert history_snapshot(connection) == before


def test_event_protection_is_limited_to_decision_linked_events(database):
    _, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    with connection:
        # Even the decision event type is writable until a decision links to it.
        event_id = connection.execute("""
            INSERT INTO work_events (work_id, occurred_at, event_type, actor, details_json, work_version)
            VALUES (?, ?, 'LABOUR_REVIEW_DECIDED', 'Recorder', '{}', 1)
        """, (inquiry_id, NOW)).lastrowid
        connection.execute("UPDATE work_events SET actor='Changed' WHERE id=?", (event_id,))
        replace_existing(connection, "work_events", {"details_json": '{"ordinary":true}'},
                         where=f"WHERE id={event_id}")
    assert connection.execute("SELECT actor, details_json FROM work_events WHERE id=?", (event_id,)).fetchone()[1] == '{"ordinary":true}'
    with connection:
        connection.execute("DELETE FROM work_events WHERE id=?", (event_id,))
    assert connection.execute("SELECT count(*) FROM labour_review_decisions").fetchone()[0] == 1


def test_repository_connections_enable_both_protections_each_time(database):
    repository, _, _ = database
    for _ in range(3):
        with repository._connect() as connection:
            assert not connection.in_transaction
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


@pytest.mark.parametrize("setting", ["foreign_keys", "recursive_triggers"])
def test_repository_rejects_connection_without_required_protection(monkeypatch, setting):
    original_connect = sqlite3.connect
    closed = []

    class DisabledProtectionConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == f"PRAGMA {setting} = ON":
                sql = f"PRAGMA {setting} = OFF"
            return super().execute(sql, parameters)

        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr("webapp.database.sqlite3.connect", lambda *args, **kwargs:
                        original_connect(*args, **kwargs, factory=DisabledProtectionConnection))
    with pytest.raises(RuntimeError, match=f"{setting}=ON"):
        with WorkRepository(":memory:")._connect():
            pytest.fail("an unprotected connection must not be yielded")
    assert closed == [True]


def test_existing_labour_api_can_append_history_with_a_protected_event(database):
    repository, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    before = history_snapshot(connection)
    with TestClient(create_app(database_path=repository.database_path)) as client:
        response = client.put(f"/api/works/{inquiry_id}/labour", json={
            "version": 1, "hours": "1.60", "work_description": "Original work",
            "recorded_by": "Recorder", "autodata_reference": "REF",
        })
    assert response.status_code == 200
    work = response.json()
    assert work["version"] == 2
    assert work["total_price"] == "144.00"
    assert len(work["events"]) == len(before["work_events"]) + 1
    after = history_snapshot(connection)
    assert after["work_events"][:-1] == before["work_events"]
    for table in NEW_TABLES:
        assert after[table] == before[table]


def test_version_one_database_gains_event_protection_without_rewriting_history(database):
    repository, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    with connection:
        # Simulate the Web 4A schema before event protection was introduced.
        connection.execute("DROP TRIGGER immutable_review_event_update")
        connection.execute("DROP TRIGGER immutable_review_event_delete")
        connection.execute("DELETE FROM webapp_schema_migrations WHERE version=2")
    before = history_snapshot(connection)
    repository.initialize()
    repository.initialize()
    with repository._connect() as upgraded:
        after = history_snapshot(upgraded)
        assert after["webapp_schema_migrations"][0] == before["webapp_schema_migrations"][0]
        assert [row[0] for row in after["webapp_schema_migrations"]] == [1, 2]
        for table in before.keys() - {"webapp_schema_migrations"}:
            assert after[table] == before[table]
        with pytest.raises(sqlite3.IntegrityError, match="review event is immutable"), upgraded:
            replace_existing(upgraded, "work_events", {"actor": "Other recorder"},
                             where="WHERE id=(SELECT event_id FROM labour_review_decisions)")


def test_failed_event_protection_migration_rolls_back_triggers_and_ledger(database):
    repository, connection, inquiry_id = database
    seed_review_history(connection, inquiry_id)
    with connection:
        connection.execute("DROP TRIGGER immutable_review_event_update")
        connection.execute("DROP TRIGGER immutable_review_event_delete")
        connection.execute("DELETE FROM webapp_schema_migrations WHERE version=2")
    before = history_snapshot(connection)

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "CREATE TRIGGER immutable_review_event_delete" in sql:
                raise RuntimeError("simulated second trigger failure")
            return super().execute(sql, parameters)

    with sqlite3.connect(repository.database_path, factory=FailingConnection) as failing:
        with pytest.raises(RuntimeError), failing:
            failing.execute("BEGIN IMMEDIATE")
            apply_labour_migrations(failing)
    assert history_snapshot(connection) == before
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'immutable_review_event_%'").fetchall() == []
    repository.initialize()
    assert connection.execute("SELECT count(*) FROM webapp_schema_migrations").fetchone()[0] == 2
