"""Explicit trusted offline operations. Never register these functions as routes."""

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from webapp.database import WorkRepository
from webapp.workshop_repository import WorkshopDirectory, audit


APPROVED_DATABASE = Path(__file__).resolve().parents[1] / "var" / "carbit.db"
CARBIT_REPAIR_ORGANIZATION = str(uuid5(NAMESPACE_URL, "carbit:repair-business"))
WORKSHOP_1 = str(uuid5(NAMESPACE_URL, "carbit:repair-business:workshop-1"))
WORKSHOP_2 = str(uuid5(NAMESPACE_URL, "carbit:repair-business:workshop-2"))


def assign_inquiries(connection, inquiry_ids, *, organization_id, workshop_id, operator_reference):
    """An operator-supplied exact list, never a heuristic/default legacy assignment."""
    if not connection.in_transaction or not inquiry_ids or len(set(inquiry_ids)) != len(inquiry_ids):
        raise ValueError("an explicit unique inquiry list in a transaction is required")
    organization_id, workshop_id = str(UUID(organization_id)), str(UUID(workshop_id))
    for inquiry_id in inquiry_ids:
        connection.execute("INSERT INTO inquiry_workshop_assignments VALUES (?,?,?)",
                           (inquiry_id, workshop_id, organization_id))
    audit(connection, "INQUIRIES_ASSIGNED", operator_reference,
          {"inquiry_ids": list(inquiry_ids), "organization_id": organization_id, "workshop_id": workshop_id})


def _historical_rows(connection):
    # Names are read from this database's schema and quoted as identifiers.
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if not row[0].startswith(('sqlite_', 'workshop_', 'repair_', 'active_user_',
                                        'inquiry_workshop_', 'scoped_inquiry_'))]
    return {table: [tuple(row) for row in connection.execute(
        'SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid')]
        for table in tables}


