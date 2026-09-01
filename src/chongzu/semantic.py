"""Provider-neutral semantic-enrichment boundary; no network client lives here."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Protocol

from .assets import AssetType, SemanticMetadata


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "LLMConfig":
        timeout = value.get("timeout_seconds", 60)
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise ValueError("timeout_seconds must be an integer")
        strings: dict[str, str] = {}
        for name in ("base_url", "api_key", "model"):
            item = value.get(name, "")
            if not isinstance(item, str):
                raise ValueError(f"{name} must be a string")
            strings[name] = item
        return cls(
            base_url=strings["base_url"],
            api_key=strings["api_key"],
            model=strings["model"],
            timeout_seconds=timeout,
        )

    @classmethod
    def load(cls, path: Path) -> "LLMConfig":
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("LLM configuration must be a JSON object")
        return cls.from_mapping(value)

    def validate_for_use(self) -> None:
        if not self.base_url.strip():
            raise ValueError("base_url must be explicitly configured")
        if not self.api_key.strip():
            raise ValueError("api_key must be explicitly configured")
        if not self.model.strip():
            raise ValueError("model must be explicitly configured")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class SemanticEnrichmentRequest:
    asset_id: str
    asset_type: AssetType
    extracted_content: Mapping[str, object]
    prompt_version: str


class SemanticEnrichmentProvider(Protocol):
    """A future adapter may implement this against a user-configured service."""

    def generate_metadata(self, request: SemanticEnrichmentRequest) -> SemanticMetadata:
        ...
