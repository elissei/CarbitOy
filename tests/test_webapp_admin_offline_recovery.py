"""IT_ADMIN own offline MFA recovery: fictional accounts, runtime keys, isolated databases."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from uuid import NAMESPACE_OID, uuid4, uuid5

import pyotp
import pytest
from cryptography.fernet import Fernet

import webapp.admin_offline_recovery as recovery
from webapp.admin_offline_recovery import (
    GRANT_SECONDS, AdminRecoveryUnavailable, begin_admin_recovery_enrollment,
    cancel_admin_offline_recovery, complete_admin_offline_recovery, start_admin_offline_recovery,
)
from webapp.auth_crypto import PasswordService, TotpCipher
from webapp.auth_models import MfaResetRequest, UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import (
    AuthIdempotencyConflictError, AuthService, AuthenticationError, AuthVersionConflictError,
    PermissionDeniedError, RateLimitError, TrustedClientContext,
)
from webapp.database import WorkRepository
from webapp.workshop_admin import establish_initial_workshop


PASSWORD = "Fictional recovery passphrase only 7Q"
OFFLINE = TrustedClientContext("offline-recovery-console-test")


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


def provision(service, role, name):
    password = None if role == UserRole.OPENCLAW else PASSWORD
    return service.provision_identity(username=name, display_name="Fictional user", role=role, password=password)


def enroll(service, clock, user):
    connection = TrustedClientContext(str(user.user_id))
    challenge = service.begin_login(user.username, PASSWORD, client=connection)
    token = challenge.token.get_secret_value()
    secret = service.begin_mfa_enrollment(token).secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(clock.value), client=connection)
    return secret, grant


@pytest.fixture
def world(tmp_path, passwords):
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    clock = TestClock()
    service = AuthService(repository, TotpCipher((Fernet.generate_key(),)), passwords=passwords, clock=clock)
    admin = provision(service, UserRole.IT_ADMIN, "test.admin")
    admin_secret, admin_grant = enroll(service, clock, admin)
    owner = provision(service, UserRole.OWNER, "test.owner")
    _, owner_grant = enroll(service, clock, owner)
    clock.advance(60)
    return {"service": service, "repository": repository, "clock": clock, "admin": admin,
            "admin_secret": admin_secret, "admin_grant": admin_grant, "owner": owner, "owner_grant": owner_grant}


def rows(repository, query, parameters=()):
    with repository._connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters)]


def user_row(world, user):
    return rows(world["repository"], "SELECT * FROM auth_users WHERE user_id=?", (str(user.user_id),))[0]


def approval(clock, **overrides):
    values = {"authorizer_reference": "Fictional authorizer, entrepreneur",
              "verified_person_reference": "Fictional IT administrator",
              "verified_at": clock.value - timedelta(minutes=5),
              "approval_reference": "Fictional recovery approval A"}
    return values | overrides


def start_request(world, *, user=None, request_id=None, expected=None, **approval_overrides):
    user = user or world["admin"]
    return {"target_user_id": str(user.user_id),
            "expected_auth_version": expected if expected is not None else user_row(world, user)["auth_version"],
            "request_id": str(request_id or uuid4()), "approval": approval(world["clock"], **approval_overrides),
            "reason": "Fictional lost authenticator and recovery codes"}


def recover(world, state):
    service, clock = world["service"], world["clock"]
    token = state.grant_token.get_secret_value()
    enrollment = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
    secret = enrollment.secret.get_secret_value()
    return token, secret, complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE)


def snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: sorted(map(repr, connection.execute(f'SELECT * FROM "{table}"').fetchall())) for table in tables}


def _schema_objects(repository):
    return {row["name"] for row in rows(repository, "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def test_genuine_v2_store_upgrades_to_the_current_schema_and_is_repeatable(world, tmp_path):
    # Version 2 is a supported starting point: initialize() accepts any applied
    # set that contains 1 and is a subset of AUTH_SCHEMA_VERSIONS.
    repository, admin = world["repository"], world["admin"]
    repository.initialize()
    assert [row["version"] for row in rows(repository, "SELECT version FROM auth_schema_migrations ORDER BY version")] == [1, 2, 3, 4]
    current_objects = _schema_objects(repository)
    legacy = AuthRepository(tmp_path / "legacy.db")
    legacy.initialize()
    admin_id, reset_id, fresh_id = str(uuid4()), str(uuid4()), str(uuid4())
    password_hash = user_row(world, admin)["password_hash"]
    with legacy.transaction() as connection:  # strip every version-3 and version-4 structure
        for trigger in ("auth_users_recovery_state_insert", "auth_users_recovery_state_update"):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in ("auth_user_recovery_grants", "auth_admin_recovery_outbox", "auth_admin_recovery_requests"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE auth_users DROP COLUMN mfa_recovery_required")
        connection.execute("DELETE FROM auth_schema_migrations WHERE version IN (3,4)")
        for user_id, name, role in ((admin_id, "legacy.admin", "IT_ADMIN"), (reset_id, "legacy.reset", "OWNER"),
                                    (fresh_id, "legacy.fresh", "SERVICE_MANAGER")):
            connection.execute("INSERT INTO auth_users(user_id,username,display_name,kind,role,password_hash,created_at) "
                               "VALUES (?,?,?,?,?,?,1)", (user_id, name, "Fictional", "HUMAN", role, password_hash))
        connection.execute("INSERT INTO auth_events(event_type,subject_user_id,occurred_at,details_json) "
                           "VALUES ('MFA_RESET',?,1,'{}')", (reset_id,))
    v2_objects = _schema_objects(legacy)
    assert [row["version"] for row in rows(legacy, "SELECT version FROM auth_schema_migrations")] == [1, 2]
    assert "auth_store_identity" in v2_objects and not {name for name in v2_objects if name.startswith(
        ("auth_admin_recovery", "auth_user_recovery")) or "recovery_state" in name}
    assert "mfa_recovery_required" not in {row["name"] for row in rows(legacy, "PRAGMA table_info(auth_users)")}
    before_users = rows(legacy, "SELECT * FROM auth_users ORDER BY user_id")
    store_id = legacy.store_id()

    legacy.initialize()
    after_users = rows(legacy, "SELECT * FROM auth_users ORDER BY user_id")
    assert [{key: row[key] for key in before_users[0]} for row in after_users] == before_users
    assert {row["user_id"]: row["mfa_recovery_required"] for row in after_users} == {admin_id: 0, reset_id: 1, fresh_id: 0}
    assert [row["version"] for row in rows(legacy, "SELECT version FROM auth_schema_migrations ORDER BY version")] == [1, 2, 3, 4]
    assert _schema_objects(legacy) == current_objects and legacy.store_id() == store_id
    upgraded = snapshot(legacy.database_path)
    legacy.initialize()  # repeatable: no change on a second run
    assert snapshot(legacy.database_path) == upgraded


def test_legacy_reset_it_admin_marked_by_upgrade_completes_offline_recovery(world, tmp_path, passwords):
    # A pre-version-4 MFA_RESET on a HUMAN account that is now IT_ADMIN: the upgrade
    # marks it recovery-required, and the offline recovery must still finish validly.
    legacy = AuthRepository(tmp_path / "legacy-admin.db")
    legacy.initialize()
    admin_id = str(uuid4())
    with legacy.transaction() as connection:
        for trigger in ("auth_users_recovery_state_insert", "auth_users_recovery_state_update"):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in ("auth_user_recovery_grants", "auth_admin_recovery_outbox", "auth_admin_recovery_requests"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE auth_users DROP COLUMN mfa_recovery_required")
        connection.execute("DELETE FROM auth_schema_migrations WHERE version IN (3,4)")
        connection.execute("INSERT INTO auth_users(user_id,username,display_name,kind,role,password_hash,created_at) "
                           "VALUES (?,?,?,?,?,?,1)", (admin_id, "legacy.reset.admin", "Fictional", "HUMAN", "IT_ADMIN",
                                                      user_row(world, world["admin"])["password_hash"]))
        connection.execute("INSERT INTO auth_events(event_type,subject_user_id,occurred_at,details_json) "
                           "VALUES ('MFA_RESET',?,1,'{}')", (admin_id,))
    legacy.initialize()
    marked = rows(legacy, "SELECT * FROM auth_users WHERE user_id=?", (admin_id,))[0]
    assert (marked["mfa_recovery_required"], marked["mfa_enabled"], marked["enabled"]) == (1, 0, 1)

    clock = world["clock"]
    service = AuthService(legacy, TotpCipher((Fernet.generate_key(),)), passwords=passwords, clock=clock)
    state = start_admin_offline_recovery(service, {
        "target_user_id": admin_id, "expected_auth_version": marked["auth_version"], "request_id": str(uuid4()),
        "approval": approval(clock), "reason": "Fictional legacy reset administrator"})
    token = state.grant_token.get_secret_value()
    secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()
    done = complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE)
    assert done.status == "COMPLETED" and len(done.recovery_codes) == 10

    final = rows(legacy, "SELECT * FROM auth_users WHERE user_id=?", (admin_id,))[0]
    assert (final["mfa_enabled"], final["mfa_recovery_required"], final["enabled"]) == (1, 0, 1)
    assert final["auth_version"] == marked["auth_version"] + 1
    grant = rows(legacy, "SELECT * FROM auth_admin_recovery_requests WHERE request_id=?", (str(state.request_id),))[0]
    assert grant["status"] == "COMPLETED" and grant["pending_totp_ciphertext"] is None
    # The used grant is spent: a replay returns no codes and enrollment cannot restart.
    clock.advance(30)
    assert complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value),
                                           client=OFFLINE).recovery_codes == ()
    with pytest.raises(AdminRecoveryUnavailable):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)


def test_full_recovery_invalidates_old_state_verifies_new_totp_and_grants_no_session(world):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    old = user_row(world, admin)
    pending_login = service.begin_login(admin.username, PASSWORD, client=OFFLINE)
    old_code = world["admin_grant"].recovery_codes[0].get_secret_value()
    state = start_admin_offline_recovery(service, start_request(world))
    assert state.status == "OPEN" and state.grant_token is not None and not state.replayed
    assert state.recovery_auth_version == old["auth_version"] + 1
    assert state.expires_at == clock.value + timedelta(seconds=GRANT_SECONDS)
    closed = user_row(world, admin)
    assert (closed["enabled"], closed["mfa_enabled"], closed["totp_ciphertext"]) == (0, 0, None)
    assert closed["password_hash"] == old["password_hash"] and closed["role"] == "IT_ADMIN"
    with pytest.raises(AuthenticationError):
        service.require_session(world["admin_grant"].session.token.get_secret_value())
    with pytest.raises(AuthenticationError):
        service.complete_login(pending_login.token.get_secret_value(), client=OFFLINE, recovery_code=old_code)
    with pytest.raises(AuthenticationError):
        service.begin_login(admin.username, PASSWORD, client=OFFLINE)  # closed: password alone cannot enroll
    assert not rows(world["repository"], "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(admin.user_id),))
    assert not rows(world["repository"], "SELECT * FROM auth_challenges WHERE user_id=? AND consumed_at IS NULL",
                    (str(admin.user_id),))

    token, secret, done = recover(world, state)
    assert done.status == "COMPLETED" and len({code.get_secret_value() for code in done.recovery_codes}) == 10
    reopened = user_row(world, admin)
    assert (reopened["enabled"], reopened["mfa_enabled"]) == (1, 1) and reopened["auth_version"] == state.recovery_auth_version
    assert not rows(world["repository"], "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (str(admin.user_id),))  # no login session from recovery
    request = rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")[0]
    assert request["status"] == "COMPLETED" and request["pending_totp_ciphertext"] is None
    outbox = rows(world["repository"], "SELECT * FROM auth_admin_recovery_outbox ORDER BY created_at, message_type DESC")
    assert [row["message_type"] for row in outbox] == ["ADMIN_RECOVERY_STARTED", "ADMIN_RECOVERY_COMPLETED"]
    assert all(row["delivered_at"] is None for row in outbox)  # recorded intent, not delivery
    events = [row["event_type"] for row in rows(world["repository"], "SELECT event_type FROM auth_events ORDER BY event_id")]
    for expected in ("ADMIN_OFFLINE_RECOVERY_STARTED", "ADMIN_OFFLINE_RECOVERY_ENROLLMENT_STARTED",
                     "ADMIN_OFFLINE_RECOVERY_COMPLETED"):
        assert expected in events
    started = [row for row in rows(world["repository"], "SELECT details_json FROM auth_events WHERE event_type=?",
                                   ("ADMIN_OFFLINE_RECOVERY_STARTED",))]
    assert "Fictional authorizer" in started[0]["details_json"] and token not in started[0]["details_json"]

    clock.advance(30)
    session = service.complete_login(service.begin_login(admin.username, PASSWORD, client=OFFLINE).token.get_secret_value(),
                                     client=OFFLINE, totp_code=pyotp.TOTP(secret).at(clock.value))
    assert service.require_session(session.token.get_secret_value()).role == UserRole.IT_ADMIN
    challenge = service.begin_login(admin.username, PASSWORD, client=TrustedClientContext("second-console"))
    assert service.complete_login(challenge.token.get_secret_value(), client=TrustedClientContext("second-console"),
                                  recovery_code=done.recovery_codes[0].get_secret_value()).session.factor == "RECOVERY"


def test_wrong_password_and_totp_are_rejected_and_keep_the_account_closed(world):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    with pytest.raises(AuthenticationError):
        begin_admin_recovery_enrollment(service, token, "Wrong fictional passphrase 1", client=OFFLINE)
    assert rows(world["repository"], "SELECT pending_totp_ciphertext FROM auth_admin_recovery_requests")[0][
        "pending_totp_ciphertext"] is None
    with pytest.raises(AuthenticationError):
        begin_admin_recovery_enrollment(service, "x" * 43, PASSWORD, client=OFFLINE)
    secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()
    with pytest.raises(AuthenticationError):
        complete_admin_offline_recovery(service, token, pyotp.TOTP(pyotp.random_base32()).at(clock.value), client=OFFLINE)
    assert (user_row(world, admin)["enabled"], user_row(world, admin)["mfa_enabled"]) == (0, 0)
    assert complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value),
                                           client=OFFLINE).status == "COMPLETED"
    assert sum(row["event_type"] == "ADMIN_OFFLINE_RECOVERY_REJECTED"
               for row in rows(world["repository"], "SELECT event_type FROM auth_events")) == 2


@pytest.mark.parametrize("change", [
    {"authorizer_reference": ""}, {"verified_person_reference": "ab"}, {"approval_reference": "   "},
    {"verified_person_reference": "Fictional admin 131052-308T"}, {"approval_reference": "Document 12345678"},
    {"verified_at": datetime(2026, 10, 17, 8, 55)}, {"verified_at": datetime(2026, 10, 17, 10, tzinfo=timezone.utc)},
    {"method": "REMOTE_VIDEO"}, {"unexpected": "field"}, "missing-authorizer", "empty-reason",
])
def test_incomplete_or_identifying_approval_is_rejected_without_changes(world, change):
    service, admin = world["service"], world["admin"]
    before = user_row(world, admin)
    request = start_request(world)
    if change == "missing-authorizer":
        del request["approval"]["authorizer_reference"]
    elif change == "empty-reason":
        request["reason"] = "   "
    else:
        request["approval"].update(change)
    with pytest.raises(ValueError):
        start_admin_offline_recovery(service, request)
    assert user_row(world, admin) == before
    assert not rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")
    assert service.require_session(world["admin_grant"].session.token.get_secret_value())


@pytest.mark.parametrize("target", ["owner", "manager", "openclaw", "unknown", "stale-version"])
def test_wrong_target_or_stale_version_is_rejected_without_changes(world, target):
    service = world["service"]
    if target == "stale-version":
        request, error = start_request(world, expected=user_row(world, world["admin"])["auth_version"] + 1), AuthVersionConflictError
    else:
        user = {"owner": lambda: world["owner"],
                "manager": lambda: provision(service, UserRole.SERVICE_MANAGER, "test.manager"),
                "openclaw": lambda: provision(service, UserRole.OPENCLAW, "test.openclaw"),
                "unknown": lambda: None}[target]()
        request, error = start_request(world, user=user or world["admin"]), PermissionDeniedError
        if user is None:
            request["target_user_id"] = str(uuid4())
    snapshot_before = rows(world["repository"], "SELECT * FROM auth_users ORDER BY user_id")
    with pytest.raises(error):
        start_admin_offline_recovery(service, request)
    assert rows(world["repository"], "SELECT * FROM auth_users ORDER BY user_id") == snapshot_before
    assert not rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")


def test_existing_reset_still_rejects_it_admin_targets(world):
    service, clock = world["service"], world["clock"]
    other_admin = provision(service, UserRole.IT_ADMIN, "test.admin2")
    _, other_grant = enroll(service, clock, other_admin)
    request = MfaResetRequest(target_user_id=world["admin"].user_id,
                              expected_auth_version=user_row(world, world["admin"])["auth_version"], request_id=uuid4(),
                              identity_verification_reference="Fictional ticket", reason="Fictional")
    with pytest.raises(AuthenticationError):
        service.reset_user_mfa(other_grant.session.token.get_secret_value(), request)
    assert user_row(world, world["admin"])["mfa_enabled"] == 1


def test_expired_grant_keeps_account_closed_and_requires_a_new_authorization(world):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    first = start_admin_offline_recovery(service, start_request(world))
    token = first.grant_token.get_secret_value()
    secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()
    clock.advance(GRANT_SECONDS)
    with pytest.raises(AdminRecoveryUnavailable):
        complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE)
    request = rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")[0]
    assert request["status"] == "EXPIRED" and request["pending_totp_ciphertext"] is None
    assert user_row(world, admin)["enabled"] == 0
    with pytest.raises(AdminRecoveryUnavailable):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
    with pytest.raises(AuthenticationError):
        service.begin_login(admin.username, PASSWORD, client=OFFLINE)
    second = start_admin_offline_recovery(service, start_request(world, expected=first.recovery_auth_version))
    assert second.status == "OPEN" and second.recovery_auth_version == first.recovery_auth_version + 1
    with pytest.raises(AdminRecoveryUnavailable):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)  # old grant never revives
    assert recover(world, second)[2].status == "COMPLETED"


def test_used_grant_cannot_complete_again_and_replay_returns_no_codes(world):
    service, clock = world["service"], world["clock"]
    token, secret, done = recover(world, start_admin_offline_recovery(service, start_request(world)))
    clock.advance(30)
    replay = complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE)
    assert replay.status == "ALREADY_COMPLETED" and replay.recovery_codes == () and replay.request_id == done.request_id
    assert len(rows(world["repository"], "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(world["admin"].user_id),))) == 10
    with pytest.raises(AdminRecoveryUnavailable):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)


def test_start_replay_returns_truthful_state_without_token_and_conflicts_are_rejected(world):
    service, clock = world["service"], world["clock"]
    request_id = uuid4()
    request = start_request(world, request_id=request_id)
    first = start_admin_offline_recovery(service, request)
    events = len(rows(world["repository"], "SELECT * FROM auth_events"))
    replay = start_admin_offline_recovery(service, request)
    assert replay.replayed and replay.grant_token is None and replay.status == "OPEN"
    assert replay.recovery_auth_version == first.recovery_auth_version == user_row(world, world["admin"])["auth_version"]
    assert len(rows(world["repository"], "SELECT * FROM auth_events")) == events
    with pytest.raises(AuthIdempotencyConflictError):
        start_admin_offline_recovery(service, request | {"reason": "Different fictional reason"})
    with pytest.raises(AuthIdempotencyConflictError):
        start_admin_offline_recovery(service, start_request(world))  # one open recovery per account
    # Lost token: cancel, then a new authorization with a new request key.
    cancelled = cancel_admin_offline_recovery(service, request_id, reason="Fictional lost grant output")
    assert cancelled.status == "CANCELLED" and user_row(world, world["admin"])["enabled"] == 0
    assert cancel_admin_offline_recovery(service, request_id, reason="again").status == "CANCELLED"
    assert start_admin_offline_recovery(service, request).status == "CANCELLED"
    second = start_admin_offline_recovery(service, start_request(world))
    assert second.grant_token is not None
    clock.advance(GRANT_SECONDS)
    assert start_admin_offline_recovery(service, request).status == "CANCELLED"


def test_repeated_enrollment_step_replaces_the_pending_secret(world):
    service, clock = world["service"], world["clock"]
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    first = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
    second = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
    assert not first.replaced_previous_secret and second.replaced_previous_secret
    with pytest.raises(AuthenticationError):
        complete_admin_offline_recovery(service, token, pyotp.TOTP(first.secret.get_secret_value()).at(clock.value),
                                        client=OFFLINE)
    assert complete_admin_offline_recovery(service, token, pyotp.TOTP(second.secret.get_secret_value()).at(clock.value),
                                           client=OFFLINE).status == "COMPLETED"


def test_completed_recovery_cannot_be_cancelled_and_closed_account_is_not_reopened(world):
    service = world["service"]
    state = start_admin_offline_recovery(service, start_request(world))
    recover(world, state)
    with pytest.raises(AuthIdempotencyConflictError):
        cancel_admin_offline_recovery(service, state.request_id, reason="Fictional")
    with world["repository"].transaction() as connection:
        connection.execute("UPDATE auth_users SET enabled=0 WHERE user_id=?", (str(world["admin"].user_id),))
    with pytest.raises(PermissionDeniedError):
        start_admin_offline_recovery(service, start_request(world))


def test_failures_before_commit_roll_back_start_and_completion(world, monkeypatch):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    before = user_row(world, admin)
    original_audit = service._audit

    def failing_audit(connection, event, *args, **kwargs):
        if event == "ADMIN_OFFLINE_RECOVERY_STARTED":
            raise RuntimeError("injected audit failure")
        return original_audit(connection, event, *args, **kwargs)

    monkeypatch.setattr(service, "_audit", failing_audit)
    with pytest.raises(RuntimeError):
        start_admin_offline_recovery(service, start_request(world))
    assert user_row(world, admin) == before and not rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")
    assert service.require_session(world["admin_grant"].session.token.get_secret_value())
    monkeypatch.setattr(service, "_audit", original_audit)
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()

    def failing_codes():
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(recovery, "new_recovery_codes", failing_codes)
    with pytest.raises(RuntimeError):
        complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE)
    request = rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")[0]
    assert request["status"] == "OPEN" and request["pending_totp_ciphertext"] is not None
    assert (user_row(world, admin)["enabled"], user_row(world, admin)["mfa_enabled"]) == (0, 0)
    monkeypatch.undo()
    assert complete_admin_offline_recovery(service, token, pyotp.TOTP(secret).at(clock.value),
                                           client=OFFLINE).status == "COMPLETED"


def test_concurrent_starts_and_completions_have_one_winner(world):
    service, clock = world["service"], world["clock"]
    barrier = Barrier(2)

    def start(_):
        barrier.wait()
        try:
            return start_admin_offline_recovery(service, start_request(world))
        except (AuthIdempotencyConflictError, AuthVersionConflictError) as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(start, range(2)))
    winners = [result for result in results if not isinstance(result, Exception)]
    assert len(winners) == 1 and len(rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")) == 1
    token = winners[0].grant_token.get_secret_value()
    code = pyotp.TOTP(begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
                      .secret.get_secret_value()).at(clock.value)

    def complete(_):
        barrier.wait()
        return complete_admin_offline_recovery(service, token, code, client=OFFLINE).status

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(complete, range(2))) == ["ALREADY_COMPLETED", "COMPLETED"]
    assert len(rows(world["repository"], "SELECT * FROM auth_recovery_codes WHERE user_id=?",
                    (str(world["admin"].user_id),))) == 10


def test_attempts_are_rate_limited(world):
    service = world["service"]
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            begin_admin_recovery_enrollment(service, token, "Wrong fictional passphrase 1", client=OFFLINE)
    with pytest.raises(RateLimitError):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)


def test_other_users_and_business_data_are_preserved(world, tmp_path):
    service = world["service"]
    business = tmp_path / "works.db"
    WorkRepository(business).initialize()
    establish_initial_workshop(business, organization_id=str(uuid5(NAMESPACE_OID, "recovery.org")),
                               organization_name="Fictional organization",
                               workshop_id=str(uuid5(NAMESPACE_OID, "recovery.workshop")),
                               workshop_name="Fictional workshop", operator_reference="Fictional test setup")
    business_before = snapshot(business)
    owner_before = user_row(world, world["owner"])
    recover(world, start_admin_offline_recovery(service, start_request(world)))
    assert snapshot(business) == business_before
    assert user_row(world, world["owner"]) == owner_before
    assert service.require_session(world["owner_grant"].session.token.get_secret_value()).role == UserRole.OWNER


def _paused_inside_transaction(world, monkeypatch, attribute):
    """Pause the first call of a cipher method; it runs inside the caller's BEGIN IMMEDIATE.

    Only this pause point is wrapped; SQLite's real writer lock and the service
    code are unchanged. Returns (paused, release) events.
    """
    cipher, paused, release = world["service"].cipher, Event(), Event()
    original = getattr(cipher, attribute)

    def wrapper(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            assert release.wait(10), "test did not release the paused transaction"
        return original(*args, **kwargs)

    monkeypatch.setattr(cipher, attribute, wrapper)
    return paused, release


def _run(target):
    outcome = {}

    def runner():
        try:
            outcome["value"] = target()
        except Exception as error:  # surfaced through the assertions
            outcome["error"] = error

    thread = Thread(target=runner)
    thread.start()
    return thread, outcome


def _assert_writer_lock_held(world):
    # A separate connection cannot reserve the writer lock while the paused
    # operation holds it: the transaction boundary is SQLite's real lock.
    probe = sqlite3.connect(world["repository"].database_path, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.execute("BEGIN IMMEDIATE")
    finally:
        probe.close()


def test_secret_replacement_serialized_before_completion_rejects_the_previous_secret(world, monkeypatch):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    old_secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()
    old_code = pyotp.TOTP(old_secret).at(clock.value)
    paused, release = _paused_inside_transaction(world, monkeypatch, "encrypt")
    replacing, replaced = _run(lambda: begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE))
    assert paused.wait(10)  # the replacement holds the writer reservation, before its commit
    _assert_writer_lock_held(world)
    completing, completed = _run(lambda: complete_admin_offline_recovery(service, token, old_code, client=OFFLINE))
    release.set()
    replacing.join(10)
    completing.join(10)
    assert not replacing.is_alive() and not completing.is_alive()
    new_secret = replaced["value"].secret.get_secret_value()
    assert replaced["value"].replaced_previous_secret and new_secret != old_secret
    assert pyotp.TOTP(new_secret).at(clock.value) != old_code
    assert isinstance(completed.get("error"), AuthenticationError)
    assert not isinstance(completed["error"], (AdminRecoveryUnavailable, RateLimitError))
    # The rejected attempt neither reopened the account nor consumed the grant.
    assert (user_row(world, admin)["enabled"], user_row(world, admin)["mfa_enabled"]) == (0, 0)
    request = rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")[0]
    assert request["status"] == "OPEN" and request["pending_totp_ciphertext"] is not None
    assert not rows(world["repository"], "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(admin.user_id),))
    assert [row["details_json"] for row in rows(world["repository"], "SELECT details_json FROM auth_events WHERE "
            "event_type='ADMIN_OFFLINE_RECOVERY_REJECTED'")] == [f'{{"request_id": "{request["request_id"]}", "stage": "TOTP"}}']
    done = complete_admin_offline_recovery(service, token, pyotp.TOTP(new_secret).at(clock.value), client=OFFLINE)
    assert done.status == "COMPLETED" and len(done.recovery_codes) == 10
    assert not rows(world["repository"], "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (str(admin.user_id),))


def test_completion_serialized_before_secret_replacement_finishes_exactly_once(world, monkeypatch):
    service, clock, admin = world["service"], world["clock"], world["admin"]
    token = start_admin_offline_recovery(service, start_request(world)).grant_token.get_secret_value()
    secret = begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE).secret.get_secret_value()
    paused, release = _paused_inside_transaction(world, monkeypatch, "decrypt")
    completing, completed = _run(lambda: complete_admin_offline_recovery(
        service, token, pyotp.TOTP(secret).at(clock.value), client=OFFLINE))
    assert paused.wait(10)  # the completion holds the writer reservation, before its commit
    _assert_writer_lock_held(world)
    replacing, replaced = _run(lambda: begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE))
    release.set()
    completing.join(10)
    replacing.join(10)
    assert not completing.is_alive() and not replacing.is_alive()
    assert completed["value"].status == "COMPLETED" and len(completed["value"].recovery_codes) == 10
    assert isinstance(replaced.get("error"), AdminRecoveryUnavailable)
    after = user_row(world, admin)
    stored_factor = after["totp_ciphertext"]
    assert (after["enabled"], after["mfa_enabled"]) == (1, 1)
    request = rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")[0]
    assert request["status"] == "COMPLETED" and request["pending_totp_ciphertext"] is None
    codes = rows(world["repository"], "SELECT code_hash FROM auth_recovery_codes WHERE user_id=?", (str(admin.user_id),))
    events = [row["event_type"] for row in rows(world["repository"], "SELECT event_type FROM auth_events ORDER BY event_id")]
    assert events.count("ADMIN_OFFLINE_RECOVERY_COMPLETED") == 1
    assert events.count("ADMIN_OFFLINE_RECOVERY_ENROLLMENT_STARTED") == 1  # the later replacement wrote nothing
    assert len(codes) == 10
    assert not rows(world["repository"], "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (str(admin.user_id),))
    # A further replay changes neither the factor nor the codes.
    with pytest.raises(AdminRecoveryUnavailable):
        begin_admin_recovery_enrollment(service, token, PASSWORD, client=OFFLINE)
    assert user_row(world, admin)["totp_ciphertext"] == stored_factor
    assert rows(world["repository"], "SELECT code_hash FROM auth_recovery_codes WHERE user_id=?", (str(admin.user_id),)) == codes


# Approved D1 exception: local self-recovery of the operator's own IT_ADMIN account.

def self_request(world, *, user=None, **approval_overrides):
    request = start_request(world, user=user)
    request["approval"] = {"method": recovery.SELF_RECOVERY_METHOD, "operator_reference": "Fictional operator",
                           "record_reference": "Fictional self-recovery record A",
                           "operator_confirmed_own_account": True,
                           "operator_confirmed_service_stopped": True} | approval_overrides
    return request


def started_details(world):
    return [json.loads(row["details_json"]) for row in rows(
        world["repository"], "SELECT details_json FROM auth_events WHERE event_type=? ORDER BY event_id",
        ("ADMIN_OFFLINE_RECOVERY_STARTED",))]


def test_local_self_recovery_completes_and_records_no_external_authorizer(world):
    service, admin = world["service"], world["admin"]
    request = self_request(world)
    state = start_admin_offline_recovery(service, request)
    assert state.status == "OPEN" and state.grant_token is not None
    with pytest.raises(AuthenticationError):
        service.require_session(world["admin_grant"].session.token.get_secret_value())
    replay = start_admin_offline_recovery(service, request)
    assert replay.replayed and replay.grant_token is None
    token, _, done = recover(world, state)
    assert done.status == "COMPLETED" and len(done.recovery_codes) == 10
    assert (user_row(world, admin)["enabled"], user_row(world, admin)["mfa_enabled"]) == (1, 1)
    assert not rows(world["repository"], "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (str(admin.user_id),))
    [details] = started_details(world)
    assert details["recovery_mode"] == "LOCAL_SELF_RECOVERY" and details["method"] == recovery.SELF_RECOVERY_METHOD
    assert details["external_authorizer"] is None and details["in_person_identity_check"] is None
    assert details["operator_confirmed_own_account"] is True and details["operator_confirmed_service_stopped"] is True
    assert details["confirmations_verified_by_program"] is False
    assert details["operator_reference"] == "Fictional operator"
    assert not {"authorizer_reference", "verified_person_reference", "verified_at", "approval_reference"} & set(details)
    stored = json.loads(rows(world["repository"], "SELECT request_json FROM auth_admin_recovery_requests")[0]["request_json"])
    assert stored["approval"]["method"] == recovery.SELF_RECOVERY_METHOD
    assert token not in json.dumps(details)


def test_externally_authorized_recovery_is_recorded_separately_from_self_recovery(world):
    start_admin_offline_recovery(world["service"], start_request(world))
    [details] = started_details(world)
    assert details["recovery_mode"] == "EXTERNALLY_AUTHORIZED" and details["method"] == "IN_PERSON_ID_DOCUMENT"
    assert details["authorizer_reference"] == "Fictional authorizer, entrepreneur"
    assert not {"operator_reference", "external_authorizer", "operator_confirmed_own_account"} & set(details)


@pytest.mark.parametrize("change", [
    {"operator_confirmed_own_account": False}, {"operator_confirmed_service_stopped": False},
    "missing-confirmation", "missing-method", {"operator_reference": "ab"},
    {"record_reference": "Record 20261017"}, {"authorizer_reference": "Fictional authorizer"},
    {"verified_at": datetime(2026, 10, 17, 8, 55, tzinfo=timezone.utc)}, "self-method-on-authorized-approval",
])
def test_self_recovery_needs_explicit_selection_and_confirmations_or_writes_nothing(world, change):
    service, admin = world["service"], world["admin"]
    before = user_row(world, admin)
    request = self_request(world)
    if change == "missing-confirmation":
        del request["approval"]["operator_confirmed_service_stopped"]
    elif change == "missing-method":
        del request["approval"]["method"]  # without the explicit selection it is not a self-recovery
    elif change == "self-method-on-authorized-approval":
        request = start_request(world, method=recovery.SELF_RECOVERY_METHOD)
    else:
        request["approval"].update(change)
    with pytest.raises(ValueError):
        start_admin_offline_recovery(service, request)
    assert user_row(world, admin) == before
    assert not rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")
    assert service.require_session(world["admin_grant"].session.token.get_secret_value())


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.SERVICE_MANAGER, UserRole.OPENCLAW])
def test_self_recovery_is_not_accepted_for_business_or_service_accounts(world, role):
    service = world["service"]
    user = world["owner"] if role == UserRole.OWNER else provision(service, role, f"test.{role.value.lower()}")
    snapshot_before = rows(world["repository"], "SELECT * FROM auth_users ORDER BY user_id")
    with pytest.raises(PermissionDeniedError):
        start_admin_offline_recovery(service, self_request(world, user=user))
    assert rows(world["repository"], "SELECT * FROM auth_users ORDER BY user_id") == snapshot_before
    assert not rows(world["repository"], "SELECT * FROM auth_admin_recovery_requests")
