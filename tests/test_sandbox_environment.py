from pathlib import Path

import pytest


def test_build_sandbox_environment_scrubs_host_values_and_creates_private_root(
    tmp_path: Path,
) -> None:
    from multiclaw.governance import build_sandbox_environment

    environment = build_sandbox_environment(
        base_env={
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "HOME": "/Users/felix",
            "TMPDIR": "/var/folders/tmp",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "OPENAI_API_KEY": "masked",
            "COLORTERM": "truecolor",
        },
        overrides={"custom_flag": "1"},
        allowed_secret_keys=frozenset(),
        temp_root=tmp_path,
        default_path="/usr/bin:/bin",
    )

    env = environment.env

    assert environment.private_root.parent == tmp_path
    assert environment.private_root.name.startswith("launch-")
    assert oct(environment.private_root.stat().st_mode & 0o777) == "0o700"
    assert environment.home == environment.private_root / "home"
    assert environment.tmp == environment.private_root / "tmp"
    assert environment.home.is_dir()
    assert environment.tmp.is_dir()
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["TERM"] == "xterm-256color"
    assert env["USER"] == "sandbox"
    assert env["LOGNAME"] == "sandbox"
    assert env["SHELL"] == "/bin/sh"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == str(environment.home)
    assert env["TMPDIR"] == str(environment.tmp)
    assert env["XDG_CONFIG_HOME"] == str(environment.private_root / "xdg" / "config")
    assert env["XDG_CACHE_HOME"] == str(environment.private_root / "xdg" / "cache")
    assert env["XDG_DATA_HOME"] == str(environment.private_root / "xdg" / "data")
    assert env["XDG_STATE_HOME"] == str(environment.private_root / "xdg" / "state")
    assert env["XDG_RUNTIME_DIR"] == str(environment.private_root / "xdg" / "runtime")
    assert env["CUSTOM_FLAG"] == "1"
    assert "HOME" in env
    assert "COLORTERM" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_sandbox_environment_rejects_secret_override_without_allowlist(
    tmp_path: Path,
) -> None:
    from multiclaw.governance import SandboxPolicyError, build_sandbox_environment

    with pytest.raises(SandboxPolicyError, match="OPENAI_API_KEY"):
        build_sandbox_environment(
            base_env={},
            overrides={"openai_api_key": "masked"},
            allowed_secret_keys=frozenset(),
            temp_root=tmp_path,
            default_path="/usr/bin:/bin",
        )


def test_build_sandbox_environment_allows_explicit_secret_override_allowlist(
    tmp_path: Path,
) -> None:
    from multiclaw.governance import build_sandbox_environment

    environment = build_sandbox_environment(
        base_env={},
        overrides={"openai_api_key": "masked"},
        allowed_secret_keys=frozenset({"OPENAI_API_KEY"}),
        temp_root=tmp_path,
        default_path="/usr/bin:/bin",
    )

    assert environment.env["OPENAI_API_KEY"] == "masked"
    assert "openai_api_key" not in environment.env


@pytest.mark.parametrize(
    "key",
    [
        "HOME",
        "TMPDIR",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "XDG_RUNTIME_DIR",
    ],
)
def test_build_sandbox_environment_rejects_runtime_owned_overrides(
    tmp_path: Path,
    key: str,
) -> None:
    from multiclaw.governance import SandboxPolicyError, build_sandbox_environment

    with pytest.raises(SandboxPolicyError, match=key):
        build_sandbox_environment(
            base_env={},
            overrides={key.lower(): "/tmp/override"},
            allowed_secret_keys=frozenset({key}),
            temp_root=tmp_path,
            default_path="/usr/bin:/bin",
        )


def test_build_sandbox_environment_cleans_up_private_root_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox import environment as environment_module

    original_mkdir = Path.mkdir

    def failing_mkdir(self: Path, *args, **kwargs):
        if self.name == "home":
            raise OSError("boom")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(environment_module.Path, "mkdir", failing_mkdir)

    with pytest.raises(OSError, match="boom"):
        environment_module.build_sandbox_environment(
            base_env={},
            overrides={},
            allowed_secret_keys=frozenset(),
            temp_root=tmp_path,
            default_path="/usr/bin:/bin",
        )

    leaked_roots = [path for path in tmp_path.iterdir() if path.name.startswith("launch-")]
    assert leaked_roots == []
