"""Storage contracts only; all identities/data below are isolated test fixtures."""

import json
import hashlib
import sqlite3
from uuid import UUID, uuid4

import pytest

from workshop_support import seed_test_scope, test_context
from test_webapp_workshop_migrations import legacy_database, snapshot
from webapp.database import WorkRepository
from webapp.draft_migrations import (
    EXPIRED_CONTENT_CLEANUP_SQL, MAX_CLEANUP_BATCH, MAX_PAYLOAD_BYTES,
    RETENTION_SECONDS, apply_draft_migrations,
)
import webapp.draft_migrations as migrations
from webapp.workshop_context import WorkshopAccessDenied, WorkshopPermission


NOW = 1791712800  # Fixed UTC instant; tests replace only the SQLite clock.
CTX = test_context()
PERMISSION = WorkshopPermission.SAVE_LABOUR_PLAN


@pytest.fixture
def repository(tmp_path):
    result = WorkRepository(tmp_path / 'draft-tests.db')
    result.initialize()
    seed_test_scope(result)
    return result


def clock(connection, instant=NOW):
    connection.create_function('unixepoch', -1, lambda *args: instant)


def insert_draft(connection, *, draft_type='WORK_INQUIRY', payload=None, content_metadata=None, **overrides):
    values = dict(organization_id=CTX.organization_id, workshop_id=CTX.workshop_id,
                  auth_store_id=CTX.auth_store_id, owner_employee_id=CTX.employee_id,
                  draft_type=draft_type)
    values.update(overrides)
    row = connection.execute(
        f"INSERT INTO draft_registry ({','.join(values)}) VALUES ({','.join('?' for _ in values)}) "
        'RETURNING draft_id', tuple(values.values()),
    ).fetchone()
    draft_id = row[0]
    content = dict(draft_id=draft_id, payload_json='{}' if payload is None else payload)
    content.update(content_metadata or {})
    connection.execute(f"INSERT INTO draft_contents({','.join(content)}) VALUES ({','.join('?' for _ in content)})",
                       tuple(content.values()))
    return draft_id


def work(connection, *, work_id='draft-test-work', workshop_id=None, organization_id=None, version=7):
    connection.execute("""INSERT INTO work_inquiries
        (id,registration,customer_description,status,version,created_at,updated_at)
        VALUES (?,'TEST','Fictional unfinished work','KESKEN',?,'historic','historic')""",
        (work_id, version))
    connection.execute('INSERT INTO inquiry_workshop_assignments VALUES (?,?,?)',
                       (work_id, workshop_id or CTX.workshop_id, organization_id or CTX.organization_id))
    return work_id


def foreign_scope(connection, *, other_organization=False):
    workshop_id, organization_id = str(uuid4()), CTX.organization_id
    if other_organization:
        organization_id = str(uuid4())
        connection.execute("INSERT INTO repair_organizations VALUES (?,'Other test organization',1)",
                           (organization_id,))
    connection.execute("INSERT INTO workshops VALUES (?,?,'Other test workshop','ACTIVE')",
                       (workshop_id, organization_id))
    connection.execute('INSERT INTO workshop_memberships VALUES (?,?,?,1,1)',
                       (CTX.auth_store_id, CTX.employee_id, workshop_id))
    return workshop_id, organization_id


def receipt(connection, draft_id, *, conversion=False, **overrides):
    values = dict(draft_id=draft_id, auth_store_id=CTX.auth_store_id,
                  owner_employee_id=CTX.employee_id, workshop_id=CTX.workshop_id,
                  draft_type='WORK_INQUIRY')
    table = 'draft_conversion_receipts' if conversion else 'draft_creation_receipts'
    if conversion:
        values.update(conversion_request_id=str(uuid4()), draft_revision=1,
                      work_id='draft-test-work', work_version=7)
    else:
        values['creation_request_id'] = str(uuid4())
    values.update(overrides)
    connection.execute(f"INSERT INTO {table} ({','.join(values)}) "
                       f"VALUES ({','.join('?' for _ in values)})", tuple(values.values()))


