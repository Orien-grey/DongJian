"""Bounded, read-only SQL over explicitly selected normalized table assets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping

import duckdb
import polars as pl

from dongjian import paths
from dongjian.extract.artifacts import artifact_absolute
from dongjian.registry import Registry
from .table_trust import is_table_trusted_for_analysis


SQL_SANDBOX_VERSION = "duckdb-memory-external-access-disabled-v1"
SQL_MAX_SELECTED_ASSETS = 8
SQL_MAX_INPUT_ROWS = 50_000
SQL_MAX_RESULT_ROWS = 500
SQL_MAX_QUERY_CHARS = 32 * 1024
SQL_TIMEOUT_SECONDS = 10.0
SQL_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SQL_MAX_CELL_CHARS = 10_000

FORBIDDEN_SQL_WORDS = (
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "copy",
    "export",
    "import",
    "attach",
    "detach",
    "install",
    "load",
    "pragma",
    "set",
    "call",
    "vacuum",
    "replace",
    "merge",
    "execute",
    "prepare",
    "deallocate",
)
FORBIDDEN_SQL_FUNCTIONS = (
    "query",
    "query_table",
    "eval",
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "parquet_scan",
    "read_json",
    "read_json_auto",
    "read_text",
    "httpfs",
    "http_get",
    "glob",
    "csv_scan",
    "json_scan",
    "parquet_scan",
    "range",
    "generate_series",
    "duckdb_",
    "pragma_",
    "information_schema",
    "pg_catalog",
    "sqlite_",
    "sqlite",
    "postgres",
    "mysql",
    "http",
    "url",
    "read_",
    "arrow",
    "delta",
    "iceberg",
)


class SqlServiceError(ValueError):
    """Safe, user-facing query service error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SqlValidationError(SqlServiceError):
    pass


class SqlAssetError(SqlServiceError):
    pass


class SqlTimeoutError(TimeoutError):
    code = "query_timeout"
    message = "query exceeded the local execution time limit"

    def __init__(self) -> None:
        super().__init__(self.message)


class SqlExecutionError(SqlServiceError):
    pass


@dataclass(frozen=True)
class SqlRelation:
    alias: str
    asset_id: str
    display_name: str
    source_file: str
    source_format: str | None
    columns: tuple[Mapping[str, Any], ...]
    row_count: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "assetId": self.asset_id,
            "displayName": self.display_name,
            "sourceFile": self.source_file,
            "sourceFormat": self.source_format,
            "columns": [dict(column) for column in self.columns],
            "rowCount": self.row_count,
        }


@dataclass(frozen=True)
class SqlSchemaResponse:
    relations: tuple[SqlRelation, ...]
    limits: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relations": [relation.as_dict() for relation in self.relations],
            "limits": dict(self.limits),
            "sandbox": SQL_SANDBOX_VERSION,
        }


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > SQL_MAX_CELL_CHARS:
            return value[:SQL_MAX_CELL_CHARS] + "…"
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value[:SQL_MAX_CELL_CHARS].hex()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, depth=depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):  # noqa: PLR0124
        return None
    return str(value)[:SQL_MAX_CELL_CHARS]


def _quote_identifier(value: str) -> str:
    """Quote a catalog-derived DuckDB identifier."""

    return '"' + str(value).replace('"', '""') + '"'


def _duckdb_type(dtype: pl.DataType) -> str:
    """Map the bounded Polars schema without requiring PyArrow."""

    if dtype == pl.Boolean:
        return "BOOLEAN"
    if dtype in {pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64}:
        return "BIGINT"
    if dtype in {pl.Float32, pl.Float64}:
        return "DOUBLE"
    if dtype == pl.Date:
        return "DATE"
    if dtype == pl.Time:
        return "TIME"
    if isinstance(dtype, pl.Datetime):
        return "TIMESTAMP"
    if isinstance(dtype, pl.Decimal):
        return "DECIMAL(38, 18)"
    if dtype == pl.Binary:
        return "BLOB"
    return "VARCHAR"


