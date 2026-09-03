"""Debug adapter discovery and launch configuration helpers."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crabcode_core.subprocess_utils import (
    resolve_executable_command,
    subprocess_group_options,
)


_EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".rs": "rust",
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


@dataclass(frozen=True)
class AdapterCandidate:
    """A DAP adapter candidate for a language."""

    language: str
    adapter_id: str
    label: str
    command: list[str]
    install_hint: str
    official: bool = True
    probe_command: list[str] | None = None


@dataclass
class AdapterStatus:
    """Availability status for an adapter candidate."""

    language: str
    adapter_id: str
    label: str
    command: list[str]
    available: bool
    reason: str
    install_hint: str
    official: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "adapter_id": self.adapter_id,
            "label": self.label,
            "command": self.command,
            "available": self.available,
            "reason": self.reason,
            "install_hint": self.install_hint,
            "official": self.official,
        }


def infer_language(path_or_language: str | None) -> str | None:
    """Infer a normalized language name from a language or file path."""
    if not path_or_language:
        return None
    value = path_or_language.strip().lower()
    aliases = {
        "c": "cpp",
        "c++": "cpp",
        "cc": "cpp",
        "cxx": "cpp",
        "py": "python",
        "js": "javascript",
        "node": "javascript",
        "ts": "typescript",
    }
    if value in aliases:
        return aliases[value]
    if value in {
        "cpp",
        "rust",
        "python",
        "go",
        "java",
        "typescript",
        "javascript",
    }:
        return value
    suffix = Path(value).suffix.lower()
    return _EXTENSION_LANGUAGE_MAP.get(suffix)


class AdapterRegistry:
    """Finds configured and built-in DAP adapters."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._timeout = float(self._config.get("adapter_probe_timeout_seconds", 2))

    def status(self, language: str | None = None) -> list[AdapterStatus]:
        """Return adapter status for one language or all supported languages."""
        candidates = self._configured_candidates() + self._builtin_candidates()
        wanted = infer_language(language) if language else None
        results: list[AdapterStatus] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for candidate in candidates:
            if wanted and candidate.language != wanted:
                continue
            key = (candidate.language, candidate.adapter_id, tuple(candidate.command))
            if key in seen:
                continue
            seen.add(key)
            results.append(self._probe(candidate))
        return results

    def resolve(self, language: str, adapter_id: str | None = None) -> AdapterStatus | None:
        """Return the preferred available adapter for a language."""
        normalized = infer_language(language)
        if not normalized:
            return None
        statuses = self.status(normalized)
        available = [s for s in statuses if s.available]
        if adapter_id:
            for status in available:
                if status.adapter_id == adapter_id:
                    return status
            return None
        return available[0] if available else None

    def install_hints(self, language: str | None = None) -> dict[str, list[str]]:
        hints: dict[str, list[str]] = {}
        for status in self.status(language):
            if not status.available:
                hints.setdefault(status.language, [])
                if status.install_hint not in hints[status.language]:
                    hints[status.language].append(status.install_hint)
        return hints

    def _configured_candidates(self) -> list[AdapterCandidate]:
        adapters = self._config.get("adapters", {})
        if not isinstance(adapters, dict):
            return []
        candidates: list[AdapterCandidate] = []
        for language, raw in adapters.items():
            normalized = infer_language(str(language))
            if not normalized or not isinstance(raw, dict):
                continue
            command = raw.get("command")
            if not isinstance(command, list) or not command:
                continue
            command = [str(part) for part in command]
            candidates.append(
                AdapterCandidate(
                    language=normalized,
                    adapter_id=str(raw.get("adapter_id", "configured")),
                    label=str(raw.get("label", f"Configured {normalized} adapter")),
                    command=command,
                    install_hint="Configured in tool_settings.Debugger.adapters.",
                    official=bool(raw.get("official", False)),
                    probe_command=[str(part) for part in raw.get("probe_command", [])]
                    if isinstance(raw.get("probe_command"), list)
                    else None,
                )
            )
        return candidates

    def _probe(self, candidate: AdapterCandidate) -> AdapterStatus:
        executable = candidate.command[0]
        if not shutil.which(executable):
            return AdapterStatus(
                language=candidate.language,
                adapter_id=candidate.adapter_id,
                label=candidate.label,
                command=candidate.command,
                available=False,
                reason=f"executable not found: {executable}",
                install_hint=candidate.install_hint,
                official=candidate.official,
            )
        probe = candidate.probe_command
        if probe:
            try:
                result = subprocess.run(
                    resolve_executable_command(probe),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._timeout,
                    check=False,
                    **subprocess_group_options(),
                )
            except Exception as exc:
                return AdapterStatus(
                    language=candidate.language,
                    adapter_id=candidate.adapter_id,
                    label=candidate.label,
                    command=candidate.command,
                    available=False,
                    reason=f"probe failed: {exc}",
                    install_hint=candidate.install_hint,
                    official=candidate.official,
                )
            if result.returncode != 0:
                return AdapterStatus(
                    language=candidate.language,
                    adapter_id=candidate.adapter_id,
                    label=candidate.label,
                    command=candidate.command,
                    available=False,
                    reason=f"probe exited with {result.returncode}",
                    install_hint=candidate.install_hint,
                    official=candidate.official,
                )
        return AdapterStatus(
            language=candidate.language,
            adapter_id=candidate.adapter_id,
            label=candidate.label,
            command=candidate.command,
            available=True,
            reason="available",
            install_hint=candidate.install_hint,
            official=candidate.official,
        )

    @staticmethod
    def _builtin_candidates() -> list[AdapterCandidate]:
        return [
            AdapterCandidate(
                language="python",
                adapter_id="debugpy",
                label="debugpy",
                command=["python", "-m", "debugpy.adapter"],
                probe_command=["python", "-c", "import debugpy.adapter"],
                install_hint="Install Python debugpy: python -m pip install debugpy",
            ),
            AdapterCandidate(
                language="python",
                adapter_id="debugpy-python3",
                label="debugpy via python3",
                command=["python3", "-m", "debugpy.adapter"],
                probe_command=["python3", "-c", "import debugpy.adapter"],
                install_hint="Install Python debugpy: python3 -m pip install debugpy",
            ),
            AdapterCandidate(
                language="go",
                adapter_id="delve",
                label="Delve DAP",
                command=["dlv", "dap"],
                install_hint="Install Delve: go install github.com/go-delve/delve/cmd/dlv@latest",
            ),
            AdapterCandidate(
                language="cpp",
                adapter_id="lldb-dap",
                label="LLVM lldb-dap",
                command=["lldb-dap"],
                install_hint="Install LLVM lldb-dap from your LLVM/LLDB toolchain.",
            ),
            AdapterCandidate(
                language="cpp",
                adapter_id="lldb-vscode",
                label="LLVM lldb-vscode",
                command=["lldb-vscode"],
                install_hint="Install LLVM/LLDB; older distributions expose lldb-vscode.",
            ),
            AdapterCandidate(
                language="cpp",
                adapter_id="gdb-dap",
                label="GDB DAP",
                command=["gdb", "--interpreter=dap"],
                install_hint="Install a GDB build with DAP support.",
            ),
            AdapterCandidate(
                language="rust",
                adapter_id="lldb-dap",
                label="LLVM lldb-dap",
                command=["lldb-dap"],
                install_hint="Install LLVM lldb-dap and build Rust binaries with debug symbols.",
            ),
            AdapterCandidate(
                language="rust",
                adapter_id="lldb-vscode",
                label="LLVM lldb-vscode",
                command=["lldb-vscode"],
                install_hint="Install LLVM/LLDB and build Rust binaries with debug symbols.",
            ),
            AdapterCandidate(
                language="java",
                adapter_id="vscode-java-debug",
                label="Java Debug Server",
                command=["java-debug-adapter"],
                install_hint=(
                    "Install the official vscode-java-debug adapter and configure "
                    "tool_settings.Debugger.adapters.java.command if it is not on PATH."
                ),
            ),
            AdapterCandidate(
                language="javascript",
                adapter_id="vscode-js-debug",
                label="Microsoft vscode-js-debug",
                command=["js-debug-adapter"],
                install_hint=(
                    "Install Microsoft vscode-js-debug and configure "
                    "tool_settings.Debugger.adapters.javascript.command if it is not on PATH."
                ),
            ),
            AdapterCandidate(
                language="typescript",
                adapter_id="vscode-js-debug",
                label="Microsoft vscode-js-debug",
                command=["js-debug-adapter"],
                install_hint=(
                    "Install Microsoft vscode-js-debug and configure "
                    "tool_settings.Debugger.adapters.typescript.command if it is not on PATH."
                ),
            ),
        ]
