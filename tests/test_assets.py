from __future__ import annotations

from dataclasses import fields

import pytest

from chongzu.assets import (
    AssetQualityStatus,
    AssetType,
    ChunkProvenance,
    FileAssetSet,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueStatus,
    SemanticMetadata,
    SourceKind,
    TableAsset,
    TextAsset,
    TextChunk,
    make_chunk_id,
    make_table_id,
    make_text_asset_id,
    utc_now,
)


CONTENT_SHA256 = "a" * 64
FILE_ID = "file-1"
RUN_ID = "extract-run-1"


def _table(index: int) -> TableAsset:
    return TableAsset(
        table_id=make_table_id(
            file_id=FILE_ID,
            content_sha256=CONTENT_SHA256,
            extractor="synthetic",
            extractor_version="1",
            source_kind=SourceKind.PAGE,
            source_locator=f"page:1/table:{index}",
            asset_index=index,
        ),
        file_id=FILE_ID,
        content_sha256=CONTENT_SHA256,
        extraction_run_id=RUN_ID,
        extractor="synthetic",
        extractor_version="1",
        source_kind=SourceKind.PAGE,
        sheet_name=None,
        page_number=1,
        bbox=None,
        row_count=2,
        column_count=2,
        columns=("a", "b"),
        raw_artifact_path=f"workspace/output/raw/table-{index}.json",
        normalized_artifact_path=None,
        extraction_confidence=0.9,
        quality_status=AssetQualityStatus.NOT_ASSESSED,
        created_at=utc_now(),
    )


def _text() -> TextAsset:
    return TextAsset(
        text_asset_id=make_text_asset_id(
            file_id=FILE_ID,
            content_sha256=CONTENT_SHA256,
            extractor="synthetic",
            extractor_version="1",
            source_kind=SourceKind.PAGE,
            source_locator="page:1/text:0",
            asset_index=0,
        ),
        file_id=FILE_ID,
        content_sha256=CONTENT_SHA256,
        extraction_run_id=RUN_ID,
        extractor="synthetic",
        extractor_version="1",
        source_kind=SourceKind.PAGE,
        page_number=1,
        section="body",
        bbox=None,
        text="research text",
        language="en",
        created_at=utc_now(),
    )


def test_one_file_can_have_multiple_table_assets() -> None:
    assets = FileAssetSet(file_id=FILE_ID, table_assets=(_table(0), _table(1)))
    assert len(assets.table_assets) == 2
    assert len({asset.table_id for asset in assets.table_assets}) == 2


def test_one_file_can_have_table_and_text_assets_at_once() -> None:
    assets = FileAssetSet(file_id=FILE_ID, table_assets=(_table(0),), text_assets=(_text(),))
    assert len(assets.table_assets) == 1
    assert len(assets.text_assets) == 1


def test_semantic_metadata_is_separate_from_raw_asset() -> None:
    table = _table(0)
    raw_fields = {field.name for field in fields(TableAsset)}
    assert "display_name" not in raw_fields
    metadata = SemanticMetadata(
        asset_id=table.table_id,
        asset_type=AssetType.TABLE,
        display_name="AI generated name",
        category="measurements",
        description="description",
        keywords=("sample",),
        summary="summary",
        semantic_fields={"a": {"meaning": "identifier"}},
        model="configured-model",
        prompt_version="v1",
        confidence=0.8,
        generated_at=utc_now(),
    )
    assert metadata.asset_id == table.table_id
    assert table.table_id not in metadata.display_name


@pytest.mark.parametrize("status", list(QualityIssueStatus))
def test_quality_issue_status_contract(status: QualityIssueStatus) -> None:
    issue = QualityIssue(
        issue_id=f"issue-{status.value}",
        asset_id=_table(0).table_id,
        severity=QualityIssueSeverity.WARNING,
        issue_type="ambiguous_header",
        description="Header requires review",
        evidence={"row": 1},
        detected_by="deterministic-rule",
        suggested_action="Review header",
        status=status,
    )
    assert issue.status is status


def test_stable_asset_id_does_not_depend_on_ai_display_name() -> None:
    provenance = {
        "file_id": FILE_ID,
        "content_sha256": CONTENT_SHA256,
        "extractor": "synthetic",
        "extractor_version": "1",
        "source_kind": SourceKind.SHEET,
        "source_locator": "sheet:Data/table:0",
        "asset_index": 0,
    }
    before_ai = make_table_id(**provenance)
    after_ai = make_table_id(**provenance)
    assert before_ai == after_ai


def test_text_chunk_provenance_reaches_source_and_extraction_run() -> None:
    text_asset = _text()
    provenance = ChunkProvenance(
        file_id=text_asset.file_id,
        content_sha256=text_asset.content_sha256,
        text_asset_id=text_asset.text_asset_id,
        extraction_run_id=text_asset.extraction_run_id,
        extractor=text_asset.extractor,
        extractor_version=text_asset.extractor_version,
        source_kind=text_asset.source_kind,
        page_number=text_asset.page_number,
        section=text_asset.section,
    )
    chunk = TextChunk(
        chunk_id=make_chunk_id(text_asset_id=text_asset.text_asset_id, chunk_index=0, char_start=0, char_end=8),
        text_asset_id=text_asset.text_asset_id,
        file_id=text_asset.file_id,
        chunk_index=0,
        text="research",
        char_start=0,
        char_end=8,
        provenance=provenance,
    )
    assert chunk.provenance.content_sha256 == CONTENT_SHA256
    assert chunk.provenance.page_number == 1
    assert chunk.provenance.extraction_run_id == RUN_ID
