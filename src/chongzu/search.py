"""Deterministic local lexical retrieval contracts and implementation.

Phase 9 deliberately keeps retrieval small and inspectable.  The backend reads
the current catalog view, text chunks, table column metadata, and bounded
profile/semantic metadata directly from the local DuckDB registry.  It does
not materialize full table cells or install a DuckDB extension, so newly
processed assets become searchable without a rebuild step.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import time
import unicodedata
from typing import Any, Iterable, Mapping, Protocol, Sequence

from chongzu import paths
from chongzu.registry import Registry


SEARCH_INDEX_VERSION = "lexical-live-v1"
SEARCH_BACKEND = "duckdb-live-catalog"
MAX_QUERY_CHARS = 512
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_OFFSET = 10_000_000
MAX_SNIPPET_CHARS = 500
MAX_METADATA_VALUE_CHARS = 2_000
MAX_METADATA_ITEMS = 100
MAX_RESULTS_PER_ASSET = 3

# Ranking is intentionally centralized and documented.  These are local
# ordinal scores, not semantic relevance probabilities.
RANK_WEIGHTS: dict[str, float] = {
    "effective_display_name": 120.0,
    "semantic_display_name": 118.0,
    "fallback_display_name": 110.0,
    "source_file": 92.0,
    "source_root": 80.0,
    "sheet_name": 78.0,
    "column_name": 76.0,
    "semantic_metadata": 70.0,
    "sample_value": 42.0,
    "source_format": 38.0,
    "text_phrase": 105.0,
    "text_token": 82.0,
}
QUALITY_PENALTIES: dict[str, float] = {"ready": 0.0, "needs_review": -4.0, "unusable": -18.0}


class SearchValidationError(ValueError):
    """Raised when a search request falls outside the local contract."""


@dataclass(frozen=True)
class SearchQuery:
    query: str
    asset_type: str = "all"
    source_format: str | None = None
    quality_status: str | None = None
    limit: int = 30
    offset: int = 0
    match: str = "all"

    def validated(self) -> "SearchQuery":
        value = unicodedata.normalize("NFC", self.query).strip()
        if len(value) > MAX_QUERY_CHARS:
            raise SearchValidationError(f"query must be at most {MAX_QUERY_CHARS} characters")
        if "\x00" in value:
            raise SearchValidationError("query contains an invalid control character")
        if self.asset_type not in {"all", "table", "text"}:
            raise SearchValidationError("type must be all, table, or text")
        if self.quality_status not in {None, "ready", "needs_review", "unusable"}:
            raise SearchValidationError("quality must be ready, needs_review, or unusable")
        source_format = self.source_format.strip().casefold() if self.source_format else None
        if source_format and len(source_format) > 32:
            raise SearchValidationError("format is too long")
        if self.match not in {"all", "phrase"}:
            raise SearchValidationError("match must be all or phrase")
        if self.limit < 1 or self.limit > MAX_SEARCH_LIMIT:
            raise SearchValidationError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if self.offset < 0 or self.offset > MAX_SEARCH_OFFSET:
            raise SearchValidationError(f"offset must be between 0 and {MAX_SEARCH_OFFSET}")
        return SearchQuery(
            query=value,
            asset_type=self.asset_type,
            source_format=source_format,
            quality_status=self.quality_status,
            limit=self.limit,
            offset=self.offset,
            match=self.match,
        )


@dataclass(frozen=True)
class SearchResult:
    result_id: str
    asset_id: str
    asset_type: str
    chunk_id: str | None
    display_name: str
    source_file: str
    source_format: str | None
    page_number: int | None
    sheet_name: str | None
    match_kind: str
    snippet: str
    score: float
    quality_status: str
    provenance: Mapping[str, Any]
    match_offsets: tuple[tuple[int, int], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "resultId": self.result_id,
            "assetId": self.asset_id,
            "assetType": self.asset_type,
            "chunkId": self.chunk_id,
            "displayName": self.display_name,
            "sourceFile": self.source_file,
            "sourceFormat": self.source_format,
            "pageNumber": self.page_number,
            "sheetName": self.sheet_name,
            "matchKind": self.match_kind,
            "snippet": self.snippet,
            "score": round(float(self.score), 4),
            "qualityStatus": self.quality_status,
            "matchOffsets": [[int(start), int(end)] for start, end in self.match_offsets],
            "provenance": _json_value(dict(self.provenance)),
        }


@dataclass(frozen=True)
class SearchResponse:
    query: str
    total: int
    limit: int
    offset: int
    results: tuple[SearchResult, ...]
    backend: str = SEARCH_BACKEND
    index_version: str = SEARCH_INDEX_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "results": [result.as_dict() for result in self.results],
            "backend": self.backend,
            "indexVersion": self.index_version,
        }


@dataclass(frozen=True)
class RetrievalReference:
    """Stable evidence object for future consumers such as RAG adapters."""

    reference_id: str
    asset_id: str
    chunk_id: str | None
    asset_type: str
    display_name: str
    source: Mapping[str, Any]
    page_number: int | None
    sheet_name: str | None
    excerpt: str
    provenance: Mapping[str, Any]
    score: float

    @classmethod
    def from_result(cls, result: SearchResult) -> "RetrievalReference":
        return cls(
            reference_id=result.result_id,
            asset_id=result.asset_id,
            chunk_id=result.chunk_id,
            asset_type=result.asset_type,
            display_name=result.display_name,
            source={
                "file": result.source_file,
                "format": result.source_format,
                "page": result.page_number,
                "sheet": result.sheet_name,
            },
            page_number=result.page_number,
            sheet_name=result.sheet_name,
            excerpt=result.snippet,
            provenance=result.provenance,
            score=result.score,
        )

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "referenceId": self.reference_id,
                "assetId": self.asset_id,
                "chunkId": self.chunk_id,
                "assetType": self.asset_type,
                "displayName": self.display_name,
                "source": dict(self.source),
                "pageNumber": self.page_number,
                "sheetName": self.sheet_name,
                "excerpt": self.excerpt,
                "provenance": dict(self.provenance),
                "score": self.score,
            }
        )


class TextRetriever(Protocol):
    def retrieve(self, query: str, *, limit: int) -> Sequence[SearchResult]:
        """Return deterministic local lexical evidence."""


class EmbeddingProvider(Protocol):
    """Future injection point only; Phase 9 does not implement embeddings."""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        ...


@dataclass(frozen=True)
class _TextMatch:
    kind: str
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _Candidate:
    result: SearchResult
    asset_order: str
    source_row: Mapping[str, Any]
    chunk_provenance: Any = None

    def hydrate(self) -> SearchResult:
        """Attach provenance only after ranking, capping, and pagination."""

        return replace(
            self.result,
            provenance=_provenance(
                self.source_row,
                chunk_id=self.result.chunk_id,
                chunk_provenance=self.chunk_provenance,
            ),
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):  # noqa: PLR0124
        return None
    return value


def _parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _find_spans(text: str, query: str, mode: str) -> tuple[tuple[int, int], ...]:
    if not text or not query:
        return ()
    folded_text = _fold(text)
    folded_query = _fold(query)
    if mode == "phrase":
        position = folded_text.find(folded_query)
        return ((position, position + len(folded_query)),) if position >= 0 else ()
    tokens = [_fold(token) for token in query.split() if token]
    if not tokens:
        return ()
    spans: list[tuple[int, int]] = []
    for token in tokens:
        position = folded_text.find(token)
        if position < 0:
            return ()
        spans.append((position, position + len(token)))
    return tuple(sorted(set(spans)))


def _match(text: str, query: str, mode: str) -> _TextMatch | None:
    if not text or not query:
        return None
    folded_text = _fold(text)
    folded_query = _fold(query)
    if mode == "phrase":
        position = folded_text.find(folded_query)
        if position < 0:
            return None
        return _TextMatch("phrase", ((position, position + len(folded_query)),))
    tokens = [_fold(token) for token in query.split() if token]
    if not tokens:
        return None
    spans: list[tuple[int, int]] = []
    for token in tokens:
        position = folded_text.find(token)
        if position < 0:
            return None
        spans.append((position, position + len(token)))
    if len(tokens) == 1 and folded_text == folded_query:
        kind = "exact"
    elif len(tokens) == 1:
        kind = "substring"
    else:
        kind = "tokens"
    return _TextMatch(kind, tuple(sorted(set(spans))))


def _snippet(text: str, query: str, mode: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    text = unicodedata.normalize("NFC", str(text))
    if len(text) <= MAX_SNIPPET_CHARS:
        spans = _find_spans(text, query, mode)
        return text, spans
    spans = _find_spans(text, query, mode)
    if spans:
        start = max(0, min(item[0] for item in spans) - MAX_SNIPPET_CHARS // 3)
        end = min(len(text), start + MAX_SNIPPET_CHARS)
        start = max(0, end - MAX_SNIPPET_CHARS)
    else:
        start, end = 0, MAX_SNIPPET_CHARS
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    value = prefix + text[start:end] + suffix
    adjusted = tuple((left - start + len(prefix), right - start + len(prefix)) for left, right in spans)
    return value, adjusted


def _string_items(value: Any, *, limit: int = MAX_METADATA_ITEMS) -> list[str]:
    parsed = _parse_json(value)
    if parsed is None:
        if value is None:
            return []
        return [str(value)[:MAX_METADATA_VALUE_CHARS]]
    if isinstance(parsed, Mapping):
        items: list[str] = []
        for key, item in list(parsed.items())[:limit]:
            if isinstance(item, (str, int, float, bool)):
                items.append(str(item)[:MAX_METADATA_VALUE_CHARS])
            elif key in {"name", "semantic_name", "source_column", "description", "unit"}:
                items.append(str(item)[:MAX_METADATA_VALUE_CHARS])
        return items
    if isinstance(parsed, list):
        items = []
        for item in parsed[:limit]:
            if isinstance(item, Mapping):
                for key in ("name", "semantic_name", "source_column", "value", "text", "description", "unit"):
                    if item.get(key) is not None:
                        items.append(str(item[key])[:MAX_METADATA_VALUE_CHARS])
            elif item is not None:
                items.append(str(item)[:MAX_METADATA_VALUE_CHARS])
        return items
    return [str(parsed)[:MAX_METADATA_VALUE_CHARS]]


def _profile_search_values(value: Any) -> tuple[list[str], list[str]]:
    parsed = _parse_json(value)
    if not isinstance(parsed, Mapping):
        return [], []
    names: list[str] = []
    samples: list[str] = []
    columns = parsed.get("columns")
    if isinstance(columns, list):
        for column in columns[:MAX_METADATA_ITEMS]:
            if not isinstance(column, Mapping):
                continue
            if column.get("name") is not None:
                names.append(str(column["name"])[:MAX_METADATA_VALUE_CHARS])
            values = column.get("sample_values")
            if isinstance(values, list):
                samples.extend(str(item)[:MAX_METADATA_VALUE_CHARS] for item in values[:5] if item is not None)
    return names, samples[:MAX_METADATA_ITEMS]


def _metadata_fields(row: Mapping[str, Any]) -> Iterable[tuple[str, str, str]]:
    for field_name, label in (
        ("effective_display_name", "name"),
        ("semantic_display_name", "semantic name"),
        ("fallback_display_name", "fallback name"),
        ("source_file", "source file"),
        ("source_root", "source path"),
        ("sheet_name", "sheet"),
        ("source_format", "format"),
        ("category", "category"),
    ):
        value = row.get(field_name)
        if value:
            yield field_name, str(value)[:MAX_METADATA_VALUE_CHARS], label
    for value in _string_items(row.get("columns_json")):
        yield "column_name", value, "column"
    profile_names, profile_samples = _profile_search_values(row.get("profile_json"))
    for value in profile_names:
        yield "column_name", value, "profile column"
    for value in profile_samples:
        yield "sample_value", value, "sample"
    for value in _string_items(row.get("keywords_json")):
        yield "semantic_metadata", value, "keyword"
    for value in _string_items(row.get("semantic_fields_json")):
        yield "semantic_metadata", value, "semantic field"


def _quality_adjustment(row: Mapping[str, Any]) -> float:
    return QUALITY_PENALTIES.get(str(row.get("quality_status") or "needs_review"), -4.0)


def _provenance(row: Mapping[str, Any], *, chunk_id: str | None = None, chunk_provenance: Any = None) -> dict[str, Any]:
    provenance = _parse_json(chunk_provenance)
    if not isinstance(provenance, Mapping):
        provenance = {}
    result = {
        "assetId": row.get("asset_id"),
        "fileId": row.get("file_id"),
        "contentSha256": row.get("content_sha256"),
        "sourceFile": row.get("source_file"),
        "sourceFormat": row.get("source_format"),
        "sourceKind": row.get("source_kind"),
        "pageNumber": row.get("page_number") or provenance.get("page_number"),
        "sheetName": row.get("sheet_name") or provenance.get("section"),
        "chunkId": chunk_id,
        "extractor": row.get("extractor"),
        "extractorVersion": row.get("extractor_version"),
        "extractionRunId": row.get("extraction_run_id"),
        "provenance": dict(provenance),
    }
    return result


def _result_id(asset_id: str, chunk_id: str | None, match_kind: str, snippet: str) -> str:
    identity = json.dumps([asset_id, chunk_id, match_kind, snippet], ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"sres_{hashlib.sha256(identity).hexdigest()[:32]}"


def _metadata_candidate(row: Mapping[str, Any], query: SearchQuery) -> _Candidate | None:
    best: tuple[float, str, str, _TextMatch] | None = None
    for field_name, value, label in _metadata_fields(row):
        matched = _match(value, query.query, query.match)
        if matched is None:
            continue
        base = RANK_WEIGHTS.get(field_name, RANK_WEIGHTS["semantic_metadata"])
        if matched.kind == "exact":
            score = base
        elif matched.kind == "phrase":
            score = base * 0.88
        elif matched.kind == "substring":
            score = base * 0.76
        else:
            score = base * 0.60
        candidate = (score, field_name, f"{label}: {value}", matched)
        if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
            best = candidate
    if best is None:
        return None
    score, field_name, body, matched = best
    snippet, offsets = _snippet(body, query.query, query.match)
    suffix = "exact" if matched.kind == "exact" else query.match if query.match == "phrase" else "contains"
    match_kind = f"{field_name}_{suffix}"
    asset_id = str(row.get("asset_id"))
    result = SearchResult(
        result_id=_result_id(asset_id, None, match_kind, snippet),
        asset_id=asset_id,
        asset_type=str(row.get("asset_type") or ""),
        chunk_id=None,
        display_name=str(row.get("effective_display_name") or row.get("fallback_display_name") or asset_id),
        source_file=str(row.get("source_file") or ""),
        source_format=str(row.get("source_format")) if row.get("source_format") else None,
        page_number=int(row["page_number"]) if row.get("page_number") is not None else None,
        sheet_name=str(row.get("sheet_name")) if row.get("sheet_name") else None,
        match_kind=match_kind,
        snippet=snippet,
        score=score + _quality_adjustment(row),
        quality_status=str(row.get("quality_status") or "needs_review"),
        # Provenance is hydrated only for the final result page.  Keeping the
        # candidate lightweight avoids reconstructing complete provenance for
        # every metadata hit before the per-asset cap and pagination window.
        provenance={},
        match_offsets=offsets,
    )
    return _Candidate(result=result, asset_order=asset_id, source_row=row)


def _chunk_candidate(row: Mapping[str, Any], query: SearchQuery) -> _Candidate | None:
    text = str(row.get("chunk_text") or "")
    matched = _match(text, query.query, query.match)
    if matched is None:
        return None
    snippet, offsets = _snippet(text, query.query, query.match)
    match_kind = "text_phrase" if query.match == "phrase" else "text_token"
    if query.match == "all" and len(query.query.split()) == 1 and matched.kind in {"exact", "substring"}:
        match_kind = "text_exact" if matched.kind == "exact" else "text_substring"
    asset_id = str(row.get("asset_id"))
    chunk_id = str(row.get("chunk_id")) if row.get("chunk_id") else None
    result = SearchResult(
        result_id=_result_id(asset_id, chunk_id, match_kind, snippet),
        asset_id=asset_id,
        asset_type="text",
        chunk_id=chunk_id,
        display_name=str(row.get("effective_display_name") or row.get("fallback_display_name") or asset_id),
        source_file=str(row.get("source_file") or ""),
        source_format=str(row.get("source_format")) if row.get("source_format") else None,
        page_number=int(row["page_number"]) if row.get("page_number") is not None else None,
        sheet_name=str(row.get("sheet_name")) if row.get("sheet_name") else None,
        match_kind=match_kind,
        snippet=snippet,
        score=RANK_WEIGHTS["text_phrase" if query.match == "phrase" else "text_token"] + _quality_adjustment(row),
        quality_status=str(row.get("quality_status") or "needs_review"),
        provenance={},
        match_offsets=offsets,
    )
    return _Candidate(
        result=result,
        asset_order=asset_id,
        source_row=row,
        chunk_provenance=row.get("provenance_json"),
    )


class SearchService:
    """Read-only lexical search over current registry/catalog records."""

    def __init__(self, *, registry_path: Path | str | None = None) -> None:
        self.registry_path = Path(registry_path or paths.REGISTRY_PATH).resolve()

    def _open(self) -> Registry:
        return Registry.open(self.registry_path)

    @staticmethod
    def _rows(registry: Registry, query: SearchQuery) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if query.asset_type != "all":
            clauses.append("asset_type=?")
            params.append(query.asset_type)
        if query.source_format:
            clauses.append("LOWER(source_format)=?")
            params.append(query.source_format)
        if query.quality_status:
            clauses.append("quality_status=?")
            params.append(query.quality_status)
        cursor = registry.connection.execute(
            f"SELECT * FROM catalog_assets WHERE {' AND '.join(clauses)} ORDER BY source_file, asset_type, asset_id",
            params,
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _add_search_metadata(registry: Registry, rows: list[dict[str, Any]]) -> None:
        """Attach bounded metadata without reading Parquet cell contents."""

        table_ids = [str(row["asset_id"]) for row in rows if row.get("asset_type") == "table"]
        if table_ids:
            placeholders = ",".join("?" for _ in table_ids)
            cursor = registry.connection.execute(
                f"SELECT table_id, columns_json FROM table_assets WHERE table_id IN ({placeholders})",
                table_ids,
            )
            columns = {str(asset_id): value for asset_id, value in cursor.fetchall()}
            for row in rows:
                row["columns_json"] = columns.get(str(row["asset_id"]))
        # Table profile JSON contains bounded column/sample metadata.  Text
        # profiles are not needed for source/name matching because their
        # searchable evidence is fetched from text_chunks below; avoiding a
        # read of every text profile keeps a catalog-sized query responsive.
        profile_paths = [
            str(row["profile_artifact_path"])
            for row in rows
            if row.get("profile_artifact_path") and row.get("asset_type") == "table"
        ]
        if profile_paths:
            placeholders = ",".join("?" for _ in profile_paths)
            cursor = registry.connection.execute(
                f"SELECT profile_artifact_path, profile_json FROM table_profiles WHERE profile_artifact_path IN ({placeholders}) UNION ALL SELECT profile_artifact_path, profile_json FROM text_profiles WHERE profile_artifact_path IN ({placeholders})",
                [*profile_paths, *profile_paths],
            )
            profiles = {str(path): value for path, value in cursor.fetchall()}
            for row in rows:
                row["profile_json"] = profiles.get(str(row.get("profile_artifact_path")))
        # Do not build an ``IN`` list containing every catalog asset.  With a
        # few thousand assets DuckDB spends seconds binding/planning a large
        # parameter list even when the current semantic table is empty.  The
        # current semantic rows are already bounded by the number of enriched
        # assets, so one batch read and an in-memory map is both cheaper and
        # future-compatible with semantic metadata search.
        cursor = registry.connection.execute(
            "SELECT asset_id, asset_type, keywords_json, semantic_fields_json "
            "FROM semantic_metadata WHERE current=TRUE"
        )
        semantic = {
            (str(asset_id), str(asset_type)): (keywords, fields)
            for asset_id, asset_type, keywords, fields in cursor.fetchall()
        }
        for row in rows:
            row["keywords_json"], row["semantic_fields_json"] = semantic.get(
                (str(row["asset_id"]), str(row["asset_type"])), (None, None)
            )

    @staticmethod
    def _chunk_rows(registry: Registry, query: SearchQuery) -> list[dict[str, Any]]:
        if query.asset_type == "table":
            return []
        clauses = ["c.asset_type='text'"]
        params: list[Any] = []
        if query.source_format:
            clauses.append("LOWER(c.source_format)=?")
            params.append(query.source_format)
        if query.quality_status:
            clauses.append("c.quality_status=?")
            params.append(query.quality_status)
        # Push a conservative substring prefilter into DuckDB so normal
        # queries do not transfer every TextChunk into Python.  Final matching
        # and offsets still use the canonical Unicode-aware matcher.  Queries
        # containing SQL LIKE wildcards fall back to the bounded Python scan;
        # their characters remain literal to the user-facing matcher.
        terms = [query.query] if query.match == "phrase" else [token for token in query.query.split() if token]
        if terms and not any(character in term for term in terms for character in ("%", "_")):
            for term in terms:
                clauses.append("LOWER(tc.text) LIKE ?")
                params.append(f"%{term.casefold()}%")
        cursor = registry.connection.execute(
            f"""
            SELECT c.asset_id, c.asset_type, c.file_id, c.content_sha256,
                   c.source_file, c.source_format, c.source_kind, c.page_number,
                   c.sheet_name, c.effective_display_name, c.fallback_display_name,
                   c.quality_status, c.extractor, c.extractor_version,
                   c.extraction_run_id, tc.chunk_id, tc.text AS chunk_text,
                   tc.provenance_json
            FROM catalog_assets c
            JOIN text_chunks tc ON tc.text_asset_id=c.asset_id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.source_file, c.page_number NULLS FIRST, tc.chunk_index, tc.chunk_id
            """,
            params,
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def search(self, request: SearchQuery) -> SearchResponse:
        query = request.validated()
        if not query.query:
            return SearchResponse(query="", total=0, limit=query.limit, offset=query.offset, results=())
        registry = self._open()
        try:
            rows = self._rows(registry, query)
            self._add_search_metadata(registry, rows)
            candidates: list[_Candidate] = []
            for row in rows:
                candidate = _metadata_candidate(row, query)
                if candidate is not None:
                    candidates.append(candidate)
            if query.asset_type in {"all", "text"}:
                for row in self._chunk_rows(registry, query):
                    candidate = _chunk_candidate(row, query)
                    if candidate is not None:
                        candidates.append(candidate)
        finally:
            registry.close()
        candidates.sort(
            key=lambda item: (
                -item.result.score,
                item.asset_order,
                item.result.chunk_id or "",
                item.result.result_id,
            )
        )
        counts: dict[str, int] = {}
        bounded: list[_Candidate] = []
        for candidate in candidates:
            count = counts.get(candidate.asset_order, 0)
            if count >= MAX_RESULTS_PER_ASSET:
                continue
            counts[candidate.asset_order] = count + 1
            bounded.append(candidate)
        page = tuple(
            candidate.hydrate()
            for candidate in bounded[query.offset : query.offset + query.limit]
        )
        return SearchResponse(query=query.query, total=len(bounded), limit=query.limit, offset=query.offset, results=page)

    def retrieve(
        self,
        query: str | SearchQuery,
        *,
        top_k: int = 20,
        asset_type: str = "all",
        source_format: str | None = None,
        quality_status: str | None = None,
        match: str = "all",
    ) -> list[SearchResult]:
        request = query if isinstance(query, SearchQuery) else SearchQuery(
            query=query,
            asset_type=asset_type,
            source_format=source_format,
            quality_status=quality_status,
            limit=top_k,
            match=match,
        )
        return list(self.search(request).results)

    def counts(self) -> dict[str, int]:
        registry = self._open()
        try:
            row = registry.connection.execute(
                "SELECT COUNT(*), COALESCE((SELECT COUNT(*) FROM text_chunks), 0) FROM catalog_assets"
            ).fetchone()
            return {"assets": int(row[0] or 0), "text_chunks": int(row[1] or 0)}
        finally:
            registry.close()


@dataclass(frozen=True)
class SearchBenchmark:
    assets: int
    text_chunks: int
    queries: int
    p50_query_ms: float
    p95_query_ms: float
    result_count: int
    index_build_ms: float
    wall_time_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "assets": self.assets,
            "text chunks": self.text_chunks,
            "queries": self.queries,
            "p50 query ms": self.p50_query_ms,
            "p95 query ms": self.p95_query_ms,
            "result count": self.result_count,
            "index/build ms": self.index_build_ms,
            "wall time ms": self.wall_time_ms,
        }


def run_search_benchmark(
    service: SearchService,
    *,
    queries: Sequence[str] = ("data", "pdf", "表", ""),
    source_format: str | None = None,
) -> SearchBenchmark:
    started = time.perf_counter_ns()
    durations: list[float] = []
    result_count = 0
    for value in queries:
        query_started = time.perf_counter_ns()
        result = service.search(SearchQuery(value, source_format=source_format, limit=100))
        durations.append((time.perf_counter_ns() - query_started) / 1_000_000)
        result_count += result.total
    ordered = sorted(durations)

    def percentile(percent: float) -> float:
        if not ordered:
            return 0.0
        position = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percent))))
        return ordered[position]

    counts = service.counts()
    return SearchBenchmark(
        assets=counts["assets"],
        text_chunks=counts["text_chunks"],
        queries=len(queries),
        p50_query_ms=percentile(0.50),
        p95_query_ms=percentile(0.95),
        result_count=result_count,
        index_build_ms=0.0,
        wall_time_ms=(time.perf_counter_ns() - started) / 1_000_000,
    )
