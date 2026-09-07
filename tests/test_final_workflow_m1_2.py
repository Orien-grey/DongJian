"""Focused contracts for the Final Workflow M1.2 streaming closure."""

from __future__ import annotations

import inspect
from pathlib import Path
from threading import Event
import time

import pytest

import dongjian.scan as scan_module
from dongjian.cancellation import CancellationRequested
from dongjian.clean import process_source
from dongjian.clean.runner import MAX_READY_PROCESS_QUEUE
from dongjian.discovery import discover_iter
from dongjian.registry import Registry
from dongjian.services.catalog import CatalogService
from dongjian.services.process import ProcessTask, ProcessTaskManager


def _fixture(tmp_path: Path, count: int = 8) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    for index in range(count):
        (source / f"note-{index:04d}.txt").write_text(
            f"streaming research note {index} with enough local content for a visible text asset.\n",
            encoding="utf-8",
        )
    return source, workspace, workspace / "state" / "registry.duckdb"


def test_discover_iter_is_lazy_for_large_flat_directory(tmp_path: Path) -> None:
    source, _, _ = _fixture(tmp_path, 1_000)
    _, iterator, issues = discover_iter(source)
    assert inspect.isgenerator(iterator)
    first = next(iterator)
    assert first.relative_path
    # The issue list is shared with the generator and discovery has not been
    # required to construct a source-sized candidate collection.
    assert isinstance(issues, list)


def test_first_observation_batch_is_registered_before_scan_returns(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 40)
    callback_seen = False
    returned = False

    def callback(_outcome, summary) -> None:
        nonlocal callback_seen
        first_callback = not callback_seen
        callback_seen = True
        assert not returned
        assert summary.scan_complete_ms is None
        if first_callback:
            reader = Registry.open_reader(registry_path)
            try:
                assert reader.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0
            finally:
                reader.close()

    summary = scan_module.scan_source(
        source,
        workers=2,
        registry_path=registry_path,
        registry_batch_size=16,
        outcome_callback=callback,
    )
    returned = True
    assert callback_seen
    assert summary.registry_batch_size == 16
    assert summary.registry_batch_count >= 3
    assert summary.time_to_first_discovered_ms is not None
    assert summary.time_to_first_registered_ms is not None
    assert summary.scan_complete_ms is not None


def test_first_ready_local_precedes_scan_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 20)
    original = scan_module._process_one
    events: list[tuple[str, dict[str, object]]] = []
    catalog = CatalogService(registry_path=registry_path, workspace_root=workspace)

    def slowed(item, existing, force_rehash):
        time.sleep(0.05)
        return original(item, existing, force_rehash)

    monkeypatch.setattr(scan_module, "_process_one", slowed)

    def progress(stage: str, _value: float, **metadata: object) -> None:
        events.append((stage, metadata))
        if stage == "local_ready" and len([item for item in events if item[0] == "local_ready"]) == 1:
            assert metadata.get("scan_complete") is False
            assert metadata.get("total") is None
            assert catalog.list_files(limit=5)["pagination"]["total"] >= 1

    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
        progress_callback=progress,
        scan_batch_size=16,
    )
    first_ready = next(metadata for stage, metadata in events if stage == "local_ready")
    completed = next(metadata for stage, metadata in reversed(events) if stage == "completed")
    assert first_ready["scan_complete"] is False
    assert first_ready["total"] is None
    assert completed["scan_complete"] is True
    assert completed["total"] == 20
    assert summary.files_discovered == 20


def test_stream_progress_reports_discovered_registered_ready_and_unknown_total(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 5)
    events: list[tuple[str, dict[str, object]]] = []

    def progress(stage: str, _value: float, **metadata: object) -> None:
        events.append((stage, metadata))

    process_source(
        source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
        progress_callback=progress,
    )
    first = events[0][1]
    final = next(metadata for stage, metadata in reversed(events) if stage == "completed")
    assert int(first["discovered_count"]) >= 1
    assert any(stage == "registering" and int(metadata["registered_count"]) >= 1 for stage, metadata in events)
    assert any(stage == "local_ready" and int(metadata["ready_local_count"]) >= 1 for stage, metadata in events)
    assert final["scan_complete"] is True
    assert final["total"] == 5


