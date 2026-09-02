"""Repeatable benchmark workflows that only publish below ``workspace``."""

from .pdf_real import RealPDFBenchmarkResult, run_real_pdf_benchmark
from .ocr_consistency import RenderedPageConsistencyResult, run_rendered_page_consistency

__all__ = [
    "RealPDFBenchmarkResult",
    "run_real_pdf_benchmark",
    "RenderedPageConsistencyResult",
    "run_rendered_page_consistency",
]
