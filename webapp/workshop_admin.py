"""Explicit trusted offline operations. Never register these functions as routes."""

import argparse
import hashlib
import json
import os
import sqlite3
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='operation', required=True)
    migrate = subparsers.add_parser('migrate-approved-development')
    migrate.add_argument('--database', required=True)
    migrate.add_argument('--backup', required=True)
    migrate.add_argument('--expected-sha256', required=True)
    migrate.add_argument('--operator-reference', required=True)
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
