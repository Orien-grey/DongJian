# Phase 7A semantic enrichment infrastructure

Phase 7A provides a strict, auditable semantic seam without making a real
model call. It consumes bounded summaries of already published normalized
`TableAsset` and `TextAsset` values.

## Boundaries

```text
TableAsset/TextAsset
        -> normalized artifact + profile + quality/provenance
        -> bounded SemanticRequest
        -> FakeSemanticProvider (Phase 7A only)
        -> strict local JSON validation
        -> semantic_metadata history/current + open review suggestions
        -> catalog_assets effective display name
```

The semantic layer never receives image/PDF binary, raw Parquet, raw text, or a
complete large table. It cannot write extraction/cleaning artifacts, rename
physical columns, alter units, merge/delete rows, resolve a quality issue, or
change an asset ID. `raw -> normalized -> semantic` is a permanent boundary.

## Configuration

The only runtime configuration source is a project-root `.env` copied or
created by the operator from `.env.example`:

```text
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=2
```

The actual `.env` is ignored by Git and no real `.env` is shipped. The tracked
`config/llm.example.json` is legacy documentation only and is not implicitly
loaded. Missing base URL, key, or model gives `LLM_STATUS=NOT_CONFIGURED`; this
is optional and does not fail doctor.

```text
.\chongzu.cmd semantic status
```

In this round it reports provider/model not configured and network calls
disabled. A real configuration still does not enable calls in Phase 7A.
`semantic enrich` without configuration returns the safe message
`Semantic enrichment is not configured.`. The explicit test route is:

```text
.\chongzu.cmd semantic enrich --provider fake --asset <asset-id>
```

## Input and prompt contract

Prompt versions are `table-semantic-v1` and `text-semantic-v1`. Each request
keeps `instructions`, untrusted `reference_data`, and `output_contract` as
separate sections. Asset text is reference data: commands embedded in a file
must not change the semantic task.

Table reference data contains source filename/sheet/page, original and
normalized columns, physical profile, dimensions, quality/provenance hints, and
at most 18 deterministic head/middle/tail sample rows. Each cell is capped at
240 characters. Text reference data contains provenance/profile/chunk hints and
at most 12,000 characters of normalized excerpt. Reference data is capped at
48 KiB and the complete envelope at 64 KiB. The request stores
`source_chars`, `sent_chars`, `sampled_rows`, `input_truncated`, and
`reference_bytes`; the input hash covers the bounded payload.

The table JSON contract requires `display_name`, `category`, `description`,
`keywords`, `summary`, `semantic_fields`, and `confidence`. Each semantic field
must refer to an existing normalized `source_column` and includes semantic
name/description/type, nullable unit, aliases, and confidence. Text uses the
common fields without `semantic_fields`. A known optional
`quality_suggestions` array creates open review records only.

The validator rejects malformed JSON, missing/unknown fields, wrong types,
confidence outside `[0,1]`, oversized JSON, hallucinated columns, and data
mutation fields such as `corrected_rows`. Duplicate mappings remain visible as
warnings. A failed validation writes no semantic metadata.

## Cache and history

The semantic identity includes asset ID/type, normalized artifact SHA-256,
model, prompt version, semantic config version, provider, and the bounded input
hash. Same identity is reused. Changing model or prompt creates a new semantic
run; extraction and cleaning caches are untouched. Registry schema v5 stores `semantic_runs` for
successful, failed, and interrupted attempts. `semantic_metadata.current`
selects the current successful result while old results remain available.

`catalog_assets` exposes `semantic_display_name`, `effective_display_name`,
`category`, `semantic_status`, `semantic_model`, `semantic_confidence`, and
`semantic_run_id`. Without a result, effective name is the deterministic
filename/sheet/page fallback. Semantic quality suggestions are `open` and
`detected_by=semantic`; no model can mark an existing issue resolved.

## Provider policy

`FakeSemanticProvider` is deterministic and has no network path. The
OpenAI-compatible text-only adapter uses standard-library `urllib` and has
bounded timeout/retry, response-size, JSON, HTTP, timeout, and connection
error handling. It has no hard-coded vendor or fallback endpoint. The Phase 7A
runner rejects real-provider execution before any HTTP call, even if `.env` is
filled. Vision, file upload, embedding, vector retrieval, GMFT, Docling, new
OCR, and frontend work are outside this phase.

Phase 7B is **BLOCKED BY USER CONFIGURATION / NOT RUN**. It requires the user
to fill `.env` and explicitly authorize a controlled test before any real
provider request is permitted.
