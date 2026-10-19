"""Offline IT_ADMIN recovery console: synthetic accounts and isolated databases only.

Secrets never reach stdout/stderr in these tests: the console seams collect them
in memory, and leak checks only report booleans.
"""

import getpass
import json
import os
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet

import webapp.admin_offline_recovery as recovery
import webapp.admin_recovery_cli as cli
from webapp.auth_crypto import PasswordService, TotpCipher
from webapp.auth_models import UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService, AuthenticationError, TrustedClientContext
from webapp.database import WorkRepository


PASSWORD = "Fictional console passphrase only 9K"
REAL_INTERACTIVE = cli._interactive
REAL_HIDDEN_INPUT = cli._hidden_input_available
REAL_ASK_SECRET = cli._ask_secret
REAL_SHOW_SECRET = cli._show_secret


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeConsole:
    """Scripted prompts; shown one-time secrets are kept in memory, never printed."""

    def __init__(self, monkeypatch):
        self.interactive, self.answers, self.secret_inputs, self.shown = True, [], [], []
        monkeypatch.setattr(cli, "_interactive", lambda: self.interactive)
        monkeypatch.setattr(cli, "_hidden_input_available", lambda: True)
        monkeypatch.setattr(cli, "_ask", self._next(self.answers))
        monkeypatch.setattr(cli, "_ask_secret", self._next(self.secret_inputs))
        monkeypatch.setattr(cli, "_show_secret", lambda label, value: self.shown.append((label, value)))

    @staticmethod
    def _next(queue):
        def read(prompt):
            item = queue.pop(0)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item() if callable(item) else item
        return read

    def value(self, label):
        """The most recently shown value for a label."""
        return [value for shown_label, value in self.shown if shown_label == label][-1]

    def labels(self):
        return [label for label, _ in self.shown]


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


@pytest.fixture
def world(tmp_path, monkeypatch, passwords, capsys):
    key = Fernet.generate_key()
    monkeypatch.setenv("CARBIT_TOTP_KEYS", key.decode("ascii"))
    clock = TestClock()
    monkeypatch.setattr(cli, "_clock", clock)
    repository = AuthRepository(tmp_path / "auth.db")
    repository.initialize()
    service = AuthService(repository, TotpCipher((key,)), passwords=passwords, clock=clock)
    admin = service.provision_identity(username="console.admin", display_name="Fictional console admin",
                                       role=UserRole.IT_ADMIN, password=PASSWORD)
    owner = service.provision_identity(username="console.owner", display_name="Fictional owner",
                                       role=UserRole.OWNER, password=PASSWORD)
    connection = TrustedClientContext("fixture")
    challenge = service.begin_login(admin.username, PASSWORD, client=connection)
    secret = service.begin_mfa_enrollment(challenge.token.get_secret_value()).secret.get_secret_value()
    grant = service.confirm_mfa_enrollment(challenge.token.get_secret_value(), pyotp.TOTP(secret).at(clock.value),
                                           client=connection)
    clock.advance(60)
    console = FakeConsole(monkeypatch)
    world = {"path": repository.database_path, "repository": repository, "service": service, "clock": clock, "admin": admin,
             "owner": owner, "console": console, "admin_session": grant.session.token.get_secret_value(),
             "key": key, "output": [], "capsys": capsys,
             "secrets": [PASSWORD, secret, grant.session.token.get_secret_value(), key.decode("ascii")]}
    yield world
    read(world)
    known = world["secrets"] + [value for _, value in console.shown]
    text = "".join(world["output"])
    assert not any(value and value in text for value in known), "secret reached console output"


def read(world):
    """Read captured console output, keeping it for the final leak check."""
    captured = world["capsys"].readouterr()
    world["output"].extend([captured.out, captured.err])
    return captured


def rows(world, query, parameters=()):
    with world["repository"]._connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters)]


def admin_row(world):
    return rows(world, "SELECT * FROM auth_users WHERE user_id=?", (str(world["admin"].user_id),))[0]


def start_args(world, *, request_id=None, user=None, expected=None, **overrides):
    user = user or world["admin"]
    values = {"--auth-database": str(world["path"]), "--target-user-id": str(user.user_id),
              "--expected-auth-version": str(expected if expected is not None else admin_row(world)["auth_version"]),
              "--request-id": str(request_id or uuid4()), "--authorizer-reference": "Fictional entrepreneur",
              "--verified-person-reference": "Fictional IT administrator",
              "--verified-at": (world["clock"].value - timedelta(minutes=5)).isoformat(),
              "--approval-reference": "Fictional approval record 1", "--reason": "Fictional lost factor"} | overrides
    return ["start", *[item for pair in values.items() for item in pair]]