def test_fresh_initialization_installs_empty_storage_with_no_seed_or_auth_fk(tmp_path):
    path = tmp_path / 'fresh.db'
    repository = WorkRepository(path)
    repository.initialize()
    with repository._connect() as connection:
        assert connection.execute('SELECT version FROM draft_schema_migrations').fetchall()[0][0] == 1
        for table in (*migrations.IMMUTABLE_TABLES, 'draft_contents', 'active_drafts',
                      'repair_organizations', 'workshop_memberships', 'work_inquiries'):
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        for table in (*migrations.IMMUTABLE_TABLES, 'draft_contents'):
            assert all(not row['table'].startswith('auth_') for row in
                       connection.execute(f'PRAGMA foreign_key_list({table})'))
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('labour', [False, True])
def test_existing_history_and_repeat_migration_are_exactly_preserved(tmp_path, labour):
    path = legacy_database(tmp_path, labour=labour)
    before = snapshot(path)
    repository = WorkRepository(path)
    repository.initialize()
    after = snapshot(path)
    for table, rows in before.items():
        assert after[table] == rows
    repository.initialize()
    assert snapshot(path) == after
    assert after['draft_registry'] == after['draft_contents'] == []
    assert after['workshop_memberships'] == after['inquiry_workshop_assignments'] == []


def test_cp4b1_database_migrates_without_changing_any_existing_rows(tmp_path):
    path = legacy_database(tmp_path, labour=True)
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA recursive_triggers=ON')
        connection.execute('BEGIN IMMEDIATE')
        # Build the exact pre-2A initialization with the existing migrations.
        from webapp.labour_migrations import apply_labour_migrations
        from webapp.workshop_migrations import apply_workshop_migrations
        apply_labour_migrations(connection)
        apply_workshop_migrations(connection)
    seed_test_scope(WorkRepository(path))
    before = snapshot(path)
    repository = WorkRepository(path)
    repository.initialize()
    for table, rows in before.items():
        assert snapshot(path)[table] == rows


def test_migration_failure_rolls_back_schema_and_ledger(tmp_path, monkeypatch):
    path = legacy_database(tmp_path)
    before = snapshot(path)
    monkeypatch.setattr(migrations, 'SCHEMA', migrations.SCHEMA + ('INVALID SQL',))
    with pytest.raises(sqlite3.DatabaseError):
        WorkRepository(path).initialize()
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'draft_%'").fetchall()


@pytest.mark.parametrize('setting', ['transaction', 'foreign_keys', 'recursive_triggers'])
def test_migration_requires_transaction_and_security_pragmas(tmp_path, setting):
    with sqlite3.connect(tmp_path / 'invalid.db') as connection:
        for pragma in ('foreign_keys', 'recursive_triggers'):
            connection.execute(f'PRAGMA {pragma}={0 if pragma==setting else 1}')
        if setting != 'transaction':
            connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(RuntimeError):
            apply_draft_migrations(connection)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'draft_%'").fetchall()


@pytest.mark.parametrize('version', [5, 99])
def test_unknown_draft_version_refuses_startup(repository, version):
    with repository._connect() as connection, connection:
        connection.execute('INSERT INTO draft_schema_migrations VALUES (?,?)', (version, 'test'))
    before = snapshot(repository.database_path)
    with pytest.raises(RuntimeError, match='draft schema version'):
        repository.initialize()
    assert snapshot(repository.database_path) == before


