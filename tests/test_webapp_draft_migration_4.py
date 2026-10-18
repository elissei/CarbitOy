"""Real populated v2/v3 upgrades; requester separation without guessed change flags."""
import json
import sqlite3
from uuid import uuid4
import pytest

from webapp.database import WorkRepository
from webapp.draft_repository import DraftRepository
from webapp.draft_service import DraftService
from webapp.draft_models import DraftType
from webapp.workshop_context import WorkshopPermission
import webapp.draft_migrations as migrations
import webapp.draft_shared_migrations as shared
from test_webapp_draft_service import http, drafts, journal, ctx, colleague, authors, business_clock, KEYRING
from test_webapp_workshop_isolation import create
from test_webapp_workshop_migrations import snapshot
from test_webapp_draft_migrations import clock, NOW

pytestmark=pytest.mark.usefixtures('journal')


def original_message(person,operation,request):
    # Frozen original wire fingerprint, independently of the changed helper.
    return json.dumps(['carbit.private-draft-request.v1',person.organization_id,person.workshop_id,
        person.auth_store_id,person.employee_id,operation,request],sort_keys=True,
        ensure_ascii=False,separators=(',',':')).encode('utf-8')


def populated_legacy(http,drafts,tmp_path,monkeypatch,kind,version):
    first,second=ctx(http),colleague(http)
    parent=create(http) if kind==DraftType.LABOUR_ENTRY else None
    repo=WorkRepository(tmp_path/'populated-legacy.db')
    with monkeypatch.context() as old:
        old.setattr(migrations,'apply_migration_4',lambda *args:None)
        if version==2:old.setattr(migrations,'apply_migration_3',lambda *args:None)
        repo.initialize()
    with http.app.state.repository._connect() as source,repo._connect() as dest,dest:
        for table in ['repair_organizations','workshops','workshop_principals','workshop_memberships',
                      'active_user_workshops','work_inquiries','inquiry_workshop_assignments']:
            rows=source.execute('SELECT * FROM '+table).fetchall()
            if rows:dest.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in rows[0])+')',map(tuple,rows))
    payload={'registration':'OLD'} if kind==DraftType.WORK_INQUIRY else {'hours':'unfinished'}
    changed={'registration':'SAVED'} if kind==DraftType.WORK_INQUIRY else {'hours':'1.5'}
    keys={name:str(uuid4()) for name in ['create','save','noop','discard']}
    def record(c,draft_id,key,op,request):
        message=original_message(first,op,request)
        extra=',fingerprint_key_id' if version==3 else ''
        value=",'test-v1'" if version==3 else ''
        c.execute('''INSERT INTO draft_request_receipts
            (request_id,operation,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type,
             request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state'''+extra+''')
            SELECT ?,?,d.draft_id,d.auth_store_id,d.owner_employee_id,d.workshop_id,d.draft_type,?,
                c.revision+(?='DISCARD'),c.last_content_saved_at,
                CASE WHEN ?='DISCARD' THEN 'DISCARDED' ELSE 'ACTIVE' END'''+value+'''
            FROM draft_registry d JOIN draft_contents c USING(draft_id) WHERE d.draft_id=?''',
            (key,op,KEYRING.fingerprint(message,key_id='test-v1'),op,op,draft_id))
    def insert(c,key,when):
        clock(c,when)
        work_id=parent['id'] if parent else None;work_version=parent['version'] if parent else None
        did=c.execute('''INSERT INTO draft_registry
            (organization_id,workshop_id,auth_store_id,owner_employee_id,draft_type,work_id,original_work_version)
            VALUES (?,?,?,?,?,?,?) RETURNING draft_id''',(first.organization_id,first.workshop_id,
            first.auth_store_id,first.employee_id,kind.value,work_id,work_version)).fetchone()[0]
        c.execute('INSERT INTO draft_contents(draft_id,payload_json) VALUES (?,?)',(did,json.dumps(payload)))
        c.execute('''INSERT INTO draft_creation_receipts
            (creation_request_id,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            VALUES (?,?,?,?,?,?)''',(key,did,first.auth_store_id,first.employee_id,first.workshop_id,kind.value))
        record(c,did,key,'CREATE',dict(type=kind.value,payload=payload,work_id=work_id,original_work_version=work_version))
        return did
    with repo.operation(first,WorkshopPermission.WRITE_WORKSHOP_DRAFT,write=True) as c:
        did=insert(c,keys['create'],NOW)
        clock(c,NOW+10)
        c.execute('''UPDATE draft_contents SET payload_json=?,revision=2,last_content_saved_at=unixepoch('now')
            WHERE draft_id=?''',(json.dumps(changed),did))
        saved_request=dict(draft_id=did,expected_revision=1,payload=changed)
        record(c,did,keys['save'],'SAVE',saved_request)
        record(c,did,keys['noop'],'SAVE',dict(draft_id=did,expected_revision=2,payload=changed))
        terminal=insert(c,str(uuid4()),NOW)
        record(c,terminal,keys['discard'],'DISCARD',dict(draft_id=terminal,expected_revision=1))
        c.execute('DELETE FROM draft_contents WHERE draft_id=?',(terminal,))
        expired=insert(c,str(uuid4()),NOW-8*86400)  # Content exists, logically expired.
    return repo,first,second,did,keys,changed,terminal,expired


