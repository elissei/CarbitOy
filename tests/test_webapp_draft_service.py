"""Private drafts with real fictional MFA sessions and explicit memberships."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
import sqlite3
from uuid import uuid4

import pytest

from test_webapp_workshop_isolation import http, grant, context, directory, create
from test_webapp_auth_business_ordering import (
    journal, change_authorization, ObservedConnection, assert_writer_excluded, observe_begin,
)
from test_webapp_workshop_migrations import snapshot
from http_auth_support import grant_session
from webapp.auth_service import AuthenticationError, PermissionDeniedError
from webapp.draft_models import (
    DraftType, DraftNotFoundError, DraftRevisionConflictError, DraftIdempotencyConflictError,
)
from webapp.draft_repository import DraftRepository
from webapp.draft_keys import DraftKeyring
from webapp.draft_service import DraftService
from webapp.workshop_context import WorkshopAccessDenied, WorkshopPermission
from webapp.workshop_identity import resolve_workshop_context
import webapp.draft_migrations as migrations


pytestmark = pytest.mark.usefixtures('journal')
KEY = b'Fictional draft fingerprint test key only!'
KEYRING = DraftKeyring(active_key_id='test-v1',keys={'test-v1':KEY},legacy_key_id='test-v1')


@pytest.fixture
def drafts(http):
    grant(http)
    return DraftService(DraftRepository(http.app.state.repository,keyring=KEYRING))


def ctx(http, permission=WorkshopPermission.WRITE_OWN_DRAFT):
    return context(http,permission)


def new(drafts, resolved, *, key=None, payload=None):
    return drafts.create_work_inquiry(resolved,request_id=key or str(uuid4()),
                                      payload={} if payload is None else payload)


@contextmanager
def business_clock(repository, monkeypatch, value):
    original = repository._connect
    @contextmanager
    def timed():
        with original() as connection:
            connection.create_function('unixepoch',-1,lambda *args:value[0])
            yield connection
    with monkeypatch.context() as scoped:
        scoped.setattr(repository,'_connect',timed)
        yield


def test_create_read_list_are_private_and_do_not_change_business_records(http, drafts):
    resolved = ctx(http)
    before = snapshot(http.app.state.repository.database_path)
    receipt = new(drafts,resolved,payload={'registration':'AB-','customer_description':None})
    assert receipt.revision==1 and receipt.state=='ACTIVE'
    row = drafts.read(resolved,receipt.draft_id)
    assert row.payload == {'registration':'AB-','customer_description':None}
    assert row.expires_at-row.last_content_saved_at == 7*24*60*60
    assert drafts.list(resolved)==(row,)
    assert 'registration' not in repr(row)
    after = snapshot(http.app.state.repository.database_path)
    for table,rows in before.items():
        if not table.startswith('draft_'):
            assert after[table]==rows
    assert len(after['draft_audit_events'])==1


@pytest.mark.parametrize('workshop_index', [0,1,2])
def test_owner_workshop_and_organization_never_leak_content_or_existence(http, drafts, workshop_index):
    first = new(drafts,ctx(http),payload={'customer_description':'Private fictional customer text'})
    other = grant_session(http.app.state.auth_service,membership=False)
    directory(http).grant_membership(other.user.user_id,http.workshops[workshop_index],operator_reference='Test only')
    second_ctx = resolve_workshop_context(http.app.state.auth_service,other.token.get_secret_value(),
                                         required_permission=WorkshopPermission.WRITE_OWN_DRAFT)
    own = new(drafts,second_ctx)
    assert [row.draft_id for row in drafts.list(second_ctx)]==[own.draft_id]
    before = snapshot(http.app.state.repository.database_path)
    for target in [first.draft_id,str(uuid4()),'invalid']:
        operations = [lambda:drafts.read(second_ctx,target),
            lambda:drafts.save(second_ctx,target,expected_revision=999,request_id=str(uuid4()),payload={}),
            lambda:drafts.discard(second_ctx,target,expected_revision=999,request_id=str(uuid4()))]
        for operation in operations:
            with pytest.raises(DraftNotFoundError,match='^draft unavailable$'):
                operation()
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('role', ['IT_ADMIN','OPENCLAW'])
def test_role_downgrade_or_service_identity_denies_all_content_and_replays(http, drafts, role):
    resolved = ctx(http)
    key = str(uuid4())
    row = new(drafts,resolved,key=key)
    with http.app.state.auth_service.repository.transaction() as connection:
        if role=='OPENCLAW':
            connection.execute("UPDATE auth_users SET role='OPENCLAW',kind='SERVICE',mfa_enabled=0,password_hash=NULL,totp_ciphertext=NULL,last_totp_step=NULL")
        else:
            connection.execute("UPDATE auth_users SET role='IT_ADMIN'")
    before = snapshot(http.app.state.repository.database_path)
    for operation in [lambda:drafts.read(resolved,row.draft_id), lambda:drafts.list(resolved),
                      lambda:new(drafts,resolved,key=key),
                      lambda:drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={}),
                      lambda:drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()))]:
        with pytest.raises((AuthenticationError,PermissionDeniedError)):
            operation()
    assert snapshot(http.app.state.repository.database_path)==before


def test_context_cannot_be_supplied_as_browser_identity_or_offline_bypass(http, drafts):
    resolved = ctx(http)
    before = snapshot(http.app.state.repository.database_path)
    for forged in ({'employee_id':resolved.employee_id},
                   replace(resolved,auth_binding=None),replace(resolved,actor=None),
                   replace(resolved,employee_id=str(uuid4()),actor=None),
                   replace(resolved,organization_id=str(uuid4())),
                   replace(resolved,workshop_id=http.workshops[1]),
                   replace(resolved,membership_version=resolved.membership_version+1)):
        with pytest.raises((WorkshopAccessDenied,AuthenticationError)):
            new(drafts,forged)
    with pytest.raises(TypeError):
        drafts.create_work_inquiry(resolved,request_id=str(uuid4()),payload={},owner_employee_id=str(uuid4()))
    assert snapshot(http.app.state.repository.database_path)==before


def test_create_and_save_retry_return_original_metadata_after_later_edit_and_restart(http, drafts):
    resolved,key = ctx(http),str(uuid4())
    first = new(drafts,resolved,key=key,payload={'registration':'A'})
    save_key = str(uuid4())
    saved = drafts.save(resolved,first.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'B'})
    later = drafts.save(resolved,first.draft_id,expected_revision=2,request_id=str(uuid4()),payload={'registration':'C'})
    restarted = DraftService(DraftRepository(http.app.state.repository,keyring=KEYRING))
    before = snapshot(http.app.state.repository.database_path)
    assert new(restarted,resolved,key=key,payload={'registration':'A'})==first
    assert restarted.save(resolved,first.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'B'})==saved
    assert restarted.read(resolved,first.draft_id).revision==later.revision==3
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('change', ['payload','revision','operation','type'])
def test_same_key_different_fingerprint_conflicts_without_partial_changes(http, drafts, change):
    resolved,key = ctx(http),str(uuid4())
    row = new(drafts,resolved,key=key,payload={'registration':'A'})
    target = create(http)
    save_key = str(uuid4())
    drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'B'})
    before = snapshot(http.app.state.repository.database_path)
    with pytest.raises(DraftIdempotencyConflictError):
        if change=='payload':
            new(drafts,resolved,key=key,payload={'registration':'other'})
        elif change=='type':
            drafts.create_labour_entry(resolved,request_id=key,payload={},work_id=target['id'],original_work_version=1)
        elif change=='operation':
            drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=save_key)
        else:
            drafts.save(resolved,row.draft_id,expected_revision=2,request_id=save_key,payload={'registration':'B'})
    assert snapshot(http.app.state.repository.database_path)==before


def test_revision_conflict_and_unchanged_saves_do_not_renew_retention(http, drafts, monkeypatch):
    resolved = ctx(http)
    clock = [1791712800]
    with business_clock(http.app.state.repository,monkeypatch,clock):
        row = new(drafts,resolved,payload={'registration':'Ä','customer_description':None})
        clock[0]+=3600
        unchanged = drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),
                                payload={'customer_description':None,'registration':'Ä'})
        assert unchanged==row
        assert drafts.read(resolved,row.draft_id).expires_at==row.expires_at
        changed = drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':'B'})
        assert changed.revision==2 and changed.expires_at==row.expires_at+3600
        before = snapshot(http.app.state.repository.database_path)
        with pytest.raises(DraftRevisionConflictError):
            drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':'C'})
        assert snapshot(http.app.state.repository.database_path)==before


def test_exact_expiry_boundary_hides_content_but_keeps_safe_receipt_replays(http, drafts, monkeypatch):
    resolved,key,save_key = ctx(http),str(uuid4()),str(uuid4())
    clock = [1791712800]
    with business_clock(http.app.state.repository,monkeypatch,clock):
        row = new(drafts,resolved,key=key)
        saved = drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'A'})
        clock[0]=saved.expires_at-1
        assert drafts.read(resolved,row.draft_id)
        clock[0]+=1
        before = snapshot(http.app.state.repository.database_path)
        assert drafts.list(resolved)==()
        for operation in [lambda:drafts.read(resolved,row.draft_id),
            lambda:drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=str(uuid4()))]:
            with pytest.raises(DraftNotFoundError):
                operation()
        assert new(drafts,resolved,key=key)==replace(row,termination_reason='EXPIRED',terminated_at=saved.expires_at)
        assert drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'A'})==replace(
            saved,termination_reason='EXPIRED',terminated_at=saved.expires_at)
        assert snapshot(http.app.state.repository.database_path)==before


def test_discard_removes_content_and_retry_never_recreates_or_touches_business(http, drafts):
    resolved,key = ctx(http),str(uuid4())
    create_key = str(uuid4())
    row = new(drafts,resolved,key=create_key,payload={'customer_description':'Fictional private text'})
    before = snapshot(http.app.state.repository.database_path)
    discarded = drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=key)
    assert discarded.state=='DISCARDED' and discarded.revision==2
    after = snapshot(http.app.state.repository.database_path)
    assert after['draft_contents']==[]
    assert drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=key)==discarded
    replay = new(drafts,resolved,key=create_key,payload={'customer_description':'Fictional private text'})
    assert replay==replace(row,termination_reason='DISCARDED',terminated_at=discarded.terminated_at)
    for operation in [lambda:drafts.read(resolved,row.draft_id),
                      lambda:drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=str(uuid4()))]:
        with pytest.raises(DraftNotFoundError):
            operation()
    for table,rows in before.items():
        if not table.startswith('draft_'):
            assert after[table]==rows
    assert len(after['draft_audit_events'])==2
    assert 'Fictional private text' not in str(after['draft_request_receipts'])
    assert 'Fictional private text' not in str(after['draft_audit_events'])


def test_labour_draft_keeps_original_work_and_version_without_submission(http, drafts):
    resolved = ctx(http)
    target = create(http)
    receipt = drafts.create_labour_entry(resolved,work_id=target['id'],original_work_version=1,
                                         request_id=str(uuid4()),payload={'hours':'1,','source':'MANUA'})
    with http.app.state.repository.operation(resolved,WorkshopPermission.SAVE_LABOUR_PLAN,write=True) as connection:
        connection.execute('UPDATE work_inquiries SET version=2 WHERE id=?',(target['id'],))
    saved = drafts.save(resolved,receipt.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'hours':'not ready'})
    row = drafts.read(resolved,saved.draft_id)
    assert row.original_work_version==1 and row.work_id==target['id']
    before = snapshot(http.app.state.repository.database_path)
    with pytest.raises(DraftRevisionConflictError):
        drafts.create_labour_entry(resolved,work_id=target['id'],original_work_version=1,request_id=str(uuid4()),payload={})
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('bad_payload', [{'password':'x'},{'total_price':'1'},{'registration':1},
    {'registration':'A'*21},{'customer_description':'x'*4001},{'registration':'a\0b'},
    {'registration':'\ud800'},{'registration':['a']},{'owner_employee_id':str(uuid4())}])
def test_bounded_incomplete_payload_does_not_accept_privileged_fields(http, drafts, bad_payload):
    resolved = ctx(http)
    before = snapshot(http.app.state.repository.database_path)
    with pytest.raises(ValueError,match='invalid draft editor payload'):
        new(drafts,resolved,payload=bad_payload)
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('operation',['create','save','discard','cleanup'])
def test_commit_failure_rolls_back_content_receipts_and_audit(http, drafts, monkeypatch, operation):
    resolved = ctx(http)
    row = new(drafts,resolved)
    before = snapshot(http.app.state.repository.database_path)
    repository,original = http.app.state.repository,http.app.state.repository._connect
    @contextmanager
    def failing():
        with original() as connection:
            yield ObservedConnection(connection,commit=lambda c:(_ for _ in ()).throw(sqlite3.OperationalError('test commit failure')))
    monkeypatch.setattr(repository,'_connect',failing)
    with pytest.raises(sqlite3.OperationalError):
        if operation=='create':
            new(drafts,resolved)
        elif operation=='save':
            drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':'A'})
        elif operation=='discard':
            drafts.discard(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()))
        else:
            # Force expiry only on the isolated business connection.
            with business_clock(repository,monkeypatch,[row.expires_at]):
                drafts.cleanup_expired(resolved)
    assert snapshot(repository.database_path)==before


@pytest.mark.parametrize('state', ['session','role','disabled','version','membership'])
@pytest.mark.parametrize('operation', ['read','create-retry','save-retry','discard-retry'])
def test_revocations_deny_reads_and_lost_response_replays_without_changes(http, drafts, state, operation):
    resolved,create_key,save_key,discard_key = ctx(http),str(uuid4()),str(uuid4()),str(uuid4())
    row = new(drafts,resolved,key=create_key)
    drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'A'})
    if operation=='discard-retry':
        drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=discard_key)
    change_authorization(http,state)
    before = snapshot(http.app.state.repository.database_path)
    calls={'read':lambda:drafts.read(resolved,row.draft_id),
        'create-retry':lambda:new(drafts,resolved,key=create_key),
        'save-retry':lambda:drafts.save(resolved,row.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'A'}),
        'discard-retry':lambda:drafts.discard(resolved,row.draft_id,expected_revision=2,request_id=discard_key)}
    with pytest.raises((AuthenticationError,WorkshopAccessDenied)):
        calls[operation]()
    assert snapshot(http.app.state.repository.database_path)==before


def test_same_revision_competing_saves_have_one_winner(http, drafts):
    resolved = ctx(http)
    row = new(drafts,resolved)
    ready = Barrier(2)
    def save(value):
        ready.wait(timeout=3)
        try:
            return drafts.save(resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':value})
        except DraftRevisionConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(save,value) for value in ['A','B']]
        results=[future.result(timeout=5) for future in futures]
    assert sum(result is not None for result in results)==1
    assert drafts.read(resolved,row.draft_id).revision==2
    with http.app.state.repository._connect() as connection:
        assert connection.execute("SELECT count(*) FROM draft_request_receipts WHERE operation='SAVE'").fetchone()[0]==1


@pytest.mark.parametrize('state', ['session','role','disabled','version','membership'])
def test_draft_commit_precedes_waiting_auth_or_membership_revocation(http, drafts, monkeypatch, state):
    resolved = ctx(http)
    row = new(drafts,resolved)
    ready,release,changing,committed = Event(),Event(),Event(),Event()
    repository,original = http.app.state.repository,http.app.state.repository._connect
    def commit(connection):
        ready.set()
        assert release.wait(3)
        connection.commit()
        committed.set()
    @contextmanager
    def observed():
        with original() as connection:
            yield ObservedConnection(connection,commit=commit)
    monkeypatch.setattr(repository,'_connect',observed)
    target = repository if state=='membership' else http.app.state.auth_service.repository
    observe_begin(target,monkeypatch,changing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(drafts.save,resolved,row.draft_id,expected_revision=1,request_id=str(uuid4()),payload={'registration':'A'})
        try:
            assert ready.wait(2)
            assert_writer_excluded(repository)
            assert_writer_excluded(http.app.state.auth_service.repository)
            changing.clear()  # Ignore the writer probes and saving BEGIN trace.
            revoking = pool.submit(change_authorization,http,state)
            assert changing.wait(2)
            assert not revoking.done()
        finally:
            release.set()
        assert saving.result(timeout=3).revision==2
        revoking.result(timeout=3)
    assert committed.is_set()
    with pytest.raises((AuthenticationError,WorkshopAccessDenied)):
        drafts.read(resolved,row.draft_id)


def test_internal_cleanup_is_bounded_owner_only_and_never_deletes_business(http, drafts, monkeypatch):
    resolved,clock = ctx(http),[1791712800]
    with business_clock(http.app.state.repository,monkeypatch,clock):
        rows = [new(drafts,resolved) for _ in range(3)]
        other = grant_session(http.app.state.auth_service,membership=False)
        directory(http).grant_membership(other.user.user_id,http.workshops[0],operator_reference='Explicit test membership')
        other_ctx = resolve_workshop_context(http.app.state.auth_service,other.token.get_secret_value(),
                                             required_permission=WorkshopPermission.WRITE_OWN_DRAFT)
        foreign = new(drafts,other_ctx)
        clock[0] = rows[0].expires_at
        before = snapshot(http.app.state.repository.database_path)
        assert drafts.cleanup_expired(resolved,batch_size=2)==2
        assert drafts.cleanup_expired(resolved,batch_size=2)==1
        after = snapshot(http.app.state.repository.database_path)
        assert [row[0] for row in after['draft_contents']]==[foreign.draft_id]
        for table,data in before.items():
            if not table.startswith('draft_'):
                assert after[table]==data
        for batch in [None,-1,0,101,'1',True,1.5]:
            with pytest.raises(ValueError):
                drafts.cleanup_expired(resolved,batch_size=batch)


def test_missing_membership_and_regrant_cannot_revive_stale_context(http, drafts):
    resolved = ctx(http)
    row = new(drafts,resolved)
    directory(http).revoke_membership(http.grant.user.user_id,http.workshops[0],operator_reference='Test revocation')
    grant(http)
    before = snapshot(http.app.state.repository.database_path)
    for call in [lambda:drafts.read(resolved,row.draft_id),lambda:drafts.cleanup_expired(resolved)]:
        with pytest.raises(WorkshopAccessDenied):
            call()
    assert snapshot(http.app.state.repository.database_path)==before
    assert drafts.read(ctx(http),row.draft_id).draft_id==row.draft_id


def test_concurrent_same_nonce_creations_and_saves_are_single_effect(http, drafts):
    resolved,key = ctx(http),str(uuid4())
    gate = Barrier(2)
    def creating():
        gate.wait(timeout=3)
        return new(drafts,resolved,key=key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(creating) for _ in range(2)]
        left,right=[future.result(timeout=5) for future in futures]
    assert left==right
    save_key = str(uuid4())
    def saving():
        gate.wait(timeout=3)
        return drafts.save(resolved,left.draft_id,expected_revision=1,request_id=save_key,payload={'registration':'A'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(saving) for _ in range(2)]
        first,second=[future.result(timeout=5) for future in futures]
    assert first==second and first.revision==2
    with http.app.state.repository._connect() as connection:
        assert connection.execute('SELECT count(*) FROM draft_registry').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM draft_request_receipts').fetchone()[0]==2
        assert connection.execute('SELECT count(*) FROM draft_audit_events').fetchone()[0]==2


def test_foreign_or_missing_labour_work_reference_returns_same_generic_error(http, drafts):
    resolved = ctx(http)
    other = grant_session(http.app.state.auth_service,membership=False)
    directory(http).grant_membership(other.user.user_id,http.workshops[2],operator_reference='Foreign organization test')
    foreign_ctx = resolve_workshop_context(http.app.state.auth_service,other.token.get_secret_value(),
                                           required_permission=WorkshopPermission.SAVE_LABOUR_PLAN)
    target = http.app.state.repository.create_work(context=foreign_ctx,actor=foreign_ctx.actor,
        registration='TEST',customer_description='Fictional',idempotency_key=str(uuid4()))
    before = snapshot(http.app.state.repository.database_path)
    for work_id in [target['id'],str(uuid4())]:
        with pytest.raises(DraftNotFoundError,match='^draft unavailable$'):
            drafts.create_labour_entry(resolved,work_id=work_id,original_work_version=999,request_id=str(uuid4()),payload={})
    assert snapshot(http.app.state.repository.database_path)==before


def test_v1_creation_receipt_without_fingerprint_is_never_silently_adopted(http, drafts):
    resolved = ctx(http)
    key = str(uuid4())
    with http.app.state.repository.operation(resolved,WorkshopPermission.WRITE_OWN_DRAFT,write=True) as connection:
        draft_id = connection.execute('''INSERT INTO draft_registry
            (organization_id,workshop_id,auth_store_id,owner_employee_id,draft_type)
            VALUES (?,?,?,?,'WORK_INQUIRY') RETURNING draft_id''',
            (resolved.organization_id,resolved.workshop_id,resolved.auth_store_id,resolved.employee_id)).fetchone()[0]
        connection.execute('INSERT INTO draft_contents(draft_id) VALUES (?)',(draft_id,))
        connection.execute('''INSERT INTO draft_creation_receipts
            (creation_request_id,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            VALUES (?,?,?,?,?,'WORK_INQUIRY')''',
            (key,draft_id,resolved.auth_store_id,resolved.employee_id,resolved.workshop_id))
    before = snapshot(http.app.state.repository.database_path)
    with pytest.raises(DraftIdempotencyConflictError):
        new(drafts,resolved,key=key)
    assert snapshot(http.app.state.repository.database_path)==before


def test_new_receipts_are_immutable_and_replacement_cannot_reset_them(http, drafts):
    resolved = ctx(http)
    new(drafts,resolved)
    before = snapshot(http.app.state.repository.database_path)
    with http.app.state.repository.operation(resolved,WorkshopPermission.WRITE_OWN_DRAFT,write=True) as connection:
        for statement in ['UPDATE draft_request_receipts SET request_id=request_id',
                          'DELETE FROM draft_request_receipts',
                          '''INSERT OR REPLACE INTO draft_request_receipts
                            (request_id,operation,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type,
                            request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state,recorded_at)
                            SELECT request_id,operation,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type,
                            request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state,recorded_at
                            FROM draft_request_receipts''']:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
    assert snapshot(http.app.state.repository.database_path)==before
