"""Skill auto-trigger matcher.

Matches user context (file paths, bash commands, import lines) against
skill patterns and returns a list of skills to auto-invoke, in order.

Pattern types:
  - pathPatterns: glob patterns matched against file paths the user is
    working with (from conversation context, git status, etc.)
  - bashPatterns: regex patterns matched against bash commands the user
    has run or wants to run
  - importPatterns: regex patterns matched against import/require lines
    found in the project or mentioned by the user

Chain routing:
  - chainTo: after a skill executes, automatically queue the listed
    skill(s) as follow-ups
"""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from crabcode_core.skills.loader import SkillDefinition


def match_path_patterns(
    skills: list[SkillDefinition], file_paths: list[str],
) -> list[SkillDefinition]:
    """Return skills whose pathPatterns match any of the given file paths."""
    matched: list[SkillDefinition] = []
    for skill in skills:
        if not skill.pathPatterns:
            continue
        for pattern in skill.pathPatterns:
            for fpath in file_paths:
                if fnmatch.fnmatch(fpath, pattern):
                    matched.append(skill)
                    break
                if fnmatch.fnmatch(fpath.lower(), pattern.lower()):
                    matched.append(skill)
                    break
            else:
                continue
            break
    return matched


def match_bash_patterns(
    skills: list[SkillDefinition], bash_commands: list[str],
) -> list[SkillDefinition]:
    """Return skills whose bashPatterns match any of the given bash commands."""
    matched: list[SkillDefinition] = []
    for skill in skills:
        if not skill.bashPatterns:
            continue
        for pattern in skill.bashPatterns:
            try:
                regex = re.compile(pattern)
            except re.error:
                continue
            for cmd in bash_commands:
                if regex.search(cmd):
                    matched.append(skill)
                    break
            else:
                continue
            break
    return matched


def match_import_patterns(
    skills: list[SkillDefinition], import_lines: list[str],
) -> list[SkillDefinition]:
    """Return skills whose importPatterns match any of the given import lines."""
    matched: list[SkillDefinition] = []
    for skill in skills:
        if not skill.importPatterns:
            continue
        for pattern in skill.importPatterns:
            try:
                regex = re.compile(pattern)
            except re.error:
                continue
            for line in import_lines:
                if regex.search(line):
                    matched.append(skill)
                    break
            else:
                continue
            break
    return matched


def resolve_chain(
    skill: SkillDefinition,
    skill_map: dict[str, SkillDefinition],
    visited: set[str] | None = None,
) -> list[SkillDefinition]:
    """Resolve the chainTo routing for a skill, returning the full chain.

    Detects circular references and skips unknown skill names.
    The source skill's name is added to visited to prevent cycles.
    """
    if visited is None:
        visited = set()
    visited.add(skill.name)

    chain: list[SkillDefinition] = []
    if not skill.chainTo:
        return chain

    for target_name in skill.chainTo:
        if target_name in visited:
            continue  # break circular chain
        target = skill_map.get(target_name)
        if not target:
            continue
        visited.add(target_name)
        chain.append(target)
        # Recursively resolve nested chains
        chain.extend(resolve_chain(target, skill_map, visited))

    return chain


def auto_match(
    skills: list[SkillDefinition],
    file_paths: list[str] | None = None,
    bash_commands: list[str] | None = None,
    import_lines: list[str] | None = None,
) -> list[SkillDefinition]:
    """Match skills against context, returning ordered results with chains.

    Deduplicates and preserves insertion order. Chain-following skills
    are appended after their trigger skill.
    """
    file_paths = file_paths or []
    bash_commands = bash_commands or []
    import_lines = import_lines or []

    if not skills:
        return []

    skill_map = {s.name: s for s in skills}
    seen: set[str] = set()
    result: list[SkillDefinition] = []

    # Collect direct matches
    direct_matches: list[SkillDefinition] = []
    direct_matches.extend(match_path_patterns(skills, file_paths))
    direct_matches.extend(match_bash_patterns(skills, bash_commands))
    direct_matches.extend(match_import_patterns(skills, import_lines))

    # Add matched skills + their chains, deduplicating
    for skill in direct_matches:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        result.append(skill)

        # Resolve chainTo
        for chained in resolve_chain(skill, skill_map):
            if chained.name not in seen:
                seen.add(chained.name)
                result.append(chained)

    return result
