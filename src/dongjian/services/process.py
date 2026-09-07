"""Asynchronous process-task service for the local product server."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from dongjian.cancellation import CancellationRequested
from dongjian.clean.runner import RegisteredFileReprocessError, process_source, reprocess_registered_file
from dongjian.locking import registry_write_mutex
from dongjian.registry import Registry, RegistryError, is_registry_busy_error
from dongjian.worker_runtime import configure_hidden_worker_executable
from dongjian.vision.runner import normalize_vision_mode
from .report import ReportExecutionError
from .file_insight import FileInsightError, FileInsightService
from .catalog import CatalogService

from .analysis import AnalysisExecutionError


_FILE_INSIGHT_PROVIDER_SLOTS = threading.BoundedSemaphore(2)


def _with_file_insight_provider_slot(operation: Callable[[], Any]) -> Any:
    with _FILE_INSIGHT_PROVIDER_SLOTS:
        return operation()


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
        current_file_id: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_page_total: int | None = None,
        current_substage: str | None = None,
        current_step: int | None = None,
        max_steps: int | None = None,
        run_id: str | None = None,
        discovered_count: int | None = None,
        registered_count: int | None = None,
        ready_local_count: int | None = None,
        failed_count: int | None = None,
        skipped_count: int | None = None,
        scan_complete: bool | None = None,
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
    task_type: str = "process"
    vision_mode: str = "local"
    status: str = "queued"
    progress: float = 0.0
    current_stage: str = "queued"
    current_file: str | None = None
    current_file_id: str | None = None
    current_page: int | None = None
    current_page_total: int | None = None
    completed: int = 0
    total: int | None = None
    discovered_count: int = 0
    registered_count: int = 0
    ready_local_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    scan_complete: bool = False
    current_substage: str | None = None
    current_step: int = 0
    max_steps: int = 0
    elapsed_seconds: float = 0.0
    run_id: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now().astimezone().replace(tzinfo=None))
    error_summary: str | None = None
    error: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    cancel_reason: str | None = None
    recent_files_per_minute: float | None = None
    request_id: str | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _started_clock: float | None = field(default=None, repr=False)
    _future: Future[Any] | None = field(default=None, repr=False)
    _throughput_samples: list[tuple[float, int]] = field(default_factory=list, repr=False)
    _vision_provider: object | None = field(default=None, repr=False)
    file_insight_id: str | None = None
    file_insight_force: bool = False
    auto_file_insight: bool = False
    _file_insight_provider: object | None = field(default=None, repr=False)
    _file_insight_service: FileInsightService | None = field(default=None, repr=False)
    analysis_run_id: str | None = None
    _analysis_runner: Callable[..., Mapping[str, Any]] | None = field(default=None, repr=False)
    _analysis_provider: object | None = field(default=None, repr=False)
    report_id: str | None = None
    _report_runner: Callable[..., Mapping[str, Any]] | None = field(default=None, repr=False)
    _report_provider: object | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        elapsed_seconds = self.elapsed_seconds
        if self._started_clock is not None and self.status in {"queued", "running", "cancelling"}:
            elapsed_seconds = max(0.0, time.perf_counter() - self._started_clock)
        return {
            "taskId": self.task_id,
            "source": self.source,
            "taskType": self.task_type,
            "visionMode": self.vision_mode,
            "status": self.status,
            "progress": round(float(self.progress), 4),
            "currentStage": self.current_stage,
            "currentFile": self.current_file,
            "currentFileId": self.current_file_id,
            "currentPage": self.current_page,
            "currentPageTotal": self.current_page_total,
            "completed": self.completed,
            "total": self.total,
            "discoveredCount": self.discovered_count,
            "registeredCount": self.registered_count,
            "readyLocalCount": self.ready_local_count,
            "failedCount": self.failed_count,
            "skippedCount": self.skipped_count,
            "scanComplete": self.scan_complete,
            "currentSubstage": self.current_substage,
            "currentStep": self.current_step,
            "maxSteps": self.max_steps,
            "elapsedSeconds": round(float(elapsed_seconds), 2),
            "runId": self.run_id,
            "analysisRunId": self.analysis_run_id,
            "reportId": self.report_id,
            "fileInsightId": self.file_insight_id,
            "autoFileInsight": self.auto_file_insight,
            "cancelReason": self.cancel_reason,
            "counts": dict(self.counts),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "updatedAt": (self.finished_at or self.updated_at).isoformat(),
            "errorSummary": self.error_summary,
            "error": self.error,
            "summary": self.summary,
            "recentFilesPerMinute": round(self.recent_files_per_minute, 2) if self.recent_files_per_minute is not None else None,
        }


class ProcessTaskManager:
    """One bounded local worker; process_source retains file isolation."""

    def __init__(self, *, max_workers: int = 1, ai_max_workers: int = 2) -> None:
        if int(max_workers) != 1:
            raise ValueError("the product process task manager supports exactly one active writer task")
        if int(ai_max_workers) not in {1, 2}:
            raise ValueError("AI file insight workers must be 1 or 2")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dongjian-api-process")
        self._ai_executor = ThreadPoolExecutor(max_workers=int(ai_max_workers), thread_name_prefix="dongjian-api-file-insight")
        self._lock = threading.RLock()
        self._tasks: dict[str, ProcessTask] = {}
        self._shutdown_requested = False
        # Reset uses the same admission lock as task submission.  This is a
        # process-local barrier: once it is raised, no new writer/task can be
        # admitted while the reset service is changing generated state.
        self._reset_in_progress = False
        self.registry_path: Path | None = None
        self.workspace_root: Path | None = None
        self._workspace_change_callback: Callable[[str | None], None] | None = None

    @classmethod
    def for_paths(
        cls,
        *,
        registry_path: Path | str | None = None,
        workspace_root: Path | str | None = None,
        max_workers: int = 1,
        ai_max_workers: int = 2,
    ) -> "ProcessTaskManager":
        manager = cls(max_workers=max_workers, ai_max_workers=ai_max_workers)
        manager.registry_path = Path(registry_path).resolve() if registry_path is not None else None
        manager.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        return manager

    def set_workspace_change_callback(self, callback: Callable[[str | None], None] | None) -> None:
        self._workspace_change_callback = callback

    def _workspace_changed(self, file_id: str | None = None) -> None:
        callback = self._workspace_change_callback
        if callback is not None:
            callback(file_id)

    def submit(
        self,
        source: Path | str,
        *,
        force: bool = False,
        request_id: str | None = None,
        vision_mode: str = "local",
        vision_provider: object | None = None,
        file_insight_provider: object | None = None,
        auto_file_insight: bool = False,
    ) -> ProcessTask:
        validated = validate_source_directory(source)
        normalized_vision_mode = normalize_vision_mode(vision_mode)
        if normalized_vision_mode == "ai_vision" and vision_provider is None:
            raise TaskAdmissionError("AI Vision provider is not verified")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new processing tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source=str(validated),
                vision_mode=normalized_vision_mode,
                request_id=request_id,
                _vision_provider=vision_provider,
                _file_insight_provider=file_insight_provider,
                auto_file_insight=bool(auto_file_insight),
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run, task.task_id, validated, force)
        return task

    def submit_file_reprocess(
        self,
        file_id: str,
        source_root: str,
        relative_path: str,
        expected_sha256: str,
        *,
        request_id: str | None = None,
    ) -> ProcessTask:
        """Queue one local reprocess for a registered file."""

        if not file_id or not source_root or not relative_path or not expected_sha256:
            raise TaskAdmissionError("registered file reprocess identity is incomplete")
        with self._lock:
            active = [
                item
                for item in self._tasks.values()
                if item.task_type == "file_reprocess"
                and item.run_id == file_id
                and item.status in {"queued", "running", "cancelling"}
            ]
            if active:
                return max(active, key=lambda item: item.task_id)
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept file reprocess tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source=relative_path,
                task_type="file_reprocess",
                request_id=request_id,
                run_id=file_id,
                current_file=relative_path,
                current_file_id=file_id,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(
                self._run_file_reprocess,
                task.task_id,
                Path(source_root),
                file_id,
                expected_sha256,
            )
        return task

    def submit_file_insight(
        self,
        file_id: str,
        source: str,
        service: FileInsightService,
        *,
        provider: object,
        request_id: str | None = None,
        force: bool = False,
    ) -> ProcessTask:
        """Queue one file-level insight on the independent, bounded AI pool."""

        if not file_id or not callable(getattr(service, "enrich", None)):
            raise TaskAdmissionError("file insight service is unavailable")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept AI file understanding tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source=source,
                task_type="file_insight",
                request_id=request_id,
                run_id=file_id,
                file_insight_id=file_id,
                file_insight_force=force,
                _file_insight_provider=provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._ai_executor.submit(self._run_file_insight, task.task_id, service)
        return task

    def submit_file_insight_batch(
        self,
        service: FileInsightService,
        *,
        provider: object,
        request_id: str | None = None,
        total: int = 0,
    ) -> ProcessTask:
        """Start one manifest consumer for all admitted FileInsight work."""

        if not callable(getattr(service, "enrich", None)):
            raise TaskAdmissionError("file insight service is unavailable")
        with self._lock:
            active = [
                item
                for item in self._tasks.values()
                if item.task_type == "file_insight_batch"
                and item.status in {"queued", "running", "cancelling"}
            ]
            if active:
                active_task = max(active, key=lambda item: item.task_id)
                active_task.total = max(int(active_task.total or 0), int(active_task.completed or 0) + max(0, int(total)))
                return max(active, key=lambda item: item.task_id)
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept AI file understanding tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source="AI 文件整理",
                task_type="file_insight_batch",
                request_id=request_id,
                total=max(0, int(total)),
                counts={"queued": max(0, int(total)), "running": 0, "completed": 0, "failed": 0, "total": max(0, int(total))},
                _file_insight_provider=provider,
                _file_insight_service=service,
            )
            self._tasks[task.task_id] = task
            task._future = self._ai_executor.submit(self._run_file_insight_batch, task.task_id)
        return task

    def submit_analysis(
        self,
        analysis_run_id: str,
        runner: Callable[..., Mapping[str, Any]],
        *,
        provider: object | None = None,
        request_id: str | None = None,
        max_steps: int = 6,
    ) -> ProcessTask:
        """Admit one bounded AI Analysis run onto the existing task worker."""

        if not isinstance(analysis_run_id, str) or not analysis_run_id.strip():
            raise TaskAdmissionError("analysis run ID is required")
        if not callable(runner):
            raise TaskAdmissionError("analysis runner is unavailable")
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1 or max_steps > 6:
            raise TaskAdmissionError("analysis step limit is invalid")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new analysis tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source="AI Analysis",
                task_type="ai_analysis",
                vision_mode="local",
                request_id=request_id,
                analysis_run_id=analysis_run_id,
                run_id=analysis_run_id,
                max_steps=max_steps,
                _analysis_runner=runner,
                _analysis_provider=provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run_analysis, task.task_id)
        return task

    def submit_report(
        self,
        report_id: str,
        runner: Callable[..., Mapping[str, Any]],
        *,
        provider: object | None = None,
        request_id: str | None = None,
    ) -> ProcessTask:
        """Admit one bounded report composition task onto the same worker."""

        if not isinstance(report_id, str) or not report_id.strip():
            raise TaskAdmissionError("report ID is required")
        if not callable(runner):
            raise TaskAdmissionError("report runner is unavailable")
        with self._lock:
            if self._shutdown_requested or self._reset_in_progress:
                raise TaskAdmissionError("server is stopping and cannot accept new report tasks")
            task = ProcessTask(
                task_id=f"task_{uuid4().hex}",
                source="Analysis Report",
                task_type="report_generation",
                vision_mode="local",
                request_id=request_id,
                report_id=report_id,
                run_id=report_id,
                max_steps=1,
                _report_runner=runner,
                _report_provider=provider,
            )
            self._tasks[task.task_id] = task
            task._future = self._executor.submit(self._run_report, task.task_id)
        return task

    def _set_stage(
        self,
        task: ProcessTask,
        stage: str,
        progress: float,
        *,
        current_file: str | None = None,
        current_file_id: str | None = None,
        completed: int | None = None,
        total: int | None = None,
        current_page: int | None = None,
        current_page_total: int | None = None,
        current_substage: str | None = None,
        current_step: int | None = None,
        max_steps: int | None = None,
        run_id: str | None = None,
        discovered_count: int | None = None,
        registered_count: int | None = None,
        ready_local_count: int | None = None,
        failed_count: int | None = None,
        skipped_count: int | None = None,
        scan_complete: bool | None = None,
    ) -> None:
        with self._lock:
            if task.status == "queued":
                task.status = "running"
                task.started_at = datetime.now().astimezone().replace(tzinfo=None)
                task._started_clock = time.perf_counter()
            elif task.status in {"cancelling", "succeeded", "failed", "cancelled", "interrupted"}:
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
            if current_file_id is not None:
                if task.current_file_id is not None and task.current_file_id != current_file_id:
                    task.current_page = None
                    task.current_page_total = None
                task.current_file_id = current_file_id
            if completed is not None:
                task.completed = max(0, int(completed))
                now = time.perf_counter()
                if not task._throughput_samples or task.completed > task._throughput_samples[-1][1]:
                    task._throughput_samples.append((now, task.completed))
                    task._throughput_samples = task._throughput_samples[-20:]
                    if len(task._throughput_samples) >= 2:
                        first_time, first_count = task._throughput_samples[0]
                        elapsed = now - first_time
                        delta = task.completed - first_count
                        if elapsed > 0 and delta >= 0:
                            task.recent_files_per_minute = delta / elapsed * 60.0
            if total is not None:
                task.total = max(0, int(total))
            if discovered_count is not None:
                task.discovered_count = max(0, int(discovered_count))
            if registered_count is not None:
                task.registered_count = max(0, int(registered_count))
            if ready_local_count is not None:
                task.ready_local_count = max(0, int(ready_local_count))
            if failed_count is not None:
                task.failed_count = max(0, int(failed_count))
            if skipped_count is not None:
                task.skipped_count = max(0, int(skipped_count))
            if scan_complete is not None:
                task.scan_complete = bool(scan_complete)
            if current_page is not None:
                task.current_page = max(1, int(current_page))
            if current_page_total is not None:
                task.current_page_total = max(1, int(current_page_total))
            if current_substage is not None:
                task.current_substage = current_substage
            if current_step is not None:
                task.current_step = max(0, int(current_step))
            if max_steps is not None:
                task.max_steps = max(0, int(max_steps))
            if run_id is not None:
                task.run_id = run_id
            if task._started_clock is not None:
                task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            task.updated_at = datetime.now().astimezone().replace(tzinfo=None)

    @staticmethod
    def _error_for(task: ProcessTask, exc: Exception) -> dict[str, Any]:
        detail = str(exc).strip()
        provider = (
            task._analysis_provider
            if task.task_type == "ai_analysis"
            else task._report_provider
            if task.task_type == "report_generation"
            else task._file_insight_provider
            if task.task_type in {"file_insight", "file_insight_batch"}
            else task._vision_provider
        )
        provider_config = getattr(provider, "config", None)
        provider_secret = getattr(provider_config, "api_key", "") if provider_config is not None else ""
        if isinstance(provider_secret, str) and provider_secret:
            detail = detail.replace(provider_secret, "[REDACTED]")
        lowered = detail.casefold()
        if task.task_type == "ai_analysis":
            if isinstance(exc, AnalysisExecutionError):
                code = exc.code
                message = exc.message
                retryable = exc.retryable
                stage = exc.stage
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "ANALYSIS_CANCELLED"
                message = "analysis was cancelled"
                retryable = True
                stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "cancelled"
            else:
                code = "ANALYSIS_FAILED"
                message = "AI analysis could not be completed"
                retryable = True
                stage = task.current_stage
            scope = "analysis"
        elif task.task_type == "report_generation":
            if isinstance(exc, ReportExecutionError):
                code = exc.code
                message = exc.message
                retryable = exc.retryable
                stage = exc.stage
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "REPORT_CANCELLED"
                message = "report generation was cancelled"
                retryable = True
                stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "cancelled"
            else:
                code = "REPORT_FAILED"
                message = "analysis report could not be generated"
                retryable = True
                stage = task.current_stage
            scope = "report"
        elif task.task_type == "file_reprocess":
            if isinstance(exc, RegisteredFileReprocessError):
                code = exc.code
                message = str(exc)
                retryable = code in {"STALE_SOURCE", "SOURCE_NOT_FOUND"}
                stage = "source_validation"
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "FILE_REPROCESS_CANCELLED"
                message = "local file reprocessing was cancelled"
                retryable = True
                stage = "cancelled"
            else:
                code = "FILE_REPROCESS_FAILED"
                message = "local file reprocessing failed"
                retryable = True
                stage = task.current_stage
            scope = "file"
        elif task.task_type in {"file_insight", "file_insight_batch"}:
            if isinstance(exc, FileInsightError):
                code = exc.code
                message = exc.message
                retryable = exc.retryable
                stage = getattr(exc, "stage", task.current_stage)
            elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
                code = "FILE_INSIGHT_CANCELLED"
                message = "AI file understanding was cancelled"
                retryable = True
                stage = "cancelled"
            else:
                code = "FILE_INSIGHT_FAILED"
                message = "AI file understanding failed"
                retryable = True
                stage = task.current_stage
            scope = "file" if task.task_type == "file_insight" else "files"
        elif isinstance(exc, CancellationRequested) or task._cancel_event.is_set():
            code = "TASK_CANCELLED"
            message = "用户已请求停止处理。"
            retryable = True
            stage = task.current_stage if task.current_stage not in {"queued", "cancelling"} else "shutdown"
            scope = "file" if task.current_file else "directory"
        elif is_registry_busy_error(exc):
            code = "REGISTRY_BUSY"
            message = "数据目录正在更新，请稍后重试。"
            retryable = True
            stage = "registry_init" if "initialize registry" in lowered else task.current_stage
            scope = "file" if task.current_file else "directory"
        elif isinstance(exc, RegistryError) or type(exc).__name__.casefold() in {"ioexception", "catalogexception"}:
            code = "REGISTRY_UNAVAILABLE"
            message = "数据目录暂时不可用，请稍后重试。"
            retryable = True
            stage = "registry_init"
            scope = "file" if task.current_file else "directory"
        else:
            code = "PROCESS_FAILED"
            message = "目录处理失败，请查看技术详情后重试。"
            retryable = True
            stage = task.current_stage
            scope = "file" if task.current_file else "directory"
        technical = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        return {
            "code": code,
            "stage": stage,
            "message": message,
            "retryable": retryable,
            "affectedFile": task.current_file,
            "runId": task.run_id,
            "requestId": task.request_id,
            "scope": scope,
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
            "filesFailed": int(values.get("extraction_failures", 0) or 0) + int(values.get("cleaning_failures", 0) or 0),
            "filesSkipped": values.get("files_unsupported", 0),
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
            file_insight_service = None
            if task._file_insight_provider is not None:
                file_insight_service = FileInsightService(
                    catalog=CatalogService(
                        registry_path=self.registry_path,
                        workspace_root=self.workspace_root,
                    ),
                    workspace_root=self.workspace_root or source.parent / "workspace",
                )
            # The core coordinator remains independent from HTTP.  The callback
            # is a small stage event seam; future coordinators can emit finer
            # file events without changing the API contract.
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                current_file_id: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_page_total: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
                run_id: str | None = None,
                discovered_count: int | None = None,
                registered_count: int | None = None,
                ready_local_count: int | None = None,
                failed_count: int | None = None,
                skipped_count: int | None = None,
                scan_complete: bool | None = None,
            ) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=current_file,
                    current_file_id=current_file_id,
                    completed=completed,
                    total=total,
                    current_page=current_page,
                    current_page_total=current_page_total,
                    current_substage=current_substage,
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id,
                    discovered_count=discovered_count,
                    registered_count=registered_count,
                    ready_local_count=ready_local_count,
                    failed_count=failed_count,
                    skipped_count=skipped_count,
                    scan_complete=scan_complete,
                )
                if stage == "local_ready" and task.auto_file_insight and current_file_id and task._file_insight_provider is not None and file_insight_service is not None:
                    self._workspace_changed(current_file_id)
                    try:
                        if file_insight_service.enqueue_file(current_file_id):
                            self.submit_file_insight_batch(
                                file_insight_service,
                                provider=task._file_insight_provider,
                                request_id=task.request_id,
                                total=1,
                            )
                    except Exception:
                        # The local result is already durable and usable.  AI
                        # admission/queue failure must never turn it into a
                        # process failure.
                        pass
                elif stage == "local_ready" and current_file_id:
                    self._workspace_changed(current_file_id)

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
                "scan": dict(summary.scan_metrics),
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
                task.discovered_count = max(task.discovered_count, int(summary.files_discovered))
                task.registered_count = max(task.registered_count, int(summary.files_discovered))
                if task.ready_local_count == 0 and summary.files_supported:
                    task.ready_local_count = max(0, int(summary.files_supported))
                task.failed_count = max(task.failed_count, int(summary.extraction_failures) + int(summary.cleaning_failures))
                task.skipped_count = max(task.skipped_count, int(summary.files_unsupported))
                task.scan_complete = True
                task.total = int(summary.files_discovered)
                task.counts.update(
                    {
                        "discoveredCount": task.discovered_count,
                        "registeredCount": task.registered_count,
                        "readyLocalCount": task.ready_local_count,
                        "failedCount": task.failed_count,
                        "skippedCount": task.skipped_count,
                        "scanComplete": task.scan_complete,
                    }
                )
                task.summary = public_summary
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except CancellationRequested as exc:
            self._recover_source(source)
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                task.status = "cancelled"
                task.current_stage = "cancelled"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:  # one task failure must be returned as JSON only
            if task._cancel_event.is_set():
                self._recover_source(source)
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
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

    def _run_file_reprocess(
        self,
        task_id: str,
        source_root: Path,
        file_id: str,
        expected_sha256: str,
    ) -> None:
        with self._lock:
            task = self._tasks[task_id]
        try:
            def progress(stage: str, value: float, **metadata: Any) -> None:
                self._set_stage(
                    task,
                    stage,
                    value,
                    current_file=str(metadata.get("current_file") or task.source),
                    current_file_id=file_id,
                    completed=metadata.get("completed"),
                    total=metadata.get("total"),
                    current_page=metadata.get("current_page"),
                    current_page_total=metadata.get("current_page_total"),
                    current_substage=metadata.get("current_substage"),
                    run_id=metadata.get("run_id"),
                )
                if stage == "local_ready":
                    self._workspace_changed(file_id)

            summary = reprocess_registered_file(
                source_root,
                file_id=file_id,
                expected_sha256=expected_sha256,
                registry_path=self.registry_path or source_root / "workspace" / "state" / "registry.duckdb",
                workspace_root=self.workspace_root or source_root / "workspace",
                progress_callback=progress,
                cancel_event=task._cancel_event,
            )
            with self._lock:
                if task._cancel_event.is_set() or task.status == "cancelling":
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
                else:
                    task.status = "succeeded"
                    task.progress = 1.0
                    task.current_stage = "completed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.completed = 1
                task.total = 1
                task.counts = self._summary_counts(summary)
                task.summary = {
                    "fileId": file_id,
                    "sourceRoot": summary.source_root,
                    "filesDiscovered": summary.files_discovered,
                    "extractionFailures": summary.extraction_failures,
                    "cleaningFailures": summary.cleaning_failures,
                    "tableAssets": summary.table_assets,
                    "textAssets": summary.text_assets,
                }
        except CancellationRequested as exc:
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "interrupted" if task._cancel_event.is_set() else "failed"
                    task.current_stage = "interrupted" if task._cancel_event.is_set() else "failed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)

    @staticmethod
    def _queue_update(
        queue: object | None,
        method: str,
        file_id: str,
    source_sha256: str | None = None,
    phase: str | None = None,
    error_code: str | None = None,
    error_stage: str | None = None,
    ) -> None:
        if queue is None or not file_id:
            return
        try:
            operation = getattr(queue, method, None)
            if callable(operation):
                if method == "set_phase":
                    operation(file_id, phase or "requesting_model", source_sha256=source_sha256)
                elif method == "mark_failed":
                    try:
                        operation(file_id, source_sha256=source_sha256, error_code=error_code, error_stage=error_stage)
                    except TypeError:
                        operation(file_id, source_sha256=source_sha256)
                elif source_sha256:
                    try:
                        operation(file_id, source_sha256=source_sha256)
                    except TypeError:
                        # Keep the small queue seam compatible with older
                        # test/dummy implementations that accept file_id only.
                        operation(file_id)
                else:
                    operation(file_id)
        except Exception:
            # Queue durability is an aid to recovery, never a reason to turn
            # a completed local/AI result into a task failure.
            return

    def _file_insight_phase(
        self,
        task: ProcessTask,
        queue: object | None,
        file_id: str,
        source_sha256: str | None,
        phase: str,
        *,
        current_file: str,
        completed: int,
        total: int,
    ) -> None:
        self._queue_update(queue, "set_phase", file_id, source_sha256, phase)
        self._workspace_changed(file_id)
        phase_progress = {"requesting_model": 0.25, "validating": 0.7, "persisting": 0.9}[phase]
        self._set_stage(
            task,
            "ai_file_insight",
            min(0.98, (completed + phase_progress) / max(1, total)),
            current_file=current_file,
            current_file_id=file_id,
            completed=completed,
            total=total,
            current_substage=phase,
            run_id=file_id,
        )

    def _run_file_insight(self, task_id: str, service: FileInsightService) -> None:
        with self._lock:
            task = self._tasks[task_id]
        try:
            queue = getattr(service, "queue", None)
            queue_entry = queue.entry(task.file_insight_id or "") if queue is not None and callable(getattr(queue, "entry", None)) else None
            source_sha256 = str(queue_entry.get("source_sha256") or "") if isinstance(queue_entry, Mapping) else None
            self._queue_update(queue, "mark_running", task.file_insight_id or "", source_sha256)
            self._workspace_changed(task.file_insight_id)
            self._set_stage(
                task,
                "ai_file_insight",
                0.05,
                current_file=task.source,
                current_file_id=task.file_insight_id,
                completed=0,
                total=1,
                current_substage="preparing bounded context",
                run_id=task.file_insight_id,
            )
            provider = task._file_insight_provider
            if provider is None or task.file_insight_id is None:
                raise FileInsightError("FILE_INSIGHT_NOT_CONFIGURED", "AI file understanding is not configured")
            result = _with_file_insight_provider_slot(lambda: service.enrich(
                task.file_insight_id,
                provider,
                force=task.file_insight_force,
                expected_source_sha256=source_sha256,
                cancel_event=task._cancel_event,
                phase_callback=lambda phase: self._file_insight_phase(
                    task,
                    queue,
                    task.file_insight_id or "",
                    source_sha256,
                    phase,
                    current_file=task.source,
                    completed=0,
                    total=1,
                ),
            ))
            if task._cancel_event.is_set() or task.status == "cancelling":
                raise CancellationRequested()
            with self._lock:
                if task.status in {"cancelled", "failed", "interrupted"}:
                    return
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "AI file understanding complete"
                task.completed = 1
                task.total = 1
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = {"providerCalls": int(result.get("provider_calls", 0) or 0), "reused": bool(result.get("reused", False))}
                task.summary = {"fileId": task.file_insight_id, "status": result.get("status"), "reused": bool(result.get("reused", False))}
            self._queue_update(queue, "mark_completed", task.file_insight_id or "", source_sha256)
            self._workspace_changed(task.file_insight_id)
        except CancellationRequested as exc:
            queue = getattr(service, "queue", None)
            cancel_file = getattr(queue, "cancel_file", None)
            if callable(cancel_file):
                cancel_file(task.file_insight_id or "", source_sha256 if "source_sha256" in locals() else None, reason=task.cancel_reason or "user_cancelled")
            self._workspace_changed(task.file_insight_id)
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]
        except Exception as exc:
            queue = getattr(service, "queue", None)
            self._queue_update(
                queue,
                "mark_failed",
                task.file_insight_id or "",
                source_sha256 if "source_sha256" in locals() else None,
                error_code=getattr(exc, "code", type(exc).__name__.upper()),
                error_stage=getattr(exc, "stage", task.current_stage),
            )
            self._workspace_changed(task.file_insight_id)
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "failed"
                    task.current_stage = "failed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]

    def _run_file_insight_batch(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
        service = task._file_insight_service
        queue = getattr(service, "queue", None)
        processed = successful = failed = 0
        counter_lock = threading.Lock()

        def counts() -> dict[str, Any]:
            value = queue.counts() if queue is not None and callable(getattr(queue, "counts", None)) else {}
            value = dict(value)
            with counter_lock:
                completed_local = successful
                failed_local = failed
                processed_local = processed
            value.setdefault("queued", 0)
            value.setdefault("running", 0)
            value["completed"] = max(int(value.get("completed", 0) or 0), completed_local)
            value["failed"] = max(int(value.get("failed", 0) or 0), failed_local)
            value.setdefault("cancelled", 0)
            value["total"] = max(int(value.get("total", 0) or 0), int(task.total or 0), processed_local)
            return value

        def worker() -> None:
            nonlocal processed, successful, failed
            while True:
                if task._cancel_event.is_set() or task.status == "cancelling":
                    queue.cancel_pending(reason=task.cancel_reason or "user_cancelled")
                    raise CancellationRequested()
                item = queue.claim_next()
                if item is None:
                    return
                current_file_id = str(item.get("file_id") or "")
                current_source_sha256 = str(item.get("source_sha256") or "")
                current_file = current_file_id
                catalog = getattr(service, "catalog", None)
                detail_lookup = getattr(catalog, "file_detail", None)
                if callable(detail_lookup):
                    detail = detail_lookup(current_file_id)
                    if isinstance(detail, Mapping):
                        file_meta = detail.get("file") if isinstance(detail.get("file"), Mapping) else {}
                        source_meta = detail.get("source") if isinstance(detail.get("source"), Mapping) else {}
                        current_file = str(file_meta.get("displayName") or source_meta.get("relativePath") or current_file_id)
                with self._lock:
                    task.total = max(int(task.total or 0), int(queue.counts().get("total", 0) or 0), processed + 1)
                    current_total = int(task.total or 0)
                self._set_stage(
                    task,
                    "ai_file_insight",
                    processed / max(1, current_total),
                    current_file=current_file,
                    current_file_id=current_file_id,
                    completed=processed,
                    total=current_total,
                    current_substage="preparing bounded context",
                    run_id=current_file_id,
                )
                try:
                    _with_file_insight_provider_slot(lambda: service.enrich(
                        current_file_id,
                        task._file_insight_provider,
                        expected_source_sha256=current_source_sha256,
                        cancel_event=task._cancel_event,
                        phase_callback=lambda phase, item_id=current_file_id, item_sha=current_source_sha256, item_name=current_file, item_total=current_total: self._file_insight_phase(
                            task,
                            queue,
                            item_id,
                            item_sha,
                            phase,
                            current_file=item_name,
                            completed=processed,
                            total=item_total,
                        ),
                    ))
                    if task._cancel_event.is_set() or task.status == "cancelling":
                        raise CancellationRequested()
                except CancellationRequested:
                    queue.cancel_file(
                        current_file_id,
                        source_sha256=current_source_sha256,
                        reason=task.cancel_reason or "user_cancelled",
                    )
                    self._workspace_changed(current_file_id)
                    queue.cancel_all(reason=task.cancel_reason or "user_cancelled")
                    raise
                except Exception as exc:
                    self._queue_update(
                        queue,
                        "mark_failed",
                        current_file_id,
                        current_source_sha256,
                        error_code=getattr(exc, "code", type(exc).__name__.upper()),
                        error_stage=getattr(exc, "stage", task.current_stage),
                    )
                    self._workspace_changed(current_file_id)
                    with counter_lock:
                        failed += 1
                        processed += 1
                    with self._lock:
                        task.counts = counts()
                        task.current_substage = "file failed; continuing queue"
                        task.error_summary = self._error_for(task, exc)["message"]
                    continue
                queue.mark_completed(current_file_id, source_sha256=current_source_sha256)
                self._workspace_changed(current_file_id)
                with counter_lock:
                    successful += 1
                    processed += 1
                    completed_local = processed
                with self._lock:
                    task.counts = counts()
                self._set_stage(
                    task,
                    "ai_file_insight",
                    completed_local / max(1, int(task.total or 0)),
                    current_file=current_file,
                    current_file_id=current_file_id,
                    completed=completed_local,
                    total=int(task.total or 0),
                    current_substage="AI file understanding complete",
                    run_id=current_file_id,
                )

        try:
            provider = task._file_insight_provider
            if service is None or provider is None or queue is None:
                raise FileInsightError("FILE_INSIGHT_NOT_CONFIGURED", "AI file understanding is not configured")
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dongjian-file-insight-batch") as executor:
                futures = [executor.submit(worker) for _ in range(2)]
                for future in futures:
                    future.result()
            with self._lock:
                if task.status in {"cancelled", "failed", "interrupted"}:
                    return
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "AI file understanding queue complete"
                task.completed = processed
                task.total = max(int(task.total or 0), processed)
                task.counts = counts()
                task.summary = {"queued": task.counts.get("queued", 0), "running": task.counts.get("running", 0), "completed": successful, "failed": failed, "total": task.total}
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
        except CancellationRequested as exc:
            if queue is not None:
                try:
                    queue.cancel_all(reason=task.cancel_reason or "user_cancelled")
                except Exception:
                    pass
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "cancelled"
                    task.current_stage = "cancelled"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.completed = processed
                    task.counts = counts()
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                if task.status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    task.status = "failed"
                    task.current_stage = "failed"
                    task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.completed = processed
                    task.counts = counts()
                    task.error = self._error_for(task, exc)
                    task.error_summary = task.error["message"]
    def _run_analysis(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
        started = time.perf_counter()
        try:
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
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
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id or task.analysis_run_id,
                )

            self._set_stage(
                task,
                "ai_analysis",
                0.01,
                current_file="AI Analysis",
                completed=0,
                total=task.max_steps,
                current_substage="preparing_scope",
                current_step=0,
                max_steps=task.max_steps,
                run_id=task.analysis_run_id,
            )
            runner = task._analysis_runner
            if runner is None:
                raise AnalysisExecutionError("ANALYSIS_FAILED", "AI analysis runner is unavailable")
            result = runner(progress, task._cancel_event)
            if task._cancel_event.is_set() or task.status == "cancelling":
                raise AnalysisExecutionError("ANALYSIS_CANCELLED", "analysis was cancelled", stage="cancelled", retryable=True)
            result_status = str(result.get("status") or "completed") if isinstance(result, Mapping) else "completed"
            steps_used = int(result.get("steps_used", task.current_step)) if isinstance(result, Mapping) else task.current_step
            provider_calls = int(result.get("provider_calls", 0)) if isinstance(result, Mapping) else 0
            with self._lock:
                if task.status in {"cancelled", "failed", "interrupted"}:
                    return
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "completed"
                task.current_step = max(0, steps_used)
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.counts = {"steps": max(0, steps_used), "providerCalls": max(0, provider_calls)}
                task.summary = {
                    "analysisRunId": task.analysis_run_id,
                    "analysisStatus": result_status,
                    "stepsUsed": max(0, steps_used),
                    "maxSteps": task.max_steps,
                    "providerCalls": max(0, provider_calls),
                }
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except (CancellationRequested, AnalysisExecutionError) as exc:
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                task.status = "cancelled" if isinstance(exc, CancellationRequested) or getattr(exc, "code", "") == "ANALYSIS_CANCELLED" else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                task.status = "cancelled" if task._cancel_event.is_set() else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        finally:
            with self._lock:
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
            _ = started

    def _run_report(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
        started = time.perf_counter()
        try:
            def progress(
                stage: str,
                value: float,
                *,
                current_file: str | None = None,
                completed: int | None = None,
                total: int | None = None,
                current_page: int | None = None,
                current_substage: str | None = None,
                current_step: int | None = None,
                max_steps: int | None = None,
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
                    current_step=current_step,
                    max_steps=max_steps,
                    run_id=run_id or task.report_id,
                )

            self._set_stage(
                task,
                "report_generation",
                0.01,
                current_file="Analysis Report",
                completed=0,
                total=1,
                current_substage="loading_analysis",
                current_step=0,
                max_steps=1,
                run_id=task.report_id,
            )
            runner = task._report_runner
            if runner is None:
                raise ReportExecutionError("REPORT_FAILED", "report runner is unavailable")
            result = runner(progress, task._cancel_event)
            if task._cancel_event.is_set() or task.status == "cancelling":
                raise ReportExecutionError("REPORT_CANCELLED", "report generation was cancelled", stage="cancelled", retryable=True)
            with self._lock:
                if task.status in {"cancelled", "failed", "interrupted"}:
                    return
                task.status = "succeeded"
                task.progress = 1.0
                task.current_stage = "completed"
                task.current_substage = "completed"
                task.current_step = 1
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.completed = 1
                task.total = 1
                task.counts = {"reports": 1}
                task.summary = {
                    "reportId": task.report_id,
                    "generationMode": result.get("generation_mode") if isinstance(result, Mapping) else None,
                }
                if task._started_clock is not None:
                    task.elapsed_seconds = max(0.0, time.perf_counter() - task._started_clock)
        except (CancellationRequested, ReportExecutionError) as exc:
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                task.status = "cancelled" if isinstance(exc, CancellationRequested) or getattr(exc, "code", "") == "REPORT_CANCELLED" else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
                task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
                task.error = self._error_for(task, exc)
                task.error_summary = task.error["message"]
        except Exception as exc:
            with self._lock:
                if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return
                task.status = "cancelled" if task._cancel_event.is_set() else "failed"
                task.current_stage = "cancelled" if task.status == "cancelled" else "failed"
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

    def begin_reset(self) -> None:
        """Raise the task-admission barrier for one workspace reset."""

        with self._lock:
            if self._reset_in_progress:
                raise TaskAdmissionError("a workspace reset is already in progress")
            self._reset_in_progress = True

    def end_reset(self) -> None:
        """Release the reset barrier after success or a safe failure."""

        with self._lock:
            self._reset_in_progress = False

    @property
    def reset_in_progress(self) -> bool:
        with self._lock:
            return self._reset_in_progress

    def list(self, *, limit: int = 50) -> list[ProcessTask]:
        with self._lock:
            values = sorted(self._tasks.values(), key=lambda item: item.task_id, reverse=True)
            return values[:limit]

    def file_insight_task(self, file_id: str) -> ProcessTask | None:
        """Find the latest in-memory file insight task without list truncation."""
        with self._lock:
            values = [
                task
                for task in self._tasks.values()
                if (
                    task.task_type == "file_insight" and task.file_insight_id == file_id
                )
                or (
                    task.task_type == "file_insight_batch"
                    and task.current_file_id == file_id
                    and task.status in {"queued", "running", "cancelling"}
                )
            ]
            return max(values, key=lambda item: item.task_id, default=None)

    def file_insight_batch_task(self) -> ProcessTask | None:
        with self._lock:
            values = [
                task
                for task in self._tasks.values()
                if task.task_type == "file_insight_batch"
                and task.status in {"queued", "running", "cancelling"}
            ]
            return max(values, key=lambda item: item.task_id, default=None)

    def clear_finished(self) -> int:
        """Drop terminal in-memory task records after a project reset."""

        terminal = {"succeeded", "failed", "cancelled", "interrupted"}
        with self._lock:
            task_ids = [task_id for task_id, task in self._tasks.items() if task.status in terminal]
            for task_id in task_ids:
                self._tasks.pop(task_id, None)
            return len(task_ids)

    def cancel(self, task_id: str) -> ProcessTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                return task
            task._cancel_event.set()
            future = task._future
            task.cancel_reason = "user_cancelled"
            if task.task_type in {"file_insight", "file_insight_batch"} and task._file_insight_service is not None:
                try:
                    if task.task_type == "file_insight_batch":
                        task._file_insight_service.queue.cancel_all(reason="user_cancelled")
                    else:
                        entry = task._file_insight_service.queue.entry(task.file_insight_id or "")
                        source_sha256 = str(entry.get("source_sha256") or "") if isinstance(entry, Mapping) else None
                        task._file_insight_service.queue.cancel_file(task.file_insight_id or "", source_sha256, reason="user_cancelled")
                except Exception:
                    pass
            if future is not None:
                future.cancel()
            self._mark_cancelled_locked(task, "user_cancelled")
            return task

    def _mark_cancelled_locked(self, task: ProcessTask, reason: str) -> None:
        task._cancel_event.set()
        task.cancel_reason = reason
        task.status = "cancelled"
        task.current_stage = "cancelled"
        task.finished_at = datetime.now().astimezone().replace(tzinfo=None)
        task.updated_at = datetime.now().astimezone().replace(tzinfo=None)
        task.error = self._error_for(task, CancellationRequested())
        task.error_summary = task.error["message"]

    def cancel_for_reset(self) -> None:
        """Request reset cancellation without claiming active workers drained."""

        self.request_shutdown()
        with self._lock:
            for task in self._tasks.values():
                if task.status == "cancelling":
                    task.cancel_reason = "cancelled_by_reset"

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
                        task.updated_at = datetime.now().astimezone().replace(tzinfo=None)
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
                        task.updated_at = datetime.now().astimezone().replace(tzinfo=None)
                elif task.status in {"running", "cancelling"}:
                    task.status = "cancelling"
                    task.current_stage = "cancelling"
                    task._cancel_event.set()
                    task.updated_at = datetime.now().astimezone().replace(tzinfo=None)

    def shutdown(self, *, timeout: float = 5.0) -> bool:
        """Request cancellation and wait a bounded time for the coordinator."""

        self.request_shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._ai_executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                active = [
                    task
                    for task in self._tasks.values()
                    if task.status in {"running", "cancelling"}
                    or (task._future is not None and not task._future.done())
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
                    task.updated_at = datetime.now().astimezone().replace(tzinfo=None)
                    task.error = self._error_for(task, CancellationRequested())
                    task.error_summary = task.error["message"]
        return False
