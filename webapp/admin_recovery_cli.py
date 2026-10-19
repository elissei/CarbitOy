"""Trusted offline console for an IT_ADMIN's own MFA emergency recovery. Never a route.

Thin operator wrapper over ``webapp.admin_offline_recovery``; it adds no recovery
logic. Run it on the stopped service host with the service's own auth database
path and ``CARBIT_TOTP_KEYS``. The tool cannot verify that the service is
stopped, that the authorizer was present or that an identity check happened:
those are recorded procedural statements, not technical proof.

Two ways to start: ``start`` records a separate authorizer and an in-person
identity check; ``start-self`` is the approved local self-recovery of the
operator's own IT_ADMIN account, with no external authorizer and no identity
check recorded. ``start-self`` must be confirmed with its own phrase.

Secrets (password, grant token, TOTP code) are only read through hidden console
prompts, never from arguments. If hidden input is not available (getpass would
fall back to echoed input), the prompt fails closed before reading. A new grant
token, TOTP secret and recovery codes are shown only on an interactive console;
there is no JSON or pipe output for them and they are never written to files or
error messages.

Exit codes:
  0  the requested step completed (or a status/cancellation was confirmed)
  1  unexpected internal error; check ``status`` before retrying
  2  usage, configuration or console error before this command wrote anything
  3  rejected by validation or the recovery service; no recovery step was taken
  4  incomplete: interrupted, declined, a console failure after a committed step,
     or a replay without new one-time data; the message says what is committed
     and how to continue
"""

import argparse
import getpass
import os
import sqlite3
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from webapp.admin_offline_recovery import (
    SELF_RECOVERY_METHOD, AdminRecoveryUnavailable, begin_admin_recovery_enrollment, cancel_admin_offline_recovery,
    complete_admin_offline_recovery, start_admin_offline_recovery,
)
from webapp.auth_crypto import TotpCipher
from webapp.auth_repository import AUTH_SCHEMA_VERSIONS, AuthRepository
from webapp.auth_service import (
    AuthIdempotencyConflictError, AuthService, AuthVersionConflictError, AuthenticationError,
    PermissionDeniedError, RateLimitError, TrustedClientContext,
)


EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_REJECTED, EXIT_INCOMPLETE = 0, 1, 2, 3, 4
SUPPORTED_AUTH_VERSIONS = set(AUTH_SCHEMA_VERSIONS)
REQUIRED_TABLES = {"auth_users", "auth_sessions", "auth_events", "auth_recovery_codes",
                   "auth_admin_recovery_requests", "auth_admin_recovery_outbox"}
CLIENT = TrustedClientContext("offline-admin-recovery-console")
CONFIRMATION = "YES"
SELF_RECOVERY_CONFIRMATION = "SELF-RECOVERY"
# Console failures: a closed/broken stream, or an interruption while printing.
CONSOLE_FAILURES = (ValueError, OSError, EOFError, KeyboardInterrupt)


class UsageError(Exception):
    """A usage, configuration or console problem detected before this command wrote anything."""


class Incomplete(Exception):
    """The step did not finish; the message states what is committed."""


class HiddenInputUnavailable(Exception):
    """Hidden input is not available; nothing was read."""


class ConsoleOutputError(Exception):
    """Normal console output failed; the caller decides what was committed."""


# Console seams. Tests replace the scripted parts; the hidden-input adapter below
# stays the production path.
def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _hidden_input_available() -> bool:
    """Whether getpass will use its hidden path rather than the echoed fallback."""
    if sys.stdin is None or sys.stdin is not sys.__stdin__ or not sys.stdin.isatty():
        return False  # Windows getpass falls back whenever stdin was replaced
    try:
        if os.name == "nt":
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401
    except ImportError:
        return False
    return True


def _ask(prompt: str) -> str:
    return input(prompt)


