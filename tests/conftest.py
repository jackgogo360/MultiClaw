import os
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def clear_multiclaw_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MULTICLAW_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "MULTICLAW_TEST_MYSQL_URL",
        "MULTICLAW_SECRETS_KEYRING_B64",
        "MULTICLAW_AUTH_JWT_SIGNING_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def test_config_path(tmp_path):
    config_file = tmp_path / "multiclaw.toml"
    config_file.write_text("""
[app]
name = "TestApp"
version = "0.0.1"
debug = true

[database]
path = ":memory:"

[llm]
default_provider = "openai"
default_model = "gpt-4o-mini"

[llm.providers.openai]
api_key = "test-key"
base_url = "https://test.openai.com"

[llm.capability_tags]
"gpt-4o-mini" = ["text", "function_calling"]

[memory]
short_term_limit = 50
context_window_limit = 64000

[governance]
audit_enabled = false

[governance.sandbox]
mode = "auto"
backend_probe_on_startup = true
unsafe_fallback_requires_debug = true
write_protected_workspace_paths = [".git"]
read_hidden_workspace_paths = [".env", ".env.*"]
""")
    return config_file