def test_bad_hash_does_not_stop_later_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 3)
    original = scan_module.hash_file
    failed: list[str] = []
    ready: list[str] = []

    def broken(path: Path):
        if path.name == "note-0001.txt":
            raise OSError("synthetic hash failure")
        return original(path)

    monkeypatch.setattr(scan_module, "hash_file", broken)

    def progress(stage: str, _value: float, **metadata: object) -> None:
        if stage == "failed":
            failed.append(str(metadata.get("current_file_id") or ""))
        if stage == "local_ready":
            ready.append(str(metadata.get("current_file_id") or ""))

    summary = process_source(
        source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
        progress_callback=progress,
    )
    assert summary.files_discovered == 3
    assert failed
    assert len(ready) == 2


def test_registry_batch_falls_back_to_per_item_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 4)
    original = Registry.record_file_outcomes
    called = False

    def fail_once(registry, run_id, outcomes):
        nonlocal called
        if not called:
            called = True
            raise RuntimeError("synthetic batch failure")
        return original(registry, run_id, outcomes)

    monkeypatch.setattr(Registry, "record_file_outcomes", fail_once)
    summary = scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=16)
    assert called
    assert summary.registry_fallback_count == 4
    assert summary.registry_file_mutation_count == 4


def test_registry_batch_literal_escaping_preserves_file_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "researcher's-note.txt"
    path.write_text("quoted registry observation\n", encoding="utf-8")
    registry_path = tmp_path / "workspace" / "state" / "registry.duckdb"

    summary = scan_module.scan_source(source, workers=1, registry_path=registry_path)
    assert summary.discovered_count == 1
    reader = Registry.open_reader(registry_path)
    try:
        row = reader.connection.execute(
            "SELECT filename, relative_path FROM files WHERE source_root=?",
            [str(source.resolve()).casefold()],
        ).fetchone()
    finally:
        reader.close()
    assert row == (path.name, path.name)


def test_cancel_stops_discovery_but_completed_file_remains_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 20)
    original = scan_module._process_one
    cancel_event = Event()
    ready: list[str] = []

    def slowed(item, existing, force_rehash):
        time.sleep(0.03)
        return original(item, existing, force_rehash)

    monkeypatch.setattr(scan_module, "_process_one", slowed)

    def progress(stage: str, _value: float, **metadata: object) -> None:
        if stage == "local_ready":
            ready.append(str(metadata.get("current_file_id") or ""))
            cancel_event.set()

    with pytest.raises(CancellationRequested):
        process_source(
            source,
            workers=1,
            force=True,
            registry_path=registry_path,
            workspace_root=workspace,
            progress_callback=progress,
            cancel_event=cancel_event,
        )
    assert ready
    reader = Registry.open_reader(registry_path)
    try:
        assert reader.connection.execute(
            "SELECT COUNT(*) FROM files WHERE source_root=? AND current_presence_state='present'",
            [str(source.resolve()).casefold()],
        ).fetchone()[0] >= 1
        assert reader.connection.execute(
            "SELECT status FROM scan_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0] != "running"
    finally:
        reader.close()


def test_restart_scan_reuses_unchanged_hashes_without_duplicate_attempts(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 6)
    first = scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=32)
    second = scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=32)
    assert first.discovered_count == second.discovered_count == 6
    assert second.reused_hash_count == 6
    reader = Registry.open_reader(registry_path)
    try:
        assert reader.connection.execute("SELECT COUNT(*) FROM file_attempts").fetchone()[0] == 12
    finally:
        reader.close()


def test_registry_batch_sizes_are_explicitly_selectable(tmp_path: Path) -> None:
    source, _, registry_path = _fixture(tmp_path, 3)
    for batch_size in (16, 32, 64):
        summary = scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=batch_size, rehash=True)
        assert summary.registry_batch_size == batch_size


def test_stream_summary_records_bounded_queue_sizes(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 4)
    summary = process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    assert summary.scan_metrics["candidate_queue_max"] == 2
    assert summary.scan_metrics["pending_futures_max"] == 2
    assert summary.scan_metrics["ready_queue_max"] == min(MAX_READY_PROCESS_QUEUE, 2)


