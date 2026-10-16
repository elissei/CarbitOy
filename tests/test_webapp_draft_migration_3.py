"""Real v2 upgrades, terminal timestamps, forward-only guards and rollback."""

import sqlite3

import pytest

from webapp.database import WorkRepository
import webapp.draft_migrations as migrations
import webapp.draft_hardening_migrations as hardening
from webapp.draft_maintenance import DraftMaintenance
from test_webapp_draft_migrations import clock, insert_draft, receipt, work, NOW, CTX, PERMISSION
from test_webapp_workshop_migrations import snapshot
from workshop_support import seed_test_scope


def version_two(tmp_path, monkeypatch):
    repository = WorkRepository(tmp_path/'v2.db')
    with monkeypatch.context() as scoped:
        scoped.setattr(migrations,'apply_migration_3',lambda *args:None)
        scoped.setattr(migrations,'apply_migration_4',lambda *args:None)
        repository.initialize()
    seed_test_scope(repository)
    return repository


def test_upgrade_preserves_rows_and_derives_expiry_from_save_not_late_cleanup(tmp_path,monkeypatch):
    repository = version_two(tmp_path,monkeypatch)
    with repository.operation(CTX,PERMISSION,write=True) as c:
        clock(c)
        active = insert_draft(c)
        expired = insert_draft(c)
        receipt(c,expired)
        clock(c,NOW+86400)
        c.execute("UPDATE draft_contents SET payload_json='{}',revision=revision WHERE draft_id=?",(active,))
        c.execute('''UPDATE draft_contents SET payload_json='{"registration":"B"}',
            revision=revision+1,last_content_saved_at=unixepoch('now') WHERE draft_id=?''',(expired,))
        clock(c,NOW+200*86400)
        c.execute('DELETE FROM draft_contents WHERE draft_id=?',(expired,))
    before=snapshot(repository.database_path)
    repository.initialize()
    after=snapshot(repository.database_path)
    for table,rows in before.items():
        if table!='draft_schema_migrations':
            assert after[table]==rows
    assert {r[0] for r in after['draft_schema_migrations']}=={1,2,3,4}
    with repository._connect() as c:
        assert tuple(c.execute('SELECT reason,terminated_at FROM draft_terminations WHERE draft_id=?',(expired,)).fetchone())==(
            'EXPIRED',NOW+8*86400)
        assert not c.execute('SELECT * FROM draft_terminations WHERE draft_id=?',(active,)).fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    repository.initialize()
    assert snapshot(repository.database_path)==after


def test_third_migration_failure_rolls_back_ddl_ledger_and_backfill(tmp_path,monkeypatch):
    repository=version_two(tmp_path,monkeypatch)
    before=snapshot(repository.database_path)
    with repository._connect() as c:
        schema=[tuple(row) for row in c.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
    monkeypatch.setattr(hardening,'MIGRATION_3',hardening.MIGRATION_3+('INVALID SQL',))
    with pytest.raises(sqlite3.DatabaseError):
        repository.initialize()
    assert snapshot(repository.database_path)==before
    with repository._connect() as c:
        assert [tuple(row) for row in c.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]==schema


def test_future_conversion_retention_does_not_delete_business_evidence(tmp_path,monkeypatch):
    repository=WorkRepository(tmp_path/'converted.db')
    repository.initialize()
    seed_test_scope(repository)
    with repository.operation(CTX,PERMISSION,write=True) as c:
        clock(c)
        work(c)
        draft_id=insert_draft(c)
        receipt(c,draft_id)
        receipt(c,draft_id,conversion=True)
        clock(c,NOW+3600)
        c.execute("UPDATE draft_contents SET state='CONVERTED',revision=2 WHERE draft_id=?",(draft_id,))
        assert tuple(c.execute('SELECT reason,terminated_at FROM draft_terminations').fetchone())==('CONVERTED',NOW)
    from test_webapp_draft_service import business_clock
    before=snapshot(repository.database_path)
    with business_clock(repository,monkeypatch,[NOW+90*86400]):
        executor=DraftMaintenance(repository)
        assert executor.cleanup_expired()==1
        assert executor.cleanup_metadata()>0
    after=snapshot(repository.database_path)
    assert after['draft_registry']==after['draft_conversion_receipts']==after['draft_audit_events']==[]
    for table,rows in before.items():
        if not table.startswith('draft_'):
            assert after[table]==rows


def test_retention_guards_cannot_be_disabled_or_replace_immutable_termination(tmp_path):
    repository=WorkRepository(tmp_path/'guards.db')
    repository.initialize()
    seed_test_scope(repository)
    with repository.operation(CTX,PERMISSION,write=True) as c:
        clock(c)
        draft_id=insert_draft(c)
        clock(c,NOW+7*86400)
        c.execute('DELETE FROM draft_contents WHERE draft_id=?',(draft_id,))
        for sql in ('DELETE FROM draft_retention_policy',
                    'UPDATE draft_retention_policy SET metadata_seconds=0',
                    'UPDATE draft_retention_policy SET metadata_seconds=7776001',
                    'UPDATE draft_terminations SET terminated_at=0',
                    'DELETE FROM draft_terminations',
                    'DELETE FROM draft_registry',
                    'DELETE FROM draft_audit_events',
                    "INSERT OR REPLACE INTO draft_terminations SELECT draft_id,reason,0 FROM draft_terminations"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
