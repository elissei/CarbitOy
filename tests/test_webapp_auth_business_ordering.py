"""Deterministic auth/business ordering with independent SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event
import sqlite3
import subprocess
import sys

import pytest

from test_webapp_workshop_isolation import (
    http, grant, context, payload, directory, create,
)
from http_auth_support import business_headers
from http_auth_support import settings_for
from fastapi.testclient import TestClient
from test_webapp_labour_service import save_command, approval
from webapp.auth_service import AuthenticationError
from webapp.labour_service import LabourPlanService
from webapp.main import create_app
from webapp.workshop_context import WorkshopAccessDenied, WorkshopPermission
import webapp.business_authorization as authorization


pytestmark = pytest.mark.usefixtures('journal')


@pytest.fixture(params=['DELETE', 'WAL'])
def journal(http, request):
    for repository in (http.app.state.repository, http.app.state.auth_service.repository):
        with repository._connect() as connection:
            assert connection.execute('PRAGMA journal_mode=' + request.param).fetchone()[0].upper() == request.param
    return request.param


def business_snapshot(http):
    with http.app.state.repository._connect() as connection:
        return {name: [tuple(row) for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                for name in ('work_inquiries', 'work_events', 'inquiry_workshop_assignments',
                    'scoped_inquiry_creation_requests', 'inquiry_work_items', 'labour_plan_revisions',
                    'labour_plan_revision_items', 'inquiry_labour_plan_state', 'labour_review_decisions',
                    'labour_plan_requests', 'sqlite_sequence')}


def prepare_write(http, operation):
    repository = http.app.state.repository
    resolved = context(http)
    if operation in ('create', 'create-retry'):
        body = payload()
        def write():
            return repository.create_work(registration=body['registration'],
                customer_description=body['customer_description'], idempotency_key=body['idempotency_key'],
                actor=resolved.actor, context=resolved)
        if operation == 'create-retry': write()
        return write
    work = create(http)
    if operation == 'labour':
        labour = {'hours':'1.60','source':'MANUAL_AUTODATA','work_description':'Fictional',
            'recorded_by':resolved.employee_id,'autodata_reference':'','hourly_rate':'90.00',
            'total_price':'144.00','net_price':'114.74','vat_price':'29.26','vat_rate':'0.255','currency':'EUR'}
        return lambda: repository.record_labour(work_id=work['id'], expected_version=work['version'],
                                                labour=labour, actor=resolved.actor, context=resolved)
    service = LabourPlanService(repository)
    save = save_command(work['id'])
    if operation in ('save', 'save-retry'):
        if operation == 'save-retry': service.save_plan(resolved, save)
        return lambda: service.save_plan(resolved, save)
    review = approval(service.save_plan(resolved, save))
    if operation == 'approve-retry': service.approve_plan(resolved, review)
    return lambda: service.approve_plan(resolved, review)


def change_authorization(http, state):
    service = http.app.state.auth_service
    if state == 'membership':
        directory(http).revoke_membership(http.grant.user.user_id, http.workshops[0],
                                          operator_reference='Explicit test revocation')
        return
    with service.repository.transaction() as connection:
        if state == 'session':
            connection.execute('UPDATE auth_sessions SET revoked_at=?', (service._now(),))
        else:
            expression = {'role': "role='IT_ADMIN'", 'disabled': 'enabled=0',
                          'version': 'auth_version=auth_version+1'}[state]
            connection.execute('UPDATE auth_users SET ' + expression)


@pytest.mark.parametrize('state', ['session', 'role', 'disabled', 'version', 'membership'])
@pytest.mark.parametrize('operation', ['create', 'labour', 'save', 'approve', 'create-retry', 'save-retry', 'approve-retry'])
def test_revocation_committed_after_context_resolution_denies_write(http, state, operation):
    grant(http)
    operation_call = prepare_write(http, operation)
    ready, release = Event(), Event()
    before = business_snapshot(http)

    def write():
        ready.set()
        assert release.wait(3)
        return operation_call()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write)
        try:
            assert ready.wait(2)
            change_authorization(http, state)
        finally:
            release.set()
        with pytest.raises((AuthenticationError, WorkshopAccessDenied)):
            future.result(timeout=3)
    assert business_snapshot(http) == before


class ObservedConnection:
    def __init__(self, connection, *, commit=None):
        self.connection, self.commit_hook = connection, commit

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def commit(self):
        if self.commit_hook is not None:
            self.commit_hook(self.connection)
        else:
            self.connection.commit()


def observe_begin(repository, monkeypatch, event):
    original = repository._connect
    @contextmanager
    def observed():
        with original() as connection:
            connection.set_trace_callback(lambda sql: event.set() if sql == 'BEGIN IMMEDIATE' else None)
            yield connection
    monkeypatch.setattr(repository, '_connect', observed)


def assert_writer_excluded(repository):
    with repository._connect() as connection:
        connection.execute('PRAGMA busy_timeout=0')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            connection.execute('BEGIN IMMEDIATE')


@pytest.mark.parametrize('state', ['session', 'role', 'disabled', 'version', 'membership'])
def test_business_commit_holds_both_ordering_locks_until_commit(http, monkeypatch, state):
    grant(http)
    write = prepare_write(http, 'create')
    repository = http.app.state.repository
    before = business_snapshot(http)
    ready, release, committed, changing = Event(), Event(), Event(), Event()
    original = repository._connect

    def commit(connection):
        ready.set()
        assert release.wait(3)
        connection.commit()
        committed.set()

    @contextmanager
    def observed():
        with original() as connection:
            yield ObservedConnection(connection, commit=commit)

    monkeypatch.setattr(repository, '_connect', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        business = pool.submit(write)
        try:
            assert ready.wait(2)
            assert business_snapshot(http) == before  # Uncommitted partial inserts are invisible.
            assert_writer_excluded(http.app.state.auth_service.repository)
            assert_writer_excluded(repository)
            contender_repository = repository if state == 'membership' else http.app.state.auth_service.repository
            observe_begin(contender_repository, monkeypatch, changing)
            revocation = pool.submit(change_authorization, http, state)
            assert changing.wait(2)
            assert not revocation.done()
        finally:
            release.set()
        result = business.result(timeout=3)
        revocation.result(timeout=3)
    assert committed.is_set()
    assert result['id'] in {r[0] for r in business_snapshot(http)['work_inquiries']}
    assert len(business_snapshot(http)['scoped_inquiry_creation_requests']) == 1
    # A completed earlier write remains committed; the stale context is now denied.
    after = business_snapshot(http)
    with pytest.raises(AuthenticationError): write()
    assert business_snapshot(http) == after


@pytest.mark.parametrize('state', ['session', 'role', 'disabled', 'version', 'membership'])
def test_revocation_transaction_wins_before_queued_business_operation(http, monkeypatch, state):
    grant(http)
    write = prepare_write(http, 'create')
    service = http.app.state.auth_service
    repository = http.app.state.repository
    contender = repository if state == 'membership' else service.repository
    before = business_snapshot(http)
    attempting = Event()
    original = contender._connect
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if state == 'membership':
                connection.execute('UPDATE workshop_memberships SET enabled=0,version=version+1')
            elif state == 'session':
                connection.execute('UPDATE auth_sessions SET revoked_at=?', (service._now(),))
            else:
                expression = {'role':"role='IT_ADMIN'",'disabled':'enabled=0','version':'auth_version=auth_version+1'}[state]
                connection.execute('UPDATE auth_users SET ' + expression)
            observe_begin(contender, monkeypatch, attempting)
            business = pool.submit(write)
            assert attempting.wait(2)
            assert not business.done()
            assert business_snapshot(http) == before
        with pytest.raises(AuthenticationError): business.result(timeout=3)
    assert business_snapshot(http) == before


def test_two_concurrent_business_commits_serialize_and_keep_idempotency(http, monkeypatch):
    grant(http)
    first = prepare_write(http, 'create')
    ready, release, attempting = Event(), Event(), Event()
    repository = http.app.state.repository
    original = repository._connect
    calls = 0
    def commit(connection):
        nonlocal calls
        calls += 1
        if calls == 1:
            ready.set()
            assert release.wait(3)
        connection.commit()
    @contextmanager
    def observed():
        with original() as connection:
            yield ObservedConnection(connection, commit=commit)
    monkeypatch.setattr(repository, '_connect', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(first)
        try:
            assert ready.wait(2)
            observe_begin(http.app.state.auth_service.repository, monkeypatch, attempting)
            right = pool.submit(first)
            assert attempting.wait(2)
            assert not right.done()
        finally:
            release.set()
        assert left.result(timeout=3) == right.result(timeout=3)
    rows = business_snapshot(http)
    assert len(rows['work_inquiries']) == len(rows['work_events']) == len(rows['scoped_inquiry_creation_requests']) == 1


@pytest.mark.parametrize('failure', ['idle', 'absolute', 'deadline', 'sql-interrupted'])
@pytest.mark.parametrize('operation', ['create', 'labour', 'save', 'approve'])
def test_late_failure_rolls_back_every_business_row_and_releases_auth(http, monkeypatch, failure, operation):
    grant(http)
    write = prepare_write(http, operation)
    repository = http.app.state.repository
    service = http.app.state.auth_service
    before = business_snapshot(http)
    clock_value = [0.0]
    if failure in ('deadline', 'sql-interrupted'):
        monkeypatch.setattr(authorization, 'monotonic', lambda: clock_value[0])
    original = repository.operation
    @contextmanager
    def late_failure(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield connection
            if failure in ('idle', 'absolute'):
                now = service.clock()
                service.clock = lambda: now + timedelta(seconds=1800 if failure == 'idle' else 28800)
            elif failure in ('deadline', 'sql-interrupted'):
                clock_value[0] = 6.0
                if failure == 'sql-interrupted':
                    connection.execute('''WITH RECURSIVE n(x) AS (VALUES(1)
                        UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n''').fetchone()
    monkeypatch.setattr(repository, 'operation', late_failure)
    with pytest.raises((AuthenticationError, sqlite3.OperationalError)):
        write()
    assert business_snapshot(http) == before
    # Both mutexes released, also on an exception after all inserts/updates.
    for target in (service.repository, repository):
        with target._connect() as connection:
            connection.execute('PRAGMA busy_timeout=0')
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()


@pytest.mark.parametrize('state', ['session', 'role', 'disabled', 'version', 'membership'])
def test_http_revocation_after_resolution_denies_without_idle_touch_or_partial_write(http, monkeypatch, state):
    grant(http)
    repository = http.app.state.repository
    service = http.app.state.auth_service
    headers = business_headers(http)
    before = business_snapshot(http)
    with service.repository._connect() as connection:
        activity = list(connection.execute('SELECT last_seen_at FROM auth_sessions'))
    original = repository.create_work
    def changed(**kwargs):
        change_authorization(http, state)
        return original(**kwargs)
    monkeypatch.setattr(repository, 'create_work', changed)
    response = http.post('/api/works', headers=headers, json=payload())
    assert response.status_code == (403 if state in ('role','membership') else 401)
    assert response.headers['cache-control'] == 'no-store'
    assert business_snapshot(http) == before
    with service.repository._connect() as connection:
        assert list(connection.execute('SELECT last_seen_at FROM auth_sessions')) == activity


@pytest.mark.parametrize('target', ['auth', 'business'])
def test_lock_contention_is_bounded_and_fails_closed(http, monkeypatch, target):
    grant(http)
    write = prepare_write(http, 'create')
    repository = http.app.state.repository
    auth_repository = http.app.state.auth_service.repository
    contender = auth_repository if target == 'auth' else repository
    before = business_snapshot(http)
    original = contender._connect
    if target == 'business':
        monkeypatch.setattr(authorization, 'TRANSACTION_SECONDS', 0.025)
    else:
        @contextmanager
        def fast_timeout():
            with original() as connection:
                connection.execute('PRAGMA busy_timeout=25')
                yield connection
        monkeypatch.setattr(contender, '_connect', fast_timeout)
    with original() as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            with pytest.raises(sqlite3.OperationalError): future.result(timeout=2)
        assert business_snapshot(http) == before
    with auth_repository._connect() as connection:
        connection.execute('PRAGMA busy_timeout=0')
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()


def test_authenticated_context_cannot_omit_auth_binding(http):
    grant(http)
    resolved = replace(context(http), auth_binding=None)
    before = business_snapshot(http)
    with pytest.raises(WorkshopAccessDenied):
        http.app.state.repository.list_works(context=resolved)
    assert business_snapshot(http) == before


def test_same_database_is_rejected_before_guard_lock(http):
    grant(http)
    write = prepare_write(http, 'create')
    http.app.state.repository.database_path = str(http.app.state.auth_service.repository.database_path)
    with pytest.raises(sqlite3.OperationalError, match='distinct'):
        write()


@pytest.mark.parametrize('operation', ['create', 'labour', 'save', 'approve'])
def test_commit_error_rolls_back_all_business_changes_and_releases_auth(http, monkeypatch, operation):
    grant(http)
    write = prepare_write(http, operation)
    repository = http.app.state.repository
    before = business_snapshot(http)
    original = repository._connect
    def fail_commit(connection):
        assert connection.in_transaction
        raise sqlite3.OperationalError('injected SQLite commit failure')
    @contextmanager
    def observed():
        with original() as connection:
            yield ObservedConnection(connection, commit=fail_commit)
    monkeypatch.setattr(repository, '_connect', observed)
    with pytest.raises(sqlite3.OperationalError, match='commit failure'): write()
    assert business_snapshot(http) == before
    with http.app.state.auth_service.repository._connect() as connection:
        connection.execute('PRAGMA busy_timeout=0')
        connection.execute('BEGIN IMMEDIATE')
        connection.rollback()


def test_auth_guard_is_read_only_and_keeps_reservation_without_durable_auth_change(http, monkeypatch):
    grant(http)
    resolved = context(http)
    auth_repository = http.app.state.auth_service.repository
    original = auth_repository._connect
    connections = []
    @contextmanager
    def captured():
        with original() as connection:
            connections.append(connection)
            yield connection
    monkeypatch.setattr(auth_repository, '_connect', captured)
    with http.app.state.repository.operation(resolved, WorkshopPermission.SAVE_LABOUR_PLAN, write=True):
        guard_connection = connections[-1]
        assert guard_connection.in_transaction
        assert guard_connection.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            guard_connection.execute('UPDATE auth_users SET enabled=0')
        assert guard_connection.total_changes == 0
        assert_writer_excluded(auth_repository)


def test_auth_guard_excludes_an_independent_process_until_business_completion(http):
    grant(http)
    resolved = context(http)
    code = '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0)
try:
    c.execute('BEGIN IMMEDIATE')
    print('acquired')
except sqlite3.OperationalError as e:
    if 'locked' not in str(e): raise
    print('blocked')
finally:
    c.rollback()
    c.close()
'''
    def probe():
        result = subprocess.run([sys.executable, '-c', code,
            str(http.app.state.auth_service.repository.database_path)],
            capture_output=True, text=True, timeout=3, check=True)
        return result.stdout.strip()
    with http.app.state.repository.operation(resolved, WorkshopPermission.SAVE_LABOUR_PLAN, write=True):
        assert probe() == 'blocked'
    assert probe() == 'acquired'


def test_same_database_configuration_stops_startup_before_initialization(http, tmp_path):
    path = tmp_path / 'misconfigured.db'
    app = create_app(path, auth_settings=settings_for(path))
    with pytest.raises(ValueError, match='distinct'):
        with TestClient(app): pass
    assert not path.exists()


@pytest.mark.parametrize('failure', ['idle', 'absolute', 'deadline'])
def test_http_late_failure_rolls_back_and_does_not_refresh_session(http, monkeypatch, failure):
    grant(http)
    repository = http.app.state.repository
    service = http.app.state.auth_service
    before = business_snapshot(http)
    headers = business_headers(http)
    with service.repository._connect() as connection:
        activity = list(connection.execute('SELECT last_seen_at FROM auth_sessions'))
    now = service.clock()
    monotonic_value = [0.0]
    if failure == 'deadline':
        monkeypatch.setattr(authorization, 'monotonic', lambda: monotonic_value[0])
    original = repository._insert_event
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if failure == 'deadline': monotonic_value[0] = 6.0
        else: service.clock = lambda: now + timedelta(seconds=1800 if failure == 'idle' else 28800)
        return result
    monkeypatch.setattr(repository, '_insert_event', changed)
    response = http.post('/api/works', headers=headers, json=payload())
    assert response.status_code == (503 if failure == 'deadline' else 401)
    assert response.headers['cache-control'] == 'no-store'
    assert business_snapshot(http) == before
    with service.repository._connect() as connection:
        assert list(connection.execute('SELECT last_seen_at FROM auth_sessions')) == activity
