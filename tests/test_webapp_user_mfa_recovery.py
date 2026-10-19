"""Business-user MFA recovery bound to a one-time grant: fictional accounts, isolated databases."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet

from webapp.auth_crypto import PasswordService, TotpCipher
from webapp.auth_models import MfaResetRequest, UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import (
    AuthIdempotencyConflictError, AuthService, AuthenticationError, AuthVersionConflictError,
    PermissionDeniedError, TrustedClientContext,
)
from webapp.user_mfa_recovery import (
    GRANT_SECONDS, UserRecoveryUnavailable, begin_user_recovery_enrollment, complete_user_mfa_recovery,
    start_user_mfa_recovery,
)


PASSWORD = "Fictional recovery passphrase only 3W"
CONSOLE = TrustedClientContext("user-recovery-console-test")


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


def provision(service, role, name, password=PASSWORD):
    return service.provision_identity(username=name, display_name="Fictional user", role=role,
                                      password=None if role == UserRole.OPENCLAW else password)


def enroll(service, clock, user):
    connection = TrustedClientContext(str(user.user_id))
    challenge = service.begin_login(user.username, PASSWORD, client=connection)
    token = challenge.token.get_secret_value()
    secret = service.begin_mfa_enrollment(token).secret.get_secret_value()
    return secret, service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(clock.value), client=connection)


@pytest.fixture
def world(tmp_path, passwords):
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    clock = TestClock()
    service = AuthService(repository, TotpCipher((Fernet.generate_key(),)), passwords=passwords, clock=clock)
    admin = provision(service, UserRole.IT_ADMIN, "test.admin")
    admin_secret, admin_grant = enroll(service, clock, admin)
    owner = provision(service, UserRole.OWNER, "test.owner")
    owner_secret, owner_grant = enroll(service, clock, owner)
    clock.advance(60)
    return {"service": service, "repository": repository, "clock": clock, "admin": admin,
            "admin_secret": admin_secret, "admin_token": admin_grant.session.token.get_secret_value(),
            "admin_codes": admin_grant.recovery_codes, "owner": owner, "owner_secret": owner_secret,
            "owner_grant": owner_grant}


def rows(world, query, parameters=()):
    with world["repository"]._connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters)]


def user_row(world, user):
    return rows(world, "SELECT * FROM auth_users WHERE user_id=?", (str(user.user_id),))[0]


def request(world, user=None, **changes):
    user = user or world["owner"]
    return {"target_user_id": str(user.user_id), "expected_auth_version": user_row(world, user)["auth_version"],
            "request_id": str(uuid4()), "identity_verification_reference": "Fictional in-person check A",
            "verified_at": world["clock"].value - timedelta(minutes=5), "reason": "Fictional lost phone"} | changes


def start(world, user=None, token=None, **changes):
    return start_user_mfa_recovery(world["service"], token or world["admin_token"], request(world, user, **changes))


def recover(world, state, password=PASSWORD):
    service, clock = world["service"], world["clock"]
    token = state.grant_token.get_secret_value()
    secret = begin_user_recovery_enrollment(service, token, password, client=CONSOLE).secret.get_secret_value()
    return token, secret, complete_user_mfa_recovery(service, token, pyotp.TOTP(secret).at(clock.value), client=CONSOLE)


def events(world, event_type):
    return [json.loads(row["details_json"]) | {"actor": row["actor_user_id"], "subject": row["subject_user_id"]}
            for row in rows(world, "SELECT * FROM auth_events WHERE event_type=? ORDER BY event_id", (event_type,))]


def test_full_recovery_chain_requires_the_grant_password_and_new_totp(world):
    service, clock, owner = world["service"], world["clock"], world["owner"]
    before = user_row(world, owner)
    pending_login = service.begin_login(owner.username, PASSWORD, client=CONSOLE)
    state = start(world)
    assert state.status == "OPEN" and state.grant_token is not None and not state.replayed
    assert state.expires_at == clock.value + timedelta(seconds=GRANT_SECONDS)
    reset = user_row(world, owner)
    assert (reset["mfa_enabled"], reset["mfa_recovery_required"], reset["totp_ciphertext"]) == (0, 1, None)
    assert (reset["role"], reset["password_hash"], reset["enabled"]) == ("OWNER", before["password_hash"], 1)
    assert reset["auth_version"] == before["auth_version"] + 1 == state.recovery_auth_version
    with pytest.raises(AuthenticationError):
        service.require_session(world["owner_grant"].session.token.get_secret_value())
    with pytest.raises(AuthenticationError):
        service.complete_login(pending_login.token.get_secret_value(), client=CONSOLE,
                               recovery_code=world["owner_grant"].recovery_codes[0].get_secret_value())
    challenges_before = rows(world, "SELECT * FROM auth_challenges")
    with pytest.raises(AuthenticationError):  # the password alone never opens enrollment now
        service.begin_login(owner.username, PASSWORD, client=CONSOLE)
    assert rows(world, "SELECT * FROM auth_challenges") == challenges_before
    assert not rows(world, "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(owner.user_id),))

    token, secret, done = recover(world, state)
    assert done.status == "COMPLETED" and len({code.get_secret_value() for code in done.recovery_codes}) == 10
    after = user_row(world, owner)
    assert (after["mfa_enabled"], after["mfa_recovery_required"], after["role"]) == (1, 0, "OWNER")
    assert not rows(world, "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL", (str(owner.user_id),))
    assert rows(world, "SELECT status FROM auth_user_recovery_grants")[0]["status"] == "COMPLETED"
    [started] = events(world, "USER_MFA_RECOVERY_STARTED")
    assert started["actor"] == str(world["admin"].user_id) and started["subject"] == str(owner.user_id)
    assert started["identity_verification_reference"] == "Fictional in-person check A"
    assert started["reason"] == "Fictional lost phone" and started["identity_check_verified_by_program"] is False
    assert started["purpose"] == "MFA_RECOVERY_ENROLLMENT" and started["method"] == "IN_PERSON_ID_DOCUMENT"
    assert events(world, "USER_MFA_RECOVERY_ENROLLMENT_STARTED") and events(world, "USER_MFA_RECOVERY_COMPLETED")
    history = json.dumps(rows(world, "SELECT * FROM auth_events")) + json.dumps(rows(world, "SELECT * FROM auth_user_recovery_grants"))
    assert token not in history and secret not in history and PASSWORD not in history
    assert not any(code.get_secret_value() in history for code in done.recovery_codes)
    outbox = rows(world, "SELECT * FROM auth_notification_outbox")
    assert [(row["message_type"], row["delivered_at"]) for row in outbox] == [("MFA_RESET", None)]

    clock.advance(30)
    session = service.complete_login(service.begin_login(owner.username, PASSWORD, client=CONSOLE).token.get_secret_value(),
                                     client=CONSOLE, totp_code=pyotp.TOTP(secret).at(clock.value))
    assert service.require_session(session.token.get_secret_value()).can_approve_labour
    challenge = service.begin_login(owner.username, PASSWORD, client=CONSOLE)
    assert service.complete_login(challenge.token.get_secret_value(), client=CONSOLE,
                                  recovery_code=done.recovery_codes[0].get_secret_value()).session.factor == "RECOVERY"


def _admin_with_recovery_code_session(world):
    service = world["service"]
    challenge = service.begin_login(world["admin"].username, PASSWORD, client=CONSOLE)
    return service.complete_login(challenge.token.get_secret_value(), client=CONSOLE,
                                  recovery_code=world["admin_codes"][0].get_secret_value()).token.get_secret_value()


@pytest.mark.parametrize("authorization", ["missing", "forged", "owner", "manager", "recovery_code", "stale", "revoked"])
def test_missing_wrong_or_stale_administrator_authorization_is_rejected(world, authorization):
    service, owner = world["service"], world["owner"]
    token = {
        "missing": lambda: "",
        "forged": lambda: "pretend-IT_ADMIN",
        "owner": lambda: world["owner_grant"].session.token.get_secret_value(),
        "manager": lambda: enroll(service, world["clock"], provision(service, UserRole.SERVICE_MANAGER, "test.manager"))[1]
        .session.token.get_secret_value(),
        "recovery_code": lambda: _admin_with_recovery_code_session(world),
        "stale": lambda: world["admin_token"],
        "revoked": lambda: world["admin_token"],
    }[authorization]()
    if authorization == "stale":
        world["clock"].advance(service.policy.admin_mfa_fresh_seconds)
    elif authorization == "revoked":
        service.revoke_session(token)
    before = user_row(world, owner)
    with pytest.raises(AuthenticationError):
        start_user_mfa_recovery(service, token, request(world))
    assert user_row(world, owner) == before and not rows(world, "SELECT * FROM auth_user_recovery_grants")
    assert not rows(world, "SELECT * FROM auth_notification_outbox")
    assert events(world, "USER_MFA_RECOVERY_REJECTED")[-1]["stage"] == "START"


@pytest.mark.parametrize("target", ["it_admin", "other_admin", "openclaw", "unknown", "closed", "stale-version"])
def test_wrong_target_role_kind_state_or_version_is_rejected(world, target):
    service = world["service"]
    error, user = PermissionDeniedError, None
    if target == "it_admin":
        user = world["admin"]
    elif target == "other_admin":
        user = provision(service, UserRole.IT_ADMIN, "test.admin2")
    elif target == "openclaw":
        user = provision(service, UserRole.OPENCLAW, "test.openclaw")
    elif target == "closed":
        user = world["owner"]
        with world["repository"].transaction() as connection:
            connection.execute("UPDATE auth_users SET enabled=0 WHERE user_id=?", (str(user.user_id),))
    elif target == "stale-version":
        user, error = world["owner"], AuthVersionConflictError
    payload = request(world, user or world["owner"])
    if target == "unknown":
        payload["target_user_id"] = str(uuid4())
    elif target == "stale-version":
        payload["expected_auth_version"] += 1
    snapshot = rows(world, "SELECT * FROM auth_users ORDER BY user_id")
    with pytest.raises(error):
        start_user_mfa_recovery(service, world["admin_token"], payload)
    assert rows(world, "SELECT * FROM auth_users ORDER BY user_id") == snapshot
    assert not rows(world, "SELECT * FROM auth_user_recovery_grants")


@pytest.mark.parametrize("change", [
    {"identity_verification_reference": "ab"}, {"identity_verification_reference": "Card 12345678"},
    {"verified_at": datetime(2026, 10, 17, 8, 55)}, {"verified_at": datetime(2026, 10, 17, 10, tzinfo=timezone.utc)},
    {"reason": "  "}, {"method": "REMOTE_VIDEO"}, {"authorizer_user_id": "self-declared"},
])
def test_invalid_request_fields_change_nothing(world, change):
    before = user_row(world, world["owner"])
    with pytest.raises(ValueError):
        start(world, **change)
    assert user_row(world, world["owner"]) == before and not rows(world, "SELECT * FROM auth_user_recovery_grants")


def test_future_identity_check_is_audited_as_a_rejection_without_recovery_side_effects(world):
    owner = world["owner"]
    before, sessions = user_row(world, owner), rows(world, "SELECT * FROM auth_sessions ORDER BY session_id")
    payload = request(world, verified_at=world["clock"].value + timedelta(hours=1))
    with pytest.raises(ValueError, match="future"):
        start_user_mfa_recovery(world["service"], world["admin_token"], payload)
    assert user_row(world, owner) == before and not rows(world, "SELECT * FROM auth_user_recovery_grants")
    assert rows(world, "SELECT * FROM auth_sessions ORDER BY session_id") == sessions
    assert not rows(world, "SELECT * FROM auth_notification_outbox")
    rejected = events(world, "USER_MFA_RECOVERY_REJECTED")
    assert [(e["request_id"], e["stage"], e["actor"], e["subject"]) for e in rejected] == [
        (payload["request_id"], "START", str(world["admin"].user_id), str(owner.user_id))]
    assert not events(world, "USER_MFA_RECOVERY_STARTED")


def test_success_and_accepted_replay_record_no_rejection(world):
    payload = request(world)
    start_user_mfa_recovery(world["service"], world["admin_token"], payload)
    assert start_user_mfa_recovery(world["service"], world["admin_token"], payload).replayed
    assert not events(world, "USER_MFA_RECOVERY_REJECTED")


def test_password_alone_cannot_enroll_after_reset_expiry_or_the_existing_reset_path(world):
    service, clock, owner = world["service"], world["clock"], world["owner"]
    start(world)
    clock.advance(GRANT_SECONDS)
    for _ in range(2):  # neither before nor after the grant expired
        with pytest.raises(AuthenticationError):
            service.begin_login(owner.username, PASSWORD, client=CONSOLE)
    assert events(world, "ENROLLMENT_REQUIRES_RECOVERY_GRANT")
    # The internal reset_user_mfa now leaves the same grant-required state.
    manager = provision(service, UserRole.SERVICE_MANAGER, "test.manager")
    enroll(service, clock, manager)
    clock.advance(30)
    fresh = service.complete_login(service.begin_login(world["admin"].username, PASSWORD, client=CONSOLE).token.get_secret_value(),
                                   client=CONSOLE, totp_code=pyotp.TOTP(world["admin_secret"]).at(clock.value))
    service.reset_user_mfa(fresh.token.get_secret_value(), MfaResetRequest(
        target_user_id=manager.user_id, expected_auth_version=user_row(world, manager)["auth_version"],
        request_id=uuid4(), identity_verification_reference="Fictional case B", reason="Fictional"))
    assert user_row(world, manager)["mfa_recovery_required"] == 1
    with pytest.raises(AuthenticationError):
        service.begin_login(manager.username, PASSWORD, client=CONSOLE)


def test_enrollment_challenge_issued_before_the_reset_is_dead(world):
    service = world["service"]
    newcomer = provision(service, UserRole.SERVICE_MANAGER, "test.newcomer")
    challenge = service.begin_login(newcomer.username, PASSWORD, client=CONSOLE)
    assert challenge.requires_enrollment  # an ordinary first enrollment is still offered
    token = challenge.token.get_secret_value()
    secret = service.begin_mfa_enrollment(token).secret.get_secret_value()
    start(world, newcomer)
    with pytest.raises(AuthenticationError):
        service.begin_mfa_enrollment(token)
    with pytest.raises(AuthenticationError):
        service.confirm_mfa_enrollment(token, pyotp.TOTP(secret).at(world["clock"].value), client=CONSOLE)
    assert user_row(world, newcomer)["mfa_enabled"] == 0


def test_wrong_expired_used_or_other_account_grants_are_rejected(world):
    service, clock = world["service"], world["clock"]
    manager = provision(service, UserRole.SERVICE_MANAGER, "test.manager", password="Other fictional passphrase 8Z")
    enroll_manager = TrustedClientContext("manager")
    challenge = service.begin_login(manager.username, "Other fictional passphrase 8Z", client=enroll_manager)
    secret = service.begin_mfa_enrollment(challenge.token.get_secret_value()).secret.get_secret_value()
    service.confirm_mfa_enrollment(challenge.token.get_secret_value(), pyotp.TOTP(secret).at(clock.value), client=enroll_manager)
    owner_state, manager_state = start(world), start(world, manager)
    owner_token = owner_state.grant_token.get_secret_value()
    for wrong in ("x" * 43, "not-a-token", ""):
        with pytest.raises(AuthenticationError):
            begin_user_recovery_enrollment(service, wrong, PASSWORD, client=TrustedClientContext(f"wrong-{wrong}"))
    # The owner's grant with the manager's password: the grant binds the account.
    with pytest.raises(AuthenticationError):
        begin_user_recovery_enrollment(service, owner_token, "Other fictional passphrase 8Z", client=CONSOLE)
    assert rows(world, "SELECT pending_totp_ciphertext FROM auth_user_recovery_grants WHERE request_id=?",
                (str(owner_state.request_id),))[0]["pending_totp_ciphertext"] is None
    token, _, done = recover(world, owner_state)
    clock.advance(300)  # a new attempt-limit window; the manager's grant is still valid
    with pytest.raises(UserRecoveryUnavailable):  # used
        begin_user_recovery_enrollment(service, token, PASSWORD, client=CONSOLE)
    assert complete_user_mfa_recovery(service, token, "000000", client=CONSOLE).status == "ALREADY_COMPLETED"
    assert complete_user_mfa_recovery(service, token, "000000", client=CONSOLE).recovery_codes == ()
    clock.advance(GRANT_SECONDS)
    with pytest.raises(UserRecoveryUnavailable):  # expired
        begin_user_recovery_enrollment(service, manager_state.grant_token.get_secret_value(),
                                       "Other fictional passphrase 8Z", client=CONSOLE)
    assert rows(world, "SELECT status FROM auth_user_recovery_grants WHERE request_id=?",
                (str(manager_state.request_id),))[0]["status"] == "EXPIRED"
    assert (user_row(world, manager)["mfa_enabled"], user_row(world, manager)["mfa_recovery_required"]) == (0, 1)


def test_new_totp_must_verify_and_repeated_enrollment_replaces_the_pending_secret(world):
    service, clock, owner = world["service"], world["clock"], world["owner"]
    token = start(world).grant_token.get_secret_value()
    first = begin_user_recovery_enrollment(service, token, PASSWORD, client=CONSOLE)
    with pytest.raises(AuthenticationError):
        complete_user_mfa_recovery(service, token, pyotp.TOTP(pyotp.random_base32()).at(clock.value), client=CONSOLE)
    second = begin_user_recovery_enrollment(service, token, PASSWORD, client=CONSOLE)
    assert second.replaced_previous_secret and not first.replaced_previous_secret
    with pytest.raises(AuthenticationError):  # the replaced secret no longer completes
        complete_user_mfa_recovery(service, token, pyotp.TOTP(first.secret.get_secret_value()).at(clock.value), client=CONSOLE)
    assert (user_row(world, owner)["mfa_enabled"], user_row(world, owner)["mfa_recovery_required"]) == (0, 1)
    clock.advance(300)  # a new attempt-limit window; the grant is still valid
    done = complete_user_mfa_recovery(service, token, pyotp.TOTP(second.secret.get_secret_value()).at(clock.value),
                                      client=CONSOLE)
    assert done.status == "COMPLETED"
    with pytest.raises(AuthenticationError):
        begin_user_recovery_enrollment(service, token, "Wrong fictional passphrase 1", client=CONSOLE)


def test_replay_conflict_and_a_new_attempt_supersede_explicitly(world):
    service = world["service"]
    payload = request(world)
    first = start_user_mfa_recovery(service, world["admin_token"], payload)
    replay = start_user_mfa_recovery(service, world["admin_token"], payload)
    assert replay.replayed and replay.grant_token is None and replay.status == "OPEN"
    with pytest.raises(AuthIdempotencyConflictError):
        start_user_mfa_recovery(service, world["admin_token"], payload | {"reason": "Different fictional reason"})
    second = start(world)  # current auth_version, new request ID
    assert second.superseded_request_id == first.request_id
    with pytest.raises(UserRecoveryUnavailable):
        begin_user_recovery_enrollment(service, first.grant_token.get_secret_value(), PASSWORD, client=CONSOLE)
    statuses = {row["request_id"]: row["status"] for row in rows(world, "SELECT * FROM auth_user_recovery_grants")}
    assert statuses == {str(first.request_id): "SUPERSEDED", str(second.request_id): "OPEN"}
    assert recover(world, second)[2].status == "COMPLETED"


def test_concurrent_completions_finish_exactly_once(world):
    service, clock = world["service"], world["clock"]
    token = start(world).grant_token.get_secret_value()
    secret = begin_user_recovery_enrollment(service, token, PASSWORD, client=CONSOLE).secret.get_secret_value()
    code, barrier = pyotp.TOTP(secret).at(clock.value), Barrier(2)

    def run(index):
        barrier.wait()
        return complete_user_mfa_recovery(service, token, code, client=TrustedClientContext(f"console-{index}")).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(run, range(2)))
    assert results == ["ALREADY_COMPLETED", "COMPLETED"]
    assert len(rows(world, "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(world["owner"].user_id),))) == 10
    assert len(events(world, "USER_MFA_RECOVERY_COMPLETED")) == 1


def test_failure_before_commit_rolls_back_the_whole_start(world):
    owner = world["owner"]
    tables = ("auth_users", "auth_sessions", "auth_recovery_codes", "auth_user_recovery_grants", "auth_notification_outbox")
    before = {table: rows(world, f"SELECT * FROM {table}") for table in tables}
    with world["repository"].transaction() as connection:
        connection.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON auth_notification_outbox "
                           "BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        start(world)
    assert {table: rows(world, f"SELECT * FROM {table}") for table in tables} == before
    assert world["service"].require_session(world["owner_grant"].session.token.get_secret_value())
    assert user_row(world, owner)["mfa_recovery_required"] == 0


def test_ordinary_first_enrollment_still_works_and_needs_no_grant(world):
    service, clock = world["service"], world["clock"]
    newcomer = provision(service, UserRole.OWNER, "test.newcomer")
    _, grant = enroll(service, clock, newcomer)
    assert service.require_session(grant.session.token.get_secret_value()).mfa_enabled
    assert user_row(world, newcomer)["mfa_recovery_required"] == 0
    with world["repository"].transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE auth_users SET mfa_recovery_required=1 WHERE user_id=?", (str(newcomer.user_id),))


def test_schema_v4_is_additive_and_keeps_legacy_reset_accounts_grant_bound(world, tmp_path):
    assert [row["version"] for row in rows(world, "SELECT version FROM auth_schema_migrations ORDER BY version")] == [1, 2, 3, 4]
    legacy = AuthRepository(tmp_path / "legacy.db")
    legacy.initialize()
    reset_id, fresh_id = str(uuid4()), str(uuid4())
    password_hash = user_row(world, world["owner"])["password_hash"]
    with legacy.transaction() as connection:  # emulate a version-3 store holding a reset account
        for trigger in ("auth_users_recovery_state_insert", "auth_users_recovery_state_update"):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE auth_user_recovery_grants")
        connection.execute("ALTER TABLE auth_users DROP COLUMN mfa_recovery_required")
        connection.execute("DELETE FROM auth_schema_migrations WHERE version=4")
        for user_id, name in ((reset_id, "legacy.reset"), (fresh_id, "legacy.fresh")):
            connection.execute("INSERT INTO auth_users(user_id,username,display_name,kind,role,password_hash,created_at) "
                               "VALUES (?,?,?,?,?,?,1)", (user_id, name, "Fictional", "HUMAN", "OWNER", password_hash))
        connection.execute("INSERT INTO auth_events(event_type,subject_user_id,occurred_at,details_json) "
                           "VALUES ('MFA_RESET',?,1,'{}')", (reset_id,))
    legacy.initialize()
    with legacy._connect() as connection:
        flags = dict(connection.execute("SELECT user_id,mfa_recovery_required FROM auth_users").fetchall())
        versions = [row[0] for row in connection.execute("SELECT version FROM auth_schema_migrations ORDER BY version")]
    assert flags == {reset_id: 1, fresh_id: 0} and versions == [1, 2, 3, 4]
    legacy.initialize()  # repeatable