def _ask_secret(prompt: str) -> str:
    """Hidden input only: getpass's echoed fallback warns before reading, which fails here.

    The warning filter is local to this call and restored afterwards.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise HiddenInputUnavailable() from None


def _show_secret(label: str, value: str) -> None:
    # Called only after the interactive console was confirmed for this command.
    print(f"{label}: {value}")


def _out(text: str = "") -> None:
    try:
        print(text)
    except (ValueError, OSError) as error:
        raise ConsoleOutputError() from error


def _report(text: str) -> None:
    """Final status line on stderr; a broken stderr must not raise a traceback."""
    try:
        print(text, file=sys.stderr)
    except (ValueError, OSError, KeyboardInterrupt):
        pass


def _clock():
    return datetime.now(timezone.utc)


class _ExistingAuthRepository(AuthRepository):
    """Same connection rules as AuthRepository, but never creates a missing file."""

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            for setting in ("foreign_keys", "recursive_triggers"):
                connection.execute(f"PRAGMA {setting}=ON")
                if connection.execute(f"PRAGMA {setting}").fetchone()[0] != 1:
                    raise RuntimeError(f"authentication storage requires {setting}=ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        raise UsageError("this tool never initializes or migrates an authentication database")


def _auth_database(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise UsageError("an existing absolute authentication database path is required")
    path = path.resolve()
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            versions = ({row[0] for row in connection.execute("SELECT version FROM auth_schema_migrations")}
                        if "auth_schema_migrations" in tables else set())
    except sqlite3.DatabaseError:
        raise UsageError("the file is not a readable authentication database") from None
    if versions != SUPPORTED_AUTH_VERSIONS or not REQUIRED_TABLES <= tables:
        raise UsageError("an authentication database migrated by the current release is required")
    return path


def _service(path: Path) -> AuthService:
    raw = os.environ.get("CARBIT_TOTP_KEYS", "").strip()
    if not raw:
        raise UsageError("CARBIT_TOTP_KEYS of this service must be set in the environment")
    try:
        cipher = TotpCipher(tuple(key.strip().encode("ascii") for key in raw.split(",")))
    except (ValueError, TypeError, UnicodeError):
        raise UsageError("CARBIT_TOTP_KEYS is not a valid key list") from None
    return AuthService(_ExistingAuthRepository(path), cipher, clock=_clock)


def _require_interactive(*, hidden_input: bool):
    if not _interactive():
        raise UsageError("one-time secrets are shown only on an interactive console; nothing was written")
    if hidden_input and not _hidden_input_available():
        raise UsageError("hidden input is not available on this console; nothing was written")


def _confirm(question: str, word: str = CONFIRMATION) -> None:
    try:
        answer = _ask(f"{question}\nType {word} to continue: ")
    except (EOFError, KeyboardInterrupt):
        raise Incomplete("not confirmed; nothing was written") from None
    except (ValueError, OSError):
        raise UsageError("console input failed; nothing was written") from None
    if answer.strip() != word:
        raise Incomplete("not confirmed; nothing was written")


def _secret(prompt: str) -> str:
    """A secret read before this command's first write."""
    try:
        value = _ask_secret(prompt)
    except HiddenInputUnavailable:
        raise UsageError("hidden input is not available on this console; nothing was written") from None
    except (EOFError, KeyboardInterrupt):
        raise Incomplete("input cancelled before the step; nothing was written by it") from None
    except (ValueError, OSError):
        raise UsageError("console input failed; nothing was written") from None
    if not value:
        raise Incomplete("empty input; nothing was written by this step")
    return value


def _call(step, after_commit_note):
    """Run one committing service call; an interruption during it is reported as uncertain."""
    try:
        return step()
    except KeyboardInterrupt:
        raise Incomplete(f"interrupted while the step was running; it may have committed. {after_commit_note}") from None


@contextmanager
def _after_commit(note: str):
    """Console output after a committed step: a failure is never a rejection or a rollback."""
    try:
        yield
    except (ConsoleOutputError,) + CONSOLE_FAILURES:
        raise Incomplete(note) from None


