from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NsJailSystemMount:
    path: str
    is_dir: bool
    mandatory: bool = False


_SYSTEM_READ_ONLY_ROOTS = (
    NsJailSystemMount("/bin", True),
    NsJailSystemMount("/usr", True),
    NsJailSystemMount("/lib", True),
    NsJailSystemMount("/lib64", True),
    NsJailSystemMount("/sbin", True),
    NsJailSystemMount("/etc", True),
    NsJailSystemMount("/dev/null", False),
    NsJailSystemMount("/dev/urandom", False),
)

_COMMON_SECCOMP_RULES = (
    "KILL_PROCESS {",
    "  ptrace,",
    "  process_vm_readv,",
    "  process_vm_writev",
    "}",
)

_NO_CHILD_PROCESS_SECCOMP_RULES = (
    "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }",
)

_DEFAULT_ALLOW_SECCOMP_RULE = "DEFAULT ALLOW"


def _seccomp_policy(*rules: str) -> str:
    return "\n".join(rules)


@dataclass(frozen=True)
class NsJailProfileTemplate:
    name: str
    workspace_mode: str
    network_mode: str
    allow_subprocesses: bool
    write_protected_patterns: tuple[str, ...]
    read_hidden_patterns: tuple[str, ...]
    system_read_only_roots: tuple[NsJailSystemMount, ...]
    seccomp_policy: str
    rlimit_as_mb: int
    rlimit_cpu_seconds: int
    rlimit_fsize_mb: int
    rlimit_nofile: int
    rlimit_nproc: int


SHELL_WORKSPACE_PROFILE = NsJailProfileTemplate(
    name="shell_workspace",
    workspace_mode="rw",
    network_mode="disabled",
    allow_subprocesses=True,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
    system_read_only_roots=_SYSTEM_READ_ONLY_ROOTS,
    seccomp_policy=_seccomp_policy(*_COMMON_SECCOMP_RULES, _DEFAULT_ALLOW_SECCOMP_RULE),
    rlimit_as_mb=4096,
    rlimit_cpu_seconds=30,
    rlimit_fsize_mb=16,
    rlimit_nofile=64,
    rlimit_nproc=1024,
)

CODE_EXEC_PYTHON_PROFILE = NsJailProfileTemplate(
    name="code_exec_python",
    workspace_mode="rw",
    network_mode="disabled",
    allow_subprocesses=False,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
    system_read_only_roots=_SYSTEM_READ_ONLY_ROOTS,
    seccomp_policy=_seccomp_policy(
        *_COMMON_SECCOMP_RULES,
        *_NO_CHILD_PROCESS_SECCOMP_RULES,
        _DEFAULT_ALLOW_SECCOMP_RULE,
    ),
    rlimit_as_mb=4096,
    rlimit_cpu_seconds=30,
    rlimit_fsize_mb=16,
    rlimit_nofile=64,
    rlimit_nproc=1,
)

MCP_STDIO_LOCAL_PROFILE = NsJailProfileTemplate(
    name="mcp_stdio_local",
    workspace_mode="ro",
    network_mode="inherit",
    allow_subprocesses=True,
    write_protected_patterns=(".git",),
    read_hidden_patterns=(".env", ".env.*"),
    system_read_only_roots=_SYSTEM_READ_ONLY_ROOTS,
    seccomp_policy=_seccomp_policy(*_COMMON_SECCOMP_RULES, _DEFAULT_ALLOW_SECCOMP_RULE),
    rlimit_as_mb=4096,
    rlimit_cpu_seconds=30,
    rlimit_fsize_mb=16,
    rlimit_nofile=64,
    rlimit_nproc=1024,
)

NSJAIL_PROFILES = {
    SHELL_WORKSPACE_PROFILE.name: SHELL_WORKSPACE_PROFILE,
    CODE_EXEC_PYTHON_PROFILE.name: CODE_EXEC_PYTHON_PROFILE,
    MCP_STDIO_LOCAL_PROFILE.name: MCP_STDIO_LOCAL_PROFILE,
}