def migrate_approved_development(database_path, *, backup_path, expected_sha256, operator_reference):
    """Only the specifically approved local file, with a checked preimage/backup.

    The caller stops the local server first. Other environments require their
    own explicit record-to-workshop approval and migration procedure.
    """
    path = Path(database_path).absolute()
    if path != APPROVED_DATABASE.absolute() or path.resolve() != APPROVED_DATABASE.absolute():
        raise ValueError("approval is restricted to the exact local development database")
    if not path.is_file() or Path(str(path) + '-wal').exists():
        raise ValueError("existing development database without a WAL is required; stop the server first")
    backup_path = Path(backup_path).absolute()
    if backup_path == path or not backup_path.parent.is_dir() or backup_path.exists():
        raise ValueError("a new backup path in an existing protected directory is required")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("explicit SHA-256 preimage is required")
    repository = WorkRepository(path)
    with repository._connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256.lower():
            raise ValueError("development database differs from the reviewed preimage")
        if connection.execute("SELECT count(*) FROM work_inquiries").fetchone()[0] != 3 or (
            connection.execute("SELECT count(*) FROM work_events").fetchone()[0] != 8
        ):
            raise ValueError("development data differs from the approved 3 inquiries / 8 events")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != 'ok' or connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall():
            raise ValueError("database integrity check failed")
        before = _historical_rows(connection)
        inquiry_ids = [row[0] for row in connection.execute("SELECT id FROM work_inquiries ORDER BY id")]
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='inquiry_workshop_assignments'").fetchone():
            assignments = [tuple(row) for row in connection.execute(
                "SELECT inquiry_id,workshop_id,organization_id FROM inquiry_workshop_assignments ORDER BY inquiry_id")]
            expected = [(item, WORKSHOP_1, CARBIT_REPAIR_ORGANIZATION) for item in inquiry_ids]
            if assignments:
                if assignments != expected or not connection.execute(
                    "SELECT 1 FROM workshop_audit_events WHERE operation='APPROVED_DEVELOPMENT_MIGRATION'"
                ).fetchone():
                    raise ValueError("existing assignments differ from this approved migration")
                return {"organization_id": CARBIT_REPAIR_ORGANIZATION, "workshop_id": WORKSHOP_1,
                        "assigned_inquiries": 3, "already_migrated": True}
        # A separate read-only source sees the stable pre-migration database while
        # this BEGIN IMMEDIATE prevents another writer from changing the preimage.
        descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError("backup integrity check failed")
        repository._initialize(connection)
        connection.execute("INSERT INTO repair_organizations VALUES (?, ?, 1) ON CONFLICT DO NOTHING",
                           (CARBIT_REPAIR_ORGANIZATION, "Carbit Oy – autokorjaamotoiminta"))
        connection.execute("INSERT INTO workshops VALUES (?, ?, ?, 'ACTIVE') ON CONFLICT DO NOTHING",
                           (WORKSHOP_1, CARBIT_REPAIR_ORGANIZATION, "Workshop 1 – nykyinen korjaamo"))
        connection.execute("INSERT INTO workshops VALUES (?, ?, ?, 'PLANNED') ON CONFLICT DO NOTHING",
                           (WORKSHOP_2, CARBIT_REPAIR_ORGANIZATION, "Workshop 2 – suunniteltu korjaamo"))
        if [tuple(row) for row in connection.execute(
            "SELECT workshop_id,organization_id,status FROM workshops WHERE workshop_id IN (?,?) ORDER BY workshop_id",
            (WORKSHOP_1, WORKSHOP_2),
        )] != sorted([(WORKSHOP_1, CARBIT_REPAIR_ORGANIZATION, 'ACTIVE'),
                      (WORKSHOP_2, CARBIT_REPAIR_ORGANIZATION, 'PLANNED')]):
            raise ValueError("existing workshop structure conflicts with the approval")
        # No ON CONFLICT update of an existing assignment: conflicts stop/rollback.
        assign_inquiries(connection, inquiry_ids, organization_id=CARBIT_REPAIR_ORGANIZATION,
                         workshop_id=WORKSHOP_1, operator_reference=operator_reference)
        audit(connection, "APPROVED_DEVELOPMENT_MIGRATION", operator_reference,
              {"database_path": str(path), "preimage_sha256": expected_sha256.lower(),
               "backup_path": str(backup_path), "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
               "inquiry_count": 3, "event_count": 8})
        after = _historical_rows(connection)
        for table, rows in before.items():
            # The oldest supported events acquire only the nullable version column.
            current = after[table]
            if table == 'webapp_schema_migrations':
                current = [row for row in current if row[0] in {value[0] for value in rows}]
            if table == 'work_events' and rows and len(current[0]) == len(rows[0]) + 1:
                current = [row[:-1] for row in current]
            if current != rows:
                raise ValueError("historical data changed; migration rolled back")
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError("migration foreign-key check failed")
    return {"organization_id": CARBIT_REPAIR_ORGANIZATION, "workshop_id": WORKSHOP_1,
            "assigned_inquiries": len(inquiry_ids), "backup_path": str(backup_path)}


INITIAL_WORKSHOP_OPERATION = "INITIAL_WORKSHOP_ESTABLISHED"
# Ledgers of a business database fully initialized by this release. Older,
# partial or newer databases are rejected; establishment never migrates.
SUPPORTED_BUSINESS_LEDGERS = (
    ("webapp_schema_migrations", "SELECT version FROM webapp_schema_migrations", {1, 2}),
    ("workshop_schema_migrations", "SELECT version FROM workshop_schema_migrations", {1}),
    ("draft_schema_migrations", "SELECT version FROM draft_schema_migrations", {1, 2, 3, 4}),
)
# Tables the operation reads or writes. A first establishment requires all of
# them to be empty: no legacy work, scope, members or earlier offline operations.
ESTABLISHMENT_TABLES = (
    "work_inquiries", "work_events", "inquiry_creation_requests", "repair_organizations",
    "workshops", "workshop_principals", "workshop_memberships", "active_user_workshops",
    "inquiry_workshop_assignments", "scoped_inquiry_creation_requests", "workshop_audit_events",
)
AUDIT_IMMUTABILITY_TRIGGERS = (
    "immutable_workshop_audit_events_update", "immutable_workshop_audit_events_delete",
)


class WorkshopEstablishmentError(ValueError):
    """Rejected establishment request; this call committed nothing."""


