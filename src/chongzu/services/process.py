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

from chongzu.cancellation import CancellationRequested
from chongzu.clean.runner import process_source
from chongzu.locking import registry_write_mutex
from chongzu.registry import Registry, RegistryError, is_registry_busy_error
from chongzu.worker_runtime import configure_hidden_worker_executable
from chongzu.vision.runner import normalize_vision_mode


class SourceValidationError(ValueError):
    """Raised when a user supplied process source is not a directory."""


class TaskAdmissionError(RuntimeError):
    """Raised when the server is already draining and accepts no new work."""


class ProgressCallback(Protocol):
    """Stable stage callback; the core pipeline does not know about HTTP."""

    def __call__(
        self,
        stage: str,
        progress: float,
        *,
        current_file: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_substage: str | None = None,
        run_id: str | None = None,
    ) -> None: ...


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
    vision_mode: str = "local"
    status: str = "queued"
    progress: float = 0.0
    current_stage: str = "queued"
    current_file: str | None = None
    current_page: int | None = None
    completed: int = 0
    total: int = 0
    current_substage: str | None = None
    elapsed_seconds: float = 0.0
    run_id: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_summary: str | None = None
    error: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    request_id: str | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _started_clock: float | None = field(default=None, repr=False)
    _future: Future[Any] | None = field(default=None, repr=False)
    _vision_provider: object | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "source": self.source,
            "visionMode": self.vision_mode,
            "status": self.status,
            "progress": round(float(self.progress), 4),
            "currentStage": self.current_stage,
            "currentFile": self.current_file,
            "currentPage": self.current_page,
            "completed": self.completed,
            "total": self.total,
            "currentSubstage": self.current_substage,
            "elapsedSeconds": round(float(self.elapsed_seconds), 2),
            "runId": self.run_id,
            "counts": dict(self.counts),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "errorSummary": self.error_summary,
            "error": self.error,
            "summary": self.summary,
        }


