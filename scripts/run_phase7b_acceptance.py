"""Run the explicitly authorized Phase 7B acceptance on synthetic assets only.

The script intentionally emits counts and validated semantic metadata, never a
prompt, API key, response envelope, or traceback.  It uses an isolated
temporary registry/workspace so the existing catalog and user files are not
selected by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from dongjian import paths
from dongjian.clean import process_source
from dongjian.registry import Registry
from dongjian.semantic.config import load_semantic_config
from dongjian.semantic.input_builder import build_semantic_request
from dongjian.semantic.openai_compatible import OpenAICompatibleProvider
from dongjian.semantic.runner import SemanticRunner, provider_for_name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_json(value: object) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded
    return {}


def _write_synthetic_source(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "synthetic_measurements.csv").write_text(
        "sample_id,temperature_c,material_code\n"
        "SYN-001,21.5,ALPHA\n"
        "SYN-002,22.0,BETA\n"
        "SYN-003,20.7,ALPHA\n",
        encoding="utf-8",
    )
    (source / "synthetic_note.txt").write_text(
        "Synthetic non-sensitive project note. The measured samples are examples "
        "for provider acceptance and are not research-corpus content.\n",
        encoding="utf-8",
    )


def _metadata(registry: Registry, asset_id: str) -> dict[str, Any] | None:
    cursor = registry.connection.execute(
        """
        SELECT display_name, category, description, keywords_json, summary,
               semantic_fields_json, model, prompt_version, confidence,
               generated_at, semantic_run_id, input_hash
        FROM semantic_metadata
        WHERE asset_id=? AND current=TRUE
        ORDER BY generated_at DESC, semantic_run_id DESC
        LIMIT 1
        """,
        [asset_id],
    )
    row = cursor.fetchone()
    if row is None:
        return None
    result = dict(zip((item[0] for item in cursor.description), row))
    for key in ("keywords_json", "semantic_fields_json"):
        result[key.removesuffix("_json")] = _parse_json(result.pop(key))
    return result


def _semantic_run_metadata(registry: Registry, asset_id: str) -> dict[str, Any]:
    row = registry.connection.execute(
        """
        SELECT input_metadata_json, status, provider, model, prompt_version,
               semantic_run_id, error_code, error_message
        FROM semantic_runs
        WHERE asset_id=?
        ORDER BY started_at DESC, semantic_run_id DESC
        LIMIT 1
        """,
        [asset_id],
    ).fetchone()
    if row is None:
        return {}
    input_metadata = _parse_json(row[0])
    return {
        "input": input_metadata if isinstance(input_metadata, dict) else {},
        "status": row[1],
        "provider": row[2],
        "model": row[3],
        "prompt_version": row[4],
        "semantic_run_id": row[5],
        "error_code": row[6],
        "error_message": row[7],
    }


def _metadata_summary(metadata: dict[str, Any] | None, *, asset_type: str) -> dict[str, Any]:
    if not metadata:
        return {"present": False}
    fields = metadata.get("semantic_fields") or []
    field_summary = []
    if asset_type == "table" and isinstance(fields, list):
        field_summary = [
            {
                "source_column": item.get("source_column"),
                "semantic_name": item.get("semantic_name"),
                "confidence": item.get("confidence"),
            }
            for item in fields
            if isinstance(item, dict)
        ]
    return {
        "present": True,
        "display_name": metadata.get("display_name"),
        "category": metadata.get("category"),
        "description": metadata.get("description"),
        "keywords": metadata.get("keywords"),
        "summary": metadata.get("summary"),
        "confidence": metadata.get("confidence"),
        "semantic_fields": field_summary,
        "model": metadata.get("model"),
        "prompt_version": metadata.get("prompt_version"),
        "semantic_run_id": metadata.get("semantic_run_id"),
    }


def _asset_artifact_hashes(details: dict[str, Any], workspace: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for layer in ("raw_artifact_path", "normalized_artifact_path"):
        value = details.get(layer)
        if value:
            path = (workspace / str(value)).resolve()
            hashes[layer.removesuffix("_artifact_path")] = _sha256(path)
    return hashes


def run(*, allow_real_provider: bool) -> tuple[int, dict[str, Any]]:
    config = load_semantic_config(paths.PROJECT_ROOT)
    report: dict[str, Any] = {
        "phase": "7B",
        "api_key": "configured" if config.api_key else "not configured",
        "provider": "openai-compatible",
        "model": config.model if config.configured else "not configured",
        "timeout_seconds": config.timeout_seconds,
        "max_retries": config.max_retries,
        "qwen_acceptance": "NOT RUN",
        "synthetic_only": True,
        "real_provider_calls": 0,
    }
    if not config.configured:
        report["status"] = "BLOCKED_NOT_CONFIGURED"
        return 2, report
    if not allow_real_provider:
        report["status"] = "BLOCKED_EXPLICIT_AUTHORIZATION_REQUIRED"
        return 2, report

    config.validate_for_use()
    provider = provider_for_name("openai-compatible", config, allow_real_provider=True)
    if not isinstance(provider, OpenAICompatibleProvider):
        raise RuntimeError("provider-neutral OpenAI-compatible provider was not constructed")
    report["request_url"] = provider.endpoint
    report["response_format"] = {"type": "json_object"}
    report["url_contract"] = provider.endpoint.casefold().endswith("/chat/completions") and "/v1/v1/" not in provider.endpoint.casefold()

    with tempfile.TemporaryDirectory(prefix="phase7b-", dir=str(paths.TEMP_ROOT)) as temporary:
        root = Path(temporary)
        source = root / "synthetic-source"
        workspace = root / "workspace"
        registry_path = workspace / "state" / "registry.duckdb"
        _write_synthetic_source(source)
        process = process_source(
            source,
            workers=1,
            force=True,
            registry_path=registry_path,
            workspace_root=workspace,
        )
        report["synthetic_process"] = {
            "table_assets": process.table_assets,
            "text_assets": process.text_assets,
            "cleaning_failures": process.cleaning_failures,
        }
        registry = Registry.open(registry_path)
        try:
            assets = registry.list_catalog_assets(limit=20)
            tables = [item for item in assets if item.get("asset_type") == "table"]
            texts = [item for item in assets if item.get("asset_type") == "text"]
            if len(tables) != 1 or len(texts) != 1:
                raise RuntimeError("synthetic source did not produce exactly one table and one text asset")
            table_id = str(tables[0]["asset_id"])
            text_id = str(texts[0]["asset_id"])
            before = {
                "table": registry.catalog_asset_details(table_id),
                "text": registry.catalog_asset_details(text_id),
            }
            if any(item is None or item.get("semantic_status") != "pending" for item in before.values()):
                raise RuntimeError("synthetic assets were not pending before enrichment")
            before_hashes = {
                "table": _asset_artifact_hashes(before["table"], workspace),
                "text": _asset_artifact_hashes(before["text"], workspace),
            }

            runner = SemanticRunner(
                registry,
                provider,
                workspace_root=workspace,
                allow_real_provider=True,
            )
            table_result = runner.enrich(asset_id=table_id)
            table_after = registry.catalog_asset_details(table_id)
            table_metadata = _metadata(registry, table_id)
            table_run = _semantic_run_metadata(registry, table_id)
            text_result = runner.enrich(asset_id=text_id)
            text_after = registry.catalog_asset_details(text_id)
            text_metadata = _metadata(registry, text_id)
            text_run = _semantic_run_metadata(registry, text_id)

            table_columns = set(build_semantic_request(table_after, workspace_root=workspace, model=provider.model).validation_columns)
            returned_columns = {
                str(item.get("source_column"))
                for item in (table_metadata or {}).get("semantic_fields", {})
                if isinstance(item, dict)
            }
            table_ok = (
                table_result.enriched == 1
                and table_after is not None
                and table_after.get("semantic_status") == "enriched"
                and table_after.get("effective_display_name") == table_metadata.get("display_name")
                and table_columns
                and returned_columns
                and returned_columns <= table_columns
            )
            text_ok = (
                text_result.enriched == 1
                and text_after is not None
                and text_after.get("semantic_status") == "enriched"
                and text_after.get("effective_display_name") == text_metadata.get("display_name")
            )
            after_hashes = {
                "table": _asset_artifact_hashes(table_after, workspace),
                "text": _asset_artifact_hashes(text_after, workspace),
            }
            report["table"] = {
                "asset_id": table_id,
                "pass": bool(table_ok),
                "result": {
                    "enriched": table_result.enriched,
                    "failed": table_result.failed,
                    "quality_suggestions": table_result.quality_suggestions,
                },
                "failures": table_result.failures,
                "catalog": {
                    "semantic_status": table_after.get("semantic_status") if table_after else None,
                    "semantic_model": table_after.get("semantic_model") if table_after else None,
                    "semantic_confidence": table_after.get("semantic_confidence") if table_after else None,
                    "effective_display_name": table_after.get("effective_display_name") if table_after else None,
                },
                "metadata": _metadata_summary(table_metadata, asset_type="table"),
                "run": table_run,
                "hashes": {
                    "before": before_hashes["table"],
                    "after": after_hashes["table"],
                    "unchanged": before_hashes["table"] == after_hashes["table"],
                },
                "asset_id_stable": table_id == str(table_after.get("asset_id")) if table_after else False,
                "strict_validation": "PASS" if table_result.enriched == 1 else "FAIL",
            }
            report["text"] = {
                "asset_id": text_id,
                "pass": bool(text_ok),
                "result": {
                    "enriched": text_result.enriched,
                    "failed": text_result.failed,
                    "quality_suggestions": text_result.quality_suggestions,
                },
                "failures": text_result.failures,
                "catalog": {
                    "semantic_status": text_after.get("semantic_status") if text_after else None,
                    "semantic_model": text_after.get("semantic_model") if text_after else None,
                    "semantic_confidence": text_after.get("semantic_confidence") if text_after else None,
                    "effective_display_name": text_after.get("effective_display_name") if text_after else None,
                },
                "metadata": _metadata_summary(text_metadata, asset_type="text"),
                "run": text_run,
                "hashes": {
                    "before": before_hashes["text"],
                    "after": after_hashes["text"],
                    "unchanged": before_hashes["text"] == after_hashes["text"],
                },
                "asset_id_stable": text_id == str(text_after.get("asset_id")) if text_after else False,
                "strict_validation": "PASS" if text_result.enriched == 1 else "FAIL",
            }

            if table_result.enriched == 1 and table_metadata is not None:
                calls_before_reuse = provider.call_count
                reuse_result = runner.enrich(asset_id=table_id)
                report["cache_second_run"] = {
                    "status": "REUSE" if reuse_result.reused == 1 else "NOT_REUSED",
                    "reused": reuse_result.reused,
                    "provider_calls_delta": provider.call_count - calls_before_reuse,
                }
            else:
                report["cache_second_run"] = {"status": "SKIPPED_AFTER_INITIAL_FAILURE", "provider_calls_delta": 0}
            report["request_audit"] = {
                "table": table_run.get("input", {}),
                "text": text_run.get("input", {}),
                "request_payload_bytes": provider.request_payload_bytes_history,
                "response_payload_bytes": provider.response_payload_bytes_history,
                "usage": provider.usage_history,
            }
            report["real_provider_calls"] = provider.call_count
            report["provider_instrumentation"] = {
                "request_url": provider.last_endpoint,
                "request_payload_bytes_total": provider.request_payload_bytes,
                "response_payload_bytes_total": provider.response_payload_bytes,
                "usage_available": bool(provider.usage_history),
            }
        finally:
            registry.close()
    report["status"] = "PASS" if report.get("table", {}).get("pass") and report.get("text", {}).get("pass") and report.get("cache_second_run", {}).get("status") == "REUSE" and report.get("cache_second_run", {}).get("provider_calls_delta") == 0 else "FAIL"
    return (0 if report["status"] == "PASS" else 1), report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7B synthetic-only real provider acceptance")
    parser.add_argument(
        "--allow-real-provider",
        action="store_true",
        help="explicitly authorize the configured provider for these two synthetic requests",
    )
    args = parser.parse_args()
    try:
        code, report = run(allow_real_provider=args.allow_real_provider)
    except Exception as exc:
        # Never print a traceback or configuration value from this acceptance
        # path.  Provider errors are already mapped to safe codes by the
        # adapter; an unexpected exception is reported only by type/message.
        code = 1
        report = {"phase": "7B", "status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps({key: _json_value(value) for key, value in report.items()}, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
