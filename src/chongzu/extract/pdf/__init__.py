"""Native PDF text extraction and profiling entry points."""

from .profiling import PDFProfile, PageProfile
from .pymupdf_extractor import PdfExtractionResult, extract_pdf_file
from .runner import PDFExtractionError, PDFExtractionSummary, extract_pdf

__all__ = [
    "PDFExtractionError",
    "PDFExtractionSummary",
    "PDFProfile",
    "PageProfile",
    "PdfExtractionResult",
    "extract_pdf",
    "extract_pdf_file",
]

