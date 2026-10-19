"""Business-user MFA recovery console: synthetic accounts and isolated databases only.

The console seams of webapp.admin_recovery_cli are scripted; shown one-time values
are kept in memory and the fixture asserts that no known secret reached stdout/stderr.
"""

import signal
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet

import webapp.admin_recovery_cli as console
import webapp.user_recovery_cli as cli
from webapp.auth_crypto import PasswordService, TotpCipher
from webapp.auth_models import UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService, TrustedClientContext


ADMIN_PASSWORD = "Fictional administrator passphrase 5M"
OWNER_PASSWORD = "Fictional owner passphrase only 6N"


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeConsole:
    def __init__(self, monkeypatch):
        self.interactive, self.answers, self.secret_inputs, self.shown = True, [], [], []
        monkeypatch.setattr(console, "_interactive", lambda: self.interactive)
        monkeypatch.setattr(console, "_hidden_input_available", lambda: True)
        monkeypatch.setattr(console, "_ask", self._next(self.answers))
        monkeypatch.setattr(console, "_ask_secret", self._next(self.secret_inputs))
        monkeypatch.setattr(console, "_show_secret", lambda label, value: self.shown.append((label, value)))

    @staticmethod
    def _next(queue):
        def read(prompt):
            item = queue.pop(0)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item() if callable(item) else item
        return read

    def value(self, label):
        return [value for shown_label, value in self.shown if shown_label == label][-1]


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


def _enroll(service, clock, user, password):
    connection = TrustedClientContext(f"fixture-{user.username}")
    challenge = service.begin_login(user.username, password, client=connection)
    secret = service.begin_mfa_enrollment(challenge.token.get_secret_value()).secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(challenge.token.get_secret_value(), pyotp.TOTP(secret).at(clock.value),
                                           client=connection)
    return secret, grant


@pytest.fixture
def world(tmp_path, monkeypatch, passwords, capsys):
    key = Fernet.generate_key()
    monkeypatch.setenv("CARBIT_TOTP_KEYS", key.decode("ascii"))
    clock = TestClock()
    monkeypatch.setattr(console, "_clock", clock)
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    service = AuthService(repository, TotpCipher((key,)), passwords=passwords, clock=clock)
    admin = service.provision_identity(username="console.admin", display_name="Fictional admin",
                                       role=UserRole.IT_ADMIN, password=ADMIN_PASSWORD)
    owner = service.provision_identity(username="console.owner", display_name="Fictional owner",
                                       role=UserRole.OWNER, password=OWNER_PASSWORD)
    admin_secret, admin_grant = _enroll(service, clock, admin, ADMIN_PASSWORD)
    owner_secret, owner_grant = _enroll(service, clock, owner, OWNER_PASSWORD)
    clock.advance(60)
    world = {"path": repository.database_path, "repository": repository, "service": service, "clock": clock,
             "admin": admin, "owner": owner, "admin_secret": admin_secret, "owner_grant": owner_grant,
             "console": FakeConsole(monkeypatch), "capsys": capsys, "output": [],
             "secrets": [ADMIN_PASSWORD, OWNER_PASSWORD, admin_secret, owner_secret, key.decode("ascii"),
                         admin_grant.session.token.get_secret_value(), owner_grant.session.token.get_secret_value()]}
    yield world
    read(world)
    known = world["secrets"] + [value for _, value in world["console"].shown]
    text = "".join(world["output"])
    assert not any(value and value in text for value in known), "secret reached console output"


def read(world):
    captured = world["capsys"].readouterr()
    world["output"].extend([captured.out, captured.err])
    return captured


def rows(world, query, parameters=()):
    with world["repository"]._connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters)]


def account(world, user=None):
    return rows(world, "SELECT * FROM auth_users WHERE user_id=?", (str((user or world["owner"]).user_id),))[0]


def grants(world):
    return rows(world, "SELECT * FROM auth_user_recovery_grants ORDER BY created_at, rowid")


def active_sessions(world, user):
    return rows(world, "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL", (str(user.user_id),))


def start_args(world, *, user=None, request_id=None, **overrides):
    user = user or world["owner"]
    values = {"--auth-database": str(world["path"]), "--target-user-id": str(user.user_id),
              "--expected-auth-version": str(account(world, user)["auth_version"]),
              "--request-id": str(request_id or uuid4()),
              "--identity-verification-reference": "Fictional in-person check 1",
              "--verified-at": (world["clock"].value - timedelta(minutes=5)).isoformat(),
              "--reason": "Fictional lost phone"} | overrides
    return ["start", *[item for pair in values.items() for item in pair]]


