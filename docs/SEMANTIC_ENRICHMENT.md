# Phase 7C semantic product closure

Phase 7A/7B/7C provide a strict, auditable semantic seam over already
published normalized `TableAsset` and `TextAsset` values. Phase 7B accepted
the configured OpenAI-compatible provider with two synthetic real requests;
Phase 7C exposes the same single-asset operation through the local UI.

## Boundaries

```text
TableAsset/TextAsset
        -> normalized artifact + profile + quality/provenance
        -> bounded SemanticRequest
        -> FakeSemanticProvider or explicitly authorized OpenAI-compatible provider
        -> strict local JSON validation
        -> semantic_metadata history/current + open review suggestions
        -> catalog_assets effective display name
```

The semantic layer never receives image/PDF binary, raw Parquet, raw text, or a
complete large table. It cannot write extraction/cleaning artifacts, rename
physical columns, alter units, merge/delete rows, resolve a quality issue, or
change an asset ID. `raw -> normalized -> semantic` is a permanent boundary.

## Configuration

The portable runtime uses the directly editable `config/llm.json` project
configuration. A blank `config/llm.example.json` is shipped as the template:

```json
{
  "base_url": "",
  "api_key": "",
  "model": "",
  "timeout_seconds": 120,
  "vision_enabled": false
}
```

The Settings page reads and writes this same file. The encrypted DPAPI store
and project-root `.env` remain backward-compatible fallback sources only when
the project file is absent:

```text
config/llm.json > legacy DPAPI store > legacy .env > offline
```

The legacy `.env` form is:

```text
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=2
```

The actual `.env` and `config/llm.json` are ignored by Git; releases contain a
blank `config/llm.json` template and no secret. Missing or blank project values
give `LLM_STATUS=NOT_CONFIGURED`, mean fully offline, and do not fail doctor.
`vision_enabled=false` permits text Semantic/Analysis only. `true` permits
Vision only after the user explicitly selects an AI Vision process.

```text
.\dongjian.cmd semantic status
```

`semantic status` reports configuration without making a network call. The
CLI remains offline by default; a real provider requires the explicit
`--allow-real-provider` flag. The local semantic API is also only executed by
an explicit user `POST` after UI confirmation. Without configuration it
returns HTTP 409 with `SEMANTIC_NOT_CONFIGURED` and makes no provider call.

The CLI fake route is:

```text
.\dongjian.cmd semantic enrich --provider fake --asset <asset-id>
```

## Input and prompt contract

Prompt versions are `table-semantic-v2` and `text-semantic-v2`. Each request
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
error handling. It has no hard-coded vendor or fallback endpoint. The local
API endpoint accepts only one existing table/text asset and never exposes
`force`; CLI/debug use remains the only forced regeneration path.

The UI shows `尚未配置 AI 模型` when the provider is absent. When configured,
Asset Detail → AI语义 offers `AI 整理`, first showing an explicit confirmation
that only a bounded summary/sample is sent. Success refreshes the current
asset detail; a matching result is shown as `已使用现有 AI 整理结果` without a
second provider call. `dongjian process SOURCE` and the local API process task
never invoke this layer. There is no bulk enrichment, vision upload,
embedding route, chat surface, or public fallback.

Phase 9's local Search and SQL workbench do not change this boundary. Lexical
retrieval consumes local Catalog and TextChunk data, while SQL is explicit
selected-table read-only execution. Neither path calls the semantic provider or
creates embeddings.

Phase 7B real provider acceptance is complete for the configured development
provider using synthetic assets only. Qwen acceptance remains **NOT RUN**.
Release bundles contain `.env.example` and a blank `config/llm.json`, and report
`LLM_STATUS=NOT_CONFIGURED`.
