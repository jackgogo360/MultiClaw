from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_docs import check_content, check_links, github_slug  # noqa: E402


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
