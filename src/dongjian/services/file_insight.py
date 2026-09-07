"""Bounded, file-level AI understanding kept separate from extraction.

FileInsight is deliberately an artifact, not a replacement for asset-level
SemanticMetadata or AnalysisRun.  The local pipeline can finish without it;
the optional provider is called at most once by this service invocation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import os
import tempfile
import threading
import time
from typing import Any, Callable

from dongjian.semantic.models import SemanticRequest, SemanticResponse, canonical_json, sha256_json
from dongjian.semantic.provider import SemanticProviderError
from dongjian.semantic.settings import load_runtime_ai_settings
from dongjian.cancellation import CancellationRequested, check_cancel

from .catalog import CatalogService
from .table_trust import is_table_trusted_for_analysis


FILE_INSIGHT_PROMPT_VERSION = "file-insight-v2"
FILE_INSIGHT_CONFIG_VERSION = "file-insight-contract-v2"
MAX_FILE_INSIGHT_CONTEXT_BYTES = 96 * 1024
MAX_FILE_INSIGHT_TEXT_CHARS = 6_000
MAX_FILE_INSIGHT_TABLES = 24
MAX_FILE_INSIGHT_EVIDENCE = 64
MAX_FILE_INSIGHT_QUEUE_ITEMS = 10_000
_QUEUE_IO_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


def _safe_id(value: str) -> str:
    return "".join(character for character in value if character.isalnum() or character in {"-", "_"})[:160] or hashlib.sha256(value.encode()).hexdigest()[:24]


def _provider_failure_code(error: SemanticProviderError) -> str:
    code = str(getattr(error, "code", "") or "").casefold()
    if code in {"timeout", "timed_out"}:
        return "PROVIDER_TIMEOUT"
    if code in {"cancelled", "canceled"}:
        return "CANCELLED"
    if code in {"malformed_json", "invalid_json"}:
        return "INVALID_JSON"
    if code.startswith("http_"):
        try:
            status = int(code.split("_", 1)[1])
        except (TypeError, ValueError):
            status = 0
        if status in {401, 403}:
            return "PROVIDER_AUTH"
        if status == 429:
            return "PROVIDER_RATE_LIMIT"
        return "PROVIDER_HTTP_ERROR"
    return "PROVIDER_HTTP_ERROR"


def _http_status_class(error_code: str | None, *, response_received: bool) -> str | None:
    if response_received:
        return "2xx"
    value = str(error_code or "")
    if value.startswith("PROVIDER_AUTH"):
        return "4xx"
    if value == "PROVIDER_RATE_LIMIT" or value == "PROVIDER_HTTP_ERROR":
        return "4xx/5xx"
    return None


class FileInsightStore:
    """Atomic artifact store with source/model/input identity in every record."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.root = self.workspace_root / "artifacts" / "file_insights"

    def _directory(self, file_id: str) -> Path:
        return self.root / _safe_id(file_id)

    def _path(self, file_id: str, source_sha256: str, input_hash: str) -> Path:
        return self._directory(file_id) / f"{source_sha256[:16]}-{input_hash[:32]}.json"

    def current_path(self, file_id: str) -> Path:
        return self._directory(file_id) / "current.json"

    def read_current(self, file_id: str, source_sha256: str | None = None) -> dict[str, Any] | None:
        path = self.current_path(file_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("file_id") != file_id:
            return None
        if source_sha256 and value.get("source_sha256") != source_sha256:
            return None
        return value

    def write(self, record: Mapping[str, Any]) -> Path:
        file_id = str(record.get("file_id") or "")
        source_sha256 = str(record.get("source_sha256") or "")
        input_hash = str(record.get("input_hash") or "")
        if not file_id or len(source_sha256) < 16 or len(input_hash) < 16:
            raise ValueError("file insight identity is invalid")
        directory = self._directory(file_id)
        directory.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(dict(record), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_FILE_INSIGHT_CONTEXT_BYTES * 2:
            raise ValueError("file insight artifact exceeds the local limit")
        target = self._path(file_id, source_sha256, input_hash)
        for path in (target, self.current_path(file_id)):
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=f"{path.name}.", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return target


class FileInsightPolicyStore:
    """Small non-secret policy file for the Settings separation."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = Path(workspace_root).resolve() / "state" / "file-insight-policy.json"

    def enabled(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return bool(value.get("enabled")) if isinstance(value, Mapping) else False

    def save(self, enabled: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix="file-insight-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"version": 1, "enabled": bool(enabled)}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class FileInsightQueueStore:
    """Persist only admitted work, so restart recovery never scans old files."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = Path(workspace_root).resolve() / "state" / "file-insight-queue.json"

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    def _write(self, values: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix="file-insight-queue-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(values[:MAX_FILE_INSIGHT_QUEUE_ITEMS], handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def enqueue(self, file_id: str, source_sha256: str) -> bool:
        return bool(self.enqueue_many([{"file_id": file_id, "source_sha256": source_sha256}]))

    def enqueue_many(self, items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Admit a bounded manifest without creating one Future per file."""

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in items:
            file_id = str(value.get("file_id") or "")
            source_sha256 = str(value.get("source_sha256") or "")
            if not file_id or not source_sha256 or file_id in seen:
                continue
            seen.add(file_id)
            normalized.append(
                {
                    "file_id": file_id,
                    "source_sha256": source_sha256,
                    "status": "queued",
                    "enqueued_at": _now(),
                }
            )
        if not normalized:
            return []
        with _QUEUE_IO_LOCK:
            # Terminal entries describe an earlier batch. Keep every active
            # item for recovery, including an item named by this request. A
            # repeated bulk click must not reset a running/queued item back to
            # the end of the manifest.
            values = [
                item
                for item in self._read()
                if str(item.get("status") or "") in {"queued", "running"}
            ]
            active_keys = {
                (str(item.get("file_id") or ""), str(item.get("source_sha256") or ""))
                for item in values
            }
            admitted: list[dict[str, Any]] = []
            for item in normalized:
                key = (item["file_id"], item["source_sha256"])
                if key in active_keys:
                    continue
                # A queued item for an older content hash can never become a
                # valid current insight. Cancel it before admitting the new
                # identity. A running older item is left to finish, but its
                # identity-specific terminal update cannot touch the new one.
                for existing in values:
                    if (
                        str(existing.get("file_id") or "") == item["file_id"]
                        and str(existing.get("source_sha256") or "") != item["source_sha256"]
                        and str(existing.get("status") or "") == "queued"
                    ):
                        existing["status"] = "cancelled"
                        existing["cancelled_at"] = _now()
                values.append(item)
                active_keys.add(key)
                admitted.append(dict(item))
            self._write(values)
        return admitted

    @staticmethod
    def _matches(item: Mapping[str, Any], file_id: str, source_sha256: str | None) -> bool:
        return (
            str(item.get("file_id") or "") == file_id
            and (source_sha256 is None or str(item.get("source_sha256") or "") == source_sha256)
        )

    def mark_running(self, file_id: str, source_sha256: str | None = None) -> None:
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if self._matches(item, file_id, source_sha256) and str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "running"
                    item["phase"] = "requesting_model"
            self._write(values)

    def set_phase(self, file_id: str, phase: str, source_sha256: str | None = None) -> None:
        if phase not in {"requesting_model", "validating", "persisting"}:
            raise ValueError("file insight phase is invalid")
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if self._matches(item, file_id, source_sha256) and str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "running"
                    item["phase"] = phase
            self._write(values)

    def mark_completed(self, file_id: str, source_sha256: str | None = None) -> None:
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if self._matches(item, file_id, source_sha256) and str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "completed"
                    item["phase"] = "completed"
                    item["completed_at"] = _now()
            self._write(values)

    def mark_failed(
        self,
        file_id: str,
        source_sha256: str | None = None,
        *,
        error_code: str | None = None,
        error_stage: str | None = None,
    ) -> None:
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if self._matches(item, file_id, source_sha256) and str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "failed"
                    item["phase"] = "failed"
                    item["failed_at"] = _now()
                    if error_code:
                        item["error_code"] = str(error_code)[:120]
                    if error_stage:
                        item["error_stage"] = str(error_stage)[:120]
            self._write(values)

    def pending(self) -> list[dict[str, Any]]:
        with _QUEUE_IO_LOCK:
            return [item for item in self._read() if str(item.get("status") or "") in {"queued", "running"}]

    def entries(self) -> list[dict[str, Any]]:
        with _QUEUE_IO_LOCK:
            return [dict(item) for item in self._read()]

    def entry(self, file_id: str) -> dict[str, Any] | None:
        with _QUEUE_IO_LOCK:
            for item in reversed(self._read()):
                if str(item.get("file_id") or "") == file_id:
                    return dict(item)
        return None

    def counts(self) -> dict[str, int]:
        values = self.entries()
        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for item in values:
            status = str(item.get("status") or "")
            if status in counts:
                counts[status] += 1
        counts["total"] = sum(counts.values())
        return counts

    def claim_next(self) -> dict[str, Any] | None:
        """Move one manifest item to running for the bounded AI consumer."""

        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if str(item.get("status") or "") == "queued":
                    item["status"] = "running"
                    item["started_at"] = _now()
                    self._write(values)
                    return dict(item)
        return None

    def recover_running(self) -> int:
        """Return interrupted running work to queued on a process restart."""

        changed = 0
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if str(item.get("status") or "") == "running":
                    item["status"] = "queued"
                    item["recovered_at"] = _now()
                    changed += 1
            if changed:
                self._write(values)
        return changed

    def cancel_pending(self, *, reason: str = "user_cancelled") -> int:
        """Cancel queued work without removing its durable terminal record."""

        changed = 0
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if str(item.get("status") or "") == "queued":
                    item["status"] = "cancelled"
                    item["phase"] = "cancelled"
                    item["cancel_reason"] = reason
                    item["cancelled_at"] = _now()
                    changed += 1
            if changed:
                self._write(values)
        return changed

    def cancel_file(self, file_id: str, source_sha256: str | None = None, *, reason: str = "user_cancelled") -> int:
        changed = 0
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if self._matches(item, file_id, source_sha256) and str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "cancelled"
                    item["phase"] = "cancelled"
                    item["cancel_reason"] = reason
                    item["cancelled_at"] = _now()
                    changed += 1
            if changed:
                self._write(values)
        return changed

    def cancel_all(self, *, reason: str = "cancelled_by_reset") -> int:
        changed = 0
        with _QUEUE_IO_LOCK:
            values = self._read()
            for item in values:
                if str(item.get("status") or "") in {"queued", "running"}:
                    item["status"] = "cancelled"
                    item["phase"] = "cancelled"
                    item["cancel_reason"] = reason
                    item["cancelled_at"] = _now()
                    changed += 1
            if changed:
                self._write(values)
        return changed

    def discard(self, file_id: str, source_sha256: str | None = None) -> None:
        with _QUEUE_IO_LOCK:
            self._write([item for item in self._read() if not self._matches(item, file_id, source_sha256)])


class FileInsightError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, stage: str = "validation") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.stage = stage


def _validate_evidence(value: object, allowed: list[dict[str, Any]], *, file_id: str | None = None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FILE_INSIGHT_EVIDENCE:
        raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight evidence is invalid", stage="evidence")
    allowed_keys = {
        (str(item.get("asset_id") or ""), item.get("page_number"), str(item.get("sheet_name") or ""))
        for item in allowed
    }
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight evidence must be source references", stage="evidence")
        asset_id = str(item.get("asset_id") or item.get("assetId") or "")
        page = item.get("page_number", item.get("pageNumber"))
        sheet = str(item.get("sheet_name", item.get("sheetName")) or "")
        if (asset_id, page, sheet) not in allowed_keys:
            raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight cited evidence outside this file", stage="evidence")
        cited_file = str(item.get("file_id") or item.get("fileId") or "")
        if file_id and cited_file and cited_file != file_id:
            raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight cited another file", stage="evidence")
        bbox = item.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight bbox is invalid", stage="evidence")
            try:
                bbox = [float(value) for value in bbox]
            except (TypeError, ValueError) as exc:
                raise FileInsightError("EVIDENCE_VALIDATION_FAILED", "file insight bbox is invalid", stage="evidence") from exc
        result.append({"file_id": file_id or cited_file, "asset_id": asset_id, "page_number": page, "sheet_name": sheet, "bbox": bbox})
    return result


def validate_file_insight(
    value: object,
    allowed: list[dict[str, Any]],
    *,
    file_id: str | None = None,
    file_type: str = "unknown",
    document_kind: str = "file",
) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FileInsightError("INVALID_JSON", "file insight response was not valid JSON", stage="parse") from exc
    if not isinstance(value, Mapping):
        raise FileInsightError("INVALID_RESPONSE_SHAPE", "file insight response must be a JSON object", stage="shape")
    fields = {
        "file_type", "document_kind", "summary", "important_topics", "key_entities_or_fields",
        "important_metrics", "table_summaries", "date_range", "quality_notes",
        "analysis_suggestions", "evidence_refs", "confidence",
    }
    if set(value) - fields:
        raise FileInsightError("INVALID_RESPONSE_SHAPE", "file insight response contains unsupported fields", stage="shape")
    lists = ("important_topics", "key_entities_or_fields", "important_metrics", "table_summaries", "quality_notes", "analysis_suggestions")
    result: dict[str, Any] = {
        "file_type": _bounded_text(file_type, 120) or "unknown",
        "document_kind": _bounded_text(document_kind, 160) or "file",
        "summary": _bounded_text(value.get("summary"), 4_000),
        "date_range": _bounded_text(value.get("date_range"), 256),
        "confidence": max(0.0, min(1.0, float(value.get("confidence", 0.0) or 0.0))),
    }
    for name in lists:
        raw = value.get(name, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or len(raw) > 32:
            raise FileInsightError("INVALID_RESPONSE_SHAPE", f"{name} is invalid", stage="shape")
        result[name] = [_bounded_text(item, 1_000) for item in raw if _bounded_text(item, 1_000)]
    result["evidence_refs"] = _validate_evidence(value.get("evidence_refs"), allowed, file_id=file_id)
    if not result["summary"]:
        raise FileInsightError("SUMMARY_MISSING", "file insight summary is required", stage="required_fields")
    for name in ("summary", "important_topics", "quality_notes", "analysis_suggestions", "table_summaries"):
        values = [result[name]] if name == "summary" else result[name]
        if any(text and not _contains_cjk(text) for text in values):
            raise FileInsightError("LANGUAGE_VALIDATION_FAILED", f"{name} must be written in Simplified Chinese", stage="language")
    return result


class FileInsightService:
    def __init__(self, *, catalog: CatalogService, workspace_root: Path | str) -> None:
        self.catalog = catalog
        self.workspace_root = Path(workspace_root).resolve()
        self.store = FileInsightStore(self.workspace_root)
        self.queue = FileInsightQueueStore(self.workspace_root)

    def enqueue_file(self, file_id: str, *, force: bool = False) -> bool:
        """Admit one current READY_LOCAL file without uploading its content."""

        detail = self.catalog.file_detail(file_id)
        if detail is None:
            raise FileInsightError("FILE_NOT_FOUND", "file was not found")
        source = detail.get("source") if isinstance(detail.get("source"), Mapping) else {}
        file_meta = detail.get("file") if isinstance(detail.get("file"), Mapping) else {}
        if str(file_meta.get("processingStatus") or "") != "ready":
            raise FileInsightError("FILE_INSIGHT_NOT_READY", "local file processing is not READY_LOCAL")
        source_sha = str(source.get("sha256") or "")
        if not source_sha:
            raise FileInsightError("FILE_INSIGHT_IDENTITY_MISSING", "file content identity is unavailable")
        if not force and self.status(file_id).get("status") == "completed":
            return False
        return self.queue.enqueue(file_id, source_sha)

    def bulk_preview(self) -> dict[str, int]:
        """Count READY_LOCAL work without reading file contents."""

        candidates = cached = active = 0
        offset = 0
        limit = 100
        active_entries = {
            str(item.get("file_id") or ""): item
            for item in self.queue.entries()
            if str(item.get("status") or "") in {"queued", "running"}
        }
        while True:
            page = self.catalog.list_files(limit=limit, offset=offset)
            items = page.get("items", []) if isinstance(page, Mapping) else []
            for item in items:
                if not isinstance(item, Mapping) or str(item.get("processingStatus") or "") != "ready":
                    continue
                file_id = str(item.get("fileId") or "")
                source_sha = str(item.get("sha256") or "")
                if not file_id or not source_sha:
                    continue
                candidates += 1
                active_item = active_entries.get(file_id)
                if active_item and str(active_item.get("source_sha256") or "") == source_sha:
                    active += 1
                else:
                    record = self.store.read_current(file_id, source_sha)
                    if record and record.get("status") == "completed":
                        cached += 1
                    else:
                        continue
            pagination = page.get("pagination", {}) if isinstance(page, Mapping) else {}
            if not bool(pagination.get("hasNext")):
                break
            offset += len(items)
            if not items:
                break
        return {"ready": candidates, "cached": cached, "active": active, "pending": max(0, candidates - cached - active)}

    def enqueue_ready_files(self) -> dict[str, Any]:
        """Admit only current READY_LOCAL files missing a valid insight."""

        items: list[dict[str, str]] = []
        active_entries = {
            str(item.get("file_id") or ""): item
            for item in self.queue.entries()
            if str(item.get("status") or "") in {"queued", "running"}
        }
        cached = active = ready = 0
        offset = 0
        while True:
            page = self.catalog.list_files(limit=100, offset=offset)
            page_items = page.get("items", []) if isinstance(page, Mapping) else []
            for item in page_items:
                if not isinstance(item, Mapping) or str(item.get("processingStatus") or "") != "ready":
                    continue
                file_id = str(item.get("fileId") or "")
                source_sha = str(item.get("sha256") or "")
                if not file_id or not source_sha:
                    continue
                ready += 1
                active_item = active_entries.get(file_id)
                if active_item and str(active_item.get("source_sha256") or "") == source_sha:
                    active += 1
                    continue
                record = self.store.read_current(file_id, source_sha)
                if record and record.get("status") == "completed":
                    cached += 1
                    continue
                items.append({"file_id": file_id, "source_sha256": source_sha})
            pagination = page.get("pagination", {}) if isinstance(page, Mapping) else {}
            if not bool(pagination.get("hasNext")) or not page_items:
                break
            offset += len(page_items)
        admitted = self.queue.enqueue_many(items)
        return {
            "ready": ready,
            "cached": cached,
            "active": active,
            "queued": len(admitted),
            "pending": max(0, ready - cached - active - len(admitted)),
            "fileIds": [str(item.get("file_id") or "") for item in admitted if item.get("file_id")],
        }

    def queue_summary(self) -> dict[str, Any]:
        counts = self.queue.counts()
        preview = self.bulk_preview()
        phase_counts = {"requesting_model": 0, "validating": 0, "persisting": 0}
        for item in self.queue.entries():
            if str(item.get("status") or "") == "running":
                phase = str(item.get("phase") or "")
                if phase in phase_counts:
                    phase_counts[phase] += 1
        return {
            **counts,
            **phase_counts,
            "pendingReady": int(preview.get("pending", 0)),
            "ready": int(preview.get("ready", 0)),
            "cached": int(preview.get("cached", 0)),
            "active": int(preview.get("active", 0)),
        }

    def _context(self, file_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        detail = self.catalog.file_detail(file_id)
        content = self.catalog.file_content(file_id)
        if detail is None or content is None:
            raise FileInsightError("FILE_NOT_FOUND", "file was not found")
        source = detail.get("source") if isinstance(detail.get("source"), Mapping) else {}
        file_meta = detail.get("file") if isinstance(detail.get("file"), Mapping) else {}
        allowed: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        for section in content.get("sections", []) if isinstance(content.get("sections"), list) else []:
            if not isinstance(section, Mapping):
                continue
            bounded_blocks: list[dict[str, Any]] = []
            for block in section.get("blocks", []) if isinstance(section.get("blocks"), list) else []:
                if not isinstance(block, Mapping):
                    continue
                provenance = block.get("provenance") if isinstance(block.get("provenance"), Mapping) else {}
                asset_id = str(block.get("assetId") or provenance.get("assetId") or "")
                page = block.get("pageNumber", provenance.get("pageNumber"))
                sheet = block.get("sheetName", provenance.get("sheetName"))
                if block.get("type") == "text":
                    allowed.append({"asset_id": asset_id, "page_number": page, "sheet_name": sheet})
                    bounded_blocks.append({"kind": "text", "asset_id": asset_id, "page_number": page, "sheet_name": sheet, "text": _bounded_text(block.get("text"), 1_500)})
                elif block.get("type") == "table" and len([item for item in bounded_blocks if item.get("kind") == "table"]) < MAX_FILE_INSIGHT_TABLES:
                    if not is_table_trusted_for_analysis(block):
                        bounded_blocks.append(
                            {
                                "kind": "table_candidate",
                                "asset_id": asset_id,
                                "page_number": page,
                                "sheet_name": sheet,
                                "note": "candidate table; verify against the source before drawing conclusions",
                            }
                        )
                        continue
                    allowed.append({"asset_id": asset_id, "page_number": page, "sheet_name": sheet})
                    preview = block.get("preview") if isinstance(block.get("preview"), Mapping) else {}
                    raw_columns = [str(item) for item in preview.get("columns", [])] if isinstance(preview.get("columns"), list) else []
                    display_columns = [str(item) for item in preview.get("presentationColumns", [])] if isinstance(preview.get("presentationColumns"), list) else []
                    insight_columns = display_columns if len(display_columns) == len(raw_columns) else raw_columns
                    raw_rows = preview.get("rows", []) if isinstance(preview.get("rows"), list) else []
                    insight_rows = [
                        {insight_columns[index]: row.get(raw_columns[index]) for index in range(len(raw_columns))}
                        for row in raw_rows[:5]
                        if isinstance(row, Mapping)
                    ] if insight_columns else []
                    bounded_blocks.append({"kind": "table", "asset_id": asset_id, "page_number": page, "sheet_name": sheet, "columns": insight_columns[:32], "sample_rows": insight_rows})
            if bounded_blocks:
                sections.append({"section_id": section.get("sectionId"), "kind": section.get("kind"), "label": section.get("label"), "blocks": bounded_blocks})
        dimensions = dict(file_meta)
        dimensions.pop("fileInsightStatus", None)
        context = {
            "file": {
                "file_id": file_id,
                "relative_path": source.get("relativePath"),
                "format": source.get("format"),
                "sha256": source.get("sha256"),
                "dimensions": dimensions,
            },
            "sections": sections[:32],
            "asset_counts": {"text": file_meta.get("textAssets"), "tables": file_meta.get("tableAssets")},
        }
        encoded = canonical_json(context).encode("utf-8")
        if len(encoded) > MAX_FILE_INSIGHT_CONTEXT_BYTES:
            context["sections"] = sections[:8]
            encoded = canonical_json(context).encode("utf-8")
        return context, allowed[:MAX_FILE_INSIGHT_EVIDENCE], str(source.get("sha256") or "")

    def status(self, file_id: str) -> dict[str, Any]:
        detail = self.catalog.file_detail(file_id)
        if detail is None:
            return {"fileId": file_id, "status": "not_found"}
        source = detail.get("source") if isinstance(detail.get("source"), Mapping) else {}
        file_meta = detail.get("file") if isinstance(detail.get("file"), Mapping) else {}
        source_sha = str(source.get("sha256") or "")
        record = self.store.read_current(file_id, source_sha)
        queue_entry = self.queue.entry(file_id)
        queue_status = str(queue_entry.get("status") or "") if queue_entry else ""
        if str(file_meta.get("processingStatus") or "") == "no_evidence":
            status = "no_evidence"
        elif queue_status == "queued":
            status = "queued"
        elif queue_status == "running":
            phase = str(queue_entry.get("phase") or "requesting_model") if queue_entry else "requesting_model"
            status = phase if phase in {"requesting_model", "validating", "persisting"} else "requesting_model"
        elif queue_status == "failed" or str(record.get("status") if record else "") == "failed":
            status = "failed"
        elif str(record.get("status") if record else "") == "completed":
            status = "completed"
        else:
            status = "not_started"
        return {"fileId": file_id, "status": status, "insight": record.get("insight") if record else None, "metadata": {key: record.get(key) for key in ("source_sha256", "model", "prompt_version", "input_hash", "created_at", "provider_calls") } if record else None, "queue": queue_entry}

    def _save_failure(
        self,
        *,
        file_id: str,
        source_sha: str,
        model: str,
        input_hash: str,
        code: str,
        telemetry: Mapping[str, Any],
    ) -> None:
        try:
            self.store.write({
                "schema_version": "file-insight-v1",
                "file_id": file_id,
                "source_sha256": source_sha,
                "model": model,
                "prompt_version": FILE_INSIGHT_PROMPT_VERSION,
                "input_hash": input_hash,
                "created_at": _now(),
                "status": "failed",
                "provider_calls": 1,
                "reused": False,
                "error_code": code,
                **dict(telemetry),
                "insight": None,
            })
        except Exception:
            # A failed status is useful, but never hide the provider/protocol
            # error merely because its diagnostic artifact could not be written.
            return

    def enrich(
        self,
        file_id: str,
        provider: object,
        *,
        force: bool = False,
        expected_source_sha256: str | None = None,
        phase_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        context, allowed, source_sha = self._context(file_id)
        if not source_sha:
            raise FileInsightError("FILE_INSIGHT_IDENTITY_MISSING", "file content identity is unavailable")
        if expected_source_sha256 and source_sha != expected_source_sha256:
            raise FileInsightError("FILE_INSIGHT_STALE", "file content changed before AI file understanding started")
        model = str(getattr(provider, "model", "file-insight-model"))[:200]
        source_format = _bounded_text(context.get("file", {}).get("format"), 32).lower()
        if source_format in {"csv", "tsv", "xls", "xlsx"}:
            document_kind = "structured_table"
        elif source_format in {"jpg", "jpeg", "png"}:
            document_kind = "image"
        else:
            document_kind = "document"
        context_identity = sha256_json({"source_sha256": source_sha, "context": context, "model": model, "prompt_version": FILE_INSIGHT_PROMPT_VERSION})
        request = SemanticRequest(
            asset_id=file_id,
            asset_type="text",
            model=model,
            prompt_version=FILE_INSIGHT_PROMPT_VERSION,
            config_version=FILE_INSIGHT_CONFIG_VERSION,
            normalized_artifact_identity=context_identity,
            instructions=(
                "仅总结应用提供的 DongJian 文件内容，不要编造证据或标识符。"
                "所有用户可见文字必须使用简体中文。"
            ),
            reference_data=context,
            output_contract=(
                "Return a JSON object with only these fields: summary (required and non-empty), "
                "important_topics, key_entities_or_fields, quality_notes, analysis_suggestions, "
                "and optional evidence_refs. The narrative fields may be a string or an array "
                "of strings. Include evidence_refs only when you can provide source-reference "
                "objects with the exact asset_id/page_number/sheet_name values present in the "
                "reference data; otherwise omit evidence_refs. Never put prose, excerpts, or "
                "plain strings in evidence_refs. Do not return file type, document kind, asset "
                "IDs, source identity, or counts; the local application supplies those values. "
                "All narrative values must be Simplified Chinese."
            ),
        )
        input_hash = request.input_hash
        cached = None if force else self.store.read_current(file_id, source_sha)
        if cached and cached.get("input_hash") == input_hash and cached.get("status") == "completed":
            cached["reused"] = True
            cached["provider_calls"] = 0
            return cached
        provider_kind = _bounded_text(getattr(provider, "name", type(provider).__name__), 120)
        provider_call_count = getattr(provider, "call_count", None)
        request_bytes_count = getattr(provider, "request_payload_bytes", None)
        response_bytes_count = getattr(provider, "response_payload_bytes", None)
        telemetry: dict[str, Any] = {
            "provider_called": False,
            "provider_kind": provider_kind,
            "model": model,
            "duration_ms": 0.0,
            "http_status_class": None,
            "response_received": False,
            "response_bytes": None,
            "parse_stage": "not_started",
            "validation_stage": "not_started",
            "failure_code": None,
            "retry_count": 0,
        }
        started = time.perf_counter()

        def finish_telemetry(failure_code: str | None = None) -> None:
            telemetry["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            current_calls = getattr(provider, "call_count", None)
            if isinstance(provider_call_count, int) and isinstance(current_calls, int):
                calls = max(0, current_calls - provider_call_count)
            else:
                calls = 1 if telemetry["provider_called"] else 0
            telemetry["provider_calls"] = calls
            telemetry["retry_count"] = max(0, calls - 1)
            if failure_code:
                telemetry["failure_code"] = failure_code
                telemetry["http_status_class"] = _http_status_class(
                    failure_code,
                    response_received=bool(telemetry["response_received"]),
                )
        try:
            check_cancel(cancel_event)
            if phase_callback is not None:
                phase_callback("requesting_model")
            telemetry["provider_called"] = True
            cancellable = getattr(provider, "generate_cancellable", None)
            if callable(cancellable) and cancel_event is not None:
                response = cancellable(request, cancel_event)
            else:
                response = provider.generate(request)
            telemetry["response_received"] = response is not None
            telemetry["response_bytes"] = getattr(response, "raw_size_bytes", None)
            telemetry["parse_stage"] = "complete" if isinstance(response, SemanticResponse) else "response_shape"
            check_cancel(cancel_event)
            if not isinstance(response, SemanticResponse):
                raise FileInsightError("INVALID_RESPONSE_SHAPE", "provider returned an invalid response", stage="shape")
            if phase_callback is not None:
                phase_callback("validating")
            insight = validate_file_insight(
                response.payload,
                allowed,
                file_id=file_id,
                file_type=source_format,
                document_kind=document_kind,
            )
            telemetry["validation_stage"] = "complete"
            check_cancel(cancel_event)
        except CancellationRequested:
            raise
        except FileInsightError as exc:
            finish_telemetry(exc.code)
            telemetry["validation_stage"] = getattr(exc, "stage", telemetry["validation_stage"])
            self._save_failure(file_id=file_id, source_sha=source_sha, model=model, input_hash=input_hash, code=exc.code, telemetry=telemetry)
            raise
        except SemanticProviderError as exc:
            code = _provider_failure_code(exc)
            if code == "CANCELLED":
                raise CancellationRequested() from exc
            finish_telemetry(code)
            telemetry["parse_stage"] = "provider"
            self._save_failure(file_id=file_id, source_sha=source_sha, model=model, input_hash=input_hash, code=code, telemetry=telemetry)
            raise FileInsightError(code, "AI file understanding was unavailable", retryable=bool(exc.retryable), stage="provider") from exc
        except Exception as exc:
            finish_telemetry("INTERNAL_ERROR")
            telemetry["parse_stage"] = "provider"
            self._save_failure(file_id=file_id, source_sha=source_sha, model=model, input_hash=input_hash, code="INTERNAL_ERROR", telemetry=telemetry)
            raise FileInsightError("INTERNAL_ERROR", "AI file understanding failed", retryable=False, stage="internal") from exc
        finish_telemetry()
        record = {
            "schema_version": "file-insight-v1",
            "file_id": file_id,
            "source_sha256": source_sha,
            "model": model,
            "prompt_version": FILE_INSIGHT_PROMPT_VERSION,
            "input_hash": request.input_hash,
            "created_at": _now(),
            "status": "completed",
            "provider_calls": 1,
            "reused": False,
            **telemetry,
            "insight": insight,
        }
        if phase_callback is not None:
            phase_callback("persisting")
        check_cancel(cancel_event)
        self.store.write(record)
        return record


def configured_file_insight_provider(project_root: Path | str, override: object | None = None) -> object | None:
    if override is not None:
        return override
    try:
        runtime = load_runtime_ai_settings(project_root)
        if not runtime.configured or not runtime.enabled:
            return None
        from dongjian.semantic.runner import provider_for_name

        # FileInsight owns a strict per-file budget: one normal request and at
        # most one bounded retry for a transient 429/5xx/timeout.
        config = replace(runtime.config, max_retries=min(1, runtime.config.max_retries))
        return provider_for_name("openai-compatible", config, allow_real_provider=True)
    except Exception:
        return None