def start(world, **kwargs):
    world["console"].answers[:] = [cli.CONFIRMATION]
    try:
        return cli.main(start_args(world, **kwargs))
    finally:
        world["console"].answers.clear()


def enroll(world, token, *, password=PASSWORD, code=None):
    console, clock = world["console"], world["clock"]
    world["secrets"].append(token)
    console.secret_inputs[:] = [token, password,
                                code or (lambda: pyotp.TOTP(console.value("TOTP secret")).at(clock.value))]
    try:
        return cli.main(["enroll", "--auth-database", str(world["path"])])
    finally:
        console.secret_inputs.clear()  # a rejected step leaves unread scripted inputs


def open_requests(world):
    return rows(world, "SELECT * FROM auth_admin_recovery_requests ORDER BY created_at, rowid")


def test_full_chain_completes_without_a_session_and_shows_secrets_only_on_the_console(world, capsys):
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id", str(world["admin"].user_id)]) == 0
    assert "auth_version 1" in read(world).out
    assert start(world) == cli.EXIT_OK
    token = world["console"].value("Recovery authorization token")
    assert (admin_row(world)["enabled"], admin_row(world)["mfa_enabled"]) == (0, 0)
    assert enroll(world, token) == cli.EXIT_OK
    labels = world["console"].labels()
    assert labels[:3] == ["Recovery authorization token", "TOTP secret", "Provisioning URI"]
    assert [label for label in labels if label.startswith("Recovery code")] == [f"Recovery code {i}" for i in range(1, 11)]
    assert (admin_row(world)["enabled"], admin_row(world)["mfa_enabled"]) == (1, 1)
    assert not rows(world, "SELECT * FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (str(world["admin"].user_id),))
    assert open_requests(world)[0]["status"] == "COMPLETED"
    output = read(world).out
    assert "no login session was created" in output and "no notification is delivered" in output


def test_non_interactive_console_refuses_before_writing(world):
    world["console"].interactive = False
    before = admin_row(world)
    assert cli.main(start_args(world)) == cli.EXIT_USAGE
    assert cli.main(["enroll", "--auth-database", str(world["path"])]) == cli.EXIT_USAGE
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_USAGE
    assert admin_row(world) == before and not open_requests(world) and not world["console"].shown


@pytest.mark.parametrize("target", ["missing", "relative", "directory", "empty", "business", "auth-v2"])
def test_missing_wrong_or_unmigrated_database_is_rejected_and_never_created(world, tmp_path, target):
    path = {"missing": tmp_path / "missing.db", "relative": "relative-auth.db", "directory": tmp_path,
            "empty": tmp_path / "empty.db", "business": tmp_path / "works.db", "auth-v2": tmp_path / "old-auth.db"}[target]
    if target == "empty":
        path.write_bytes(b"")
    elif target == "business":
        WorkRepository(path).initialize()
    elif target == "auth-v2":
        old = AuthRepository(path)
        old.initialize()
        with old.transaction() as connection:
            connection.execute("DROP TABLE auth_admin_recovery_outbox")
            connection.execute("DROP TABLE auth_admin_recovery_requests")
            connection.execute("DELETE FROM auth_schema_migrations WHERE version=3")
    before = path.read_bytes() if target in {"empty", "business", "auth-v2"} else None
    for argv in (["status", "--auth-database", str(path), "--target-user-id", str(world["admin"].user_id)],
                 start_args(world, **{"--auth-database": str(path)}),
                 ["enroll", "--auth-database", str(path)]):
        assert cli.main(argv) == cli.EXIT_USAGE
    if before is not None:
        assert path.read_bytes() == before
    assert not (tmp_path / "missing.db").exists() and not (tmp_path / "relative-auth.db").exists()
    with pytest.raises(cli.UsageError):
        cli._ExistingAuthRepository(world["path"]).initialize()


@pytest.mark.parametrize("keys", [None, "", "not-a-fernet-key"])
def test_missing_or_invalid_totp_keys_are_a_usage_error(world, monkeypatch, keys):
    if keys is None:
        monkeypatch.delenv("CARBIT_TOTP_KEYS")
    else:
        monkeypatch.setenv("CARBIT_TOTP_KEYS", keys)
    assert cli.main(start_args(world)) == cli.EXIT_USAGE
    assert not open_requests(world)


@pytest.mark.parametrize("change", [
    {"--verified-at": "2026-10-17T08:55:00"},  # naive time: rejected by the service model
    {"--verified-person-reference": "Fictional admin 131052-308T"},
    {"--authorizer-reference": "console.admin"}, {"--authorizer-reference": "Fictional console admin"},
    "owner", "stale-version",
])
def test_invalid_approval_and_service_rejections_write_nothing(world, change):
    before = admin_row(world)
    if change == "owner":
        argv = start_args(world, user=world["owner"])
    elif change == "stale-version":
        argv = start_args(world, expected=before["auth_version"] + 1)
    else:
        argv = start_args(world, **change)
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(argv) == cli.EXIT_REJECTED
    assert admin_row(world) == before and not open_requests(world) and not world["console"].shown


@pytest.mark.parametrize("argv", [
    ["start", "--auth-database", "x", "--target-user-id", "not-a-uuid"],
    ["cancel", "--auth-database", "x", "--request-id", str(uuid4())],
    ["enroll", "--auth-database", "x", "--password", "never-an-argument"],
])
def test_argument_errors_exit_2_and_secrets_are_not_arguments(world, argv):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == cli.EXIT_USAGE
    with pytest.raises(SystemExit):
        cli.main(start_args(world, **{"--verified-at": "yesterday"}))


def test_wrong_password_token_and_totp_are_rejected_and_keep_the_account_closed(world):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token, password="Wrong fictional passphrase 1") == cli.EXIT_REJECTED
    assert open_requests(world)[0]["pending_totp_ciphertext"] is None
    assert enroll(world, "x" * 43) == cli.EXIT_REJECTED
    assert enroll(world, token, code=lambda: pyotp.TOTP(pyotp.random_base32()).at(world["clock"].value)) == cli.EXIT_REJECTED
    assert (admin_row(world)["enabled"], admin_row(world)["mfa_enabled"]) == (0, 0)
    assert open_requests(world)[0]["status"] == "OPEN"


