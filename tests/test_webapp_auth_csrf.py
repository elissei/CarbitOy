"""Isolated crypto/cookie foundation: deterministic clocks, fictional secrets."""

import base64
import hmac
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from pydantic import SecretStr
from starlette.responses import Response

from http_auth_support import settings_for
from webapp.auth_csrf import (
    COOKIE_NAMES, CsrfProtector, CsrfValidationError, _frame, clear_auth_cookies,
    set_mfa_cookie, set_preauth_cookie, set_session_cookie,
)
from webapp.auth_models import IdentityKind, SessionGrant, SessionRecord, User, UserRole
from webapp.auth_settings import CsrfSigningKey


ORIGIN = "https://testserver"
OTHER_ORIGIN = "https://testserver:8443"


def opaque(byte=71):
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def key(identifier="active", byte=81):
    return CsrfSigningKey(identifier, SecretStr(base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii")))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def foundation(tmp_path):
    clock = Clock()
    settings = replace(
        settings_for(tmp_path / "auth.db"), external_origins=(ORIGIN, OTHER_ORIGIN),
        csrf_active_key_id="active", csrf_keys=(key(),),
    )
    return CsrfProtector(settings, clock=clock), settings, clock


def contexts(protector, clock, purpose):
    if purpose == "session":
        return dict(session_token=opaque(), session_absolute_expiry=clock() + timedelta(hours=8), session_idle_expiry=clock() + timedelta(minutes=30))
    preauth = protector.create_preauth(ORIGIN).value.get_secret_value()
    kwargs = dict(preauth_cookie=preauth)
    if purpose == "mfa":
        kwargs["mfa_cookie"] = protector.seal_mfa(preauth, opaque(), ORIGIN, challenge_expires_at=clock() + timedelta(seconds=300)).value.get_secret_value()
    return kwargs


@pytest.mark.parametrize("purpose", ["preauth", "mfa", "session"])
def test_valid_proofs_are_private_random_and_reusable(foundation, purpose):
    protector, settings, clock = foundation
    kwargs = contexts(protector, clock, purpose)
    proofs = [protector.issue_proof(purpose, ORIGIN, **kwargs) for _ in range(2)]
    assert proofs[0] != proofs[1]
    for proof in proofs:
        assert proof.startswith(f"v1.active.{purpose}.")
        for _ in range(2):
            assert protector.verify_proof(proof, purpose, ORIGIN, **kwargs) is None
        assert opaque() not in proof
        assert settings.csrf_keys[0].secret.get_secret_value() not in proof
        if purpose != "session":
            context = protector.open_preauth(kwargs["preauth_cookie"], ORIGIN)
            assert context.secret.get_secret_value() not in proof
            assert context.secret.get_secret_value() not in repr(context)
            assert context.secret.get_secret_value() not in repr(protector)


@pytest.mark.parametrize("purpose,seconds", [("preauth", 600), ("mfa", 300), ("session", 600)])
def test_exact_proof_lifetime_boundaries(foundation, purpose, seconds):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, purpose)
    proof = protector.issue_proof(purpose, ORIGIN, **kwargs)
    assert int(proof.split(".")[4]) - int(proof.split(".")[3]) == seconds
    clock.advance(seconds - 1)
    protector.verify_proof(proof, purpose, ORIGIN, **kwargs)
    clock.advance(1)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, purpose, ORIGIN, **kwargs)


@pytest.mark.parametrize("purpose", ["preauth", "mfa", "session"])
def test_not_yet_valid_proof_rejected(foundation, purpose):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, purpose)
    proof = protector.issue_proof(purpose, ORIGIN, **kwargs)
    clock.advance(-1)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, purpose, ORIGIN, **kwargs)


@pytest.mark.parametrize("invalid", [None, "", "v1", "a" * 513, "\n", [], 12])
def test_missing_or_malformed_proof(foundation, invalid):
    protector, _, clock = foundation
    with pytest.raises(CsrfValidationError, match="^Invalid CSRF security context$"):
        protector.verify_proof(invalid, "session", ORIGIN, **contexts(protector, clock, "session"))


