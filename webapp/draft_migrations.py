"""Versioned private-draft storage; never register migration functions as routes.

Content has a seven-day UTC deadline. Content-free identity/receipt/audit rows
survive its removal until the configured post-termination metadata deadline.
"""

import sqlite3
from datetime import datetime, timezone
from webapp.draft_hardening_migrations import apply_migration_3
from webapp.draft_shared_migrations import apply_migration_4


RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_CLEANUP_BATCH = 100

# Compare editor values, not JSON whitespace, key order or Unicode escaping.
CONTENT_CHANGED_SQL = """(EXISTS (
    SELECT key,type,atom FROM json_each(NEW.payload_json)
    EXCEPT SELECT key,type,atom FROM json_each(OLD.payload_json)) OR EXISTS (
    SELECT key,type,atom FROM json_each(OLD.payload_json)
    EXCEPT SELECT key,type,atom FROM json_each(NEW.payload_json)))"""


def _uuid(column):
    return f"""typeof({column})='text' AND length({column})=36
        AND substr({column},9,1)='-' AND substr({column},14,1)='-'
        AND substr({column},19,1)='-' AND substr({column},24,1)='-'
        AND length(replace({column},'-',''))=32
        AND replace({column},'-','') NOT GLOB '*[^0-9a-f]*'"""


# Generated inside SQLite, never from an editor's payload. Future server code
# must omit draft_id on INSERT: SQL cannot attest the origin of a supplied UUID.
UUID_DEFAULT = """(lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2)))
    || '-4' || substr(lower(hex(randomblob(2))),2) || '-'
    || substr('89ab',abs(random()%4)+1,1) || substr(lower(hex(randomblob(2))),2)
    || '-' || lower(hex(randomblob(6))))"""

