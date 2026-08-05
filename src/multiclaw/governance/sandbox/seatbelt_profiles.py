from __future__ import annotations

from dataclasses import dataclass

_MAX_RUNTIME_ROOTS = 16


@dataclass(frozen=True)
class SeatbeltProfileTemplate:
    name: str
    profile_text: str
    workspace_mode: str
    network_mode: str
    allow_subprocesses: bool
    write_protected_patterns: tuple[str, ...]
    read_hidden_patterns: tuple[str, ...]


def _runtime_root_read_rules() -> str:
    rules = []
    for index in range(_MAX_RUNTIME_ROOTS):
        rules.append(
            '(allow file-read* (subpath (param "RUNTIME_ROOT_' + str(index) + '")))'
        )
    return "\n".join(rules)


_COMMON_PREFIX = """
(version 1)
(deny default)

; Base runtime access
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/usr"))
(allow file-read* (subpath "/bin"))
(allow file-read* (subpath "/sbin"))
(allow file-read* (subpath "/dev"))

; Dynamic runtime roots
""".strip()

_COMMON_SUFFIX = """

; Workspace and private runtime roots
(allow file-read* (subpath (param "WORKSPACE")))
(allow file-read* (subpath (param "PRIVATE_HOME")))
(allow file-read* (subpath (param "PRIVATE_TMP")))

; Hide sensitive dot-env files everywhere
(deny file-read* (regex #".*/\\.env(\\..*)?$"))

; Protect git metadata from writes everywhere
(deny file-write* (regex #".*/\\.git($|/.*)"))
""".strip()

_WORKSPACE_RW_BLOCK = """
(allow file-write* (subpath (param "WORKSPACE")))
(allow file-write* (subpath (param "PRIVATE_HOME")))
(allow file-write* (subpath (param "PRIVATE_TMP")))
""".strip()

_WORKSPACE_RO_BLOCK = """
; MCP stdio remains read-only against the workspace
(allow file-write* (subpath (param "PRIVATE_HOME")))
(allow file-write* (subpath (param "PRIVATE_TMP")))
""".strip()

_SHELL_PROCESS_BLOCK = """
(allow process-fork)
(deny network*)
""".strip()

_CODE_PROCESS_BLOCK = """
(deny process-fork)
(deny network*)
""".strip()

_MCP_PROCESS_BLOCK = """
(allow process-fork)
(allow network*)
""".strip()


def _build_profile(process_block: str, workspace_block: str) -> str:
    segments = [
        _COMMON_PREFIX,
        _runtime_root_read_rules(),
        _COMMON_SUFFIX,
        workspace_block,
        process_block,
    ]
    return "\n\n".join(segment for segment in segments if segment)


SHELL_WORKSPACE_PROFILE = SeatbeltProfileTemplate(
    name="shell_workspace",
    profile_text=_build_profile(_SHELL_PROCESS_BLOCK, _WORKSPACE_RW_BLOCK),
    workspace_mode="rw",
    network_mode="disabled",
    allow_subprocesses=True,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
)

CODE_EXEC_PYTHON_PROFILE = SeatbeltProfileTemplate(
    name="code_exec_python",
    profile_text=_build_profile(_CODE_PROCESS_BLOCK, _WORKSPACE_RW_BLOCK),
    workspace_mode="rw",
    network_mode="disabled",
    allow_subprocesses=False,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
)

MCP_STDIO_LOCAL_PROFILE = SeatbeltProfileTemplate(
    name="mcp_stdio_local",
    profile_text=_build_profile(_MCP_PROCESS_BLOCK, _WORKSPACE_RO_BLOCK),
    workspace_mode="ro",
    network_mode="inherit",
    allow_subprocesses=True,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
)

SEATBELT_PROFILES = {
    SHELL_WORKSPACE_PROFILE.name: SHELL_WORKSPACE_PROFILE,
    CODE_EXEC_PYTHON_PROFILE.name: CODE_EXEC_PYTHON_PROFILE,
    MCP_STDIO_LOCAL_PROFILE.name: MCP_STDIO_LOCAL_PROFILE,
}
