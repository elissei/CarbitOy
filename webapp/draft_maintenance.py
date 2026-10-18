"""Trusted offline deletion only. Never expose this executor through HTTP.

No auth session is used and no auth database is accessed. Thus there is no
auth/business inversion: membership writes and content saves serialize with
this bounded business transaction. No capability to read draft payloads exists.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic

from webapp.database import WorkRepository
from webapp.business_authorization import TRANSACTION_SECONDS
from webapp.draft_migrations import MAX_CLEANUP_BATCH
from webapp.draft_hardening_migrations import METADATA_TABLES


@dataclass(frozen=True)
class DraftRetentionPolicy:
    metadata_days: int = 90

    def __post_init__(self):
        if type(self.metadata_days) is not int or not 1<=self.metadata_days<=90:
            raise ValueError('draft metadata retention must be 1..90 days')


def _batch(value, *, minimum=1):
    if type(value) is not int or not minimum<=value<=MAX_CLEANUP_BATCH:
        raise ValueError(f'cleanup batch size must be {minimum}..100')
    return value


def _deletion_only(action, table, column, database, trigger):
    if action==sqlite3.SQLITE_READ and (
            (table=='draft_contents' and column=='payload_json')
            or (table=='work_inquiries' and column not in ('id','version'))
            or table in ('auth_users','auth_sessions','work_events')):
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_DELETE,sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE):
        allowed = (
            action==sqlite3.SQLITE_DELETE and table in (*METADATA_TABLES,'draft_registry',
                                                       'draft_terminations','draft_contents')
            or action==sqlite3.SQLITE_INSERT and table in ('draft_audit_events','draft_terminations')
            or action==sqlite3.SQLITE_UPDATE and table=='draft_retention_policy')
        if not allowed:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class DraftMaintenance:
    def __init__(self, repository: WorkRepository, *, policy=DraftRetentionPolicy()):
        if not isinstance(repository,WorkRepository) or not isinstance(policy,DraftRetentionPolicy):
            raise TypeError('business repository and retention policy required')
        self._repository = repository
        self._policy = policy

    @contextmanager
    def _transaction(self):
        with self._repository._connect() as connection:
            deadline = monotonic()+TRANSACTION_SECONDS
            connection.execute(f'PRAGMA busy_timeout={int(TRANSACTION_SECONDS*1000)}')
            connection.set_progress_handler(lambda:int(monotonic()>=deadline),1000)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.set_authorizer(_deletion_only)
                yield connection
                if monotonic()>=deadline:
                    raise sqlite3.OperationalError('draft maintenance deadline exceeded')
                connection.commit()
            except BaseException:
                connection.set_authorizer(None)
                connection.set_progress_handler(None,0)
                connection.rollback()
                raise
            finally:
                connection.set_authorizer(None)
                connection.set_progress_handler(None,0)

    def cleanup_expired(self, *, batch_size=MAX_CLEANUP_BATCH):
        batch_size = _batch(batch_size)
        with self._transaction() as connection:
            return connection.execute('''DELETE FROM draft_contents WHERE draft_id IN (
                SELECT draft_id FROM draft_contents WHERE expires_at<=unixepoch('now')
                ORDER BY expires_at,draft_id LIMIT ?)''',(batch_size,)).rowcount

    def cleanup_metadata(self, *, batch_size=MAX_CLEANUP_BATCH):
        """At most batch_size physical metadata rows, not unbounded draft groups.

        Run content cleanup first. Registry+termination deletion costs two rows;
        batch_size>=2 permits final tombstone removal. No purge audit is retained
        past its own deadline. Return counts only, never IDs or customer payload.
        """
        remaining = _batch(batch_size,minimum=2)
        with self._transaction() as connection:
            connection.execute('UPDATE draft_retention_policy SET metadata_seconds=? WHERE singleton=1',
                               (self._policy.metadata_days*86400,))
            due = '''SELECT t.draft_id FROM draft_terminations t CROSS JOIN draft_retention_policy p
                WHERE t.terminated_at<=unixepoch('now')-p.metadata_seconds
                AND NOT EXISTS(SELECT 1 FROM draft_contents c WHERE c.draft_id=t.draft_id)'''
            removed = 0
            for table in METADATA_TABLES:
                count = connection.execute(f'''DELETE FROM {table} WHERE rowid IN (
                    SELECT rowid FROM {table} WHERE draft_id IN ({due}) ORDER BY rowid LIMIT ?)''',
                    (remaining,)).rowcount
                remaining -= count
                removed += count
                if not remaining:
                    return removed
            # Keep the terminal registry until every child receipt/audit is gone.
            empty = ' AND '.join(f'NOT EXISTS(SELECT 1 FROM {t} r WHERE r.draft_id=d.draft_id)'
                                 for t in METADATA_TABLES)
            count = connection.execute(f'''DELETE FROM draft_registry WHERE draft_id IN (
                SELECT d.draft_id FROM draft_registry d WHERE d.draft_id IN ({due})
                    AND {empty} ORDER BY d.draft_id LIMIT ?)''',(remaining//2,)).rowcount
            return removed+count*2
