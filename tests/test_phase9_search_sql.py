"""Phase 9 lexical retrieval and safe SQL contract tests."""

from __future__ import annotations

from pathlib import Path
import socket
import time

import pytest
import duckdb

from dongjian.clean import process_source
from dongjian.registry import Registry
import dongjian.search as search_module
from dongjian.search import RetrievalReference, SearchQuery, SearchService
from dongjian.services.sql import (
    SqlServiceError,
    SqlTimeoutError,
    SqlQueryService,
    run_sql_benchmark,
    verify_sql_sandbox,
)


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _registry(tmp_path: Path) -> Path:
    return _workspace(tmp_path) / "state" / "registry.duckdb"


@pytest.fixture()
def prepared(tmp_path: Path) -> tuple[Path, Path, list[dict[str, object]], list[dict[str, object]]]:
    source = tmp_path / "retrieval corpus 中文 with spaces"
    source.mkdir()
    rows = ["id,name,grp,amount", "001,北京大学,A,10", "002,清华大学,A,20", "003,北京医院,B,30"]
    rows.extend(f"{index:03d},sample-{index},C,{index}" for index in range(4, 524))
    (source / "measurements.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (source / "labels.csv").write_text(
        "id,label\n001,alpha\n002,beta\n003,gamma\n", encoding="utf-8"
    )
    (source / "notes.txt").write_text(
        "这是北京大学的本地研究记录。\n第二段包含实验条件和样本说明。\n",
        encoding="utf-8",
    )
    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=_registry(tmp_path),
        workspace_root=_workspace(tmp_path),
    )
    assert summary.cleaning_failures == 0
    registry = Registry.open(_registry(tmp_path))
    try:
        tables = registry.list_catalog_assets(asset_type="table", limit=100)
        texts = registry.list_catalog_assets(asset_type="text", limit=100)
    finally:
        registry.close()
    assert len(tables) == 2
    assert texts
    return source, _registry(tmp_path), tables, texts


def test_search_empty_chinese_text_metadata_and_provenance(prepared) -> None:
    _source, registry_path, tables, texts = prepared
    service = SearchService(registry_path=registry_path)

    empty = service.search(SearchQuery("   ", limit=10))
    assert empty.total == 0
    assert empty.results == ()

    text_result = service.search(SearchQuery("北京大学", asset_type="text", limit=10))
    assert text_result.results
    result = text_result.results[0]
    assert result.chunk_id
    assert "北京大学" in result.snippet
    assert result.match_kind in {"text_substring", "text_phrase"}
    assert result.provenance["contentSha256"]
    assert result.provenance["fileId"]
    assert result.match_offsets
    assert result.match_offsets[0][0] < result.match_offsets[0][1]
    reference = RetrievalReference.from_result(result)
    assert reference.asset_id == result.asset_id
    assert reference.excerpt == result.snippet

    token_result = service.search(SearchQuery("北京 大学", asset_type="text", limit=10))
    assert token_result.results
    phrase_result = service.search(SearchQuery("北京大学", asset_type="text", match="phrase", limit=10))
    assert phrase_result.results

    column_result = service.search(SearchQuery("amount", asset_type="table", limit=10))
    assert column_result.results
    assert any("column" in item.match_kind for item in column_result.results)
    assert column_result.results[0].quality_status in {"ready", "needs_review", "unusable"}

    filtered = service.search(SearchQuery("大学", asset_type="text", source_format="txt", limit=10))
    assert filtered.results
    assert all(item.source_format == "txt" for item in filtered.results)


def test_search_is_deterministic_bounded_per_asset_and_live_after_catalog(prepared) -> None:
    source, registry_path, _tables, _texts = prepared
    service = SearchService(registry_path=registry_path)
    first = service.search(SearchQuery("sample", asset_type="table", limit=100))
    second = service.search(SearchQuery("sample", asset_type="table", limit=100))
    assert [item.result_id for item in first.results] == [item.result_id for item in second.results]
    counts: dict[str, int] = {}
    for item in first.results:
        counts[item.asset_id] = counts.get(item.asset_id, 0) + 1
    assert max(counts.values()) <= 3
    assert len(first.results) <= 3
    (source / "late-arriving.txt").write_text("late arriving catalog evidence", encoding="utf-8")
    process_source(
        source,
        workers=1,
        registry_path=registry_path,
        workspace_root=registry_path.parent.parent,
    )
    assert service.search(SearchQuery("late arriving", asset_type="text", limit=5)).results


