"""Project-local multiprocessing worker configuration.

Windows ``multiprocessing`` starts a fresh interpreter for every spawned
worker. Pointing that interpreter at the bundled ``pythonw.exe`` keeps the
worker hidden in a GUI product while preserving the normal spawn protocol and
exception transport. If the executable is absent, the standard Python
executable is left untouched so development and diagnostics remain usable.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from . import paths


def configure_hidden_worker_executable() -> Path | None:
    """Use the bundled windowless interpreter for Windows spawned workers."""

    if os.name != "nt":
        return None
    executable = paths.PYTHONW_EXE.resolve()
    if not executable.is_file():
        return None
    multiprocessing.set_executable(str(executable))
    return executable
