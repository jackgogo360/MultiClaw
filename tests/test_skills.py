"""Tests for the skill system."""
import tempfile
from pathlib import Path

import pytest
from multiclaw.skills import (
    Skill,
    SkillMetadata,
    Trigger,
    TriggerType,
    DisclosureLevel,
    SkillDiscovery,
    SkillActivator,
    SkillManager,
    parse_skill_file,
    load_skill_body,
    load_skill_resources,
)


SAMPLE_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing
triggers:
  - type: keyword
    keywords: [test, testing, unittest]
inputs: [target, scope]
paths: ["*.test.ts", "tests/**"]
allowed_tools: [Bash, Read]
max_tokens: 2000
version: "1.0"
tags: [testing]
---

You are a testing assistant.

Run tests on $target with scope $scope.
Full args: $ARGUMENTS
"""


# --- Helpers ---

def _make_skill_dir(parent: Path, name: str, content: str) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir / "SKILL.md"


def _make_minimal_skill(parent: Path, name: str,
                        keywords: list[str] | None = None) -> Path:
    if keywords:
        trigger_yaml = "triggers:\n"
        for kw in keywords:
            trigger_yaml += f"    - type: keyword\n      keywords: [{kw}]\n"
    else:
        trigger_yaml = ""
    content = f"""---
name: {name}
description: {name} description
{trigger_yaml}---
Body of {name}. Args: $ARGUMENTS
"""
    return _make_skill_dir(parent, name, content)


# --- Parser tests ---

def test_parse_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)

    assert skill.name == "test-skill"
    assert skill.description == "A test skill for unit testing"
    assert skill.level == DisclosureLevel.METADATA
    assert skill.body == ""
    assert len(skill.metadata.triggers) == 1
    assert skill.metadata.triggers[0].type == TriggerType.KEYWORD
    assert "test" in skill.metadata.triggers[0].keywords
    assert skill.metadata.inputs == ["target", "scope"]
    assert skill.metadata.paths == ["*.test.ts", "tests/**"]


def test_load_body():
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)
        load_skill_body(skill)

    assert skill.level == DisclosureLevel.INSTRUCTIONS
    assert "testing assistant" in skill.body
    assert "$target" in skill.body


def test_load_resources():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hi")

        skill = parse_skill_file(skill_dir / "SKILL.md")
        load_skill_resources(skill)

    assert skill.level == DisclosureLevel.RESOURCES
    assert "scripts/run.sh" in skill.resources


def test_user_invocable_auto_keyword():
    """user-invocable: true without triggers → skill name becomes keyword."""
    content = """---
name: last30days
description: Research skill
user-invocable: true
---
Research body.
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "last30days", content)
        skill = parse_skill_file(path)

    assert skill.name == "last30days"
    assert skill.metadata.triggers[0].type == TriggerType.KEYWORD
    assert "last30days" in skill.keyword_triggers


def test_no_frontmatter_defaults_to_manual():
    content = "Just plain text, no frontmatter."
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "plain", content)
        skill = parse_skill_file(path)

    assert skill.name == "plain"
    assert skill.metadata.triggers[0].type == TriggerType.MANUAL


def test_idempotent_load_body():
    """load_skill_body is idempotent -- calling twice doesn't change anything."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_skill_dir(Path(tmp), "test-skill", SAMPLE_SKILL_MD)
        skill = parse_skill_file(path)
        load_skill_body(skill)
        body1 = skill.body
        load_skill_body(skill)
        assert skill.body == body1


# --- Discovery tests ---

def test_discover_from_user_dir():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user_skills"
        _make_minimal_skill(user_dir, "skill-a", keywords=["test"])
        _make_minimal_skill(user_dir, "skill-b", keywords=["deploy"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[user_dir])
        skills = discovery.discover()

    assert "skill-a" in skills
    assert "skill-b" in skills


def test_project_overrides_user():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user_skills"
        _make_minimal_skill(user_dir, "shared", keywords=["test"])

        project_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(project_dir, "shared", keywords=["deploy"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[user_dir])
        skills = discovery.discover()

    assert skills["shared"].keyword_triggers == ["deploy"]


def test_empty_dirs_no_error():
    with tempfile.TemporaryDirectory() as tmp:
        discovery = SkillDiscovery(
            project_root=tmp,
            user_dirs=[Path(tmp) / "nonexistent"],
            extra_dirs=[],
        )
        skills = discovery.discover()
    assert skills == {}


def test_dot_multiclaw_and_dot_agents_both_scanned():
    """Both .multiclaw/skills/ and .agents/skills/ are scanned at project level."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dir_a = tmp_path / ".multiclaw" / "skills"
        dir_b = tmp_path / ".agents" / "skills"
        _make_minimal_skill(dir_a, "skill-a", keywords=["a"])
        _make_minimal_skill(dir_b, "skill-b", keywords=["b"])

        discovery = SkillDiscovery(project_root=tmp, user_dirs=[], extra_dirs=[])
        skills = discovery.discover()

    assert "skill-a" in skills
    assert "skill-b" in skills