def _establishment_request(database_path, organization_id, organization_name, workshop_id,
                           workshop_name, operator_reference):
    """Validate plain inputs before any database is opened."""
    if not isinstance(database_path, (str, os.PathLike)) or not Path(database_path).is_absolute():
        raise WorkshopEstablishmentError("an explicit absolute business database path is required")
    try:
        path = Path(database_path).resolve(strict=True)
    except OSError:
        raise WorkshopEstablishmentError("an existing business database file is required") from None
    if not path.is_file():
        raise WorkshopEstablishmentError("an existing business database file is required")
    approved = APPROVED_DATABASE.absolute()
    if path == approved.resolve() or (approved.exists() and os.path.samefile(path, approved)):
        # The local development database has its own approved migration procedure.
        raise WorkshopEstablishmentError("the approved development database cannot be established")
    identifiers = []
    for value in (organization_id, workshop_id):
        try:
            if not isinstance(value, str):
                raise ValueError
            identifiers.append(str(UUID(value)))
        except ValueError:
            raise WorkshopEstablishmentError("explicit organization and workshop UUIDs are required") from None
    if identifiers[0] == identifiers[1]:
        raise WorkshopEstablishmentError("organization and workshop UUIDs must differ")
    if {CARBIT_REPAIR_ORGANIZATION, WORKSHOP_1, WORKSHOP_2} & set(identifiers):
        raise WorkshopEstablishmentError("development migration identifiers are reserved")
    names = []
    for value in (organization_name, workshop_name):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            raise WorkshopEstablishmentError("organization and workshop names must be 1-200 characters")
        names.append(value.strip())
    if not isinstance(operator_reference, str) or not 1 <= len(operator_reference.strip()) <= 500:
        raise WorkshopEstablishmentError("a bounded offline operator/approval reference is required")
    return path, {"organization_id": identifiers[0], "organization_name": names[0],
                  "workshop_id": identifiers[1], "workshop_name": names[1],
                  "workshop_status": "ACTIVE"}


@contextmanager
def _existing_business_connection(path):
    # mode=rw never creates a missing file, even if it vanished after validation.
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        for setting in ("foreign_keys", "recursive_triggers"):
            connection.execute(f"PRAGMA {setting} = ON")
            enabled = connection.execute(f"PRAGMA {setting}").fetchone()
            if enabled is None or enabled[0] != 1:
                raise RuntimeError(f"SQLite requires {setting}=ON")
        yield connection
    finally:
        connection.close()


