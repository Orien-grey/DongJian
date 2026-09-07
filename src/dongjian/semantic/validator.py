"""Strict local validation for semantic JSON responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import (
    SemanticFieldSuggestion,
    SemanticMetadataPayload,
    SemanticQualitySuggestion,
    SemanticRequest,
    SemanticResponse,
    SemanticValidationResult,
    valid_confidence,
)


MAX_SEMANTIC_JSON_BYTES = 256 * 1024
REQUIRED_TABLE_KEYS = frozenset({"display_name", "category", "description", "keywords", "summary", "semantic_fields", "confidence"})
REQUIRED_TEXT_KEYS = frozenset({"display_name", "category", "description", "keywords", "summary", "confidence"})
TABLE_KEYS = REQUIRED_TABLE_KEYS | {"quality_suggestions"}
TEXT_KEYS = REQUIRED_TEXT_KEYS | {"quality_suggestions"}
FIELD_KEYS = frozenset({"source_column", "semantic_name", "description", "semantic_type", "unit", "aliases", "confidence"})


def _string(value: object, path: str, errors: list[str]) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{path} must be a string")
        return None
    if not value.strip():
        errors.append(f"{path} must not be blank")
        return None
    return value


def _string_list(value: object, path: str, errors: list[str]) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        errors.append(f"{path} must be a list of strings")
        return None
    values: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{path}[{index}] must be a string")
        else:
            values.append(item)
    return tuple(values) if len(values) == len(value) else None


def _confidence(value: object, path: str, errors: list[str]) -> float | None:
    if not valid_confidence(value):
        errors.append(f"{path} must be a finite number between 0 and 1")
        return None
    return float(value)


def _parse_payload(payload: object) -> tuple[Mapping[str, object] | None, list[str]]:
    errors: list[str] = []
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_SEMANTIC_JSON_BYTES:
            return None, ["semantic JSON exceeds the size limit"]
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None, ["semantic response is not valid JSON"]
    else:
        try:
            if len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")) > MAX_SEMANTIC_JSON_BYTES:
                return None, ["semantic JSON exceeds the size limit"]
        except (TypeError, ValueError):
            return None, ["semantic response cannot be serialized as JSON"]
    if not isinstance(payload, Mapping):
        return None, ["semantic response must be a JSON object"]
    return payload, errors


def validate_semantic_payload(payload: object, request: SemanticRequest) -> SemanticValidationResult:
    """Validate an object/string and reject unknown or data-mutating fields."""

    value, errors = _parse_payload(payload)
    if value is None:
        return SemanticValidationResult(False, errors=tuple(errors))
    expected = TABLE_KEYS if request.asset_type == "table" else TEXT_KEYS
    required = REQUIRED_TABLE_KEYS if request.asset_type == "table" else REQUIRED_TEXT_KEYS
    unknown = sorted((set(value) - expected), key=str)
    missing = sorted(required - set(value))
    if unknown:
        errors.append("unknown fields are not allowed: " + ", ".join(str(item) for item in unknown))
    if missing:
        errors.append("missing required fields: " + ", ".join(missing))
    display_name = _string(value.get("display_name"), "display_name", errors)
    category = _string(value.get("category"), "category", errors)
    description = _string(value.get("description"), "description", errors)
    keywords = _string_list(value.get("keywords"), "keywords", errors)
    summary = _string(value.get("summary"), "summary", errors)
    confidence = _confidence(value.get("confidence"), "confidence", errors)
    fields: list[SemanticFieldSuggestion] = []
    warnings: list[str] = []
    suggestions: list[SemanticQualitySuggestion] = []
    raw_suggestions = value.get("quality_suggestions", [])
    if not isinstance(raw_suggestions, list):
        errors.append("quality_suggestions must be a list")
    else:
        for index, raw_suggestion in enumerate(raw_suggestions):
            if not isinstance(raw_suggestion, Mapping):
                errors.append(f"quality_suggestions[{index}] must be an object")
                continue
            suggestion_keys = frozenset({"issue_type", "explanation", "suggested_action", "severity"})
            unknown_suggestion = sorted((set(raw_suggestion) - suggestion_keys), key=str)
            missing_suggestion = sorted({"issue_type", "explanation", "suggested_action"} - set(raw_suggestion))
            if unknown_suggestion:
                errors.append(f"quality_suggestions[{index}] has unknown fields: {', '.join(str(item) for item in unknown_suggestion)}")
            if missing_suggestion:
                errors.append(f"quality_suggestions[{index}] missing fields: {', '.join(missing_suggestion)}")
            issue_type = _string(raw_suggestion.get("issue_type"), f"quality_suggestions[{index}].issue_type", errors)
            explanation = _string(raw_suggestion.get("explanation"), f"quality_suggestions[{index}].explanation", errors)
            suggested_action = _string(raw_suggestion.get("suggested_action"), f"quality_suggestions[{index}].suggested_action", errors)
            severity = raw_suggestion.get("severity", "warning")
            if not isinstance(severity, str) or severity not in {"info", "warning", "error", "critical"}:
                errors.append(f"quality_suggestions[{index}].severity must be info, warning, error, or critical")
                severity = "warning"
            if None not in (issue_type, explanation, suggested_action):
                suggestions.append(
                    SemanticQualitySuggestion(
                        issue_type=issue_type,
                        explanation=explanation,
                        suggested_action=suggested_action,
                        severity=severity,
                    )
                )
    if request.asset_type == "table":
        raw_fields = value.get("semantic_fields")
        if not isinstance(raw_fields, list):
            errors.append("semantic_fields must be a list")
        else:
            normalized_columns = request.reference_data.get("normalized_columns")
            allowed_columns = set(request.validation_columns)
            if not allowed_columns:
                allowed_columns = {str(item) for item in normalized_columns} if isinstance(normalized_columns, list) else set()
            seen_source: set[str] = set()
            seen_semantic: set[str] = set()
            for index, raw_field in enumerate(raw_fields):
                if not isinstance(raw_field, Mapping):
                    errors.append(f"semantic_fields[{index}] must be an object")
                    continue
                field_unknown = sorted((set(raw_field) - FIELD_KEYS), key=str)
                field_missing = sorted(FIELD_KEYS - set(raw_field))
                if field_unknown:
                    errors.append(
                        f"semantic_fields[{index}] has unknown fields: "
                        + ", ".join(str(item) for item in field_unknown)
                    )
                if field_missing:
                    errors.append(
                        f"semantic_fields[{index}] missing fields: " + ", ".join(field_missing)
                    )
                source_column = _string(raw_field.get("source_column"), f"semantic_fields[{index}].source_column", errors)
                semantic_name = _string(raw_field.get("semantic_name"), f"semantic_fields[{index}].semantic_name", errors)
                field_description = _string(raw_field.get("description"), f"semantic_fields[{index}].description", errors)
                semantic_type = _string(raw_field.get("semantic_type"), f"semantic_fields[{index}].semantic_type", errors)
                unit = raw_field.get("unit")
                if unit is not None and not isinstance(unit, str):
                    errors.append(f"semantic_fields[{index}].unit must be a string or null")
                    unit = None
                aliases = _string_list(raw_field.get("aliases"), f"semantic_fields[{index}].aliases", errors)
                field_confidence = _confidence(raw_field.get("confidence"), f"semantic_fields[{index}].confidence", errors)
                if source_column is not None and source_column not in allowed_columns:
                    errors.append(f"semantic_fields[{index}].source_column is not an existing normalized column")
                if source_column is not None and source_column in seen_source:
                    warnings.append(f"duplicate semantic mapping for source column: {source_column}")
                if semantic_name is not None and semantic_name in seen_semantic:
                    warnings.append(f"duplicate semantic name: {semantic_name}")
                if source_column is not None:
                    seen_source.add(source_column)
                if semantic_name is not None:
                    seen_semantic.add(semantic_name)
                if None not in (source_column, semantic_name, field_description, semantic_type, aliases, field_confidence):
                    fields.append(
                        SemanticFieldSuggestion(
                            source_column=source_column,
                            semantic_name=semantic_name,
                            description=field_description,
                            semantic_type=semantic_type,
                            unit=unit,
                            aliases=aliases,
                            confidence=field_confidence,
                        )
                    )
    if errors or None in (display_name, category, description, keywords, summary, confidence):
        return SemanticValidationResult(False, errors=tuple(errors), warnings=tuple(warnings))
    return SemanticValidationResult(
        True,
        metadata=SemanticMetadataPayload(
            display_name=display_name,
            category=category,
            description=description,
            keywords=keywords,
            summary=summary,
            confidence=confidence,
            semantic_fields=tuple(fields),
            quality_suggestions=tuple(suggestions),
        ),
        warnings=tuple(warnings),
    )


def validate_semantic_response(response: SemanticResponse, request: SemanticRequest) -> SemanticValidationResult:
    return validate_semantic_payload(response.payload, request)
