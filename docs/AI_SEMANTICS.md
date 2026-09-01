# AI semantic enrichment

## Role

The future semantic layer interprets already extracted `TableAsset` and
`TextAsset` records. It may propose human-facing names, categories, field
meanings, summaries, quality explanations, and difficult visual/OCR judgments.
It is optional and is never part of extraction correctness.

The expected initial target is an OpenAI-compatible service serving
`Qwen3.6-35B-A3B`. Neither the provider nor model is hard-coded as the only
choice.

## Configuration contract

The tracked `config/llm.example.json` is intentionally inactive:

```json
{
  "base_url": "",
  "api_key": "",
  "model": "Qwen3.6-35B-A3B",
  "timeout_seconds": 60
}
```

Real `config/llm.json` and `config/llm.*.json` files are ignored. The
standard-library `LLMConfig` loader validates an explicitly configured base
URL, API key, model, and positive timeout before use. It does not select a
default public URL, read a global OpenAI config, inspect `%USERPROFILE%`, or
fall back to another endpoint.

## Provider boundary

`src/chongzu/semantic.py` contains only:

- `LLMConfig`;
- `SemanticEnrichmentRequest`, which references an existing asset and a
  versioned prompt contract;
- `SemanticEnrichmentProvider`, a protocol returning `SemanticMetadata`.

There is no HTTP SDK/client and no API call in the Architecture Refactor. A
future adapter must remain replaceable and receive configuration explicitly.

## Semantic operations

Candidate tasks include:

- dataset/table/text display naming;
- research-domain category and keyword assignment;
- column/field descriptions and semantic types;
- multi-row table-header interpretation;
- synonym, unit, and related-table judgments;
- text and asset summaries;
- anomaly/quality explanations;
- optional review of difficult OCR or visual extraction evidence.

The output is a new `SemanticMetadata`, `QualityIssue`, or proposed cleaning
operation. The model cannot issue a direct write that replaces raw table cells,
normalized Parquet, TextAsset text, source files, or provenance.

## Deterministic versus semantic cleaning

Local deterministic code owns mechanical operations such as Unicode
normalization, trim, empty rows/columns, duplicate rows, null handling,
numeric/date inference, obvious encoding repair, and mechanical column-name
normalization.

The model owns only interpretations that need context: actual header meaning,
units, dataset identity/category, related tables, ambiguous anomalies, and
hard OCR/visual review. A semantic suggestion must be accepted/modified/ignored
through a review workflow before any later deterministic transformation is
published.

## Audit and failure behavior

Every semantic record stores:

- target asset ID/type;
- model and prompt version;
- generated time and confidence;
- structured semantic fields rather than an opaque replacement blob.

Future request/run logging must store outcome, timing, retry/error category,
and non-secret request-contract hashes. API keys and authorization headers must
never be logged. A timeout, invalid response, unavailable server, or refused
request leaves extraction assets valid and records an isolated semantic
failure. It never triggers a public-network fallback.

## Future search and embeddings

Structured questions will eventually select table candidates, ask Qwen for
restricted read-only SQL, validate the SQL, and query DuckDB/Parquet. Text
questions will select TextChunks by keyword and later optional vector retrieval
before synthesis.

The embedding service is unknown. `src/chongzu/search.py` reserves only an
`EmbeddingProvider` interface location. The project does not assume an
embedding endpoint, add embedding fields to core assets, install a local model,
or install a vector database in this phase.
