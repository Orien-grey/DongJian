# Safe Local SQL Query

Phase 9's SQL workbench is a power-user read path over selected normalized
TableAssets. It is not a general DuckDB console and it never invokes a model
to generate SQL.

## Request boundary

The caller first supplies one to eight stable TableAsset IDs. The service
resolves those IDs through the Registry, verifies current normalized artifacts
under `workspace/`, and assigns aliases in request order:

```text
t1 = selected asset 1
t2 = selected asset 2
...
```

`POST /api/v1/query/schema` returns this mapping, bounded column schemas, and
row counts. It never returns a filesystem path. The frontend displays the
mapping before a query is run.

## Sandbox architecture

The Registry connection is used only to resolve the allowlist and is closed
before user SQL executes. Polars reads the selected normalized Parquet files.
Because PyArrow is intentionally not installed, the service copies scalar
values through parameterized batches into temporary relations on a new
DuckDB `:memory:` connection. No selected path is included in user SQL.

The query connection sets:

```sql
SET enable_external_access=false;
SET threads=1;
SET memory_limit='512MB';
```

The setting is checked by `verify_sql_sandbox()` and real file-function tests.
The service never runs `INSTALL`, `LOAD`, or any other extension provisioning.

## Accepted and rejected SQL

One statement beginning with `SELECT` or `WITH` is accepted. The service
rejects multiple statements, DDL, DML, `COPY`, `EXPORT`, `IMPORT`, `ATTACH`,
`DETACH`, `INSTALL`, `LOAD`, `PRAGMA`, `SET`, `CALL`, `VACUUM`, generator
functions, external file functions, system/catalog relations, and relations
that were not selected. Comments and quoted strings are masked while checking
the statement so a forbidden word cannot be hidden in a comment or literal.

The SQL limit is 32 KiB, selected assets are capped at 8, selected input is
capped at 50,000 total rows, results at 500 rows, JSON response size at 2 MiB,
and execution at 10 seconds with an interruptible worker. Input above the row
guard is rejected before loading; it is never silently truncated. A bounded
outer limit detects result truncation. Cell output is bounded as well. A timeout
or execution failure returns a stable API error and does not modify the
Registry, source files, or Parquet artifacts.

The adversarial suite verifies rejection of `read_csv_auto`, `read_parquet`,
`read_json`, `ATTACH`, `INSTALL`, `LOAD`, `COPY`, DDL, DML, multi-statement
input, unselected relations, and system catalog access. Direct DuckDB tests
also verify that `enable_external_access=false` blocks real file functions.

## API and CLI surface

```text
POST /api/v1/query/schema
POST /api/v1/query/sql
```

The SQL response contains columns, bounded rows, rowCount, truncation,
execution time, relation mappings, and the sandbox version. Phase 9 exposes
the workbench in the local UI under 数据查询. It provides a plain textarea and
does not expose SQL generation, filesystem browsing, arbitrary file reads, or
the Registry schema.
