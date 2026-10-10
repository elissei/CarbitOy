import sqlite3
from uuid import uuid4

import pytest

import webapp.auth_repository as auth_storage
from webapp.auth_repository import AuthRepository
from webapp.database import WorkRepository


def test_new_schema_and_repeatable_initialization(tmp_path):
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    repository.initialize()
    with repository._connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"auth_users", "auth_sessions", "auth_challenges", "auth_enrollments", "auth_recovery_codes",
                "auth_rate_limits", "auth_events", "auth_reset_requests", "auth_notification_outbox"} <= tables
        assert connection.execute("SELECT version FROM auth_schema_migrations").fetchall()[0][0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_additive_migration_preserves_old_work_data_and_approval_schema(tmp_path):
    path = tmp_path / "legacy.db"
    work_repository = WorkRepository(path)
    work_repository.initialize()
    work = work_repository.create_work(registration="FAKE-123", customer_description="Fictional migration test", actor="Test fixture", idempotency_key=str(uuid4()))
    with work_repository._connect() as connection:
        before = connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        before = [tuple(row) for row in before]
    auth_repository = AuthRepository(path)
    auth_repository.initialize()
    auth_repository.initialize()
    assert work_repository.get_work(work["id"]) == work
    with auth_repository._connect() as connection:
        after = [tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'auth_%' ORDER BY name")]
        assert before == after
        assert connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM labour_review_decisions").fetchone()[0] == 0
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_migration_failure_rolls_back_every_new_table(tmp_path, monkeypatch):
    repository = AuthRepository(tmp_path / "failed.db")
    monkeypatch.setattr(auth_storage, "SCHEMA", auth_storage.SCHEMA + "INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        repository.initialize()
    with repository._connect() as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'auth_%'").fetchall()


def test_unknown_schema_version_fails_closed(tmp_path):
    repository = AuthRepository(tmp_path / "future.db")
    repository.initialize()
    with repository.transaction() as connection:
        connection.execute("INSERT INTO auth_schema_migrations VALUES (999)")
    with pytest.raises(RuntimeError, match="unknown"):
        repository.initialize()


def test_constraints_reject_invalid_human_service_and_session_references(tmp_path):
    repository = AuthRepository(tmp_path / "constraints.db")
    repository.initialize()
    with repository._connect() as connection:
        for kind, role, password in (("HUMAN", "OPENCLAW", "$argon2id$fixture"), ("HUMAN", "IT_ADMIN", None), ("SERVICE", "OWNER", None)):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO auth_users(user_id,username,display_name,kind,role,password_hash,created_at) VALUES (?,?,?,?,?,?,?)", (str(uuid4()), "test.fixture", "Fictional fixture", kind, role, password, 1))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO auth_challenges VALUES (?,?,?,?,?,?,?)", ("a" * 64, str(uuid4()), 1, "LOGIN", 1, 2, None))


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_security_events_are_immutable_including_replace(tmp_path, operation):
    repository = AuthRepository(tmp_path / "audit.db")
    repository.initialize()
    with repository.transaction() as connection:
        connection.execute("INSERT INTO auth_events VALUES (1,'TEST_EVENT',NULL,NULL,1,'{}')")
    with pytest.raises(sqlite3.IntegrityError):
        with repository.transaction() as connection:
            if operation == "UPDATE":
                connection.execute("UPDATE auth_events SET event_type='changed' WHERE event_id=1")
            elif operation == "DELETE":
                connection.execute("DELETE FROM auth_events WHERE event_id=1")
            else:
                connection.execute("INSERT OR REPLACE INTO auth_events VALUES (1,'changed',NULL,NULL,1,'{}')")
    with repository._connect() as connection:
        assert connection.execute("SELECT event_type FROM auth_events WHERE event_id=1").fetchone()[0] == "TEST_EVENT"