def _require_supported_business_schema(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for ledger, query, versions in SUPPORTED_BUSINESS_LEDGERS:
        if ledger not in tables or {row[0] for row in connection.execute(query)} != versions:
            raise WorkshopEstablishmentError("an initialized business database of the supported schema is required")
    triggers = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", ("workshop_audit_events",))}
    if set(ESTABLISHMENT_TABLES) - tables or set(AUDIT_IMMUTABILITY_TRIGGERS) - triggers:
        raise WorkshopEstablishmentError("an initialized business database of the supported schema is required")


def _existing_establishment(connection, request):
    """Event ID of the matching audited establishment, None for a clean database.

    Any other organization/workshop/establishment state is a conflict. It is
    never completed, reactivated, updated or inferred from history.
    """
    organizations = [tuple(row) for row in connection.execute(
        "SELECT organization_id, name, enabled FROM repair_organizations ORDER BY organization_id")]
    workshops = [tuple(row) for row in connection.execute(
        "SELECT workshop_id, organization_id, name, status FROM workshops ORDER BY workshop_id")]
    events = connection.execute(
        "SELECT event_id, details_json FROM workshop_audit_events WHERE operation=? ORDER BY event_id",
        (INITIAL_WORKSHOP_OPERATION,)).fetchall()
    if not organizations and not workshops and not events:
        return None
    if (organizations != [(request["organization_id"], request["organization_name"], 1)]
            or workshops != [(request["workshop_id"], request["organization_id"],
                              request["workshop_name"], "ACTIVE")]
            or len(events) != 1 or json.loads(events[0]["details_json"]) != request):
        raise WorkshopEstablishmentError("existing workshop structure conflicts with this request; nothing was changed")
    return events[0]["event_id"]


def establish_initial_workshop(database_path, *, organization_id, organization_name, workshop_id,
                               workshop_name, operator_reference):
    """Trusted offline operator only: one organization and one ACTIVE workshop.

    The target is an existing business database already initialized by the
    current release. This never opens the auth database, migrates, creates
    accounts, memberships or an active-workshop selection. An identical retry,
    also after later memberships or work, returns ALREADY_ESTABLISHED.
    """
    path, request = _establishment_request(database_path, organization_id, organization_name,
                                           workshop_id, workshop_name, operator_reference)
    with _existing_business_connection(path) as connection, connection:
        # Every state decision and write happens under one writer reservation.
        connection.execute("BEGIN IMMEDIATE")
        _require_supported_business_schema(connection)
        event_id = _existing_establishment(connection, request)
        status = "ALREADY_ESTABLISHED"
        if event_id is None:
            for table in ESTABLISHMENT_TABLES:
                # Identifiers come from the constant tuple above, never from input.
                if connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                    raise WorkshopEstablishmentError("a first establishment requires an empty new environment")
            connection.execute("INSERT INTO repair_organizations (organization_id, name, enabled) VALUES (?, ?, 1)",
                               (request["organization_id"], request["organization_name"]))
            connection.execute("INSERT INTO workshops (workshop_id, organization_id, name, status) "
                               "VALUES (?, ?, ?, 'ACTIVE')",
                               (request["workshop_id"], request["organization_id"], request["workshop_name"]))
            audit(connection, INITIAL_WORKSHOP_OPERATION, operator_reference, request)
            event = connection.execute("SELECT event_id, operation FROM workshop_audit_events "
                                       "WHERE event_id = last_insert_rowid()").fetchone()
            if event is None or event["operation"] != INITIAL_WORKSHOP_OPERATION:
                raise RuntimeError("establishment audit event was not recorded")
            event_id, status = event["event_id"], "ESTABLISHED"
        # Both a first establishment and a recognized retry require an intact
        # database; this check never repairs rows and a failure rolls back.
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise WorkshopEstablishmentError("business database foreign-key check failed; nothing was changed")
    # Reached only after a successful commit (or a write-free recognition).
    return {"status": status, "organization_id": request["organization_id"],
            "workshop_id": request["workshop_id"], "audit_event_id": event_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='operation', required=True)
    migrate = subparsers.add_parser('migrate-approved-development')
    migrate.add_argument('--database', required=True)
    migrate.add_argument('--backup', required=True)
    migrate.add_argument('--expected-sha256', required=True)
    migrate.add_argument('--operator-reference', required=True)
    establish = subparsers.add_parser('establish-initial-workshop')
    for option in ('--database', '--organization-id', '--organization-name', '--workshop-id',
                   '--workshop-name', '--operator-reference'):
        establish.add_argument(option, required=True)
    for operation in ('grant-membership', 'revoke-membership', 'select-workshop'):
        command = subparsers.add_parser(operation)
        command.add_argument('--database', required=True)
        command.add_argument('--auth-database', required=True)
        command.add_argument('--user-id', required=True)
        command.add_argument('--workshop-id', required=True)
        command.add_argument('--operator-reference', required=True)
    args = parser.parse_args()
    if args.operation == 'migrate-approved-development':
        result = migrate_approved_development(
            args.database, backup_path=args.backup, expected_sha256=args.expected_sha256,
            operator_reference=args.operator_reference,
        )
        print(json.dumps(result))
        return
    if args.operation == 'establish-initial-workshop':
        # Business database only; never initialize it or touch the auth database.
        if not Path(args.database).is_absolute() or not Path(args.database).is_file():
            parser.error('an existing absolute business database path is required')
        result = establish_initial_workshop(
            args.database, organization_id=args.organization_id, organization_name=args.organization_name,
            workshop_id=args.workshop_id, workshop_name=args.workshop_name,
            operator_reference=args.operator_reference,
        )
        print(json.dumps(result))
        return
    # Do not initialize or create any database/account on membership operations.
    from webapp.auth_repository import AuthRepository
    for value in (args.database, args.auth_database):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            parser.error('existing absolute database paths are required')
    directory = WorkshopDirectory(WorkRepository(args.database), AuthRepository(args.auth_database))
    operation = {'grant-membership': directory.grant_membership,
                 'revoke-membership': directory.revoke_membership,
                 'select-workshop': directory.select_active_workshop}[args.operation]
    operation(args.user_id, args.workshop_id, operator_reference=args.operator_reference)
    print('Offline workshop operation recorded.')


if __name__ == '__main__':
    main()
