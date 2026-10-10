"""Explicit CSRF key provisioning. All key material here is test-only."""

import base64
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from http_auth_support import settings_for
from webapp.auth_settings import AuthConfigurationError, AuthSettings, CsrfSigningKey
from webapp.main import create_app


def encoded(size=32, byte=91):
    return base64.urlsafe_b64encode(bytes([byte]) * size).decode("ascii")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    settings = settings_for(tmp_path / "auth.db")
    values = {
        "CARBIT_AUTH_DATABASE": str(settings.database_path),
        "CARBIT_ALLOWED_HOSTS": "testserver", "CARBIT_EXTERNAL_ORIGINS": "https://testserver",
        "CARBIT_TOTP_KEYS": settings.totp_keys[0].get_secret_value(),
        "CARBIT_CSRF_ACTIVE_KEY_ID": settings.csrf_active_key_id,
        "CARBIT_CSRF_KEYS": json.dumps([{"id": item.key_id, "key": item.secret.get_secret_value()} for item in settings.csrf_keys]),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return settings


def test_valid_environment_and_secret_masking(environment):
    parsed = AuthSettings.from_environment()
    assert parsed == environment
    for item in parsed.csrf_keys:
        assert item.secret.get_secret_value() not in repr(parsed)
        assert item.secret.get_secret_value() not in repr(item)
        assert len(item.key_bytes()) == 32


@pytest.mark.parametrize("identifier", ["", None, [], "a.b", " space", "a" * 33, "ä", "\n"])
def test_invalid_key_ids(identifier):
    with pytest.raises(AuthConfigurationError):
        CsrfSigningKey(identifier, SecretStr(encoded()))


@pytest.mark.parametrize("material", [
    "", "fictional-bad-key", encoded(16), encoded(31), encoded(33), encoded().rstrip("="),
    encoded() + "=", encoded() + "\n", encoded().replace("=", " "), "ä" * 44,
    # Alternate pad bits decode to the same bytes but are not canonical.
    encoded()[:-2] + "t=",
])
def test_invalid_key_material_does_not_leak(material):
    with pytest.raises(AuthConfigurationError) as error:
        CsrfSigningKey("test", SecretStr(material))
    if material:
        assert material not in str(error.value)


def test_key_requires_secret_wrapper():
    with pytest.raises(AuthConfigurationError):
        CsrfSigningKey("test", encoded())


@pytest.mark.parametrize("changes", [
    {"csrf_keys": ()}, {"csrf_keys": []}, {"csrf_keys": ("wrong",)},
    {"csrf_active_key_id": ""}, {"csrf_active_key_id": None}, {"csrf_active_key_id": "missing"},
    {"csrf_keys": tuple(CsrfSigningKey(str(index), SecretStr(encoded(byte=91 + index))) for index in range(4))},
])
def test_missing_active_or_unbounded_key_set_rejected(tmp_path, changes):
    with pytest.raises(AuthConfigurationError):
        replace(settings_for(tmp_path / "auth.db"), **changes)


@pytest.mark.parametrize("same", ["id", "material"])
def test_duplicate_keys_rejected(tmp_path, same):
    first = CsrfSigningKey("active", SecretStr(encoded()))
    second = CsrfSigningKey("active" if same == "id" else "previous", SecretStr(encoded(byte=92) if same == "id" else encoded()))
    with pytest.raises(AuthConfigurationError):
        replace(settings_for(tmp_path / "auth.db"), csrf_active_key_id="active", csrf_keys=(first, second))


def test_totp_key_reuse_rejected(tmp_path):
    settings = settings_for(tmp_path / "auth.db")
    with pytest.raises(AuthConfigurationError, match="independently"):
        replace(settings, csrf_active_key_id="active", csrf_keys=(CsrfSigningKey("active", settings.totp_keys[0]),))


def test_two_previous_keys_allowed_without_order_selecting_active(tmp_path):
    keys = tuple(CsrfSigningKey(str(index), SecretStr(encoded(byte=91 + index))) for index in range(3))
    settings = replace(settings_for(tmp_path / "auth.db"), csrf_keys=keys, csrf_active_key_id="1")
    assert settings.csrf_active_key_id == "1"


@pytest.mark.parametrize("name", ["CARBIT_CSRF_ACTIVE_KEY_ID", "CARBIT_CSRF_KEYS"])
@pytest.mark.parametrize("missing", [None, "", " "])
def test_missing_csrf_configuration_fails_before_database_creation(environment, monkeypatch, tmp_path, name, missing):
    if missing is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, missing)
    with pytest.raises(AuthConfigurationError):
        with TestClient(create_app(tmp_path / "uncreated" / "works.db")):
            pass
    assert not environment.database_path.exists()
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("value", [
    "not-json", "{}", "[]", "null", "[null]", '[{"id":"active"}]',
    '[{"id":"active","key":123}]', '[{"id":"active","key":"invalid","extra":true}]',
    '[{"id":"active","id":"active","key":"invalid"}]', "[" * 2049, "[" * 1024 + "]" * 1024,
])
def test_invalid_or_ambiguous_environment_json(environment, monkeypatch, value):
    monkeypatch.setenv("CARBIT_CSRF_KEYS", value)
    with pytest.raises(AuthConfigurationError) as error:
        AuthSettings.from_environment()
    assert value not in str(error.value)


def test_rotation_configuration_uniqueness_and_active_presence(environment, monkeypatch):
    entries = [{"id": "old", "key": encoded()}, {"id": "new", "key": encoded(byte=92)}]
    monkeypatch.setenv("CARBIT_CSRF_ACTIVE_KEY_ID", "new")
    monkeypatch.setenv("CARBIT_CSRF_KEYS", json.dumps(entries))
    assert AuthSettings.from_environment().csrf_active_key_id == "new"
    for bad_entries in (entries + [entries[0]], [entries[0]], [entries[0], {"id": "new", "key": encoded()}]):
        monkeypatch.setenv("CARBIT_CSRF_KEYS", json.dumps(bad_entries))
        with pytest.raises(AuthConfigurationError):
            AuthSettings.from_environment()