def test_search_uses_bounded_batched_registry_queries(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    _source, registry_path, _tables, _texts = prepared
    service = SearchService(registry_path=registry_path)
    original_open = service._open
    query_count = 0

    class CountingConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def execute(self, *args, **kwargs):
            nonlocal query_count
            query_count += 1
            return self._connection.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def counted_open():
        registry = original_open()
        registry.connection = CountingConnection(registry.connection)
        return registry

    monkeypatch.setattr(service, "_open", counted_open)
    result = service.search(SearchQuery("sample", limit=30))
    assert result.results
    assert query_count <= 8


def test_search_hydrates_provenance_only_for_returned_page(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    _source, registry_path, _tables, _texts = prepared
    calls = 0
    original_provenance = search_module._provenance

    def counted_provenance(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_provenance(*args, **kwargs)

    monkeypatch.setattr(search_module, "_provenance", counted_provenance)
    result = SearchService(registry_path=registry_path).search(SearchQuery("sample", limit=5))
    assert result.results
    assert calls == len(result.results)
    assert calls <= 5


def test_sql_schema_select_aggregate_join_and_limit(prepared) -> None:
    _source, registry_path, tables, _texts = prepared
    service = SqlQueryService(registry_path=registry_path, workspace_root=registry_path.parent.parent)
    measurements = next(item for item in tables if str(item["source_file"]).endswith("measurements.csv"))
    labels = next(item for item in tables if str(item["source_file"]).endswith("labels.csv"))
    table_ids = [str(measurements["asset_id"]), str(labels["asset_id"])]

    schema = service.schema(table_ids)
    assert [item.alias for item in schema.relations] == ["t1", "t2"]
    assert schema.relations[0].columns
    assert schema.limits["maxResultRows"] == 500

    selected = service.execute([table_ids[0]], "SELECT id, amount FROM t1 ORDER BY id LIMIT 2")
    assert selected["columns"] == ["id", "amount"]
    assert selected["rowCount"] == 2
    assert selected["rows"][0]["id"] == "001"

    aggregate = service.execute(
        [table_ids[0]],
        "SELECT grp, SUM(amount) AS total FROM t1 GROUP BY grp ORDER BY grp",
    )
    assert aggregate["columns"] == ["grp", "total"]
    assert aggregate["rowCount"] == 3

    join = service.execute(
        table_ids,
        "SELECT t1.id, t1.name, t2.label FROM t1 JOIN t2 ON t1.id=t2.id ORDER BY t1.id",
    )
    assert join["rowCount"] == 3
    assert join["rows"][0]["label"] == "alpha"

    bounded = service.execute([table_ids[0]], "SELECT * FROM t1")
    assert bounded["rowCount"] == 500
    assert bounded["truncated"] is True


def test_sql_rejects_external_access_mutation_and_unselected_relations(prepared) -> None:
    _source, registry_path, tables, _texts = prepared
    service = SqlQueryService(registry_path=registry_path, workspace_root=registry_path.parent.parent)
    table_id = str(next(item for item in tables if str(item["source_file"]).endswith("measurements.csv"))["asset_id"])
    forbidden = [
        "SELECT * FROM read_csv_auto('C:\\some-file.csv')",
        "SELECT * FROM read_parquet('C:\\something.parquet')",
        "SELECT * FROM read_json('C:\\something.json')",
        "SELECT * FROM glob('C:\\*.csv')",
        "SELECT * FROM sqlite_scan('C:\\outside.db', 'items')",
        "SELECT * FROM postgres_scan('C:\\outside.db', 'items')",
        "SELECT http_get('https://127.0.0.1:18765/') FROM t1",
        "SELECT query('SELECT * FROM read_csv_auto(\'C:\\\\outside.csv\')') FROM t1",
        "SELECT * FROM custom_scan('C:\\\\outside.csv')",
        "ATTACH 'C:\\registry.duckdb' AS x",
        "EXPORT DATABASE 'C:\\export'",
        "IMPORT DATABASE 'C:\\import'",
        "INSTALL fts",
        "LOAD fts",
        "COPY (SELECT * FROM t1) TO 'C:\\out.csv'",
        "DELETE FROM t1",
        "CREATE TABLE x (id INTEGER)",
        "SELECT * FROM t1; SELECT * FROM t1",
    ]
    for sql in forbidden:
        with pytest.raises(SqlServiceError):
            service.execute([table_id], sql)
    with pytest.raises(SqlServiceError, match="not selected"):
        service.execute([table_id], "SELECT * FROM t2")
    with pytest.raises(SqlServiceError):
        service.execute([table_id], "SELECT * FROM information_schema.tables")


def test_sql_timeout_and_sandbox_contract(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    _source, registry_path, tables, _texts = prepared
    service = SqlQueryService(registry_path=registry_path, workspace_root=registry_path.parent.parent)
    table_id = str(next(item for item in tables if str(item["source_file"]).endswith("measurements.csv"))["asset_id"])
    import dongjian.services.sql as sql_module

    monkeypatch.setattr(sql_module, "SQL_TIMEOUT_SECONDS", 0.0001)
    with pytest.raises(SqlTimeoutError):
        service.execute([table_id], "SELECT * FROM t1")
    verify_sql_sandbox()


def test_sql_100k_row_resource_smoke(tmp_path: Path) -> None:
    source = tmp_path / "large sql corpus"
    source.mkdir()
    lines = ["id,grp,value"]
    lines.extend(f"{index},g{index % 10},{index * 2}" for index in range(100_000))
    (source / "large.csv").write_text("\n".join(lines), encoding="utf-8")
    workspace = tmp_path / "workspace"
    registry_path = workspace / "state" / "registry.duckdb"
    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
    )
    assert summary.cleaning_failures == 0
    registry = Registry.open(registry_path)
    try:
        table = registry.list_catalog_assets(asset_type="table", limit=10)[0]
    finally:
        registry.close()
    started = time.perf_counter()
    service = SqlQueryService(registry_path=registry_path, workspace_root=workspace)
    with pytest.raises(SqlServiceError) as raised:
        service.execute(
            [str(table["asset_id"])],
            "SELECT grp, COUNT(*) AS rows, SUM(value) AS total FROM t1 GROUP BY grp ORDER BY grp",
        )
    elapsed = time.perf_counter() - started
    assert raised.value.code == "input_rows_too_large"
    assert elapsed < 10.0


def test_duckdb_external_access_setting_blocks_real_file_functions(prepared) -> None:
    source, _registry_path, _tables, _texts = prepared
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET enable_external_access=false")
        csv_path = str((source / "measurements.csv").resolve()).replace("'", "''")
        for statement in (
            f"SELECT * FROM read_csv_auto('{csv_path}')",
            f"SELECT * FROM read_parquet('{csv_path}')",
            f"SELECT * FROM read_json('{csv_path}')",
        ):
            with pytest.raises(Exception) as raised:  # DuckDB driver exception types vary by function.
                connection.execute(statement)
            assert "external" in str(raised.value).casefold() or "disabled" in str(raised.value).casefold()
    finally:
        connection.close()


def test_search_and_sql_do_not_open_network(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    _source, registry_path, tables, _texts = prepared

    def blocked_connect(*_args, **_kwargs):
        raise AssertionError("Phase 9 local retrieval/query attempted network access")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    SearchService(registry_path=registry_path).search(SearchQuery("name", limit=5))
    table_id = str(next(item for item in tables if str(item["source_file"]).endswith("measurements.csv"))["asset_id"])
    SqlQueryService(registry_path=registry_path, workspace_root=registry_path.parent.parent).execute(
        [table_id], "SELECT * FROM t1 LIMIT 1"
    )


def test_sql_benchmark_is_bounded_and_read_only(prepared) -> None:
    _source, registry_path, _tables, _texts = prepared
    benchmark = run_sql_benchmark(
        SqlQueryService(registry_path=registry_path, workspace_root=registry_path.parent.parent)
    )
    assert benchmark.selected_assets == 1
    assert benchmark.rows_input >= benchmark.result_rows
    assert benchmark.result_rows <= 500
    assert benchmark.timeout_behavior.startswith("interruptible/")