# --- Activation tests ---

def _make_skill_obj(name: str, keywords: list[str] | None = None,
                    paths: list[str] | None = None) -> Skill:
    triggers = []
    if keywords:
        triggers.append(Trigger(type=TriggerType.KEYWORD, keywords=keywords))
    else:
        triggers.append(Trigger(type=TriggerType.MANUAL))

    return Skill(
        name=name,
        metadata=SkillMetadata(
            name=name,
            description=f"{name} desc",
            triggers=triggers,
            paths=paths or [],
        ),
        body=f"Body of {name}. Use $ARGUMENTS here.",
        level=DisclosureLevel.INSTRUCTIONS,
    )


def test_keyword_matching():
    activator = SkillActivator()
    skills = {
        "tester": _make_skill_obj("tester", keywords=["test", "unittest"]),
        "deployer": _make_skill_obj("deployer", keywords=["deploy", "release"]),
    }
    matched = activator.match_keywords("please run the test suite", skills)
    assert len(matched) == 1
    assert matched[0].name == "tester"


def test_keyword_case_insensitive():
    activator = SkillActivator()
    skills = {"s": _make_skill_obj("s", keywords=["Deploy"])}
    matched = activator.match_keywords("let's DEPLOY this", skills)
    assert len(matched) == 1


def test_skip_already_active():
    activator = SkillActivator()
    skill = _make_skill_obj("s", keywords=["test"])
    skill.active = True
    skills = {"s": skill}
    matched = activator.match_keywords("run test", skills)
    assert matched == []


def test_path_matching():
    activator = SkillActivator()
    skills = {"tester": _make_skill_obj("tester", paths=["*.test.ts", "tests/*"])}
    assert len(activator.match_paths(["src/foo.test.ts"], skills)) == 1
    assert activator.match_paths(["src/foo.ts"], skills) == []


def test_activate_deactivate():
    activator = SkillActivator()
    skill = _make_skill_obj("s", keywords=["x"])
    assert not skill.active
    activator.activate(skill)
    assert skill.active
    activator.deactivate(skill)
    assert not skill.active


def test_max_active_guard():
    activator = SkillActivator(max_active=2)
    assert activator.can_activate(0)
    assert activator.can_activate(1)
    assert not activator.can_activate(2)


def test_substitute_args():
    activator = SkillActivator()
    skill = _make_skill_obj("s")
    result = activator.substitute_args(skill, args="hello world")
    assert "hello world" in result
    assert "$ARGUMENTS" not in result


def test_substitute_named_args():
    activator = SkillActivator()
    skill = _make_skill_obj("s")
    skill.body = "Target: $target, Scope: $scope"
    skill.metadata.inputs = ["target", "scope"]
    result = activator.substitute_args(
        skill, args="", named_args={"target": "src/", "scope": "unit"}
    )
    assert "Target: src/" in result
    assert "Scope: unit" in result


# --- Manager tests ---

def test_manager_invoke():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "greet", keywords=["hello"])

        manager = SkillManager(project_root=tmp)
        manager.discover()

        result = manager.invoke("greet", args="world")
        assert result is not None
        assert "world" in result


def test_manager_keyword_activation():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "tester", keywords=["test"])

        manager = SkillManager(project_root=tmp)
        manager.discover()

        activated = manager.process_message("run the test suite")
        assert len(activated) == 1
        assert activated[0].name == "tester"


def test_manager_nonexistent_skill():
    with tempfile.TemporaryDirectory() as tmp:
        manager = SkillManager(project_root=tmp)
        manager.discover()
        result = manager.invoke("nonexistent")
        assert result is None


def test_manager_get_active_skill_prompts():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_dir = tmp_path / ".multiclaw" / "skills"
        _make_minimal_skill(skill_dir, "greet", keywords=["hello"])

        manager = SkillManager(project_root=tmp)
        manager.discover()
        manager.invoke("greet", args="world")

        prompts = manager.get_active_skill_prompts()
        assert len(prompts) == 1
        name, body = prompts[0]
        assert name == "greet"
        assert 'skill name="greet"' in body
