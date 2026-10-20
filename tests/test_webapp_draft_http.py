"""Real MFA/session/CSRF boundary over the production owner-only draft service."""

import base64
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from http_auth_support import settings_for, grant_session, business_headers
from test_webapp_draft_service import business_clock
from test_webapp_auth_business_ordering import (
    journal, change_authorization, ObservedConnection, assert_writer_excluded, observe_begin,
)
from test_webapp_workshop_isolation import create
from test_webapp_workshop_migrations import snapshot
from webapp.auth_csrf import SESSION_COOKIE
from webapp.auth_models import UserRole
from webapp.draft_keys import DraftKeyring, DraftKeyConfigurationError
from webapp.draft_settings import DraftApiSettings
from webapp.draft_maintenance import DraftMaintenance
from webapp.main import create_app
from webapp.http_drafts import MAX_BODY_BYTES


KEY = bytes(range(32))  # Fictional fixture only, not a deployment credential.
KEYRING = DraftKeyring(active_key_id='http-test',keys={'http-test':KEY})
pytestmark = pytest.mark.usefixtures('journal')


@pytest.fixture
def http(tmp_path):
    app=create_app(tmp_path/'works.db',auth_settings=settings_for(tmp_path/'auth.db'),
                   draft_settings=DraftApiSettings(enabled=True,keyring=KEYRING))
    with TestClient(app,base_url='https://testserver') as client:
        client.organizations=(str(uuid4()),str(uuid4()))
        client.workshops=tuple(str(uuid4()) for _ in range(3))
        with app.state.repository._connect() as c,c:
            c.executemany('INSERT INTO repair_organizations VALUES (?,?,1)',
                          [(client.organizations[0],'Fictional A'),(client.organizations[1],'Fictional B')])
            c.executemany("INSERT INTO workshops VALUES (?,?,?,'ACTIVE')",[
                (client.workshops[0],client.organizations[0],'First'),
                (client.workshops[1],client.organizations[0],'Second'),
                (client.workshops[2],client.organizations[1],'Other organization')])
        client.grant=grant_session(app.state.auth_service,membership=False)
        app.state.auth_service.workshop_directory.grant_membership(client.grant.user.user_id,client.workshops[0],
            operator_reference='Explicit fictional draft HTTP fixture membership')
        client.cookies.set(SESSION_COOKIE,client.grant.token.get_secret_value())
        yield client


def body(*, key=None, payload=None):
    return {'draft_type':'WORK_INQUIRY','request_id':key or str(uuid4()),
            'payload':{} if payload is None else payload}


def new(http, request=None):
    response=http.post('/api/drafts',json=request or body(),headers=business_headers(http))
    assert response.status_code==201,response.text
    return response.json()


def operations(http, draft_id):
    return [
        ('POST','/api/drafts',body()),
        ('GET','/api/drafts',None),
        ('GET','/api/drafts/'+draft_id,None),
        ('PUT','/api/drafts/'+draft_id,{'request_id':str(uuid4()),'expected_revision':1,'payload':{}}),
        ('POST','/api/drafts/'+draft_id+'/discard',{'request_id':str(uuid4()),'expected_revision':1}),
    ]


