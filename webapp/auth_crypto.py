"""Library-backed crypto. Keys come from server configuration, never the DB."""

import hashlib
import json
import re
import secrets
from threading import BoundedSemaphore

import pyotp
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoBusyError(RuntimeError):
    pass


class PasswordService:
    def __init__(self) -> None:
        # 64 MiB / 3 iterations; two lanes for the target's two CPU cores.
        self._hasher = PasswordHasher(
            type=Type.ID, memory_cost=65536, time_cost=3, parallelism=2,
            hash_len=32, salt_len=16,
        )
        self._slots = BoundedSemaphore(2)
        self._dummy_hash = self.hash_password(secrets.token_urlsafe(32))

    @staticmethod
    def validate_password(password: str) -> None:
        if not isinstance(password, str) or not 15 <= len(password) <= 256:
            raise ValueError("password must contain 15..256 characters")
        if len(password.encode("utf-8")) > 1024:
            raise ValueError("password is too large")

    def hash_password(self, password: str) -> str:
        self.validate_password(password)
        if not self._slots.acquire(blocking=False):
            raise CryptoBusyError("password processing is busy")
        try:
            return self._hasher.hash(password)
        finally:
            self._slots.release()

    def check_needs_rehash(self, stored_hash: str) -> bool:
        """Check library parameters only after successful password verification."""
        return self._hasher.check_needs_rehash(stored_hash)

    def verify(self, stored_hash: str | None, password: str) -> bool:
        try:
            self.validate_password(password)
        except (ValueError, UnicodeError):
            return False
        if not self._slots.acquire(blocking=False):
            raise CryptoBusyError("password processing is busy")
        try:
            candidate = stored_hash or self._dummy_hash
            if not candidate.startswith("$argon2id$"):
                return False
            try:
                verified = self._hasher.verify(candidate, password)
                return verified and stored_hash is not None
            except (VerificationError, InvalidHashError):
                return False
        finally:
            self._slots.release()


class TotpCipher:
    def __init__(self, keys: tuple[bytes, ...]) -> None:
        if not keys:
            raise ValueError("a server-held MFA encryption key is required")
        self._cipher = MultiFernet([Fernet(key) for key in keys])

    def encrypt(self, user_id: str, secret: str) -> str:
        payload = json.dumps({"user_id": user_id, "purpose": "carbit-totp-v1", "secret": secret})
        return self._cipher.encrypt(payload.encode()).decode("ascii")

    def decrypt(self, user_id: str, ciphertext: str) -> str:
        payload = json.loads(self._cipher.decrypt(ciphertext.encode("ascii")))
        if payload.get("user_id") != user_id or payload.get("purpose") != "carbit-totp-v1":
            raise InvalidToken
        return payload["secret"]

    def rotate(self, ciphertext: str) -> str:
        return self._cipher.rotate(ciphertext.encode("ascii")).decode("ascii")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def recovery_digest(user_id: str, code: str) -> str | None:
    if not isinstance(code, str) or len(code) > 40:
        return None
    normalized = code.lower().replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        return None
    # SHA-256 is suitable for uniformly random 128-bit secrets, not passwords.
    return hashlib.sha256(("carbit-recovery-v1\0" + user_id + "\0" + normalized).encode()).hexdigest()


def new_recovery_codes() -> tuple[str, ...]:
    codes = []
    for _ in range(10):
        code = secrets.token_hex(16)
        codes.append("-".join(code[index:index + 8] for index in range(0, 32, 8)))
    return tuple(codes)


def matching_totp_step(secret: str, code: str, now: int, last_step: int | None) -> int | None:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        return None
    totp = pyotp.TOTP(secret)
    # One adjacent step tolerates small clock skew; replay counter persists in SQLite.
    for offset in (0, -1, 1):
        step = now // 30 + offset
        if step >= 0 and totp.verify(code, for_time=step * 30, valid_window=0):
            return step if last_step is None or step > last_step else None
    return None
