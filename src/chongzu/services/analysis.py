"""Small, bounded service contract for the future AI Data Analysis phase.

This is deliberately an adapter over the existing catalog, lexical search,
and safe-SQL services.  It does not add chat, embeddings, RAG, or a second
asset model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chongzu import paths
from chongzu.registry import Registry

from .catalog import CatalogService
from .sql import SqlQueryService, SqlServiceError
from chongzu.search import SearchQuery, SearchService, SearchValidationError


ANALYSIS_CONTRACT_VERSION = "analysis-context-v1"
MAX_ANALYSIS_ASSETS = 8
MAX_ANALYSIS_SAMPLE_ROWS = 20
MAX_ANALYSIS_TEXT_CHARS = 12_000
MAX_ANALYSIS_CHUNKS = 64


class AnalysisServiceError(ValueError):
    """Safe boundary error for a future AI analysis caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AnalysisService:
    """Compose bounded local evidence for an explicitly selected asset set."""

    def __init__(
        self,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        catalog: CatalogService | None = None,
        search: SearchService | None = None,
        sql: SqlQueryService | None = None,
    ) -> None:
        self.registry_path = Path(registry_path or paths.REGISTRY_PATH).resolve()
        self.workspace_root = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()
        self.catalog = catalog or CatalogService(
            registry_path=self.registry_path,
            workspace_root=self.workspace_root,
        )
        self.search_service = search or SearchService(registry_path=self.registry_path)
        self.sql_service = sql or SqlQueryService(
            registry_path=self.registry_path,
            workspace_root=self.workspace_root,
        )

    @staticmethod
    def _asset_ids(value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise AnalysisServiceError("assets_required", "assetIds must contain one or more assets")
        if len(value) > MAX_ANALYSIS_ASSETS:
            raise AnalysisServiceError(
                "too_many_assets",
                f"at most {MAX_ANALYSIS_ASSETS} assets may be selected",
            )
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 160:
                raise AnalysisServiceError("invalid_asset_id", "assetIds must contain bounded strings")
            item = item.strip()
            if item in result:
                raise AnalysisServiceError("duplicate_asset_id", "assetIds must not contain duplicates")
            result.append(item)
        return result

    @staticmethod
    def _parse_provenance(value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def _text_chunks(self, asset_id: str) -> list[dict[str, Any]]:
        registry = Registry.open_reader(self.registry_path)
        try:
            cursor = registry.connection.execute(
                """
                SELECT chunk_id, chunk_index, text, char_start, char_end, provenance_json
                FROM text_chunks
                WHERE text_asset_id=?
                ORDER BY chunk_index
                LIMIT ?
                """,
                [asset_id, MAX_ANALYSIS_CHUNKS],
            )
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            registry.close()
        return [
            {
                "chunkId": row.get("chunk_id"),
                "index": row.get("chunk_index"),
                "text": str(row.get("text") or "")[:MAX_ANALYSIS_TEXT_CHARS],
                "charStart": row.get("char_start"),
                "charEnd": row.get("char_end"),
                "provenance": self._parse_provenance(row.get("provenance_json")),
            }
            for row in rows
        ]

    def _one(self, asset_id: str) -> dict[str, Any]:
        detail = self.catalog.asset_detail(asset_id)
        if detail is None:
            raise AnalysisServiceError("asset_not_found", "one or more selected assets were not found")
        asset_type = str(detail.get("assetType") or "")
        context: dict[str, Any] = {
            "assetId": detail.get("assetId"),
            "assetType": asset_type,
            "displayName": detail.get("displayName"),
            "fallbackDisplayName": detail.get("fallbackDisplayName"),
            "semantic": detail.get("semantic"),
            "source": detail.get("source"),
            "provenance": detail.get("provenance"),
            "dimensions": detail.get("dimensions"),
            "profile": detail.get("profile"),
            "extractorMetadata": detail.get("extractorMetadata"),
        }
        if asset_type == "table":
            try:
                schema = self.sql_service.schema([asset_id])
            except SqlServiceError as exc:
                raise AnalysisServiceError(exc.code, exc.message) from exc
            relation = schema.relations[0] if schema.relations else None
            preview = self.catalog.table_preview(
                asset_id,
                layer="normalized",
                limit=MAX_ANALYSIS_SAMPLE_ROWS,
                offset=0,
            )
            context.update(
                {
                    "schema": relation.as_dict() if relation else None,
                    "rowCount": relation.row_count if relation else detail.get("dimensions", {}).get("rows"),
                    "sampleRows": preview.get("rows", []),
                    "profiling": detail.get("profile"),
                }
            )
        elif asset_type == "text":
            preview = self.catalog.text_preview(
                asset_id,
                offset=0,
                limit=MAX_ANALYSIS_TEXT_CHARS,
            )
            context.update(
                {
                    "title": detail.get("displayName"),
                    "text": str(preview.get("text") or "")[:MAX_ANALYSIS_TEXT_CHARS],
                    "chunks": self._text_chunks(asset_id),
                }
            )
        else:
            raise AnalysisServiceError("asset_type_unsupported", "only table and text assets can be analyzed")
        return context

    def asset_context(self, asset_ids: object) -> dict[str, Any]:
        ids = self._asset_ids(asset_ids)
        return {
            "contract": ANALYSIS_CONTRACT_VERSION,
            "assets": [self._one(asset_id) for asset_id in ids],
            "limits": {
                "maxAssets": MAX_ANALYSIS_ASSETS,
                "maxSampleRows": MAX_ANALYSIS_SAMPLE_ROWS,
                "maxTextChars": MAX_ANALYSIS_TEXT_CHARS,
                "maxChunks": MAX_ANALYSIS_CHUNKS,
            },
            "capabilities": ["catalog_metadata", "lexical_search", "safe_sql"],
        }

    def search(self, query: object, **kwargs: Any) -> dict[str, Any]:
        if not isinstance(query, str):
            raise AnalysisServiceError("invalid_search", "query must be a string")
        try:
            request = SearchQuery(query=query, **kwargs)
            return self.search_service.search(request).as_dict()
        except SearchValidationError as exc:
            raise AnalysisServiceError("invalid_search", str(exc)) from exc

    def safe_sql(self, asset_ids: object, sql: object) -> dict[str, Any]:
        if not isinstance(sql, str):
            raise AnalysisServiceError("invalid_sql", "sql must be a string")
        try:
            return self.sql_service.execute(asset_ids, sql)
        except SqlServiceError as exc:
            raise AnalysisServiceError(exc.code, exc.message) from exc
