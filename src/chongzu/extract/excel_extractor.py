"""XLS/XLSX extraction through the native Calamine wheel."""

from __future__ import annotations

from importlib.metadata import version
import time

from python_calamine import CalamineError, CalamineWorkbook

from .artifacts import make_issue, publish_matrix_region, write_sheet_snapshot
from .models import FileExtractionResult, StructuredSource
from .table_regions import detect_table_regions


EXTRACTOR_NAME = "python-calamine"


def extract_excel(source: StructuredSource, extraction_run_id: str, extraction_identity: str) -> FileExtractionResult:
    result = FileExtractionResult(
        source=source,
        extraction_run_id=extraction_run_id,
        extraction_identity=extraction_identity,
        extractor=EXTRACTOR_NAME,
        extractor_version=version("python-calamine"),
    )
    try:
        open_started = time.perf_counter_ns()
        workbook = CalamineWorkbook.from_path(source.path, load_tables=False)
        result.timings.workbook_open_ms = (time.perf_counter_ns() - open_started) / 1_000_000
        with workbook:
            metadata_by_name = {item.name: item for item in workbook.sheets_metadata}
            result.sheet_count = len(workbook.sheet_names)
            asset_index = 0
            for sheet_index, sheet_name in enumerate(workbook.sheet_names):
                extraction_started = time.perf_counter_ns()
                sheet = workbook.get_sheet_by_index(sheet_index)
                rows = sheet.to_python(skip_empty_area=False)
                result.timings.extraction_ms += (time.perf_counter_ns() - extraction_started) / 1_000_000
                sheet_meta = metadata_by_name.get(sheet_name)
                full_sheet = write_sheet_snapshot(source, sheet_index, sheet_name, rows, result)
                result.warnings.append(
                    {
                        "sheet_index": sheet_index,
                        "sheet_name": sheet_name,
                        "visibility": getattr(getattr(sheet_meta, "visible", None), "name", None),
                        "sheet_type": getattr(getattr(sheet_meta, "typ", None), "name", None),
                        "empty": not bool(detect_table_regions(rows)),
                        "raw_sheet_artifact": full_sheet,
                    }
                )
                regions = detect_table_regions(rows)
                if not regions:
                    result.issues.append(
                        make_issue(
                            asset_id=f"file:{source.file_id}",
                            issue_type="empty_sheet",
                            evidence={
                                "source_relative_path": source.relative_path,
                                "sheet_index": sheet_index,
                                "sheet_name": sheet_name,
                            },
                        )
                    )
                    continue
                for region in regions:
                    asset = publish_matrix_region(
                        source=source,
                        result=result,
                        rows=rows,
                        region=region,
                        asset_index=asset_index,
                        sheet_name=sheet_name,
                        sheet_index=sheet_index,
                        full_sheet_artifact=full_sheet,
                    )
                    result.assets.append(asset)
                    asset_index += 1
        result.status = "partial" if result.issues else "successful"
    except (CalamineError, OSError, ValueError) as exc:
        result.status = "failed"
        result.error_category = "corrupt_workbook"
        result.error_message = str(exc)
    return result
