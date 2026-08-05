from __future__ import annotations

import json
from pathlib import Path

import pytest

import multiclaw.mcp.config as mcp_config_module
from multiclaw.mcp.config import _load_from_file, _parse_server_config, load_mcp_config
from multiclaw.mcp.types import HTTPServerConfig, StdioServerConfig


def _write_mcp_config(path: Path, servers: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


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


def test_load_mcp_config_allows_exact_same_key_secret_expansion_with_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".mcp.json"
    monkeypatch.setenv("API_TOKEN", "dummy-secret-token")
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "/usr/bin/env",
                        "env": {"API_TOKEN": "${API_TOKEN}"},
                        "sandboxEnvAllowlist": ["API_TOKEN"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = _load_from_file(config_path)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].env == {"API_TOKEN": "dummy-secret-token"}


def test_load_mcp_config_rejects_secret_expansion_without_allowlist_and_sanitizes_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = tmp_path / ".mcp.json"
    monkeypatch.setenv("API_TOKEN", "dummy-secret-token")
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "/usr/bin/env",
                        "env": {"API_TOKEN": "${API_TOKEN}"},
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
    assert "API_TOKEN" in caplog.text
    assert "dummy-secret-token" not in caplog.text


@pytest.mark.parametrize(
    "env_mapping",
    [
        {"VISIBLE_FLAG": "${API_TOKEN}", "API_TOKEN": "placeholder"},
        {"API_TOKEN": "prefix-${API_TOKEN}"},
        {"API_TOKEN": "${A}${B}"},
    ],
)
def test_load_mcp_config_rejects_laundered_or_composite_secret_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env_mapping: dict[str, str],
) -> None:
    config_path = tmp_path / ".mcp.json"
    monkeypatch.setenv("API_TOKEN", "dummy-secret-token")
    monkeypatch.setenv("A", "dummy-a")
    monkeypatch.setenv("B", "dummy-b")
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "/usr/bin/env",
                        "env": env_mapping,
                        "sandboxEnvAllowlist": ["API_TOKEN", "VISIBLE_FLAG", "A", "B"],
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
    assert "dummy-secret-token" not in caplog.text
    assert "dummy-a" not in caplog.text
    assert "dummy-b" not in caplog.text


def test_load_mcp_config_keeps_literal_env_values_without_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".mcp.json"
    monkeypatch.setenv("API_TOKEN", "dummy-secret-token")
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "/usr/bin/env",
                        "env": {"VISIBLE_FLAG": "literal-value"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = _load_from_file(config_path)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].env == {"VISIBLE_FLAG": "literal-value"}


def test_stdio_server_config_repr_redacts_env_values() -> None:
    config = StdioServerConfig(
        command="/usr/bin/env",
        env={"API_TOKEN": "dummy-secret-token", "VISIBLE_FLAG": "literal-value"},
    )

    rendered = repr(config)

    assert "dummy-secret-token" not in rendered
    assert "literal-value" not in rendered


def test_load_mcp_config_marks_auto_workspace_config_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / ".mcp.json"
    _write_mcp_config(config_path, {"demo": {"command": "/usr/bin/env"}})
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        mcp_config_module,
        "DEFAULT_CONFIG_PATHS",
        [tmp_path / "home" / ".mcp.json", Path(".mcp.json")],
    )

    loaded = load_mcp_config(search_parents=False, workspace_root=workspace)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].config_source == "auto_workspace"
    assert loaded["demo"].config_trust == "workspace_untrusted"


def test_load_mcp_config_marks_explicit_outside_path_trusted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "operator" / "outside.json"
    _write_mcp_config(outside, {"demo": {"command": "/usr/bin/env"}})

    loaded = load_mcp_config(path=outside, workspace_root=workspace)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].config_source == "explicit_path"
    assert loaded["demo"].config_trust == "trusted_operator"


def test_load_mcp_config_marks_explicit_workspace_path_untrusted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.json"
    _write_mcp_config(inside, {"demo": {"command": "/usr/bin/env"}})

    loaded = load_mcp_config(path=inside, workspace_root=workspace)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].config_trust == "workspace_untrusted"


def test_load_mcp_config_marks_both_symlink_directions_untrusted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_root = tmp_path / "operator"
    outside_root.mkdir()

    outside_target = outside_root / "outside.json"
    _write_mcp_config(outside_target, {"outside": {"command": "/usr/bin/env"}})
    workspace_link = workspace / "workspace-link.json"
    workspace_link.symlink_to(outside_target)

    inside_target = workspace / "inside.json"
    _write_mcp_config(inside_target, {"inside": {"command": "/usr/bin/env"}})
    outside_link = outside_root / "outside-link.json"
    outside_link.symlink_to(inside_target)

    workspace_loaded = load_mcp_config(path=workspace_link, workspace_root=workspace)
    outside_loaded = load_mcp_config(path=outside_link, workspace_root=workspace)

    assert workspace_loaded["outside"].config_trust == "workspace_untrusted"
    assert outside_loaded["inside"].config_trust == "workspace_untrusted"


def test_load_mcp_config_preserves_home_first_winner_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home_config = tmp_path / "home" / ".mcp.json"
    workspace_config = workspace / ".mcp.json"
    _write_mcp_config(home_config, {"demo": {"command": "/usr/bin/env"}})
    _write_mcp_config(workspace_config, {"demo": {"command": "/bin/echo"}})
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        mcp_config_module,
        "DEFAULT_CONFIG_PATHS",
        [home_config, Path(".mcp.json")],
    )

    loaded = load_mcp_config(search_parents=False, workspace_root=workspace)

    assert isinstance(loaded["demo"], StdioServerConfig)
    assert loaded["demo"].command == "/usr/bin/env"
    assert loaded["demo"].config_source == "auto_home"
    assert loaded["demo"].config_trust == "trusted_operator"


def test_load_mcp_config_rejects_any_template_in_untrusted_remote_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / ".mcp.json"
    monkeypatch.setenv("API_TOKEN", "dummy-secret-token")
    _write_mcp_config(
        config_path,
        {
            "demo": {
                "url": "https://example.com/${API_TOKEN}",
                "headers": {"Authorization": "Bearer ${API_TOKEN}"},
            }
        },
    )

    with caplog.at_level("WARNING"):
        loaded = load_mcp_config(path=config_path, workspace_root=workspace)

    assert loaded == {}
    assert "workspace_untrusted" in caplog.text
    assert "dummy-secret-token" not in caplog.text


def test_load_mcp_config_keeps_untrusted_remote_literal_config_allowed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / ".mcp.json"
    _write_mcp_config(
        config_path,
        {
            "demo": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer literal-token"},
            }
        },
    )

    loaded = load_mcp_config(path=config_path, workspace_root=workspace)

    assert isinstance(loaded["demo"], HTTPServerConfig)
    assert loaded["demo"].config_trust == "workspace_untrusted"