@pytest.mark.parametrize("index,replacement", [
    (0, "v2"), (1, "unknown"), (1, "active.active"), (2, "preauth"),
    (3, "01791633600"), (3, "+1791633600"), (3, "-1"), (3, "1.0"),
    (3, " 1791633600"), (3, "١٧٩١٦٣٣٦٠٠"), (4, "99999999999"),
    (5, "A" * 22 + "="), (5, "A" * 21), (6, "A" * 43), (6, "A" * 43 + "="),
])
def test_tampered_public_fields_and_encodings(foundation, index, replacement):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "session")
    fields = protector.issue_proof("session", ORIGIN, **kwargs).split(".")
    fields[index] = replacement
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(".".join(fields), "session", ORIGIN, **kwargs)


def test_noncanonical_base64_pad_bits_rejected(foundation):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "session")
    fields = protector.issue_proof("session", ORIGIN, **kwargs).split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for index in (5, 6):
        changed = fields.copy()
        changed[index] = fields[index][:-1] + alphabet[alphabet.index(fields[index][-1]) + 1]
        assert base64.urlsafe_b64decode(changed[index] + "=" * (-len(changed[index]) % 4)) == base64.urlsafe_b64decode(fields[index] + "=" * (-len(fields[index]) % 4))
        with pytest.raises(CsrfValidationError):
            protector.verify_proof(".".join(changed), "session", ORIGIN, **kwargs)


@pytest.mark.parametrize("purpose", ["preauth", "mfa", "session"])
def test_cross_origin_context_and_purpose_replay(foundation, purpose):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, purpose)
    proof = protector.issue_proof(purpose, ORIGIN, **kwargs)
    for origin in (OTHER_ORIGIN, "https://evil.example", ORIGIN + "/", ORIGIN.upper(), "http://testserver"):
        with pytest.raises(CsrfValidationError):
            protector.verify_proof(proof, purpose, origin, **kwargs)
    changed = contexts(protector, clock, purpose)
    if purpose == "session":
        changed["session_token"] = opaque(72)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, purpose, ORIGIN, **changed)
    for other in {"preauth", "mfa", "session"} - {purpose}:
        with pytest.raises(CsrfValidationError):
            protector.verify_proof(proof, other, ORIGIN, **contexts(protector, clock, other))


def test_mfa_substitution_and_replacement_preauth(foundation):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "mfa")
    proof = protector.issue_proof("mfa", ORIGIN, **kwargs)
    challenge = protector.open_mfa(kwargs["preauth_cookie"], kwargs["mfa_cookie"], ORIGIN)
    assert challenge.challenge_token.get_secret_value() == opaque()
    assert opaque() not in kwargs["mfa_cookie"] and opaque() not in repr(challenge)
    replacement = protector.seal_mfa(kwargs["preauth_cookie"], opaque(72), ORIGIN, challenge_expires_at=clock() + timedelta(seconds=300))
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "mfa", ORIGIN, **{**kwargs, "mfa_cookie": replacement.value.get_secret_value()})
    new_preauth = protector.create_preauth(ORIGIN).value.get_secret_value()
    with pytest.raises(CsrfValidationError):
        protector.open_mfa(new_preauth, kwargs["mfa_cookie"], ORIGIN)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "mfa", ORIGIN, **{**kwargs, "preauth_cookie": new_preauth})


@pytest.mark.parametrize("purpose", ["preauth", "mfa"])
@pytest.mark.parametrize("field", range(7))
def test_envelope_field_tampering(foundation, purpose, field):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, purpose)
    value = kwargs["preauth_cookie"] if purpose == "preauth" else kwargs["mfa_cookie"]
    fields = value.split(".")
    replacements = ["e2", "unknown", "session", str(int(fields[3]) - 1), str(int(fields[4]) - 1), "A" * 16, "A" * len(fields[6])]
    fields[field] = replacements[field]
    with pytest.raises(CsrfValidationError):
        if purpose == "preauth":
            protector.open_preauth(".".join(fields), ORIGIN)
        else:
            protector.open_mfa(kwargs["preauth_cookie"], ".".join(fields), ORIGIN)


