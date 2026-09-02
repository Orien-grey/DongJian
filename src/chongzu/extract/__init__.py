"""Native extraction entry points."""

from .pdf import (
    PDFExtractionError,
    PDFExtractionSummary,
    PDFTableExtractionError,
    PDFTableExtractionSummary,
    extract_pdf,
    extract_pdf_tables,
)
from .structured import StructuredExtractionError, extract_structured
from .ocr import OCRExtractionError, OCRExtractionSummary, extract_ocr
from .text import TextExtractionError, TextExtractionSummary, extract_text
from .unified import UnifiedExtractionError, UnifiedExtractionSummary, extract_unified

__all__ = [
    "PDFExtractionError",
    "PDFExtractionSummary",
    "PDFTableExtractionError",
    "PDFTableExtractionSummary",
    "StructuredExtractionError",
    "extract_pdf",
    "extract_pdf_tables",
    "extract_structured",
    "OCRExtractionError",
    "OCRExtractionSummary",
    "extract_ocr",
    "TextExtractionError",
    "TextExtractionSummary",
    "extract_text",
    "UnifiedExtractionError",
    "UnifiedExtractionSummary",
    "extract_unified",
]