def test_server_uuid_defaults_and_initial_metadata(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        ids = [insert_draft(connection) for _ in range(2)]
        assert len(set(ids)) == 2
        assert all(UUID(value).version == 4 for value in ids)
        row = connection.execute('SELECT * FROM active_drafts WHERE draft_id=?', (ids[0],)).fetchone()
        assert row['revision'] == row['payload_schema_version'] == 1
        assert row['created_at'] == row['last_content_saved_at'] == NOW
        assert row['expires_at'] == NOW + RETENTION_SECONDS
        assert row['state'] == 'ACTIVE'
        assert row['owner_employee_id'] == CTX.employee_id


@pytest.mark.parametrize('field,value', [
    ('draft_id', str(uuid4())), ('organization_id', str(uuid4())),
    ('workshop_id', str(uuid4())), ('auth_store_id', str(uuid4())),
    ('owner_employee_id', str(uuid4())), ('draft_type', 'LABOUR_ENTRY'),
    ('payload_schema_version', 2), ('work_id', 'different-work'),
    ('original_work_version', 8), ('created_at', NOW+1),
])
def test_identity_fields_are_immutable_even_for_noop_updates(repository, field, value):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'UPDATE draft_registry SET {field}=? WHERE draft_id=?', (value, draft_id))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'UPDATE draft_registry SET {field}={field} WHERE draft_id=?', (draft_id,))


@pytest.mark.parametrize('overrides', [
    {'draft_id': 'not-a-uuid'}, {'draft_id': '11111111-1111-1111-8111-111111111111'},
    {'draft_id': '11111111-1111-4111-7111-111111111111'},
    {'owner_employee_id': 'invalid'}, {'owner_employee_id': str(uuid4())},
    {'auth_store_id': str(uuid4())}, {'payload_schema_version': 2},
    {'draft_type': 'QUOTE'}, {'created_at': NOW+1},
    {'work_id': 'draft-test-work'}, {'original_work_version': 1},
    {'draft_type': 'LABOUR_ENTRY'},
])
def test_invalid_identity_and_unapproved_membership_are_rejected(repository, overrides):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_draft(connection, **overrides)


@pytest.mark.parametrize('draft_type,payload', [
    ('WORK_INQUIRY', '{}'), ('WORK_INQUIRY', '{"registration":"","customer_description":null}'),
    ('WORK_INQUIRY', '{"customer_description":"unfinished","registration":"AB-"}'),
    ('LABOUR_ENTRY', '{}'), ('LABOUR_ENTRY', '{"hours":"1,","source":"MANUA"}'),
    ('LABOUR_ENTRY', '{"hours":"not yet a number","work_description":"","autodata_reference":null}'),
])
def test_type_specific_payload_allows_incomplete_editor_input(repository, draft_type, payload):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        kwargs = {} if draft_type=='WORK_INQUIRY' else dict(work_id=work(connection), original_work_version=7)
        draft_id = insert_draft(connection, draft_type=draft_type, payload=payload, **kwargs)
        assert connection.execute('SELECT payload_json FROM active_drafts WHERE draft_id=?',
                                  (draft_id,)).fetchone()[0] == payload


@pytest.mark.parametrize('payload', [
    'invalid-json', 'null', '[]', '"text"',
    '{"registration":123}', '{"registration":true}', '{"registration":[]}',
    '{"registration":{}}', '{"registration":"A","registration":"B"}',
    '{"registration":"A\\u0000B"}', json.dumps({'registration': 'a'*21}),
    json.dumps({'customer_description': 'a'*4001}),
    '{"hours":"1"}', '{"owner_employee_id":null}', '{"password":null}',
    '{"session_token":null}', '{"csrf_proof":null}', '{"mfa_secret":null}',
    '{"total_price":null}', '{"hourly_rate":null}', '{"purchase_instructions":null}',
    '{"customer_description":""}' + ' '*MAX_PAYLOAD_BYTES,
    # The byte limit also covers escaped Unicode, not just visible characters.
    json.dumps({'customer_description': '\U0001f600'*3000}),
], ids=lambda payload: 'payload-'+hashlib.sha256(payload.encode()).hexdigest()[:10])
def test_invalid_or_privileged_work_inquiry_payload_is_rejected(repository, payload):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute('UPDATE draft_contents SET payload_json=?,revision=2 WHERE draft_id=?',
                               (payload, draft_id))
        assert connection.execute('SELECT payload_json,revision FROM draft_contents').fetchone()[:] == ('{}',1)
        assert connection.execute('SELECT count(*) FROM draft_audit_events').fetchone()[0] == 1


