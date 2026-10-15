"""Real MFA sessions and raw production repositories; no implicit test memberships."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from http_auth_support import business_headers, grant_session, settings_for
from test_webapp_labour_service import approval, save_command
from webapp.auth_models import UserRole
from webapp.auth_service import PermissionDeniedError
from webapp.database import WorkNotFoundError, WorkRepository
from webapp.labour_service import LabourPlanService, WorkshopPermission
from webapp.main import create_app
from webapp.workshop_context import WorkshopAccessDenied
from webapp.workshop_identity import resolve_workshop_context


@pytest.fixture
def http(tmp_path):
    app = create_app(tmp_path / 'works.db', auth_settings=settings_for(tmp_path / 'auth.db'))
    with TestClient(app, base_url='https://testserver') as client:
        repository = app.state.repository
        org, other_org = str(uuid4()), str(uuid4())
        workshops = tuple(str(uuid4()) for _ in range(4))
        with repository._connect() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.executemany('INSERT INTO repair_organizations VALUES (?,?,1)',
                                   [(org, 'Fictional repair A'), (other_org, 'Fictional repair B')])
            connection.executemany('INSERT INTO workshops VALUES (?,?,?,?)', [
                (workshops[0], org, 'First', 'ACTIVE'), (workshops[1], org, 'Second', 'ACTIVE'),
                (workshops[2], other_org, 'Other organization', 'ACTIVE'),
                (workshops[3], org, 'Planned', 'PLANNED'),
            ])
        client.workshops = workshops
        client.organizations = org, other_org
        client.grant = grant_session(app.state.auth_service, membership=False)
        # Use the actual reviewed cookie policy, never a fabricated session.
        from webapp.auth_csrf import SESSION_COOKIE
        client.cookies.set(SESSION_COOKIE, client.grant.token.get_secret_value())
        yield client


def directory(http):
    return http.app.state.auth_service.workshop_directory


def grant(http, workshop=None):
    directory(http).grant_membership(http.grant.user.user_id, workshop or http.workshops[0],
                                     operator_reference='Explicit isolated test approval')


def context(http, permission=WorkshopPermission.SAVE_LABOUR_PLAN):
    return resolve_workshop_context(http.app.state.auth_service, http.grant.token.get_secret_value(),
                                    required_permission=permission)


def payload(key=None):
    return {'registration': 'TEST-123', 'customer_description': 'Fictional boundary test',
            'idempotency_key': key or str(uuid4())}


def create(http, body=None):
    response = http.post('/api/works', headers=business_headers(http), json=body or payload())
    assert response.status_code == 201, response.text
    return response.json()


def business_snapshot(http):
    with http.app.state.repository._connect() as connection:
        return {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name)]
                for name in ('work_inquiries','work_events','inquiry_workshop_assignments',
                             'scoped_inquiry_creation_requests','labour_plan_requests')}


@pytest.mark.parametrize('role', [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.IT_ADMIN])
def test_roles_never_grant_membership_and_denials_do_not_extend_session(http, role):
    service = http.app.state.auth_service
    with service.repository.transaction() as connection:
        connection.execute('UPDATE auth_users SET role=?', (role.value,))
        before_idle = connection.execute('SELECT last_seen_at FROM auth_sessions').fetchall()
    original_clock = service.clock
    service.clock = lambda: original_clock() + timedelta(seconds=60)
    before = business_snapshot(http)
    for route in ('/', '/static/app.js', '/static/labour-review-demo.html', '/api/works', '/api/works/unknown'):
        assert http.get(route).status_code == 403
    assert http.post('/api/works', headers=business_headers(http), json=payload()).status_code == 403
    assert http.put('/api/works/unknown/labour', headers=business_headers(http),
                    json={'version':1,'hours':'1.60','work_description':'Fictional'}).status_code == 403
    assert business_snapshot(http) == before
    with service.repository._connect() as connection:
        assert connection.execute('SELECT last_seen_at FROM auth_sessions').fetchall() == before_idle
    assert http.get('/api/auth/session').status_code == 200


@pytest.mark.parametrize('target_index', [1,2])
def test_inquiry_events_and_labour_are_isolated_by_workshop_and_organization(http, target_index):
    grant(http)
    own = create(http)
    original_user = http.grant
    other = grant_session(http.app.state.auth_service, membership=False)
    directory(http).grant_membership(other.user.user_id, http.workshops[target_index], operator_reference='Other test scope')
    from webapp.auth_csrf import SESSION_COOKIE
    http.cookies.set(SESSION_COOKIE, other.token.get_secret_value())
    http.grant = other
    foreign = create(http)
    assert [row['id'] for row in http.get('/api/works').json()] == [foreign['id']]
    unknown = http.get('/api/works/' + str(uuid4()))
    denied = http.get('/api/works/' + own['id'])
    assert denied.status_code == unknown.status_code == 404 and denied.json() == unknown.json()
    before = business_snapshot(http)
    assert http.put('/api/works/' + own['id'] + '/labour', headers=business_headers(http),
                    json={'version':999,'hours':'1.60','work_description':'Fictional'}).status_code == 404
    assert business_snapshot(http) == before
    with pytest.raises(WorkNotFoundError):
        http.app.state.repository.get_work(own['id'], context=context(http))
    assert foreign['events'][0]['actor'] == str(other.user.user_id)
    http.cookies.set(SESSION_COOKIE, original_user.token.get_secret_value())
    http.grant = original_user
    assert [row['id'] for row in http.get('/api/works').json()] == [own['id']]


@pytest.mark.parametrize('state', ['revoke','regrant','organization-disabled','workshop-inactive','planned'])
def test_stale_memberships_and_inactive_scopes_cannot_read_write_or_replay(http, state):
    grant(http)
    body = payload()
    work = create(http, body)
    captured = context(http)
    directory(http).select_active_workshop(http.grant.user.user_id, http.workshops[0], operator_reference='Selection')
    if state in ('revoke','regrant'):
        directory(http).revoke_membership(http.grant.user.user_id, http.workshops[0], operator_reference='Revocation')
        if state == 'regrant':
            grant(http)
    else:
        with http.app.state.repository._connect() as connection, connection:
            if state == 'organization-disabled':
                connection.execute('UPDATE repair_organizations SET enabled=0 WHERE organization_id=?', (http.organizations[0],))
            else:
                connection.execute('UPDATE workshops SET status=? WHERE workshop_id=?',
                                   ('PLANNED' if state=='planned' else 'INACTIVE', http.workshops[0]))
    before = business_snapshot(http)
    repository = http.app.state.repository
    with pytest.raises(WorkshopAccessDenied):
        repository.get_work(work['id'], context=captured)
    with pytest.raises(WorkshopAccessDenied):
        repository.create_work(registration=body['registration'], customer_description=body['customer_description'],
                               idempotency_key=body['idempotency_key'], actor=captured.actor, context=captured)
    assert business_snapshot(http) == before
    if state != 'regrant':
        assert http.get('/api/works').status_code == 403
        assert http.post('/api/works', headers=business_headers(http), json=body).status_code == 403
    else:
        assert http.post('/api/works', headers=business_headers(http), json=body).json() == work


def test_ambiguous_memberships_require_explicit_server_selection_and_old_context_is_invalidated(http):
    grant(http)
    body = payload()
    own = create(http, body)
    first_context = context(http)
    grant(http, http.workshops[1])
    assert http.get('/api/works').status_code == 403
    directory(http).select_active_workshop(http.grant.user.user_id, http.workshops[0], operator_reference='First scope')
    assert context(http).workshop_id == http.workshops[0]
    # Possessing another legitimate membership still cannot forge the active context.
    forged = replace(first_context, workshop_id=http.workshops[1])
    with pytest.raises(WorkshopAccessDenied):
        http.app.state.repository.list_works(context=forged)
    directory(http).select_active_workshop(http.grant.user.user_id, http.workshops[1], operator_reference='Second scope')
    with pytest.raises(WorkshopAccessDenied):
        http.app.state.repository.get_work(own['id'], context=first_context)
    assert http.get('/api/works/' + own['id']).status_code == 404
    # Same key in another workshop is an independent creation, not a conflict/leak.
    foreign = create(http, body)
    assert foreign['id'] != own['id']
    assert create(http, body) == foreign


@pytest.mark.parametrize('field', ['workshop_id','organization_id','auth_store_id','membership_version'])
def test_body_scope_forgeries_are_rejected(http, field):
    grant(http)
    body = payload() | {field: http.workshops[2]}
    before = business_snapshot(http)
    assert http.post('/api/works', headers=business_headers(http), json=body).status_code == 422
    assert business_snapshot(http) == before


def test_header_query_and_context_namespace_cannot_choose_workshop(http):
    grant(http)
    own = create(http)
    response = http.get('/api/works?workshop_id=' + http.workshops[2],
                        headers={'X-Workshop-ID':http.workshops[2], 'X-Organization-ID':http.organizations[1]})
    assert [row['id'] for row in response.json()] == [own['id']]
    captured = context(http)
    for forged in (replace(captured, organization_id=http.organizations[1]),
                   replace(captured, auth_store_id=str(uuid4())),
                   replace(captured, workshop_id=http.workshops[2]),
                   replace(captured, membership_version=0), replace(captured, membership_version=True)):
        with pytest.raises(WorkshopAccessDenied):
            http.app.state.repository.get_work(own['id'], context=forged)


@pytest.mark.parametrize('operation', ['save','approve','read','history','pricing'])
def test_internal_labour_and_retry_paths_check_membership_before_access(http, operation):
    grant(http)
    work = create(http)
    captured = context(http)
    service = LabourPlanService(http.app.state.repository)
    command = save_command(work['id'])
    saved = service.save_plan(captured, command)
    review_command = approval(saved)
    approved = service.approve_plan(captured, review_command)
    directory(http).revoke_membership(http.grant.user.user_id, http.workshops[0], operator_reference='Revoke test')
    before = business_snapshot(http)
    with pytest.raises(WorkshopAccessDenied):
        if operation == 'save':
            service.save_plan(captured, command)
        elif operation == 'approve':
            service.approve_plan(captured, review_command)
        elif operation == 'read':
            service.get_active_plan(captured, work['id'])
        elif operation == 'history':
            service.decision_history(captured, work['id'])
        else:
            service.require_approved_final_hours(captured, inquiry_id=work['id'], expected_version=approved.work_version,
                expected_content_id=approved.content_id, expected_rules_version=approved.rules_version,
                expected_revision_id=approved.revision_id)
    assert business_snapshot(http) == before


@pytest.mark.parametrize('operation', ['save','approve','read','history','pricing'])
@pytest.mark.parametrize('target_index', [1,2])
def test_foreign_labour_retry_history_and_pricing_never_disclose_existing_work(http, operation, target_index):
    grant(http)
    work = create(http)
    captured = context(http)
    service = LabourPlanService(http.app.state.repository)
    command = save_command(work['id'])
    saved = service.save_plan(captured, command)
    review_command = approval(saved)
    approved = service.approve_plan(captured, review_command)
    grant(http, http.workshops[target_index])
    directory(http).select_active_workshop(http.grant.user.user_id, http.workshops[target_index], operator_reference='Other scope')
    current = context(http)
    before = business_snapshot(http)
    with pytest.raises(WorkNotFoundError):
        if operation == 'save': service.save_plan(current, command)
        elif operation == 'approve': service.approve_plan(current, review_command)
        elif operation == 'read': service.get_active_plan(current, work['id'])
        elif operation == 'history': service.decision_history(current, work['id'])
        else:
            service.require_approved_final_hours(current, inquiry_id=work['id'], expected_version=approved.work_version,
                expected_content_id=approved.content_id, expected_rules_version=approved.rules_version,
                expected_revision_id=approved.revision_id)
    assert business_snapshot(http) == before


def test_membership_revoked_after_resolver_is_checked_again_inside_write_transaction(http, monkeypatch):
    grant(http)
    repository = http.app.state.repository
    original = repository.create_work
    def revoke_before_transaction(**kwargs):
        directory(http).revoke_membership(http.grant.user.user_id, http.workshops[0], operator_reference='Concurrent revocation')
        return original(**kwargs)
    monkeypatch.setattr(repository, 'create_work', revoke_before_transaction)
    before = business_snapshot(http)
    assert http.post('/api/works', headers=business_headers(http), json=payload()).status_code == 403
    assert business_snapshot(http) == before


def test_planned_workshop_and_service_or_unknown_accounts_are_not_usable_members(http):
    grant(http, http.workshops[3])
    assert http.get('/api/works').status_code == 403
    with pytest.raises(WorkshopAccessDenied):
        directory(http).select_active_workshop(http.grant.user.user_id, http.workshops[3], operator_reference='Reject planned')
    service = http.app.state.auth_service
    agent = service.provision_identity(username='test.openclaw', display_name='Fictional service', role=UserRole.OPENCLAW)
    for user_id in (agent.user_id, uuid4()):
        with pytest.raises(WorkshopAccessDenied):
            directory(http).grant_membership(user_id, http.workshops[0], operator_reference='Reject nonhuman/unknown')


def test_member_role_ceiling_is_preserved_for_it_admin(http):
    grant(http)
    own = create(http)
    with http.app.state.auth_service.repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET role='IT_ADMIN'")
    assert http.get('/api/works/' + own['id']).status_code == 200
    assert http.post('/api/works', headers=business_headers(http), json=payload()).status_code == 403
    with pytest.raises(PermissionDeniedError):
        context(http, WorkshopPermission.APPROVE_LABOUR_PLAN)