def start(world, *, admin_user="console.admin", admin_password=ADMIN_PASSWORD, answer=console.CONFIRMATION, **kwargs):
    fake, clock = world["console"], world["clock"]
    clock.advance(30)  # every console sign-in uses a fresh TOTP period
    fake.answers[:] = [answer, admin_user]
    fake.secret_inputs[:] = [admin_password, lambda: pyotp.TOTP(world["admin_secret"]).at(clock.value)]
    try:
        return cli.main(start_args(world, **kwargs))
    finally:
        fake.answers.clear()
        fake.secret_inputs.clear()


def enroll(world, token, *, password=OWNER_PASSWORD, code=None):
    fake, clock = world["console"], world["clock"]
    world["secrets"].append(token)
    fake.secret_inputs[:] = [token, password, code or (lambda: pyotp.TOTP(fake.value("TOTP secret")).at(clock.value))]
    try:
        return cli.main(["enroll", "--auth-database", str(world["path"])])
    finally:
        fake.secret_inputs.clear()


def test_full_console_chain_resets_binds_enrollment_and_revokes_the_admin_session(world):
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id", str(world["owner"].user_id)]) == 0
    assert "recovery grant required False" in read(world).out
    assert start(world) == cli.EXIT_OK
    token = world["console"].value("Recovery authorization token")
    assert (account(world)["mfa_enabled"], account(world)["mfa_recovery_required"]) == (0, 1)
    assert len(active_sessions(world, world["admin"])) == 1  # the fixture's; the console session was revoked
    assert len(rows(world, "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NOT NULL",
                    (str(world["admin"].user_id),))) == 1
    assert not active_sessions(world, world["owner"])
    assert "operator does not need it" in read(world).out
    assert enroll(world, token) == cli.EXIT_OK
    assert [label for label, _ in world["console"].shown if label.startswith("Recovery code")] == [
        f"Recovery code {index}" for index in range(1, 11)]
    assert (account(world)["mfa_enabled"], account(world)["mfa_recovery_required"]) == (1, 0)
    assert not active_sessions(world, world["owner"]) and grants(world)[0]["status"] == "COMPLETED"
    output = read(world).out
    assert "no login session was created" in output and "no notification is delivered" in output


@pytest.mark.parametrize("case", ["wrong-password", "owner-as-authorizer", "it-admin-target", "stale-version"])
def test_rejected_authorization_or_target_recovers_nothing(world, case):
    before = account(world)
    kwargs = {"wrong-password": {"admin_password": "Wrong fictional passphrase 1"},
              "owner-as-authorizer": {"admin_user": "console.owner", "admin_password": OWNER_PASSWORD},
              "it-admin-target": {"user": world["admin"]},
              "stale-version": {"--expected-auth-version": str(before["auth_version"] + 1)}}[case]
    if case == "owner-as-authorizer":  # the owner's own valid TOTP, so only the role decides
        world["clock"].advance(30)
        world["console"].answers[:] = [console.CONFIRMATION, "console.owner"]
        world["console"].secret_inputs[:] = [OWNER_PASSWORD, lambda: pyotp.TOTP(
            world["secrets"][3]).at(world["clock"].value)]
        result = cli.main(start_args(world))
    else:
        result = start(world, **kwargs)
    assert result == cli.EXIT_REJECTED
    # A sign-in with the owner's own TOTP advances only its replay guard (last_totp_step).
    unchanged = lambda row: {key: value for key, value in row.items() if key != "last_totp_step"}
    assert unchanged(account(world)) == unchanged(before) and not grants(world) and not world["console"].shown
    assert len(active_sessions(world, world["admin"])) == 1 and len(active_sessions(world, world["owner"])) == 1


@pytest.mark.parametrize("answer", ["no", "", EOFError, KeyboardInterrupt])
def test_declined_or_interrupted_confirmation_writes_nothing(world, answer):
    before = rows(world, "SELECT * FROM auth_events")
    world["console"].answers[:] = [answer]
    assert cli.main(start_args(world)) == cli.EXIT_INCOMPLETE
    assert rows(world, "SELECT * FROM auth_events") == before and not grants(world)
    assert "nothing was written" in read(world).err


def test_non_interactive_console_refuses_before_writing(world):
    world["console"].interactive = False
    before = rows(world, "SELECT * FROM auth_events")
    for argv in (start_args(world), ["enroll", "--auth-database", str(world["path"])],
                 ["complete", "--auth-database", str(world["path"])]):
        assert cli.main(argv) == cli.EXIT_USAGE
    assert rows(world, "SELECT * FROM auth_events") == before and not world["console"].shown


def test_interrupt_at_the_code_prompt_keeps_the_pending_secret_for_complete(world):
    assert start(world) == cli.EXIT_OK
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token, code=KeyboardInterrupt) == cli.EXIT_INCOMPLETE
    assert "stored as pending" in read(world).err
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id",
                     str(world["owner"].user_id)]) == 0
    assert "pending new TOTP True" in read(world).out
    fake = world["console"]
    fake.secret_inputs[:] = [token, lambda: pyotp.TOTP(fake.value("TOTP secret")).at(world["clock"].value)]
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_OK
    assert account(world)["mfa_enabled"] == 1


