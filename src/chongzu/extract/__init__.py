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
]
