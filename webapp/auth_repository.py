"""Independent, additive SQLite authentication storage; no business-table writes."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


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
            if applied - {1}:
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
