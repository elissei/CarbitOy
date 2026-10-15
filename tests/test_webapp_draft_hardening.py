"""Offline deletion, rotation, terminal replay and retention under real locks."""

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from uuid import uuid4

import pytest

from test_webapp_draft_service import (
    http, journal, drafts, ctx, new, business_clock, KEY, KEYRING,
    snapshot, change_authorization, ObservedConnection, assert_writer_excluded, observe_begin,
)
from webapp.draft_keys import DraftKeyring, DraftKeyConfigurationError
from webapp.draft_maintenance import DraftMaintenance, DraftRetentionPolicy
from webapp.draft_models import DraftNotFoundError, DraftIdempotencyConflictError
from webapp.draft_repository import DraftRepository
from webapp.draft_service import DraftService
from webapp.auth_service import AuthenticationError
from webapp.workshop_context import WorkshopAccessDenied
from webapp.workshop_context import WorkshopPermission


pytestmark = pytest.mark.usefixtures('journal')
NEXT = bytes(range(32))  # Fictional tests only, never deployment configuration.


@pytest.mark.parametrize('state',['membership','disabled','session','role','version'])
def test_revoked_owner_cleanup_does_not_use_auth_and_never_reads_payload(http, drafts, monkeypatch, state):
    repository = http.app.state.repository
    resolved = ctx(http)
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        expired = new(drafts,resolved,payload={'customer_description':'private fictional text'})
        now[0] += 86400
        active = new(drafts,resolved)
        change_authorization(http,state)
        # No revoked session and no auth database access permitted by executor.
        monkeypatch.setattr(http.app.state.auth_service.repository,'_connect',
                            lambda:(_ for _ in ()).throw(AssertionError('auth accessed')))
        now[0] = expired.expires_at
        maintenance = DraftMaintenance(repository)
        assert maintenance.cleanup_expired(batch_size=1)==1
        assert maintenance.cleanup_expired(batch_size=1)==0
        with repository._connect() as connection:
            assert [r[0] for r in connection.execute('SELECT draft_id FROM draft_contents')]==[active.draft_id]
            assert tuple(connection.execute('SELECT reason,terminated_at FROM draft_terminations').fetchone())==(
                'EXPIRED',expired.expires_at)
            assert 'private fictional text' not in str(connection.execute('SELECT * FROM draft_audit_events').fetchall())
        assert not hasattr(maintenance,'read') and not hasattr(maintenance,'list')


@pytest.mark.parametrize('kind',['expired','discarded'])
def test_terminal_create_save_replay_is_metadata_only_and_conflicts_on_changed_request(http, drafts, monkeypatch, kind):
    resolved = ctx(http)
    repository = http.app.state.repository
    now = [1791712800]
    creation,save = str(uuid4()),str(uuid4())
    with business_clock(repository,monkeypatch,now):
        created = new(drafts,resolved,key=creation,payload={'registration':'A'})
        saved = drafts.save(resolved,created.draft_id,expected_revision=1,request_id=save,payload={'registration':'B'})
        if kind=='discarded':
            drafts.discard(resolved,created.draft_id,expected_revision=2,request_id=str(uuid4()))
        else:
            now[0] = saved.expires_at
            DraftMaintenance(repository).cleanup_expired()
        before = snapshot(repository.database_path)
        a = new(drafts,resolved,key=creation,payload={'registration':'A'})
        b = drafts.save(resolved,created.draft_id,expected_revision=1,request_id=save,payload={'registration':'B'})
        assert a.revision==1 and b.revision==2
        assert a.termination_reason==b.termination_reason==kind.upper()
        assert not hasattr(a,'payload') and not hasattr(b,'payload')
        with pytest.raises(DraftIdempotencyConflictError):
            new(drafts,resolved,key=creation,payload={'registration':'Changed'})
        with pytest.raises(DraftIdempotencyConflictError):
            drafts.save(resolved,created.draft_id,expected_revision=1,request_id=save,payload={})
        with pytest.raises(DraftNotFoundError):
            drafts.read(resolved,created.draft_id)
        assert snapshot(repository.database_path)==before


@pytest.mark.parametrize('state',['membership','disabled','session','role','version'])
def test_terminated_replay_still_revalidates_live_authorization(http, drafts, state):
    resolved,key = ctx(http),str(uuid4())
    row = new(drafts,resolved,key=key)
    drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()))
    change_authorization(http,state)
    with pytest.raises((AuthenticationError,WorkshopAccessDenied)):
        new(drafts,resolved,key=key)


