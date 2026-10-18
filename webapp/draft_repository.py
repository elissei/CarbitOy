"""Authenticated workshop-shared SQLite draft operations; auth -> business ordering.

The explicitly configured keyring stays out of storage/repr. Retired keys remain
available while active drafts and retained historical receipts require them.
"""

import json
from contextlib import contextmanager
from uuid import UUID

from webapp.business_authorization import WorkshopAuthBinding
from webapp.database import WorkRepository, WorkNotFoundError
from webapp.draft_migrations import MAX_CLEANUP_BATCH
from webapp.draft_keys import DraftKeyring
from webapp.draft_models import (
    DraftType, DraftSnapshot, DraftReceipt, DraftNotFoundError,
    DraftRevisionConflictError, DraftIdempotencyConflictError, editor_json,
)
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, WorkshopPermission


DRAFT_SCOPE = 'organization_id=? AND workshop_id=? AND auth_store_id=?'


def _draft_scope(context):
    return context.organization_id,context.workshop_id,context.auth_store_id


def _request_scope(context):
    # Keep the original HMAC scope order and per-person nonce namespace.
    return *_draft_scope(context),context.employee_id


def _uuid(value, *, unavailable=False):
    try:
        if not isinstance(value,str) or len(value)!=36:
            raise ValueError()
        return str(UUID(value))
    except (ValueError,TypeError,AttributeError):
        if unavailable:
            raise DraftNotFoundError() from None
        raise ValueError('request UUID required') from None


def _revision(value):
    if type(value) is not int or not 1<=value<=9223372036854775807:
        raise ValueError('positive integer revision required')
    return value


