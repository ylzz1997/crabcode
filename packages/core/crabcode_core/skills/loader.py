"""Skill loading — scans .crabcode/skills/ and .claude/skills/ directories.

Search order (later entries override earlier ones with the same name):
  1. ~/.claude/skills/<name>/SKILL.md     (user global, Claude Code compat)
  2. ~/.crabcode/skills/<name>/SKILL.md   (user global, crabcode native)
  3. .claude/skills/<name>/SKILL.md       (project, walking up to home)
  4. .crabcode/skills/<name>/SKILL.md     (project, walking up to home, highest priority)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillDefinition:
    name: str
    description: str
    content: str
    source_path: str
    when_to_use: str | None = None
    paths: list[str] = field(default_factory=list)
    # Auto-trigger patterns
    pathPatterns: list[str] = field(default_factory=list)  # glob: file paths
    bashPatterns: list[str] = field(default_factory=list)  # regex: bash commands
    importPatterns: list[str] = field(default_factory=list)  # regex: imports
    chainTo: list[str] = field(default_factory=list)  # skill names to chain


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from Markdown body.

    Returns (frontmatter_dict, body). If no frontmatter block is found,
    returns ({}, original text).

    Supports scalar values and simple list values (one-per-line or inline
    comma-separated).  Example::

        name: commit
        pathPatterns: "src/**/*.py, tests/**/*.py"
        bashPatterns:
          - "git commit.*"
        chainTo: "lint"
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not match:
        return {}, text

    raw_yaml = match.group(1)
    body = text[match.end():]

    data: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in raw_yaml.splitlines():
        # List item under a key: "  - value"
        list_item = re.match(r'^\s+-\s+"?(.*?)"?\s*$', line)
        if list_item and current_key is not None:
            if current_list is None:
                current_list = []
            current_list.append(list_item.group(1))
            continue

        # Key-value pair: "key: value"
        kv = re.match(r'^([\w][\w_-]*):\s*"?(.*?)"?\s*$', line)
        if kv:
            # Flush previous list
            if current_key is not None and current_list is not None:
                data[current_key] = current_list
            current_key = kv.group(1)
            val = kv.group(2)
            if val:
                data[current_key] = val
                current_list = None
            else:
                # Key with no value — might be followed by list items
                current_list = []
            continue

    # Flush last list
    if current_key is not None and current_list is not None:
        data[current_key] = current_list

    return data, body.lstrip("\n")


def _parse_list_field(value: Any) -> list[str]:
    """Normalize a frontmatter field to a list of strings.

    Accepts: list (already parsed), comma-separated string, single string, None.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [item.strip() for item in value if item and item.strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


def _load_skill_from_file(skill_file: Path) -> SkillDefinition | None:
    """Parse a single SKILL.md file and return a SkillDefinition, or None on error."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, body = _parse_frontmatter(text)
    if not body.strip() and not fm:
        return None

    # Derive name: prefer frontmatter, fall back to parent dir name
    name = fm.get("name") or skill_file.parent.name
    description = fm.get("description", "")
    when_to_use = fm.get("when_to_use") or None

    raw_paths = fm.get("paths", "")
    paths = _parse_list_field(raw_paths)

    pathPatterns = _parse_list_field(fm.get("pathPatterns", ""))
    bashPatterns = _parse_list_field(fm.get("bashPatterns", ""))
    importPatterns = _parse_list_field(fm.get("importPatterns", ""))
    chainTo = _parse_list_field(fm.get("chainTo", ""))

    return SkillDefinition(
        name=name,
        description=description,
        content=body.strip(),
        source_path=str(skill_file),
        when_to_use=when_to_use,
        paths=paths,
        pathPatterns=pathPatterns,
        bashPatterns=bashPatterns,
        importPatterns=importPatterns,
        chainTo=chainTo,
    )


def _scan_skills_dir(base: Path) -> dict[str, SkillDefinition]:
    """Scan a skills directory for <skill-name>/SKILL.md entries."""
    skills: dict[str, SkillDefinition] = {}
    if not base.is_dir():
        return skills

    for entry in sorted(base.iterdir()):
        if not (entry.is_dir() or entry.is_symlink()):
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        skill = _load_skill_from_file(skill_file)
        if skill:
            skills[skill.name] = skill

    return skills


def _dirs_up_to_home(cwd: str, config_dir: str) -> list[Path]:
    """Return skill dirs from home down to cwd for a given config directory name.

    Walking home → cwd means closer (more specific) directories override
    farther ones when we merge.
    """
    home = Path.home()
    current = Path(cwd).resolve()
    dirs: list[Path] = []

    path = current
    while True:
        # Skip the home directory itself — it's covered by the global scan
        if path != home:
            dirs.append(path / config_dir / "skills")
        if path == home or path.parent == path:
            break
        path = path.parent

    # Reverse so home-adjacent dirs come first (lower priority), cwd last (highest)
    dirs.reverse()
    return dirs


def load_skills(cwd: str) -> list[SkillDefinition]:
    """Load all skills visible from *cwd*, merged by priority.

    Skills with the same name loaded later override earlier ones.
    """
    merged: dict[str, SkillDefinition] = {}
    home = Path.home()

    # 1. ~/.claude/skills/ — lowest priority (Claude Code global compat)
    for name, skill in _scan_skills_dir(home / ".claude" / "skills").items():
        merged[name] = skill

    # 2. ~/.crabcode/skills/ — overrides claude global
    for name, skill in _scan_skills_dir(home / ".crabcode" / "skills").items():
        merged[name] = skill

    # 3. Project-level .claude/skills/ dirs (home-adjacent → cwd)
    for skills_dir in _dirs_up_to_home(cwd, ".claude"):
        for name, skill in _scan_skills_dir(skills_dir).items():
            merged[name] = skill

    # 4. Project-level .crabcode/skills/ dirs (highest priority)
    for skills_dir in _dirs_up_to_home(cwd, ".crabcode"):
        for name, skill in _scan_skills_dir(skills_dir).items():
            merged[name] = skill

    return list(merged.values())
