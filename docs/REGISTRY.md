# Phase 2 registry

The Phase 2 source of truth is the project-local DuckDB database at
`workspace/state/registry.duckdb`. A scan writes only this database and a
JSONL run log below `workspace/logs/`; the source directory is opened
read-only.

## Schema version

`registry_meta` stores the key `chongzu_file_registry` and schema version `1`.
The pipeline version is recorded in every `scan_runs` row. A future schema
change must add an explicit migration rather than silently changing a table.

## Tables

| Table | Purpose |
| --- | --- |
| `scan_runs` | One run record: run ID, normalized source root, start/end/status, counts, byte total, stage timings, pipeline/schema version, and log path. |
| `files` | A path observation. The primary `file_id` is deterministic for `(source_root, relative_path)` and is not a content ID. It stores filename, extension hint, stat metadata, latest type/detection evidence, routing class, presence, errors, and run links. |
| `contents` | One row per stable SHA-256. This is the content identity and records size plus first/last run references. |
| `file_attempts` | The per-run observation, including before/after metadata, whether a hash was reused, detector output, status, errors, and fingerprint/detection timings. |
| `run_errors` | Discovery and run-level errors that do not belong to a successfully observed file. |
| `registry_meta` | Schema-name/version metadata. |

`scan_runs.status = 'complete'` means the coordinator finished the run. It may
still have a non-zero `failed_count`; the per-file `file_attempts.status` and
error columns isolate those failures without aborting the batch. A run-level
`failed` status is reserved for an inability to write or finalize the registry.
If a process stops after per-file writes, the next run marks its still-open
same-source run `interrupted` and retries/reconciles from the durable path rows.

Path identity and content identity are intentionally separate. Two paths may
have different `file_id` values and the same `contents.sha256`; those are exact
duplicate paths. Nothing is deleted or coalesced because of a duplicate.

## Incremental rule

The first scan hashes every discovered regular file that can be opened, with a
streaming SHA-256 reader. On a
later scan, a present path whose relative path, byte size, and `mtime_ns` all
match the previous observation is a **fast candidate** and its previously
stored digest may be reused. `--rehash` disables that optimization. A new path,
or any size/mtime change, is hashed again. A file that disappears is retained
in `files` and marked `current_presence_state = 'missing'`.

The metadata fast path is only a performance optimization. It never replaces
SHA-256 as the authoritative content identity. A stat before and after hashing
is compared; a change or disappearance yields `changed_during_scan` and that
digest is not accepted as stable. If a previous stable digest exists, the path
row retains that last-known digest while the failed `file_attempts` row records
the unstable attempt; no new content row is created.

## Lightweight detection

The detector combines the observed extension, bounded header reads, and (for
ZIP signatures) the central directory only. It recognizes PDF, JPEG, PNG, ZIP,
OOXML XLSX/DOCX/PPTX containers, OLE compound storage, basic HTML/XML/text,
CSS, and `Zone.Identifier` metadata. A `.下载` name is therefore still
recognized when its bytes identify a PDF or OOXML package. Corrupt ZIPs are
recorded as isolated detector errors; they do not abort a run. Uncertain binary
files remain `unknown` and are not ignored.

This is deliberately not authoritative MIME detection. A later Phase 5
project-local Tika service will be an explicit detector/parser fallback for
legacy Office and ambiguous or failed inputs. Its result will be recorded as a
new detection method; it will not change the path/content identity model.

## Read-only guarantee

Discovery does not follow directory symlinks or Windows reparse-point
junctions. Hashing and detection only open source files for reads. No rename,
move, copy, delete, timestamp update, or sidecar write is performed below the
source root. Registry, logs, and all mutable state stay under `workspace/`.
