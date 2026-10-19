"""Local console for an OWNER or SERVICE_MANAGER MFA recovery. Never a route.

Thin wrapper over ``webapp.user_mfa_recovery``; separate from the IT_ADMIN
offline recovery commands, but it reuses that console's fail-closed seams
(interactive check, hidden input, one-time output, storage-aware errors).

- ``start``: the operator signs in as IT_ADMIN with password and TOTP (hidden
  prompts) after confirming the in-person identity check. The step resets the
  factor and shows a one-time recovery grant. The IT_ADMIN console session is
  revoked afterwards. The tool cannot verify the identity check.
- ``enroll``: the user types the grant and their own password (hidden), adds the
  new secret to their own authenticator and confirms a code. The operator does
  not need the user's password.
- ``complete``: finishes an enrollment whose code prompt was interrupted.

Secrets are never arguments, never printed in errors and never written to
files. Exit codes are the same as ``webapp.admin_recovery_cli``.
"""

import argparse
import signal
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from webapp import admin_recovery_cli as console
from webapp.admin_recovery_cli import (
    EXIT_ERROR, EXIT_INCOMPLETE, EXIT_OK, EXIT_REJECTED, EXIT_USAGE, ConsoleOutputError, Incomplete, UsageError,
)
from webapp.auth_service import (
    AuthIdempotencyConflictError, AuthVersionConflictError, AuthenticationError, PermissionDeniedError,
    RateLimitError, TrustedClientContext,
)
from webapp.user_mfa_recovery import (
    UserRecoveryUnavailable, begin_user_recovery_enrollment, complete_user_mfa_recovery, recovery_status,
    start_user_mfa_recovery,
)


CLIENT = TrustedClientContext("local-user-mfa-recovery-console")


def _status(args) -> int:
    path = console._auth_database(args.auth_database)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        user = connection.execute("SELECT username,role,kind,enabled,mfa_enabled,mfa_recovery_required,auth_version "
                                  "FROM auth_users WHERE user_id=?", (args.target_user_id,)).fetchone()
        grants = recovery_status(connection, args.target_user_id)
    if user is None:
        console._out("No such account.")
        return EXIT_REJECTED
    console._out(f"Account {user['username']}: role {user['role']}, enabled {bool(user['enabled'])}, "
                 f"MFA {bool(user['mfa_enabled'])}, recovery grant required {bool(user['mfa_recovery_required'])}, "
                 f"auth_version {user['auth_version']}")
    now = int(console._clock().timestamp())
    for row in grants:
        shown = "OPEN (expired; recorded as EXPIRED on next use)" if (
            row["status"] == "OPEN" and now >= row["expires_at"]) else row["status"]
        console._out(f"Recovery {row['request_id']}: {shown}, "
                     f"created {datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat()}, "
                     f"expires {datetime.fromtimestamp(row['expires_at'], timezone.utc).isoformat()}, "
                     f"pending new TOTP {bool(row['pending'])}")
    if not grants:
        console._out("No user MFA recovery grants.")
    return EXIT_OK


@contextmanager
def _interrupt_deferred(interruption: BaseException | None = None):
    """Hold Ctrl+C until the block ends, then raise it.

    A sign-in commits its session before returning the token. Without this, an
    interruption in between would leave a stored IT_ADMIN session whose token the
    console never received and so could not revoke. The same holds for the
    revocation itself. Only this command's own session is ever revoked.
    """
    pending = []
    previous = signal.signal(signal.SIGINT, lambda signum, frame: pending.append(signum))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if pending:
        raise interruption or KeyboardInterrupt()


