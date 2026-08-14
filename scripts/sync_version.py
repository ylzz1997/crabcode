#!/usr/bin/env python3
"""Synchronize static package metadata with CrabCode's VERSION constant."""

from __future__ import annotations

import argparse
import re
import runpy
import sys
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "packages/core/crabcode_core/_version.py"


def load_version() -> str:
    """Load and validate the canonical version without importing CrabCode."""
    version = runpy.run_path(str(VERSION_FILE)).get("VERSION")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"VERSION in {VERSION_FILE} must use the X.Y.Z format")
    return version


def replace_once(text: str, pattern: str, version: str, path: Path) -> str:
    """Replace one version value while preserving the surrounding file."""
    compiled = re.compile(pattern, re.MULTILINE)
    updated, count = compiled.subn(
        lambda match: f"{match.group(1)}{version}{match.group(2)}",
        text,
    )
    if count != 1:
        relative_path = path.relative_to(ROOT)
        raise ValueError(f"expected one version field in {relative_path}, found {count}")
    return updated


def render_pyproject(path: Path, version: str, *, core_dependency: bool) -> str:
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, r'^(version = ")[^"]+("[ \t]*)$', version, path)
    if core_dependency:
        text = replace_once(
            text,
            r'^([ \t]*"crabcode-core>=)[^"]+("[ \t]*,[ \t]*)$',
            version,
            path,
        )
    return text


def render_package_json(path: Path, version: str) -> str:
    text = path.read_text(encoding="utf-8")
    return replace_once(text, r'^(  "version": ")[^"]+("[ \t]*,[ \t]*)$', version, path)


def render_package_lock(path: Path, version: str) -> str:
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, r'^(  "version": ")[^"]+("[ \t]*,[ \t]*)$', version, path)
    return replace_once(
        text,
        r'^(      "version": ")[^"]+("[ \t]*,[ \t]*\n      "license":)',
        version,
        path,
    )


def rendered_files(version: str) -> Iterable[tuple[Path, str]]:
    pyprojects = {
        ROOT / "pyproject.toml": False,
        ROOT / "packages/core/pyproject.toml": False,
        ROOT / "packages/cli/pyproject.toml": True,
        ROOT / "packages/gateway/pyproject.toml": True,
        ROOT / "packages/search/pyproject.toml": True,
        ROOT / "packages/debugger/pyproject.toml": True,
    }
    for path, has_core_dependency in pyprojects.items():
        yield path, render_pyproject(path, version, core_dependency=has_core_dependency)

    package_json = ROOT / "packages/vscode/package.json"
    yield package_json, render_package_json(package_json, version)

    package_lock = ROOT / "packages/vscode/package-lock.json"
    yield package_lock, render_package_lock(package_lock, version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale metadata without modifying files",
    )
    args = parser.parse_args()

    version = load_version()
    stale: list[tuple[Path, str]] = []
    for path, rendered in rendered_files(version):
        if path.read_text(encoding="utf-8") != rendered:
            stale.append((path, rendered))

    if args.check:
        if stale:
            for path, _ in stale:
                print(f"out of sync: {path.relative_to(ROOT)}", file=sys.stderr)
            print("run: python scripts/sync_version.py", file=sys.stderr)
            return 1
        print(f"version metadata is in sync ({version})")
        return 0

    for path, rendered in stale:
        path.write_text(rendered, encoding="utf-8")
        print(f"updated {path.relative_to(ROOT)}")
    if not stale:
        print(f"version metadata is already in sync ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