def test_envelope_confidentiality_and_boundaries(foundation):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "mfa")
    context = protector.open_preauth(kwargs["preauth_cookie"], ORIGIN)
    ciphertext = base64.urlsafe_b64decode(kwargs["preauth_cookie"].split(".")[6] + "==")
    assert base64.urlsafe_b64decode(context.secret.get_secret_value() + "=") not in ciphertext
    assert opaque().encode() not in base64.urlsafe_b64decode(kwargs["mfa_cookie"].split(".")[6] + "==")
    with pytest.raises(CsrfValidationError):
        protector.open_preauth(kwargs["preauth_cookie"], OTHER_ORIGIN)
    clock.advance(299)
    protector.open_mfa(kwargs["preauth_cookie"], kwargs["mfa_cookie"], ORIGIN)
    clock.advance(1)
    with pytest.raises(CsrfValidationError):
        protector.open_mfa(kwargs["preauth_cookie"], kwargs["mfa_cookie"], ORIGIN)
    clock.advance(299)
    protector.open_preauth(kwargs["preauth_cookie"], ORIGIN)
    clock.advance(1)
    with pytest.raises(CsrfValidationError):
        protector.open_preauth(kwargs["preauth_cookie"], ORIGIN)


@pytest.mark.parametrize("limit", ["session_absolute_expiry", "session_idle_expiry"])
def test_session_proof_clamped_to_server_deadlines(foundation, limit):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "session")
    kwargs[limit] = clock() + timedelta(seconds=17)
    proof = protector.issue_proof("session", ORIGIN, **kwargs)
    assert int(proof.split(".")[4]) == int(kwargs[limit].timestamp())
    changed = {**kwargs, limit: clock() + timedelta(seconds=16)}
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "session", ORIGIN, **changed)
    clock.advance(16)
    protector.verify_proof(proof, "session", ORIGIN, **kwargs)
    clock.advance(1)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "session", ORIGIN, **kwargs)


def test_mfa_deadlines_clamped_to_preauth_and_actual_challenge(foundation):
    protector, _, clock = foundation
    preauth = protector.create_preauth(ORIGIN)
    clock.advance(550)
    mfa = protector.seal_mfa(preauth.value.get_secret_value(), opaque(), ORIGIN, challenge_expires_at=clock() + timedelta(seconds=300))
    assert mfa.expires_at == preauth.expires_at
    kwargs = dict(preauth_cookie=preauth.value.get_secret_value(), mfa_cookie=mfa.value.get_secret_value())
    assert int(protector.issue_proof("mfa", ORIGIN, **kwargs).split(".")[4]) == int(preauth.expires_at.timestamp())
    actual = clock() + timedelta(seconds=10)
    shorter = protector.seal_mfa(preauth.value.get_secret_value(), opaque(), ORIGIN, challenge_expires_at=actual)
    assert shorter.expires_at == actual
    with pytest.raises(CsrfValidationError):
        protector.seal_mfa(preauth.value.get_secret_value(), opaque(), ORIGIN, challenge_expires_at=clock())


@pytest.mark.parametrize("lifetime", [0, -1, 601, True, 1.0, "600"])
def test_invalid_requested_lifetimes(foundation, lifetime):
    protector, _, clock = foundation
    with pytest.raises(CsrfValidationError):
        protector.issue_proof("session", ORIGIN, lifetime_seconds=lifetime, **contexts(protector, clock, "session"))


