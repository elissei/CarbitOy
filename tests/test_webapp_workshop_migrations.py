"""Independent legacy fixtures, backup/rollback and trusted offline provisioning."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from http_auth_support import grant_session
from test_webapp_labour_migrations import seed_review_history
from workshop_support import TEST_STORE, seed_test_scope, test_context
from webapp.auth_crypto import TotpCipher
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService
from webapp.database import SCHEMA, WorkNotFoundError, WorkRepository
from webapp.labour_migrations import apply_labour_migrations
from webapp.workshop_admin import (
    CARBIT_REPAIR_ORGANIZATION, WORKSHOP_1, WORKSHOP_2, migrate_approved_development,
)
from webapp.workshop_repository import WorkshopDirectory
import webapp.workshop_admin as admin
import webapp.workshop_migrations as migrations


def legacy_database(tmp_path, *, labour=False):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript(SCHEMA)
        for index in range(3):
            connection.execute("""INSERT INTO work_inquiries
                (id,registration,customer_description,status,version,created_at,updated_at,
                 labour_hours,labour_source,labour_recorded_by,total_price)
                VALUES (?,?,?,'KESKEN',7,?,?, '1.60','MANUAL_AUTODATA','Historical','144.00')""",
                (f'legacy-{index}', f'TEST{index}', 'Fictional legacy',
                 '2026-10-01T12:00:00+00:00', '2026-10-01T12:00:00+00:00'))
        for index in range(7 if labour else 8):
            connection.execute("""INSERT INTO work_events
                (work_id,occurred_at,event_type,actor,details_json,work_version)
                VALUES (?,'2026-10-01T12:00:00+00:00','LEGACY_EVENT','Historical','{}',NULL)""",
                (f'legacy-{index % 3}',))
        request = {'registration':'TEST0','customer_description':'Fictional legacy','actor':'Historical'}
        connection.execute('INSERT INTO inquiry_creation_requests VALUES (?,?,?,?)',
            ('legacy-key',json.dumps(request, ensure_ascii=False,sort_keys=True),'legacy-0','{"id":"legacy-0","version":1}'))
        if labour:
            apply_labour_migrations(connection)
            seed_review_history(connection, 'legacy-0')
    return path


def snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row[0].startswith('sqlite_')]
        return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                for table in tables}


def run_migration(path, tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(admin, 'APPROVED_DATABASE', path)
    return migrate_approved_development(path, backup_path=tmp_path / 'verified-backup.db',
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        operator_reference='Explicit isolated migration-test approval', **kwargs)


def test_generic_initialization_never_assigns_legacy_data_or_creates_workshops_or_memberships(tmp_path):
    path = legacy_database(tmp_path)
    before = snapshot(path)
    repository = WorkRepository(path)
    repository.initialize()
    repository.initialize()
    after = snapshot(path)
    for table, rows in before.items():
        assert after[table] == rows
    for table in ('repair_organizations','workshops','workshop_memberships','inquiry_workshop_assignments'):
        assert after[table] == []
    seed_test_scope(repository)  # Explicit fixture-only assignment for a controlled access check.
    with repository._connect() as connection, connection:
        connection.execute('DROP TRIGGER immutable_inquiry_workshop_assignments_delete')
        connection.execute("DELETE FROM inquiry_workshop_assignments WHERE inquiry_id='legacy-0'")
    with pytest.raises(WorkNotFoundError):
        repository.get_work('legacy-0', context=test_context())
    assert 'legacy-0' not in {row['id'] for row in repository.list_works(context=test_context())}


@pytest.mark.parametrize('labour', [False,True])
def test_approved_development_migration_preserves_ids_versions_history_retries_and_verified_backup(tmp_path, monkeypatch, labour):
    path = legacy_database(tmp_path, labour=labour)
    before = snapshot(path)
    result = run_migration(path,tmp_path,monkeypatch)
    after = snapshot(path)
    assert result['assigned_inquiries'] == 3
    for table, rows in before.items():
        assert after[table] == rows
    assert snapshot(Path(result['backup_path'])) == before
    assert len(after['inquiry_workshop_assignments']) == 3
    assert all(row[1:] == (WORKSHOP_1,CARBIT_REPAIR_ORGANIZATION) for row in after['inquiry_workshop_assignments'])
    assert after['workshop_principals'] == after['workshop_memberships'] == []
    with sqlite3.connect(path) as connection:
        assert dict(connection.execute('SELECT workshop_id,status FROM workshops')) == {WORKSHOP_1:'ACTIVE',WORKSHOP_2:'PLANNED'}
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute("SELECT count(*) FROM workshop_audit_events WHERE operation='INQUIRIES_ASSIGNED'").fetchone()[0] == 1
    # Repeating an already-audited migration is a no-op, including audit/history.
    result = migrate_approved_development(path, backup_path=tmp_path/'unused.db',
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), operator_reference='Repeat')
    assert result['already_migrated'] and snapshot(path) == after and not (tmp_path/'unused.db').exists()


def test_legacy_creation_retry_remains_exact_and_scoped_after_assignment(tmp_path, monkeypatch):
    path = legacy_database(tmp_path)
    run_migration(path,tmp_path,monkeypatch)
    repository = WorkRepository(path)
    captured = replace(test_context(), organization_id=CARBIT_REPAIR_ORGANIZATION, workshop_id=WORKSHOP_1)
    with repository._connect() as connection, connection:
        connection.execute('INSERT INTO workshop_principals VALUES (?,?)',(TEST_STORE,captured.employee_id))
        connection.execute('INSERT INTO workshop_memberships VALUES (?,?,?,1,1)',(TEST_STORE,captured.employee_id,WORKSHOP_1))
    before = snapshot(path)
    assert repository.create_work(registration='TEST0',customer_description='Fictional legacy',
        actor='Historical',idempotency_key='legacy-key',context=captured) == {'id':'legacy-0','version':1}
    assert snapshot(path) == before
    assert repository.get_work('legacy-0',context=captured)['version'] == 7


@pytest.mark.parametrize('failure', ['wrong-path','wrong-preimage','extra-record','existing-backup','wal','schema-error','audit-error'])
def test_migration_stops_or_rolls_back_without_guessing_or_data_loss(tmp_path,monkeypatch,failure):
    path = legacy_database(tmp_path)
    approved = path if failure != 'wrong-path' else tmp_path/'different.db'
    monkeypatch.setattr(admin,'APPROVED_DATABASE',approved)
    backup = tmp_path/'backup.db'
    if failure == 'extra-record':
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM work_events WHERE id=8")
    if failure == 'existing-backup': backup.write_bytes(b'never overwrite')
    if failure == 'wal': Path(str(path)+'-wal').write_bytes(b'active writer marker')
    if failure == 'schema-error': monkeypatch.setattr(migrations,'SCHEMA',migrations.SCHEMA+('INVALID SQL',))
    if failure == 'audit-error':
        def fail(*args,**kwargs): raise RuntimeError('injected audit failure')
        monkeypatch.setattr(admin,'audit',fail)
    before = snapshot(path)
    digest = '0'*64 if failure=='wrong-preimage' else hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises((ValueError,sqlite3.Error,RuntimeError)):
        migrate_approved_development(path,backup_path=backup,expected_sha256=digest,operator_reference='Explicit test')
    assert snapshot(path) == before
    if failure == 'existing-backup': assert backup.read_bytes() == b'never overwrite'
    if failure in ('schema-error','audit-error'): assert snapshot(backup) == before


def test_unknown_scope_migration_version_fails_closed(tmp_path):
    repository = WorkRepository(tmp_path/'future.db')
    repository.initialize()
    with repository._connect() as connection, connection:
        connection.execute("INSERT INTO workshop_schema_migrations VALUES (999,'future')")
    before = snapshot(Path(repository.database_path))
    with pytest.raises(RuntimeError,match='unknown workshop'):
        repository.initialize()
    assert snapshot(Path(repository.database_path)) == before


@pytest.mark.parametrize('table', ['inquiry_workshop_assignments','workshop_audit_events','workshop_principals','scoped_inquiry_creation_requests'])
@pytest.mark.parametrize('operation', ['UPDATE','DELETE','REPLACE'])
def test_scope_assignments_retry_snapshots_and_audit_are_immutable(tmp_path,monkeypatch,table,operation):
    path=legacy_database(tmp_path)
    run_migration(path,tmp_path,monkeypatch)
    repository=WorkRepository(path)
    captured=replace(test_context(),organization_id=CARBIT_REPAIR_ORGANIZATION,workshop_id=WORKSHOP_1)
    with repository._connect() as connection,connection:
        connection.execute('INSERT INTO workshop_principals VALUES (?,?)',(TEST_STORE,captured.employee_id))
        connection.execute('INSERT INTO workshop_memberships VALUES (?,?,?,1,1)',(TEST_STORE,captured.employee_id,WORKSHOP_1))
    repository.create_work(registration='TEST-NEW',customer_description='Fictional',actor='Fixture',
                           idempotency_key='new-key',context=captured)
    before=snapshot(path)
    with pytest.raises(sqlite3.IntegrityError):
        with repository._connect() as connection,connection:
            if operation=='REPLACE':
                row=connection.execute('SELECT * FROM '+table+' LIMIT 1').fetchone()
                connection.execute('INSERT OR REPLACE INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',tuple(row))
            elif operation=='DELETE': connection.execute('DELETE FROM '+table)
            else:
                column=connection.execute('PRAGMA table_info('+table+')').fetchone()['name']
                connection.execute('UPDATE '+table+' SET '+column+'='+column)
    assert snapshot(path)==before


def test_scope_foreign_keys_and_membership_version_constraints(tmp_path):
    repository=WorkRepository(tmp_path/'constraints.db')
    repository.initialize()
    seed_test_scope(repository)
    with repository._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO workshop_memberships VALUES (?,?,?,1,1)',(str(uuid4()),str(uuid4()),str(uuid4())))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('UPDATE workshop_memberships SET enabled=0')
        with pytest.raises(sqlite3.IntegrityError): connection.execute('DELETE FROM workshop_memberships')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO inquiry_workshop_assignments VALUES (?,?,?)',('missing',str(uuid4()),str(uuid4())))


def test_membership_operation_does_not_create_missing_auth_database(tmp_path):
    repository=WorkRepository(tmp_path/'works.db')
    repository.initialize()
    auth_path=tmp_path/'missing-auth.db'
    directory=WorkshopDirectory(repository,AuthRepository(auth_path))
    with pytest.raises(ValueError,match='existing auth'):
        directory.grant_membership(uuid4(),uuid4(),operator_reference='Explicit test')
    assert not auth_path.exists()
    assert snapshot(Path(repository.database_path))['workshop_memberships']==[]


def test_auth_store_identity_upgrade_preserves_accounts_mfa_and_sessions(tmp_path):
    from cryptography.fernet import Fernet
    repository=AuthRepository(tmp_path/'auth.db')
    repository.initialize()
    service=AuthService(repository,TotpCipher((Fernet.generate_key(),)))
    session=grant_session(service,membership=False)
    # Independently remove v2 to emulate the prior schema with a real valid session.
    with repository.transaction() as connection:
        connection.execute('DROP TABLE auth_store_identity')
        connection.execute('DELETE FROM auth_schema_migrations WHERE version=2')
    before=snapshot(repository.database_path)
    repository.initialize()
    first=repository.store_id()
    repository.initialize()
    assert repository.store_id()==first
    after=snapshot(repository.database_path)
    for table,rows in before.items():
        if table!='auth_schema_migrations': assert after[table]==rows
    assert service.inspect_session(session.token.get_secret_value()).user.user_id==session.user.user_id
    with pytest.raises(sqlite3.IntegrityError):
        with repository.transaction() as connection:
            connection.execute('INSERT OR REPLACE INTO auth_store_identity VALUES (1,?)',(str(uuid4()),))