@pytest.mark.parametrize("answer", ["no", EOFError, KeyboardInterrupt])
def test_declined_or_interrupted_confirmation_writes_nothing(world, answer, capsys):
    before = admin_row(world)
    world["console"].answers.append(answer)
    assert cli.main(start_args(world)) == cli.EXIT_INCOMPLETE
    assert admin_row(world) == before and not open_requests(world)
    assert "nothing was written" in read(world).err


@pytest.mark.parametrize("token_input", [KeyboardInterrupt, EOFError, ""])
def test_interrupted_or_empty_secret_prompt_writes_nothing(world, token_input):
    assert start(world) == 0
    world["console"].secret_inputs.append(token_input)
    assert cli.main(["enroll", "--auth-database", str(world["path"])]) == cli.EXIT_INCOMPLETE
    assert open_requests(world)[0]["pending_totp_ciphertext"] is None


def test_interruption_after_a_committed_start_is_reported_as_uncertain_and_recoverable(world, monkeypatch, capsys):
    real = cli.start_admin_offline_recovery

    def committed_then_interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "start_admin_offline_recovery", committed_then_interrupted)
    request_id = uuid4()
    assert start(world, request_id=request_id) == cli.EXIT_INCOMPLETE
    assert "may have committed" in read(world).err and not world["console"].shown
    assert open_requests(world)[0]["status"] == "OPEN"
    monkeypatch.setattr(cli, "start_admin_offline_recovery", real)
    replay = start_args(world, request_id=request_id, expected=1)
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(replay) == cli.EXIT_INCOMPLETE and not world["console"].shown  # only a hash is stored
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(["cancel", "--auth-database", str(world["path"]), "--request-id", str(request_id),
                     "--reason", "Fictional lost token"]) == cli.EXIT_OK
    assert open_requests(world)[0]["status"] == "CANCELLED" and admin_row(world)["enabled"] == 0
    assert start(world) == cli.EXIT_OK and world["console"].labels() == ["Recovery authorization token"]


