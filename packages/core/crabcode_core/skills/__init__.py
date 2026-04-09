"""Skills package — skill discovery, definitions, and auto-trigger matching."""

from crabcode_core.skills.loader import SkillDefinition, load_skills
from crabcode_core.skills.matcher import auto_match, resolve_chain

__all__ = ["SkillDefinition", "load_skills", "auto_match", "resolve_chain"]
