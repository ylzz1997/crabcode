"""CrabCode ASCII banner."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

# ASCII crab art — hand-crafted
_CRAB = (
    "   ▐▛██▜▌    ▐▛██▜▌   \n"
    " ▝▜██████▛▘ ▝▜██████▛▘ \n"
    "     ▐██████████▌     \n"
    "   ▐████████████▌     \n"
    "   ▐████████████▌     \n"
    "    ▝▜████████▛▘      \n"
    "  ▐▛██▛▘    ▝▜██▜▌    \n"
    " ▐▛▘            ▝▜▌   \n"
    "▝▘                ▝▘  \n"
    "                    "
)

# "CrabCode" in figlet slant font
_CRABCODE = r"""   ______           __    ______          __
  / ____/________ _/ /_  / ____/___  ____/ /__
 / /   / ___/ __ `/ __ \/ /   / __ \/ __  / _ \
/ /___/ /  / /_/ / /_/ / /___/ /_/ / /_/ /  __/
\____/_/   \__,_/_.___/\____/\____/\__,_/\___/ """


def print_banner(console: Console) -> None:
    """Print the CrabCode banner with ASCII crab art on the left."""
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_row(
        Text(_CRAB, style="bold red"),
        Text(_CRABCODE, style="bold cyan"),
    )
    console.print()
    console.print(grid)
    console.print()
