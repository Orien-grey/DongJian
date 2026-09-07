"""Provider-neutral Vision provider protocol and safe error taxonomy."""

from __future__ import annotations

from typing import Protocol

from .models import VisionCapabilities, VisionRequest, VisionResponse


class VisionProviderError(RuntimeError):
    """A safe provider boundary error without API-key material."""

    def __init__(self, message: str, *, code: str = "vision_provider_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class VisionProvider(Protocol):
    name: str
    model: str
    capabilities: VisionCapabilities

    def extract(self, request: VisionRequest) -> VisionResponse:
        """Return one decoded JSON payload for one image."""
