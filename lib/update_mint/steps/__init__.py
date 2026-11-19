
'Steps executed by the engine. Each submodule exposes ``steps(ctx, round)`` for its phase.'
from __future__ import annotations

from typing import Callable

from .. import packages as packages


def apt_line_callback(ctx, total: int = 0, download_only: bool = False) -> Callable[[str], None]:
    'Converts normal apt/dpkg output into the step progress bar.\n\n    ``Get:N ...`` = downloading a file; ``Unpacking``/``Setting up`` = installation by dpkg.\n    With ``total`` (number of packages predicted by the simulation), downloads account for the first 35%\n    of the bar and installation for the remainder. Without ``total`` (apt update), progress is indeterminate and shows only the current line.\n    (We do not use ``APT::Status-Fd=1``: it breaks dpkg maintenance scripts.)\n    '
    progress_counts = {"downloaded": 0, "installed": 0}

    def f(line: str) -> None:
        if line.startswith(("Get:", "Hit:", "Ign:")):
            progress_counts["downloaded"] += line.startswith("Get:")
            frac = None
            if total and not download_only:
                frac = 0.35 * min(1.0, progress_counts["downloaded"] / total)
            ctx.progress(frac, line[:90])
        elif line.startswith(("Unpacking ", "Setting up ")):
            progress_counts["installed"] += 1
            frac = None
            if total:
                frac = 0.35 + 0.65 * min(1.0, progress_counts["installed"] / (2.0 * total))
            ctx.progress(frac, line[:90])
        elif line.startswith(("Preparing to unpack", "Processing triggers", "Selecting previously")):
            ctx.progress(None, line[:90])
    return f


def apt_pending(ctx) -> int:
    return packages.simulate(ctx, "full-upgrade").n
