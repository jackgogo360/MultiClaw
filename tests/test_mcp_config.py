from __future__ import annotations

import json
from pathlib import Path

import pytest

from multiclaw.mcp.config import _load_from_file, _parse_server_config
from multiclaw.mcp.types import StdioServerConfig


def test_parse_stdio_server_config_supports_aliases_and_conservative_defaults() -> None:
    config = _parse_server_config(
        {
            "command": "/usr/bin/env",
            "args": ["python", "-m", "demo"],
            "env": {"VISIBLE_FLAG": "1"},
            "cwd": "/tmp",
            "sandboxNetwork": "inherit",
            "sandbox_workspace": "rw",
            "sandboxAllowSubprocesses": True,
            "sandbox_env_allowlist": ["VISIBLE_FLAG"],
            "sandboxReadOnlyPaths": ["/usr/local/bin", "/opt/homebrew/bin"],
        }
    )

    assert isinstance(config, StdioServerConfig)
    assert config.command == "/usr/bin/env"
    assert config.args == ["python", "-m", "demo"]
    assert config.env == {"VISIBLE_FLAG": "1"}
    assert config.cwd == Path("/tmp")
    assert config.sandbox_network == "inherit"
    assert config.sandbox_workspace == "rw"
    assert config.sandbox_allow_subprocesses is True
    assert config.sandbox_env_allowlist == ["VISIBLE_FLAG"]
    assert config.sandbox_read_only_paths == [
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]

    defaulted = _parse_server_config({"command": "/usr/bin/env"})
    assert isinstance(defaulted, StdioServerConfig)
    assert defaulted.cwd is None
    assert defaulted.sandbox_network == "disabled"
    assert defaulted.sandbox_workspace == "ro"
    assert defaulted.sandbox_allow_subprocesses is False
    assert defaulted.sandbox_env_allowlist == []
    assert defaulted.sandbox_read_only_paths == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"command": "/usr/bin/env", "sandboxAllowSubprocesses": 1}, "sandbox_allow_subprocesses"),
        ({"command": "/usr/bin/env", "sandboxWorkspace": "unsafe"}, "sandbox_workspace"),
        ({"command": "/usr/bin/env", "sandboxEnvAllowlist": "TOKEN"}, "sandbox_env_allowlist"),
        ({"command": "/usr/bin/env", "sandboxReadOnlyPaths": ["/ok", 7]}, "sandbox_read_only_paths"),
    ],
)
def test_parse_stdio_server_config_rejects_invalid_types(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_server_config(payload)


def test_load_mcp_config_logs_server_and_key_without_secret_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = tmp_path / ".mcp.json"
    secret_value = "super-secret-token"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "/usr/bin/env",
                        "env": {"API_TOKEN": secret_value},
                        "sandboxEnvAllowlist": "API_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        loaded = _load_from_file(config_path)

    assert loaded == {}
    assert "demo" in caplog.text
    assert "sandbox_env_allowlist" in caplog.text
    assert secret_value not in caplog.text
