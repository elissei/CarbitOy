"""Forward-only draft v3: key provenance and guarded terminal retention."""

from datetime import datetime, timezone


MAX_METADATA_SECONDS = 90 * 86400

MIGRATION_3 = (
    """ALTER TABLE draft_request_receipts ADD COLUMN fingerprint_key_id TEXT
        CHECK(fingerprint_key_id IS NULL OR (typeof(fingerprint_key_id)='text'
            AND length(fingerprint_key_id) BETWEEN 1 AND 64
            AND fingerprint_key_id NOT GLOB '*[^A-Za-z0-9_-]*'))""",
    "CREATE INDEX idx_draft_receipt_key ON draft_request_receipts(fingerprint_key_id)",
    """CREATE TRIGGER draft_new_receipt_key BEFORE INSERT ON draft_request_receipts
        WHEN NEW.fingerprint_key_id IS NULL
        BEGIN SELECT RAISE(ABORT,'new receipt requires a server key identifier'); END""",
    f"""CREATE TABLE draft_retention_policy (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        metadata_seconds INTEGER NOT NULL CHECK(typeof(metadata_seconds)='integer'
            AND metadata_seconds BETWEEN 86400 AND {MAX_METADATA_SECONDS})
    )""",
    f"INSERT INTO draft_retention_policy VALUES (1,{MAX_METADATA_SECONDS})",
    """CREATE TRIGGER draft_retention_policy_no_delete BEFORE DELETE ON draft_retention_policy
        BEGIN SELECT RAISE(ABORT,'draft retention policy must remain configured'); END""",
    """CREATE TABLE draft_terminations (
        draft_id TEXT PRIMARY KEY NOT NULL REFERENCES draft_registry(draft_id) ON DELETE CASCADE,
        reason TEXT NOT NULL CHECK(reason IN ('DISCARDED','EXPIRED','CONVERTED')),
        terminated_at INTEGER NOT NULL CHECK(typeof(terminated_at)='integer' AND terminated_at>=0)
    )""",
    "CREATE INDEX idx_draft_terminated ON draft_terminations(terminated_at,draft_id)",
    # Existing v2 discarded rows have their exact receipt timestamp. For v1/v2
    # expiry already physically purged, CREATED/CONTENT_SAVED triggers recorded
    # the last content-save statement's server time (not the later purge time).
    """INSERT INTO draft_terminations
        SELECT d.draft_id,CASE WHEN r.draft_id IS NOT NULL THEN 'DISCARDED'
            WHEN v.draft_id IS NOT NULL THEN 'CONVERTED' ELSE 'EXPIRED' END,
            COALESCE(r.recorded_at,v.converted_at,c.expires_at,
                (SELECT max(a.occurred_at)+604800 FROM draft_audit_events a
                 WHERE a.draft_id=d.draft_id AND a.event_type IN ('CREATED','CONTENT_SAVED')))
        FROM draft_registry d LEFT JOIN draft_contents c USING(draft_id)
        LEFT JOIN draft_request_receipts r ON r.draft_id=d.draft_id AND r.operation='DISCARD'
        LEFT JOIN draft_conversion_receipts v ON v.draft_id=d.draft_id
            AND (c.state='CONVERTED' OR EXISTS(SELECT 1 FROM draft_audit_events a
                WHERE a.draft_id=d.draft_id AND a.event_type='CONVERTED'))
        WHERE r.draft_id IS NOT NULL OR (c.state='CONVERTED' AND v.draft_id IS NOT NULL)
            OR (c.draft_id IS NULL AND EXISTS(SELECT 1 FROM draft_audit_events a
                WHERE a.draft_id=d.draft_id AND a.event_type='CONTENT_PURGED'))""",
    """CREATE TRIGGER immutable_draft_terminations_update BEFORE UPDATE ON draft_terminations
        BEGIN SELECT RAISE(ABORT,'immutable draft termination'); END""",
    """CREATE TRIGGER draft_discard_termination AFTER INSERT ON draft_request_receipts
        WHEN NEW.operation='DISCARD' BEGIN
        INSERT INTO draft_terminations VALUES (NEW.draft_id,'DISCARDED',NEW.recorded_at); END""",
    """CREATE TRIGGER draft_conversion_termination AFTER UPDATE ON draft_contents
        WHEN NEW.state='CONVERTED' AND OLD.state='ACTIVE' BEGIN
        INSERT INTO draft_terminations SELECT NEW.draft_id,'CONVERTED',converted_at
            FROM draft_conversion_receipts WHERE draft_id=NEW.draft_id; END""",
    """CREATE TRIGGER draft_expiry_termination AFTER DELETE ON draft_contents BEGIN
        INSERT INTO draft_terminations SELECT OLD.draft_id,'EXPIRED',OLD.expires_at
            WHERE NOT EXISTS(SELECT 1 FROM draft_terminations WHERE draft_id=OLD.draft_id);
        END""",
    """CREATE VIEW draft_termination_status AS
        SELECT d.draft_id,COALESCE(t.reason,CASE WHEN c.expires_at<=unixepoch('now')
            THEN 'EXPIRED' END) AS termination_reason,
            COALESCE(t.terminated_at,CASE WHEN c.expires_at<=unixepoch('now')
            THEN c.expires_at END) AS terminated_at
        FROM draft_registry d LEFT JOIN draft_terminations t USING(draft_id)
        LEFT JOIN draft_contents c USING(draft_id)""",
)

METADATA_TABLES = ('draft_request_receipts','draft_creation_receipts',
                  'draft_conversion_receipts','draft_audit_events')


def apply_migration_3(connection, applied):
    if 3 in applied:
        return
    for statement in MIGRATION_3:
        connection.execute(statement)
    for table in (*METADATA_TABLES,'draft_registry','draft_terminations'):
        if table!='draft_terminations':
            connection.execute(f'DROP TRIGGER immutable_{table}_delete')
        # No UPDATE exceptions. DELETE becomes possible only after termination,
        # the configured deadline, and physical removal of all content.
        time = 'OLD.terminated_at' if table=='draft_terminations' else 't.terminated_at'
        source = 'draft_retention_policy p' if table=='draft_terminations' else (
            'draft_terminations t CROSS JOIN draft_retention_policy p')
        match = '' if table=='draft_terminations' else 't.draft_id=OLD.draft_id AND '
        connection.execute(f"""CREATE TRIGGER immutable_{table}_delete BEFORE DELETE ON {table}
            WHEN NOT EXISTS(SELECT 1 FROM {source} WHERE {match}
                {time}<=unixepoch('now')-p.metadata_seconds
                AND NOT EXISTS(SELECT 1 FROM draft_contents c WHERE c.draft_id=OLD.draft_id))
            BEGIN SELECT RAISE(ABORT,'immutable draft metadata within retention'); END""")
    connection.execute('INSERT INTO draft_schema_migrations VALUES (3,?)',
                       (datetime.now(timezone.utc).isoformat(),))
