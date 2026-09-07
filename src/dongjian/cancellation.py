"""Cooperative cancellation shared by coordinator stages."""

from __future__ import annotations

from threading import Event


class CancellationRequested(RuntimeError):
    """Raised at a safe coordinator boundary after a stop request."""


def check_cancel(event: Event | None) -> None:
    if event is not None and event.is_set():
        raise CancellationRequested("processing was cancelled by the user")


__all__ = ["CancellationRequested", "check_cancel"]