def test_short_proof_and_expired_or_ambiguous_bindings(foundation):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "session")
    proof = protector.issue_proof("session", ORIGIN, lifetime_seconds=1, **kwargs)
    protector.verify_proof(proof, "session", ORIGIN, **kwargs)
    for changes in ({"preauth_cookie": "extra"}, {"session_idle_expiry": clock()}, {"session_absolute_expiry": clock().replace(tzinfo=None)}, {"session_token": opaque() + "="}, {"session_token": opaque() * 10000}):
        with pytest.raises(CsrfValidationError):
            protector.issue_proof("session", ORIGIN, **{**kwargs, **changes})
    for purpose in ("unknown", None, []):
        with pytest.raises(CsrfValidationError):
            protector.issue_proof(purpose, ORIGIN)
    clock.advance(1)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "session", ORIGIN, **kwargs)


def test_constant_time_mac_comparison(foundation, monkeypatch):
    protector, _, clock = foundation
    kwargs = contexts(protector, clock, "session")
    proof = protector.issue_proof("session", ORIGIN, **kwargs)
    calls, original = [], hmac.compare_digest
    def compare(first, second):
        calls.append((type(first), len(first), type(second), len(second)))
        return original(first, second)
    monkeypatch.setattr(hmac, "compare_digest", compare)
    protector.verify_proof(proof, "session", ORIGIN, **kwargs)
    fields = proof.split(".")
    fields[6] = opaque(90)
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(".".join(fields), "session", ORIGIN, **kwargs)
    assert calls == [(bytes, 32, bytes, 32)] * 2


