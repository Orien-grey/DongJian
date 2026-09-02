"""Offline OCR extraction for images and scanned PDF pages."""

from .rapidocr_engine import (
    OCRBlock,
    OCREngineError,
    RapidOCREngine,
    required_model_paths,
    validate_ocr_models,
)
from .runner import OCRExtractionError, OCRExtractionSummary, extract_ocr

__all__ = [
    "OCRBlock",
    "OCREngineError",
    "RapidOCREngine",
    "required_model_paths",
    "validate_ocr_models",
    "OCRExtractionError",
    "OCRExtractionSummary",
    "extract_ocr",
]
