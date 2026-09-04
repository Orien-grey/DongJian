"""Provider-neutral semantic provider protocol and error taxonomy."""

from __future__ import annotations

from typing import Protocol

from .models import SemanticRequest, SemanticResponse


class SemanticProviderError(RuntimeError):
    """Safe provider boundary error; messages must never contain API keys."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        diagnostic: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.diagnostic = diagnostic or message


class SemanticProvider(Protocol):
    name: str

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        """Generate a JSON response for one bounded semantic request."""