SCHEMA = (
    f"""CREATE TABLE draft_registry (
        draft_id TEXT PRIMARY KEY NOT NULL DEFAULT {UUID_DEFAULT}
            CHECK({_uuid('draft_id')} AND substr(draft_id,15,1)='4'
                  AND substr(draft_id,20,1) IN ('8','9','a','b')),
        organization_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        auth_store_id TEXT NOT NULL CHECK({_uuid('auth_store_id')}),
        owner_employee_id TEXT NOT NULL CHECK({_uuid('owner_employee_id')}),
        draft_type TEXT NOT NULL CHECK(draft_type IN ('WORK_INQUIRY','LABOUR_ENTRY')),
        payload_schema_version INTEGER NOT NULL DEFAULT 1
            CHECK(typeof(payload_schema_version)='integer' AND payload_schema_version=1),
        work_id TEXT,
        original_work_version INTEGER,
        created_at INTEGER NOT NULL DEFAULT (unixepoch('now'))
            CHECK(typeof(created_at)='integer' AND created_at>=0),
        CHECK((draft_type='WORK_INQUIRY' AND work_id IS NULL AND original_work_version IS NULL)
            OR (draft_type='LABOUR_ENTRY' AND work_id IS NOT NULL
                AND typeof(original_work_version)='integer' AND original_work_version>=1)),
        UNIQUE(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type),
        FOREIGN KEY(workshop_id,organization_id) REFERENCES workshops(workshop_id,organization_id),
        FOREIGN KEY(auth_store_id,owner_employee_id,workshop_id)
            REFERENCES workshop_memberships(auth_store_id,user_id,workshop_id),
        FOREIGN KEY(work_id,workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id,workshop_id)
    )""",
    """CREATE INDEX idx_draft_registry_owner
        ON draft_registry(workshop_id,auth_store_id,owner_employee_id,draft_type,draft_id)""",
    "CREATE INDEX idx_draft_registry_work ON draft_registry(work_id,workshop_id)",
    f"""CREATE TABLE draft_contents (
        draft_id TEXT PRIMARY KEY NOT NULL REFERENCES draft_registry(draft_id),
        payload_json TEXT NOT NULL DEFAULT '{{}}'
            CHECK(typeof(payload_json)='text' AND json_valid(payload_json)
                  AND json_type(payload_json)='object'
                  AND length(CAST(payload_json AS BLOB)) <= {MAX_PAYLOAD_BYTES}),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
        state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','CONVERTED')),
        last_content_saved_at INTEGER NOT NULL DEFAULT (unixepoch('now'))
            CHECK(typeof(last_content_saved_at)='integer' AND last_content_saved_at>=0),
        expires_at INTEGER GENERATED ALWAYS AS
            (last_content_saved_at + {RETENTION_SECONDS}) STORED
    )""",
    "CREATE INDEX idx_draft_contents_expiry ON draft_contents(expires_at,draft_id)",
    f"""CREATE TABLE draft_creation_receipts (
        creation_request_id TEXT NOT NULL CHECK({_uuid('creation_request_id')}),
        draft_id TEXT NOT NULL UNIQUE,
        auth_store_id TEXT NOT NULL,
        owner_employee_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (unixepoch('now')) CHECK(typeof(created_at)='integer'),
        PRIMARY KEY(auth_store_id,owner_employee_id,workshop_id,draft_type,creation_request_id),
        FOREIGN KEY(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            REFERENCES draft_registry(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
    )""",
    f"""CREATE TABLE draft_conversion_receipts (
        conversion_request_id TEXT NOT NULL CHECK({_uuid('conversion_request_id')}),
        draft_id TEXT NOT NULL UNIQUE,
        auth_store_id TEXT NOT NULL,
        owner_employee_id TEXT NOT NULL,
        workshop_id TEXT NOT NULL,
        draft_type TEXT NOT NULL,
        draft_revision INTEGER NOT NULL CHECK(typeof(draft_revision)='integer' AND draft_revision>=1),
        work_id TEXT NOT NULL,
        work_version INTEGER NOT NULL CHECK(typeof(work_version)='integer' AND work_version>=1),
        converted_at INTEGER NOT NULL DEFAULT (unixepoch('now')) CHECK(typeof(converted_at)='integer'),
        PRIMARY KEY(auth_store_id,owner_employee_id,workshop_id,conversion_request_id),
        FOREIGN KEY(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            REFERENCES draft_registry(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type),
        FOREIGN KEY(work_id,workshop_id)
            REFERENCES inquiry_workshop_assignments(inquiry_id,workshop_id)
    )""",
    "CREATE INDEX idx_draft_conversion_work ON draft_conversion_receipts(work_id,workshop_id)",
    """CREATE TABLE draft_audit_events (
        event_id INTEGER PRIMARY KEY,
        draft_id TEXT NOT NULL REFERENCES draft_registry(draft_id),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
        event_type TEXT NOT NULL CHECK(event_type IN
            ('CREATED','CONTENT_SAVED','CONVERTED','CONTENT_PURGED')),
        occurred_at INTEGER NOT NULL DEFAULT (unixepoch('now')) CHECK(typeof(occurred_at)='integer')
    )""",
    "CREATE INDEX idx_draft_audit_history ON draft_audit_events(draft_id,event_id)",
    """CREATE TRIGGER draft_registry_insert BEFORE INSERT ON draft_registry BEGIN
        SELECT CASE WHEN NEW.created_at <> unixepoch('now')
            THEN RAISE(ABORT,'draft creation time must be server time') END;
        SELECT CASE WHEN NEW.draft_type='LABOUR_ENTRY' AND NEW.original_work_version <>
            (SELECT version FROM work_inquiries WHERE id=NEW.work_id)
            THEN RAISE(ABORT,'draft requires the original current work version') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM workshop_memberships m JOIN workshops w USING(workshop_id)
            JOIN repair_organizations o USING(organization_id)
            WHERE m.auth_store_id=NEW.auth_store_id AND m.user_id=NEW.owner_employee_id
              AND m.workshop_id=NEW.workshop_id AND w.organization_id=NEW.organization_id
              AND m.enabled=1 AND w.status='ACTIVE' AND o.enabled=1)
            THEN RAISE(ABORT,'draft requires an explicit active membership') END;
    END""",
    """CREATE TRIGGER draft_contents_insert BEFORE INSERT ON draft_contents BEGIN
        SELECT CASE WHEN NEW.revision<>1 OR NEW.state<>'ACTIVE'
            OR NEW.last_content_saved_at<>unixepoch('now')
            OR NEW.last_content_saved_at<(SELECT created_at FROM draft_registry WHERE draft_id=NEW.draft_id)
            THEN RAISE(ABORT,'invalid initial draft metadata') END;
        SELECT CASE WHEN EXISTS(SELECT 1 FROM draft_audit_events WHERE draft_id=NEW.draft_id)
            THEN RAISE(ABORT,'a purged draft cannot be recreated') END;
    END""",
    f"""CREATE TRIGGER draft_contents_update BEFORE UPDATE ON draft_contents BEGIN
        SELECT CASE WHEN NEW.draft_id<>OLD.draft_id OR OLD.state<>'ACTIVE'
            OR OLD.expires_at<=unixepoch('now')
            THEN RAISE(ABORT,'immutable or expired draft') END;
        SELECT CASE WHEN {CONTENT_CHANGED_SQL} AND (
            NEW.state<>OLD.state OR NEW.revision<>OLD.revision+1
            OR NEW.last_content_saved_at<>unixepoch('now')
            OR NEW.last_content_saved_at<OLD.last_content_saved_at)
            THEN RAISE(ABORT,'content save requires a new revision and server time') END;
        SELECT CASE WHEN NOT {CONTENT_CHANGED_SQL} AND (
            NEW.last_content_saved_at<>OLD.last_content_saved_at
            OR NEW.revision<>OLD.revision+(NEW.state<>OLD.state))
            THEN RAISE(ABORT,'unchanged content cannot renew retention') END;
        SELECT CASE WHEN NEW.state<>OLD.state AND ({CONTENT_CHANGED_SQL}
            OR NEW.state<>'CONVERTED' OR NOT EXISTS (
                SELECT 1 FROM draft_conversion_receipts r WHERE r.draft_id=OLD.draft_id
                AND r.draft_revision=OLD.revision))
            THEN RAISE(ABORT,'conversion requires a matching receipt') END;
    END""",
    """CREATE TRIGGER draft_contents_delete BEFORE DELETE ON draft_contents
        WHEN OLD.expires_at>unixepoch('now')
        BEGIN SELECT RAISE(ABORT,'only expired draft content may be purged'); END""",
    """CREATE TRIGGER draft_creation_receipt_insert BEFORE INSERT ON draft_creation_receipts
        BEGIN SELECT CASE WHEN NEW.created_at<>unixepoch('now') OR NOT EXISTS (
            SELECT 1 FROM draft_registry d JOIN draft_contents c USING(draft_id)
            WHERE d.draft_id=NEW.draft_id AND d.created_at<=NEW.created_at
                AND c.revision=1 AND c.state='ACTIVE' AND c.expires_at>unixepoch('now'))
            THEN RAISE(ABORT,'creation receipt requires a new active draft') END; END""",
    """CREATE TRIGGER draft_conversion_receipt_insert BEFORE INSERT ON draft_conversion_receipts
        BEGIN SELECT CASE WHEN NEW.converted_at<>unixepoch('now') OR NOT EXISTS (
            SELECT 1 FROM draft_registry d JOIN draft_contents c USING(draft_id)
            JOIN work_inquiries w ON w.id=NEW.work_id
            WHERE d.draft_id=NEW.draft_id AND c.state='ACTIVE'
                AND c.expires_at>unixepoch('now') AND c.revision=NEW.draft_revision
                AND w.version=NEW.work_version AND (d.draft_type='WORK_INQUIRY'
                    OR (d.work_id=NEW.work_id AND NEW.work_version>d.original_work_version)))
            THEN RAISE(ABORT,'conversion receipt requires a current scoped result') END; END""",
    """CREATE VIEW active_drafts AS
        SELECT d.*,c.payload_json,c.revision,c.state,c.last_content_saved_at,c.expires_at
        FROM draft_registry d JOIN draft_contents c USING(draft_id)
        JOIN workshop_memberships m ON m.auth_store_id=d.auth_store_id
            AND m.user_id=d.owner_employee_id AND m.workshop_id=d.workshop_id
        JOIN workshops w ON w.workshop_id=d.workshop_id AND w.organization_id=d.organization_id
        JOIN repair_organizations o ON o.organization_id=d.organization_id
        WHERE c.state='ACTIVE' AND c.expires_at>unixepoch('now')
            AND m.enabled=1 AND w.status='ACTIVE' AND o.enabled=1""",
)


