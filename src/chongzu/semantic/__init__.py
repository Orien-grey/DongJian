"""Offline-first semantic enrichment infrastructure.

The package owns the provider boundary, bounded input construction, prompt
contracts, local validation, and semantic run persistence.  It never imports
or calls a provider merely because the package is imported.  Phase 7A only
executes :class:`FakeSemanticProvider`; the HTTP implementation is reserved
for a separately authorized Phase 7B.
"""

from .config import LLMConfig, SemanticConfig, load_semantic_config
from .fake_provider import FakeSemanticProvider
from .models import (
    SemanticFieldSuggestion,
    SemanticMetadataPayload,
    SemanticQualitySuggestion,
    SemanticRequest,
    SemanticResponse,
)
from .provider import SemanticProvider, SemanticProviderError

__all__ = [
    "FakeSemanticProvider",
    "LLMConfig",
    "SemanticConfig",
    "SemanticFieldSuggestion",
    "SemanticMetadataPayload",
    "SemanticQualitySuggestion",
    "SemanticProvider",
    "SemanticProviderError",
    "SemanticRequest",
    "SemanticResponse",
    "load_semantic_config",
]
