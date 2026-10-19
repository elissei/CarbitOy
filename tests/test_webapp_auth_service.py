"""Internal authentication flows, using fictional accounts and runtime-generated keys."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pyotp
import pytest
from argon2 import PasswordHasher, Type
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from webapp.auth_crypto import PasswordService, TotpCipher, token_digest
from webapp.auth_models import IdentityKind, MfaResetRequest, UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import (
    AuthIdempotencyConflictError, AuthPolicy, AuthService, AuthenticationError,
    AuthVersionConflictError, PermissionDeniedError, RateLimitError, TrustedClientContext,
)
from webapp.user_mfa_recovery import (
    begin_user_recovery_enrollment, complete_user_mfa_recovery, start_user_mfa_recovery,
)
from http_auth_support import create_test_app as create_app


PASSWORD = "Fictional test passphrase only 4E2"


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


@pytest.fixture
def setup(tmp_path, passwords):
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    clock = TestClock()
    service = AuthService(repository, TotpCipher((Fernet.generate_key(),)), passwords=passwords, clock=clock)
    return service, repository, clock


def client(key="fictional-test-connection"):
    return TrustedClientContext(key)


def provision(service, role=UserRole.OWNER, name="test.owner"):
    return service.provision_identity(username=name, display_name="Fictional test user", role=role, password=PASSWORD)


def enroll(service, clock, user):
    connection = client(str(user.user_id))
    challenge = service.begin_login(user.username, PASSWORD, client=connection)
    token = challenge.token.get_secret_value()
    enrollment = service.begin_mfa_enrollment(token)
    secret = enrollment.secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(clock.value), client=connection)
    return secret, grant


def login(service, clock, user, secret, *, recovery_code=None):
    connection = client(str(user.user_id))
    challenge = service.begin_login(user.username, PASSWORD, client=connection)
    kwargs = {"recovery_code": recovery_code} if recovery_code else {"totp_code": pyotp.TOTP(secret).at(clock.value)}
    return service.complete_login(challenge.token.get_secret_value(), client=connection, **kwargs)


def reset_request(user, **changes):
    values = dict(target_user_id=user.user_id, expected_auth_version=user.auth_version,
                  request_id=uuid4(), identity_verification_reference="fictional-case-123",
                  reason="Test operator verified identity separately")
    values.update(changes)
    return MfaResetRequest(**values)


def rows(repository, table):
    assert table in {"auth_users", "auth_sessions", "auth_challenges", "auth_enrollments",
                     "auth_recovery_codes", "auth_events", "auth_notification_outbox", "auth_reset_requests"}
    with repository._connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def install_old_password_hash(repository, user):
    # Deliberately obsolete parameters only for this fictional test fixture.
    old_hash = PasswordHasher(type=Type.ID, memory_cost=8192, time_cost=1, parallelism=1).hash(PASSWORD)
    with repository.transaction() as connection:
        connection.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?", (old_hash, str(user.user_id)))
    return old_hash


def test_successful_password_rehash_preserves_mfa_and_sessions(setup):
    service, repository, clock = setup
    user = provision(service)
    secret, enrolled = enroll(service, clock, user)
    old_hash = install_old_password_hash(repository, user)
    before = rows(repository, "auth_users")[0]
    sessions = rows(repository, "auth_sessions")
    codes = rows(repository, "auth_recovery_codes")
    challenge = service.begin_login(user.username, PASSWORD, client=client())
    current = rows(repository, "auth_users")[0]
    assert current["password_hash"] != old_hash
    assert service.passwords.verify(current["password_hash"], PASSWORD)
    assert not service.passwords.check_needs_rehash(current["password_hash"])
    assert {**current, "password_hash": old_hash} == before
    assert rows(repository, "auth_sessions") == sessions
    assert rows(repository, "auth_recovery_codes") == codes
    assert not challenge.requires_enrollment
    assert len([event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]) == 1
    # Password success grants a challenge, never a session; the original MFA is required.
    with pytest.raises(AuthenticationError):
        service.require_session(challenge.token.get_secret_value())
    assert service.require_session(enrolled.session.token.get_secret_value()).mfa_enabled
    clock.advance(30)
    grant = service.complete_login(challenge.token.get_secret_value(), totp_code=pyotp.TOTP(secret).at(clock.value), client=client())
    assert grant.session.auth_version == before["auth_version"]


def test_wrong_password_does_not_check_or_rehash(setup, monkeypatch):
    service, repository, _ = setup
    user = provision(service)
    old_hash = install_old_password_hash(repository, user)
    def unexpected(*args):
        pytest.fail("incorrect password must not trigger rehash")
    monkeypatch.setattr(service.passwords, "check_needs_rehash", unexpected)
    monkeypatch.setattr(service.passwords, "hash_password", unexpected)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, "Wrong fictional test passphrase", client=client())
    assert rows(repository, "auth_users")[0]["password_hash"] == old_hash
    assert not rows(repository, "auth_challenges")
    assert not rows(repository, "auth_sessions")
    assert not [event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]


def test_current_password_hash_is_not_rewritten(setup, monkeypatch):
    service, repository, _ = setup
    user = provision(service)
    before = rows(repository, "auth_users")
    def unexpected(*args):
        pytest.fail("current parameters must not trigger hashing")
    monkeypatch.setattr(service.passwords, "hash_password", unexpected)
    service.begin_login(user.username, PASSWORD, client=client())
    assert rows(repository, "auth_users") == before
    assert not [event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]


def test_concurrent_password_rehash_updates_once_and_reverifies_peer(setup, monkeypatch):
    service, repository, _ = setup
    user = provision(service)
    old_hash = install_old_password_hash(repository, user)
    barrier = Barrier(2)
    original_verify = service.passwords.verify
    def racing_verify(stored_hash, password):
        result = original_verify(stored_hash, password)
        if stored_hash == old_hash:
            barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(service.passwords, "verify", racing_verify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        challenges = list(pool.map(lambda key: service.begin_login(user.username, PASSWORD, client=client(key)), ("peer-a", "peer-b")))
    assert len({challenge.token.get_secret_value() for challenge in challenges}) == 2
    assert len(rows(repository, "auth_challenges")) == 2
    assert not rows(repository, "auth_sessions")
    current = rows(repository, "auth_users")[0]
    assert current["auth_version"] == user.auth_version
    assert not service.passwords.check_needs_rehash(current["password_hash"])
    assert len([event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]) == 1


def test_rehash_cannot_overwrite_concurrent_password_change(setup, monkeypatch):
    service, repository, _ = setup
    user = provision(service)
    install_old_password_hash(repository, user)
    new_password = "Another fictional changed password"
    original_hash = service.passwords.hash_password
    new_hash = original_hash(new_password)
    def racing_hash(password):
        result = original_hash(password)
        with repository.transaction() as connection:
            # Even a writer forgetting to increment auth_version cannot be overwritten.
            connection.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?", (new_hash, str(user.user_id)))
        return result
    monkeypatch.setattr(service.passwords, "hash_password", racing_hash)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, PASSWORD, client=client())
    assert rows(repository, "auth_users")[0]["password_hash"] == new_hash
    assert not rows(repository, "auth_challenges")
    assert not [event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]


def test_password_hash_race_retry_is_bounded(setup, monkeypatch):
    service, repository, _ = setup
    user = provision(service)
    original_verify = service.passwords.verify
    calls = []
    def racing_verify(stored_hash, password):
        result = original_verify(stored_hash, password)
        calls.append(stored_hash)
        with repository.transaction() as connection:
            connection.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?", (service.passwords.hash_password(PASSWORD), str(user.user_id)))
        return result
    monkeypatch.setattr(service.passwords, "verify", racing_verify)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, PASSWORD, client=client())
    assert len(calls) == 2
    assert not rows(repository, "auth_challenges")


@pytest.mark.parametrize("failure", ["challenge", "rehash_audit", "password_audit"])
def test_password_rehash_and_challenge_roll_back_together(setup, failure):
    service, repository, _ = setup
    user = provision(service)
    install_old_password_hash(repository, user)
    before = {table: rows(repository, table) for table in ("auth_users", "auth_events", "auth_challenges", "auth_sessions")}
    with repository.transaction() as connection:
        if failure == "challenge":
            connection.execute("CREATE TRIGGER fail_rehash BEFORE INSERT ON auth_challenges BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        else:
            event = "PASSWORD_REHASHED" if failure == "rehash_audit" else "PASSWORD_VERIFIED"
            connection.execute(f"CREATE TRIGGER fail_rehash BEFORE INSERT ON auth_events WHEN NEW.event_type='{event}' BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.begin_login(user.username, PASSWORD, client=client())
    assert {table: rows(repository, table) for table in before} == before
    # Attempt reservations are intentionally committed even when the later write fails.
    with repository.transaction() as connection:
        connection.execute("DROP TRIGGER fail_rehash")
    service.begin_login(user.username, PASSWORD, client=client())
    assert not service.passwords.check_needs_rehash(rows(repository, "auth_users")[0]["password_hash"])


@pytest.mark.parametrize("role,business", [(UserRole.OWNER, True), (UserRole.SERVICE_MANAGER, True), (UserRole.IT_ADMIN, False)])
def test_personal_accounts_roles_and_mfa_before_session(setup, role, business):
    service, repository, clock = setup
    user = provision(service, role)
    assert user.kind == IdentityKind.HUMAN and not user.mfa_enabled
    assert not user.can_approve_labour
    challenge = service.begin_login(user.username, PASSWORD, client=client())
    assert challenge.requires_enrollment
    assert not rows(repository, "auth_sessions")
    with pytest.raises(AuthenticationError):
        service.require_session(challenge.token.get_secret_value())
    secret, enrolled = enroll(service, clock, user)
    current = service.require_session(enrolled.session.token.get_secret_value())
    assert current.can_approve_labour is business
    assert current.mfa_enabled
    assert enrolled.session.session.mfa_verified_at.tzinfo is not None
    assert len(enrolled.recovery_codes) == 10
    serialized = json.dumps(rows(repository, "auth_users") + rows(repository, "auth_sessions") + rows(repository, "auth_enrollments") + rows(repository, "auth_recovery_codes") + rows(repository, "auth_events"))
    assert secret not in serialized and PASSWORD not in serialized
    assert enrolled.session.token.get_secret_value() not in serialized
    assert all(code.get_secret_value() not in serialized for code in enrolled.recovery_codes)
    assert "password_hash" not in current.model_dump()
    assert secret not in repr(enrolled) and enrolled.session.token.get_secret_value() not in repr(enrolled)


def test_service_identity_cannot_login_or_receive_human_password(setup):
    service, repository, _ = setup
    identity = service.provision_identity(username="test.openclaw", display_name="Fictional service", role=UserRole.OPENCLAW)
    assert identity.kind == IdentityKind.SERVICE and not identity.can_approve_labour
    with pytest.raises(ValueError):
        service.provision_identity(username="test.agent2", display_name="Fictional service", role=UserRole.OPENCLAW, password=PASSWORD)
    with pytest.raises(AuthenticationError):
        service.begin_login(identity.username, PASSWORD, client=client())
    assert not rows(repository, "auth_sessions") and not rows(repository, "auth_challenges")


@pytest.mark.parametrize("username,password", [("unknown.test", PASSWORD), ("test.owner", "Wrong fictional passphrase"), ("' OR 1=1 --", PASSWORD)])
def test_bad_password_unknown_account_and_sql_input_are_rejected(setup, username, password):
    service, repository, _ = setup
    provision(service)
    with pytest.raises(AuthenticationError, match="authentication failed"):
        service.begin_login(username, password, client=client())
    assert not rows(repository, "auth_challenges")
    assert rows(repository, "auth_events")[-1]["event_type"] == "PASSWORD_REJECTED"
    assert PASSWORD not in rows(repository, "auth_events")[-1]["details_json"]


def test_pending_enrollment_is_idempotent_and_requires_possession_proof(setup):
    service, repository, clock = setup
    user = provision(service)
    challenge = service.begin_login(user.username, PASSWORD, client=client())
    token = challenge.token.get_secret_value()
    first = service.begin_mfa_enrollment(token)
    assert service.begin_mfa_enrollment(token).secret == first.secret
    with pytest.raises(AuthenticationError):
        service.confirm_mfa_enrollment(token, "invalid", client=client())
    assert not rows(repository, "auth_sessions")
    secret = first.secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(clock.value), client=client())
    with pytest.raises(AuthenticationError):
        service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(clock.value), client=client())
    assert len(rows(repository, "auth_sessions")) == 1
    assert service.require_session(grant.session.token.get_secret_value()).mfa_enabled


def test_password_then_totp_login_and_global_replay_protection(setup):
    service, repository, clock = setup
    user = provision(service)
    secret, enrolled = enroll(service, clock, user)
    # Enrollment already consumed this TOTP step, including across new challenges.
    with pytest.raises(AuthenticationError):
        login(service, clock, user, secret)
    clock.advance(30)
    grant = login(service, clock, user, secret)
    assert service.require_session(grant.token.get_secret_value()).user_id == user.user_id
    with pytest.raises(AuthenticationError):
        login(service, clock, user, secret)
    assert len(rows(repository, "auth_sessions")) == 2


def test_recovery_code_is_one_use_after_password_and_survives_restart(setup):
    service, repository, clock = setup
    user = provision(service)
    secret, enrolled = enroll(service, clock, user)
    code = enrolled.recovery_codes[0].get_secret_value()
    restarted = AuthService(repository, service.cipher, passwords=service.passwords, clock=clock)
    grant = login(restarted, clock, user, secret, recovery_code=code)
    assert grant.session.factor == "RECOVERY"
    with pytest.raises(AuthenticationError):
        login(restarted, clock, user, secret, recovery_code=code)
    assert sum(row["consumed_at"] is not None for row in rows(repository, "auth_recovery_codes")) == 1


@pytest.mark.parametrize("factor", ["TOTP", "RECOVERY"])
def test_concurrent_second_factor_is_consumed_once(setup, factor):
    service, repository, clock = setup
    user = provision(service)
    secret, enrolled = enroll(service, clock, user)
    clock.advance(30)
    tokens = [service.begin_login(user.username, PASSWORD, client=client(str(user.user_id))).token.get_secret_value() for _ in range(2)]
    kwargs = {"totp_code": pyotp.TOTP(secret).at(clock.value)} if factor == "TOTP" else {"recovery_code": enrolled.recovery_codes[0].get_secret_value()}
    barrier = Barrier(2)
    def run(token):
        barrier.wait()
        try:
            service.complete_login(token, client=client(str(user.user_id)), **kwargs)
            return "granted"
        except AuthenticationError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, tokens)) == ["granted", "rejected"]
    assert len(rows(repository, "auth_sessions")) == 2


@pytest.mark.parametrize("factor", ["TOTP", "RECOVERY"])
def test_login_rollback_preserves_factor_and_challenge_for_retry(setup, factor):
    service, repository, clock = setup
    user = provision(service)
    secret, enrolled = enroll(service, clock, user)
    clock.advance(30)
    token = service.begin_login(user.username, PASSWORD, client=client(str(user.user_id))).token.get_secret_value()
    kwargs = {"totp_code": pyotp.TOTP(secret).at(clock.value)} if factor == "TOTP" else {"recovery_code": enrolled.recovery_codes[0].get_secret_value()}
    tables = ("auth_users", "auth_sessions", "auth_challenges", "auth_recovery_codes", "auth_events")
    before = {table: rows(repository, table) for table in tables}
    with repository.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_session BEFORE INSERT ON auth_sessions BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.complete_login(token, client=client(str(user.user_id)), **kwargs)
    assert {table: rows(repository, table) for table in tables} == before
    with repository.transaction() as connection:
        connection.execute("DROP TRIGGER fail_session")
    grant = service.complete_login(token, client=client(str(user.user_id)), **kwargs)
    assert service.require_session(grant.token.get_secret_value()).user_id == user.user_id


def test_enrollment_rollback_does_not_activate_mfa_or_issue_codes(setup):
    service, repository, clock = setup
    user = provision(service)
    token = service.begin_login(user.username, PASSWORD, client=client()).token.get_secret_value()
    enrollment = service.begin_mfa_enrollment(token)
    with repository.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_session BEFORE INSERT ON auth_sessions BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.confirm_mfa_enrollment(token, pyotp.TOTP(enrollment.secret.get_secret_value()).at(clock.value), client=client())
    assert rows(repository, "auth_users")[0]["mfa_enabled"] == 0
    assert not rows(repository, "auth_sessions") and not rows(repository, "auth_recovery_codes")
    assert len(rows(repository, "auth_enrollments")) == 1


@pytest.mark.parametrize("expiry", ["challenge", "idle", "absolute"])
def test_challenges_and_sessions_expire(setup, expiry):
    service, repository, clock = setup
    user = provision(service)
    if expiry == "challenge":
        challenge = service.begin_login(user.username, PASSWORD, client=client())
        clock.advance(300)
        with pytest.raises(AuthenticationError):
            service.begin_mfa_enrollment(challenge.token.get_secret_value())
        assert not rows(repository, "auth_sessions")
    else:
        _, grant = enroll(service, clock, user)
        token = grant.session.token.get_secret_value()
        if expiry == "absolute":
            for _ in range(16):
                clock.advance(1799)
                service.require_session(token)
            clock.advance(16)
        else:
            clock.advance(1800)
        with pytest.raises(AuthenticationError):
            service.require_session(token)


@pytest.mark.parametrize("operation", ["revoke", "disable", "version"])
def test_server_can_invalidate_sessions(setup, operation):
    service, repository, clock = setup
    user = provision(service)
    _, grant = enroll(service, clock, user)
    token = grant.session.token.get_secret_value()
    if operation == "revoke":
        service.revoke_session(token)
    else:
        with repository.transaction() as connection:
            if operation == "disable":
                connection.execute("UPDATE auth_users SET enabled=0 WHERE user_id=?", (str(user.user_id),))
            else:
                connection.execute("UPDATE auth_users SET auth_version=auth_version+1 WHERE user_id=?", (str(user.user_id),))
    with pytest.raises(AuthenticationError):
        service.require_session(token)


def test_persistent_rate_limits_cover_password_and_mfa(setup):
    service, repository, clock = setup
    user = provision(service)
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            service.begin_login(user.username, "Wrong fictional password", client=client())
    restarted = AuthService(repository, service.cipher, passwords=service.passwords, clock=clock)
    with pytest.raises(RateLimitError):
        restarted.begin_login(user.username, PASSWORD, client=client("different-client"))
    assert rows(repository, "auth_events")[-1]["event_type"] == "AUTH_RATE_LIMITED"
    clock.advance(300)
    challenge = restarted.begin_login(user.username, PASSWORD, client=client())
    restarted.begin_mfa_enrollment(challenge.token.get_secret_value())
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            restarted.confirm_mfa_enrollment(challenge.token.get_secret_value(), "invalid", client=client())
    with pytest.raises(RateLimitError):
        restarted.confirm_mfa_enrollment(challenge.token.get_secret_value(), "invalid", client=client("another-client"))


def test_global_limit_cannot_be_bypassed_by_rotating_unknown_accounts_and_clients(setup):
    original, repository, clock = setup
    service = AuthService(repository, original.cipher, passwords=original.passwords, clock=clock,
                          policy=AuthPolicy(individual_attempts=3, global_attempts=3))
    for index in range(3):
        with pytest.raises(AuthenticationError):
            service.begin_login(f"unknown{index}", PASSWORD, client=client(f"connection{index}"))
    with pytest.raises(RateLimitError):
        service.begin_login("new-unknown", PASSWORD, client=client("new-connection"))
    assert not rows(repository, "auth_challenges")


def prepared_reset(setup):
    service, repository, clock = setup
    target = provision(service)
    secret, target_grant = enroll(service, clock, target)
    admin = provision(service, UserRole.IT_ADMIN, "test.admin")
    _, admin_grant = enroll(service, clock, admin)
    return service, repository, clock, target, secret, target_grant, admin_grant


def test_admin_reset_revokes_all_state_queues_notice_and_requires_reenrollment(setup):
    service, repository, clock, user, secret, target, admin = prepared_reset(setup)
    pending = service.begin_login(user.username, PASSWORD, client=client(str(user.user_id)))
    result = service.reset_user_mfa(admin.session.token.get_secret_value(), reset_request(user))
    assert result.auth_version == 2
    with pytest.raises(AuthenticationError):
        service.require_session(target.session.token.get_secret_value())
    with pytest.raises(AuthenticationError):
        service.complete_login(pending.token.get_secret_value(), client=client(), recovery_code=target.recovery_codes[0].get_secret_value())
    assert not [row for row in rows(repository, "auth_recovery_codes") if row["user_id"] == str(user.user_id)]
    target_row = next(row for row in rows(repository, "auth_users") if row["user_id"] == str(user.user_id))
    assert target_row["totp_ciphertext"] is None and target_row["mfa_enabled"] == 0
    assert service.passwords.verify(target_row["password_hash"], PASSWORD)
    assert len(rows(repository, "auth_notification_outbox")) == 1
    assert rows(repository, "auth_notification_outbox")[0]["user_id"] == str(user.user_id)
    assert rows(repository, "auth_notification_outbox")[0]["delivered_at"] is None
    assert not service.require_session(admin.session.token.get_secret_value()).can_approve_labour
    # The password alone no longer opens enrollment; a bound recovery grant is required
    # (webapp.user_mfa_recovery, covered in tests/test_webapp_user_mfa_recovery.py).
    assert target_row["mfa_recovery_required"] == 1
    clock.advance(300)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, PASSWORD, client=client(str(user.user_id)))
    assert not [row for row in rows(repository, "auth_challenges") if row["consumed_at"] is None]


@pytest.mark.parametrize("identity", ["owner", "manager", "fake", "admin_self", "service", "stale_admin", "revoked_admin"])
def test_reset_authorization_rejects_business_accounts_and_forgery(setup, identity):
    service, repository, clock, target, secret, target_grant, admin = prepared_reset(setup)
    token = admin.session.token.get_secret_value()
    request = reset_request(target)
    if identity == "owner":
        token = target_grant.session.token.get_secret_value()
    elif identity == "manager":
        manager = provision(service, UserRole.SERVICE_MANAGER, "test.manager")
        _, grant = enroll(service, clock, manager)
        token = grant.session.token.get_secret_value()
    elif identity == "fake":
        token = "pretend-IT_ADMIN"
    elif identity == "admin_self":
        request = reset_request(admin.session.user)
    elif identity == "service":
        agent = service.provision_identity(username="test.openclaw", display_name="Fictional service", role=UserRole.OPENCLAW)
        request = reset_request(agent)
    elif identity == "stale_admin":
        clock.advance(300)
    else:
        service.revoke_session(token)
    with pytest.raises(AuthenticationError):
        service.reset_user_mfa(token, request)
    assert not rows(repository, "auth_reset_requests")
    assert not rows(repository, "auth_notification_outbox")
    assert next(row for row in rows(repository, "auth_users") if row["user_id"] == str(target.user_id))["mfa_enabled"] == 1


def test_reset_idempotence_and_conflicting_payload(setup):
    service, repository, _, user, _, _, admin = prepared_reset(setup)
    request, token = reset_request(user), admin.session.token.get_secret_value()
    first = service.reset_user_mfa(token, request)
    assert service.reset_user_mfa(token, request) == first
    with pytest.raises(AuthIdempotencyConflictError):
        service.reset_user_mfa(token, request.model_copy(update={"reason": "different reason"}))
    assert len(rows(repository, "auth_reset_requests")) == len(rows(repository, "auth_notification_outbox")) == 1
    assert len([row for row in rows(repository, "auth_events") if row["event_type"] == "MFA_RESET"]) == 1


def test_reset_retry_after_recovery_enrollment_is_historical_and_does_not_revoke_it(setup):
    service, repository, clock, user, _, _, admin = prepared_reset(setup)
    request, token = reset_request(user), admin.session.token.get_secret_value()
    first = service.reset_user_mfa(token, request)
    clock.advance(30)
    state = start_user_mfa_recovery(service, token, {
        "target_user_id": str(user.user_id), "expected_auth_version": first.auth_version, "request_id": str(uuid4()),
        "identity_verification_reference": "fictional-case-124", "verified_at": clock.value, "reason": "Fictional"})
    grant = state.grant_token.get_secret_value()
    secret = begin_user_recovery_enrollment(service, grant, PASSWORD, client=client()).secret.get_secret_value()
    complete_user_mfa_recovery(service, grant, pyotp.TOTP(secret).at(clock.value), client=client())
    clock.advance(30)
    new = login(service, clock, user, secret)
    assert service.reset_user_mfa(token, request) == first
    assert service.require_session(new.token.get_secret_value()).mfa_enabled
    assert len(rows(repository, "auth_notification_outbox")) == 2  # reset and recovery start; the replay adds none


@pytest.mark.parametrize("old_parameters", [False, True])
def test_reset_during_password_check_invalidates_login_challenge(setup, monkeypatch, old_parameters):
    service, repository, _, user, _, _, admin = prepared_reset(setup)
    if old_parameters:
        install_old_password_hash(repository, user)
    original_verify = service.passwords.verify
    def racing_verify(stored_hash, password):
        valid = original_verify(stored_hash, password)
        service.reset_user_mfa(admin.session.token.get_secret_value(), reset_request(user))
        return valid
    monkeypatch.setattr(service.passwords, "verify", racing_verify)
    with pytest.raises(AuthenticationError):
        service.begin_login(user.username, PASSWORD, client=client(str(user.user_id)))
    assert not [row for row in rows(repository, "auth_challenges") if row["consumed_at"] is None]
    assert not [event for event in rows(repository, "auth_events") if event["event_type"] == "PASSWORD_REHASHED"]


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_reset_requests_are_atomic_and_idempotent(setup, same_key):
    service, repository, _, user, _, _, admin = prepared_reset(setup)
    first = reset_request(user)
    second = first if same_key else reset_request(user)
    barrier = Barrier(2)
    def run(request):
        barrier.wait()
        try:
            return service.reset_user_mfa(admin.session.token.get_secret_value(), request)
        except AuthVersionConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, second)))
    assert results[0] == results[1] if same_key else results.count("conflict") == 1
    assert len(rows(repository, "auth_notification_outbox")) == len(rows(repository, "auth_reset_requests")) == 1


@pytest.mark.parametrize("failure_table", ["auth_events", "auth_notification_outbox", "auth_reset_requests"])
def test_reset_failure_rolls_back_secrets_sessions_audit_and_notification(setup, failure_table):
    service, repository, _, user, _, target, admin = prepared_reset(setup)
    tables = ("auth_users", "auth_sessions", "auth_challenges", "auth_recovery_codes", "auth_events", "auth_notification_outbox", "auth_reset_requests")
    before = {table: rows(repository, table) for table in tables}
    with repository.transaction() as connection:
        connection.execute(f"CREATE TRIGGER inject_failure BEFORE INSERT ON {failure_table} BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.reset_user_mfa(admin.session.token.get_secret_value(), reset_request(user))
    assert {table: rows(repository, table) for table in tables} == before
    assert service.require_session(target.session.token.get_secret_value()).mfa_enabled


@pytest.mark.parametrize("extra", ["reviewer_id", "role", "timestamp", "password", "totp_secret"])
def test_reset_commands_reject_authoritative_browser_fields(setup, extra):
    service, _, _ = setup
    user = provision(service)
    request = reset_request(user).model_dump()
    request[extra] = "untrusted input"
    with pytest.raises(ValueError):
        MfaResetRequest.model_validate(request)


def test_reset_requires_verification_evidence_and_no_customer_notification_destination(setup):
    service, _, _ = setup
    user = provision(service)
    for changes in ({"reason": " "}, {"identity_verification_reference": " "}, {"identity_verification_reference": "x"}, {"identity_verification_reference": " x "}):
        with pytest.raises(ValueError):
            reset_request(user, **changes)


def test_only_approved_auth_routes_and_no_recovery_or_business_context_bridge(tmp_path):
    app = create_app(tmp_path / "work.db")
    assert {route.path for route in app.routes if route.path.startswith('/api/')} == {
        "/api/works", "/api/works/{work_id}", "/api/works/{work_id}/labour",
        "/api/auth/csrf", "/api/auth/login", "/api/auth/mfa", "/api/auth/session", "/api/auth/logout",
        "/api/drafts", "/api/drafts/{draft_id}", "/api/drafts/{draft_id}/discard",
    }
    with TestClient(app, base_url="https://testserver") as http:
        assert http.post("/api/auth/login").status_code == 403
        assert http.post("/api/auth/reset").status_code == 404
        assert http.get("/api/works").status_code == 401  # no anonymous business access
