"""Stable internal contracts for semantic requests and validated metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping


def canonical_json(value: object) -> str:
    """Serialize a contract value deterministically for hashing and audit."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticFieldSuggestion:
    source_column: str
    semantic_name: str
    description: str
    semantic_type: str
    unit: str | None
    aliases: tuple[str, ...]
    confidence: float

    def as_dict(self) -> dict[str, object]:
        return {
            "source_column": self.source_column,
            "semantic_name": self.semantic_name,
            "description": self.description,
            "semantic_type": self.semantic_type,
            "unit": self.unit,
            "aliases": list(self.aliases),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SemanticQualitySuggestion:
    issue_type: str
    explanation: str
    suggested_action: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, str]:
        return {
            "issue_type": self.issue_type,
            "explanation": self.explanation,
            "suggested_action": self.suggested_action,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class SemanticMetadataPayload:
    """Validated model output; it is metadata only and never table/text data."""

    display_name: str
    category: str
    description: str
    keywords: tuple[str, ...]
    summary: str
    confidence: float
    semantic_fields: tuple[SemanticFieldSuggestion, ...] = ()
    quality_suggestions: tuple[SemanticQualitySuggestion, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "display_name": self.display_name,
            "category": self.category,
            "description": self.description,
            "keywords": list(self.keywords),
            "summary": self.summary,
            "confidence": self.confidence,
        }
        if self.semantic_fields:
            result["semantic_fields"] = [item.as_dict() for item in self.semantic_fields]
        if self.quality_suggestions:
            result["quality_suggestions"] = [item.as_dict() for item in self.quality_suggestions]
        return result


@dataclass(frozen=True)
class SemanticRequest:
    """Provider-neutral, bounded request assembled from a normalized asset."""

    asset_id: str
    asset_type: str
    model: str
    prompt_version: str
    config_version: str
    normalized_artifact_identity: str
    instructions: str
    reference_data: Mapping[str, object]
    output_contract: str
    input_metadata: Mapping[str, object] = field(default_factory=dict)
    # Complete normalized names are kept for local validation even when the
    # provider-facing reference section has to be compacted.  This is not
    # serialized into ``payload`` and therefore does not weaken the exposure
    # limit.
    validation_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "asset_id",
            "asset_type",
            "model",
            "prompt_version",
            "config_version",
            "normalized_artifact_identity",
            "instructions",
            "output_contract",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.asset_type not in {"table", "text"}:
            raise ValueError("asset_type must be table or text")
        object.__setattr__(self, "validation_columns", tuple(str(item) for item in self.validation_columns))

    @property
    def payload(self) -> dict[str, object]:
        """Return the auditable prompt envelope sent to a provider adapter."""

        return {
            "instructions": self.instructions,
            "reference_data": dict(self.reference_data),
            "output_contract": self.output_contract,
        }

    @property
    def input_hash(self) -> str:
        return sha256_json(
            {
                "asset_id": self.asset_id,
                "asset_type": self.asset_type,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "config_version": self.config_version,
                "normalized_artifact_identity": self.normalized_artifact_identity,
                "validation_columns": list(self.validation_columns),
                "payload": self.payload,
                "input_metadata": dict(self.input_metadata),
            }
        )

    @property
    def payload_bytes(self) -> int:
        return len(canonical_json(self.payload).encode("utf-8"))


@dataclass(frozen=True)
class SemanticResponse:
    """Provider-neutral response.  ``payload`` is JSON object data, not a vendor type."""

    payload: object
    provider: str
    model: str
    raw_size_bytes: int | None = None
    request_id: str | None = None
    usage: Mapping[str, int | float] | None = None


@dataclass(frozen=True)
class SemanticValidationResult:
    valid: bool
    metadata: SemanticMetadataPayload | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def error_code(self) -> str | None:
        return None if self.valid else "semantic_validation_failed"


def valid_confidence(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