class ProcessTaskManager:
    """One bounded local worker; process_source retains file isolation."""

    def __init__(self, *, max_workers: int = 1) -> None:
        if int(max_workers) != 1:
            raise ValueError("the product process task manager supports exactly one active writer task")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chongzu-api-process")
        self._lock = threading.RLock()
        self._tasks: dict[str, ProcessTask] = {}
        self._shutdown_requested = False
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

    def submit(
        self,
        source: Path | str,
        *,
        force: bool = False,
        request_id: str | None = None,
        vision_mode: str = "local",
        vision_provider: object | None = None,
    ) -> ProcessTask:
        validated = validate_source_directory(source)
        normalized_vision_mode = normalize_vision_mode(vision_mode)
        if normalized_vision_mode == "ai_vision" and vision_provider is None:
            raise TaskAdmissionError("AI Vision provider is not verified")
        with self._lock:
            if self._shutdown_requested:
                raise TaskAdmissionError("server is stopping and cannot accept new processing tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source=str(validated),
                vision_mode=normalized_vision_mode,
                request_id=request_id,
                _vision_provider=vision_provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run, task.task_id, validated, force)
        return task

    def _set_stage(
        self,
        task: ProcessTask,
        stage: str,
        progress: float,
        *,
        current_file: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_substage: str | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._lock:
            if task.status == "queued":
                task.status = "running"
                task.started_at = datetime.now().astimezone().replace(tzinfo=None)
                task._started_clock = time.perf_counter()
            elif task.status == "cancelling":
                # A stop request owns the visible state. Late callbacks from
                # an active route must not make a cancelling task look alive
                # again while the server is draining it.
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
                return
            task.current_stage = stage
            task.progress = max(0.0, min(1.0, progress))
            if current_file is not None:
                task.current_file = current_file
            if completed is not None:
                task.completed = max(0, int(completed))
            if total is not None:
                task.total = max(0, int(total))
            if current_page is not None:
                task.current_page = max(1, int(current_page))
            if current_substage is not None:
                task.current_substage = current_substage
            if run_id is not None:
                task.run_id = run_id
            if task._started_clock is not None:
                task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)

    @staticmethod
    def _error_for(task: ProcessTask, exc: Exception) -> dict[str, Any]:
        detail = str(exc).strip()
        provider_config = getattr(task._vision_provider, "config", None)
        provider_secret = getattr(provider_config, "api_key", "") if provider_config is not None else ""
        if isinstance(provider_secret, str) and provider_secret:
            detail = detail.replace(provider_secret, "[REDACTED]")
        lowered = detail.casefold()
        if isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
            code = "TASK_CANCELLED"
            message = "用户已请求停止处理。"
            retryable = True
            stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "shutdown"
        elif is_registry_busy_error(exc):
            code = "REGISTRY_BUSY"
            message = "数据目录正在更新，请稍后重试。"
            retryable = True
            stage = "registry_init" if "initialize registry" in lowered else task.current_stage
        elif isinstance(exc, RegistryError) or type(exc).__name__.casefold() in {"ioexception", "catalogexception"}:
            code = "REGISTRY_UNAVAILABLE"
            message = "数据目录暂时不可用，请稍后重试。"
            retryable = True
            stage = "registry_init"
        else:
            code = "PROCESS_FAILED"
            message = "目录处理失败，请查看技术详情后重试。"
            retryable = True
            stage = task.current_stage
        technical = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return {
            "code": code,
            "stage": stage,
            "message": message,
            "retryable": retryable,
            "affectedFile": task.current_file,
            "runId": task.run_id,
            "requestId": task.request_id,
            "scope": "file" if task.current_file else "directory",
            "technicalDetail": technical,
        }

    def _recover_source(self, source: Path) -> None:
        if self.registry_path is None or not self.registry_path.is_file():
            return
        registry = None
        try:
            with registry_write_mutex(self.registry_path):
                registry = Registry.open(self.registry_path, initialize=False)
                source_root = str(source.resolve()).casefold()
                registry.recover_incomplete_runs(source_root)
                registry.recover_incomplete_extractions(source_root)
                registry.recover_incomplete_cleaning(source_root)
        except Exception:
            # Startup recovery is the final safety net if the database itself
            # is unavailable; never mask the original task failure here.
            return
        finally:
            if registry is not None:
                registry.close()

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
        configure_hidden_worker_executable()
        started = time.perf_counter()
        try:
            # The core coordinator remains independent from HTTP.  The callback
            # is a small stage event seam; future coordinators can emit finer
            # file events without changing the API contract.
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                run_id: str | None = None,
            ) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=current_file,
                    completed=completed,
                    total=total,
                    current_page=current_page,
                    current_substage=current_substage,
                    run_id=run_id,
                )

            with registry_write_mutex(self.registry_path or source / ".registry"):
                summary = process_source(
                    source,
                    force=force,
                    registry_path=self.registry_path,
                    workspace_root=self.workspace_root,
                    progress_callback=progress,
                    cancel_event=task._cancel_event,
                    vision_mode=task.vision_mode,
                    vision_provider=task._vision_provider,
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
                if task._cancel_event.is_set() or task.status == "cancelling":
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.progress = min(task.progress, 1.0)
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
                else:
                    task.status = "succeeded"
                    task.progress = 1.0
                    task.current_stage = "completed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = self._summary_counts(summary)
                task.summary = public_summary
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except CancellationRequested as exc:
            self._recover_source(source)
            with self._lock:
                task.status = "cancelled"
                task.current_stage = "cancelled"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:  # one task failure must be returned as JSON only
            if task._cancel_event.is_set():
                self._recover_source(source)
            with self._lock:
                task.status = "interrupted" if task._cancel_event.is_set() else "failed"
                task.progress = min(task.progress, 0.99)
                task.current_stage = "interrupted" if task._cancel_event.is_set() else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            _ = started

    def get(self, task_id: str) -> ProcessTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, *, limit: int = 50) -> list[ProcessTask]:
        with self._lock:
            values = sorted(self._tasks.values(), key=lambda item: item.task_id, reverse=True)
            return values[:limit]

    def request_shutdown(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            for task in self._tasks.values():
                if task.status == "queued":
                    task._cancel_event.set()
                    future = task._future
                    if future is not None and future.cancel():
                        task.status = "cancelled"
                        task.current_stage = "cancelled"
                        task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                        task.error = self._error_for(task, CancellationRequested())
                        task.error_summary = task.error["message"]
                    else:
                        # The coordinator may have started between submit and
                        # this stop request, before its first progress event.
                        # Treat that state as active so shutdown waits for its
                        # cancellation/recovery path instead of declaring the
                        # server drained while the task is still opening files.
                        task.status = "cancelling"
                        task.current_stage = "cancelling"
                elif task.status in {"running", "cancelling"}:
                    task.status = "cancelling"
                    task.current_stage = "cancelling"
                    task._cancel_event.set()

    def shutdown(self, *, timeout: float = 5.0) -> bool:
        """Request cancellation and wait a bounded time for the coordinator."""

        self.request_shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                active = [
                    task
                    for task in self._tasks.values()
                    if task.status in {"running", "cancelling"}
                ]
            if not active:
                return True
            time.sleep(0.05)
        with self._lock:
            for task in self._tasks.values():
                if task.status in {"running", "cancelling"}:
                    task.status = "interrupted"
                    task.current_stage = "interrupted"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
        return False
