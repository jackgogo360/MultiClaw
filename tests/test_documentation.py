from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_docs import check_content, check_links, github_slug  # noqa: E402


REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/architecture.md",
    "docs/configuration.md",
    "docs/development.md",
    "docs/api.md",
    "docs/testing.md",
    "docs/deployment.md",
    "docs/security-model.md",
    "docs/troubleshooting.md",
    "docs/multi-tenant-operations.md",
    "docs/sandbox-deployment.md",
    "frontend/README.md",
)


def _write(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_github_slug_retains_chinese_and_normalizes_markdown() -> None:
    assert github_slug("配置与 `MULTICLAW_` 环境变量") == "配置与-multiclaw_-环境变量"


def test_check_links_reports_missing_local_target(tmp_path: Path) -> None:
    document = _write(tmp_path, "README.md", "# 项目\n\n[缺失](docs/missing.md)\n")

    assert check_links(tmp_path, (document,)) == [
        "README.md: local link target does not exist: docs/missing.md"
    ]


def test_check_links_accepts_existing_file_and_heading(tmp_path: Path) -> None:
    target = _write(tmp_path, "docs/guide.md", "# 快速开始\n")
    source = _write(tmp_path, "README.md", "[开始](docs/guide.md#快速开始)\n")

    assert check_links(tmp_path, (source, target)) == []


def test_check_content_rejects_unresolved_marker_and_old_health_route(tmp_path: Path) -> None:
    marker = "TO" "DO"
    document = _write(tmp_path, "README.md", f"{marker}\n`/health/ready`\n")

    issues = check_content(tmp_path, (document,))

    assert any("unresolved work marker" in issue for issue in issues)
    assert any("obsolete health route" in issue for issue in issues)


def test_required_documentation_files_exist_and_are_not_empty() -> None:
    for relative_path in REQUIRED_FILES:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        assert path.read_text(encoding="utf-8").strip(), relative_path


def test_readme_exposes_the_supported_developer_entry_points() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for required_text in (
        "uv sync",
        "multiclaw db upgrade",
        "multiclaw db check",
        "npm ci",
        "npm run build",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert required_text in readme


def test_reference_docs_cover_settings_and_public_route_groups() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    api = (ROOT / "docs/api.md").read_text(encoding="utf-8")
    for group in (
        "app",
        "deployment",
        "database",
        "workspace",
        "runtime",
        "workflow",
        "secrets",
        "deletion",
        "llm",
        "memory",
        "governance",
        "tools",
        "agent",
        "skills",
        "auth",
        "email",
        "brevo",
        "resend",
        "mcp",
    ):
        assert f"`{group}`" in configuration
    for route_group in (
        "/auth",
        "/api/sessions",
        "/api/chat",
        "/api/approvals",
        "/api/secrets",
        "/api/account",
        "/api/health",
    ):
        assert route_group in api


def test_formal_docs_use_current_health_routes() -> None:
    markdown = [ROOT / name for name in REQUIRED_FILES if name.endswith(".md")]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in markdown)

    assert "/api/health/live" in joined
    assert "/api/health/ready" in joined
    assert "`/health/live`" not in joined
    assert "`/health/ready`" not in joined


def test_runtime_openapi_endpoints_are_described_as_authenticated() -> None:
    for relative_path in (
        "README.md",
        "docs/getting-started.md",
        "docs/development.md",
        "docs/api.md",
    ):
        lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        endpoint_lines = [
            line
            for line in lines
            if "`/docs`" in line
            or "15800/docs" in line
            or "/openapi.json" in line
        ]
        assert endpoint_lines, relative_path
        assert all(
            any(term in line for term in ("认证", "登录", "有效会话"))
            for line in endpoint_lines
        ), relative_path


def test_skill_environment_variables_use_the_singular_settings_field() -> None:
    configuration = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    joined = f"{configuration}\n{development}"

    for name in (
        "MULTICLAW_SKILL__ENABLED",
        "MULTICLAW_SKILL__MAX_ACTIVE",
        "MULTICLAW_SKILL__EXTRA_DIRS",
        "MULTICLAW_SKILL__USER_DIR",
    ):
        assert name in configuration
    assert "MULTICLAW_SKILL__ENABLED" in development
    assert "MULTICLAW_SKILLS__" not in joined


def test_mysql_support_is_not_narrowed_to_community_edition() -> None:
    markdown = [ROOT / name for name in REQUIRED_FILES if name.endswith(".md")]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in markdown)

    assert "MySQL Community" not in joined
    assert "Community major" not in joined
    assert "Oracle MySQL" in joined
