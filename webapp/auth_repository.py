"""Independent, additive SQLite authentication storage; no business-table writes."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('HUMAN','SERVICE')),
    role TEXT NOT NULL CHECK(role IN ('OWNER','SERVICE_MANAGER','IT_ADMIN','OPENCLAW')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    password_hash TEXT,
    mfa_enabled INTEGER NOT NULL DEFAULT 0 CHECK(mfa_enabled IN (0,1)),
    totp_ciphertext TEXT,
    last_totp_step INTEGER CHECK(last_totp_step IS NULL OR last_totp_step >= 0),
    auth_version INTEGER NOT NULL DEFAULT 1 CHECK(auth_version >= 1),
    created_at INTEGER NOT NULL,
    CHECK((kind='HUMAN' AND role!='OPENCLAW' AND password_hash IS NOT NULL AND password_hash LIKE '$argon2id$%') OR
          (kind='SERVICE' AND role='OPENCLAW' AND password_hash IS NULL AND mfa_enabled=0)),
    CHECK((mfa_enabled=1 AND totp_ciphertext IS NOT NULL) OR
          (mfa_enabled=0 AND totp_ciphertext IS NULL AND last_totp_step IS NULL))
);
CREATE TABLE IF NOT EXISTS auth_challenges (
    token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64),
    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    auth_version INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('LOGIN','ENROLL')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
    consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS auth_enrollments (
    user_id TEXT PRIMARY KEY REFERENCES auth_users(user_id),
    challenge_hash TEXT NOT NULL UNIQUE REFERENCES auth_challenges(token_hash),
    auth_version INTEGER NOT NULL,
    totp_ciphertext TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_recovery_codes (
    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    auth_version INTEGER NOT NULL,
    code_hash TEXT NOT NULL CHECK(length(code_hash)=64),
    consumed_at INTEGER,
    PRIMARY KEY(user_id, auth_version, code_hash)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    auth_version INTEGER NOT NULL,
    factor TEXT NOT NULL CHECK(factor IN ('TOTP','RECOVERY')),
    created_at INTEGER NOT NULL,
    mfa_verified_at INTEGER NOT NULL CHECK(mfa_verified_at >= created_at),
    expires_at INTEGER NOT NULL CHECK(expires_at > mfa_verified_at),
    last_seen_at INTEGER NOT NULL CHECK(last_seen_at >= created_at),
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
    scope_hash TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    attempts INTEGER NOT NULL CHECK(attempts >= 1)
);
CREATE TABLE IF NOT EXISTS auth_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_user_id TEXT REFERENCES auth_users(user_id),
    subject_user_id TEXT REFERENCES auth_users(user_id),
    occurred_at INTEGER NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json))
);
CREATE TABLE IF NOT EXISTS auth_notification_outbox (
    notification_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    event_id INTEGER NOT NULL UNIQUE REFERENCES auth_events(event_id),
    message_type TEXT NOT NULL CHECK(message_type='MFA_RESET'),
    created_at INTEGER NOT NULL,
    delivered_at INTEGER
);
CREATE TABLE IF NOT EXISTS auth_reset_requests (
    actor_user_id TEXT NOT NULL REFERENCES auth_users(user_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    PRIMARY KEY(actor_user_id, request_id)
);
"""

