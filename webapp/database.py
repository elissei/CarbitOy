from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from webapp.labour_migrations import apply_labour_migrations
from webapp.draft_migrations import apply_draft_migrations
from webapp.business_identity import BusinessActor
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission
from webapp.workshop_migrations import apply_workshop_migrations
from webapp.workshop_repository import authorize_workshop
from webapp.business_authorization import reserve_business_authorization

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_inquiries (
    id TEXT PRIMARY KEY,
    registration TEXT NOT NULL,
    customer_description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('KESKEN')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    labour_hours TEXT,
    labour_source TEXT,
    labour_work_description TEXT,
    labour_recorded_by TEXT,
    autodata_reference TEXT,
    hourly_rate TEXT,
    total_price TEXT,
    net_price TEXT,
    vat_price TEXT,
    vat_rate TEXT,
    currency TEXT
);

CREATE TABLE IF NOT EXISTS work_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL REFERENCES work_inquiries(id),
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    work_version INTEGER
);

CREATE INDEX IF NOT EXISTS idx_work_events_work_id
ON work_events(work_id, id);

CREATE TABLE IF NOT EXISTS inquiry_creation_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES work_inquiries(id),
    response_json TEXT NOT NULL
);
"""


class VersionConflictError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


class WorkNotFoundError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._initialize(connection)

    @staticmethod
    def _initialize(connection) -> None:
        if not connection.in_transaction:
            raise RuntimeError("initialization requires a transaction")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(work_events)")}
        if "work_version" not in columns:
            connection.execute("ALTER TABLE work_events ADD COLUMN work_version INTEGER")
        apply_labour_migrations(connection)
        apply_workshop_migrations(connection)
        apply_draft_migrations(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            # REPLACE deletes conflicting rows: run their immutability triggers.
            connection.execute("PRAGMA recursive_triggers = ON")
            for setting in ("foreign_keys", "recursive_triggers"):
                enabled = connection.execute(f"PRAGMA {setting}").fetchone()
                if enabled is None or enabled[0] != 1:
                    raise RuntimeError(f"SQLite requires {setting}=ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def operation(self, context, permission, *, write=False):
        """Auth reservation -> business snapshot/lock -> validation -> commit.

        The auth reservation is released only after commit/rollback. No auth
        methods or external I/O may be called from the yielded business block.
        """
        with reserve_business_authorization(context, permission, self.database_path) as reservation:
            with self._connect() as connection:
                try:
                    if reservation is not None:
                        reservation.limit_business_connection(connection)
                    connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                    authorize_workshop(connection, context, permission)
                    yield connection
                    if reservation is not None:
                        # Auth writers are STILL excluded. Recheck time/expiry
                        # before commit, not with an unlocked second connection.
                        reservation.validate()
                        reservation.limit_business_connection(connection)
                    connection.commit()
                except BaseException:
                    connection.set_progress_handler(None, 0)
                    connection.rollback()
                    raise
                finally:
                    connection.set_progress_handler(None, 0)

    def create_work(
        self,
        *,
        registration: str,
        customer_description: str,
        actor: str | BusinessActor,
        idempotency_key: str,
        context: TrustedWorkshopContext,
    ) -> dict[str, Any]:
        supplied_actor = actor
        work_id = str(uuid4())
        now = utc_now()
        details = {
            "registration": registration,
            "customer_description": customer_description,
        }
        if isinstance(actor, BusinessActor):
            attribution = actor.attribution()
            actor = str(actor.user_id)
            fingerprint = {**details, "contract": "carbit.create-work.authenticated.v1",
                           "actor_user_id": actor}
            details["actor_attribution"] = attribution
        elif isinstance(actor, str) and actor.strip():
            # Offline/legacy callers retain their old contract, never an auth
            # provenance marker. HTTP writes always supply a resolved actor.
            fingerprint = {**details, "actor": actor}
        else:
            raise ValueError("a server actor or explicit legacy label is required")
        request_json = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
        with self.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            if context.actor is not None and supplied_actor != context.actor:
                raise ValueError("actor must match the workshop context")
            previous = connection.execute(
                "SELECT request_json, response_json FROM scoped_inquiry_creation_requests "
                "WHERE workshop_id = ? AND idempotency_key = ? "
                "UNION ALL SELECT r.request_json,r.response_json FROM inquiry_creation_requests r "
                "JOIN inquiry_workshop_assignments a ON a.inquiry_id=r.work_id "
                "WHERE a.workshop_id=? AND a.organization_id=? AND r.idempotency_key=?",
                (context.workshop_id, idempotency_key, context.workshop_id,
                 context.organization_id, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["request_json"] != request_json:
                    raise IdempotencyConflictError(idempotency_key)
                return json.loads(previous["response_json"])
            connection.execute(
                """
                INSERT INTO work_inquiries (
                    id, registration, customer_description, status,
                    version, created_at, updated_at
                ) VALUES (?, ?, ?, 'KESKEN', 1, ?, ?)
                """,
                (work_id, registration, customer_description, now, now),
            )
            connection.execute("INSERT INTO inquiry_workshop_assignments VALUES (?,?,?)",
                               (work_id, context.workshop_id, context.organization_id))
            self._insert_event(
                connection,
                work_id=work_id,
                occurred_at=now,
                event_type="TIEDUSTELU_LUOTU",
                actor=actor,
                details=details,
                work_version=1,
            )
            result = self._read_work(connection, work_id, context)
            connection.execute(
                "INSERT INTO scoped_inquiry_creation_requests "
                "(workshop_id,idempotency_key, request_json, work_id, response_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    context.workshop_id, idempotency_key, request_json, work_id,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
        return result

    def list_works(self, *, context: TrustedWorkshopContext) -> list[dict[str, Any]]:
        with self.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            rows = connection.execute(
                "SELECT w.* FROM work_inquiries w JOIN inquiry_workshop_assignments a ON a.inquiry_id=w.id "
                "WHERE a.workshop_id=? AND a.organization_id=? ORDER BY w.created_at DESC",
                (context.workshop_id, context.organization_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_work(self, work_id: str, *, context: TrustedWorkshopContext) -> dict[str, Any]:
        with self.operation(context, WorkshopPermission.READ_LABOUR_PLAN, write=False) as connection:
            return self._read_work(connection, work_id, context)

    @staticmethod
    def _find_inquiry(connection, work_id, context):
        row = connection.execute(
            "SELECT w.* FROM work_inquiries w JOIN inquiry_workshop_assignments a ON a.inquiry_id=w.id "
            "WHERE w.id=? AND a.workshop_id=? AND a.organization_id=?",
            (work_id, context.workshop_id, context.organization_id),
        ).fetchone()
        if row is None:
            raise WorkNotFoundError(work_id)
        return row

    @staticmethod
    def _read_work(connection: sqlite3.Connection, work_id: str, context) -> dict[str, Any]:
        row = WorkRepository._find_inquiry(connection, work_id, context)
        event_rows = connection.execute(
            """
            SELECT occurred_at, event_type, actor, details_json, work_version
            FROM work_events WHERE work_id = ? ORDER BY id DESC
            """,
            (work_id,),
        ).fetchall()
        result = dict(row)
        result["events"] = [
            {
                "occurred_at": event["occurred_at"],
                "event_type": event["event_type"],
                "actor": event["actor"],
                "work_version": event["work_version"],
                "details": json.loads(event["details_json"]),
            }
            for event in event_rows
        ]
        return result

    def record_labour(
        self,
        *,
        work_id: str,
        expected_version: int,
        labour: dict[str, str],
        actor: BusinessActor | None = None,
        context: TrustedWorkshopContext,
    ) -> dict[str, Any]:
        labour = dict(labour)
        if actor is not None:
            if not isinstance(actor, BusinessActor):
                raise ValueError("a server actor snapshot is required")
            labour["recorded_by"] = str(actor.user_id)
        details = dict(labour)
        if actor is not None:
            details["actor_attribution"] = actor.attribution()
        now = utc_now()
        with self.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            self._find_inquiry(connection, work_id, context)
            if context.actor is not None and actor != context.actor:
                raise ValueError("actor must match the workshop context")
            cursor = connection.execute(
                """
                UPDATE work_inquiries SET
                    labour_hours = ?, labour_source = ?,
                    labour_work_description = ?, labour_recorded_by = ?,
                    autodata_reference = ?, hourly_rate = ?,
                    total_price = ?, net_price = ?, vat_price = ?,
                    vat_rate = ?, currency = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    labour["hours"], labour["source"],
                    labour["work_description"], labour["recorded_by"],
                    labour["autodata_reference"], labour["hourly_rate"],
                    labour["total_price"], labour["net_price"],
                    labour["vat_price"], labour["vat_rate"],
                    labour["currency"], now, work_id, expected_version,
                ),
            )
            if cursor.rowcount == 0:
                raise VersionConflictError(work_id)
            self._insert_event(
                connection,
                work_id=work_id,
                occurred_at=now,
                event_type="AUTODATA_TYOAIKA_KIRJATTU",
                actor=labour["recorded_by"],
                details=details,
                work_version=expected_version + 1,
            )
            result = self._read_work(connection, work_id, context)
        return result

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        occurred_at: str,
        event_type: str,
        actor: str,
        details: dict[str, Any],
        work_version: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO work_events (
                work_id, occurred_at, event_type, actor, details_json, work_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                work_id,
                occurred_at,
                event_type,
                actor,
                json.dumps(details, ensure_ascii=False),
                work_version,
            ),
        )
