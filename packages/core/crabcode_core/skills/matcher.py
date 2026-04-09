"""Skill auto-trigger matcher.

Matches user context (file paths, bash commands, import lines) against
skill patterns and returns a list of skills to auto-invoke, in order.

Pattern types:
  - paths: glob patterns used as a gating filter — when declared, the skill
    only activates if at least one user-mentioned file path matches
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


def _fnmatch_any(fpath: str, patterns: list[str]) -> bool:
    """Return True if *fpath* matches any glob in *patterns* (case-insensitive)."""
    for pattern in patterns:
        if fnmatch.fnmatch(fpath, pattern) or fnmatch.fnmatch(fpath.lower(), pattern.lower()):
            return True
    return False


def match_path_patterns(
    skills: list[SkillDefinition], file_paths: list[str],
) -> list[SkillDefinition]:
    """Return skills whose pathPatterns or paths match any of the given file paths.

    ``pathPatterns`` is a positive match — the skill triggers when a user-mentioned
    file path matches.  ``paths`` is a gating filter — when declared, the skill
    only triggers if at least one user-mentioned file path falls under one of the
    declared paths.
    """
    matched: list[SkillDefinition] = []
    for skill in skills:
        # If `paths` is declared, at least one user file must match it
        if skill.paths and not any(_fnmatch_any(fp, skill.paths) for fp in file_paths):
            continue
        if not skill.pathPatterns:
            continue
        if any(_fnmatch_any(fp, skill.pathPatterns) for fp in file_paths):
            matched.append(skill)
    return matched


def match_bash_patterns(
    skills: list[SkillDefinition], bash_commands: list[str],
    file_paths: list[str] | None = None,
) -> list[SkillDefinition]:
    """Return skills whose bashPatterns match any of the given bash commands.

    If a skill declares ``paths``, at least one user-mentioned file path must
    match (same gating logic as ``match_path_patterns``).
    """
    matched: list[SkillDefinition] = []
    for skill in skills:
        if skill.paths and not any(_fnmatch_any(fp, skill.paths) for fp in (file_paths or [])):
            continue
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
    file_paths: list[str] | None = None,
) -> list[SkillDefinition]:
    """Return skills whose importPatterns match any of the given import lines.

    If a skill declares ``paths``, at least one user-mentioned file path must
    match (same gating logic as ``match_path_patterns``).
    """
    matched: list[SkillDefinition] = []
    for skill in skills:
        if skill.paths and not any(_fnmatch_any(fp, skill.paths) for fp in (file_paths or [])):
            continue
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
    direct_matches.extend(match_bash_patterns(skills, bash_commands, file_paths=file_paths))
    direct_matches.extend(match_import_patterns(skills, import_lines, file_paths=file_paths))

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
