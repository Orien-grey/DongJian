"""Small contracts at the Vision provider boundary.

The provider receives image bytes and an output contract only.  Local file and
registry identity stays in the extraction runner and is never delegated to a
model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VisionCapabilities:
    """Capabilities of one configured provider adapter."""

    vision: bool
    contract: str
    image_input: str = "data_url"


@dataclass(frozen=True)
class VisionRequest:
    """One bounded image request; it contains no registry identity."""

    image_bytes: bytes
    media_type: str
    model: str
    output_contract: str


@dataclass(frozen=True)
class VisionResponse:
    payload: Any
    provider: str
    model: str
    raw_size_bytes: int
    request_id: str | None = None
    usage: dict[str, int | float] | None = None