def _sql_value(value: Any, dtype: pl.DataType) -> Any:
    """Convert nested values to text while preserving scalar values."""

    if value is None:
        return None
    if _duckdb_type(dtype) != "VARCHAR":
        return value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _load_polars_frame(connection: Any, alias: str, frame: pl.DataFrame) -> None:
    """Copy one selected frame into a private in-memory DuckDB relation.

    The normal DuckDB Polars registration path invokes ``DataFrame.to_arrow``;
    that would require PyArrow, which is intentionally not in the bundle.
    Parameterized batch insertion is a small dependency-free bridge and keeps
    user SQL isolated from every source path and from the registry connection.
    """

    if not frame.columns:
        raise SqlAssetError("empty_schema", "selected table has no columns")
    schema = frame.schema
    definitions = ", ".join(
        f"{_quote_identifier(column)} {_duckdb_type(dtype)}"
        for column, dtype in schema.items()
    )
    quoted_alias = _quote_identifier(alias)
    connection.execute(f"CREATE TEMP TABLE {quoted_alias} ({definitions})")
    placeholders = ", ".join("?" for _ in frame.columns)
    insert_sql = f"INSERT INTO {quoted_alias} VALUES ({placeholders})"
    batch: list[tuple[Any, ...]] = []
    dtypes = tuple(schema.values())
    for row in frame.iter_rows(named=False):
        batch.append(
            tuple(_sql_value(value, dtype) for value, dtype in zip(row, dtypes, strict=True))
        )
        if len(batch) >= 2000:
            connection.executemany(insert_sql, batch)
            batch.clear()
    if batch:
        connection.executemany(insert_sql, batch)


def _mask_sql(sql: str) -> str:
    """Blank strings/comments while preserving SQL keywords for validation."""

    chars = list(sql)
    output = list(sql)
    index = 0
    length = len(chars)
    while index < length:
        current = chars[index]
        if current == "-" and index + 1 < length and chars[index + 1] == "-":
            output[index] = output[index + 1] = " "
            index += 2
            while index < length and chars[index] != "\n":
                output[index] = " "
                index += 1
            continue
        if current == "/" and index + 1 < length and chars[index + 1] == "*":
            output[index] = output[index + 1] = " "
            index += 2
            while index + 1 < length and not (chars[index] == "*" and chars[index + 1] == "/"):
                output[index] = " "
                index += 1
            if index + 1 < length:
                output[index] = output[index + 1] = " "
                index += 2
            continue
        if current in {"'", '"', "`"}:
            quote = current
            output[index] = " "
            index += 1
            while index < length:
                if chars[index] == quote:
                    output[index] = " "
                    if index + 1 < length and chars[index + 1] == quote:
                        output[index + 1] = " "
                        index += 2
                        continue
                    index += 1
                    break
                output[index] = "\n" if chars[index] == "\n" else " "
                index += 1
            continue
        index += 1
    return "".join(output)


def _remove_terminal_semicolon(sql: str) -> str:
    value = sql.strip()
    masked = _mask_sql(value)
    semicolon_positions = [index for index, char in enumerate(masked) if char == ";"]
    if not semicolon_positions:
        return value
    if len(semicolon_positions) != 1 or not value.endswith(";"):
        raise SqlValidationError("multiple_statements", "only one SQL statement is allowed")
    return value[:-1].rstrip()