def test_wrong_user_password_or_code_keeps_the_grant_required(world):
    assert start(world) == cli.EXIT_OK
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token, password="Wrong fictional passphrase 2") == cli.EXIT_REJECTED
    assert enroll(world, token, code=lambda: pyotp.TOTP(pyotp.random_base32()).at(world["clock"].value)) == cli.EXIT_REJECTED
    assert (account(world)["mfa_enabled"], account(world)["mfa_recovery_required"]) == (0, 1)
    assert "authentication failed" in read(world).err


def test_replay_shows_no_token_and_a_new_attempt_supersedes(world):
    request_id, verified = uuid4(), (world["clock"].value - timedelta(minutes=5)).isoformat()
    same = {"--expected-auth-version": str(account(world)["auth_version"]), "--verified-at": verified}
    assert start(world, request_id=request_id, **same) == cli.EXIT_OK
    first = world["console"].value("Recovery authorization token")
    world["secrets"].append(first)
    world["console"].shown.clear()
    assert start(world, request_id=request_id, **same) == cli.EXIT_INCOMPLETE  # same request and content
    assert "No new token is shown" in read(world).out and not world["console"].shown
    assert start(world) == cli.EXIT_OK
    assert "was superseded" in read(world).out
    assert [row["status"] for row in grants(world)] == ["SUPERSEDED", "OPEN"]
    assert enroll(world, first) == cli.EXIT_REJECTED


def test_output_failure_after_the_committed_start_reports_the_stored_state(world, monkeypatch):
    def failing(label, value):
        raise OSError("closed console")
    monkeypatch.setattr(console, "_show_secret", failing)
    assert start(world) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "was started" in error and "cannot be shown again" in error
    assert grants(world)[0]["status"] == "OPEN" and account(world)["mfa_recovery_required"] == 1
    assert len(active_sessions(world, world["admin"])) == 1


def test_interruption_during_the_start_step_is_reported_as_uncertain(world, monkeypatch):
    real = cli.start_user_mfa_recovery

    def committed_then_interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "start_user_mfa_recovery", committed_then_interrupted)
    assert start(world) == cli.EXIT_INCOMPLETE
    assert "may have committed" in read(world).err and not world["console"].shown
    assert grants(world)[0]["status"] == "OPEN" and len(active_sessions(world, world["admin"])) == 1


def test_interrupt_after_the_committed_sign_in_revokes_exactly_that_session(world, monkeypatch):
    real, stored, handler = AuthService.complete_login, [], signal.getsignal(signal.SIGINT)

    def committed_then_interrupted(self, *args, **kwargs):
        grant = real(self, *args, **kwargs)
        stored.append(str(grant.session.session_id))
        signal.raise_signal(signal.SIGINT)  # Ctrl+C after the session is stored, before the call returns
        return grant
    monkeypatch.setattr(AuthService, "complete_login", committed_then_interrupted)
    others = active_sessions(world, world["admin"])  # the administrator's other session (from the fixture)
    assert start(world) == cli.EXIT_INCOMPLETE
    assert "interrupted at the IT_ADMIN sign-in; nothing was recovered" in read(world).err
    [session_id] = stored
    assert rows(world, "SELECT revoked_at FROM auth_sessions WHERE session_id=?", (session_id,))[0]["revoked_at"]
    assert active_sessions(world, world["admin"]) == others and len(others) == 1
    assert not grants(world) and account(world)["mfa_enabled"] == 1
    assert signal.getsignal(signal.SIGINT) is handler


def test_reusing_the_token_of_a_completed_recovery_gives_a_truthful_next_step(world):
    assert start(world) == cli.EXIT_OK
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token) == cli.EXIT_OK
    read(world)
    shown = list(world["console"].shown)
    assert enroll(world, token) == cli.EXIT_REJECTED
    error = read(world).err
    assert "can no longer be used" in error and "already completed" in error and "log in normally" in error
    assert "still needs" not in error and "ask the IT_ADMIN to check 'status'" in error
    assert world["console"].shown == shown  # nothing new is shown for a used token
    assert (account(world)["mfa_enabled"], account(world)["mfa_recovery_required"]) == (1, 0)


@pytest.mark.parametrize("argv", [
    ["enroll", "--auth-database", "x", "--password", "never-an-argument"],
    ["start-self", "--auth-database", "x"],
    ["cancel", "--auth-database", "x", "--request-id", str(uuid4())],
])
def test_secrets_are_never_arguments_and_admin_offline_commands_are_separate(world, argv):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == cli.EXIT_USAGE
