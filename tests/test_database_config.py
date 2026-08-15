from pathlib import Path

import pytest
from pydantic import ValidationError

from multiclaw.config import Settings


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


def test_repository_example_configs_contain_no_credential_literals():
    forbidden = ("sk-", "xkeysib-", "re_")
    for path in (Path("multiclaw.toml"), Path("config/multiclaw.toml")):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path
