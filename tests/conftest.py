import pytest
from pathlib import Path


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
sandbox_mode = "process"
audit_enabled = false
""")
    return config_file
