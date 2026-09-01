"""Native PDF text extraction and profiling entry points."""

from .profiling import PDFProfile, PageProfile
from .pymupdf_extractor import PdfExtractionResult, extract_pdf_file
from .runner import PDFExtractionError, PDFExtractionSummary, extract_pdf
from .img2table_extractor import (
    EXTRACTOR_NAME as PDF_TABLE_EXTRACTOR_NAME,
    EXTRACTOR_VERSION as PDF_TABLE_EXTRACTOR_VERSION,
    PDFTableConfig,
    PDFTableExtractionResult,
    extract_pdf_tables_file,
)
from .table_quality import ExpectedTable, GroundTruthScore, load_ground_truth, score_tables
from .table_runner import (
    MAX_PDF_TABLE_WORKERS,
    PDFTableExtractionError,
    PDFTableExtractionSummary,
    extract_pdf_tables,
    normalize_pdf_table_workers,
    pdf_table_extraction_identity,
)

__all__ = [
    "PDFExtractionError",
    "PDFExtractionSummary",
    "PDFProfile",
    "PageProfile",
    "PdfExtractionResult",
    "extract_pdf",
    "extract_pdf_file",
    "MAX_PDF_TABLE_WORKERS",
    "PDFTableConfig",
    "PDFTableExtractionError",
    "PDFTableExtractionResult",
    "PDFTableExtractionSummary",
    "PDF_TABLE_EXTRACTOR_NAME",
    "PDF_TABLE_EXTRACTOR_VERSION",
    "ExpectedTable",
    "GroundTruthScore",
    "extract_pdf_tables",
    "extract_pdf_tables_file",
    "load_ground_truth",
    "normalize_pdf_table_workers",
    "pdf_table_extraction_identity",
    "score_tables",
]
