"""Native PDF text extraction and page-fact collection with PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import time
from typing import Any, Callable

try:
    import pymupdf
except ImportError:  # Keep ``dongjian doctor`` usable before provisioning.
    pymupdf = None  # type: ignore[assignment]

from dongjian import paths
from dongjian.assets import (
    BoundingBox,
    ChunkProvenance,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SourceKind,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_text_asset_id,
    utc_now,
)

from ..models import StructuredSource
from .artifacts import profile_target, workspace_relative, write_json_atomic, write_text_asset
from .blocks import chunk_text, extract_text_blocks, image_info_bboxes, normalize_text
from .profiling import (
    TABLE_CANDIDATE_SEMANTICS,
    PDFProfile,
    PageProfile,
    build_pdf_profile,
    profile_page,
)


EXTRACTOR_NAME = "pymupdf-native-text"
try:
    EXTRACTOR_VERSION = version("pymupdf")
except PackageNotFoundError:
    EXTRACTOR_VERSION = paths.PYMUPDF_VERSION


def _require_pymupdf():
    if pymupdf is None:
        raise RuntimeError(
            "PyMuPDF is not provisioned in runtime\\packages; run the project-local bootstrap before PDF extraction"
        )
    return pymupdf


@dataclass
class PdfStageTimings:
    text_extraction_ms: float = 0.0
    profiling_ms: float = 0.0
    artifact_write_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "text_extraction_ms": self.text_extraction_ms,
            "profiling_ms": self.profiling_ms,
            "artifact_write_ms": self.artifact_write_ms,
        }


@dataclass
class PdfExtractionResult:
    source: StructuredSource
    extraction_run_id: str
    extraction_identity: str
    extractor: str = EXTRACTOR_NAME
    extractor_version: str = EXTRACTOR_VERSION
    status: str = "successful"
    text_assets: list[TextAsset] = field(default_factory=list)
    text_chunks: list[TextChunk] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    profile: PDFProfile | None = None
    profile_artifact_path: str | None = None
    timings: PdfStageTimings = field(default_factory=PdfStageTimings)
    error_category: str | None = None
    error_message: str | None = None

    @property
    def total_chars(self) -> int:
        return sum(len(asset.text) for asset in self.text_assets)


def _drawing_line_count(drawings: Any) -> int:
    count = 0
    if not isinstance(drawings, list):
        return count
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        rect = drawing.get("rect")
        try:
            width = float(rect.width)
            height = float(rect.height)
        except (AttributeError, TypeError, ValueError):
            continue
        if min(abs(width), abs(height)) <= 2.0 and max(abs(width), abs(height)) >= 4.0:
            count += 1
    return count


def _drawing_count(page: Any) -> tuple[int, int]:
    try:
        drawings = page.get_drawings()
    except Exception:  # pragma: no cover - depends on malformed page content
        return 0, 0
    return len(drawings) if isinstance(drawings, list) else 0, _drawing_line_count(drawings)


def _issue(
    *,
    source: StructuredSource,
    asset_id: str,
    issue_type: str,
    evidence: dict[str, Any],
    severity: QualityIssueSeverity = QualityIssueSeverity.WARNING,
) -> QualityIssue:
    identity = json.dumps(
        [source.file_id, source.content_sha256, asset_id, issue_type, evidence],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    action = {
        "suspected_scanned_page": "Queue the page for the Phase 5B OCR/image route.",
        "no_native_text_layer": "Verify whether the PDF is image-only before OCR routing.",
        "mixed_pdf_evidence": "Review native-text and scanned pages separately.",
        "possible_table_candidate": "Use only as a weak routing hint; verify with a table extractor before accepting a TableAsset.",
        "empty_pdf": "Verify the source PDF has pages and is not a placeholder export.",
    }.get(issue_type, "Review the PDF profile before escalating the route.")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=asset_id,
        severity=severity,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=evidence,
        detected_by="pymupdf-profile-v1",
        suggested_action=action,
        status=QualityIssueStatus.OPEN,
    )


def _metadata_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _ordered_page_blocks(blocks: list[Any]) -> list[Any]:
    """Return blocks in a stable top-to-bottom, left-to-right reading order."""

    return sorted(blocks, key=lambda block: (round(block.bbox.y0, 2), round(block.bbox.x0, 2), block.block_index))


def _page_bbox(blocks: list[Any]) -> BoundingBox | None:
    if not blocks:
        return None
    return BoundingBox(
        min(block.bbox.x0 for block in blocks),
        min(block.bbox.y0 for block in blocks),
        max(block.bbox.x1 for block in blocks),
        max(block.bbox.y1 for block in blocks),
    )


def extract_pdf_file(
    source: StructuredSource,
    extraction_run_id: str,
    extraction_identity: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PdfExtractionResult:
    result = PdfExtractionResult(
        source=source,
        extraction_run_id=extraction_run_id,
        extraction_identity=extraction_identity,
    )
    page_profiles: list[PageProfile] = []
    page_asset_ids: dict[int, list[str]] = {}
    started = time.perf_counter_ns()
    try:
        document = _require_pymupdf().open(str(source.path))
        try:
            metadata = {
                str(key): _metadata_value(value)
                for key, value in (document.metadata or {}).items()
            }
            for page_index in range(document.page_count):
                page_number = page_index + 1
                if progress_callback is not None:
                    progress_callback(page_number, document.page_count)
                page = document[page_index]
                extraction_started = time.perf_counter_ns()
                # TEXTFLAGS_TEXT avoids copying embedded image bytes into the
                # block dictionary. Placement geometry is collected separately
                # through get_image_info for bounded profile memory.
                page_dict = page.get_text("dict", flags=_require_pymupdf().TEXTFLAGS_TEXT)
                blocks = extract_text_blocks(page_dict)
                result.timings.text_extraction_ms += (
                    time.perf_counter_ns() - extraction_started
                ) / 1_000_000

                image_boxes = image_info_bboxes(page)
                try:
                    image_count = len(page.get_images(full=True))
                except Exception:  # pragma: no cover - malformed image object
                    image_count = len(image_boxes)
                image_count = max(image_count, len(image_boxes))
                drawing_count, grid_line_count = _drawing_count(page)

                profiling_started = time.perf_counter_ns()
                page_profile = profile_page(
                    page_number=page_number,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    rotation=int(page.rotation or 0),
                    blocks=blocks,
                    image_boxes=image_boxes,
                    image_count=image_count,
                    drawing_count=drawing_count,
                    grid_line_count=grid_line_count,
                )
                page_profiles.append(page_profile)
                result.timings.profiling_ms += (
                    time.perf_counter_ns() - profiling_started
                ) / 1_000_000

                ordered_blocks = _ordered_page_blocks(blocks)
                if not ordered_blocks:
                    continue
                normalized_blocks = [block.normalized_text for block in ordered_blocks]
                raw_text = "\n\n".join(block.raw_text for block in ordered_blocks)
                normalized = "\n\n".join(normalized_blocks)
                block_metadata: list[dict[str, Any]] = []
                normalized_offset = 0
                for block, block_normalized in zip(ordered_blocks, normalized_blocks):
                    block_metadata.append(
                        {
                            "block_index": block.block_index,
                            "bbox": block.bbox.__dict__,
                            "raw_text": block.raw_text,
                            "normalized_text": block_normalized,
                            "normalized_char_start": normalized_offset,
                            "normalized_char_end": normalized_offset + len(block_normalized),
                        }
                    )
                    normalized_offset += len(block_normalized) + 2
                source_locator = f"page:{page_number}:config:{paths.PDF_CONFIG_VERSION}"
                text_asset_id = make_text_asset_id(
                    file_id=source.file_id,
                    content_sha256=source.content_sha256,
                    extractor=result.extractor,
                    extractor_version=result.extractor_version,
                    source_kind=SourceKind.PAGE,
                    source_locator=source_locator,
                    asset_index=page_number - 1,
                )
                artifact_started = time.perf_counter_ns()
                target_metadata = {
                    "contract_version": paths.PDF_CONFIG_VERSION,
                    "file_id": source.file_id,
                    "content_sha256": source.content_sha256,
                    "source_relative_path": source.relative_path,
                    "extraction_run_id": extraction_run_id,
                    "extractor": result.extractor,
                    "extractor_version": result.extractor_version,
                    "source_kind": SourceKind.PAGE.value,
                    "page_number": page_number,
                    "page_width": page_profile.width,
                    "page_height": page_profile.height,
                    "page_rotation": page_profile.rotation,
                    "page_image_count": page_profile.image_count,
                    "page_text_block_count": page_profile.text_block_count,
                    "page_reason_codes": list(page_profile.reason_codes),
                    "block_count": len(ordered_blocks),
                    "blocks": block_metadata,
                    "chunk_config_version": paths.TEXT_CHUNK_CONFIG_VERSION,
                    "offset_basis": "normalized_text",
                }
                paths_written = write_text_asset(
                    workspace_root=source.workspace_root,
                    text_asset_id=text_asset_id,
                    raw_text=raw_text,
                    normalized_text=normalized,
                    metadata=target_metadata,
                )
                result.timings.artifact_write_ms += (
                    time.perf_counter_ns() - artifact_started
                ) / 1_000_000
                asset = TextAsset(
                    text_asset_id=text_asset_id,
                    file_id=source.file_id,
                    content_sha256=source.content_sha256,
                    extraction_run_id=extraction_run_id,
                    extractor=result.extractor,
                    extractor_version=result.extractor_version,
                    source_kind=SourceKind.PAGE,
                    page_number=page_number,
                    section=f"page-{page_number}",
                    bbox=_page_bbox(ordered_blocks),
                    text=normalized,
                    language=None,
                    created_at=utc_now(),
                    source_relative_path=source.relative_path,
                    raw_artifact_path=paths_written["raw"],
                    normalized_artifact_path=paths_written["normalized"],
                    metadata_artifact_path=paths_written["metadata"],
                )
                result.text_assets.append(asset)
                page_asset_ids.setdefault(page_number, []).append(text_asset_id)
                for chunk in chunk_text(normalized):
                    provenance = ChunkProvenance(
                        file_id=source.file_id,
                        content_sha256=source.content_sha256,
                        text_asset_id=text_asset_id,
                        extraction_run_id=extraction_run_id,
                        extractor=result.extractor,
                        extractor_version=result.extractor_version,
                        source_kind=SourceKind.PAGE,
                        page_number=page_number,
                        section=asset.section,
                    )
                    result.text_chunks.append(
                        TextChunk(
                            chunk_id=make_chunk_id(
                                text_asset_id=text_asset_id,
                                chunk_index=chunk.chunk_index,
                                char_start=chunk.char_start,
                                char_end=chunk.char_end,
                                chunk_config_version=paths.TEXT_CHUNK_CONFIG_VERSION,
                            ),
                            text_asset_id=text_asset_id,
                            file_id=source.file_id,
                            chunk_index=chunk.chunk_index,
                            text=chunk.text,
                            char_start=chunk.char_start,
                            char_end=chunk.char_end,
                            provenance=provenance,
                        )
                    )
            profile = build_pdf_profile(
                pages=page_profiles,
                metadata=metadata,
                elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000,
            )
            result.profile = profile
            profile_path = profile_target(
                source.workspace_root, source.file_id, source.content_sha256
            )
            profile_payload = profile.as_dict()
            profile_payload.update(
                {
                    "file_id": source.file_id,
                    "content_sha256": source.content_sha256,
                    "source_relative_path": source.relative_path,
                    "extraction_run_id": extraction_run_id,
                    "extractor": result.extractor,
                    "extractor_version": result.extractor_version,
                    "source_kind": "pdf",
                }
            )
            artifact_started = time.perf_counter_ns()
            write_json_atomic(profile_path, profile_payload)
            result.timings.artifact_write_ms += (
                time.perf_counter_ns() - artifact_started
            ) / 1_000_000
            result.profile_artifact_path = workspace_relative(
                profile_path, source.workspace_root
            )
            result.warnings.append(
                {
                    "classification": profile.classification,
                    "reason_codes": list(profile.reason_codes),
                    "profile_artifact_path": result.profile_artifact_path,
                }
            )
            for page in profile.pages:
                page_asset_id = (page_asset_ids.get(page.page_number) or [
                    f"pdf_page:{source.file_id}:{page.page_number}"
                ])[0]
                if page.suspected_scanned:
                    result.issues.append(
                        _issue(
                            source=source,
                            asset_id=page_asset_id,
                            issue_type="suspected_scanned_page",
                            evidence={
                                "page_number": page.page_number,
                                "effective_chars": page.effective_chars,
                                "image_count": page.image_count,
                                "image_area_coverage_ratio": page.image_area_coverage_ratio,
                                "reason_codes": list(page.reason_codes),
                            },
                        )
                    )
                if page.possible_table_candidate:
                    result.issues.append(
                        _issue(
                            source=source,
                            asset_id=page_asset_id,
                            issue_type="possible_table_candidate",
                            evidence={
                                "page_number": page.page_number,
                                "reason_codes": list(page.reason_codes),
                                "text_block_count": page.text_block_count,
                                "drawing_count": page.drawing_count,
                                "grid_line_count": page.grid_line_count,
                                "semantics": TABLE_CANDIDATE_SEMANTICS,
                            },
                        )
                    )
            if profile.classification == "unknown":
                issue_type = "empty_pdf" if profile.page_count == 0 else "no_native_text_layer"
                result.issues.append(
                    _issue(
                        source=source,
                        asset_id=f"pdf:{source.file_id}",
                        issue_type=issue_type,
                        evidence={
                            "page_count": profile.page_count,
                            "image_count": profile.image_count,
                            "reason_codes": list(profile.reason_codes),
                        },
                    )
                )
            elif profile.classification == "mixed":
                result.issues.append(
                    _issue(
                        source=source,
                        asset_id=f"pdf:{source.file_id}",
                        issue_type="mixed_pdf_evidence",
                        evidence={
                            "pages_with_text": list(profile.pages_with_text),
                            "pages_without_text": list(profile.pages_without_text),
                            "suspected_scanned_pages": list(profile.suspected_scanned_pages),
                        },
                    )
                )
            result.status = "partial" if result.issues else "successful"
        finally:
            document.close()
    except Exception as exc:  # isolate corrupt or unsupported PDF internals
        result.status = "failed"
        result.text_assets.clear()
        result.text_chunks.clear()
        result.issues.clear()
        result.profile = None
        result.profile_artifact_path = None
        result.error_category = (
            "missing_dependency" if pymupdf is None else "corrupt_pdf"
        )
        result.error_message = str(exc)
    return result
