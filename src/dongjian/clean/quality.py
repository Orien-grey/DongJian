"""Deterministic quality status and issue signals for the catalog."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from dongjian.assets import QualityIssue, QualityIssueSeverity, QualityIssueStatus
from dongjian import paths


def _issue(
    *,
    candidate: Mapping[str, Any],
    cleaning_identity: str,
    issue_type: str,
    evidence: Mapping[str, Any],
    severity: QualityIssueSeverity = QualityIssueSeverity.WARNING,
) -> QualityIssue:
    identity = json.dumps(
        ["phase6", candidate.get("asset_id"), candidate.get("content_sha256"), cleaning_identity, issue_type, evidence],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return QualityIssue(
        issue_id=f"issue_{hashlib.sha256(identity).hexdigest()[:32]}",
        asset_id=str(candidate["asset_id"]),
        severity=severity,
        issue_type=issue_type,
        description=issue_type.replace("_", " "),
        evidence=dict(evidence),
        detected_by=f"phase6-quality:{paths.PROFILE_CONFIG_VERSION}",
        suggested_action="Review the raw and normalized artifacts before semantic enrichment.",
        status=QualityIssueStatus.OPEN,
    )


def _warning_values(profile: Mapping[str, Any]) -> set[str]:
    values = profile.get("source_quality_warnings") or []
    return {str(item) for item in values}


def assess_table_quality(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    cleaning_identity: str,
) -> tuple[str, list[QualityIssue]]:
    issues: list[QualityIssue] = []
    evidence_base = {
        "source_relative_path": candidate.get("source_relative_path"),
        "source_format": candidate.get("business_format"),
        "extractor": candidate.get("extractor"),
        "source_kind": candidate.get("source_kind"),
        "row_count": profile.get("row_count"),
        "column_count": profile.get("column_count"),
    }
    row_count = int(profile.get("row_count") or 0)
    column_count = int(profile.get("column_count") or 0)
    if row_count == 0 or column_count == 0:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="empty_table",
                evidence={**evidence_base, "reason": "no_rows_or_columns"},
                severity=QualityIssueSeverity.ERROR,
            )
        )
        return "unusable", issues

    if not bool(profile.get("provenance_complete")):
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="incomplete_table_provenance",
                evidence=evidence_base,
            )
        )

    warnings = _warning_values(profile)
    review_source = (
        str(candidate.get("business_format") or "").casefold() == "pdf"
        or str(candidate.get("source_kind") or "").casefold() in {"image", "page"}
        or "ocr" in str(candidate.get("extractor") or "").casefold()
        or "img2table" in str(candidate.get("extractor") or "").casefold()
    )
    for warning in sorted(warnings):
        if warning in {"duplicate_column_names", "possible_multirow_header", "possible_title_row", "irregular_row_width", "possible_table_structure_loss", "possible_column_shift", "possible_header_loss", "possible_merged_cells", "image_table_extractor_error"}:
            issues.append(
                _issue(
                    candidate=candidate,
                    cleaning_identity=cleaning_identity,
                    issue_type=warning,
                    evidence={**evidence_base, "extraction_warning": warning},
                )
            )

    empty_ratio = float(profile.get("empty_cell_ratio") or 0.0)
    long_text_ratio = float(profile.get("long_text_cell_ratio") or 0.0)
    if empty_ratio >= 0.5 and row_count * column_count >= 4:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="sparse_ocr" if review_source else "sparse_table",
                evidence={**evidence_base, "empty_cell_ratio": empty_ratio},
            )
        )
    if long_text_ratio >= 0.5 and review_source:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="possible_table_structure_loss",
                evidence={**evidence_base, "long_text_cell_ratio": long_text_ratio},
            )
        )
    if bool(profile.get("irregular_row_width")):
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="irregular_row_width",
                evidence=evidence_base,
            )
        )
    if review_source and column_count == 1:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="suspicious_single_column",
                evidence=evidence_base,
            )
        )
    if review_source and row_count == 1:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="suspicious_single_row",
                evidence=evidence_base,
            )
        )
    confidence = profile.get("ocr_mean_confidence")
    if confidence is not None and float(confidence) < 0.65:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="low_ocr_confidence",
                evidence={**evidence_base, "ocr_mean_confidence": float(confidence)},
            )
        )
    return ("needs_review" if issues or review_source else "ready"), issues


def assess_text_quality(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    cleaning_identity: str,
) -> tuple[str, list[QualityIssue]]:
    issues: list[QualityIssue] = []
    evidence = {
        "source_relative_path": candidate.get("source_relative_path"),
        "source_format": candidate.get("business_format"),
        "extractor": candidate.get("extractor"),
        "source_kind": candidate.get("source_kind"),
        "char_count": profile.get("char_count"),
    }
    if bool(profile.get("empty_content")):
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="empty_text",
                evidence=evidence,
                severity=QualityIssueSeverity.ERROR,
            )
        )
        return "unusable", issues
    extractor = str(candidate.get("extractor") or "").casefold()
    ocr_source = "ocr" in extractor
    if not bool(profile.get("provenance_complete")):
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="incomplete_text_provenance",
                evidence=evidence,
            )
        )
    if bool(profile.get("low_content")):
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="low_content",
                evidence=evidence,
            )
        )
    confidence = profile.get("ocr_mean_confidence")
    if confidence is not None and float(confidence) < 0.65:
        issues.append(
            _issue(
                candidate=candidate,
                cleaning_identity=cleaning_identity,
                issue_type="low_ocr_confidence",
                evidence={**evidence, "ocr_mean_confidence": float(confidence)},
            )
        )
    return ("needs_review" if issues or ocr_source else "ready"), issues


def cleaning_failure_issue(
    candidate: Mapping[str, Any],
    *,
    cleaning_identity: str,
    error_category: str | None,
    error_message: str | None,
) -> QualityIssue:
    return _issue(
        candidate=candidate,
        cleaning_identity=cleaning_identity,
        issue_type="cleaning_failed",
        evidence={"error_category": error_category, "error_message": error_message},
        severity=QualityIssueSeverity.ERROR,
    )
