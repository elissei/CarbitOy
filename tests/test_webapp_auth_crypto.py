"""Fictional credentials and ephemeral keys only."""

from uuid import uuid4

import pyotp
import pytest
from argon2 import PasswordHasher, Type
from cryptography.fernet import Fernet, InvalidToken

from webapp.auth_crypto import (
    CryptoBusyError, PasswordService, TotpCipher, matching_totp_step,
    new_recovery_codes, recovery_digest, token_digest,
)
from webapp.auth_models import SessionCookiePolicy


@pytest.fixture(scope="module")
def passwords():
    return PasswordService()


def test_argon2id_random_salt_and_verification(passwords):
    password = "Fictional test passphrase 4E2"
    first, second = passwords.hash_password(password), passwords.hash_password(password)
    assert first.startswith("$argon2id$v=19$m=65536,t=3,p=2$")
    assert first != second
    assert passwords.verify(first, password)
    assert not passwords.verify(first, "Wrong fictional test password")
    assert not passwords.verify(None, password)
    assert not passwords.verify("not an Argon2id hash", password)


def test_rehash_detects_old_library_parameters(passwords):
    password = "Fictional old-policy test password"
    old_hash = PasswordHasher(type=Type.ID, memory_cost=8192, time_cost=1, parallelism=1).hash(password)
    assert passwords.verify(old_hash, password)
    assert passwords.check_needs_rehash(old_hash)
    assert not passwords.check_needs_rehash(passwords.hash_password(password))


@pytest.mark.parametrize("password", ["short", "a" * 257, None, "😀" * 256 + "x"])
def test_password_bounds(passwords, password):
    with pytest.raises(ValueError):
        passwords.hash_password(password)
    assert not passwords.verify(None, password)


def test_password_processing_is_bounded(passwords):
    assert passwords._slots.acquire(blocking=False)
    assert passwords._slots.acquire(blocking=False)
    try:
        with pytest.raises(CryptoBusyError):
            passwords.hash_password("Fictional bounded test password")
        with pytest.raises(CryptoBusyError):
            passwords.verify(None, "Fictional bounded test password")
    finally:
        passwords._slots.release()
        passwords._slots.release()


def test_fernet_encrypts_binds_user_and_supports_rotation():
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old = TotpCipher((old_key,))
    user_id, secret = str(uuid4()), pyotp.random_base32()
    ciphertext = old.encrypt(user_id, secret)
    assert secret not in ciphertext
    assert old.decrypt(user_id, ciphertext) == secret
    with pytest.raises(InvalidToken):
        old.decrypt(str(uuid4()), ciphertext)
    with pytest.raises(InvalidToken):
        TotpCipher((new_key,)).decrypt(user_id, ciphertext)
    rotated = TotpCipher((new_key, old_key)).rotate(ciphertext)
    assert TotpCipher((new_key,)).decrypt(user_id, rotated) == secret
    with pytest.raises(ValueError):
        TotpCipher(())


def test_recovery_codes_are_random_and_hashes_are_user_bound():
    codes = new_recovery_codes()
    assert len(set(codes)) == 10
    first_id, second_id = str(uuid4()), str(uuid4())
    digest = recovery_digest(first_id, codes[0])
    assert digest is not None and len(digest) == 64 and codes[0] not in digest
    assert recovery_digest(first_id, codes[0].upper().replace("-", "")) == digest
    assert recovery_digest(second_id, codes[0]) != digest
    assert recovery_digest(first_id, "invalid code") is None
    assert len(token_digest("ephemeral test token")) == 64


def test_totp_window_and_replay_counter():
    secret, now = pyotp.random_base32(), 1800000000
    code = pyotp.TOTP(secret).at(now)
    assert matching_totp_step(secret, code, now, None) == now // 30
    assert matching_totp_step(secret, code, now, now // 30) is None
    assert matching_totp_step(secret, pyotp.TOTP(secret).at(now - 30), now, None) == now // 30 - 1
    assert matching_totp_step(secret, pyotp.TOTP(secret).at(now - 90), now, None) is None
    assert matching_totp_step(secret, "１２３４５６", now, None) is None


def test_cookie_policy_cannot_disable_browser_protections():
    assert SessionCookiePolicy().options() == {
        "httponly": True, "secure": True, "samesite": "strict", "path": "/",
    }
    with pytest.raises(ValueError):
        SessionCookiePolicy(name="insecure")
    with pytest.raises(ValueError):
        SessionCookiePolicy(secure=False)
