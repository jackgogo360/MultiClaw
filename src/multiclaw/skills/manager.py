"""SkillManager — unified facade for discovery + activation."""

from __future__ import annotations

import logging
from pathlib import Path

from multiclaw.skills.types import Skill, DisclosureLevel
from multiclaw.skills.discovery import SkillDiscovery
from multiclaw.skills.activation import SkillActivator

logger = logging.getLogger(__name__)


class SkillManager:
    """Single entry point for the skill system.

    Combines discovery, activation, and prompt injection.
    """

    def __init__(self, project_root: str | Path | None = None,
                 user_dirs: list[str | Path] | None = None,
                 extra_dirs: list[str | Path] | None = None,
                 max_active: int = 5):
        self._discovery = SkillDiscovery(
            project_root=project_root,
            user_dirs=user_dirs,
            extra_dirs=extra_dirs,
        )
        self._activator = SkillActivator(max_active=max_active)
        self._skills: dict[str, Skill] = {}

    def discover(self) -> dict[str, Skill]:
        """Run discovery and return found skills."""
        self._skills = self._discovery.discover()
        logger.info("SkillManager: %d skill(s) available after discovery",
                    len(self._skills))
        return self._skills

    @property
    def skills(self) -> dict[str, Skill]:
        return self._skills

    @property
    def active_skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.active]

    @property
    def available_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def process_message(self, message: str) -> list[Skill]:
        """Check message for keyword triggers, activate matches."""
        matched = self._activator.match_keywords(message, self._skills)
        logger.debug("process_message: %d skill(s) total, %d active, "
                     "%d keyword match(es) for %r",
                     len(self._skills), len(self.active_skills),
                     len(matched), message[:80])
        if matched:
            logger.info("Keyword match in message %r: %d candidate(s) — %s",
                        message[:80], len(matched),
                        [(s.name, s.keyword_triggers) for s in matched])
        newly_activated = []
        for skill in matched:
            if self._activator.can_activate(len(self.active_skills)):
                self._activator.activate(skill)
                newly_activated.append(skill)
                logger.info("Activated skill '%s' (keyword trigger)", skill.name)
            else:
                logger.warning("Cannot activate skill '%s': max_active=%d reached",
                              skill.name, self._activator.max_active)
        return newly_activated

    def process_files(self, file_paths: list[str]) -> list[Skill]:
        """Check file paths for pattern triggers, activate matches."""
        matched = self._activator.match_paths(file_paths, self._skills)
        newly_activated = []
        for skill in matched:
            if self._activator.can_activate(len(self.active_skills)):
                self._activator.activate(skill)
                newly_activated.append(skill)
        return newly_activated

    def invoke(self, name: str, args: str = "",
               named_args: dict[str, str] | None = None) -> str | None:
        """Manually invoke a skill by name. Returns substituted content."""
        skill = self._skills.get(name)
        if not skill:
            logger.warning("Manual invoke failed: skill '%s' not found "
                          "(available: %s)", name, list(self._skills.keys()))
            return None
        self._activator.activate(skill)
        logger.info("Manually invoked skill '%s' (args=%r, level=%s)",
                    name, args, skill.level.name)
        return self._activator.substitute_args(skill, args, named_args)

    def build_prompt_section(self) -> str:
        """Build the prompt section for all active skills."""
        parts = []
        for skill in self.active_skills:
            if skill.level == DisclosureLevel.RESOURCES:
                parts.append(skill.format_resources())
            elif skill.level == DisclosureLevel.INSTRUCTIONS:
                parts.append(skill.format_instructions())
            else:
                parts.append(skill.format_metadata())
        return "\n\n".join(parts)

    def get_active_skill_prompts(self) -> list[tuple[str, str]]:
        """Return list of (name, formatted_body) for active skills.

        Each entry is suitable for injection as an independent system message.
        """
        results = []
        for skill in self.active_skills:
            if skill.body:
                results.append((skill.name, skill.format_instructions()))
        return results
