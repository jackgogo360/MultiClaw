from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.config.settings import AuthSettings
from multiclaw.auth.models import AuthConfigurationError, load_jwt_signing_key


def _contains_value(value, candidate):
    if candidate == value:
        return True
    if isinstance(candidate, dict):
        return any(_contains_value(value, item) for item in candidate.values())
    if isinstance(candidate, (list, tuple)):
        return any(_contains_value(value, item) for item in candidate)
    return False


def test_standalone_database_and_runtime_defaults():
    settings = Settings(_config_file="/nonexistent")

    assert settings.deployment.profile == "standalone"
    assert settings.database.driver == "sqlite"
    assert settings.database.url == "sqlite+aiosqlite:///data/multiclaw.db"
    assert settings.database.migration_mode == "validate"
    assert settings.database.sqlite_busy_timeout_ms == 5000
    assert settings.runtime.max_resident_tenants == 32
    assert settings.runtime.idle_ttl_seconds == 900
    assert settings.runtime.max_concurrent_runs_per_tenant == 2
    assert settings.workflow.heartbeat_ms == 5000
    assert settings.workflow.lease_ttl_ms == 20000
    assert settings.workflow.max_checkpoint_payload_bytes == 262144
    assert settings.deletion.retention_days == 7


@pytest.mark.parametrize("value", [-1, 31, "seven", 1.5, True])
def test_deletion_retention_rejects_out_of_contract_values(value):
    with pytest.raises(ValidationError):
        Settings(_config_file="/nonexistent", deletion={"retention_days": value})


@pytest.mark.parametrize(
    ("driver", "url"),
    [
        ("sqlite", "mysql+aiomysql://db/app"),
        ("mysql", "sqlite+aiosqlite:///app.db"),
    ],
)
def test_database_driver_must_match_url(driver, url):
    with pytest.raises(ValidationError, match="database.driver.*database.url"):
        Settings(
            _config_file="/nonexistent",
            database={"driver": driver, "url": url},
        )


def test_workflow_lease_ttl_must_be_at_least_three_times_heartbeat():
    with pytest.raises(
        ValidationError,
        match="workflow.lease_ttl_ms must be at least 3x heartbeat_ms",
    ):
        Settings(
            _config_file="/nonexistent",
            workflow={"heartbeat_ms": 5000, "lease_ttl_ms": 14999},
        )


def test_env_only_secrets_are_excluded_from_model_dump(monkeypatch):
    keyring_secret = "sentinel-keyring-secret"
    jwt_secret = "sentinel-jwt-signing-secret"
    monkeypatch.setenv("MULTICLAW_SECRETS_KEYRING_B64", keyring_secret)
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", jwt_secret)

    settings = Settings(_config_file="/nonexistent")
    dumped = settings.model_dump()

    assert "keyring_b64" not in dumped.get("secrets", {})
    assert "jwt_signing_key" not in dumped.get("auth", {})
    assert not _contains_value(keyring_secret, dumped)
    assert not _contains_value(jwt_secret, dumped)


def test_repository_example_configs_contain_no_credential_literals():
    forbidden = ("sk-", "xkeysib-", "re_")
    for path in (Path("multiclaw.toml"), Path("config/multiclaw.toml")):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path


def test_auth_settings_no_longer_exposes_legacy_jwt_secret():
    assert "jwt_secret" not in AuthSettings.model_fields


def test_load_jwt_signing_key_accepts_env_only_source():
    key = load_jwt_signing_key(
        SimpleNamespace(auth=SimpleNamespace(jwt_signing_key_file="")),
        environ={"MULTICLAW_AUTH_JWT_SIGNING_KEY": "x" * 32},
    )

    assert key == b"x" * 32


def test_load_jwt_signing_key_accepts_file_only_source(tmp_path):
    key_file = tmp_path / "jwt.key"
    key_file.write_bytes(b"y" * 32)
    key_file.chmod(0o600)

    key = load_jwt_signing_key(
        SimpleNamespace(auth=SimpleNamespace(jwt_signing_key_file=str(key_file))),
        environ={},
    )

    assert key == b"y" * 32


@pytest.mark.parametrize(
    ("environ", "path"),
    [
        ({}, ""),
        ({"MULTICLAW_AUTH_JWT_SIGNING_KEY": "x" * 32}, "configured"),
    ],
)
def test_load_jwt_signing_key_requires_exactly_one_source(tmp_path, environ, path):
    key_file = tmp_path / "jwt.key"
    key_file.write_bytes(b"z" * 32)
    key_file.chmod(0o600)
    source_path = str(key_file) if path else ""

    with pytest.raises(AuthConfigurationError, match="exactly one"):
        load_jwt_signing_key(
            SimpleNamespace(auth=SimpleNamespace(jwt_signing_key_file=source_path)),
            environ=environ,
        )


@pytest.mark.parametrize(
    ("environ", "path_bytes"),
    [
        ({"MULTICLAW_AUTH_JWT_SIGNING_KEY": "short"}, None),
        ({}, b"short"),
    ],
)
def test_load_jwt_signing_key_rejects_sources_shorter_than_32_bytes(tmp_path, environ, path_bytes):
    path = ""
    if path_bytes is not None:
        key_file = tmp_path / "jwt.key"
        key_file.write_bytes(path_bytes)
        key_file.chmod(0o600)
        path = str(key_file)

    with pytest.raises(AuthConfigurationError, match="at least 32 bytes"):
        load_jwt_signing_key(
            SimpleNamespace(auth=SimpleNamespace(jwt_signing_key_file=path)),
            environ=environ,
        )