def _payload_trigger(action):
    # Closed editor-only contract. Missing/null/empty/invalid decimal text is
    # allowed; nested objects, duplicate keys and derived/privileged fields aren't.
    return f"""CREATE TRIGGER draft_payload_{action.lower()}
        BEFORE {action} ON draft_contents BEGIN
        SELECT CASE WHEN typeof(NEW.payload_json)<>'text'
            OR length(CAST(NEW.payload_json AS BLOB))>{MAX_PAYLOAD_BYTES}
            THEN RAISE(ABORT,'draft payload exceeds storage bounds') END;
        SELECT CASE WHEN NOT json_valid(NEW.payload_json)
            THEN RAISE(ABORT,'invalid draft JSON') END;
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM json_each(NEW.payload_json) p
            JOIN draft_registry d ON d.draft_id=NEW.draft_id
            WHERE p.type NOT IN ('text','null') OR instr(p.value,char(0))>0
                OR CASE d.draft_type
                    WHEN 'WORK_INQUIRY' THEN CASE p.key
                        WHEN 'registration' THEN length(p.value)>20
                        WHEN 'customer_description' THEN length(p.value)>4000 ELSE 1 END
                    WHEN 'LABOUR_ENTRY' THEN CASE p.key
                        WHEN 'hours' THEN length(p.value)>64
                        WHEN 'source' THEN length(p.value)>32
                        WHEN 'work_description' THEN length(p.value)>2000
                        WHEN 'autodata_reference' THEN length(p.value)>500 ELSE 1 END
                    ELSE 1 END)
            OR (SELECT count(*) FROM json_each(NEW.payload_json)) <>
               (SELECT count(DISTINCT key) FROM json_each(NEW.payload_json))
            THEN RAISE(ABORT,'invalid draft editor fields') END;
    END"""


