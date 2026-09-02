"""Repeatable benchmark workflows that only publish below ``workspace``."""

from .pdf_real import RealPDFBenchmarkResult, run_real_pdf_benchmark

__all__ = ["RealPDFBenchmarkResult", "run_real_pdf_benchmark"]
