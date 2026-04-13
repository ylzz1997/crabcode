from __future__ import annotations

import subprocess
import sys


def test_cli_entry_import_has_no_circular_logging_import() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "from crabcode_cli.app import entry"],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert proc.returncode == 0, proc.stderr