class DraftRepository:
    def __init__(self, repository: WorkRepository, *, keyring: DraftKeyring):
        if not isinstance(repository,WorkRepository):
            raise TypeError('a business repository is required')
        if not isinstance(keyring,DraftKeyring):
            raise TypeError('a configured server draft keyring is required')
        self.repository = repository
        self._keyring = keyring

    @contextmanager
    def _operation(self, context, *, write=False):
        # No legacy offline-context fallback for shared drafts.
        if (not isinstance(context,TrustedWorkshopContext) or context.actor is None
                or not isinstance(context.auth_binding,WorkshopAuthBinding)):
            raise WorkshopAccessDenied('draft access denied')
        permission = WorkshopPermission.WRITE_WORKSHOP_DRAFT if write else WorkshopPermission.READ_WORKSHOP_DRAFT
        with self.repository.operation(context,permission,write=write) as connection:
            # Never accept a rotation which abandons still-required receipts,
            # including long-lived active drafts and restored legacy v2 rows.
            for row in connection.execute('''SELECT DISTINCT r.fingerprint_key_id
                FROM draft_request_receipts r JOIN draft_termination_status t USING(draft_id)
                CROSS JOIN draft_retention_policy p WHERE t.terminated_at IS NULL
                    OR t.terminated_at+p.metadata_seconds>unixepoch('now')'''):
                self._keyring.require(row[0])
            yield connection

    @staticmethod
    def _active(connection, context, draft_id):
        row = connection.execute(f'SELECT * FROM active_drafts WHERE draft_id=? AND {DRAFT_SCOPE}',
                                 (draft_id,*_draft_scope(context))).fetchone()
        if row is None:
            raise DraftNotFoundError()
        return row

    @staticmethod
    def _snapshot(row):
        return DraftSnapshot(row['draft_id'],DraftType(row['draft_type']),row['revision'],
            json.loads(row['payload_json']),row['payload_schema_version'],row['work_id'],
            row['original_work_version'],row['created_at'],row['last_content_saved_at'],row['expires_at'])

    def _message(self, context, operation, request):
        return json.dumps(['carbit.private-draft-request.v1',*_request_scope(context),operation,request],
                             sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')

    @staticmethod
    def _previous(connection, context, request_id):
        # organization is derived from the registry, never trusted from a nonce.
        return connection.execute('''SELECT r.*,s.termination_reason,s.terminated_at,
                p.metadata_seconds,unixepoch('now') AS server_now FROM draft_request_receipts r
            JOIN draft_registry d ON d.draft_id=r.draft_id
            JOIN draft_termination_status s ON s.draft_id=d.draft_id
            CROSS JOIN draft_retention_policy p
            WHERE d.organization_id=? AND r.workshop_id=? AND r.auth_store_id=?
                AND r.requester_employee_id=? AND r.request_id=?''',
            (*_request_scope(context),request_id)).fetchone()

    @staticmethod
    def _result(row):
        return DraftReceipt(row['draft_id'],DraftType(row['draft_type']),row['accepted_revision'],
                            row['accepted_state'],row['accepted_last_content_saved_at'],row['accepted_expires_at'],
                            row['termination_reason'],row['terminated_at'])

    def _replay(self, previous, message):
        if (previous['terminated_at'] is not None and
                previous['terminated_at']+previous['metadata_seconds']<=previous['server_now']):
            raise DraftNotFoundError()
        if not self._keyring.verify(message,key_id=previous['fingerprint_key_id'],
                                   fingerprint=previous['request_fingerprint']):
            raise DraftIdempotencyConflictError()
        return self._result(previous)

    def _record(self, connection, context, request_id, operation, message, row, *, content_changed=False):
        key_id = self._keyring.active_key_id
        fingerprint = self._keyring.fingerprint(message,key_id=key_id)
        connection.execute('''INSERT INTO draft_request_receipts
            (request_id,operation,draft_id,auth_store_id,requester_employee_id,workshop_id,draft_type,
                request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state,fingerprint_key_id,content_changed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (request_id,operation,row['draft_id'],context.auth_store_id,context.employee_id,context.workshop_id,
             row['draft_type'],fingerprint,row['revision']+(operation=='DISCARD'),row['last_content_saved_at'],
             'DISCARDED' if operation=='DISCARD' else 'ACTIVE',key_id,int(content_changed)))
        return self._result(self._previous(connection,context,request_id))

    def create(self, context, *, draft_type: DraftType, request_id, payload, work_id=None, original_work_version=None):
        with self._operation(context,write=True) as connection:
            request_id = _uuid(request_id)
            encoded = editor_json(draft_type,payload)
            if draft_type==DraftType.WORK_INQUIRY:
                if work_id is not None or original_work_version is not None:
                    raise ValueError('work inquiry draft cannot reference existing work')
            elif not isinstance(work_id,str) or not work_id or len(work_id)>200:
                raise ValueError('labour draft requires a work reference')
            else:
                _revision(original_work_version)
            message = self._message(context,'CREATE',dict(type=draft_type.value,payload=json.loads(encoded),
                work_id=work_id,original_work_version=original_work_version))
            previous = self._previous(connection,context,request_id)
            if previous is not None:
                return self._replay(previous,message)
            legacy = connection.execute('''SELECT r.draft_id FROM draft_creation_receipts r
                JOIN draft_registry d USING(draft_id) WHERE d.organization_id=? AND r.workshop_id=?
                AND r.auth_store_id=? AND r.owner_employee_id=? AND r.creation_request_id=?''',
                (*_request_scope(context),request_id)).fetchone()
            if legacy:
                self._active(connection,context,legacy['draft_id'])
                # Version 1 has no original request fingerprint: never guess it.
                raise DraftIdempotencyConflictError()
            if draft_type==DraftType.LABOUR_ENTRY:
                try:
                    work = WorkRepository._find_inquiry(connection,work_id,context)
                except WorkNotFoundError:
                    raise DraftNotFoundError() from None
                if work['version']!=original_work_version:
                    raise DraftRevisionConflictError()
                original_work_version = work['version']
            draft_id = connection.execute('''INSERT INTO draft_registry
                (organization_id,workshop_id,auth_store_id,owner_employee_id,draft_type,work_id,original_work_version)
                VALUES (?,?,?,?,?,?,?) RETURNING draft_id''',
                (*_request_scope(context),draft_type.value,work_id,original_work_version)).fetchone()[0]
            connection.execute('INSERT INTO draft_contents(draft_id,payload_json) VALUES (?,?)',(draft_id,encoded))
            connection.execute('''INSERT INTO draft_creation_receipts
                (creation_request_id,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
                VALUES (?,?,?,?,?,?)''',(request_id,draft_id,context.auth_store_id,context.employee_id,
                                       context.workshop_id,draft_type.value))
            return self._record(connection,context,request_id,'CREATE',message,
                                self._active(connection,context,draft_id))

    def read(self, context, draft_id):
        with self._operation(context) as connection:
            return self._snapshot(self._active(connection,context,_uuid(draft_id,unavailable=True)))

    def list(self, context, *, limit=50):
        with self._operation(context) as connection:
            if type(limit) is not int or not 1<=limit<=100:
                raise ValueError('draft list limit must be 1..100')
            rows = connection.execute(f'''SELECT * FROM active_drafts WHERE {DRAFT_SCOPE}
                ORDER BY created_at,draft_id LIMIT ?''',(*_draft_scope(context),limit)).fetchall()
            return tuple(self._snapshot(row) for row in rows)

    def save(self, context, draft_id, *, expected_revision, request_id, payload):
        with self._operation(context,write=True) as connection:
            draft_id = _uuid(draft_id,unavailable=True)
            expected_revision,request_id = _revision(expected_revision),_uuid(request_id)
            previous = self._previous(connection,context,request_id)
            if previous is not None and previous['draft_id']==draft_id:
                encoded = editor_json(DraftType(previous['draft_type']),payload)
                return self._replay(previous,self._message(context,'SAVE',dict(draft_id=draft_id,
                    expected_revision=expected_revision,payload=json.loads(encoded))))
            row = self._active(connection,context,draft_id)
            encoded = editor_json(DraftType(row['draft_type']),payload)
            message = self._message(context,'SAVE',dict(draft_id=draft_id,
                expected_revision=expected_revision,payload=json.loads(encoded)))
            if previous is not None:
                return self._replay(previous,message)
            if row['revision']!=expected_revision:
                raise DraftRevisionConflictError()
            content_changed = json.loads(row['payload_json'])!=json.loads(encoded)
            if content_changed:
                updated = connection.execute('''UPDATE draft_contents
                    SET payload_json=?,revision=revision+1,last_content_saved_at=unixepoch('now')
                    WHERE draft_id=? AND revision=? AND state='ACTIVE' AND expires_at>unixepoch('now')''',
                    (encoded,draft_id,expected_revision)).rowcount
                if updated!=1:
                    # Check access first; expiry never becomes a revision oracle.
                    self._active(connection,context,draft_id)
                    raise DraftRevisionConflictError()
            return self._record(connection,context,request_id,'SAVE',message,
                                self._active(connection,context,draft_id),content_changed=content_changed)

    def discard(self, context, draft_id, *, expected_revision, request_id):
        with self._operation(context,write=True) as connection:
            draft_id = _uuid(draft_id,unavailable=True)
            request_id,expected_revision = _uuid(request_id),_revision(expected_revision)
            message = self._message(context,'DISCARD',dict(draft_id=draft_id,expected_revision=expected_revision))
            previous = self._previous(connection,context,request_id)
            if previous is not None and previous['draft_id']==draft_id and previous['operation']=='DISCARD':
                return self._replay(previous,message)
            row = self._active(connection,context,draft_id)
            if previous is not None:
                return self._replay(previous,message)
            if row['revision']!=expected_revision:
                raise DraftRevisionConflictError()
            result = self._record(connection,context,request_id,'DISCARD',message,row)
            if connection.execute('DELETE FROM draft_contents WHERE draft_id=? AND revision=?',
                                  (draft_id,expected_revision)).rowcount!=1:
                raise DraftRevisionConflictError()
            return result

    def cleanup_expired(self, context, *, batch_size=MAX_CLEANUP_BATCH):
        """Bounded internal owner cleanup, with the same live authorization.

        No maintenance capability or HTTP endpoint is granted to the owner.
        Trusted offline DraftMaintenance separately handles revoked owners.
        """
        with self._operation(context,write=True) as connection:
            if type(batch_size) is not int or not 1<=batch_size<=MAX_CLEANUP_BATCH:
                raise ValueError('cleanup batch size must be 1..100')
            return connection.execute(f'''DELETE FROM draft_contents WHERE draft_id IN (
                SELECT c.draft_id FROM draft_contents c JOIN draft_registry d USING(draft_id)
                WHERE d.organization_id=? AND d.workshop_id=? AND d.auth_store_id=?
                    AND d.owner_employee_id=? AND c.expires_at<=unixepoch('now')
                ORDER BY c.expires_at,c.draft_id LIMIT ?)''',(*_request_scope(context),batch_size)).rowcount
