import os
from multiclaw.config.settings import Settings, AppSettings, DatabaseSettings, LLMSettings, MemorySettings, GovernanceSettings


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

        assert settings.governance.sandbox_mode == "process"
        assert settings.governance.audit_enabled is False

    def test_env_var_override(self, test_config_path, monkeypatch):
        monkeypatch.setenv("MULTICLAW_APP__NAME", "EnvApp")
        settings = Settings(_config_file=str(test_config_path))

        assert settings.app.name == "EnvApp"

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
sandbox_mode = "docker"
audit_enabled = true
""")
        monkeypatch.chdir(tmp_path)
        settings = Settings()

        assert settings.app.name == "DefaultApp"