def test_process_task_exposes_streaming_progress_fields(tmp_path: Path) -> None:
    manager = ProcessTaskManager()
    try:
        task = ProcessTask(task_id="task_stream", source=str(tmp_path))
        manager._set_stage(
            task,
            "discovering",
            0.01,
            discovered_count=327,
            registered_count=64,
            ready_local_count=12,
            failed_count=1,
            skipped_count=0,
            total=None,
            scan_complete=False,
        )
        before = task.public_dict()
        assert before["total"] is None
        assert before["scanComplete"] is False
        assert before["discoveredCount"] == 327
        manager._set_stage(task, "scanning", 0.2, total=1126, scan_complete=True)
        after = task.public_dict()
        assert after["total"] == 1126
        assert after["scanComplete"] is True
    finally:
        manager.shutdown(timeout=1.0)


def test_scan_metrics_include_batch_and_first_file_boundaries(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 2)
    summary = process_source(source, workers=1, force=True, registry_path=registry_path, workspace_root=workspace)
    metrics = summary.scan_metrics
    assert metrics["registry_batch_count"] >= 1
    assert metrics["registry_file_mutation_count"] == 2
    assert metrics["time_to_first_discovered_ms"] is not None
    assert metrics["time_to_first_registered_ms"] is not None
    assert metrics["scan_complete_ms"] is not None


def test_catalog_reader_can_read_after_each_registered_batch(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 34)
    reads: list[int] = []
    catalog = CatalogService(registry_path=registry_path, workspace_root=workspace)

    def callback(_outcome, summary) -> None:
        if len(reads) < 2:
            reads.append(int(catalog.list_files(limit=100)["pagination"]["total"]))
            assert summary.scan_complete_ms is None

    scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=16, outcome_callback=callback)
    assert reads
    assert reads[0] >= 1


def test_first_registered_count_is_bounded_by_observation_batches(tmp_path: Path) -> None:
    source, _, registry_path = _fixture(tmp_path, 65)
    counts: list[int] = []

    def callback(_outcome, summary) -> None:
        counts.append(summary.registry_batch_count)

    scan_module.scan_source(source, workers=1, registry_path=registry_path, registry_batch_size=16, outcome_callback=callback)
    assert counts
    assert max(counts) == 5


def test_large_discovery_first_item_does_not_require_scan_completion(tmp_path: Path) -> None:
    source, _, _ = _fixture(tmp_path, 1_000)
    _, iterator, _ = discover_iter(source)
    started = time.perf_counter()
    first = next(iterator)
    assert first.filename
    assert time.perf_counter() - started < 1.0


def test_first_ready_1000_files_precedes_scan_completion(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 1_000)
    cancel_event = Event()
    first_ready: dict[str, object] = {}

    def progress(stage: str, _value: float, **metadata: object) -> None:
        if stage == "local_ready" and not first_ready:
            first_ready.update(metadata)
            cancel_event.set()

    with pytest.raises(CancellationRequested):
        process_source(
            source,
            workers=1,
            force=True,
            registry_path=registry_path,
            workspace_root=workspace,
            progress_callback=progress,
            cancel_event=cancel_event,
        )
    assert first_ready
    assert first_ready["scan_complete"] is False
    assert first_ready["total"] is None
    assert int(first_ready["discovered_count"]) < 1_000


def test_registry_batch_metrics_bound_write_amplification(tmp_path: Path) -> None:
    source, _, registry_path = _fixture(tmp_path, 65)
    summary = scan_module.scan_source(
        source,
        workers=1,
        registry_path=registry_path,
        registry_batch_size=16,
    )
    assert summary.registry_batch_count == 5
    assert summary.registry_file_mutation_count == 65
    assert summary.registry_fallback_count == 0
    assert summary.registry_file_mutation_count / summary.registry_batch_count <= 16


def test_streaming_progress_keeps_total_unknown_until_scan_complete(tmp_path: Path) -> None:
    source, workspace, registry_path = _fixture(tmp_path, 12)
    events: list[dict[str, object]] = []

    def progress(_stage: str, _value: float, **metadata: object) -> None:
        events.append(metadata)

    process_source(
        source,
        workers=1,
        force=True,
        registry_path=registry_path,
        workspace_root=workspace,
        progress_callback=progress,
    )
    before_complete = [item for item in events if item.get("scan_complete") is False]
    after_complete = [item for item in events if item.get("scan_complete") is True]
    assert before_complete
    assert all(item.get("total") is None for item in before_complete)
    assert after_complete
    assert any(item.get("total") == 12 for item in after_complete)