@pytest.mark.parametrize('field,length', [('hours',65),('source',33),('work_description',2001),('autodata_reference',501)])
def test_labour_field_bounds(repository, field, length):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_draft(connection, draft_type='LABOUR_ENTRY', work_id=work(connection),
                         original_work_version=7, payload=json.dumps({field:'x'*length}))


@pytest.mark.parametrize('other_organization', [False, True])
def test_labour_draft_cannot_reference_foreign_workshop_or_organization(repository, other_organization):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        workshop_id, organization_id = foreign_scope(connection, other_organization=other_organization)
        target = work(connection, workshop_id=workshop_id, organization_id=organization_id)
        with pytest.raises(sqlite3.IntegrityError):
            insert_draft(connection, draft_type='LABOUR_ENTRY', work_id=target, original_work_version=7)
        if other_organization:
            with pytest.raises(sqlite3.IntegrityError):
                insert_draft(connection, organization_id=organization_id)


@pytest.mark.parametrize('version', [None, 0, 6, 8, 7.5])
def test_labour_original_version_must_match_existing_work(repository, version):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        with pytest.raises(sqlite3.IntegrityError):
            insert_draft(connection, draft_type='LABOUR_ENTRY', work_id=work(connection), original_work_version=version)


def test_original_work_version_survives_subsequent_business_changes(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        target = work(connection)
        draft_id = insert_draft(connection, draft_type='LABOUR_ENTRY', work_id=target, original_work_version=7)
        connection.execute('UPDATE work_inquiries SET version=8 WHERE id=?', (target,))
        assert connection.execute('SELECT original_work_version FROM draft_registry WHERE draft_id=?',
                                  (draft_id,)).fetchone()[0] == 7


def test_content_save_renews_exact_seven_utc_days_and_audits_only_metadata(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        clock(connection, NOW+3600)
        connection.execute("UPDATE draft_contents SET payload_json=?,revision=2,last_content_saved_at=unixepoch('now') WHERE draft_id=?",
                           ('{"customer_description":"Fictional customer text"}',draft_id))
        row = connection.execute('SELECT * FROM draft_contents').fetchone()
        assert row['expires_at'] == NOW + 3600 + RETENTION_SECONDS
        assert row['last_content_saved_at'] == NOW + 3600
        assert connection.execute('SELECT created_at FROM draft_registry').fetchone()[0] == NOW
        audit = [dict(row) for row in connection.execute('SELECT * FROM draft_audit_events ORDER BY event_id')]
        assert [row['event_type'] for row in audit] == ['CREATED','CONTENT_SAVED']
        assert set(audit[0]) == {'event_id','draft_id','revision','event_type','occurred_at'}
        assert 'Fictional customer text' not in json.dumps(audit)


@pytest.mark.parametrize('new_payload', [
    '{"registration":"Ä","customer_description":""}',
    '{ "customer_description" : "", "registration" : "Ä" }',
    '{"registration":"\\u00c4","customer_description":""}',
])
def test_read_and_semantically_unchanged_retry_never_extend_retention(repository, new_payload):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection, payload='{"registration":"Ä","customer_description":""}')
        before = tuple(connection.execute('SELECT * FROM draft_contents').fetchone())
        clock(connection, NOW+3600)
        for _ in range(2):
            assert connection.execute('SELECT * FROM active_drafts').fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='unchanged content'):
            connection.execute('UPDATE draft_contents SET payload_json=?,revision=2,last_content_saved_at=? WHERE draft_id=?',
                               (new_payload,NOW+3600,draft_id))
        connection.execute('UPDATE draft_contents SET payload_json=? WHERE draft_id=?', (new_payload,draft_id))
        after = tuple(connection.execute('SELECT * FROM draft_contents').fetchone())
        assert after[2:] == before[2:]
        assert connection.execute('SELECT count(*) FROM draft_audit_events').fetchone()[0] == 1