def _admin_session(service, sessions: list) -> None:
    """Sign in the authorizing IT_ADMIN with password and TOTP; a recovery code is not offered.

    The new session token is appended to ``sessions`` before any interruption is
    acted on, so the caller's cleanup always knows it.
    """
    try:
        username = console._ask("IT_ADMIN username: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Incomplete("input cancelled; nothing was written") from None
    except (ValueError, OSError):
        raise UsageError("console input failed; nothing was written") from None
    password = console._secret("IT_ADMIN password: ")
    code = console._secret("IT_ADMIN TOTP code: ")
    challenge = console._call(lambda: service.begin_login(username, password, client=CLIENT),
                              "Nothing was recovered. Run 'status'.")
    if challenge.requires_enrollment:
        raise AuthenticationError("authentication failed")
    with _interrupt_deferred(Incomplete("interrupted at the IT_ADMIN sign-in; nothing was recovered and this "
                                        "console's session is revoked")):
        grant = service.complete_login(challenge.token.get_secret_value(), client=CLIENT, totp_code=code)
        sessions.append(grant.token.get_secret_value())


def _revoke(service, token: str) -> None:
    try:
        service.revoke_session(token)
    except Exception:  # never print details; the session still expires on its own
        console._report("Warning: the IT_ADMIN console session could not be revoked; it expires on its own.")


def _start(args) -> int:
    path = console._auth_database(args.auth_database)
    service = console._service(path)
    console._require_interactive(hidden_input=True)
    with service.repository._connect() as connection:
        target = connection.execute("SELECT username,role FROM auth_users WHERE user_id=?",
                                    (args.target_user_id,)).fetchone()
    if target is None:
        console._out("No such account.")
        return EXIT_REJECTED
    console._confirm(f"MFA recovery of account {target['username']} (role {target['role']}).\n"
                     "Confirm: you authorize this recovery as IT_ADMIN, the user was checked in person against an "
                     "ID document (no copy kept), and the user is present to enroll a new authenticator. This tool "
                     "cannot verify the identity check.")
    request = {"target_user_id": args.target_user_id, "expected_auth_version": args.expected_auth_version,
               "request_id": args.request_id, "identity_verification_reference": args.identity_verification_reference,
               "verified_at": args.verified_at, "reason": args.reason}
    sessions = []  # this command's own IT_ADMIN session, once signed in
    try:
        _admin_session(service, sessions)
        state = console._call(lambda: start_user_mfa_recovery(service, sessions[0], request),
                              "Run 'status'. If the recovery is OPEN without a known token, start a new recovery "
                              "with a new request ID; it supersedes the open one.")
    finally:
        with _interrupt_deferred():
            for token in sessions:
                _revoke(service, token)
    if state.grant_token is None:
        with console._after_commit(f"Recovery {state.request_id} exists with status {state.status}; the console "
                                   "output failed. No token is shown again. Run 'status'."):
            console._out(f"Recovery {state.request_id} already exists with status {state.status}. "
                         "No new token is shown: only its hash is stored.")
            if state.status == "OPEN":
                console._out("If the token was lost, start a new recovery with a new request ID; it supersedes this one.")
        return EXIT_INCOMPLETE
    with console._after_commit(f"Recovery {state.request_id} was started: the old factor is removed and the account "
                               "needs this grant to enroll. Showing the token failed or was interrupted; it cannot be "
                               "shown again. Run 'status' and start a new recovery with a new request ID."):
        if state.superseded_request_id is not None:
            console._out(f"The open recovery {state.superseded_request_id} was superseded and no longer works.")
        console._out(f"Recovery {state.request_id} started. The old authenticator, recovery codes and sessions no "
                     f"longer work. The authorization expires at {state.expires_at.isoformat()} (15 minutes). "
                     "The token is shown only now:")
        console._show_secret("Recovery authorization token", state.grant_token.get_secret_value())
        console._out("Next: the user runs 'enroll' and types their own password; the operator does not need it.")
    return EXIT_OK


def _complete_with(service, token, *, pending: bool) -> int:
    code = console._code(pending=pending)
    result = console._call(lambda: complete_user_mfa_recovery(service, token, code, client=CLIENT),
                           "Run 'status'; a completed recovery shows COMPLETED and its codes cannot be shown again.")
    if result.status == "ALREADY_COMPLETED":
        with console._after_commit("This recovery was already completed; the console output failed. Run 'status'."):
            console._out("This recovery was already completed. Recovery codes are shown only once and cannot be "
                         "shown again; if they were lost, a new recovery is required.")
        return EXIT_INCOMPLETE
    with console._after_commit("Recovery completed and the new authenticator is active, but showing the recovery "
                               "codes failed or was interrupted. Codes cannot be shown again; if they were not "
                               "stored, a new recovery is required. Run 'status'."):
        console._out("Recovery completed. The new authenticator is active; no login session was created. "
                     "Store these recovery codes now; they are shown only once:")
        for index, value in enumerate(result.recovery_codes, start=1):
            console._show_secret(f"Recovery code {index}", value.get_secret_value())
        console._out("Log in normally after the next TOTP period. Record the outcome in the approval record; "
                     "no notification is delivered by the system.")
    return EXIT_OK


def _enroll(args) -> int:
    service = console._service(console._auth_database(args.auth_database))
    console._require_interactive(hidden_input=True)
    token = console._secret("Recovery authorization token: ")
    password = console._secret("Your own password (the account being recovered): ")
    enrollment = console._call(lambda: begin_user_recovery_enrollment(service, token, password, client=CLIENT),
                               "Run 'enroll' again; it replaces any pending secret.")
    with console._after_commit("A new TOTP secret is stored as pending, but showing it failed or was interrupted; "
                               "nothing was completed. Run 'enroll' again before expiry (this replaces the pending "
                               "secret), or run 'status'."):
        if enrollment.replaced_previous_secret:
            console._out("A previously pending TOTP secret was replaced and no longer works.")
        console._out(f"Add this new TOTP secret to your own authenticator (valid until "
                     f"{enrollment.expires_at.isoformat()}):")
        console._show_secret("TOTP secret", enrollment.secret.get_secret_value())
        console._show_secret("Provisioning URI", enrollment.provisioning_uri.get_secret_value())
    return _complete_with(service, token, pending=True)


def _complete(args) -> int:
    service = console._service(console._auth_database(args.auth_database))
    console._require_interactive(hidden_input=True)
    return _complete_with(service, console._secret("Recovery authorization token: "), pending=False)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="operation", required=True)
    status, start = commands.add_parser("status"), commands.add_parser("start")
    for command in (status, start):
        command.add_argument("--auth-database", required=True)
        command.add_argument("--target-user-id", required=True, type=console._uuid)
    start.add_argument("--expected-auth-version", required=True, type=int)
    start.add_argument("--request-id", required=True, type=console._uuid)
    start.add_argument("--identity-verification-reference", required=True)
    start.add_argument("--verified-at", required=True, type=console._verified_at)
    start.add_argument("--reason", required=True)
    for name in ("enroll", "complete"):
        commands.add_parser(name).add_argument("--auth-database", required=True)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    handler = {"status": _status, "start": _start, "enroll": _enroll, "complete": _complete}[args.operation]
    try:
        return handler(args)
    except UsageError as error:
        console._report(f"Error: {error}")
        return EXIT_USAGE
    except ConsoleOutputError:
        console._report("Error: console output failed; nothing was written by this command.")
        return EXIT_USAGE
    except Incomplete as error:
        console._report(f"Incomplete: {error}")
        return EXIT_INCOMPLETE
    except KeyboardInterrupt:
        console._report("Incomplete: interrupted. Run 'status' to see whether anything was committed.")
        return EXIT_INCOMPLETE
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        console._report(f"Rejected: invalid request fields ({fields}).")
        return EXIT_REJECTED
    except RateLimitError as error:
        wait = f" Retry after {error.retry_after} seconds." if error.retry_after else ""
        console._report(f"Rejected: too many attempts.{wait}")
        return EXIT_REJECTED
    except UserRecoveryUnavailable:
        # Truthful without revealing the account state to a token holder: a used
        # token may belong to a recovery that already completed.
        console._report("Rejected: this recovery authorization can no longer be used (it expired, was superseded or "
                        "was already completed); no recovery step was taken. If you completed this recovery, log in normally "
                        "with your new authenticator. Otherwise ask the IT_ADMIN to check 'status' before any new "
                        "recovery is started.")
        return EXIT_REJECTED
    except PermissionDeniedError:
        console._report("Rejected: a recent IT_ADMIN TOTP sign-in and an enabled OWNER or SERVICE_MANAGER target "
                        "are required. Nothing was recovered.")
        return EXIT_REJECTED
    except AuthVersionConflictError:
        console._report("Rejected: the account changed; run 'status' and use the current auth_version.")
        return EXIT_REJECTED
    except AuthIdempotencyConflictError:
        console._report("Rejected: the request ID is already used with other content or by another IT_ADMIN. "
                        "Run 'status'.")
        return EXIT_REJECTED
    except AuthenticationError:
        console._report("Rejected: authentication failed; nothing was completed.")
        return EXIT_REJECTED
    except ValueError:
        console._report("Rejected: invalid request values (for example a future verification time). "
                        "Nothing was recovered.")
        return EXIT_REJECTED
    except InvalidToken:
        console._report("Error: CARBIT_TOTP_KEYS does not match this database; nothing further was done.")
        return EXIT_ERROR
    except Exception:  # never print exception details that could contain secrets
        console._report("Error: unexpected failure; run 'status' before retrying.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
