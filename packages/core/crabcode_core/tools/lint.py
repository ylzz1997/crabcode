"""LintTool — read linter errors and diagnostics for specified files."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from crabcode_core.logging_utils import get_logger
from crabcode_core.types.tool import Tool, ToolContext, ToolResult

logger = get_logger(__name__)

_LINTER_CONFIGS: list[dict[str, Any]] = [
    # ── Python ──
    {
        "name": "ruff",
        "extensions": {".py", ".pyi"},
        "cmd": ["ruff", "check", "--output-format=concise"],
        "detect_files": {"ruff.toml", ".ruff.toml"},
        "detect_sections": {"ruff"},
    },
    {
        "name": "pylint",
        "extensions": {".py"},
        "cmd": ["pylint", "--output-format=text", "--score=no"],
        "detect_files": {".pylintrc", "pylintrc"},
        "detect_sections": {"pylint"},
    },
    {
        "name": "mypy",
        "extensions": {".py", ".pyi"},
        "cmd": ["mypy", "--no-error-summary", "--no-pretty", "--show-column-numbers"],
        "detect_files": {"mypy.ini", ".mypy.ini"},
        "detect_sections": {"mypy"},
    },
    # ── JavaScript / TypeScript ──
    {
        "name": "eslint",
        "extensions": {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"},
        "cmd": ["eslint", "--format=unix", "--no-color"],
        "detect_files": {
            ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
            ".eslintrc.yml", ".eslintrc.yaml", "eslint.config.js",
            "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
        },
        "detect_sections": set(),
    },
    # ── Go ──
    {
        "name": "golangci-lint",
        "extensions": {".go"},
        "cmd": ["golangci-lint", "run", "--out-format=line-number"],
        "detect_files": {".golangci.yml", ".golangci.yaml", ".golangci.toml"},
        "detect_sections": set(),
    },
    # ── Rust ──
    {
        "name": "clippy",
        "extensions": {".rs"},
        "cmd": ["cargo", "clippy", "--quiet", "--message-format=short"],
        "detect_files": {"Cargo.toml"},
        "detect_sections": set(),
        "project_level": True,
        "use_stderr": True,
    },
    # ── C / C++ ──
    {
        "name": "clang-tidy",
        "extensions": {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"},
        "cmd": ["clang-tidy"],
        "detect_files": {".clang-tidy", "compile_commands.json"},
        "detect_sections": set(),
        "suffix_args": ["--"],
    },
    {
        "name": "cppcheck",
        "extensions": {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"},
        "cmd": ["cppcheck", "--enable=warning,style,performance", "--template=gcc", "--quiet"],
        "detect_files": set(),
        "detect_sections": set(),
    },
    # ── Java ──
    {
        "name": "checkstyle",
        "extensions": {".java"},
        "cmd": ["checkstyle"],
        "detect_files": {"checkstyle.xml", ".checkstyle", "checkstyle-config.xml"},
        "detect_sections": set(),
    },
    {
        "name": "pmd",
        "extensions": {".java"},
        "cmd": ["pmd", "check", "-f", "text", "--no-progress", "-R",
                "rulesets/java/quickstart.xml"],
        "detect_files": {"ruleset.xml", "pmd-ruleset.xml"},
        "detect_sections": set(),
        "target_flag": "-d",
    },
]


def _has_pyproject_section(cwd: str, section_names: set[str]) -> bool:
    """Check if pyproject.toml has a [tool.<name>] section."""
    if not section_names:
        return False
    pyproject = Path(cwd) / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        text = pyproject.read_text(errors="replace")
        return any(f"[tool.{s}]" in text for s in section_names)
    except Exception:
        logger.debug("Failed to read %s while detecting lint config", pyproject, exc_info=True)
        return False


def _has_config_file(cwd: str, filenames: set[str]) -> bool:
    """Check if any of the given config files exist in cwd."""
    for name in filenames:
        if (Path(cwd) / name).exists():
            return True
    return False


def _detect_linters(
    paths: list[Path], cwd: str
) -> list[dict[str, Any]]:
    """Pick applicable linters based on file extensions and project config.

    When no paths are given, scans for project config files to decide which
    linters apply instead of guessing by extension.
    """
    extensions = {p.suffix for p in paths if p.suffix}
    no_paths = not paths

    matched: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for cfg in _LINTER_CONFIGS:
        if cfg["name"] in seen_names:
            continue
        if not shutil.which(cfg["cmd"][0]):
            continue

        has_config = (
            _has_config_file(cwd, cfg["detect_files"])
            or _has_pyproject_section(cwd, cfg["detect_sections"])
        )

        if no_paths:
            if not has_config:
                continue
        else:
            if not extensions & cfg["extensions"]:
                continue

        matched.append({**cfg, "has_config": has_config})
        seen_names.add(cfg["name"])

    if not matched:
        return []

    configured = [m for m in matched if m["has_config"]]
    if configured:
        return configured
    return matched[:1]


async def _run_linter(
    cfg: dict[str, Any],
    targets: list[str],
    cwd: str,
    timeout: int = 60,
) -> tuple[str, str, int]:
    """Run a linter command and return (output, stderr, exit_code).

    Respects per-linter config flags:
      project_level  – don't append file targets
      use_stderr     – read diagnostics from stderr instead of stdout
      suffix_args    – extra args appended after targets (e.g. ["--"] for clang-tidy)
      target_flag    – flag before each target (e.g. "-d" for pmd)
    """
    cmd = cfg["cmd"]
    is_project_level = cfg.get("project_level", False)
    target_flag = cfg.get("target_flag")
    suffix_args = cfg.get("suffix_args", [])

    full_cmd = list(cmd)
    if not is_project_level:
        if target_flag:
            full_cmd.extend([target_flag, ",".join(targets)])
        else:
            full_cmd.extend(targets)
        full_cmd.extend(suffix_args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if cfg.get("use_stderr"):
            return stderr, stdout, proc.returncode or 0
        return stdout, stderr, proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", f"Linter timed out after {timeout}s", -1
    except FileNotFoundError:
        return "", f"Linter command not found: {cmd[0]}", -1


class LintTool(Tool):
    name = "Lint"
    description = "Read linter errors and diagnostics for specified files."
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "File or directory paths to check. "
                    "If omitted, checks the current working directory."
                ),
            },
            "linter": {
                "type": "string",
                "description": (
                    "Force a specific linter (e.g., 'ruff', 'eslint', 'mypy'). "
                    "If omitted, auto-detects based on file types and project config."
                ),
            },
        },
    }

    async def get_prompt(self, **kwargs: Any) -> str:
        return (
            "Read linter errors and diagnostics for specified files or directories. "
            "Auto-detects the appropriate linter based on file types and project "
            "configuration.\n\n"
            "Supported linters:\n"
            "- Python: ruff, pylint, mypy\n"
            "- JavaScript/TypeScript: eslint\n"
            "- Go: golangci-lint\n"
            "- Rust: clippy (via cargo)\n"
            "- C/C++: clang-tidy, cppcheck\n"
            "- Java: checkstyle, pmd\n\n"
            "Use this tool after making edits to verify you haven't introduced "
            "errors. You can provide specific file paths or omit them to check "
            "the whole project.\n\n"
            "Guidelines:\n"
            "- Only use on files you've edited or are about to edit.\n"
            "- Results may include pre-existing errors — focus on errors in "
            "code you changed.\n"
            "- If no linter is installed or configured, the tool will report "
            "that clearly.\n"
            "- For Rust projects, clippy runs at the project level (ignores "
            "individual file paths)."
        )

    async def call(
        self,
        tool_input: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        raw_paths = tool_input.get("paths") or []
        forced_linter = tool_input.get("linter")

        cwd = context.cwd

        resolved: list[Path] = []
        for p in raw_paths:
            path = Path(p)
            if not path.is_absolute():
                path = Path(cwd) / path
            resolved.append(path)

        missing = [str(p) for p in resolved if not p.exists()]
        if missing:
            return ToolResult(
                result_for_model=f"Error: paths not found: {', '.join(missing)}",
                is_error=True,
            )

        if forced_linter:
            cfg = next(
                (c for c in _LINTER_CONFIGS if c["name"] == forced_linter), None
            )
            if cfg is None:
                known = ", ".join(c["name"] for c in _LINTER_CONFIGS)
                return ToolResult(
                    result_for_model=(
                        f"Unknown linter '{forced_linter}'. "
                        f"Supported: {known}"
                    ),
                    is_error=True,
                )
            if not shutil.which(cfg["cmd"][0]):
                return ToolResult(
                    result_for_model=(
                        f"Linter '{forced_linter}' is not installed. "
                        f"Install it and try again."
                    ),
                    is_error=True,
                )
            linters = [cfg]
        else:
            linters = _detect_linters(resolved, cwd)
            if not linters:
                return ToolResult(
                    result_for_model=(
                        "No suitable linter found. Either no linter is installed "
                        "for the given file types, or no matching files were provided.\n"
                        "Supported: ruff, pylint, mypy, eslint, golangci-lint, clippy, "
                        "clang-tidy, cppcheck, checkstyle, pmd."
                    ),
                )

        targets = [str(p) for p in resolved] if resolved else ["."]

        all_output: list[str] = []
        total_issues = 0

        for linter_cfg in linters:
            linter_name = linter_cfg["name"]
            stdout, stderr, exit_code = await _run_linter(
                linter_cfg, targets, cwd
            )

            if exit_code == -1:
                all_output.append(f"[{linter_name}] Error: {stderr}")
                continue

            output = stdout.strip()
            if not output and exit_code == 0:
                all_output.append(f"[{linter_name}] No issues found.")
                continue

            lines = output.split("\n") if output else []
            issue_count = len([
                l for l in lines
                if l.strip() and not l.startswith((" ", "\t"))
            ])
            total_issues += issue_count

            max_chars = 50_000
            if len(output) > max_chars:
                output = output[:max_chars] + "\n... (truncated)"

            header = f"[{linter_name}] {issue_count} issue(s):"
            all_output.append(f"{header}\n{output}")

            if stderr.strip() and exit_code != 0:
                err_preview = stderr.strip()[:2000]
                all_output.append(f"[{linter_name}] stderr:\n{err_preview}")

        result_text = "\n\n".join(all_output)
        return ToolResult(
            data={"total_issues": total_issues},
            result_for_model=result_text,
        )