@pytest.mark.parametrize("start,end", [(-1, 600), (1, 600), (0, 601), (0, 0), (0, -1), (-601, -1)])
def test_valid_mac_does_not_override_timestamp_rules(foundation, start, end):
    protector, settings, clock = foundation
    kwargs = contexts(protector, clock, "session")
    fields = protector.issue_proof("session", ORIGIN, **kwargs).split(".")[:6]
    fields[3], fields[4] = str(int(clock().timestamp()) + start), str(int(clock().timestamp()) + end)
    mac = hmac.digest(settings.csrf_keys[0].key_bytes(), _frame("carbit-csrf-proof-v1", fields, ORIGIN, (base64.urlsafe_b64decode(opaque() + "="),)), "sha256")
    proof = ".".join(fields + [base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")])
    with pytest.raises(CsrfValidationError):
        protector.verify_proof(proof, "session", ORIGIN, **kwargs)


def test_invalid_clock_and_missing_contexts(foundation):
    protector, settings, clock = foundation
    for purpose in ("preauth", "mfa", "session"):
        with pytest.raises(CsrfValidationError):
            protector.issue_proof(purpose, ORIGIN)
    with pytest.raises(CsrfValidationError):
        CsrfProtector(settings, clock=lambda: clock().replace(tzinfo=None)).create_preauth(ORIGIN)
    preauth = protector.create_preauth(ORIGIN).value.get_secret_value()
    for kwargs in ({"mfa_cookie": "unused"}, {"session_token": opaque()}):
        with pytest.raises(CsrfValidationError):
            protector.issue_proof("preauth", ORIGIN, preauth_cookie=preauth, **kwargs)


def test_active_signing_previous_verification_and_retirement(foundation):
    old, settings, clock = foundation
    kwargs = contexts(old, clock, "mfa")
    proof = old.issue_proof("mfa", ORIGIN, **kwargs)
    new_key = key("new", 82)
    rotated_settings = replace(settings, csrf_active_key_id="new", csrf_keys=(key(), new_key))
    rotated = CsrfProtector(rotated_settings, clock=clock)
    rotated.verify_proof(proof, "mfa", ORIGIN, **kwargs)
    assert rotated.open_mfa(kwargs["preauth_cookie"], kwargs["mfa_cookie"], ORIGIN).challenge_token.get_secret_value() == opaque()
    assert rotated.issue_proof("mfa", ORIGIN, **kwargs).split(".")[1] == "new"
    fresh = rotated.create_preauth(ORIGIN)
    assert fresh.value.get_secret_value().split(".")[1] == "new"
    retired = CsrfProtector(replace(rotated_settings, csrf_keys=(new_key,)), clock=clock)
    with pytest.raises(CsrfValidationError):
        retired.verify_proof(proof, "mfa", ORIGIN, **kwargs)
    session_kwargs = contexts(old, clock, "session")
    old_session_proof = old.issue_proof("session", ORIGIN, **session_kwargs)
    rotated.verify_proof(old_session_proof, "session", ORIGIN, **session_kwargs)
    with pytest.raises(CsrfValidationError):
        retired.verify_proof(old_session_proof, "session", ORIGIN, **session_kwargs)
    retired.verify_proof(retired.issue_proof("session", ORIGIN, **session_kwargs), "session", ORIGIN, **session_kwargs)
    clock.advance(600)
    with pytest.raises(CsrfValidationError):
        rotated.verify_proof(old_session_proof, "session", ORIGIN, **session_kwargs)


def grant(clock):
    user = User(user_id=uuid4(), username="fictional.owner", display_name="Fictional owner", role=UserRole.OWNER, kind=IdentityKind.HUMAN, enabled=True, mfa_enabled=True, auth_version=1, created_at=clock())
    return SessionGrant(token=SecretStr(opaque()), user=user, session=SessionRecord(session_id=uuid4(), user_id=user.user_id, auth_version=1, created_at=clock(), mfa_verified_at=clock(), expires_at=clock() + timedelta(hours=8), factor="TOTP"))


def cookies(response):
    return [SimpleCookie(value) for value in response.headers.getlist("set-cookie")]


def test_cookie_attributes_deadlines_and_no_sliding_expiry(foundation):
    protector, _, clock = foundation
    preauth = protector.create_preauth(ORIGIN)
    mfa = protector.seal_mfa(preauth.value.get_secret_value(), opaque(), ORIGIN, challenge_expires_at=clock() + timedelta(seconds=300))
    session = grant(clock)
    assert opaque() not in repr(session)
    response = Response()
    set_preauth_cookie(response, preauth, now=clock())
    set_mfa_cookie(response, mfa, now=clock())
    set_session_cookie(response, session, now=clock())
    for parsed, name, age in zip(cookies(response), COOKIE_NAMES, (600, 300, 28800)):
        morsel = parsed[name]
        assert morsel["secure"] and morsel["httponly"]
        assert morsel["samesite"] == "strict" and morsel["path"] == "/" and not morsel["domain"]
        assert int(morsel["max-age"]) == age
        assert parsedate_to_datetime(morsel["expires"]) == clock() + timedelta(seconds=age)
    clock.advance(60)
    response = Response()
    set_session_cookie(response, session, now=clock())
    morsel = cookies(response)[0][COOKIE_NAMES[2]]
    assert int(morsel["max-age"]) == 28740
    assert parsedate_to_datetime(morsel["expires"]) == session.session.expires_at
    clock.advance(28740)
    with pytest.raises(CsrfValidationError):
        set_session_cookie(Response(), session, now=clock())


def test_cookie_clearing_preserves_protections():
    response = Response()
    clear_auth_cookies(response)
    for parsed, name in zip(cookies(response), COOKIE_NAMES):
        morsel = parsed[name]
        assert not morsel.value and morsel["max-age"] == "0"
        assert morsel["secure"] and morsel["httponly"] and morsel["samesite"] == "strict"
        assert morsel["path"] == "/" and not morsel["domain"]


def test_cookie_helpers_reject_expired_or_extended_values(foundation):
    protector, _, clock = foundation
    preauth = protector.create_preauth(ORIGIN)
    with pytest.raises(CsrfValidationError):
        set_preauth_cookie(Response(), replace(preauth, expires_at=clock() + timedelta(seconds=601)), now=clock())
    with pytest.raises(CsrfValidationError):
        set_mfa_cookie(Response(), preauth, now=clock())
    clock.advance(600)
    with pytest.raises(CsrfValidationError):
        set_preauth_cookie(Response(), preauth, now=clock())
