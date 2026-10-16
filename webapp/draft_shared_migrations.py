"""Forward-only v4: workshop visibility and per-requester immutable receipts."""

from datetime import datetime, timezone


# Kept separate for atomic-failure testing, after the table replacement.
MIGRATION_4 = (
    "CREATE INDEX idx_draft_requests_draft ON draft_request_receipts(draft_id,recorded_at)",
    "CREATE UNIQUE INDEX idx_draft_discard_once ON draft_request_receipts(draft_id) WHERE operation='DISCARD'",
    "CREATE INDEX idx_draft_receipt_key ON draft_request_receipts(fingerprint_key_id)",
    """CREATE UNIQUE INDEX idx_draft_content_author
        ON draft_request_receipts(draft_id,accepted_revision)
        WHERE operation='SAVE' AND content_changed=1""",
    """CREATE TRIGGER draft_requester_insert BEFORE INSERT ON draft_request_receipts BEGIN
        SELECT CASE WHEN NEW.content_changed IS NULL OR NOT EXISTS (
            SELECT 1 FROM workshop_memberships m JOIN workshops w USING(workshop_id)
            JOIN repair_organizations o USING(organization_id)
            WHERE m.auth_store_id=NEW.auth_store_id AND m.user_id=NEW.requester_employee_id
                AND m.workshop_id=NEW.workshop_id AND m.enabled=1
                AND w.status='ACTIVE' AND o.enabled=1)
            THEN RAISE(ABORT,'request requires an active requester and change attribution') END;
        SELECT CASE WHEN NEW.operation='CREATE' AND NOT EXISTS (
            SELECT 1 FROM draft_registry d JOIN draft_creation_receipts r USING(draft_id)
            WHERE d.draft_id=NEW.draft_id AND d.owner_employee_id=NEW.requester_employee_id
                AND r.owner_employee_id=NEW.requester_employee_id
                AND r.creation_request_id=NEW.request_id)
            THEN RAISE(ABORT,'create requester must be the creator') END;
        SELECT CASE WHEN NEW.content_changed=1 AND NOT EXISTS (
            SELECT 1 FROM draft_audit_events a WHERE a.draft_id=NEW.draft_id
                AND a.revision=NEW.accepted_revision AND a.event_type='CONTENT_SAVED')
            THEN RAISE(ABORT,'changed save requires a content revision') END;
    END""",
    """CREATE VIEW active_drafts AS
        SELECT d.*,c.payload_json,c.revision,c.state,c.last_content_saved_at,c.expires_at
        FROM draft_registry d JOIN draft_contents c USING(draft_id)
        JOIN workshops w ON w.workshop_id=d.workshop_id AND w.organization_id=d.organization_id
        JOIN repair_organizations o ON o.organization_id=d.organization_id
        WHERE c.state='ACTIVE' AND c.expires_at>unixepoch('now')
            AND w.status='ACTIVE' AND o.enabled=1
            AND NOT EXISTS(SELECT 1 FROM draft_request_receipts r
                WHERE r.draft_id=d.draft_id AND r.operation='DISCARD')""",
)


def _request_table():
    # Import only when called: v1-v3 definitions remain independent and unchanged.
    from webapp.draft_migrations import _uuid, RETENTION_SECONDS
    return f"""CREATE TABLE draft_request_receipts_v4 (
        request_id TEXT NOT NULL CHECK({_uuid('request_id')}),
        operation TEXT NOT NULL CHECK(operation IN ('CREATE','SAVE','DISCARD')),
        draft_id TEXT NOT NULL,
        auth_store_id TEXT NOT NULL,
        requester_employee_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64
            AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
        accepted_revision INTEGER NOT NULL CHECK(typeof(accepted_revision)='integer' AND accepted_revision>=1),
        accepted_last_content_saved_at INTEGER NOT NULL CHECK(typeof(accepted_last_content_saved_at)='integer'),
        accepted_expires_at INTEGER GENERATED ALWAYS AS
            (accepted_last_content_saved_at+{RETENTION_SECONDS}) STORED,
        accepted_state TEXT NOT NULL CHECK(accepted_state IN ('ACTIVE','DISCARDED')),
        recorded_at INTEGER NOT NULL DEFAULT (unixepoch('now')) CHECK(typeof(recorded_at)='integer'),
        fingerprint_key_id TEXT CHECK(fingerprint_key_id IS NULL OR
            (typeof(fingerprint_key_id)='text' AND length(fingerprint_key_id) BETWEEN 1 AND 64
                AND fingerprint_key_id NOT GLOB '*[^A-Za-z0-9_-]*')),
        content_changed INTEGER CHECK(content_changed IS NULL OR
            (typeof(content_changed)='integer' AND content_changed IN (0,1)
                AND (operation='SAVE' OR content_changed=0))),
        PRIMARY KEY(auth_store_id,requester_employee_id,workshop_id,request_id),
        FOREIGN KEY(draft_id,auth_store_id,workshop_id,draft_type)
            REFERENCES draft_registry(draft_id,auth_store_id,workshop_id,draft_type),
        FOREIGN KEY(auth_store_id,requester_employee_id,workshop_id)
            REFERENCES workshop_memberships(auth_store_id,user_id,workshop_id)
    )"""


def apply_migration_4(connection, applied):
    """Rebuild only the receipt table in the caller's initialization transaction.

    All old data is copied before guards for NEW receipts are installed. No
    foreign-key/recursive-trigger settings are disabled. Any failure rolls the
    original table, dependent objects and ledger back with the caller.
    """
    if 4 in applied:
        return
    if not connection.in_transaction:
        raise RuntimeError('shared draft migration requires a transaction')
    # Views/triggers referencing the replaced table must be removed while its
    # name is absent; keep their exact v3 definitions rather than weakening guards.
    dependent = connection.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE type IN ('trigger','view') AND sql IS NOT NULL
            AND instr(sql,'draft_request_receipts')>0 ORDER BY type,name""").fetchall()
    for kind, name, sql in dependent:
        connection.execute(f'DROP {kind.upper()} "{name.replace(chr(34),chr(34)*2)}"')
    connection.execute('''CREATE UNIQUE INDEX idx_draft_registry_identity
        ON draft_registry(draft_id,auth_store_id,workshop_id,draft_type)''')
    connection.execute(_request_table())
    connection.execute('''INSERT INTO draft_request_receipts_v4
        (rowid,request_id,operation,draft_id,auth_store_id,requester_employee_id,workshop_id,draft_type,
            request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state,
            recorded_at,fingerprint_key_id,content_changed)
        SELECT rowid,request_id,operation,draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type,
            request_fingerprint,accepted_revision,accepted_last_content_saved_at,accepted_state,
            recorded_at,fingerprint_key_id,NULL FROM draft_request_receipts''')
    connection.execute('DROP TABLE draft_request_receipts')
    connection.execute('ALTER TABLE draft_request_receipts_v4 RENAME TO draft_request_receipts')
    for kind, name, sql in dependent:
        if name != 'active_drafts':
            connection.execute(sql)
    for sql in MIGRATION_4:
        connection.execute(sql)
    connection.execute('INSERT INTO draft_schema_migrations VALUES (4,?)',
                       (datetime.now(timezone.utc).isoformat(),))
