from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
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

_HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
_INLINE_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_MARKDOWN_FORMATTING_PATTERN = re.compile(r"[`*~]")
_UNRESOLVED_PATTERN = re.compile(
    r"\b(?:" + "|".join(("TO" "DO", "TB" "D", "FIX" "ME")) + r")\b|待定|待确认",
    re.IGNORECASE,
)
_OBSOLETE_HEALTH_PATTERN = re.compile(r"`/health/(?:live|ready)`")
_REMOVED_STORAGE_PATTERN = re.compile(
    r"aiosqlite\.connect|SqliteRepository|database\.path"
)
_SECRET_PATTERNS = (
    re.compile(r"xkeysib-[A-Za-z0-9_-]{20,}"),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    re.compile(r"Bearer [A-Za-z0-9._-]{20,}"),
    re.compile(r"mysql\+aiomysql://[^\s:/]+:[^\s@]+@"),
)


def formal_documents(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / relative_path
        for relative_path in REQUIRED_FILES
        if relative_path.endswith(".md")
    )


def github_slug(value: str) -> str:
    value = _INLINE_LINK_PATTERN.sub(r"\1", value)
    value = _HTML_TAG_PATTERN.sub("", value)
    value = _MARKDOWN_FORMATTING_PATTERN.sub("", value).strip().lower()
    normalized = "".join(
        character
        for character in value
        if character.isalnum() or character in {" ", "-", "_"}
    )
    return re.sub(r"\s+", "-", normalized)


def heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in _HEADING_PATTERN.findall(text):
        base = github_slug(heading)
        if not base:
            continue
        duplicate_index = counts.get(base, 0)
        counts[base] = duplicate_index + 1
        anchor = base if duplicate_index == 0 else f"{base}-{duplicate_index}"
        anchors.add(anchor)
    return anchors


def markdown_links(text: str) -> Iterator[tuple[str, str]]:
    for label, raw_destination in _LINK_PATTERN.findall(text):
        destination = raw_destination.strip()
        if destination.startswith("<") and ">" in destination:
            destination = destination[1 : destination.index(">")]
        else:
            destination = destination.split(maxsplit=1)[0]
        yield label, destination


def _relative_name(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def check_links(root: Path, documents: Iterable[Path]) -> list[str]:
    issues: list[str] = []
    resolved_root = root.resolve()
    anchor_cache: dict[Path, set[str]] = {}

    for document in documents:
        if not document.is_file():
            continue
        source_name = _relative_name(root, document)
        text = document.read_text(encoding="utf-8")
        for _, destination in markdown_links(text):
            parsed = urlsplit(destination)
            if parsed.scheme in {"http", "https", "mailto", "tel"} or parsed.netloc:
                continue
            if parsed.path.startswith("/"):
                continue

            target = document if not parsed.path else document.parent / unquote(parsed.path)
            resolved_target = target.resolve()
            try:
                resolved_target.relative_to(resolved_root)
            except ValueError:
                issues.append(f"{source_name}: local link escapes repository: {destination}")
                continue

            if not resolved_target.exists():
                issues.append(
                    f"{source_name}: local link target does not exist: {unquote(parsed.path)}"
                )
                continue

            if not parsed.fragment or resolved_target.suffix.lower() != ".md":
                continue

            if resolved_target not in anchor_cache:
                anchor_cache[resolved_target] = heading_anchors(
                    resolved_target.read_text(encoding="utf-8")
                )
            expected_anchor = unquote(parsed.fragment).lower()
            if expected_anchor not in anchor_cache[resolved_target]:
                issues.append(
                    f"{source_name}: local link anchor does not exist: {destination}"
                )

    return issues


def check_content(root: Path, documents: Iterable[Path]) -> list[str]:
    issues: list[str] = []
    for document in documents:
        if not document.is_file():
            continue
        name = _relative_name(root, document)
        text = document.read_text(encoding="utf-8")
        if _UNRESOLVED_PATTERN.search(text):
            issues.append(f"{name}: unresolved work marker")
        if _OBSOLETE_HEALTH_PATTERN.search(text):
            issues.append(f"{name}: obsolete health route")
        if _REMOVED_STORAGE_PATTERN.search(text):
            issues.append(f"{name}: removed storage API reference")
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            issues.append(f"{name}: possible secret or credential")
    return issues


def check_required_content(root: Path) -> list[str]:
    issues: list[str] = []
    readme_path = root / "README.md"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        for token in (
            "uv sync",
            "multiclaw db upgrade",
            "multiclaw db check",
            "npm ci",
            "npm run build",
            "CONTRIBUTING.md",
            "SECURITY.md",
        ):
            if token not in readme:
                issues.append(f"README.md: missing required entry point: {token}")

    configuration_path = root / "docs/configuration.md"
    if configuration_path.is_file():
        configuration = configuration_path.read_text(encoding="utf-8")
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
            if f"`{group}`" not in configuration:
                issues.append(f"docs/configuration.md: missing Settings group: {group}")

    api_path = root / "docs/api.md"
    if api_path.is_file():
        api = api_path.read_text(encoding="utf-8")
        for route_group in (
            "/auth",
            "/api/sessions",
            "/api/chat",
            "/api/approvals",
            "/api/secrets",
            "/api/account",
            "/api/health",
        ):
            if route_group not in api:
                issues.append(f"docs/api.md: missing public route group: {route_group}")
    return issues


def main() -> int:
    missing = [
        f"{relative_path}: required documentation file is missing"
        for relative_path in REQUIRED_FILES
        if not (ROOT / relative_path).is_file()
    ]
    documents = formal_documents(ROOT)
    issues = missing
    issues.extend(check_links(ROOT, documents))
    issues.extend(check_content(ROOT, documents))
    issues.extend(check_required_content(ROOT))
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("documentation check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
