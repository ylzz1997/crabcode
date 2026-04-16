"""LSP diagnostics formatting utilities.

Format LSP diagnostics as XML blocks that get appended to file tool results,
so the LLM can see compilation/type errors immediately after writing code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Maximum diagnostics blocks for non-current files
MAX_PROJECT_DIAGNOSTICS_FILES = 5
# Maximum diagnostics per file
MAX_DIAGNOSTICS_PER_FILE = 20

# LSP DiagnosticSeverity enum values
_SEVERITY_ERROR = 1
_SEVERITY_WARNING = 2
_SEVERITY_INFO = 3
_SEVERITY_HINT = 4

_SEVERITY_LABELS = {
    1: "ERROR",
    2: "WARN",
    3: "INFO",
    4: "HINT",
}


def _format_diagnostic(diag: dict[str, Any]) -> str:
    """Format a single LSP diagnostic as a one-liner."""
    severity = diag.get("severity", 1)
    label = _SEVERITY_LABELS.get(severity, "ERROR")
    line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
    col = diag.get("range", {}).get("start", {}).get("character", 0) + 1
    message = diag.get("message", "unknown error")
    source = diag.get("source", "")
    code = diag.get("code", "")
    suffix = f" [{source}{' ' + str(code) if code else ''}]" if source else ""
    return f"{label} [{line}:{col}] {message}{suffix}"


def format_diagnostics_block(file_path: str, diagnostics: list[dict[str, Any]]) -> str:
    """Format diagnostics for a single file as an XML block.

    Only reports errors (severity=1). Returns empty string if no errors.
    """
    errors = [d for d in diagnostics if d.get("severity", 1) == _SEVERITY_ERROR]
    if not errors:
        return ""

    limited = errors[:MAX_DIAGNOSTICS_PER_FILE]
    more = len(errors) - MAX_DIAGNOSTICS_PER_FILE
    suffix = f"\n... and {more} more" if more > 0 else ""
    lines = "\n".join(_format_diagnostic(d) for d in limited)
    return f'<diagnostics file="{file_path}">\n{lines}{suffix}\n</diagnostics>'


async def collect_and_format_diagnostics(
    lsp_manager: Any,
    file_path: str,
) -> str:
    """Touch the file in LSP, wait for diagnostics, and format the result.

    Returns a string to append to the tool result, or empty string if no errors.
    """
    from crabcode_core.lsp.client import _path_to_uri, _uri_to_path

    try:
        # Touch the file so the LSP server re-analyses it
        await lsp_manager.touch_file(file_path)

        # Collect diagnostics from all matching clients
        clients = await lsp_manager.get_clients(file_path)
        if not clients:
            return ""

        # Wait briefly for diagnostics to arrive
        all_diagnostics: dict[str, list[dict[str, Any]]] = {}
        for client in clients:
            try:
                diags = await client.wait_for_diagnostics(
                    file_path, timeout=3.0, debounce=0.15,
                )
                uri = _path_to_uri(str(file_path))
                existing = all_diagnostics.get(uri, [])
                existing.extend(diags)
                all_diagnostics[uri] = existing
            except Exception:
                pass

        if not all_diagnostics:
            return ""

        # Normalize the file path for comparison
        normalized = str(Path(file_path).resolve())

        # Format diagnostics
        parts: list[str] = []
        project_diag_count = 0

        for uri, diags in all_diagnostics.items():
            try:
                diag_path = _uri_to_path(uri)
            except Exception:
                diag_path = uri

            is_current = Path(diag_path).resolve() == Path(normalized).resolve()
            block = format_diagnostics_block(diag_path, diags)
            if not block:
                continue

            if is_current:
                parts.append(
                    f"\n\nLSP errors detected in this file, please fix:\n{block}"
                )
            else:
                if project_diag_count >= MAX_PROJECT_DIAGNOSTICS_FILES:
                    continue
                project_diag_count += 1
                parts.append(
                    f"\n\nLSP errors detected in other files:\n{block}"
                )

        return "".join(parts)

    except Exception:
        # Never let LSP failures break the write operation
        return ""
