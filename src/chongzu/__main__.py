"""Command-line entry point for ``python -m chongzu``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .doctor import main as doctor_main


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the small Phase 1 command line."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "doctor":
        return doctor_main(args[1:])

    parser = argparse.ArgumentParser(prog="python -m chongzu")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", choices=["doctor"])
    parsed = parser.parse_args(args)
    if parsed.command == "doctor":
        return doctor_main([])
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the launcher
    raise SystemExit(main())

