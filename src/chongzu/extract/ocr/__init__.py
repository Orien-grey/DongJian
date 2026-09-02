"""Offline OCR extraction for images and scanned PDF pages."""

from .rapidocr_engine import (
    OCRBlock,
    OCREngineError,
    RapidOCREngine,
    required_model_paths,
    validate_ocr_models,
)
from .runner import OCRExtractionError, OCRExtractionSummary, extract_ocr
from .img2table_adapter import (
    IMAGE_TABLE_EXTRACTOR,
    IMAGE_TABLE_EXTRACTOR_VERSION,
    ImageTableAdapterError,
    ImageTableConfig,
    ocr_data_from_blocks,
)

__all__ = [
    "OCRBlock",
    "OCREngineError",
    "RapidOCREngine",
    "required_model_paths",
    "validate_ocr_models",
    "OCRExtractionError",
    "OCRExtractionSummary",
    "extract_ocr",
    "IMAGE_TABLE_EXTRACTOR",
    "IMAGE_TABLE_EXTRACTOR_VERSION",
    "ImageTableAdapterError",
    "ImageTableConfig",
    "ocr_data_from_blocks",
]
