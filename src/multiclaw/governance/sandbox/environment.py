import shutil
import tempfile
from collections.abc import Mapping
from fnmatch import fnmatch
from pathlib import Path

from multiclaw.governance.sandbox.errors import SandboxPolicyError
from multiclaw.governance.sandbox.models import SandboxEnvironment

_PASSTHROUGH_KEYS = ("LANG", "LC_ALL", "TERM")
_RUNTIME_OWNED_KEYS = {"HOME", "TMPDIR", "PATH", "USER", "LOGNAME", "SHELL"}
_SECRET_PATTERNS = (
    "*TOKEN*",
    "*SECRET*",
    "*PASSWORD*",
    "*API_KEY*",
    "*ACCESS_KEY*",
    "*PRIVATE_KEY*",
)


def build_sandbox_environment(
    *,
    base_env: Mapping[str, str],
    overrides: Mapping[str, str],
    allowed_secret_keys: frozenset[str],
    temp_root: Path,
    default_path: str,
) -> SandboxEnvironment:
    temp_root.mkdir(parents=True, exist_ok=True)
    private_root = Path(tempfile.mkdtemp(prefix="launch-", dir=temp_root))
    private_root.chmod(0o700)

    try:
        home = private_root / "home"
        tmp = private_root / "tmp"
        xdg_root = private_root / "xdg"
        xdg_config = xdg_root / "config"
        xdg_cache = xdg_root / "cache"
        xdg_data = xdg_root / "data"
        xdg_state = xdg_root / "state"
        xdg_runtime = xdg_root / "runtime"

        for path in (home, tmp, xdg_config, xdg_cache, xdg_data, xdg_state, xdg_runtime):
            path.mkdir(parents=True, exist_ok=True)

        env: dict[str, str] = {}
        for key in _PASSTHROUGH_KEYS:
            value = base_env.get(key)
            if value:
                env[key] = value

        env.update(
            {
                "USER": "sandbox",
                "LOGNAME": "sandbox",
                "SHELL": "/bin/sh",
                "PATH": default_path,
                "HOME": str(home),
                "TMPDIR": str(tmp),
                "XDG_CONFIG_HOME": str(xdg_config),
                "XDG_CACHE_HOME": str(xdg_cache),
                "XDG_DATA_HOME": str(xdg_data),
                "XDG_STATE_HOME": str(xdg_state),
                "XDG_RUNTIME_DIR": str(xdg_runtime),
            }
        )

        allowed_secret_names = {key.upper() for key in allowed_secret_keys}
        for key, value in overrides.items():
            normalized_key = key.upper()
            if normalized_key in _RUNTIME_OWNED_KEYS or normalized_key.startswith("XDG_"):
                raise SandboxPolicyError(f"override for {normalized_key} is not allowed")
            if _is_secret_key(normalized_key) and normalized_key not in allowed_secret_names:
                raise SandboxPolicyError(
                    f"secret override for {normalized_key} is not allowed"
                )
            env[normalized_key] = value

        return SandboxEnvironment(
            env=env,
            private_root=private_root,
            home=home,
            tmp=tmp,
        )
    except Exception:
        shutil.rmtree(private_root, ignore_errors=True)
        raise


def _is_secret_key(key: str) -> bool:
    return any(fnmatch(key, pattern) for pattern in _SECRET_PATTERNS)
