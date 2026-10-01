from pathlib import Path

from multiclaw.agent.instructions import load_project_instructions


def test_root_and_applicable_subdirectory_instructions_loaded_in_order(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root agents")
    (tmp_path / "CLAUDE.md").write_text("root claude")
    (tmp_path / "src/deep").mkdir(parents=True)
    (tmp_path / "src/AGENTS.md").write_text("src agents")
    (tmp_path / "src/deep/CLAUDE.md").write_text("deep claude")
    (tmp_path / "unrelated").mkdir()
    (tmp_path / "unrelated/AGENTS.md").write_text("unrelated")
    assert load_project_instructions(tmp_path, paths=["src/deep/new.py"]) == [
        ("AGENTS.md", "root agents"), ("CLAUDE.md", "root claude"),
        ("src/AGENTS.md", "src agents"), ("src/deep/CLAUDE.md", "deep claude"),
    ]


def test_no_parent_home_or_symlink_instructions_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "AGENTS.md").write_text("parent secret")
    (tmp_path / "CLAUDE.md").write_text("outside secret")
    (workspace / "AGENTS.md").symlink_to(tmp_path / "AGENTS.md")
    (workspace / "link").symlink_to(tmp_path, target_is_directory=True)
    assert load_project_instructions(workspace, paths=["../file", "link/file", str(tmp_path)]) == []


def test_instruction_total_bytes_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("中" * 100)
    (tmp_path / "CLAUDE.md").write_text("other instructions")
    instructions = load_project_instructions(tmp_path, max_bytes=31)
    assert sum(len(body.encode()) for _, body in instructions) <= 31
    assert instructions[0][0] == "AGENTS.md"
    assert instructions[0][1] == "中" * 10


def test_instruction_directory_argument_includes_its_rules(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/AGENTS.md").write_text("subdir")
    assert load_project_instructions(tmp_path, paths=["src"]) == [("src/AGENTS.md", "subdir")]
