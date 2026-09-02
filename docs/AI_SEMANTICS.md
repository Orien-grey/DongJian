# AI semantic enrichment

Phase 7A establishes the semantic layer without making it a dependency of
extraction, deterministic cleaning, profiling, Catalog, or relocation. It
consumes only a bounded summary of an already published normalized
`TableAsset`/`TextAsset`; it never receives the source file, PDF page image, raw
artifact, or complete large table.

The layer is provider/model neutral. DeepSeek may be used for a future
explicitly authorized development test, and the company deployment may use
`Qwen3.6-35B-A3B`, but neither name appears in business routing or contract
logic. ChongZu does not select a public endpoint, fallback server, vision
endpoint, or embedding endpoint.

## Phase 7A boundary

The current package is `src/chongzu/semantic/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `SemanticRequest`, `SemanticResponse`, validated metadata contracts and hashes. |
| `provider.py` | Provider-neutral protocol and safe error taxonomy. |
| `config.py` | Project-root `.env` parser and optional status. |
| `prompts.py` | Versioned prompt sections and output contracts. |
| `input_builder.py` | Normalized-artifact sampling, truncation, and audit metadata. |
| `validator.py` | Strict local JSON/type/range/column validation. |
| `fake_provider.py` | Deterministic no-network test provider. |
| `openai_compatible.py` | Minimal standard-library text chat adapter, disabled in Phase 7A. |
| `runner.py` | Cache identity, per-asset isolation, and Registry persistence. |

The flow is:

```text
normalized asset -> bounded SemanticRequest -> provider
       -> strict validator -> SemanticMetadata / open review suggestion
       -> semantic_runs + catalog_assets current projection
```

`raw`, `normalized`, and `semantic` remain separate. A semantic response can
only add metadata or an open suggestion. It cannot rename a physical column,
rewrite a Parquet/text artifact, change units, delete/merge rows, resolve a
quality issue, or change an asset ID.

## Configuration and status

The only future runtime configuration source is the project-root `.env`:

```text
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=2
```

`.env.example` contains no secret and `.env` is ignored by Git. The tracked
`config/llm.example.json` is a legacy documentation example only; it is not an
implicit runtime configuration source. With missing values:

```text
Provider: not configured
Model: not configured
LLM_STATUS = NOT_CONFIGURED
Network calls: disabled
```

Doctor reports `LLM: NOT CONFIGURED / OPTIONAL` through its historical
`LLM STATUS` label and remains successful. `semantic enrich` without
configuration prints `Semantic enrichment is not configured.` and performs no
request. In Phase 7A, even a filled `.env` cannot enable the real provider;
Phase 7B requires a new explicit user authorization.

## Input contract and exposure audit

`SemanticRequest` contains asset ID/type, model, prompt version, semantic config
version, normalized-artifact SHA-256, separate `instructions`, structured
`reference_data`, and `output_contract`. Its input hash covers that bounded
envelope and audit metadata. The reference section is explicitly
untrusted reference data so text such as “Ignore previous instructions” cannot
change the task.

Table input includes source filename, sheet/page, original and normalized
column names, row/column counts, physical profile, null/distinct/sample hints,
quality issues, extraction provenance, and deterministic representative rows.
The default table sample is at most 18 rows using head/middle/tail selection;
each cell is capped at 240 characters. Text input includes provenance, profile,
quality hints, selected chunks, and a bounded normalized excerpt capped at
12,000 characters. No image/PDF binary is sent and no RAG is implemented.

The request audit metadata records:

```json
{
  "source_chars": 12345,
  "sent_chars": 2345,
  "sampled_rows": 18,
  "input_truncated": true,
  "reference_bytes": 12000
}
```

Reference data is compacted to a hard 48 KiB bound and the full request envelope
to a 64 KiB bound. Truncation is deterministic and included in the input hash.
The runner stores counts and hashes, not the API key or complete prompt, in
`semantic_runs`.

## Prompt and JSON contracts

Prompt versions are `table-semantic-v1` and `text-semantic-v1`. The sections
`instructions`, `reference_data`, and `output_contract` are kept distinct.
Changing a prompt version changes semantic identity and reruns only semantic
enrichment.

Table output must be strict JSON with:

```json
{
  "display_name": "...",
  "category": "...",
  "description": "...",
  "keywords": ["..."],
  "summary": "...",
  "semantic_fields": [
    {
      "source_column": "existing normalized name",
      "semantic_name": "...",
      "description": "...",
      "semantic_type": "...",
      "unit": null,
      "aliases": [],
      "confidence": 0.0
    }
  ],
  "confidence": 0.0
}
```

Text uses the common fields without `semantic_fields`. Both may include the
known optional `quality_suggestions` list. Unknown fields, including
`corrected_rows`, are rejected. The validator checks required fields, JSON
size, types, finite confidence in `[0,1]`, list contents, and table
`source_column` membership in the actual normalized columns. Duplicate mappings
are valid but produce a warning. Validation failure writes no
`SemanticMetadata`.

Quality suggestions are persisted as `QualityIssue` rows with
`detected_by=semantic`, `semantic_run_id`, and status `open`. Existing issue
statuses are never changed and a suggestion is not an accepted human decision.

## Provider behavior

`FakeSemanticProvider` is deterministic and has no socket/HTTP path. It is
available only after an explicit `--provider fake` CLI switch or from tests.
The OpenAI-compatible adapter uses Python standard library `urllib`, explicit
`.env` values, `Authorization`, JSON, timeout, response-size limits, HTTP/error
mapping, and at most `LLM_MAX_RETRIES` retries for bounded transient failures.
It has no vendor branches and no fallback. The Phase 7A runner rejects real
provider execution before constructing a request, so tests and current CLI
smoke runs cannot contact DeepSeek, Qwen, OpenAI, localhost, or any other
endpoint.

## Identity, history, and failure isolation

Semantic reuse identity includes asset ID/type, normalized artifact identity,
model, prompt version, semantic config version, provider, and input hash. The
input hash covers the bounded prompt envelope and audit metadata. Same identity is
`REUSE`; model or prompt changes create a new semantic run without extraction
or cleaning. `semantic_runs` preserves successful, failed, and interrupted
history. `semantic_metadata.current` marks the current successful result; old
model/prompt results remain available.

Provider, input-builder, and validation failures are isolated to one asset.
They record a sanitized error/status in `semantic_runs` and leave Catalog,
raw artifacts, normalized artifacts, source SHA-256, and existing quality issue
statuses intact.

## Commands and scope

```text
.\chongzu.cmd semantic status
.\chongzu.cmd semantic enrich --provider fake --asset <asset-id>
.\chongzu.cmd semantic enrich --provider fake --type table --limit 10
```

`process SOURCE` does not invoke semantic enrichment. No vision, embedding,
vector database, frontend, GMFT, Docling, new OCR, or real model acceptance is
part of Phase 7A. Phase 7B is **BLOCKED BY USER CONFIGURATION / NOT RUN**.