@pytest.mark.parametrize('kind',list(DraftType))
@pytest.mark.parametrize('version',[2,3])
def test_populated_legacy_upgrade_preserves_every_value_and_original_retry(http,drafts,tmp_path,monkeypatch,kind,version):
    repo,first,second,did,keys,payload,terminal,expired=populated_legacy(http,drafts,tmp_path,monkeypatch,kind,version)
    before=snapshot(repo.database_path)
    with repo._connect() as c:
        old=[dict(row) for row in c.execute('SELECT * FROM draft_request_receipts ORDER BY rowid')]
    repo.initialize()
    after=snapshot(repo.database_path)
    for table in before:
        if table not in ('draft_request_receipts','draft_schema_migrations'):
            assert after[table]==before[table],table
    with repo._connect() as c:
        copied=[dict(row) for row in c.execute('SELECT * FROM draft_request_receipts ORDER BY rowid')]
        for a,b in zip(old,copied,strict=True):
            assert b.pop('content_changed') is None  # Changed and no-op old SAVEs remain unknown.
            b['owner_employee_id']=b.pop('requester_employee_id')
            if version==2:assert b.pop('fingerprint_key_id') is None
            assert b==a
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    repo.initialize();assert snapshot(repo.database_path)==after
    service=DraftService(DraftRepository(repo,keyring=KEYRING))
    value=[NOW+20]
    with business_clock(repo,monkeypatch,value):
        assert service.read(second,did).payload==payload
        historical=service.save(first,did,expected_revision=1,request_id=keys['save'],payload=payload)
        assert historical.revision==2
        new_payload={'registration':'COLLEAGUE'} if kind==DraftType.WORK_INQUIRY else {'hours':'2.5'}
        saved=service.save(second,did,expected_revision=2,request_id=str(uuid4()),payload=new_payload)
        assert saved.revision==3 and authors(repo,did)==(first.employee_id,second.employee_id)
        state=snapshot(repo.database_path)
        assert service.save(first,did,expected_revision=1,request_id=keys['save'],payload=payload)==historical
        assert snapshot(repo.database_path)==state
        assert service.read(first,did).payload==new_payload
        assert authors(repo,did)==(first.employee_id,second.employee_id)
        assert service.discard(first,terminal,expected_revision=1,request_id=keys['discard']).state=='DISCARDED'