IMMUTABLE_TABLES = (
    'draft_registry', 'draft_creation_receipts', 'draft_conversion_receipts', 'draft_audit_events',
)


# Version 1 remains unchanged. Version 2 adds content-free request fingerprints
# and the explicit discard authorization required to delete ACTIVE content.
MIGRATION_2 = (
    f"""CREATE TABLE draft_request_receipts (
        request_id TEXT NOT NULL CHECK({_uuid('request_id')}),
        operation TEXT NOT NULL CHECK(operation IN ('CREATE','SAVE','DISCARD')),
        draft_id TEXT NOT NULL,
        auth_store_id TEXT NOT NULL,
        owner_employee_id TEXT NOT NULL,
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
        PRIMARY KEY(auth_store_id,owner_employee_id,workshop_id,request_id),
        FOREIGN KEY(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
            REFERENCES draft_registry(draft_id,auth_store_id,owner_employee_id,workshop_id,draft_type)
    )""",
    "CREATE INDEX idx_draft_requests_draft ON draft_request_receipts(draft_id,recorded_at)",
    "CREATE UNIQUE INDEX idx_draft_discard_once ON draft_request_receipts(draft_id) WHERE operation='DISCARD'",
    """CREATE TRIGGER draft_request_receipt_insert BEFORE INSERT ON draft_request_receipts BEGIN
        SELECT CASE WHEN NEW.recorded_at<>unixepoch('now') OR NOT EXISTS (
            SELECT 1 FROM draft_contents c WHERE c.draft_id=NEW.draft_id
            AND c.state='ACTIVE' AND c.expires_at>unixepoch('now')
            AND c.last_content_saved_at=NEW.accepted_last_content_saved_at
            AND NEW.accepted_revision=c.revision+(NEW.operation='DISCARD')
            AND NEW.accepted_state=CASE WHEN NEW.operation='DISCARD' THEN 'DISCARDED' ELSE 'ACTIVE' END
            AND NOT EXISTS(SELECT 1 FROM draft_request_receipts r WHERE r.draft_id=c.draft_id AND r.operation='DISCARD'))
            THEN RAISE(ABORT,'request receipt requires the current active draft') END;
        SELECT CASE WHEN NEW.operation='CREATE' AND NOT EXISTS (
            SELECT 1 FROM draft_creation_receipts r JOIN draft_contents c USING(draft_id)
            WHERE r.draft_id=NEW.draft_id AND r.creation_request_id=NEW.request_id AND c.revision=1)
            THEN RAISE(ABORT,'create request requires a creation receipt') END;
    END""",
    """CREATE TRIGGER immutable_draft_request_receipts_update BEFORE UPDATE ON draft_request_receipts
        BEGIN SELECT RAISE(ABORT,'immutable draft request receipt'); END""",
    """CREATE TRIGGER immutable_draft_request_receipts_delete BEFORE DELETE ON draft_request_receipts
        BEGIN SELECT RAISE(ABORT,'immutable draft request receipt'); END""",
    """CREATE TRIGGER discarded_draft_no_update BEFORE UPDATE ON draft_contents
        WHEN EXISTS(SELECT 1 FROM draft_request_receipts WHERE draft_id=OLD.draft_id AND operation='DISCARD')
        BEGIN SELECT RAISE(ABORT,'discarded draft is terminal'); END""",
    "DROP TRIGGER draft_contents_delete",
    """CREATE TRIGGER draft_contents_delete BEFORE DELETE ON draft_contents
        WHEN OLD.expires_at>unixepoch('now') AND NOT EXISTS (
            SELECT 1 FROM draft_request_receipts r WHERE r.draft_id=OLD.draft_id
                AND r.operation='DISCARD' AND r.accepted_state='DISCARDED'
                AND r.accepted_revision=OLD.revision+1
                AND r.accepted_last_content_saved_at=OLD.last_content_saved_at)
        BEGIN SELECT RAISE(ABORT,'content deletion requires expiry or an explicit discard receipt'); END""",
    "DROP VIEW active_drafts",
    """CREATE VIEW active_drafts AS
        SELECT d.*,c.payload_json,c.revision,c.state,c.last_content_saved_at,c.expires_at
        FROM draft_registry d JOIN draft_contents c USING(draft_id)
        JOIN workshop_memberships m ON m.auth_store_id=d.auth_store_id
            AND m.user_id=d.owner_employee_id AND m.workshop_id=d.workshop_id
        JOIN workshops w ON w.workshop_id=d.workshop_id AND w.organization_id=d.organization_id
        JOIN repair_organizations o ON o.organization_id=d.organization_id
        WHERE c.state='ACTIVE' AND c.expires_at>unixepoch('now')
            AND m.enabled=1 AND w.status='ACTIVE' AND o.enabled=1
            AND NOT EXISTS(SELECT 1 FROM draft_request_receipts r WHERE r.draft_id=d.draft_id AND r.operation='DISCARD')""",
)

