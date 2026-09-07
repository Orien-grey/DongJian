"""One small trust decision shared by analysis consumers."""

from __future__ import annotations

from collections.abc import Mapping


CONFIRMED_STRUCTURE = "CONFIRMED_STRUCTURE"
BEST_EFFORT = "BEST_EFFORT"
CANDIDATE_ONLY = "CANDIDATE_ONLY"
UNUSABLE = "UNUSABLE"


def _value(asset: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in asset:
            return asset[name]
    return None


def table_trust_level(asset: Mapping[str, object]) -> str:
    metadata = _value(asset, "extractorMetadata", "extractor_metadata", "metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    provenance = _value(asset, "provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    quality = str(_value(asset, "qualityStatus", "quality_status") or "").casefold()
    candidate_status = str(
        _value(asset, "candidateStatus", "candidate_status")
        or metadata.get("candidate_status")
        or ""
    ).casefold()
    parser_validity = str(
        _value(asset, "parserValidity", "parser_validity")
        or metadata.get("parser_validity")
        or ""
    ).casefold()
    source_kind = str(
        _value(asset, "sourceKind", "source_kind")
        or provenance.get("sourceKind")
        or ""
    ).casefold()
    extractor = str(
        _value(asset, "extractor")
        or provenance.get("extractor")
        or ""
    ).casefold()

    if parser_validity in {"invalid", "unusable", "corrupt"} or quality in {"unusable", "fail"}:
        return UNUSABLE
    if candidate_status == "candidate" or source_kind in {"page", "image"} or "img2table" in extractor:
        return CANDIDATE_ONLY
    if quality in {"ready", "pass"}:
        return CONFIRMED_STRUCTURE
    return BEST_EFFORT


def is_table_trusted_for_analysis(asset: Mapping[str, object]) -> bool:
    return table_trust_level(asset) == CONFIRMED_STRUCTURE


__all__ = [
    "BEST_EFFORT",
    "CANDIDATE_ONLY",
    "CONFIRMED_STRUCTURE",
    "UNUSABLE",
    "is_table_trusted_for_analysis",
    "table_trust_level",
]
