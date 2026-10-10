"""Explicit HTTPS test configuration and real password + TOTP session fixtures."""

import base64
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pyotp
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from pydantic import SecretStr

from webapp.auth_models import SessionCookiePolicy, UserRole
from webapp.auth_service import TrustedClientContext
from webapp.auth_settings import AuthSettings, CsrfSigningKey
from webapp.main import create_app


TEST_PASSWORD = "Fictional HTTPS regression test passphrase"


def settings_for(database_path, origin="https://testserver"):
    from urllib.parse import urlsplit

    return AuthSettings(
        database_path=Path(database_path).resolve(),
        allowed_hosts=(urlsplit(origin).hostname,), external_origins=(origin,),
        totp_keys=(SecretStr(Fernet.generate_key().decode("ascii")),),
        csrf_active_key_id="test-active",
        csrf_keys=(CsrfSigningKey(
            "test-active", SecretStr(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")),
        ),),
    )


def create_test_app(database_path=None, *, auth_settings=None):
    assert database_path is not None
    return create_app(database_path, auth_settings=auth_settings or settings_for(str(database_path) + ".auth.db"))


def grant_session(service, role=UserRole.OWNER):
    user = service.provision_identity(
        username="test." + uuid4().hex, display_name="Fictional HTTP test user",
        role=role, password=TEST_PASSWORD,
    )
    client = TrustedClientContext(str(user.user_id))
    challenge = service.begin_login(user.username, TEST_PASSWORD, client=client)
    token = challenge.token.get_secret_value()
    enrollment = service.begin_mfa_enrollment(token)
    grant = service.confirm_mfa_enrollment(
        token, pyotp.TOTP(enrollment.secret.get_secret_value()).at(service.clock()), client=client,
    )
    return grant.session


class AuthenticatedTestClient(TestClient):
    def __init__(self, app, *, role=UserRole.OWNER, **kwargs):
        super().__init__(app, base_url="https://testserver", **kwargs)
        self.role = role

    def __enter__(self):
        super().__enter__()
        self.grant = grant_session(self.app.state.auth_service, self.role)
        # Only tests inject a server-created token. No application cookie issuer.
        self.cookies.set(SessionCookiePolicy().name, self.grant.token.get_secret_value())
        return self


def local_tls_files(tmp_path):
    """Ephemeral self-signed loopback certificate, never a production credential."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost test only")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path