# SQL contract only; CP4B-2A installs no CRUD method, job or command. A future
# trusted maintenance executor must use a bounded transaction and a recovery plan.
# Bad or oversized limits deliberately delete nothing (SQLite LIMIT -1 is unsafe).
EXPIRED_CONTENT_CLEANUP_SQL = f"""DELETE FROM draft_contents WHERE draft_id IN (
    SELECT draft_id FROM draft_contents WHERE expires_at<=unixepoch('now')
    ORDER BY expires_at,draft_id LIMIT CASE
        WHEN typeof(:batch_size)='integer' AND :batch_size BETWEEN 1 AND {MAX_CLEANUP_BATCH}
        THEN :batch_size ELSE 0 END)
"""


def apply_draft_migrations(connection: sqlite3.Connection) -> None:
    """Add empty storage in the caller's initialization transaction, never seed."""
    if not connection.in_transaction:
        raise RuntimeError('draft migration requires an active transaction')
    for setting in ('foreign_keys', 'recursive_triggers'):
        if connection.execute(f'PRAGMA {setting}').fetchone()[0] != 1:
            raise RuntimeError(f'draft migration requires {setting}=ON')
    # Fail atomically before installing storage on an unsupported SQLite build.
    connection.execute("SELECT unixepoch('now'), json_valid('{}')").fetchone()
    connection.execute("""CREATE TABLE IF NOT EXISTS draft_schema_migrations (
        version INTEGER PRIMARY KEY NOT NULL CHECK(version>=1), applied_at TEXT NOT NULL
    )""")
    applied = {row[0] for row in connection.execute('SELECT version FROM draft_schema_migrations')}
    if applied not in (set(), {1}, {1, 2}, {1, 2, 3}, {1, 2, 3, 4}):
        raise RuntimeError('unknown or incomplete draft schema version')
    if 1 in applied:
        _apply_migration_2(connection, applied)
        apply_migration_3(connection, applied)
        apply_migration_4(connection, applied)
        return
    for statement in SCHEMA:
        connection.execute(statement)
    for action in ('INSERT', 'UPDATE'):
        connection.execute(_payload_trigger(action))
    for table in IMMUTABLE_TABLES:
        for action in ('UPDATE', 'DELETE'):
            connection.execute(f"""CREATE TRIGGER immutable_{table}_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable draft metadata'); END""")
    for action, event, condition, row in (
        ('INSERT', 'CREATED', '', 'NEW'),
        ('UPDATE', 'CONTENT_SAVED', f'WHEN {CONTENT_CHANGED_SQL}', 'NEW'),
        ('UPDATE', 'CONVERTED', 'WHEN NEW.state<>OLD.state', 'NEW'),
        ('DELETE', 'CONTENT_PURGED', '', 'OLD'),
    ):
        connection.execute(f"""CREATE TRIGGER draft_audit_{event.lower()}
            AFTER {action} ON draft_contents {condition} BEGIN
            INSERT INTO draft_audit_events(draft_id,revision,event_type)
                VALUES ({row}.draft_id,{row}.revision,'{event}'); END""")
    connection.execute('INSERT INTO draft_schema_migrations VALUES (1,?)',
                       (datetime.now(timezone.utc).isoformat(),))
    _apply_migration_2(connection, applied)
    apply_migration_3(connection, applied)
    apply_migration_4(connection, applied)


def _apply_migration_2(connection, applied):
    if 2 in applied:
        return
    for statement in MIGRATION_2:
        connection.execute(statement)
    connection.execute('INSERT INTO draft_schema_migrations VALUES (2,?)',
                       (datetime.now(timezone.utc).isoformat(),))
