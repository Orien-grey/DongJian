"""Deterministic provider used by Phase 7A tests and explicit local smoke runs."""

from __future__ import annotations

from .models import SemanticRequest, SemanticResponse


class FakeSemanticProvider:
    """Return predictable metadata without importing or opening a network client."""

    name = "fake"

    def __init__(self, *, model: str = "fake-semantic-v1") -> None:
        self.model = model
        self.calls = 0
        self.requests: list[SemanticRequest] = []

    def generate(self, request: SemanticRequest) -> SemanticResponse:
        self.calls += 1
        self.requests.append(request)
        reference = dict(request.reference_data)
        fallback = str(reference.get("fallback_display_name") or request.asset_id)
        if request.asset_type == "table":
            columns = reference.get("normalized_columns") or []
            fields = []
            for column in columns:
                name = str(column)
                fields.append(
                    {
                        "source_column": name,
                        "semantic_name": name,
                        "description": f"Fake description for {name}",
                        "semantic_type": "string",
                        "unit": None,
                        "aliases": [],
                        "confidence": 0.5,
                    }
                )
            payload = {
                "display_name": f"Fake | {fallback}",
                "category": "fake-test",
                "description": "Deterministic fake semantic metadata.",
                "keywords": ["fake", "offline"],
                "summary": f"Offline fake summary for {fallback}.",
                "semantic_fields": fields,
                "confidence": 0.5,
            }
        else:
            payload = {
                "display_name": f"Fake | {fallback}",
                "category": "fake-test",
                "description": "Deterministic fake semantic metadata.",
                "keywords": ["fake", "offline"],
                "summary": f"Offline fake summary for {fallback}.",
                "confidence": 0.5,
            }
        return SemanticResponse(payload=payload, provider=self.name, model=self.model)
