from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


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
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_events_work_id
ON work_events(work_id, id);
"""


class VersionConflictError(Exception):
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
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def create_work(
        self,
        *,
        registration: str,
        customer_description: str,
        actor: str,
    ) -> dict[str, Any]:
        work_id = str(uuid4())
        now = utc_now()
        details = {
            "registration": registration,
            "customer_description": customer_description,
        }
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO work_inquiries (
                        id, registration, customer_description, status,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, 'KESKEN', 1, ?, ?)
                    """,
                    (work_id, registration, customer_description, now, now),
                )
                self._insert_event(
                    connection,
                    work_id=work_id,
                    occurred_at=now,
                    event_type="TIEDUSTELU_LUOTU",
                    actor=actor,
                    details=details,
                )
        return self.get_work(work_id)

    def list_works(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM work_inquiries ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_work(self, work_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_inquiries WHERE id = ?", (work_id,)
            ).fetchone()
            if row is None:
                raise WorkNotFoundError(work_id)
            event_rows = connection.execute(
                """
                SELECT occurred_at, event_type, actor, details_json
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
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            with connection:
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
                    exists = connection.execute(
                        "SELECT 1 FROM work_inquiries WHERE id = ?", (work_id,)
                    ).fetchone()
                    if exists is None:
                        raise WorkNotFoundError(work_id)
                    raise VersionConflictError(work_id)
                self._insert_event(
                    connection,
                    work_id=work_id,
                    occurred_at=now,
                    event_type="AUTODATA_TYOAIKA_KIRJATTU",
                    actor=labour["recorded_by"],
                    details=dict(labour),
                )
        return self.get_work(work_id)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        occurred_at: str,
        event_type: str,
        actor: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO work_events (
                work_id, occurred_at, event_type, actor, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                work_id,
                occurred_at,
                event_type,
                actor,
                json.dumps(details, ensure_ascii=False),
            ),
        )
