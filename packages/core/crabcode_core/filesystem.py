"""Small filesystem operations shared by atomic writers."""

import os
from pathlib import Path
import time


def replace_with_retry(source: Path, target: Path) -> None:
    """Keep atomic replacement, tolerating brief Windows sharing violations."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                raise
            time.sleep(0.05 * 2**attempt)