def validate_sql(sql: str, allowed_aliases: Iterable[str]) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlValidationError("empty_sql", "sql must be a non-empty read-only query")
    if len(sql) > SQL_MAX_QUERY_CHARS:
        raise SqlValidationError("sql_too_large", f"sql must be at most {SQL_MAX_QUERY_CHARS} characters")
    if "\x00" in sql:
        raise SqlValidationError("invalid_sql", "sql contains an invalid control character")
    value = _remove_terminal_semicolon(sql)
    masked = _mask_sql(value)
    first = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", masked)
    if first is None or first.group(1).casefold() not in {"select", "with"}:
        raise SqlValidationError("read_only_required", "only SELECT or WITH ... SELECT queries are allowed")
    for word in FORBIDDEN_SQL_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", masked, flags=re.IGNORECASE):
            raise SqlValidationError("forbidden_sql", f"SQL operation is not allowed: {word.upper()}")
    for function in FORBIDDEN_SQL_FUNCTIONS:
        if re.search(rf"\b{re.escape(function)}", masked, flags=re.IGNORECASE):
            raise SqlValidationError("external_access_denied", "external files, extensions, and generator functions are disabled")
    if re.search(r"\b(?:from|join)\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", masked, flags=re.IGNORECASE):
        raise SqlValidationError("external_access_denied", "table functions are not allowed in the SQL sandbox")
    aliases = {str(alias).casefold() for alias in allowed_aliases}
    cte_names = {
        match.group(1).casefold()
        for match in re.finditer(r"\b(?:with|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", masked, flags=re.IGNORECASE)
    }
    for match in re.finditer(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", masked, flags=re.IGNORECASE):
        relation = match.group(1).casefold()
        if relation not in aliases and relation not in cte_names:
            raise SqlValidationError("unknown_relation", f"relation is not selected: {match.group(1)}")
    return value


def verify_sql_sandbox() -> None:
    """Fail fast if this DuckDB build cannot disable external access."""

    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET enable_external_access=false")
        value = connection.execute("SELECT current_setting('enable_external_access')").fetchone()
        if not value or str(value[0]).casefold() not in {"false", "0"}:
            raise RuntimeError("DuckDB enable_external_access=false was not applied")
    finally:
        connection.close()


class SqlQueryService:
    """Resolve registry-selected Parquet assets, then query only memory data."""

    def __init__(
        self,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.registry_path = Path(registry_path or paths.REGISTRY_PATH).resolve()
        self.workspace_root = Path(workspace_root or paths.WORKSPACE_ROOT).resolve()

    def _selected_rows(self, asset_ids: object) -> list[dict[str, Any]]:
        if not isinstance(asset_ids, list) or not asset_ids:
            raise SqlValidationError("assets_required", "assetIds must contain one or more table asset IDs")
        if len(asset_ids) > SQL_MAX_SELECTED_ASSETS:
            raise SqlValidationError("too_many_assets", f"at most {SQL_MAX_SELECTED_ASSETS} table assets may be selected")
        normalized_ids: list[str] = []
        for value in asset_ids:
            if not isinstance(value, str) or not value.strip() or len(value) > 160:
                raise SqlValidationError("invalid_asset_id", "assetIds must contain bounded non-empty strings")
            asset_id = value.strip()
            if asset_id in normalized_ids:
                raise SqlValidationError("duplicate_asset_id", "assetIds must not contain duplicates")
            normalized_ids.append(asset_id)
        placeholders = ",".join("?" for _ in normalized_ids)
        registry = Registry.open_reader(self.registry_path)
        try:
            cursor = registry.connection.execute(
                f"""
                SELECT c.asset_id, c.effective_display_name, c.source_file, c.source_format,
                       c.rows, c.normalized_artifact_path, c.quality_status,
                       c.source_kind, c.extractor, t.columns_json
                FROM catalog_assets c
                JOIN table_assets t ON t.table_id=c.asset_id AND t.is_current=TRUE
                WHERE c.asset_type='table' AND c.asset_id IN ({placeholders})
                """,
                normalized_ids,
            )
            columns = [item[0] for item in cursor.description]
            found = {str(row[0]): dict(zip(columns, row)) for row in cursor.fetchall()}
        finally:
            registry.close()
        missing = [asset_id for asset_id in normalized_ids if asset_id not in found]
        if missing:
            raise SqlAssetError("table_asset_not_found", "one or more selected table assets were not found")
        selected = [found[asset_id] for asset_id in normalized_ids]
        if any(not is_table_trusted_for_analysis(row) for row in selected):
            raise SqlAssetError("table_asset_not_trusted", "selected table asset is not a confirmed structured table")
        return selected

    @staticmethod
    def _columns(row: Mapping[str, Any], path: Path) -> tuple[Mapping[str, Any], ...]:
        try:
            schema = pl.scan_parquet(path).collect_schema()
            return tuple({"name": str(name), "physicalType": str(dtype)} for name, dtype in schema.items())
        except Exception as exc:  # noqa: BLE001 - safe service boundary
            raise SqlAssetError("query_artifact_unavailable", "normalized table artifact could not be read") from exc

    def _relations(self, rows: list[dict[str, Any]]) -> tuple[SqlRelation, ...]:
        relations: list[SqlRelation] = []
        for index, row in enumerate(rows, start=1):
            value = row.get("normalized_artifact_path")
            if not value:
                raise SqlAssetError("query_artifact_unavailable", "selected table has no normalized artifact")
            try:
                path = artifact_absolute(str(value), self.workspace_root)
            except ValueError as exc:
                raise SqlAssetError("query_artifact_unavailable", "selected artifact is outside the workspace") from exc
            if not path.is_file():
                raise SqlAssetError("query_artifact_unavailable", "selected normalized table artifact is unavailable")
            relations.append(
                SqlRelation(
                    alias=f"t{index}",
                    asset_id=str(row["asset_id"]),
                    display_name=str(row.get("effective_display_name") or row["asset_id"]),
                    source_file=str(row.get("source_file") or ""),
                    source_format=str(row.get("source_format")) if row.get("source_format") else None,
                    columns=self._columns(row, path),
                    row_count=int(row["rows"]) if row.get("rows") is not None else None,
                )
            )
        return tuple(relations)

    def schema(self, asset_ids: object) -> SqlSchemaResponse:
        rows = self._selected_rows(asset_ids)
        return SqlSchemaResponse(
            relations=self._relations(rows),
            limits={
                "maxSelectedAssets": SQL_MAX_SELECTED_ASSETS,
                "maxInputRows": SQL_MAX_INPUT_ROWS,
                "maxResultRows": SQL_MAX_RESULT_ROWS,
                "maxSqlChars": SQL_MAX_QUERY_CHARS,
                "timeoutSeconds": SQL_TIMEOUT_SECONDS,
            },
        )

    @staticmethod
    def _load_frames(rows: list[dict[str, Any]], workspace_root: Path) -> list[tuple[str, pl.DataFrame]]:
        frames: list[tuple[str, pl.DataFrame]] = []
        for index, row in enumerate(rows, start=1):
            try:
                path = artifact_absolute(str(row["normalized_artifact_path"]), workspace_root)
                frame = pl.read_parquet(path)
            except Exception as exc:  # noqa: BLE001 - safe service boundary
                raise SqlAssetError("query_artifact_unavailable", "selected normalized table artifact could not be loaded") from exc
            frames.append((f"t{index}", frame))
        return frames

    @staticmethod
    def _run_query(frames: list[tuple[str, pl.DataFrame]], sql: str) -> tuple[list[str], list[dict[str, Any]], bool, float]:
        holder: dict[str, Any] = {}
        finished = threading.Event()

        def worker() -> None:
            connection: duckdb.DuckDBPyConnection | None = None
            try:
                connection = duckdb.connect(":memory:")
                holder["connection"] = connection
                connection.execute("SET enable_external_access=false")
                connection.execute("SET threads=1")
                connection.execute("SET memory_limit='512MB'")
                for alias, frame in frames:
                    # DuckDB's Python ``register`` path asks Polars for a
                    # PyArrow table in this runtime.  PyArrow is intentionally
                    # not a DongJian dependency.  The replacement scan uses
                    # Polars' Arrow C stream interface directly, then copies
                    # the selected frame into a temporary in-memory relation.
                    # ``alias`` is generated by this service, never supplied
                    # by the caller.
                    _load_polars_frame(connection, alias, frame)
                started = time.perf_counter_ns()
                cursor = connection.execute(
                    f"SELECT * FROM ({sql}) AS __dongjian_result LIMIT {SQL_MAX_RESULT_ROWS + 1}"
                )
                columns = [item[0] for item in cursor.description]
                raw_rows = cursor.fetchmany(SQL_MAX_RESULT_ROWS + 1)
                rows = [
                    {str(column): _json_value(value) for column, value in zip(columns, values, strict=True)}
                    for values in raw_rows[:SQL_MAX_RESULT_ROWS]
                ]
                holder["value"] = (columns, rows, len(raw_rows) > SQL_MAX_RESULT_ROWS, (time.perf_counter_ns() - started) / 1_000_000)
            except Exception as exc:  # noqa: BLE001 - map driver details below
                holder["error"] = exc
            finally:
                if connection is not None:
                    connection.close()
                finished.set()

        thread = threading.Thread(target=worker, name="dongjian-safe-sql", daemon=True)
        thread.start()
        if not finished.wait(SQL_TIMEOUT_SECONDS):
            connection = holder.get("connection")
            if connection is not None:
                try:
                    connection.interrupt()
                except Exception:  # pragma: no cover - defensive driver boundary
                    pass
            finished.wait(1.0)
            raise SqlTimeoutError()
        if "error" in holder:
            error = holder["error"]
            text = str(error).casefold()
            if "does not exist" in text or "not found" in text or "table with name" in text:
                raise SqlValidationError("unknown_relation", "query referenced a relation that is not selected") from error
            raise SqlExecutionError("query_failed", "the read-only query could not be executed") from error
        return holder["value"]

    @staticmethod
    def _bound_response(
        columns: list[str],
        rows: list[dict[str, Any]],
        truncated: bool,
        execution_ms: float,
        relations: tuple[SqlRelation, ...],
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "columns": columns,
            "rows": rows,
            "rowCount": len(rows),
            "truncated": bool(truncated),
            "executionMs": round(execution_ms, 3),
            "relations": [relation.as_dict() for relation in relations],
            "sandbox": SQL_SANDBOX_VERSION,
        }
        while len(json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")) > SQL_MAX_RESPONSE_BYTES and rows:
            rows.pop()
            response["truncated"] = True
            response["rowCount"] = len(rows)
        if len(json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")) > SQL_MAX_RESPONSE_BYTES:
            raise SqlExecutionError("query_response_too_large", "query response exceeds the local response limit")
        return response

    def execute(self, asset_ids: object, sql: str) -> dict[str, Any]:
        rows = self._selected_rows(asset_ids)
        relations = self._relations(rows)
        input_rows = sum(relation.row_count or 0 for relation in relations)
        if any(relation.row_count is None for relation in relations) or input_rows > SQL_MAX_INPUT_ROWS:
            raise SqlAssetError(
                "input_rows_too_large",
                f"selected tables exceed the SQL input limit of {SQL_MAX_INPUT_ROWS:,} rows; narrow the selection before querying",
            )
        safe_sql = validate_sql(sql, (relation.alias for relation in relations))
        frames = self._load_frames(rows, self.workspace_root)
        columns, result_rows, truncated, execution_ms = self._run_query(frames, safe_sql)
        return self._bound_response(columns, result_rows, truncated, execution_ms, relations)


@dataclass(frozen=True)
class SqlBenchmark:
    rows_input: int
    selected_assets: int
    execution_ms: float
    result_rows: int
    timeout_behavior: str
    wall_time_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows input": self.rows_input,
            "selected assets": self.selected_assets,
            "execution ms": round(self.execution_ms, 3),
            "result rows": self.result_rows,
            "timeout behavior": self.timeout_behavior,
            "wall time ms": round(self.wall_time_ms, 3),
        }


def run_sql_benchmark(service: SqlQueryService) -> SqlBenchmark:
    """Run one bounded local query without changing extraction/catalog state."""

    started = time.perf_counter_ns()
    registry = Registry.open_reader(service.registry_path)
    try:
        row = registry.connection.execute(
            "SELECT asset_id FROM catalog_assets WHERE asset_type='table' ORDER BY source_file, asset_id LIMIT 1"
        ).fetchone()
    finally:
        registry.close()
    if row is None:
        return SqlBenchmark(0, 0, 0.0, 0, f"interruptible/{SQL_TIMEOUT_SECONDS:.1f}s", (time.perf_counter_ns() - started) / 1_000_000)
    asset_ids = [str(row[0])]
    schema = service.schema(asset_ids)
    rows_input = int(schema.relations[0].row_count or 0) if schema.relations else 0
    result = service.execute(asset_ids, f"SELECT * FROM t1 LIMIT {SQL_MAX_RESULT_ROWS}")
    return SqlBenchmark(
        rows_input=rows_input,
        selected_assets=len(asset_ids),
        execution_ms=float(result.get("executionMs") or 0.0),
        result_rows=int(result.get("rowCount") or 0),
        timeout_behavior=f"interruptible/{SQL_TIMEOUT_SECONDS:.1f}s",
        wall_time_ms=(time.perf_counter_ns() - started) / 1_000_000,
    )