@pytest.mark.parametrize('assignments', [
    'revision=0', 'revision=3', "state='DISCARDED'", "state='CONVERTED',revision=2",
    f'last_content_saved_at={NOW+1}', f'expires_at={NOW+RETENTION_SECONDS+1}',
    "payload_json='{}',revision=2", "payload_json='{\"registration\":\"A\"}'",
    f"payload_json='{{\"registration\":\"A\"}}',revision=2,last_content_saved_at={NOW-1}",
])
def test_revision_state_and_timestamps_cannot_be_forged(repository, assignments):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(f'UPDATE draft_contents SET {assignments} WHERE draft_id=?', (draft_id,))


@pytest.mark.parametrize('offset,visible', [(RETENTION_SECONDS-1,True),(RETENTION_SECONDS,False),(RETENTION_SECONDS+1,False)])
def test_expiration_is_immediate_without_cleanup(repository, offset, visible):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        clock(connection, NOW+offset)
        assert bool(connection.execute('SELECT 1 FROM active_drafts WHERE draft_id=?',(draft_id,)).fetchone()) is visible
        assert connection.execute('SELECT 1 FROM draft_contents WHERE draft_id=?',(draft_id,)).fetchone()
        if not visible:
            with pytest.raises(sqlite3.IntegrityError, match='expired draft'):
                connection.execute("UPDATE draft_contents SET payload_json='{\"registration\":\"A\"}',revision=2,last_content_saved_at=unixepoch('now') WHERE draft_id=?",(draft_id,))


@pytest.mark.parametrize('change', ['membership','organization','workshop'])
def test_revocation_hides_active_content_and_operation_still_denies(repository, change):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        insert_draft(connection)
        if change == 'membership':
            connection.execute('UPDATE workshop_memberships SET enabled=0,version=version+1 WHERE auth_store_id=? AND user_id=?',
                               (CTX.auth_store_id,CTX.employee_id))
        elif change == 'organization':
            connection.execute('UPDATE repair_organizations SET enabled=0 WHERE organization_id=?',(CTX.organization_id,))
        else:
            connection.execute("UPDATE workshops SET status='INACTIVE' WHERE workshop_id=?",(CTX.workshop_id,))
        assert bool(connection.execute('SELECT * FROM active_drafts').fetchall()) is (change=='membership')
    with pytest.raises(WorkshopAccessDenied):
        with repository.operation(CTX, PERMISSION, write=True):
            pytest.fail('revocation must deny access before business writes')


@pytest.mark.parametrize('batch', [0, -1, MAX_CLEANUP_BATCH+1, '1', 1.5, None])
def test_cleanup_rejects_unbounded_or_invalid_limits(repository, batch):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        insert_draft(connection)
        clock(connection, NOW+RETENTION_SECONDS)
        connection.execute(EXPIRED_CONTENT_CLEANUP_SQL, {'batch_size':batch})
        assert connection.execute('SELECT count(*) FROM draft_contents').fetchone()[0] == 1