def test_v4_failure_restores_populated_schema_data_and_ledger(http,drafts,tmp_path,monkeypatch):
    repo,*_=populated_legacy(http,drafts,tmp_path,monkeypatch,DraftType.WORK_INQUIRY,3)
    before=snapshot(repo.database_path)
    with repo._connect() as c:schema=[tuple(r) for r in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
    monkeypatch.setattr(shared,'MIGRATION_4',shared.MIGRATION_4+('INVALID SQL',))
    with pytest.raises(sqlite3.DatabaseError):repo.initialize()
    assert snapshot(repo.database_path)==before
    with repo._connect() as c:
        assert [tuple(r) for r in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]==schema
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_v4_new_receipts_require_real_change_flag_and_scoped_requester(http,drafts):
    first=ctx(http)
    row=drafts.create_work_inquiry(first,request_id=str(uuid4()),payload={})
    with http.app.state.repository._connect() as c:
        original=dict(c.execute('SELECT * FROM draft_request_receipts WHERE draft_id=?',(row.draft_id,)).fetchone())
        original.pop('accepted_expires_at')
        for bad in [{'content_changed':None},{'requester_employee_id':str(uuid4())},
                    {'workshop_id':http.workshops[1]},{'content_changed':1}]:
            values={**original,'request_id':str(uuid4()),'operation':'SAVE',**bad}
            with pytest.raises(sqlite3.IntegrityError):
                with c:
                    c.execute('INSERT INTO draft_request_receipts ('+','.join(values)+') VALUES ('+
                              ','.join('?' for _ in values)+')',tuple(values.values()))
        assert c.execute('SELECT count(*) FROM draft_request_receipts').fetchone()[0]==1


def test_v4_receipt_draft_workshop_fk_rejects_active_foreign_requester(http,drafts,monkeypatch):
    from http_auth_support import grant_session
    from webapp.auth_models import UserRole
    from webapp.workshop_identity import resolve_workshop_context

    repository=http.app.state.repository
    service=http.app.state.auth_service
    other=grant_session(service,role=UserRole.SERVICE_MANAGER,membership=False)
    service.workshop_directory.grant_membership(other.user.user_id,http.workshops[1],
        operator_reference='Fictional active staff in workshop B for receipt FK test')
    second=resolve_workshop_context(service,other.token.get_secret_value(),
        required_permission=WorkshopPermission.WRITE_WORKSHOP_DRAFT)
    first=ctx(http)
    assert first.workshop_id!=second.workshop_id
    # Identical server times/revisions make both drafts satisfy the receipt guard.
    with business_clock(repository,monkeypatch,[NOW]):
        a=drafts.create_work_inquiry(first,request_id=str(uuid4()),payload={})
        b=drafts.create_work_inquiry(second,request_id=str(uuid4()),payload={})
        with repository._connect() as c:
            assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
            assert c.execute('''SELECT enabled FROM workshop_memberships
                WHERE auth_store_id=? AND user_id=? AND workshop_id=?''',
                (second.auth_store_id,second.employee_id,second.workshop_id)).fetchone()[0]==1
            values=dict(c.execute('SELECT * FROM draft_request_receipts WHERE draft_id=?',
                                  (b.draft_id,)).fetchone())
            values.pop('accepted_expires_at')
            values.update(request_id=str(uuid4()),operation='SAVE',content_changed=0)
            def fingerprint(draft_id):
                return KEYRING.fingerprint(original_message(second,'SAVE',dict(
                    draft_id=draft_id,expected_revision=1,payload={})),key_id=values['fingerprint_key_id'])
            values['request_fingerprint']=fingerprint(b.draft_id)
            statement='INSERT INTO draft_request_receipts ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')'
            # B's active requester and workshop are valid. Only the association
            # with A's draft crosses the registry composite foreign key.
            wrong={**values,'draft_id':a.draft_id,'request_fingerprint':fingerprint(a.draft_id)}
            before=snapshot(repository.database_path)
            with pytest.raises(sqlite3.IntegrityError) as rejected:
                with c:c.execute(statement,tuple(wrong.values()))
            assert rejected.value.sqlite_errorcode==sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
            assert snapshot(repository.database_path)==before
            # The same SAVE/request UUID succeeds against B's own draft. Neither
            # an inactive membership nor a generic receipt trigger explains rejection.
            with c:c.execute(statement,tuple(values.values()))
            accepted=dict(c.execute('SELECT * FROM draft_request_receipts WHERE request_id=?',
                                    (values['request_id'],)).fetchone())
            accepted.pop('accepted_expires_at')
            assert accepted==values
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert drafts.read(first,a.draft_id).revision==drafts.read(second,b.draft_id).revision==1
        assert drafts.read(first,a.draft_id).payload==drafts.read(second,b.draft_id).payload=={}