def _verified_at(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO 8601 time with a timezone, e.g. 2026-10-17T09:00:00+03:00") from None


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("a UUID is required") from None


def _status(args) -> int:
    path = _auth_database(args.auth_database)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute("SELECT username,role,kind,enabled,mfa_enabled,mfa_recovery_required,auth_version "
                                  "FROM auth_users WHERE user_id=?", (args.target_user_id,)).fetchone()
        requests = connection.execute(
            "SELECT request_id,status,created_at,expires_at,finished_at,pending_totp_ciphertext IS NOT NULL AS pending "
            "FROM auth_admin_recovery_requests WHERE target_user_id=? ORDER BY created_at, rowid",
            (args.target_user_id,)).fetchall()
    if user is None:
        _out("No such account.")
        return EXIT_REJECTED
    _out(f"Account {user['username']}: role {user['role']}, enabled {bool(user['enabled'])}, "
         f"MFA {bool(user['mfa_enabled'])}, recovery grant required {bool(user['mfa_recovery_required'])}, "
         f"auth_version {user['auth_version']}")
    now = int(_clock().timestamp())
    for row in requests:
        stored = row["status"]
        shown = "OPEN (expired; recorded as EXPIRED on next use or cancellation)" if (
            stored == "OPEN" and now >= row["expires_at"]) else stored
        _out(f"Recovery {row['request_id']}: {shown}, created {datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat()}, "
             f"expires {datetime.fromtimestamp(row['expires_at'], timezone.utc).isoformat()}, "
             f"pending new TOTP {bool(row['pending'])}")
    if not requests:
        _out("No offline recovery requests.")
    return EXIT_OK


def _start(args) -> int:
    path = _auth_database(args.auth_database)
    service = _service(path)
    _require_interactive(hidden_input=False)  # start reads no secret; it shows one
    with service.repository._connect() as connection:
        target = connection.execute("SELECT username,display_name FROM auth_users WHERE user_id=?",
                                    (args.target_user_id,)).fetchone()
    if target is not None and args.authorizer_reference.strip().casefold() in {
            target["username"].casefold(), target["display_name"].strip().casefold()}:
        # D1: the IT_ADMIN does not approve their own recovery (a name check, not proof).
        _out("Rejected: the authorizer must be the named authorizer, not the IT_ADMIN being recovered.")
        return EXIT_REJECTED
    _confirm("Confirm: the service is stopped, the named authorizer is present, and the identity was "
             "checked in person against an ID document (no copy kept). This tool cannot verify these.")
    return _begin(service, {"target_user_id": args.target_user_id, "expected_auth_version": args.expected_auth_version,
                            "request_id": args.request_id, "reason": args.reason,
                            "approval": {"authorizer_reference": args.authorizer_reference,
                                         "verified_person_reference": args.verified_person_reference,
                                         "verified_at": args.verified_at,
                                         "approval_reference": args.approval_reference}})


def _start_self(args) -> int:
    path = _auth_database(args.auth_database)
    service = _service(path)
    _require_interactive(hidden_input=False)  # start-self reads no secret; it shows one
    with service.repository._connect() as connection:
        target = connection.execute("SELECT username,role FROM auth_users WHERE user_id=?",
                                    (args.target_user_id,)).fetchone()
    if target is None:
        _out("No such account.")
        return EXIT_REJECTED
    # No name comparison here: the approved self-recovery has no separate authorizer,
    # and a name is never used as access control. The service checks the role.
    _confirm(f"LOCAL SELF-RECOVERY of account {target['username']} (role {target['role']}).\n"
             "Confirm all of the following: this is your own IT_ADMIN account and you recover it yourself; no "
             "external authorizer is used and no in-person identity check is recorded; the service is stopped. "
             "These are your statements; this tool cannot verify them.", SELF_RECOVERY_CONFIRMATION)
    return _begin(service, {"target_user_id": args.target_user_id, "expected_auth_version": args.expected_auth_version,
                            "request_id": args.request_id, "reason": args.reason,
                            "approval": {"method": SELF_RECOVERY_METHOD,
                                         "operator_reference": args.operator_reference,
                                         "record_reference": args.record_reference,
                                         "operator_confirmed_own_account": True,
                                         "operator_confirmed_service_stopped": True}})


def _begin(service, request) -> int:
    """The committing start step shared by both approval forms."""
    state = _call(lambda: start_admin_offline_recovery(service, request),
                  "Run 'status'. If the request is OPEN without a known token, cancel it and start a new request.")
    if state.grant_token is None:
        with _after_commit(f"Recovery {state.request_id} exists with status {state.status}; the console output "
                           "failed. No token is shown again. Run 'status'."):
            _out(f"Recovery {state.request_id} already exists with status {state.status}. "
                 "No new token is shown: only its hash is stored.")
            if state.status == "OPEN":
                _out("If the token was lost, cancel this request and start a new one with a new request ID.")
        return EXIT_INCOMPLETE
    with _after_commit(f"Recovery {state.request_id} was started and the account is closed, but showing the "
                       "token failed or was interrupted. The token cannot be shown again: run 'status', cancel "
                       "this request and start a new one with a new request ID."):
        _out(f"Recovery {state.request_id} started. The account is closed. Authorization expires at "
             f"{state.expires_at.isoformat()} (15 minutes). The token is shown only now:")
        _show_secret("Recovery authorization token", state.grant_token.get_secret_value())
        _out("Next: run 'enroll' within the validity period.")
    return EXIT_OK


def _code(*, pending: bool) -> str:
    """The TOTP code; with ``pending`` the new secret is already stored by this command."""
    if pending:
        note = ("the new TOTP secret is stored as pending; nothing was completed. Finish with 'complete' using "
                "the same token before expiry, or run 'enroll' again (this replaces the pending secret).")
        try:
            value = _ask_secret("New TOTP code: ")
        except (HiddenInputUnavailable,) + CONSOLE_FAILURES:
            raise Incomplete(note) from None
        if not value:
            raise Incomplete(note)
        return value
    try:
        value = _ask_secret("New TOTP code: ")
    except HiddenInputUnavailable:
        raise UsageError("hidden input is not available on this console; nothing was written") from None
    except (EOFError, KeyboardInterrupt):
        raise Incomplete("input cancelled; nothing was written by this step") from None
    except (ValueError, OSError):
        raise UsageError("console input failed; nothing was written") from None
    if not value:
        raise Incomplete("empty code; nothing was completed")
    return value


def _complete_with(service, token, *, pending: bool) -> int:
    code = _code(pending=pending)
    result = _call(lambda: complete_admin_offline_recovery(service, token, code, client=CLIENT),
                   "Run 'status'; a completed recovery shows COMPLETED and its codes cannot be shown again.")
    if result.status == "ALREADY_COMPLETED":
        with _after_commit("This recovery was already completed; the console output failed. Run 'status'."):
            _out("This recovery was already completed. Recovery codes are shown only once and cannot be shown "
                 "again; if they were lost, a new offline recovery is required.")
        return EXIT_INCOMPLETE
    with _after_commit("Recovery completed and the account is open, but showing the recovery codes failed or "
                       "was interrupted. Codes cannot be shown again; if they were not stored, a new offline "
                       "recovery is required. Run 'status'. Record the outcome in the approval record."):
        _out("Recovery completed. The account is open; no login session was created. "
             "Store these recovery codes now; they are shown only once:")
        for index, value in enumerate(result.recovery_codes, start=1):
            _show_secret(f"Recovery code {index}", value.get_secret_value())
        _out("Log in normally after the next TOTP period. Record the completion in the approval record (by the "
             "authorizer, or by the operator for a self-recovery); no notification is delivered by the system.")
    return EXIT_OK


def _enroll(args) -> int:
    service = _service(_auth_database(args.auth_database))
    _require_interactive(hidden_input=True)
    token = _secret("Recovery authorization token: ")
    password = _secret("IT_ADMIN password: ")
    enrollment = _call(lambda: begin_admin_recovery_enrollment(service, token, password, client=CLIENT),
                       "Run 'enroll' again; it replaces any pending secret.")
    with _after_commit("A new TOTP secret is stored as pending, but showing it failed or was interrupted; "
                       "nothing was completed. Run 'enroll' again before expiry (this replaces the pending "
                       "secret), or run 'status'."):
        if enrollment.replaced_previous_secret:
            _out("A previously pending TOTP secret was replaced and no longer works.")
        _out(f"Add this new TOTP secret to your own authenticator (valid until {enrollment.expires_at.isoformat()}):")
        _show_secret("TOTP secret", enrollment.secret.get_secret_value())
        _show_secret("Provisioning URI", enrollment.provisioning_uri.get_secret_value())
    return _complete_with(service, token, pending=True)


def _complete(args) -> int:
    service = _service(_auth_database(args.auth_database))
    _require_interactive(hidden_input=True)
    return _complete_with(service, _secret("Recovery authorization token: "), pending=False)


def _cancel(args) -> int:
    service = _service(_auth_database(args.auth_database))
    _confirm(f"Cancel recovery {args.request_id}? The account stays closed until a new recovery completes.")
    state = _call(lambda: cancel_admin_offline_recovery(service, args.request_id, reason=args.reason),
                  "Run 'status' to see whether the request is CANCELLED.")
    with _after_commit(f"Recovery {state.request_id} is {state.status}; the console output failed. "
                       "Run 'status'. The account stays closed."):
        _out(f"Recovery {state.request_id} is {state.status}. The account stays closed. Record the interruption "
             "in the approval record (by the authorizer, or by the operator for a self-recovery); no notification "
             "is delivered by the system.")
    return EXIT_OK


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="operation", required=True)
    status = commands.add_parser("status")
    start = commands.add_parser("start", help="externally authorized recovery with an in-person identity check")
    start_self = commands.add_parser("start-self", help="approved local self-recovery of your own IT_ADMIN account")
    for command in (status, start, start_self):
        command.add_argument("--auth-database", required=True)
        command.add_argument("--target-user-id", required=True, type=_uuid)
    for command in (start, start_self):
        command.add_argument("--expected-auth-version", required=True, type=int)
        command.add_argument("--request-id", required=True, type=_uuid)
    start.add_argument("--authorizer-reference", required=True)
    start.add_argument("--verified-person-reference", required=True)
    start.add_argument("--verified-at", required=True, type=_verified_at)
    start.add_argument("--approval-reference", required=True)
    start_self.add_argument("--operator-reference", required=True)
    start_self.add_argument("--record-reference", required=True)
    for command in (start, start_self):
        command.add_argument("--reason", required=True)
    for name in ("enroll", "complete"):
        commands.add_parser(name).add_argument("--auth-database", required=True)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--auth-database", required=True)
    cancel.add_argument("--request-id", required=True, type=_uuid)
    cancel.add_argument("--reason", required=True)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    handler = {"status": _status, "start": _start, "start-self": _start_self, "enroll": _enroll,
               "complete": _complete, "cancel": _cancel}[args.operation]
    try:
        return handler(args)
    except UsageError as error:
        _report(f"Error: {error}")
        return EXIT_USAGE
    except ConsoleOutputError:
        # Only reachable before a committed step: committed steps convert it in _after_commit.
        _report("Error: console output failed; nothing was written by this command.")
        return EXIT_USAGE
    except Incomplete as error:
        _report(f"Incomplete: {error}")
        return EXIT_INCOMPLETE
    except KeyboardInterrupt:
        _report("Incomplete: interrupted. Run 'status' to see whether anything was committed.")
        return EXIT_INCOMPLETE
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        _report(f"Rejected: invalid approval or request fields ({fields}).")
        return EXIT_REJECTED
    except RateLimitError as error:
        wait = f" Retry after {error.retry_after} seconds." if error.retry_after else ""
        _report(f"Rejected: too many attempts.{wait}")
        return EXIT_REJECTED
    except AdminRecoveryUnavailable:
        _report("Rejected: the authorization is expired, cancelled or completed. The account stays closed unless "
                "the recovery completed; run 'status' and start a new recovery if needed.")
        return EXIT_REJECTED
    except PermissionDeniedError:
        _report("Rejected: only an IT_ADMIN's own offline recovery is supported, and an account closed for another "
                "reason is not reopened.")
        return EXIT_REJECTED
    except AuthVersionConflictError:
        _report("Rejected: the account changed; run 'status' and use the current auth_version.")
        return EXIT_REJECTED
    except AuthIdempotencyConflictError:
        _report("Rejected: the request conflicts with an existing recovery (same request ID with other content, "
                "an open recovery for this account, or a completed recovery). Run 'status'.")
        return EXIT_REJECTED
    except AuthenticationError:
        _report("Rejected: authentication failed; nothing was completed.")
        return EXIT_REJECTED
    except LookupError:
        _report("Rejected: unknown recovery request.")
        return EXIT_REJECTED
    except ValueError:
        # Only service validation reaches here; console errors are classified above.
        # Original exception text is not printed.
        _report("Rejected: invalid request values (for example a future verification time or an empty "
                "reason). Nothing was recovered.")
        return EXIT_REJECTED
    except InvalidToken:
        _report("Error: CARBIT_TOTP_KEYS does not match this database; nothing further was done.")
        return EXIT_ERROR
    except Exception:  # never print exception details that could contain secrets
        _report("Error: unexpected failure; run 'status' before retrying.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