def test_rotation_uses_old_key_for_replay_and_active_key_for_new_receipt(http, drafts):
    resolved,key = ctx(http),str(uuid4())
    row = new(drafts,resolved,key=key)
    ring = DraftKeyring(active_key_id='next',keys={'test-v1':KEY,'next':NEXT},legacy_key_id='test-v1')
    rotated = DraftService(DraftRepository(http.app.state.repository,keyring=ring))
    assert new(rotated,resolved,key=key)==row
    saved = rotated.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':'B'})
    with http.app.state.repository._connect() as connection:
        assert [r[0] for r in connection.execute('SELECT fingerprint_key_id FROM draft_request_receipts ORDER BY accepted_revision')]==['test-v1','next']
    rotated.discard(resolved,row.draft_id,expected_revision=saved.revision,request_id=str(uuid4()))
    assert new(rotated,resolved,key=key).termination_reason=='DISCARDED'
    missing = DraftService(DraftRepository(http.app.state.repository,keyring=DraftKeyring(active_key_id='next',keys={'next':NEXT})))
    with pytest.raises(DraftKeyConfigurationError):
        new(missing,resolved,key=key)
    with pytest.raises(DraftKeyConfigurationError):
        new(missing,resolved)  # Do not accept new work while old receipts are unverifiable.


@pytest.mark.parametrize('environment', [{}, {'CARBIT_DRAFT_HMAC_KEYS':'secret malformed'},
    {'CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID':'test','CARBIT_DRAFT_HMAC_KEYS':'{"test":"NOT BASE64 SECRET"}'},
    {'CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID':'test','CARBIT_DRAFT_HMAC_KEYS':'[]'}])
def test_config_fails_closed_without_secret_in_error(environment):
    with pytest.raises(DraftKeyConfigurationError) as error:
        DraftKeyring.from_environment(environment)
    assert str(error.value)=='draft fingerprint key configuration unavailable'


@pytest.mark.parametrize('key', [b'',b'short',b'0'*32,b'a'*64])
def test_short_and_obviously_weak_keys_are_rejected(key):
    with pytest.raises(DraftKeyConfigurationError):
        DraftKeyring(active_key_id='test',keys={'test':key})


def test_explicit_environment_keyring_is_secret_free_in_repr():
    ring = DraftKeyring.from_environment({'CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID':'test',
        'CARBIT_DRAFT_HMAC_KEYS':json.dumps({'test':base64.b64encode(NEXT).decode()})})
    assert ring.fingerprint(b'fictional request',key_id='test')
    assert repr(NEXT) not in repr(ring)


@pytest.mark.parametrize('days',[1,90])
def test_retention_boundary_is_from_termination_and_cleanup_has_total_row_budget(http, drafts, monkeypatch, days):
    repository,resolved = http.app.state.repository,ctx(http)
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        row = new(drafts,resolved)
        active = new(drafts,resolved)
        now[0] += 3600
        ended = drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()))
        maintenance = DraftMaintenance(repository,policy=DraftRetentionPolicy(days))
        now[0] = ended.terminated_at+days*86400-1
        assert maintenance.cleanup_metadata()==0
        # Keep the other draft active even when its creation receipt is older
        # than the metadata horizon (seven-day sliding content saves).
        # For 90 days, advance through legal content saves without reactivating.
        now[0] = active.last_content_saved_at
        revision=1
        while now[0]+6*86400 < ended.terminated_at+days*86400:
            now[0] += 6*86400
            revision += 1
            drafts.save(resolved,active.draft_id,expected_revision=revision-1,request_id=str(uuid4()),payload={'registration':str(revision)})
        now[0] = ended.terminated_at+days*86400
        with pytest.raises(DraftNotFoundError):
            # Horizon ends independent of physical cleanup scheduling.
            drafts.discard(resolved,row.draft_id,expected_revision=1,request_id='00000000-0000-0000-0000-000000000000')
        total=0
        for _ in range(30):
            removed = maintenance.cleanup_metadata(batch_size=2)
            assert 0<=removed<=2
            total += removed
            if not removed:
                break
        assert total==7  # 2 request + 1 creation + 2 audit + registry + termination.
        with repository._connect() as connection:
            assert [r[0] for r in connection.execute('SELECT draft_id FROM draft_registry')]==[active.draft_id]
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert drafts.read(resolved,active.draft_id)


def test_late_cleanup_keeps_exact_expiration_timestamp(http, drafts, monkeypatch):
    repository,resolved = http.app.state.repository,ctx(http)
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        row = new(drafts,resolved)
        now[0] = row.expires_at+90*86400
        maintenance = DraftMaintenance(repository)
        assert maintenance.cleanup_metadata()==0  # Content must be removed first.
        assert maintenance.cleanup_expired()==1
        with repository._connect() as connection:
            assert connection.execute('SELECT terminated_at FROM draft_terminations').fetchone()[0]==row.expires_at
        assert maintenance.cleanup_metadata()==6