def test_interrupt_at_the_code_prompt_keeps_the_pending_secret_for_complete(world, capsys):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token, code=KeyboardInterrupt) == cli.EXIT_INCOMPLETE
    assert "stored as pending" in read(world).err
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id",
                     str(world["admin"].user_id)]) == 0
    assert "pending new TOTP True" in read(world).out
    world["console"].secret_inputs.extend(
        [token, lambda: pyotp.TOTP(world["console"].value("TOTP secret")).at(world["clock"].value)])
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_OK
    assert admin_row(world)["enabled"] == 1


def test_interruption_after_completion_and_replay_never_show_codes_again(world, monkeypatch, capsys):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    real = cli.complete_admin_offline_recovery

    def committed_then_interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "complete_admin_offline_recovery", committed_then_interrupted)
    assert enroll(world, token) == cli.EXIT_INCOMPLETE
    assert "may have committed" in read(world).err
    assert open_requests(world)[0]["status"] == "COMPLETED" and admin_row(world)["enabled"] == 1
    assert not any(label.startswith("Recovery code") for label in world["console"].labels())
    monkeypatch.setattr(cli, "complete_admin_offline_recovery", real)
    world["console"].secret_inputs.extend([token, "123456"])
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_INCOMPLETE
    assert "cannot be shown again" in read(world).out
    assert not any(label.startswith("Recovery code") for label in world["console"].labels())
    assert len(rows(world, "SELECT * FROM auth_recovery_codes WHERE user_id=?", (str(world["admin"].user_id),))) == 10


def test_cancel_confirmation_idempotence_and_rejections(world):
    cancel = ["cancel", "--auth-database", str(world["path"]), "--reason", "Fictional interruption"]
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(cancel + ["--request-id", str(uuid4())]) == cli.EXIT_REJECTED
    request_id = uuid4()
    assert start(world, request_id=request_id) == 0
    token = world["console"].value("Recovery authorization token")
    world["console"].answers.append("no")
    assert cli.main(cancel + ["--request-id", str(request_id)]) == cli.EXIT_INCOMPLETE
    assert open_requests(world)[0]["status"] == "OPEN"
    for _ in range(2):
        world["console"].answers.append(cli.CONFIRMATION)
        assert cli.main(cancel + ["--request-id", str(request_id)]) == cli.EXIT_OK
    assert open_requests(world)[0]["status"] == "CANCELLED" and admin_row(world)["enabled"] == 0
    assert enroll(world, token) == cli.EXIT_REJECTED
    assert start(world) == 0
    assert enroll(world, world["console"].value("Recovery authorization token")) == cli.EXIT_OK
    completed = open_requests(world)[-1]["request_id"]
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(cancel + ["--request-id", completed]) == cli.EXIT_REJECTED


def test_expired_authorization_is_rejected_and_reported_by_status(world, capsys):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    world["clock"].advance(recovery.GRANT_SECONDS)
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id",
                     str(world["admin"].user_id)]) == 0
    assert "OPEN (expired" in read(world).out
    assert enroll(world, token) == cli.EXIT_REJECTED
    assert open_requests(world)[0]["status"] == "EXPIRED" and admin_row(world)["enabled"] == 0
    assert "expired, cancelled or completed" in read(world).err


def test_old_sessions_are_invalidated_by_the_console_start(world):
    assert start(world) == 0
    service = AuthService(AuthRepository(world["path"]), TotpCipher((world["key"],)), clock=world["clock"])
    with pytest.raises(AuthenticationError):
        service.require_session(world["admin_session"])


def test_real_console_check_refuses_captured_output(world, monkeypatch):
    # Under pytest stdout is captured, so the production check must refuse.
    monkeypatch.setattr(cli, "_interactive", REAL_INTERACTIVE)
    assert cli.main(start_args(world)) == cli.EXIT_USAGE
    assert not open_requests(world) and not world["console"].shown


def test_totp_key_mismatch_is_an_internal_error_without_details(world, monkeypatch):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    assert enroll(world, token, code=KeyboardInterrupt) == cli.EXIT_INCOMPLETE  # pending secret under key A
    other = Fernet.generate_key()
    world["secrets"].append(other.decode("ascii"))
    monkeypatch.setenv("CARBIT_TOTP_KEYS", other.decode("ascii"))
    world["console"].secret_inputs[:] = [token, "123456"]
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_ERROR
    assert "does not match" in read(world).err
    assert open_requests(world)[0]["status"] == "OPEN" and admin_row(world)["enabled"] == 0


