"""Asynchronous process-task service for the local product server."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from chongzu.clean.runner import process_source


class SourceValidationError(ValueError):
    """Raised when a user supplied process source is not a directory."""


class ProgressCallback(Protocol):
    """Stable stage callback; the core pipeline does not know about HTTP."""

    def __call__(self, stage: str, progress: float) -> None: ...


def validate_source_directory(value: object) -> Path:
    if isinstance(value, Path):
        source_value = value
    elif isinstance(value, str) and value.strip():
        source_value = Path(value.strip())
    else:
        raise SourceValidationError("source must be a non-empty directory path")
    source = source_value.expanduser().resolve()
    if not source.exists():
        raise SourceValidationError(f"source directory does not exist: {source}")
    if not source.is_dir():
        raise SourceValidationError(f"source is not a directory: {source}")
    return source


@dataclass
class ProcessTask:
    task_id: str
    source: str
    status: str = "queued"
    progress: float = 0.0
    current_stage: str = "queued"
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_summary: str | None = None
    summary: dict[str, Any] | None = None
    _future: Future[Any] | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "source": self.source,
            "status": self.status,
            "progress": round(float(self.progress), 4),
            "currentStage": self.current_stage,
            "counts": dict(self.counts),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "errorSummary": self.error_summary,
            "summary": self.summary,
        }


class ProcessTaskManager:
    """One bounded local worker; process_source retains file isolation."""

    def __init__(self, *, max_workers: int = 1) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chongzu-api-process")
        self._lock = threading.RLock()
        self._tasks: dict[str, ProcessTask] = {}
        self.registry_path: Path | None = None
        self.workspace_root: Path | None = None

    @classmethod
    def for_paths(
        cls,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        max_workers: int = 1,
    ) -> "ProcessTaskManager":
        manager = cls(max_workers=max_workers)
        manager.registry_path = Path(registry_path).resolve() if registry_path is not None else None
        manager.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        return manager

    def submit(self, source: Path | str, *, force: bool = False) -> ProcessTask:
        validated = validate_source_directory(source)
        task = ProcessTask(task_id=f"task_{uuid4().hex}", source=str(validated))
        with self._lock:
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run, task.task_id, validated, force)
        return task

    def _set_stage(self, task: ProcessTask, stage: str, progress: float) -> None:
        with self._lock:
            if task.status == "queued":
                task.status = "running"
                task.started_at = datetime.now().astimezone().replace(tzinfo=None)
            task.current_stage = stage
            task.progress = max(0.0, min(1.0, progress))

    @staticmethod
    def _summary_counts(summary: Any) -> dict[str, Any]:
        values = asdict(summary)
        return {
            "filesDiscovered": values.get("files_discovered", 0),
            "filesSupported": values.get("files_supported", 0),
            "filesUnsupported": values.get("files_unsupported", 0),
            "extracted": values.get("extracted", 0),
            "reusedExtraction": values.get("reused_extraction", 0),
            "tableAssets": values.get("table_assets", 0),
            "textAssets": values.get("text_assets", 0),
            "cleaned": values.get("cleaned", 0),
            "reusedCleaning": values.get("reused_cleaning", 0),
            "cleaningFailures": values.get("cleaning_failures", 0),
            "qualityIssues": values.get("quality_issues", 0),
            "ready": values.get("ready", 0),
            "needsReview": values.get("needs_review", 0),
            "unusable": values.get("unusable", 0),
        }

    def _run(self, task_id: str, source: Path, force: bool) -> None:
        with self._lock:
            task = self._tasks[task_id]
        started = time.perf_counter()
        try:
            # The core coordinator remains independent from HTTP.  The callback
            # is a small stage event seam; future coordinators can emit finer
            # file events without changing the API contract.
            def progress(stage: str, value: float) -> None:
                self._set_stage(task, stage, value)

            summary = process_source(
                source,
                force=force,
                registry_path=self.registry_path,
                workspace_root=self.workspace_root,
                progress_callback=progress,
            )
            public_summary = {
                "sourceRoot": summary.source_root,
                "filesDiscovered": summary.files_discovered,
                "filesSupported": summary.files_supported,
                "filesUnsupported": summary.files_unsupported,
                "extracted": summary.extracted,
                "reusedExtraction": summary.reused_extraction,
                "extractionFailures": summary.extraction_failures,
                "tableAssets": summary.table_assets,
                "textAssets": summary.text_assets,
                "cleaned": summary.cleaned,
                "reusedCleaning": summary.reused_cleaning,
                "cleaningFailures": summary.cleaning_failures,
                "ready": summary.ready,
                "needsReview": summary.needs_review,
                "unusable": summary.unusable,
                "qualityIssues": summary.quality_issues,
                "semanticPending": summary.semantic_pending,
                "rows": summary.rows,
                "chars": summary.chars,
                "wallTimeMs": summary.wall_time_ms,
            }
            with self._lock:
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = self._summary_counts(summary)
                task.summary = public_summary
        except Exception as exc:  # one task failure must be returned as JSON only
            with self._lock:
                task.status = "failed"
                task.progress = 1.0
                task.current_stage = "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error_summary = str(exc)
        finally:
            _ = started

    def get(self, task_id: str) -> ProcessTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, *, limit: int = 50) -> list[ProcessTask]:
        with self._lock:
            values = sorted(self._tasks.values(), key=lambda item: item.task_id, reverse=True)
            return values[:limit]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
