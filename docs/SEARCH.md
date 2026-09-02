# Local Search and Text Retrieval

Phase 9 provides an offline lexical retrieval layer. It is intentionally
transparent and does not call an LLM, use embeddings, load a DuckDB extension,
or scan every cell of a large Parquet table.

## Contract

`SearchQuery` accepts a query, `all|table|text` type, optional source format
and quality filters, `all|phrase` matching, and bounded pagination.
`SearchResult` contains:

```text
result_id, asset_id, asset_type, chunk_id, display_name,
source_file, source_format, page_number, sheet_name,
match_kind, snippet, match_offsets, score, quality_status, provenance
```

`score` is a deterministic local ordinal used only for ordering. It is not a
semantic relevance probability. `RetrievalReference` is the future evidence
contract and retains the same asset/chunk/source/provenance identity.

## Backend and matching

The implementation is `SearchService` in `src/chongzu/search.py`. The current
backend is `duckdb-live-catalog`, version `lexical-live-v1`. It reads current
`catalog_assets`, `text_chunks`, table column metadata, bounded profile samples,
and current semantic metadata when available. It does not create a duplicate
full-text table, so a newly processed asset is searchable immediately.

Queries are Unicode NFC normalized and trimmed. Latin matching is
case-insensitive. A no-space Chinese query is a continuous substring; input
with whitespace requires all supplied tokens to occur. `match=phrase` requires
the full query substring. Snippets are plain text, capped at 500 characters,
and return offsets for frontend highlighting. The backend never constructs
HTML.

Metadata ranking weights are centralized in the source. Effective/fallback
names rank above source fields and columns; TextChunk phrase/token hits use
separate fixed weights. Quality affects ordering with a small penalty, while
`needs_review` results remain searchable. Unusable results remain searchable but
receive a larger deterministic penalty; the quality filter can be used to focus
on them explicitly. Results are stably ordered by score, asset ID, chunk ID,
and result ID, with at most three results per asset.

## Performance hardening

The original workspace profile (3,798 assets and 3,704 TextChunks) showed the
main cost in semantic-metadata hydration: a 3,798-parameter `IN` predicate took
about 5.4 seconds even though the current semantic table was empty. A single
batch read of current semantic rows replaced that predicate. Candidates now
carry only the bounded fields needed for matching; provenance is hydrated after
per-asset capping, sorting, and pagination. The request uses a bounded set of
batched Registry queries and does not perform per-result lookups.

After the change, the production workspace service profile was about 409--474ms
per query and the corresponding API path about 438--466ms across the sampled
queries. These are observations for this corpus, not a future-scale KPI.

## API and CLI

```text
GET /api/v1/search?q=北京大学&type=all&format=&quality=&match=all&limit=30&offset=0
.\chongzu.cmd search "北京大学" --type text --limit 20
.\chongzu.cmd benchmark search
```

The API caps query length at 512 characters, limit at 100, and offset at
10,000,000. An empty query returns no results and does not execute a full
catalog scan. Search errors use the standard API error object with a request
ID.

## Future backends

`TextRetriever` and `EmbeddingProvider` are provider-neutral interfaces only.
No vector database, local embedding model, `/embeddings` endpoint, query
rewriter, reranker, Chat, or RAG prompt is implemented in Phase 9. Any future
backend must preserve the SearchResult and RetrievalReference provenance
contracts and must not change asset IDs or normalized artifacts.
