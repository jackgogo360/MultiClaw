import os
import pytest
from pydantic import ValidationError
from multiclaw.config.settings import Settings


def write_config(tmp_path, text):
    config_file = tmp_path / "multiclaw.toml"
    config_file.write_text(text)
    return config_file


class TestSettings:
    def test_loads_from_toml_file(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.app.name == "TestApp"
        assert settings.app.version == "0.0.1"
        assert settings.app.debug is True

    def test_database_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.database.path == ":memory:"

    def test_llm_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.llm.default_provider == "openai"
        assert settings.llm.default_model == "gpt-4o-mini"
        assert settings.llm.providers["openai"]["api_key"] == "test-key"
        assert settings.llm.capability_tags["gpt-4o-mini"] == ["text", "function_calling"]

    def test_memory_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.memory.short_term_limit == 50
        assert settings.memory.context_window_limit == 64000

    def test_governance_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.governance.sandbox.mode == "auto"
        assert settings.governance.sandbox.backend_probe_on_startup is True
        assert settings.governance.sandbox.unsafe_fallback_requires_debug is True
        assert settings.governance.sandbox.write_protected_workspace_paths == [".git"]
        assert settings.governance.sandbox.read_hidden_workspace_paths == [".env", ".env.*"]
        assert settings.governance.sandbox.profiles.shell == "shell_workspace"
        assert settings.governance.sandbox.profiles.code_exec == "code_exec_python"
        assert settings.governance.sandbox.profiles.mcp_stdio == "mcp_stdio_local"
        assert settings.governance.audit_enabled is False

    def test_governance_sandbox_defaults_without_config(self):
        settings = Settings(_config_file="/nonexistent")

        assert settings.governance.sandbox.mode == "auto"
        assert settings.governance.sandbox.profiles.shell == "shell_workspace"
        assert settings.governance.sandbox.profiles.code_exec == "code_exec_python"
        assert settings.governance.sandbox.profiles.mcp_stdio == "mcp_stdio_local"

    def test_legacy_process_sandbox_mode_warns_and_maps_to_auto(self, tmp_path):
        config_file = write_config(
            tmp_path,
            """
[governance]
sandbox_mode = "process"
""",
        )

        with pytest.warns(
            DeprecationWarning,
            match=r"sandbox_mode.*process",
        ):
            settings = Settings(_config_file=str(config_file))

        assert settings.governance.sandbox.mode == "auto"

    @pytest.mark.parametrize("legacy_mode", ["docker", "unsupported"])
    def test_legacy_unsupported_sandbox_modes_are_rejected(self, tmp_path, legacy_mode):
        config_file = write_config(
            tmp_path,
            f"""
[governance]
sandbox_mode = "{legacy_mode}"
""",
        )

        with pytest.raises(ValidationError, match=legacy_mode):
            Settings(_config_file=str(config_file))

    def test_legacy_and_nested_sandbox_config_cannot_be_combined(self, tmp_path):
        config_file = write_config(
            tmp_path,
            """
[governance]
sandbox_mode = "process"

[governance.sandbox]
mode = "auto"
""",
        )

        with pytest.raises(ValidationError, match="cannot be combined"):
            Settings(_config_file=str(config_file))

    def test_unsafe_mode_requires_debug(self, tmp_path):
        config_file = write_config(
            tmp_path,
            """
[app]
debug = false

[governance.sandbox]
mode = "host_unsafe_dev_only"
""",
        )

        with pytest.raises(ValidationError, match="app.debug"):
            Settings(_config_file=str(config_file))

    def test_unsafe_fallback_requires_debug_must_remain_true(self, tmp_path):
        config_file = write_config(
            tmp_path,
            """
[governance.sandbox]
unsafe_fallback_requires_debug = false
""",
        )

        with pytest.raises(ValidationError, match="unsafe_fallback_requires_debug"):
            Settings(_config_file=str(config_file))

    def test_nested_env_overrides_win_over_toml(self, tmp_path, monkeypatch):
        config_file = write_config(
            tmp_path,
            """
[app]
debug = true

[governance.sandbox]
mode = "auto"
backend_probe_on_startup = false

[governance.sandbox.profiles]
shell = "toml_shell"
code_exec = "toml_code_exec"
""",
        )
        monkeypatch.setenv("MULTICLAW_GOVERNANCE__SANDBOX__BACKEND_PROBE_ON_STARTUP", "true")
        monkeypatch.setenv("MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__SHELL", "env_shell")
        monkeypatch.setenv("MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__MCP_STDIO", "env_mcp")

        settings = Settings(_config_file=str(config_file))

        assert settings.governance.sandbox.backend_probe_on_startup is True
        assert settings.governance.sandbox.profiles.shell == "env_shell"
        assert settings.governance.sandbox.profiles.code_exec == "toml_code_exec"
        assert settings.governance.sandbox.profiles.mcp_stdio == "env_mcp"

    def test_nested_env_override_decodes_write_protected_workspace_paths_json_array(self, tmp_path, monkeypatch):
        config_file = write_config(
            tmp_path,
            """
[governance.sandbox]
write_protected_workspace_paths = [".git"]
""",
        )
        monkeypatch.setenv(
            "MULTICLAW_GOVERNANCE__SANDBOX__WRITE_PROTECTED_WORKSPACE_PATHS",
            '[".git", ".venv"]',
        )

        settings = Settings(_config_file=str(config_file))

        assert settings.governance.sandbox.write_protected_workspace_paths == [".git", ".venv"]

    def test_nested_env_override_decodes_read_hidden_workspace_paths_json_array(self, tmp_path, monkeypatch):
        config_file = write_config(
            tmp_path,
            """
[governance.sandbox]
read_hidden_workspace_paths = [".env", ".env.*"]
""",
        )
        monkeypatch.setenv(
            "MULTICLAW_GOVERNANCE__SANDBOX__READ_HIDDEN_WORKSPACE_PATHS",
            '[".env", ".env.local"]',
        )

        settings = Settings(_config_file=str(config_file))

        assert settings.governance.sandbox.read_hidden_workspace_paths == [".env", ".env.local"]

    def test_auto_mode_allows_backend_probe_to_be_disabled(self, tmp_path):
        config_file = write_config(
            tmp_path,
            """
[governance.sandbox]
mode = "auto"
backend_probe_on_startup = false
""",
        )

        settings = Settings(_config_file=str(config_file))

        assert settings.governance.sandbox.mode == "auto"
        assert settings.governance.sandbox.backend_probe_on_startup is False

    def test_feature_flags_default_disabled(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.agent.resilience_enabled is False
        assert settings.tools.parallel_read_only_enabled is False
        assert settings.memory.progressive_context_enabled is False
        assert settings.tools.web_fetch_allow_private_networks is False

    def test_env_var_override(self, test_config_path, monkeypatch):
        monkeypatch.setenv("MULTICLAW_APP__NAME", "EnvApp")
        settings = Settings(_config_file=str(test_config_path))

        assert settings.app.name == "EnvApp"

    def test_feature_flags_env_var_override(self, test_config_path, monkeypatch):
        monkeypatch.setenv("MULTICLAW_AGENT__RESILIENCE_ENABLED", "true")
        monkeypatch.setenv("MULTICLAW_TOOLS__PARALLEL_READ_ONLY_ENABLED", "true")
        monkeypatch.setenv("MULTICLAW_TOOLS__WEB_FETCH_ALLOW_PRIVATE_NETWORKS", "true")
        monkeypatch.setenv("MULTICLAW_MEMORY__PROGRESSIVE_CONTEXT_ENABLED", "true")

        settings = Settings(_config_file=str(test_config_path))

        assert settings.agent.resilience_enabled is True
        assert settings.tools.parallel_read_only_enabled is True
        assert settings.tools.web_fetch_allow_private_networks is True
        assert settings.memory.progressive_context_enabled is True

    def test_tools_settings_load_from_toml_mapping(self, tmp_path):
        config_file = tmp_path / "multiclaw.toml"
        config_file.write_text("""
[tools]
parallel_read_only_enabled = true
parallel_max_concurrency = 8
web_fetch_allow_private_networks = true
""")

        settings = Settings(_config_file=str(config_file))

        assert settings.tools.parallel_read_only_enabled is True
        assert settings.tools.parallel_max_concurrency == 8
        assert settings.tools.web_fetch_allow_private_networks is True

    @pytest.mark.parametrize(
        ("config_text", "expected_field"),
        [
            (
                """
[agent]
no_progress_repeat_limit = 1
""",
                "no_progress_repeat_limit",
            ),
            (
                """
[tools]
parallel_max_concurrency = 17
""",
                "parallel_max_concurrency",
            ),
            (
                """
[memory]
context_l1_ratio = 1.0
""",
                "context_l1_ratio",
            ),
            (
                """
[agent]
reflection_max_attempts = 4
""",
                "reflection_max_attempts",
            ),
            (
                """
[memory]
context_response_reserve_tokens = 255
""",
                "context_response_reserve_tokens",
            ),
        ],
    )
    def test_feature_flag_related_bounds_are_validated(self, tmp_path, config_text, expected_field):
        config_file = tmp_path / "multiclaw.toml"
        config_file.write_text(config_text)

        with pytest.raises(ValidationError) as exc_info:
            Settings(_config_file=str(config_file))

        assert expected_field in str(exc_info.value)

    def test_default_config_path_fallback(self, monkeypatch, tmp_path):
        default_config = tmp_path / "multiclaw.toml"
        default_config.write_text("""
[app]
name = "DefaultApp"
version = "9.9.9"
debug = false

[database]
path = "default.db"

[llm]
default_provider = "anthropic"
default_model = "claude-sonnet-4-6"

[llm.providers.anthropic]
api_key = ""
base_url = "https://api.anthropic.com"

[llm.capability_tags]
"claude-sonnet-4-6" = ["text", "function_calling"]

[memory]
short_term_limit = 100
context_window_limit = 128000

[governance]
audit_enabled = true

[governance.sandbox]
mode = "auto"
backend_probe_on_startup = true
unsafe_fallback_requires_debug = true
write_protected_workspace_paths = [".git"]
read_hidden_workspace_paths = [".env", ".env.*"]
""")
        monkeypatch.chdir(tmp_path)
        settings = Settings()

        assert settings.app.name == "DefaultApp"
