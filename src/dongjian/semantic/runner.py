"""Single-writer semantic enrichment coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Mapping
from uuid import uuid4

from .. import paths
from ..registry import Registry, canonical_source_root, utc_now
from .config import SemanticConfig, load_semantic_config
from .fake_provider import FakeSemanticProvider
from .input_builder import SemanticInputLimits, build_semantic_request
from .models import SemanticRequest, SemanticResponse, sha256_json
from .validator import validate_semantic_response


class SemanticNotConfigured(RuntimeError):
    """Optional semantic enrichment has no complete project-local .env."""


class RealSemanticProviderDisabled(RuntimeError):
    """Guard preventing real endpoint calls without explicit authorization."""


def _safe_provider_error(provider: object, value: object) -> str:
    """Bound provider detail without allowing the configured key to persist."""

    message = str(value).strip()
    config = getattr(provider, "config", None)
    secret = getattr(config, "api_key", "") if config is not None else ""
    if isinstance(secret, str) and secret:
        message = message.replace(secret, "[REDACTED]")
    return message[:4_000]


@dataclass
class SemanticSummary:
    provider: str
    model: str
    source_root: str | None = None
    assets_considered: int = 0
    attempted: int = 0
    enriched: int = 0
    reused: int = 0
    failed: int = 0
    skipped: int = 0
    warnings: int = 0
    quality_suggestions: int = 0
    source_chars: int = 0
    sent_chars: int = 0
    sampled_rows: int = 0
    input_truncated: int = 0
    provider_calls: int = 0
    request_payload_bytes: int = 0
    response_payload_bytes: int = 0
    wall_time_ms: float = 0.0
    failures: list[dict[str, str]] = field(default_factory=list)


def semantic_status(project_root: Path | None = None) -> dict[str, str | bool]:
    try:
        config = load_semantic_config(project_root)
    except Exception:
        # Keep parse/validation failures distinguishable without echoing any
        # .env value.  A malformed config never enables a network call.
        return {
            "provider": "configuration error",
            "model": "not configured",
            "llm_status": "INVALID_CONFIGURATION",
            "network_calls": False,
            "configuration_error": True,
        }
    return {
        "provider": "configured" if config.configured else "not configured",
        "model": config.model if config.configured else "not configured",
        "llm_status": config.status,
        "network_calls": False,
        "configuration_error": False,
    }


def provider_for_name(
    name: str,
    config: SemanticConfig | None = None,
    *,
    allow_real_provider: bool = False,
):
    normalized = name.casefold().replace("_", "-")
    if normalized == "fake":
        return FakeSemanticProvider()
    if normalized in {"openai-compatible", "openai", "http"}:
        selected = config or load_semantic_config()
        if not selected.configured:
            raise SemanticNotConfigured("Semantic enrichment is not configured.")
        if not allow_real_provider:
            raise RealSemanticProviderDisabled(
                "Real semantic provider requires the explicit --allow-real-provider authorization."
            )
        from .openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(selected)
    raise ValueError(f"unknown semantic provider: {name}")


def semantic_identity(request: SemanticRequest, provider_name: str) -> str:
    return sha256_json(
        {
            "asset_id": request.asset_id,
            "asset_type": request.asset_type,
            "normalized_artifact_identity": request.normalized_artifact_identity,
            "model": request.model,
            "prompt_version": request.prompt_version,
            "semantic_config_version": request.config_version,
            "provider": provider_name,
            "input_hash": request.input_hash,
        }
    )


class SemanticRunner:
    def __init__(
        self,
        registry: Registry,
        provider: object,
        *,
        workspace_root: Path = paths.WORKSPACE_ROOT,
        config_version: str = paths.SEMANTIC_CONFIG_VERSION,
        limits: SemanticInputLimits | None = None,
        allow_real_provider: bool = False,
    ) -> None:
        self.registry = registry
        provider_name = str(getattr(provider, "name", provider.__class__.__name__)).casefold().replace("_", "-")
        if provider_name in {"openai-compatible", "openai", "http"} and not allow_real_provider:
            raise RealSemanticProviderDisabled(
                "Real semantic provider requires the explicit --allow-real-provider authorization."
            )
        self.provider = provider
        self.workspace_root = Path(workspace_root).resolve()
        self.config_version = config_version
        self.limits = limits or SemanticInputLimits()

    @staticmethod
    def _provider_counter(provider: object, name: str) -> int:
        value = getattr(provider, name, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    @property
    def provider_name(self) -> str:
        return str(getattr(self.provider, "name", self.provider.__class__.__name__.casefold()))

    @property
    def model(self) -> str:
        return str(getattr(self.provider, "model", "semantic-model"))

    def _chunks(self, asset_id: str) -> list[dict[str, Any]]:
        cursor = self.registry.connection.execute(
            "SELECT chunk_index, text, char_start, char_end FROM text_chunks WHERE text_asset_id=? ORDER BY chunk_index",
            [asset_id],
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _build_request(self, details: Mapping[str, Any], prompt_version: str | None, model: str) -> SemanticRequest:
        kwargs: dict[str, Any] = {
            "workspace_root": self.workspace_root,
            "model": model,
            "config_version": self.config_version,
            "prompt_version": prompt_version,
            "limits": self.limits,
        }
        if details.get("asset_type") == "text":
            kwargs["chunks"] = self._chunks(str(details["asset_id"]))
        return build_semantic_request(details, **kwargs)

    def _record_failure_without_run(self, summary: SemanticSummary, asset_id: str, code: str, message: str) -> None:
        summary.failed += 1
        summary.failures.append({"asset_id": asset_id, "error_code": code, "error": message})

    def enrich(
        self,
        *,
        source: Path | str | None = None,
        asset_id: str | None = None,
        asset_type: str | None = None,
        force: bool = False,
        limit: int | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
    ) -> SemanticSummary:
        started = time.perf_counter_ns()
        source_root = canonical_source_root(source) if source is not None else None
        if asset_type is not None and asset_type not in {"table", "text"}:
            raise ValueError("asset_type must be table or text")
        selected_model = model or self.model
        rows = self.registry.list_catalog_assets(
            source_root=source_root,
            asset_type=asset_type,
            limit=100_000,
        )
        if asset_id is not None:
            rows = [row for row in rows if str(row.get("asset_id")) == asset_id]
            if not rows:
                raise ValueError(f"catalog asset not found: {asset_id}")
        if limit is not None:
            if limit < 1 or limit > 100_000:
                raise ValueError("limit must be between 1 and 100000")
            rows = rows[:limit]
        summary = SemanticSummary(provider=self.provider_name, model=selected_model, source_root=source_root, assets_considered=len(rows))
        calls_before = self._provider_counter(self.provider, "call_count")
        if not calls_before:
            calls_before = self._provider_counter(self.provider, "calls")
        request_bytes_before = self._provider_counter(self.provider, "request_payload_bytes")
        response_bytes_before = self._provider_counter(self.provider, "response_payload_bytes")
        for row in rows:
            current_asset_id = str(row["asset_id"])
            details = self.registry.catalog_asset_details(current_asset_id)
            if details is None:
                self._record_failure_without_run(summary, current_asset_id, "asset_not_found", "catalog asset disappeared")
                continue
            try:
                request = self._build_request(details, prompt_version, selected_model)
            except Exception as exc:  # one asset cannot stop the remaining catalog
                self._record_failure_without_run(summary, current_asset_id, "input_build_failed", str(exc))
                continue
            input_metadata = dict(request.input_metadata)
            summary.source_chars += int(input_metadata.get("source_chars") or 0)
            summary.sent_chars += int(input_metadata.get("sent_chars") or 0)
            summary.sampled_rows += int(input_metadata.get("sampled_rows") or 0)
            summary.input_truncated += int(bool(input_metadata.get("input_truncated")))
            identity = semantic_identity(request, self.provider_name)
            if not force and self.registry.semantic_reusable(identity, request.asset_id, request.asset_type):
                summary.reused += 1
                continue
            summary.attempted += 1
            semantic_run_id = f"sem_{uuid4().hex}"
            self.registry.start_semantic_run(
                semantic_run_id=semantic_run_id,
                semantic_identity=identity,
                asset_id=request.asset_id,
                asset_type=request.asset_type,
                file_id=str(details["file_id"]),
                content_sha256=str(details["content_sha256"]),
                normalized_artifact_identity=request.normalized_artifact_identity,
                model=request.model,
                prompt_version=request.prompt_version,
                config_version=request.config_version,
                input_hash=request.input_hash,
                provider=self.provider_name,
                input_metadata=input_metadata,
                started_at=utc_now(),
                force=force,
            )
            try:
                response = self.provider.generate(request)
                if not isinstance(response, SemanticResponse):
                    raise TypeError("semantic provider returned a non-contract response")
                validation = validate_semantic_response(response, request)
                summary.warnings += len(validation.warnings)
                if not validation.valid or validation.metadata is None:
                    message = "; ".join(validation.errors) or "semantic response failed validation"
                    self.registry.record_semantic_failure(
                        semantic_run_id,
                        finished_at=utc_now(),
                        error_code=validation.error_code or "semantic_validation_failed",
                        error_message=message,
                        warnings=validation.warnings,
                    )
                    self._record_failure_without_run(summary, current_asset_id, "semantic_validation_failed", message)
                    continue
                metadata = validation.metadata.as_dict()
                suggestions = metadata.pop("quality_suggestions", [])
                summary.quality_suggestions += len(suggestions)
                self.registry.record_semantic_success(
                    semantic_run_id=semantic_run_id,
                    asset_id=request.asset_id,
                    asset_type=request.asset_type,
                    metadata=metadata,
                    generated_at=utc_now(),
                    finished_at=utc_now(),
                    warnings=validation.warnings,
                    suggestions=suggestions if isinstance(suggestions, list) else [],
                )
                summary.enriched += 1
            except Exception as exc:  # provider/DB errors remain isolated to one asset
                message = _safe_provider_error(self.provider, exc)
                self.registry.record_semantic_failure(
                    semantic_run_id,
                    finished_at=utc_now(),
                    error_code=str(getattr(exc, "code", "semantic_provider_error")),
                    error_message=message,
                )
                self._record_failure_without_run(
                    summary,
                    current_asset_id,
                    str(getattr(exc, "code", "semantic_provider_error")),
                    message,
                )
        summary.wall_time_ms = (time.perf_counter_ns() - started) / 1_000_000
        calls_after = self._provider_counter(self.provider, "call_count")
        if not calls_after:
            calls_after = self._provider_counter(self.provider, "calls")
        summary.provider_calls = max(0, calls_after - calls_before)
        summary.request_payload_bytes = max(
            0, self._provider_counter(self.provider, "request_payload_bytes") - request_bytes_before
        )
        summary.response_payload_bytes = max(
            0, self._provider_counter(self.provider, "response_payload_bytes") - response_bytes_before
        )
        return summary


def enrich_catalog(
    *,
    source: Path | str | None = None,
    asset_id: str | None = None,
    asset_type: str | None = None,
    provider_name: str = "openai-compatible",
    force: bool = False,
    limit: int | None = None,
    registry_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    prompt_version: str | None = None,
    allow_real_provider: bool = False,
) -> SemanticSummary:
    normalized_provider = provider_name.casefold().replace("_", "-")
    if normalized_provider == "fake":
        # The deterministic provider must remain usable even when an optional
        # user .env is absent or malformed; it has no configuration dependency.
        provider = provider_for_name(provider_name)
    else:
        config = load_semantic_config()
        provider = provider_for_name(provider_name, config, allow_real_provider=allow_real_provider)
    registry = Registry.open(registry_path, initialize=False)
    try:
        runner = SemanticRunner(
            registry,
            provider,
            workspace_root=Path(workspace_root or paths.WORKSPACE_ROOT),
            allow_real_provider=allow_real_provider,
        )
        return runner.enrich(
            source=source,
            asset_id=asset_id,
            asset_type=asset_type,
            force=force,
            limit=limit,
            prompt_version=prompt_version,
        )
    finally:
        registry.close()