# Version 3: trusted offline IT_ADMIN own-MFA recovery. Additive only; the
# existing reset/outbox tables and their constraints are unchanged.
ADMIN_RECOVERY_SCHEMA = (
    """CREATE TABLE auth_admin_recovery_requests (
        request_id TEXT PRIMARY KEY CHECK(length(request_id)=36),
        target_user_id TEXT NOT NULL REFERENCES auth_users(user_id),
        request_json TEXT NOT NULL CHECK(json_valid(request_json)),
        previous_auth_version INTEGER NOT NULL CHECK(previous_auth_version >= 1),
        recovery_auth_version INTEGER NOT NULL CHECK(recovery_auth_version = previous_auth_version + 1),
        grant_hash TEXT NOT NULL UNIQUE CHECK(length(grant_hash)=64),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETED','CANCELLED','EXPIRED')),
        pending_totp_ciphertext TEXT,
        finished_at INTEGER,
        CHECK((status='OPEN') = (finished_at IS NULL)),
        CHECK(status='OPEN' OR pending_totp_ciphertext IS NULL)
    )""",
    """CREATE UNIQUE INDEX auth_admin_recovery_one_open
        ON auth_admin_recovery_requests(target_user_id) WHERE status='OPEN'""",
    """CREATE TRIGGER auth_admin_recovery_requests_no_delete
        BEFORE DELETE ON auth_admin_recovery_requests
        BEGIN SELECT RAISE(ABORT,'immutable administrator recovery history'); END""",
    """CREATE TRIGGER auth_admin_recovery_requests_final
        BEFORE UPDATE ON auth_admin_recovery_requests
        WHEN OLD.status <> 'OPEN' OR NEW.request_id IS NOT OLD.request_id
          OR NEW.target_user_id IS NOT OLD.target_user_id OR NEW.request_json IS NOT OLD.request_json
          OR NEW.previous_auth_version IS NOT OLD.previous_auth_version
          OR NEW.recovery_auth_version IS NOT OLD.recovery_auth_version
          OR NEW.grant_hash IS NOT OLD.grant_hash OR NEW.created_at IS NOT OLD.created_at
          OR NEW.expires_at IS NOT OLD.expires_at
        BEGIN SELECT RAISE(ABORT,'administrator recovery request is final'); END""",
    # A recorded notification intent, not a delivered message: no deliverer exists.
    """CREATE TABLE auth_admin_recovery_outbox (
        notification_id TEXT PRIMARY KEY CHECK(length(notification_id)=36),
        request_id TEXT NOT NULL REFERENCES auth_admin_recovery_requests(request_id),
        event_id INTEGER NOT NULL UNIQUE REFERENCES auth_events(event_id),
        message_type TEXT NOT NULL CHECK(message_type IN ('ADMIN_RECOVERY_STARTED','ADMIN_RECOVERY_COMPLETED')),
        created_at INTEGER NOT NULL,
        delivered_at INTEGER
    )""",
    """CREATE TRIGGER auth_admin_recovery_outbox_no_delete
        BEFORE DELETE ON auth_admin_recovery_outbox
        BEGIN SELECT RAISE(ABORT,'immutable administrator recovery notification'); END""",
    """CREATE TRIGGER auth_admin_recovery_outbox_delivery_only
        BEFORE UPDATE ON auth_admin_recovery_outbox
        WHEN NEW.notification_id IS NOT OLD.notification_id OR NEW.request_id IS NOT OLD.request_id
          OR NEW.event_id IS NOT OLD.event_id OR NEW.message_type IS NOT OLD.message_type
          OR NEW.created_at IS NOT OLD.created_at OR OLD.delivered_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'immutable administrator recovery notification'); END""",
)

# Version 4: business-user MFA recovery bound to a one-time grant. Additive only.
# ``mfa_recovery_required`` is the trusted state that separates a post-reset
# enrollment (grant required) from an ordinary first enrollment. Triggers keep
# it to 0/1 and only on a HUMAN account without an active factor.
USER_RECOVERY_SCHEMA = (
    "ALTER TABLE auth_users ADD COLUMN mfa_recovery_required INTEGER NOT NULL DEFAULT 0",
    """CREATE TRIGGER auth_users_recovery_state_insert BEFORE INSERT ON auth_users
        WHEN NEW.mfa_recovery_required NOT IN (0,1)
          OR (NEW.mfa_recovery_required=1 AND (NEW.mfa_enabled<>0 OR NEW.kind<>'HUMAN'))
        BEGIN SELECT RAISE(ABORT,'invalid MFA recovery state'); END""",
    """CREATE TRIGGER auth_users_recovery_state_update BEFORE UPDATE ON auth_users
        WHEN NEW.mfa_recovery_required NOT IN (0,1)
          OR (NEW.mfa_recovery_required=1 AND (NEW.mfa_enabled<>0 OR NEW.kind<>'HUMAN'))
        BEGIN SELECT RAISE(ABORT,'invalid MFA recovery state'); END""",
    # An account whose factor was reset before this version and never re-enrolled
    # must not regain password-only enrollment through the upgrade.
    """UPDATE auth_users SET mfa_recovery_required=1 WHERE mfa_enabled=0 AND kind='HUMAN'
        AND EXISTS (SELECT 1 FROM auth_events WHERE event_type='MFA_RESET'
                    AND subject_user_id=auth_users.user_id)""",
    """CREATE TABLE auth_user_recovery_grants (
        request_id TEXT PRIMARY KEY CHECK(length(request_id)=36),
        actor_user_id TEXT NOT NULL REFERENCES auth_users(user_id),
        target_user_id TEXT NOT NULL REFERENCES auth_users(user_id),
        request_json TEXT NOT NULL CHECK(json_valid(request_json)),
        purpose TEXT NOT NULL CHECK(purpose='MFA_RECOVERY_ENROLLMENT'),
        previous_auth_version INTEGER NOT NULL CHECK(previous_auth_version >= 1),
        recovery_auth_version INTEGER NOT NULL CHECK(recovery_auth_version = previous_auth_version + 1),
        grant_hash TEXT NOT NULL UNIQUE CHECK(length(grant_hash)=64),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL CHECK(expires_at > created_at),
        status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETED','EXPIRED','SUPERSEDED')),
        pending_totp_ciphertext TEXT,
        finished_at INTEGER,
        CHECK((status='OPEN') = (finished_at IS NULL)),
        CHECK(status='OPEN' OR pending_totp_ciphertext IS NULL)
    )""",
    """CREATE UNIQUE INDEX auth_user_recovery_one_open
        ON auth_user_recovery_grants(target_user_id) WHERE status='OPEN'""",
    """CREATE TRIGGER auth_user_recovery_grants_no_delete
        BEFORE DELETE ON auth_user_recovery_grants
        BEGIN SELECT RAISE(ABORT,'immutable user recovery history'); END""",
    """CREATE TRIGGER auth_user_recovery_grants_final
        BEFORE UPDATE ON auth_user_recovery_grants
        WHEN OLD.status <> 'OPEN' OR NEW.request_id IS NOT OLD.request_id
          OR NEW.actor_user_id IS NOT OLD.actor_user_id OR NEW.target_user_id IS NOT OLD.target_user_id
          OR NEW.request_json IS NOT OLD.request_json OR NEW.purpose IS NOT OLD.purpose
          OR NEW.previous_auth_version IS NOT OLD.previous_auth_version
          OR NEW.recovery_auth_version IS NOT OLD.recovery_auth_version
          OR NEW.grant_hash IS NOT OLD.grant_hash OR NEW.created_at IS NOT OLD.created_at
          OR NEW.expires_at IS NOT OLD.expires_at
        BEGIN SELECT RAISE(ABORT,'user recovery grant is final'); END""",
)
AUTH_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})


class AuthRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            for setting in ("foreign_keys", "recursive_triggers"):
                connection.execute(f"PRAGMA {setting}=ON")
                if connection.execute(f"PRAGMA {setting}").fetchone()[0] != 1:
                    raise RuntimeError(f"authentication storage requires {setting}=ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self._connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection

    def initialize(self) -> None:
        # Caller chooses an existing, access-controlled directory; no default DB/key.
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS auth_schema_migrations (version INTEGER PRIMARY KEY)")
            applied = {row[0] for row in connection.execute("SELECT version FROM auth_schema_migrations")}
            # Versions 2, 3 and 4 are independent additive steps on top of version 1.
            if applied and (1 not in applied or not applied <= AUTH_SCHEMA_VERSIONS):
                raise RuntimeError("unknown authentication schema version")
            if 1 not in applied:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                for table in ("auth_events", "auth_reset_requests"):
                    for operation in ("UPDATE", "DELETE"):
                        connection.execute(
                            f"CREATE TRIGGER {table}_immutable_{operation.lower()} "
                            f"BEFORE {operation} ON {table} BEGIN "
                            "SELECT RAISE(ABORT, 'immutable authentication history'); END"
                        )
                connection.execute("INSERT INTO auth_schema_migrations(version) VALUES (1)")
            if 2 not in applied:
                connection.execute("""CREATE TABLE auth_store_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    store_id TEXT NOT NULL UNIQUE CHECK(length(store_id)=36)
                )""")
                connection.execute("INSERT INTO auth_store_identity VALUES (1,?)", (str(uuid4()),))
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER auth_store_identity_immutable_{operation.lower()}
                        BEFORE {operation} ON auth_store_identity
                        BEGIN SELECT RAISE(ABORT,'immutable authentication store identity'); END""")
                connection.execute("INSERT INTO auth_schema_migrations(version) VALUES (2)")
            if 3 not in applied:
                for statement in ADMIN_RECOVERY_SCHEMA:
                    connection.execute(statement)
                connection.execute("INSERT INTO auth_schema_migrations(version) VALUES (3)")
            if 4 not in applied:
                for statement in USER_RECOVERY_SCHEMA:
                    connection.execute(statement)
                connection.execute("INSERT INTO auth_schema_migrations(version) VALUES (4)")

    def store_id(self, connection=None) -> str:
        """Persistent identity namespace; no secrets and no initialization on reads."""
        if connection is None:
            with self._connect() as connection:
                return self.store_id(connection)
        row = connection.execute("SELECT store_id FROM auth_store_identity WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("authentication store identity is missing")
        return row[0]