def test_bounded_cleanup_preserves_business_and_receipts_and_blocks_recreation(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        work(connection)
        ids = [insert_draft(connection) for _ in range(3)]
        receipt(connection, ids[0])
        receipt(connection, ids[0], conversion=True)
        connection.execute("UPDATE draft_contents SET state='CONVERTED',revision=2 WHERE draft_id=?",(ids[0],))
        # Conversion itself never moves the last-content-save deadline.
        assert connection.execute('SELECT expires_at FROM draft_contents WHERE draft_id=?',(ids[0],)).fetchone()[0] == NOW+RETENTION_SECONDS
        before_business = tuple(connection.execute('SELECT * FROM work_inquiries').fetchone())
        clock(connection, NOW+RETENTION_SECONDS)
        connection.execute(EXPIRED_CONTENT_CLEANUP_SQL, {'batch_size':2})
        assert connection.execute('SELECT count(*) FROM draft_contents').fetchone()[0] == 1
        connection.execute(EXPIRED_CONTENT_CLEANUP_SQL, {'batch_size':2})
        assert not connection.execute('SELECT * FROM draft_contents').fetchall()
        assert len(connection.execute('SELECT * FROM draft_registry').fetchall()) == 3
        assert connection.execute('SELECT count(*) FROM draft_creation_receipts').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM draft_conversion_receipts').fetchone()[0] == 1
        assert tuple(connection.execute('SELECT * FROM work_inquiries').fetchone()) == before_business
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO draft_contents(draft_id) VALUES (?)',(ids[0],))
        assert connection.execute("SELECT count(*) FROM draft_audit_events WHERE event_type='CONTENT_PURGED'").fetchone()[0] == 3
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()


def test_cleanup_never_deletes_unexpired_content(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        first = insert_draft(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('DELETE FROM draft_contents WHERE draft_id=?',(first,))
        clock(connection, NOW+1)
        second = insert_draft(connection)
        clock(connection, NOW+RETENTION_SECONDS)
        connection.execute(EXPIRED_CONTENT_CLEANUP_SQL, {'batch_size':100})
        assert [row[0] for row in connection.execute('SELECT draft_id FROM draft_contents')] == [second]


@pytest.mark.parametrize('table', list(migrations.IMMUTABLE_TABLES))
def test_metadata_receipts_and_audit_are_immutable_and_replace_is_blocked(repository, table):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        work(connection)
        draft_id = insert_draft(connection)
        receipt(connection,draft_id)
        receipt(connection,draft_id,conversion=True)
        for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET draft_id=draft_id',
                          f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT OR REPLACE INTO draft_contents SELECT draft_id,payload_json,revision,state,last_content_saved_at FROM draft_contents')


@pytest.mark.parametrize('conversion', [False, True])
@pytest.mark.parametrize('bad_field', ['workshop_id','owner_employee_id','auth_store_id','draft_type'])
def test_receipt_scope_must_match_immutable_owner_and_workshop(repository, conversion, bad_field):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        work(connection)
        draft_id = insert_draft(connection)
        value = 'LABOUR_ENTRY' if bad_field=='draft_type' else str(uuid4())
        with pytest.raises(sqlite3.IntegrityError):
            receipt(connection,draft_id,conversion=conversion,**{bad_field:value})


@pytest.mark.parametrize('other_organization', [False,True])
def test_conversion_receipt_rejects_foreign_business_result(repository, other_organization):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        workshop_id, organization_id = foreign_scope(connection,other_organization=other_organization)
        target = work(connection,workshop_id=workshop_id,organization_id=organization_id)
        draft_id = insert_draft(connection)
        with pytest.raises(sqlite3.IntegrityError):
            receipt(connection,draft_id,conversion=True,work_id=target)


def test_conversion_is_terminal_and_does_not_delete_or_mutate_work(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        target = work(connection)
        draft_id = insert_draft(connection,draft_type='LABOUR_ENTRY',work_id=target,original_work_version=7)
        with pytest.raises(sqlite3.IntegrityError):
            receipt(connection,draft_id,conversion=True,draft_type='LABOUR_ENTRY')
        connection.execute('UPDATE work_inquiries SET version=8 WHERE id=?',(target,))
        receipt(connection,draft_id,conversion=True,draft_type='LABOUR_ENTRY',work_version=8)
        connection.execute("UPDATE draft_contents SET state='CONVERTED',revision=2 WHERE draft_id=?",(draft_id,))
        assert not connection.execute('SELECT * FROM active_drafts').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE draft_contents SET state='ACTIVE',revision=3 WHERE draft_id=?",(draft_id,))
        assert connection.execute('SELECT version FROM work_inquiries').fetchone()[0] == 8


def test_failed_draft_transaction_leaves_no_partial_content_receipts_or_audit(repository):
    before = snapshot(repository.database_path)
    with pytest.raises(sqlite3.IntegrityError):
        with repository.operation(CTX, PERMISSION, write=True) as connection:
            clock(connection)
            draft_id = insert_draft(connection)
            receipt(connection,draft_id)
            connection.execute("UPDATE draft_contents SET revision=9 WHERE draft_id=?",(draft_id,))
    assert snapshot(repository.database_path) == before


@pytest.mark.parametrize('draft_type', ['WORK_INQUIRY','LABOUR_ENTRY'])
@pytest.mark.parametrize('column,value', [
    ('revision',2), ('state','CONVERTED'), ('last_content_saved_at',NOW+1),
])
def test_initial_content_metadata_is_server_controlled(repository, draft_type, column, value):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        kwargs = {} if draft_type=='WORK_INQUIRY' else dict(work_id=work(connection),original_work_version=7)
        with pytest.raises(sqlite3.IntegrityError):
            insert_draft(connection,draft_type=draft_type,content_metadata={column:value},**kwargs)


@pytest.mark.parametrize('conversion', [False,True])
def test_receipt_idempotency_keys_are_owner_scoped_and_unique(repository, conversion):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        work(connection)
        first,second = insert_draft(connection),insert_draft(connection)
        key_column = 'conversion_request_id' if conversion else 'creation_request_id'
        nonce = str(uuid4())
        receipt(connection,first,conversion=conversion,**{key_column:nonce})
        with pytest.raises(sqlite3.IntegrityError):
            receipt(connection,second,conversion=conversion,**{key_column:nonce})


@pytest.mark.parametrize('conversion', [False,True])
def test_expired_draft_cannot_gain_new_receipts(repository, conversion):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        work(connection)
        draft_id = insert_draft(connection)
        clock(connection,NOW+RETENTION_SECONDS)
        with pytest.raises(sqlite3.IntegrityError):
            receipt(connection,draft_id,conversion=conversion)


def test_private_owner_scope_filters_other_owners_even_in_same_workshop(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        mine = insert_draft(connection)
        other = test_context('another-test-employee')
        insert_draft(connection,owner_employee_id=other.employee_id)
        # This is the required future SQL contract, not a new repository method.
        rows = connection.execute('''SELECT draft_id FROM active_drafts
            WHERE organization_id=? AND workshop_id=? AND auth_store_id=? AND owner_employee_id=?''',
            (CTX.organization_id,CTX.workshop_id,CTX.auth_store_id,CTX.employee_id)).fetchall()
        assert [row[0] for row in rows] == [mine]


def test_valid_revision_compare_and_swap_detects_stale_write(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = insert_draft(connection)
        assert connection.execute('''UPDATE draft_contents SET payload_json='{"registration":"A"}',
            revision=revision+1,last_content_saved_at=unixepoch('now')
            WHERE draft_id=? AND revision=1''',(draft_id,)).rowcount == 1
        assert connection.execute('''UPDATE draft_contents SET payload_json='{"registration":"B"}',
            revision=revision+1,last_content_saved_at=unixepoch('now')
            WHERE draft_id=? AND revision=1''',(draft_id,)).rowcount == 0
        assert connection.execute('SELECT payload_json,revision FROM draft_contents').fetchone()[:] == ('{"registration":"A"}',2)


def test_initial_content_and_receipt_allow_clock_tick_between_statements(repository):
    with repository.operation(CTX, PERMISSION, write=True) as connection:
        clock(connection)
        draft_id = connection.execute('''INSERT INTO draft_registry
            (organization_id,workshop_id,auth_store_id,owner_employee_id,draft_type)
            VALUES (?,?,?,?,'WORK_INQUIRY') RETURNING draft_id''',
            (CTX.organization_id,CTX.workshop_id,CTX.auth_store_id,CTX.employee_id)).fetchone()[0]
        clock(connection,NOW+1)
        connection.execute('INSERT INTO draft_contents(draft_id) VALUES (?)',(draft_id,))
        clock(connection,NOW+2)
        receipt(connection,draft_id)
        assert connection.execute('SELECT created_at FROM draft_registry').fetchone()[0] == NOW
        assert connection.execute('SELECT last_content_saved_at,expires_at FROM draft_contents').fetchone()[:] == (NOW+1,NOW+1+RETENTION_SECONDS)
        assert connection.execute('SELECT created_at FROM draft_creation_receipts').fetchone()[0] == NOW+2
