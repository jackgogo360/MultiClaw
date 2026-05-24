"""Skill discovery — multi-layer directory scanning."""

from __future__ import annotations

import logging
from pathlib import Path

from multiclaw.skills.types import Skill
from multiclaw.skills.parser import parse_skill_file

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"

# Both directory conventions are supported
USER_SKILL_DIRS = [
    Path.home() / ".multiclaw" / "skills",
    Path.home() / ".agents" / "skills",
]
PROJECT_SKILL_DIR_NAMES = [".multiclaw/skills", ".agents/skills"]


class SkillDiscovery:
    """Discovers skills from multiple directory layers.

    Scan order (later overrides earlier by name):
    1. User skills: ~/.multiclaw/skills/ and ~/.agents/skills/
    2. Project skills: .multiclaw/skills/ and .agents/skills/ (walks up to home)
    3. Additional paths (explicit)

    Each skill is a directory containing a SKILL.md file.
    """

    def __init__(self, project_root: str | Path | None = None,
                 user_dirs: list[str | Path] | None = None,
                 extra_dirs: list[str | Path] | None = None):
        self._project_root = Path(project_root) if project_root else Path.cwd()
        self._user_dirs = [Path(d) for d in (user_dirs or USER_SKILL_DIRS)]
        self._extra_dirs = [Path(d) for d in (extra_dirs or [])]

    def discover(self) -> dict[str, Skill]:
        """Discover all skills, returning a name->Skill dict.

        Later sources override earlier ones (project > user).
        """
        skills: dict[str, Skill] = {}
        logger.info("Starting skill discovery (project_root=%s)", self._project_root)

        for user_dir in self._user_dirs:
            found = self._scan_dir(user_dir)
            if found:
                logger.info("User dir %s: found %d skill(s) — %s",
                            user_dir, len(found),
                            [s.name for s in found])
            for skill in found:
                skills[skill.name] = skill

        for project_dir in self._find_project_skill_dirs():
            found = self._scan_dir(project_dir)
            if found:
                logger.info("Project dir %s: found %d skill(s) — %s",
                            project_dir, len(found),
                            [s.name for s in found])
            for skill in found:
                skills[skill.name] = skill

        for extra_dir in self._extra_dirs:
            found = self._scan_dir(extra_dir)
            if found:
                logger.info("Extra dir %s: found %d skill(s) — %s",
                            extra_dir, len(found),
                            [s.name for s in found])
            for skill in found:
                skills[skill.name] = skill

        logger.info("Discovery complete: %d skill(s) total — %s",
                    len(skills), list(skills.keys()))
        return skills

    def _find_project_skill_dirs(self) -> list[Path]:
        """Walk up from project root looking for skill directories.

        At each ancestor, checks both .multiclaw/skills/ and .agents/skills/.
        """
        dirs: list[Path] = []
        current = self._project_root.resolve()
        home = Path.home().resolve()

        while True:
            for dir_name in PROJECT_SKILL_DIR_NAMES:
                candidate = current / dir_name
                if candidate.is_dir():
                    dirs.append(candidate)

            if current == home or current == current.parent:
                break
            current = current.parent

        dirs.reverse()
        return dirs

    def _scan_dir(self, directory: Path) -> list[Skill]:
        """Scan a directory for skill subdirectories containing SKILL.md."""
        skills = []
        if not directory.is_dir():
            return skills

        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / SKILL_FILENAME
            if skill_file.is_file():
                try:
                    skill = parse_skill_file(skill_file)
                    skills.append(skill)
                except Exception as e:
                    logger.warning("Failed to parse %s: %s", skill_file, e)

        return skills

    def discover_for_paths(self, file_paths: list[str | Path]) -> list[Skill]:
        """Discover conditional skills relevant to given file paths."""
        seen_dirs: set[Path] = set()
        skills = []

        for fp in file_paths:
            p = Path(fp).resolve()
            current = p.parent if p.is_file() else p

            while current != self._project_root.parent:
                for dir_name in PROJECT_SKILL_DIR_NAMES:
                    candidate = current / dir_name
                    if candidate.is_dir() and candidate not in seen_dirs:
                        seen_dirs.add(candidate)
                        for skill in self._scan_dir(candidate):
                            if skill.metadata.paths:
                                skills.append(skill)
                if current == current.parent:
                    break
                current = current.parent

        return skills