@pytest.mark.parametrize('operation',['content','metadata'])
def test_maintenance_commit_failure_rolls_back_every_row(http, drafts, monkeypatch, operation):
    repository = http.app.state.repository
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        row = new(drafts,ctx(http))
        now[0] = row.expires_at+90*86400
        maintenance = DraftMaintenance(repository,policy=DraftRetentionPolicy(1))
        if operation=='metadata':
            maintenance.cleanup_expired()
        before = snapshot(repository.database_path)
        original = repository._connect
        @contextmanager
        def failing():
            with original() as connection:
                yield ObservedConnection(connection,commit=lambda c:(_ for _ in ()).throw(sqlite3.OperationalError('test failure')))
        monkeypatch.setattr(repository,'_connect',failing)
        with pytest.raises(sqlite3.OperationalError):
            (maintenance.cleanup_expired if operation=='content' else maintenance.cleanup_metadata)()
        assert snapshot(repository.database_path)==before


@pytest.mark.parametrize('state',['membership','disabled'])
def test_cleanup_and_concurrent_authorization_changes_do_not_invert_locks(http, drafts, monkeypatch, state):
    repository = http.app.state.repository
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        row = new(drafts,ctx(http))
        now[0] = row.expires_at
        ready,release,changing = Event(),Event(),Event()
        original = repository._connect
        def commit(connection):
            ready.set()
            assert release.wait(3)
            connection.commit()
        @contextmanager
        def observed():
            with original() as connection:
                yield ObservedConnection(connection,commit=commit)
        monkeypatch.setattr(repository,'_connect',observed)
        target = repository if state=='membership' else http.app.state.auth_service.repository
        observe_begin(target,monkeypatch,changing)
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleaning = pool.submit(DraftMaintenance(repository).cleanup_expired)
            try:
                assert ready.wait(2)
                assert_writer_excluded(repository)
                changing.clear()
                revoking = pool.submit(change_authorization,http,state)
                assert changing.wait(2)
                if state=='membership':
                    assert not revoking.done()
                else:
                    revoking.result(timeout=2)  # Auth is independent, no auth lock needed.
            finally:
                release.set()
            assert cleaning.result(timeout=3)==1
            revoking.result(timeout=3)


@pytest.mark.parametrize('batch',[0,-1,101,True,1.5,'1',None])
def test_maintenance_batch_cannot_be_unbounded(http, drafts, batch):
    before = snapshot(http.app.state.repository.database_path)
    maintenance = DraftMaintenance(http.app.state.repository)
    for operation in (maintenance.cleanup_expired,maintenance.cleanup_metadata):
        with pytest.raises(ValueError):
            operation(batch_size=batch)
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('days',[0,91,True,1.5,'90',None])
def test_metadata_policy_cannot_exceed_ninety_days_or_disable_guard(days):
    with pytest.raises(ValueError):
        DraftRetentionPolicy(days)


def test_metadata_batch_must_allow_final_registry_and_termination_pair(http, drafts):
    with pytest.raises(ValueError):
        DraftMaintenance(http.app.state.repository).cleanup_metadata(batch_size=1)


def test_maintenance_connection_denies_customer_reads_and_business_writes(http, drafts):
    maintenance = DraftMaintenance(http.app.state.repository)
    with maintenance._transaction() as connection:
        for statement in ('SELECT payload_json FROM draft_contents',
                          'SELECT customer_description FROM work_inquiries',
                          'DELETE FROM work_inquiries',
                          'UPDATE workshop_memberships SET enabled=0'):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)


@pytest.mark.parametrize('operation',['create','save'])
def test_receipt_replay_horizon_ends_even_before_metadata_cleanup(http, drafts, monkeypatch, operation):
    repository,resolved = http.app.state.repository,ctx(http)
    now = [1791712800]
    with business_clock(repository,monkeypatch,now):
        key = str(uuid4())
        row = new(drafts,resolved,key=key)
        save_key = str(uuid4())
        drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'B'})
        ended = drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=str(uuid4()))
        replay = lambda: new(drafts,resolved,key=key) if operation=='create' else drafts.save(
            resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'B'})
        now[0] = ended.terminated_at+90*86400-1
        assert replay().termination_reason=='DISCARDED'
        now[0] += 1
        with pytest.raises(DraftNotFoundError):
            replay()


