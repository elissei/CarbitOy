"""Upgrade real version-1 storage, without rewriting legacy receipts/history."""

import sqlite3

import pytest

from test_webapp_draft_migrations import insert_draft, clock, receipt, CTX, PERMISSION
from test_webapp_workshop_migrations import snapshot
from workshop_support import seed_test_scope
from webapp.database import WorkRepository
import webapp.draft_migrations as migrations


def version_one(tmp_path, monkeypatch):
    repository = WorkRepository(tmp_path / 'version-one.db')
    with monkeypatch.context() as legacy:
        legacy.setattr(migrations,'_apply_migration_2',lambda *args:None)
        legacy.setattr(migrations,'apply_migration_3',lambda *args:None)
        legacy.setattr(migrations,'apply_migration_4',lambda *args:None)
        repository.initialize()
    seed_test_scope(repository)
    with repository.operation(CTX,PERMISSION,write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection,payload='{"registration":"Legacy"}')
        receipt(connection,draft_id)
    return repository


def test_version_two_preserves_legacy_content_receipts_metadata_and_repeated_upgrade(tmp_path, monkeypatch):
    repository = version_one(tmp_path,monkeypatch)
    before = snapshot(repository.database_path)
    repository.initialize()
    after = snapshot(repository.database_path)
    for table,rows in before.items():
        if table!='draft_schema_migrations':
            assert after[table]==rows
    assert after['draft_schema_migrations'][0]==before['draft_schema_migrations'][0]
    assert {row[0] for row in after['draft_schema_migrations']}=={1,2,3,4}
    assert after['draft_request_receipts']==[]  # No guessed legacy fingerprints.
    repository.initialize()
    assert snapshot(repository.database_path)==after
    with repository._connect() as connection:
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_second_migration_failure_restores_original_version_one_schema_and_data(tmp_path, monkeypatch):
    repository = version_one(tmp_path,monkeypatch)
    before = snapshot(repository.database_path)
    with repository._connect() as connection:
        before_schema = connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
    monkeypatch.setattr(migrations,'MIGRATION_2',migrations.MIGRATION_2+('INVALID SQL',))
    with pytest.raises(sqlite3.DatabaseError):
        repository.initialize()
    assert snapshot(repository.database_path)==before
    with repository._connect() as connection:
        assert connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()==before_schema


def test_incomplete_version_ledger_is_rejected(tmp_path):
    repository = WorkRepository(tmp_path / 'incomplete.db')
    repository.initialize()
    with repository._connect() as connection,connection:
        connection.execute('DELETE FROM draft_schema_migrations WHERE version=1')
    before = snapshot(repository.database_path)
    with pytest.raises(RuntimeError,match='draft schema version'):
        repository.initialize()
    assert snapshot(repository.database_path)==before