# ---- P1: hidden input fails closed through the real console adapter -------------------------

class TtyLikeStdin:
    """A replaced stdin that still claims to be a TTY; it must never be read visibly."""

    def __init__(self):
        self.reads = 0

    def isatty(self):
        return True

    def readline(self, *args):
        self.reads += 1
        return "visible input\n"

    read = readline


@pytest.fixture
def fallback_console(monkeypatch):
    """Force getpass onto its echoed fallback path and record any visible read."""
    stdin, visible = TtyLikeStdin(), []
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(getpass, "_raw_input", lambda *args, **kwargs: visible.append(1) or "visible input")
    if os.name != "nt":  # Windows getpass already falls back when stdin is not sys.__stdin__
        monkeypatch.setattr(getpass, "getpass", getpass.fallback_getpass)
    return stdin, visible


def test_hidden_input_adapter_blocks_the_getpass_fallback_before_reading(fallback_console):
    stdin, visible = fallback_console
    filters = list(warnings.filters)
    with pytest.raises(cli.HiddenInputUnavailable):
        REAL_ASK_SECRET("Recovery authorization token: ")
    assert not visible and stdin.reads == 0
    assert warnings.filters == filters  # the error filter was local to the call
    assert not REAL_HIDDEN_INPUT()  # a replaced stdin is not the original console


def test_enroll_preflight_rejects_a_fallback_console_before_writing(world, monkeypatch, fallback_console):
    assert start(world) == 0
    stdin, visible = fallback_console
    monkeypatch.setattr(cli, "_hidden_input_available", REAL_HIDDEN_INPUT)
    monkeypatch.setattr(cli, "_ask_secret", REAL_ASK_SECRET)
    for command in ("enroll", "complete"):
        assert cli.main([command, "--auth-database", str(world["path"])]) == cli.EXIT_USAGE
        assert "hidden input is not available" in read(world).err
    # Even with the preflight bypassed, the first secret prompt fails before any write.
    monkeypatch.setattr(cli, "_hidden_input_available", lambda: True)
    assert cli.main(["enroll", "--auth-database", str(world["path"])]) == cli.EXIT_USAGE
    assert "nothing was written" in read(world).err
    assert not visible and stdin.reads == 0
    assert open_requests(world)[0]["pending_totp_ciphertext"] is None


def test_hidden_input_failure_at_the_totp_prompt_reports_the_stored_pending_secret(world, monkeypatch, fallback_console):
    assert start(world) == 0
    stdin, visible = fallback_console
    token = world["console"].value("Recovery authorization token")
    world["secrets"].append(token)
    real_getpass, answers = getpass.getpass, [token, PASSWORD]

    def first_two_then_real(prompt="Password: ", stream=None):
        return answers.pop(0) if answers else real_getpass(prompt, stream)

    monkeypatch.setattr(getpass, "getpass", first_two_then_real)
    monkeypatch.setattr(cli, "_ask_secret", REAL_ASK_SECRET)
    assert cli.main(["enroll", "--auth-database", str(world["path"])]) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "stored as pending" in error and "nothing was written" not in error
    assert not visible and stdin.reads == 0
    assert open_requests(world)[0]["pending_totp_ciphertext"] is not None
    assert (admin_row(world)["enabled"], admin_row(world)["mfa_enabled"]) == (0, 0)


# ---- P2: console failures are classified by what is already committed ---------------------

class FailingStream:
    """stdout that discards text and fails once a trigger appears (never stores secrets)."""

    def __init__(self, trigger, failure):
        self.trigger, self.failure, self.broken = trigger, failure, False

    def write(self, text):
        if self.broken or self.trigger in text:
            self.broken = True
            raise self.failure("I/O failure detail that must not be printed")
        return len(text)

    def flush(self):
        pass


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_output_failure_after_a_committed_start_reports_the_open_request(world, monkeypatch, failure):
    monkeypatch.setattr(cli, "_show_secret", REAL_SHOW_SECRET)
    monkeypatch.setattr(sys, "stdout", FailingStream("started", failure))
    assert start(world) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "was started and the account is closed" in error and "cannot be shown again" in error
    assert "Rejected" not in error and "nothing was written" not in error and "must not be printed" not in error
    assert open_requests(world)[0]["status"] == "OPEN" and admin_row(world)["enabled"] == 0


