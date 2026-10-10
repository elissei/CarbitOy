"""Explicit, fail-closed HTTP authentication configuration. No secret defaults."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr

from webapp.auth_service import AuthPolicy


class AuthConfigurationError(ValueError):
    """Configuration errors deliberately omit supplied values and secrets."""


@dataclass(frozen=True)
class AuthSettings:
    database_path: Path
    allowed_hosts: tuple[str, ...]
    external_origins: tuple[str, ...]
    totp_keys: tuple[SecretStr, ...] = field(repr=False)
    policy: AuthPolicy = field(default_factory=AuthPolicy)

    def __post_init__(self):
        if not isinstance(self.database_path, Path) or not self.database_path.is_absolute():
            raise AuthConfigurationError("authentication database must be an absolute path")
        static_path = Path(__file__).resolve().parent / "static"
        if self.database_path.resolve().is_relative_to(static_path):
            raise AuthConfigurationError("authentication database cannot be in static files")
        if not isinstance(self.policy, AuthPolicy):
            raise AuthConfigurationError("invalid authentication policy")
        if not isinstance(self.allowed_hosts, tuple) or not self.allowed_hosts:
            raise AuthConfigurationError("exact allowed hosts are required")
        for host in self.allowed_hosts:
            if not isinstance(host, str) or host != host.lower() or not host:
                raise AuthConfigurationError("invalid allowed host")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if len(host) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                ):
                    raise AuthConfigurationError("allowed hosts must be exact DNS names or IPs")
        if not isinstance(self.external_origins, tuple) or not self.external_origins:
            raise AuthConfigurationError("exact HTTPS external origins are required")
        for origin in self.external_origins:
            try:
                parsed = urlsplit(origin)
                port = parsed.port
                valid = (
                    parsed.scheme == "https" and parsed.hostname in self.allowed_hosts
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and origin == f"https://{parsed.netloc.lower()}"
                    and (port is None or (1 <= port <= 65535 and port != 443))
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise AuthConfigurationError("invalid HTTPS external origin")
        if not isinstance(self.totp_keys, tuple) or not self.totp_keys:
            raise AuthConfigurationError("TOTP encryption keys are required")
        try:
            for key in self.totp_keys:
                if not isinstance(key, SecretStr):
                    raise ValueError
                Fernet(key.get_secret_value().encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise AuthConfigurationError("invalid TOTP encryption key configuration") from None

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        names = ("CARBIT_AUTH_DATABASE", "CARBIT_ALLOWED_HOSTS", "CARBIT_EXTERNAL_ORIGINS", "CARBIT_TOTP_KEYS")
        if any(not os.environ.get(name, "").strip() for name in names):
            raise AuthConfigurationError("required authentication environment configuration is missing")
        return cls(
            database_path=Path(os.environ[names[0]]),
            allowed_hosts=tuple(os.environ[names[1]].split(",")),
            external_origins=tuple(os.environ[names[2]].split(",")),
            totp_keys=tuple(SecretStr(key) for key in os.environ[names[3]].split(",")),
        )
