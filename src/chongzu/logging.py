"""Standard-library logging setup for project commands."""

from __future__ import annotations

import logging
import sys
from typing import TextIO


LOGGER_NAME = "chongzu"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger below the project namespace."""

    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> logging.Logger:
    """Configure one human-readable handler without changing the root logger."""

    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    return logger