def test_inquiry_lifecycle_receipts_and_unchanged_save_do_not_renew_expiry(http,monkeypatch):
    now=[1791712800]
    repository=http.app.state.repository
    with business_clock(repository,monkeypatch,now):
        request=body(payload={'registration':'A','customer_description':None})
        first=new(http,request)
        assert first['accepted'] is True and first['revision']==1 and 'payload' not in first
        assert new(http,request)==first
        path='/api/drafts/'+first['draft_id']
        read=http.get(path)
        assert read.status_code==200 and read.json()['payload']==request['payload']
        assert http.get('/api/drafts').json()==[read.json()]
        now[0]+=3600
        save={'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}}
        saved=http.put(path,json=save,headers=business_headers(http))
        assert saved.status_code==200 and saved.json()['revision']==2
        assert saved.json()['expires_at']==first['expires_at']+3600
        now[0]+=3600
        assert http.put(path,json=save,headers=business_headers(http)).json()==saved.json()
        unchanged={**save,'request_id':str(uuid4()),'expected_revision':2}
        assert http.put(path,json=unchanged,headers=business_headers(http)).json()==saved.json()
        stale={**save,'request_id':str(uuid4()),'payload':{'registration':'C'}}
        assert http.put(path,json=stale,headers=business_headers(http)).json()=={'detail':'Draft revision conflict'}
        changed={**save,'payload':{'registration':'C'}}
        assert http.put(path,json=changed,headers=business_headers(http)).json()=={'detail':'Draft request conflict'}
        discard={'request_id':str(uuid4()),'expected_revision':2}
        discarded=http.post(path+'/discard',json=discard,headers=business_headers(http))
        assert discarded.status_code==200 and discarded.json()['state']=='DISCARDED'
        assert http.post(path+'/discard',json=discard,headers=business_headers(http)).json()==discarded.json()
        assert http.get(path).status_code==404 and http.get('/api/drafts').json()==[]
        historical=new(http,request)
        assert historical['revision']==1 and historical['termination_reason']=='DISCARDED'
        assert 'payload' not in historical
        for response in (read,saved,discarded):
            assert response.headers['cache-control']=='no-store'
            assert response.headers['x-content-type-options']=='nosniff'


def test_labour_draft_preserves_parent_version_without_submission(http):
    work=create(http)
    before=snapshot(http.app.state.repository.database_path)
    request={'draft_type':'LABOUR_ENTRY','request_id':str(uuid4()),'work_id':work['id'],
             'original_work_version':work['version'],'payload':{'hours':'unfinished','work_description':None}}
    created=new(http,request)
    row=http.get('/api/drafts/'+created['draft_id']).json()
    assert row['work_id']==work['id'] and row['original_work_version']==work['version']
    assert row['draft_type']=='LABOUR_ENTRY' and row['payload']==request['payload']
    after=snapshot(http.app.state.repository.database_path)
    for table,rows in before.items():
        if not table.startswith('draft_'):
            assert after[table]==rows
    invalid={**request,'request_id':str(uuid4()),'original_work_version':work['version']+1}
    assert http.post('/api/drafts',json=invalid,headers=business_headers(http)).status_code==409


@pytest.mark.parametrize('index',[1,2])
def test_other_owner_workshop_and_organization_are_indistinguishable_from_absent(http,index):
    own=new(http,body(payload={'customer_description':'Private fictional customer'}))
    other=grant_session(http.app.state.auth_service,membership=False)
    http.app.state.auth_service.workshop_directory.grant_membership(other.user.user_id,http.workshops[index],
        operator_reference='Explicit other test owner')
    http.cookies.set(SESSION_COOKIE,other.token.get_secret_value())
    assert http.get('/api/drafts').json()==[]
    before=snapshot(http.app.state.repository.database_path)
    missing=str(uuid4())
    for method,path,payload in operations(http,own['draft_id'])[2:]:
        denied=http.request(method,path,json=payload,headers=business_headers(http))
        absent=http.request(method,path.replace(own['draft_id'],missing),json=payload,headers=business_headers(http))
        assert denied.status_code==absent.status_code==404
        assert denied.json()==absent.json()=={'detail':'Draft unavailable'}
        assert 'Private fictional customer' not in denied.text
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('state',['session','role','disabled','version','membership'])
def test_every_endpoint_revalidates_current_authorization(http,state):
    row=new(http)
    headers=business_headers(http)
    change_authorization(http,state)
    before=snapshot(http.app.state.repository.database_path)
    for method,path,payload in operations(http,row['draft_id']):
        response=http.request(method,path,json=payload,headers=headers)
        assert response.status_code in (401,403),response.text
        assert response.headers['cache-control']=='no-store'
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('operation',[0,3,4])
@pytest.mark.parametrize('proof',[None,'invalid'])
def test_csrf_denial_precedes_body_parsing_for_every_mutation(http,operation,proof):
    row=new(http)
    method,path,_=operations(http,row['draft_id'])[operation]
    headers={'Origin':'https://testserver','Content-Type':'application/json'}
    if proof is not None:
        headers['X-CSRF-Token']=proof
    before=snapshot(http.app.state.repository.database_path)
    response=http.request(method,path,content=b'not-json',headers=headers)
    assert response.status_code==403
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('operation',[0,3,4])
@pytest.mark.parametrize('name',['Origin','X-CSRF-Token','Content-Type','Content-Length','Cookie'])
def test_ambiguous_security_headers_do_not_reach_domain(http,operation,name):
    row=new(http)
    method,path,_=operations(http,row['draft_id'])[operation]
    headers={**business_headers(http),'Content-Type':'application/json','Content-Length':'2',
             'Cookie':f'{SESSION_COOKIE}={http.cookies.get(SESSION_COOKIE)}'}
    pairs=list(headers.items())+[(name,headers[name])]
    before=snapshot(http.app.state.repository.database_path)
    response=http.request(method,path,content=b'{}',headers=pairs)
    assert response.status_code==(403 if name=='Origin' else 400)
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('change',[
    {'owner_employee_id':'forged'}, {'employee_id':'forged'}, {'organization_id':'forged'},
    {'requester_employee_id':'forged'}, {'content_changed':1},
    {'workshop_id':'forged'}, {'request_id':'not-a-uuid'}, {'request_id':123},
    {'draft_type':'UNKNOWN'}, {'payload':[]}, {'payload':{'registration':123}},
    {'payload':{'customer_description':'x'*4001}}, {'payload':{'password':'DO NOT ECHO'}},
    {'payload':{'hourly_rate':'90'}}, {'payload':{'registration':'\0'}},
    {'work_id':'foreign'}, {'original_work_version':1},
])
def test_strict_create_request_is_redacted_and_leaves_no_partial_rows(http,change):
    before=snapshot(http.app.state.repository.database_path)
    response=http.post('/api/drafts',json={**body(),**change},headers=business_headers(http))
    assert response.status_code==422 and response.json()=={'detail':'Invalid draft request'}
    assert 'DO NOT ECHO' not in response.text
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('revision',[None,0,-1,True,'1',1.5,9223372036854775808])
def test_save_and_discard_require_strict_revision(http,revision):
    row=new(http)
    path='/api/drafts/'+row['draft_id']
    before=snapshot(http.app.state.repository.database_path)
    payload={'request_id':str(uuid4()),'expected_revision':revision}
    for method,target,value in [('PUT',path,{**payload,'payload':{}}),('POST',path+'/discard',payload)]:
        response=http.request(method,target,json=value,headers=business_headers(http))
        assert response.status_code==422 and response.json()=={'detail':'Invalid draft request'}
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('content',[
    b'{"request_id":"a","request_id":"b"}',b'{"payload":{"registration":"a","registration":"b"}}',
    b'{"payload":NaN}',b'[]',b'null',b'invalid',b'\xff',b'['*2000,
])
def test_invalid_json_never_echoes_input(http,content):
    before=snapshot(http.app.state.repository.database_path)
    response=http.post('/api/drafts',content=content,headers={**business_headers(http),'Content-Type':'application/json'})
    assert response.status_code==422 and response.json()=={'detail':'Invalid draft request'}
    assert snapshot(http.app.state.repository.database_path)==before


def test_body_limit_applies_to_declared_and_streamed_length(http):
    before=snapshot(http.app.state.repository.database_path)
    headers={**business_headers(http),'Content-Type':'application/json'}
    response=http.post('/api/drafts',content=b'x'*(MAX_BODY_BYTES+1),headers=headers)
    assert response.status_code==413
    # Omit both Content-Length and Transfer-Encoding to exercise actual ASGI
    # streaming size enforcement, not only the declared length check.
    request=http.build_request('POST','/api/drafts',content=iter([b'x'*32768,b'x'*32768,b'x']),headers=headers)
    request.headers.pop('transfer-encoding',None)
    request.headers.pop('content-length',None)
    assert http.send(request).status_code==413
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('query',['?owner_employee_id=forged','?workshop_id=forged','?limit=0','?limit=101','?limit=1&limit=2'])
def test_unknown_scope_query_and_unbounded_limits_are_rejected(http,query):
    assert http.get('/api/drafts'+query).status_code==422


def test_historical_replay_after_exact_expiry_and_cleanup_has_no_payload_or_recreation(http,monkeypatch):
    repository=http.app.state.repository
    now=[1791712800]
    with business_clock(repository,monkeypatch,now):
        request=body(payload={'customer_description':'Fictional private text'})
        created=new(http,request)
        path='/api/drafts/'+created['draft_id']
        save={'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}}
        saved=http.put(path,json=save,headers=business_headers(http)).json()
        now[0]=saved['expires_at']
        assert http.get(path).status_code==404 and http.get('/api/drafts').json()==[]
        assert DraftMaintenance(repository).cleanup_expired()==1
        before=snapshot(repository.database_path)
        replay=new(http,request)
        saved_replay=http.put(path,json=save,headers=business_headers(http))
        assert saved_replay.status_code==200
        assert replay['termination_reason']==saved_replay.json()['termination_reason']=='EXPIRED'
        assert 'payload' not in replay and 'payload' not in saved_replay.json()
        assert 'Fictional private text' not in json.dumps(replay)
        changed={**request,'payload':{'customer_description':'Changed'}}
        assert http.post('/api/drafts',json=changed,headers=business_headers(http)).status_code==409
        fresh={**save,'request_id':str(uuid4())}
        assert http.put(path,json=fresh,headers=business_headers(http)).status_code==404
        assert snapshot(repository.database_path)==before


def test_same_revision_competing_http_saves_have_exactly_one_winner(http):
    row=new(http)
    headers=business_headers(http)
    ready=Barrier(2)
    def save(value):
        ready.wait(timeout=3)
        return http.put('/api/drafts/'+row['draft_id'],headers=headers,json={
            'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':value}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(save,['A','B']))
    assert sorted(r.status_code for r in results)==[200,409]
    assert http.get('/api/drafts/'+row['draft_id']).json()['revision']==2


@pytest.mark.parametrize('state',['session','role','disabled','version','membership'])
def test_auth_change_after_http_context_resolution_denies_write(http,monkeypatch,state):
    row=new(http)
    headers=business_headers(http)
    original=http.app.state.draft_service.save
    before=snapshot(http.app.state.repository.database_path)
    def revoked(context,*args,**kwargs):
        change_authorization(http,state)
        return original(context,*args,**kwargs)
    monkeypatch.setattr(http.app.state.draft_service,'save',revoked)
    response=http.put('/api/drafts/'+row['draft_id'],headers=headers,json={
        'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}})
    assert response.status_code in (401,403)
    after=snapshot(http.app.state.repository.database_path)
    for table,rows in before.items():
        if table not in ('workshop_memberships','workshop_audit_events'):
            assert after[table]==rows  # The deliberate revocation itself remains committed.


def test_storage_errors_are_generic_and_atomic(http,monkeypatch):
    repository=http.app.state.repository
    row=new(http)
    before=snapshot(repository.database_path)
    original=repository._connect
    @contextmanager
    def failing():
        with original() as c:
            yield ObservedConnection(c,commit=lambda connection:(_ for _ in ()).throw(
                sqlite3.OperationalError('INTERNAL database/path secret')))
    monkeypatch.setattr(repository,'_connect',failing)
    response=http.put('/api/drafts/'+row['draft_id'],headers=business_headers(http),json={
        'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}})
    assert response.status_code==503 and response.json()=={'detail':'Draft service unavailable'}
    assert snapshot(repository.database_path)==before


def test_no_maintenance_conversion_submission_or_config_endpoint_is_registered(http):
    paths={route.path for route in http.app.routes}
    assert not any('maintenance' in p or 'cleanup' in p or 'conversion' in p or 'keyring' in p for p in paths)
    for path in ('/api/drafts/cleanup-expired','/api/drafts/cleanup-metadata','/api/drafts/'+str(uuid4())+'/convert'):
        assert http.post(path,headers=business_headers(http),json={}).status_code==(404 if path.endswith('/convert') else 405)


def test_default_disabled_api_preserves_unrelated_routes_and_does_not_create_drafts(http):
    # The production disabled representation is None, never a fallback key.
    http.app.state.draft_service=None
    before=snapshot(http.app.state.repository.database_path)
    assert http.get('/api/drafts').status_code==503
    assert http.post('/api/drafts',headers=business_headers(http),json=body()).status_code==503
    assert http.get('/api/works').status_code==200 and http.get('/api/auth/session').status_code==200
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('flag',['1','true','yes','2'])
def test_enabled_or_invalid_environment_without_keys_fails_before_database_initialization(tmp_path,monkeypatch,flag):
    monkeypatch.setenv('CARBIT_DRAFT_API_ENABLED',flag)
    for name in ('CARBIT_DRAFT_HMAC_KEYS','CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID'):
        monkeypatch.delenv(name,raising=False)
    path=tmp_path/'uninitialized.db'
    app=create_app(path,auth_settings=settings_for(tmp_path/'uninitialized-auth.db'))
    with pytest.raises(DraftKeyConfigurationError):
        with TestClient(app,base_url='https://testserver'):
            pass
    assert not path.exists() and not (tmp_path/'uninitialized-auth.db').exists()


def test_environment_configuration_has_no_fallback_or_secret_repr(monkeypatch):
    monkeypatch.delenv('CARBIT_DRAFT_API_ENABLED',raising=False)
    monkeypatch.setenv('CARBIT_DRAFT_HMAC_KEYS','IGNORED INVALID UNUSED SECRET')
    assert DraftApiSettings.from_environment()==DraftApiSettings()
    monkeypatch.setenv('CARBIT_DRAFT_API_ENABLED','1')
    monkeypatch.setenv('CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID','http-test')
    encoded=base64.b64encode(KEY).decode()
    monkeypatch.setenv('CARBIT_DRAFT_HMAC_KEYS',json.dumps({'http-test':encoded}))
    configured=DraftApiSettings.from_environment()
    assert configured.enabled and encoded not in repr(configured)


@pytest.mark.parametrize('state',['absent','expired','mfa_incomplete','openclaw'])
def test_non_human_or_incomplete_authentication_cannot_use_any_endpoint(http,state):
    row=new(http)
    headers=business_headers(http)
    if state=='absent':
        http.cookies.clear()
    elif state=='expired':
        clock=http.app.state.auth_service.clock
        http.app.state.auth_service.clock=lambda:clock()+timedelta(hours=9)
    else:
        with http.app.state.auth_service.repository.transaction() as c:
            c.execute('UPDATE auth_users SET mfa_enabled=0,totp_ciphertext=NULL,last_totp_step=NULL')
            if state=='openclaw':
                c.execute("UPDATE auth_users SET role='OPENCLAW',kind='SERVICE',password_hash=NULL")
    before=snapshot(http.app.state.repository.database_path)
    for method,path,value in operations(http,row['draft_id']):
        assert http.request(method,path,json=value,headers=headers).status_code==401
    assert snapshot(http.app.state.repository.database_path)==before


def test_forged_headers_never_choose_owner_organization_or_workshop(http):
    headers={**business_headers(http),'X-User-Id':str(uuid4()),'X-Workshop-Id':http.workshops[2],
             'X-Organization-Id':http.organizations[1],'X-Role':'IT_ADMIN'}
    response=http.post('/api/drafts',json=body(),headers=headers)
    assert response.status_code==201
    with http.app.state.repository._connect() as c:
        row=c.execute('SELECT organization_id,workshop_id,owner_employee_id FROM draft_registry').fetchone()
        assert tuple(row)==(http.organizations[0],http.workshops[0],str(http.grant.user.user_id))


def test_forged_auth_store_binding_is_denied_without_business_changes(http,monkeypatch):
    row=new(http)
    original=http.app.state.draft_service.save
    before=snapshot(http.app.state.repository.database_path)
    def forged(context,*args,**kwargs):
        return original(replace(context,auth_store_id=str(uuid4())),*args,**kwargs)
    monkeypatch.setattr(http.app.state.draft_service,'save',forged)
    response=http.put('/api/drafts/'+row['draft_id'],headers=business_headers(http),json={
        'request_id':str(uuid4()),'expected_revision':1,'payload':{}})
    assert response.status_code==403
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('state',['session','role','disabled','version','membership'])
def test_http_commit_holds_auth_reservation_before_waiting_revocation(http,monkeypatch,state):
    row=new(http)
    repository=http.app.state.repository
    headers=business_headers(http)
    ready,release,changing=Event(),Event(),Event()
    original=repository._connect
    def commit(c):
        ready.set()
        assert release.wait(3)
        c.commit()
    @contextmanager
    def observed():
        with original() as c:
            yield ObservedConnection(c,commit=commit)
    monkeypatch.setattr(repository,'_connect',observed)
    target=repository if state=='membership' else http.app.state.auth_service.repository
    observe_begin(target,monkeypatch,changing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving=pool.submit(http.put,'/api/drafts/'+row['draft_id'],headers=headers,json={
            'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}})
        try:
            assert ready.wait(2)
            assert_writer_excluded(repository)
            assert_writer_excluded(http.app.state.auth_service.repository)
            changing.clear()
            revoking=pool.submit(change_authorization,http,state)
            assert changing.wait(2) and not revoking.done()
        finally:
            release.set()
        assert saving.result(timeout=3).status_code==200
        revoking.result(timeout=3)
    assert http.get('/api/drafts/'+row['draft_id']).status_code in (401,403)


def test_http_key_rotation_and_missing_old_keys_fail_closed(http):
    from webapp.draft_repository import DraftRepository
    from webapp.draft_service import DraftService
    request=body()
    row=new(http,request)
    next_key=bytes(range(32,64))  # Fictional test key only.
    ring=DraftKeyring(active_key_id='next',keys={'http-test':KEY,'next':next_key})
    http.app.state.draft_service=DraftService(DraftRepository(http.app.state.repository,keyring=ring))
    assert new(http,request)==row
    saved=http.put('/api/drafts/'+row['draft_id'],headers=business_headers(http),json={
        'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}})
    assert saved.status_code==200
    http.app.state.draft_service=DraftService(DraftRepository(http.app.state.repository,
        keyring=DraftKeyring(active_key_id='next',keys={'next':next_key})))
    before=snapshot(http.app.state.repository.database_path)
    response=http.post('/api/drafts',headers=business_headers(http),json=request)
    assert response.status_code==503 and response.json()=={'detail':'Draft service unavailable'}
    assert http.get('/api/works').status_code==200
    assert snapshot(http.app.state.repository.database_path)==before


@pytest.mark.parametrize('source',['{"duplicate":"a","duplicate":"b"}','['*2000,'x'*32769],
                         ids=['duplicate-key','recursive-json','oversized-json'])
def test_enabled_configuration_rejects_ambiguous_oversized_or_recursive_keys(source):
    environment={'CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID':'duplicate','CARBIT_DRAFT_HMAC_KEYS':source}
    with pytest.raises(DraftKeyConfigurationError) as error:
        DraftKeyring.from_environment(environment)
    assert str(error.value)=='draft fingerprint key configuration unavailable'


@pytest.mark.parametrize('content_type',['text/plain','application/json; charset=latin-1'])
def test_unsupported_content_type_is_rejected_before_domain_write(http,content_type):
    before=snapshot(http.app.state.repository.database_path)
    response=http.post('/api/drafts',content=json.dumps(body()),
        headers={**business_headers(http),'Content-Type':content_type})
    assert response.status_code==415 and response.json()=={'detail':'JSON draft request required'}
    assert snapshot(http.app.state.repository.database_path)==before


def test_mismatched_content_length_is_rejected_without_partial_changes(http):
    before=snapshot(http.app.state.repository.database_path)
    response=http.post('/api/drafts',content=json.dumps(body()),
        headers={**business_headers(http),'Content-Type':'application/json','Content-Length':'2'})
    assert response.status_code==400 and response.json()=={'detail':'Invalid draft request framing'}
    assert snapshot(http.app.state.repository.database_path)==before


def test_service_manager_requires_explicit_membership_then_can_use_own_drafts(http):
    grant=grant_session(http.app.state.auth_service,role=UserRole.SERVICE_MANAGER,membership=False)
    http.cookies.set(SESSION_COOKIE,grant.token.get_secret_value())
    before=snapshot(http.app.state.repository.database_path)
    assert http.get('/api/drafts').status_code==403
    assert snapshot(http.app.state.repository.database_path)==before
    http.app.state.auth_service.workshop_directory.grant_membership(
        grant.user.user_id,http.workshops[0],operator_reference='Explicit fictional service manager membership')
    created=new(http,body(payload={'registration':'MANAGER'}))
    path='/api/drafts/'+created['draft_id']
    assert http.get(path).json()['payload']=={'registration':'MANAGER'}
    assert [row['draft_id'] for row in http.get('/api/drafts').json()]==[created['draft_id']]
    response=http.put(path,json={'request_id':str(uuid4()),'expected_revision':1,'payload':{}},
                      headers=business_headers(http))
    assert response.status_code==200 and response.json()['revision']==2
    assert http.post(path+'/discard',json={'request_id':str(uuid4()),'expected_revision':2},
                     headers=business_headers(http)).status_code==200


@pytest.mark.parametrize('role',[UserRole.OWNER,UserRole.SERVICE_MANAGER])
def test_default_startup_closes_draft_http_after_real_lifespan(http,tmp_path,monkeypatch,journal,role):
    # Exercise the real environment default, not DraftApiSettings(enabled=False)
    # or an app.state replacement. Unused malformed keys must remain unused.
    monkeypatch.delenv('CARBIT_DRAFT_API_ENABLED',raising=False)
    monkeypatch.setenv('CARBIT_DRAFT_HMAC_KEYS','unused invalid fictional configuration')
    monkeypatch.delenv('CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID',raising=False)
    monkeypatch.delenv('CARBIT_DRAFT_HMAC_LEGACY_KEY_ID',raising=False)
    app=create_app(tmp_path/'default-works.db',auth_settings=settings_for(tmp_path/'default-auth.db'))
    with TestClient(app,base_url='https://testserver') as client:
        for repository in (app.state.repository,app.state.auth_service.repository):
            with repository._connect() as connection:
                assert connection.execute('PRAGMA journal_mode='+journal).fetchone()[0].upper()==journal
        organization,workshop=str(uuid4()),str(uuid4())
        with app.state.repository._connect() as connection,connection:
            connection.execute('INSERT INTO repair_organizations VALUES (?,?,1)',(organization,'Fictional default'))
            connection.execute("INSERT INTO workshops VALUES (?,?,?,'ACTIVE')",(workshop,organization,'Only workshop'))
        grant=grant_session(app.state.auth_service,role=role,membership=False)
        app.state.auth_service.workshop_directory.grant_membership(grant.user.user_id,workshop,
            operator_reference='Explicit fictional default-startup membership')
        client.cookies.set(SESSION_COOKIE,grant.token.get_secret_value())
        assert client.get('/api/auth/session').status_code==200
        assert client.get('/api/works').status_code==200
        assert client.get('/static/app.js').status_code==200

        # The existing enabled fixture proves the same five method/path pairs
        # reach real handlers with valid authentication, membership and CSRF.
        control=new(http)
        for method,path,payload in operations(http,control['draft_id']):
            response=http.request(method,path,json=payload,headers=business_headers(http))
            assert response.status_code==(201 if path=='/api/drafts' and method=='POST' else 200),response.text

        before=snapshot(app.state.repository.database_path)
        assert before['draft_registry']==[]
        for method,path,payload in operations(client,str(uuid4())):
            response=client.request(method,path,json=payload,headers=business_headers(client))
            assert response.status_code==503,response.text
            assert response.json()=={'detail':'Draft service unavailable'}
            assert response.headers['cache-control']=='no-store'
        assert snapshot(app.state.repository.database_path)==before


@pytest.mark.parametrize('winner',['save','discard'])
def test_competing_save_discard_serializes_without_resurrecting_content(http,monkeypatch,winner):
    # CP4B-3A: stale revision -> 409; a new save of discarded content -> 404.
    row=new(http)
    path='/api/drafts/'+row['draft_id']
    repository=http.app.state.repository
    headers=business_headers(http)
    save={'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'SAVED'}}
    discard={'request_id':str(uuid4()),'expected_revision':1}
    requests={'save':('PUT',path,save),'discard':('POST',path+'/discard',discard)}
    before=snapshot(repository.database_path)
    ready,release,attempted=Event(),Event(),Event()
    original=repository._connect
    writers=[]

    def commit(connection):
        if not release.is_set():
            ready.set()
            assert release.wait(3),'winner commit was not released'
        connection.commit()

    class DraftConnection(ObservedConnection):
        def execute(self,sql,*args):
            if sql=='BEGIN IMMEDIATE':
                writers.append(self.connection)
            return self.connection.execute(sql,*args)

    @contextmanager
    def observed():
        with original() as connection:
            yield DraftConnection(connection,commit=commit)

    monkeypatch.setattr(repository,'_connect',observed)
    def send(operation):
        method,target,payload=requests[operation]
        return http.request(method,target,json=payload,headers=headers)

    loser='discard' if winner=='save' else 'save'
    with ThreadPoolExecutor(max_workers=2) as pool:
        winning=pool.submit(send,winner)
        try:
            assert ready.wait(2),'winner did not reach its uncommitted business write'
            assert snapshot(repository.database_path)==before
            # Observe the losing HTTP request attempting the real auth writer
            # reservation while the winner still holds auth -> business locks.
            observe_begin(http.app.state.auth_service.repository,monkeypatch,attempted)
            losing=pool.submit(send,loser)
            assert attempted.wait(2),'loser did not attempt its writer reservation'
            assert not losing.done()
        finally:
            release.set()
        won=winning.result(timeout=3)
        lost=losing.result(timeout=3)
    assert won.status_code==200,won.text
    assert won.json()['revision']==2
    assert won.json()['state']==('ACTIVE' if winner=='save' else 'DISCARDED')
    assert lost.status_code==(409 if winner=='save' else 404),lost.text
    assert lost.json()=={'detail':'Draft revision conflict' if winner=='save' else 'Draft unavailable'}
    assert len(writers)==2 and writers[0] is not writers[1]

    with repository._connect() as connection:
        content=connection.execute('SELECT revision,state,payload_json FROM draft_contents WHERE draft_id=?',
                                   (row['draft_id'],)).fetchone()
        receipts=[tuple(r) for r in connection.execute(
            'SELECT request_id,operation,accepted_revision FROM draft_request_receipts WHERE draft_id=?',
            (row['draft_id'],))]
        events=[r[0] for r in connection.execute(
            'SELECT event_type FROM draft_audit_events WHERE draft_id=? ORDER BY event_id',(row['draft_id'],))]
    assert len(receipts)==2  # CREATE and the winner; no losing request receipt.
    assert (requests[winner][2]['request_id'],winner.upper(),2) in receipts
    assert requests[loser][2]['request_id'] not in [r[0] for r in receipts]
    assert events==['CREATED','CONTENT_SAVED' if winner=='save' else 'CONTENT_PURGED']
    if winner=='save':
        assert content is not None and tuple(content[:2])==(2,'ACTIVE')
        assert json.loads(content['payload_json'])==save['payload']
        assert http.get(path).json()['revision']==2
        # The stale discard lost; an explicit discard of the current revision
        # terminates the saved draft before checking late saves below.
        response=http.post(path+'/discard',json={'request_id':str(uuid4()),'expected_revision':2},headers=headers)
        assert response.status_code==200 and response.json()['revision']==3
    else:
        assert content is None
    after_race=snapshot(repository.database_path)
    for table,rows in before.items():
        if not table.startswith('draft_'):
            assert after_race[table]==rows
    assert after_race['draft_contents']==[]
    assert http.get(path).status_code==404 and http.get('/api/drafts').json()==[]

    # A previously accepted save may replay metadata only. A rejected save's
    # nonce, or a fresh stale save, cannot recreate discarded content.
    replay=http.put(path,json=save,headers=headers)
    if winner=='save':
        assert replay.status_code==200 and replay.json()['revision']==2
        assert replay.json()['termination_reason']=='DISCARDED' and 'payload' not in replay.json()
    else:
        assert replay.status_code==404 and replay.json()=={'detail':'Draft unavailable'}
    stale=http.put(path,json={**save,'request_id':str(uuid4())},headers=headers)
    assert stale.status_code==404 and stale.json()=={'detail':'Draft unavailable'}
    with repository._connect() as connection:
        assert connection.execute('SELECT reason FROM draft_terminations WHERE draft_id=?',
                                  (row['draft_id'],)).fetchone()[0]=='DISCARDED'
    assert snapshot(repository.database_path)==after_race


@pytest.mark.parametrize('kind',['WORK_INQUIRY','LABOUR_ENTRY'])
def test_shared_staff_http_lifecycle_and_nonce_namespaces(http,kind):
    from test_webapp_draft_service import authors
    creator=http.grant
    request_id=str(uuid4())
    payload={'registration':'A'} if kind=='WORK_INQUIRY' else {'hours':'unfinished'}
    creation={'draft_type':kind,'request_id':request_id,'payload':payload}
    if kind=='LABOUR_ENTRY':
        target=create(http)
        creation.update(work_id=target['id'],original_work_version=target['version'])
    first=new(http,creation)
    other=grant_session(http.app.state.auth_service,role=UserRole.SERVICE_MANAGER,membership=False)
    http.app.state.auth_service.workshop_directory.grant_membership(other.user.user_id,http.workshops[0],
        operator_reference='Fictional shared HTTP staff')
    http.cookies.set(SESSION_COOKIE,other.token.get_secret_value())
    assert [r['draft_id'] for r in http.get('/api/drafts').json()]==[first['draft_id']]
    assert http.get('/api/drafts/'+first['draft_id']).json()['payload']==payload
    independent=new(http,creation)
    assert independent['draft_id']!=first['draft_id']
    path='/api/drafts/'+first['draft_id']
    changed={'registration':'B'} if kind=='WORK_INQUIRY' else {'hours':'1.50'}
    save_key=str(uuid4())
    saved={'request_id':save_key,'expected_revision':1,'payload':changed}
    assert http.put(path,json=saved,headers=business_headers(http)).status_code==200
    assert authors(http.app.state.repository,first['draft_id'])==(str(creator.user.user_id),str(other.user.user_id))
    http.cookies.set(SESSION_COOKIE,creator.token.get_secret_value())
    noop={'request_id':save_key,'expected_revision':2,'payload':changed}
    result=http.put(path,json=noop,headers=business_headers(http))
    assert result.status_code==200 and result.json()['revision']==2
    assert authors(http.app.state.repository,first['draft_id'])[1]==str(other.user.user_id)
    before=snapshot(http.app.state.repository.database_path)
    assert http.put(path,json=noop,headers=business_headers(http)).json()==result.json()
    assert snapshot(http.app.state.repository.database_path)==before
    http.cookies.set(SESSION_COOKIE,other.token.get_secret_value())
    discard={'request_id':str(uuid4()),'expected_revision':2}
    response=http.post(path+'/discard',json=discard,headers=business_headers(http))
    assert response.status_code==200 and response.json()['state']=='DISCARDED'
    assert http.post(path+'/discard',json=discard,headers=business_headers(http)).json()==response.json()
    http.cookies.set(SESSION_COOKIE,creator.token.get_secret_value())
    assert http.post(path+'/discard',json=discard,headers=business_headers(http)).status_code==404
    assert authors(http.app.state.repository,first['draft_id'])==(str(creator.user.user_id),str(other.user.user_id))


def _staff(http, label):
    other=grant_session(http.app.state.auth_service,role=UserRole.SERVICE_MANAGER,membership=False)
    http.app.state.auth_service.workshop_directory.grant_membership(other.user.user_id,http.workshops[0],
        operator_reference=label)
    return other


def _rename(http, grant, name):
    with http.app.state.auth_service.repository.transaction() as c:
        c.execute("UPDATE auth_users SET display_name=? WHERE user_id=?",(name,str(grant.user.user_id)))


def _attribution_of(view):
    return (view['created_by_name'],view['created_by_me'],view['last_editor_known'],
            view['last_edited_by_name'],view['last_edited_by_me'])


def test_read_and_list_show_current_names_and_never_actor_ids(http):
    creator,auth=http.grant,http.app.state.auth_service
    first=new(http,body(payload={'registration':'A'}))
    other=_staff(http,'Fictional named staff')
    _rename(http,creator,'Fictional Owner');_rename(http,other,'Fictional Manager')
    path='/api/drafts/'+first['draft_id']
    http.cookies.set(SESSION_COOKIE,other.token.get_secret_value())
    assert _attribution_of(http.get(path).json())==('Fictional Owner',False,True,'Fictional Owner',False)
    saved=http.put(path,json={'request_id':str(uuid4()),'expected_revision':1,'payload':{'registration':'B'}},
                   headers=business_headers(http))
    assert saved.status_code==200 and set(saved.json())>={'accepted','revision'} and 'created_by_name' not in saved.json()
    http.cookies.set(SESSION_COOKIE,creator.token.get_secret_value())
    for view in (http.get(path).json(),http.get('/api/drafts').json()[0]):
        assert _attribution_of(view)==('Fictional Owner',True,True,'Fictional Manager',False)
    text=http.get('/api/drafts').text+http.get(path).text
    for internal in (str(creator.user.user_id),str(other.user.user_id),auth.repository.store_id()):
        assert internal not in text


@pytest.mark.parametrize('case',['closed-departed-creator','name-missing','lookup-outage'])
def test_missing_or_closed_creator_name_never_blocks_the_shared_draft(http,monkeypatch,case):
    creator,auth=http.grant,http.app.state.auth_service
    first=new(http,body(payload={'registration':'Shared'}))
    other=_staff(http,'Fictional remaining staff')
    expected=None
    if case=='closed-departed-creator':
        auth.workshop_directory.revoke_membership(creator.user.user_id,http.workshops[0],
                                                 operator_reference='Fictional staff departure')
        with auth.repository.transaction() as c:
            c.execute('UPDATE auth_users SET enabled=0 WHERE user_id=?',(str(creator.user.user_id),))
        expected=creator.user.display_name
    elif case=='name-missing':
        monkeypatch.setattr(auth,'display_names',lambda store,ids:{})
    else:
        def outage(store,ids):
            raise sqlite3.OperationalError('Synthetic isolated auth name outage')
        monkeypatch.setattr(auth,'display_names',outage)
    http.cookies.set(SESSION_COOKIE,other.token.get_secret_value())
    single=http.get('/api/drafts/'+first['draft_id'])
    listed=http.get('/api/drafts')
    assert single.status_code==200 and listed.status_code==200
    for view in (single.json(),listed.json()[0]):
        assert view['payload']=={'registration':'Shared'}
        assert _attribution_of(view)==(expected,False,True,expected,False)


def test_name_lookup_failure_does_not_turn_a_denied_read_into_success(http,monkeypatch):
    first=new(http,body())
    monkeypatch.setattr(http.app.state.auth_service,'display_names',lambda store,ids:{})
    http.cookies.set(SESSION_COOKIE,'x'*43)
    assert http.get('/api/drafts/'+first['draft_id']).status_code==401


def test_me_requires_the_same_store_and_never_infers_a_missing_actor():
    from types import SimpleNamespace as Namespace
    from webapp.http_drafts import _attribution
    request=Namespace(app=Namespace(state=Namespace(auth_service=Namespace(display_names=lambda store,ids:{}))))
    context=Namespace(auth_store_id='store-a',employee_id='same-id')
    rows=[Namespace(creator_id='same-id',last_editor_id='same-id',actor_store_id='store-b'),
          Namespace(creator_id='same-id',last_editor_id=None,actor_store_id='store-a')]
    other_store,unknown_editor=_attribution(request,context,rows)
    assert not other_store['created_by_me'] and not other_store['last_edited_by_me']
    assert unknown_editor['created_by_me'] and not unknown_editor['last_edited_by_me']
    assert not unknown_editor['last_editor_known'] and unknown_editor['last_edited_by_name'] is None
