"""Additive scope metadata. Initialization NEVER assigns legacy data or users."""

import sqlite3
from datetime import datetime, timezone


SCHEMA = (
    """CREATE TABLE repair_organizations (
        organization_id TEXT PRIMARY KEY NOT NULL CHECK(length(organization_id)=36),
        name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
    )""",
    """CREATE TABLE workshops (
        workshop_id TEXT PRIMARY KEY NOT NULL CHECK(length(workshop_id)=36),
        organization_id TEXT NOT NULL REFERENCES repair_organizations(organization_id),
        name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','PLANNED','INACTIVE')),
        UNIQUE(workshop_id, organization_id)
    )""",
    "CREATE INDEX idx_workshops_organization ON workshops(organization_id, status)",
    """CREATE TABLE workshop_principals (
        auth_store_id TEXT NOT NULL CHECK(length(auth_store_id)=36),
        user_id TEXT NOT NULL CHECK(length(user_id)=36),
        PRIMARY KEY(auth_store_id, user_id)
    )""",
    """CREATE TABLE workshop_memberships (
        auth_store_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL REFERENCES workshops(workshop_id),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=1),
        PRIMARY KEY(auth_store_id, user_id, workshop_id),
        FOREIGN KEY(auth_store_id, user_id) REFERENCES workshop_principals(auth_store_id,user_id)
    )""",
    "CREATE INDEX idx_memberships_workshop ON workshop_memberships(workshop_id, enabled)",
    """CREATE TABLE active_user_workshops (
        auth_store_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        PRIMARY KEY(auth_store_id,user_id),
        FOREIGN KEY(auth_store_id,user_id,workshop_id)
            REFERENCES workshop_memberships(auth_store_id,user_id,workshop_id)
    )""",
    """CREATE TABLE inquiry_workshop_assignments (
        inquiry_id TEXT PRIMARY KEY NOT NULL REFERENCES work_inquiries(id),
        workshop_id TEXT NOT NULL,
        organization_id TEXT NOT NULL,
        UNIQUE(inquiry_id,workshop_id),
        FOREIGN KEY(workshop_id,organization_id) REFERENCES workshops(workshop_id,organization_id)
    )""",
    "CREATE INDEX idx_inquiry_workshop ON inquiry_workshop_assignments(workshop_id, inquiry_id)",
    """CREATE TABLE scoped_inquiry_creation_requests (
        workshop_id TEXT NOT NULL REFERENCES workshops(workshop_id),
        idempotency_key TEXT NOT NULL,
        request_json TEXT NOT NULL CHECK(json_valid(request_json)),
        work_id TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK(json_valid(response_json)),
        PRIMARY KEY(workshop_id,idempotency_key),
        FOREIGN KEY(work_id,workshop_id) REFERENCES inquiry_workshop_assignments(inquiry_id,workshop_id)
    )""",
    """CREATE TABLE workshop_audit_events (
        event_id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        operation TEXT NOT NULL,
        operator_reference TEXT NOT NULL CHECK(length(trim(operator_reference)) BETWEEN 1 AND 500),
        details_json TEXT NOT NULL CHECK(json_valid(details_json))
    )""",
    """CREATE TRIGGER membership_version_update BEFORE UPDATE ON workshop_memberships
        WHEN NEW.version <> OLD.version+1 OR NEW.auth_store_id <> OLD.auth_store_id
             OR NEW.user_id <> OLD.user_id OR NEW.workshop_id <> OLD.workshop_id
        BEGIN SELECT RAISE(ABORT,'membership change requires a new version'); END""",
    """CREATE TRIGGER membership_no_delete BEFORE DELETE ON workshop_memberships
        BEGIN SELECT RAISE(ABORT,'revoke membership instead of deleting it'); END""",
)

IMMUTABLE_TABLES = (
    "inquiry_workshop_assignments", "workshop_principals",
    "scoped_inquiry_creation_requests", "workshop_audit_events",
)


def apply_workshop_migrations(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("workshop migration requires an active transaction")
    connection.execute("""CREATE TABLE IF NOT EXISTS workshop_schema_migrations (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version>=1), applied_at TEXT NOT NULL
    )""")
    applied = {row[0] for row in connection.execute("SELECT version FROM workshop_schema_migrations")}
    if applied - {1}:
        raise RuntimeError("unknown workshop schema version")
    if 1 in applied:
        return
    for statement in SCHEMA:
        connection.execute(statement)
    for table in IMMUTABLE_TABLES:
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER immutable_{table}_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable workshop metadata'); END""")
    connection.execute("INSERT INTO workshop_schema_migrations VALUES (1,?)",
                       (datetime.now(timezone.utc).isoformat(),))