@pytest.mark.parametrize("trigger", ["Recovery completed", "Recovery code"])
@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_output_failure_after_completion_keeps_the_completed_state(world, monkeypatch, trigger, failure):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    collect = cli._show_secret

    def codes_through_real_output(label, value):
        (REAL_SHOW_SECRET if label.startswith("Recovery code") else collect)(label, value)

    monkeypatch.setattr(cli, "_show_secret", codes_through_real_output)
    console_stdout = sys.stdout
    monkeypatch.setattr(sys, "stdout", FailingStream(trigger, failure))
    assert enroll(world, token) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "Recovery completed and the account is open" in error and "cannot be shown again" in error
    assert "Rejected" not in error and "nothing was written" not in error and "must not be printed" not in error
    assert open_requests(world)[0]["status"] == "COMPLETED" and admin_row(world)["enabled"] == 1
    monkeypatch.setattr(sys, "stdout", console_stdout)
    world["console"].secret_inputs[:] = [token, "123456"]
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_INCOMPLETE
    assert "cannot be shown again" in read(world).out
    assert not any(label.startswith("Recovery code") for label in world["console"].labels())


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_output_failure_after_the_pending_secret_gives_the_enroll_continuation(world, monkeypatch, failure):
    assert start(world) == 0
    token = world["console"].value("Recovery authorization token")
    monkeypatch.setattr(sys, "stdout", FailingStream("Add this new TOTP secret", failure))
    assert enroll(world, token) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "stored as pending" in error and "Run 'enroll' again" in error and "nothing was written" not in error
    assert open_requests(world)[0]["pending_totp_ciphertext"] is not None
    assert "TOTP secret" not in world["console"].labels()  # never shown, so it must be replaced


def test_output_failure_after_a_committed_cancel_reports_the_cancelled_request(world, monkeypatch):
    request_id = uuid4()
    assert start(world, request_id=request_id) == 0
    monkeypatch.setattr(sys, "stdout", FailingStream("The account stays closed", ValueError))
    world["console"].answers[:] = [cli.CONFIRMATION]
    assert cli.main(["cancel", "--auth-database", str(world["path"]), "--request-id", str(request_id),
                     "--reason", "Fictional interruption"]) == cli.EXIT_INCOMPLETE
    error = read(world).err
    assert "is CANCELLED" in error and "Rejected" not in error
    assert open_requests(world)[0]["status"] == "CANCELLED"


def test_console_failures_before_any_write_do_not_claim_storage(world, monkeypatch):
    world["console"].answers[:] = [ValueError]
    assert cli.main(start_args(world)) == cli.EXIT_USAGE
    assert "console input failed; nothing was written" in read(world).err
    world["console"].secret_inputs[:] = [OSError]
    assert cli.main(["complete", "--auth-database", str(world["path"])]) == cli.EXIT_USAGE
    assert "nothing was written" in read(world).err
    monkeypatch.setattr(sys, "stdout", FailingStream("Account", ValueError))
    assert cli.main(["status", "--auth-database", str(world["path"]), "--target-user-id",
                     str(world["admin"].user_id)]) == cli.EXIT_USAGE
    assert "console output failed; nothing was written" in read(world).err
    assert not open_requests(world)


def test_interruption_outside_a_known_step_points_to_status(world, monkeypatch):
    def interrupted(path):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_service", interrupted)
    assert cli.main(start_args(world)) == cli.EXIT_INCOMPLETE
    assert "Run 'status'" in read(world).err and not open_requests(world)


# Approved D1 exception: local self-recovery of the operator's own IT_ADMIN account.

def self_args(world, *, user=None, expected=None, **overrides):
    user = user or world["admin"]
    current = rows(world, "SELECT auth_version FROM auth_users WHERE user_id=?", (str(user.user_id),))[0]["auth_version"]
    values = {"--auth-database": str(world["path"]), "--target-user-id": str(user.user_id),
              "--expected-auth-version": str(expected if expected is not None else current),
              "--request-id": str(uuid4()), "--operator-reference": "Fictional operator",
              "--record-reference": "Fictional self-recovery record 1", "--reason": "Fictional lost factor"} | overrides
    return ["start-self", *[item for pair in values.items() for item in pair]]


def started_details(world):
    return [json.loads(row["details_json"]) for row in rows(
        world, "SELECT details_json FROM auth_events WHERE event_type='ADMIN_OFFLINE_RECOVERY_STARTED' ORDER BY event_id")]