def test_backup_receipts_require_restored_old_key_and_never_guess_legacy_key(http, drafts, monkeypatch, tmp_path):
    from webapp.database import WorkRepository
    import webapp.draft_migrations as migrations
    repository = http.app.state.repository
    resolved,key = ctx(http),str(uuid4())
    # Build genuine v2 receipts by temporarily omitting ONLY migration 3.
    path = tmp_path/'legacy-receipts.db'
    legacy = WorkRepository(path)
    with monkeypatch.context() as scoped:
        scoped.setattr(migrations,'apply_migration_3',lambda *args:None)
        legacy.initialize()
    # Copy the test-only scope from the configured business DB, retaining its
    # auth store namespace. No real users/accounts/memberships are provisioned.
    with repository._connect() as source,legacy._connect() as destination,destination:
        for table in ('repair_organizations','workshops','workshop_principals','workshop_memberships','active_user_workshops'):
            rows = source.execute(f'SELECT * FROM {table}').fetchall()
            if rows:
                marks=','.join('?' for _ in rows[0])
                destination.executemany(f'INSERT INTO {table} VALUES ({marks})',map(tuple,rows))
    message = drafts.repository._message(resolved,'CREATE',dict(type='WORK_INQUIRY',payload={},work_id=None,original_work_version=None))
    with legacy.operation(resolved,WorkshopPermission.WRITE_OWN_DRAFT,write=True) as c:
        draft_id = c.execute('''INSERT INTO draft_registry
            (organization_id,workshop_id,auth_store_id,owner_employee_id,draft_type)
            VALUES (?,?,?,?,'WORK_INQUIRY') RETURNING draft_id''',
            (resolved.organization_id,resolved.workshop_id,resolved.auth_store_id,resolved.employee_id)).fetchone()[0]
        c.execute('INSERT INTO draft_contents(draft_id) VALUES (?)',(draft_id,))
        c.execute('''INSERT INTO draft_creation_receipts
            (creation_request_id,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            VALUES (?,?,?,?,?,'WORK_INQUIRY')''',(key,draft_id,resolved.auth_store_id,resolved.employee_id,resolved.workshop_id))
        c.execute('''INSERT INTO draft_request_receipts
            (request_id,operation,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type,
             request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state)
            VALUES (?,'CREATE',?,?,?,?,'WORK_INQUIRY',?,1,unixepoch('now'),'ACTIVE')''',
            (key,draft_id,resolved.auth_store_id,resolved.employee_id,resolved.workshop_id,
             KEYRING.fingerprint(message,key_id='test-v1')))
    legacy.initialize()
    explicit = DraftService(DraftRepository(legacy,keyring=KEYRING))
    assert new(explicit,resolved,key=key).draft_id==draft_id
    absent = DraftService(DraftRepository(legacy,keyring=DraftKeyring(active_key_id='test-v1',keys={'test-v1':KEY})))
    with pytest.raises(DraftKeyConfigurationError):
        new(absent,resolved,key=key)
    with legacy._connect() as source,sqlite3.connect(tmp_path/'backup.db') as backup:
        source.backup(backup)
    restored = WorkRepository(tmp_path/'backup.db')
    restored.initialize()
    assert new(DraftService(DraftRepository(restored,keyring=KEYRING)),resolved,key=key).draft_id==draft_id


@pytest.mark.parametrize('state',['membership','disabled','session','role','version'])
def test_historical_replay_holds_live_authorization_through_ack_commit(http, drafts, monkeypatch, state):
    resolved,key = ctx(http),str(uuid4())
    row = new(drafts,resolved,key=key)
    drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()))
    repository = http.app.state.repository
    before = snapshot(repository.database_path)
    ready,release,changing = Event(),Event(),Event()
    original=repository._connect
    def commit(connection):
        ready.set()
        assert release.wait(3)
        connection.commit()
    @contextmanager
    def observed():
        with original() as connection:
            yield ObservedConnection(connection,commit=commit)
    monkeypatch.setattr(repository,'_connect',observed)
    target=repository if state=='membership' else http.app.state.auth_service.repository
    observe_begin(target,monkeypatch,changing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replaying=pool.submit(new,drafts,resolved,key=key)
        try:
            assert ready.wait(2)
            assert_writer_excluded(repository)
            assert_writer_excluded(http.app.state.auth_service.repository)
            changing.clear()
            revoking=pool.submit(change_authorization,http,state)
            assert changing.wait(2)
            assert not revoking.done()
        finally:
            release.set()
        assert replaying.result(timeout=3).termination_reason=='DISCARDED'
        revoking.result(timeout=3)
    after=snapshot(repository.database_path)
    for table,rows in before.items():
        if table.startswith('draft_'):
            assert after[table]==rows
    with pytest.raises((AuthenticationError,WorkshopAccessDenied)):
        new(drafts,resolved,key=key)


def test_maintenance_deadline_rolls_back_instead_of_unbounded_commit(http, drafts, monkeypatch):
    import webapp.draft_maintenance as module
    repository = http.app.state.repository
    now=[1791712800]
    with business_clock(repository,monkeypatch,now):
        row=new(drafts,ctx(http))
        now[0]=row.expires_at
        before=snapshot(repository.database_path)
        calls=[0]
        def elapsed():
            calls[0]+=1
            return 0 if calls[0]==1 else 10
        monkeypatch.setattr(module,'monotonic',elapsed)
        with pytest.raises(sqlite3.OperationalError):
            DraftMaintenance(repository).cleanup_expired()
        assert snapshot(repository.database_path)==before
