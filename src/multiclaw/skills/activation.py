"""Skill activation and trigger matching."""

from __future__ import annotations

import fnmatch
import logging

from multiclaw.skills.types import Skill, DisclosureLevel
from multiclaw.skills.parser import load_skill_body, load_skill_resources

logger = logging.getLogger(__name__)


class SkillActivator:
    """Handles skill activation logic.

    Two activation modes:
    1. Keyword trigger: auto-activate when keywords appear in user message
    2. Manual: user explicitly invokes /skill-name
    """

    def __init__(self, max_active: int = 5):
        self.max_active = max_active

    def match_keywords(self, message: str, skills: dict[str, Skill]) -> list[Skill]:
        """Find skills whose keyword triggers match the message."""
        matched = []
        msg_lower = message.lower()

        for skill in skills.values():
            if skill.active:
                continue
            for keyword in skill.keyword_triggers:
                if keyword.lower() in msg_lower:
                    matched.append(skill)
                    break

        return matched

    def match_paths(self, file_paths: list[str], skills: dict[str, Skill]) -> list[Skill]:
        """Find skills whose path patterns match the given file paths."""
        matched = []

        for skill in skills.values():
            if skill.active or not skill.metadata.paths:
                continue
            for pattern in skill.metadata.paths:
                for fp in file_paths:
                    if fnmatch.fnmatch(fp, pattern):
                        matched.append(skill)
                        break
                else:
                    continue
                break

        return matched

    def activate(self, skill: Skill) -> Skill:
        """Activate a skill: load body, then resources, mark as active."""
        if skill.level == DisclosureLevel.METADATA:
            load_skill_body(skill)
            logger.info("Loaded body for skill '%s' (%d chars)", skill.name, len(skill.body))
        load_skill_resources(skill)
        if skill.resources:
            logger.info("Loaded %d resource(s) for skill '%s'", len(skill.resources), skill.name)
        skill.active = True
        return skill

    def deactivate(self, skill: Skill) -> Skill:
        """Deactivate a skill."""
        logger.info("Deactivated skill '%s'", skill.name)
        skill.active = False
        return skill

    def can_activate(self, active_count: int) -> bool:
        """Check if we can activate more skills (token budget guard)."""
        return active_count < self.max_active

    def substitute_args(self, skill: Skill, args: str = "",
                        named_args: dict[str, str] | None = None) -> str:
        """Substitute $ARG placeholders in skill body.

        Supports:
        - $ARGUMENTS: full argument string
        - $arg_name: named argument from inputs declaration
        """
        content = skill.body
        if not content:
            return ""

        content = content.replace("$ARGUMENTS", args)

        if named_args:
            for key, value in named_args.items():
                content = content.replace(f"${key}", value)

        if skill.metadata.inputs and args and not named_args:
            parts = args.split(None, len(skill.metadata.inputs) - 1)
            for i, input_name in enumerate(skill.metadata.inputs):
                if i < len(parts):
                    content = content.replace(f"${input_name}", parts[i])

        return content