def test_self_recovery_chain_is_confirmed_explicitly_and_recorded_truthfully(world, monkeypatch):
    prompts = []
    monkeypatch.setattr(cli, "_ask", lambda prompt: prompts.append(prompt) or cli.SELF_RECOVERY_CONFIRMATION)
    assert cli.main(self_args(world)) == cli.EXIT_OK
    [prompt] = prompts
    assert "LOCAL SELF-RECOVERY of account console.admin (role IT_ADMIN)" in prompt
    assert "your own IT_ADMIN account" in prompt and "no external authorizer" in prompt
    assert "no in-person identity check is recorded" in prompt and "the service is stopped" in prompt
    assert "cannot verify" in prompt and f"Type {cli.SELF_RECOVERY_CONFIRMATION}" in prompt
    assert "authorizer is present" not in prompt and "against an ID document" not in prompt
    assert enroll(world, world["console"].value("Recovery authorization token")) == cli.EXIT_OK
    assert (admin_row(world)["enabled"], admin_row(world)["mfa_enabled"]) == (1, 1)
    assert open_requests(world)[0]["status"] == "COMPLETED"
    [details] = started_details(world)
    assert details["recovery_mode"] == "LOCAL_SELF_RECOVERY" and details["external_authorizer"] is None
    assert details["in_person_identity_check"] is None and details["confirmations_verified_by_program"] is False
    assert "authorizer_reference" not in details and details["record_reference"] == "Fictional self-recovery record 1"
    assert "operator for a self-recovery" in read(world).out


def test_ordinary_start_is_recorded_as_externally_authorized(world):
    assert start(world) == cli.EXIT_OK
    [details] = started_details(world)
    assert details["recovery_mode"] == "EXTERNALLY_AUTHORIZED" and details["authorizer_reference"] == "Fictional entrepreneur"
    assert "operator_reference" not in details


@pytest.mark.parametrize("answer", [cli.CONFIRMATION, "self-recovery", "no", "", EOFError, KeyboardInterrupt])
def test_self_recovery_without_its_own_confirmation_writes_nothing(world, answer, capsys):
    before = admin_row(world)
    world["console"].answers.append(answer)
    assert cli.main(self_args(world)) == cli.EXIT_INCOMPLETE
    assert admin_row(world) == before and not open_requests(world) and not world["console"].shown
    assert "nothing was written" in read(world).err


@pytest.mark.parametrize("target", ["owner", "manager"])
def test_self_recovery_rejects_business_accounts(world, target):
    user = world["owner"] if target == "owner" else world["service"].provision_identity(
        username="console.manager", display_name="Fictional manager", role=UserRole.SERVICE_MANAGER, password=PASSWORD)
    before = rows(world, "SELECT * FROM auth_users ORDER BY user_id")
    world["console"].answers.append(cli.SELF_RECOVERY_CONFIRMATION)
    assert cli.main(self_args(world, user=user)) == cli.EXIT_REJECTED
    assert rows(world, "SELECT * FROM auth_users ORDER BY user_id") == before
    assert not open_requests(world) and not world["console"].shown


def test_self_recovery_preflight_and_unknown_target_write_nothing(world):
    before = admin_row(world)
    world["console"].interactive = False
    assert cli.main(self_args(world)) == cli.EXIT_USAGE
    world["console"].interactive = True
    unknown = self_args(world)
    unknown[unknown.index("--target-user-id") + 1] = str(uuid4())
    assert cli.main(unknown) == cli.EXIT_REJECTED
    assert admin_row(world) == before and not open_requests(world) and not world["console"].shown


def test_ordinary_start_keeps_the_self_authorization_block_and_forms_do_not_mix(world):
    before = admin_row(world)
    world["console"].answers.append(cli.CONFIRMATION)
    assert cli.main(start_args(world, **{"--authorizer-reference": "console.admin"})) == cli.EXIT_REJECTED
    assert admin_row(world) == before and not open_requests(world)
    for argv in (start_args(world, **{"--operator-reference": "Fictional operator"}),
                 self_args(world, **{"--authorizer-reference": "Fictional entrepreneur"}),
                 self_args(world, **{"--verified-at": world["clock"].value.isoformat()})):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == cli.EXIT_USAGE
    assert admin_row(world) == before and not open_requests(world)
