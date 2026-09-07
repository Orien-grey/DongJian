"""Small runtime compatibility boundary for the pinned img2table adapter."""

from __future__ import annotations

from typing import Any


def ensure_img2table_threshold_compat() -> None:
    """Keep img2table 2.0.0 usable with the bundled OpenCV 5 wheel.

    The pinned Windows OpenCV build exposes ``ximgproc`` but no longer ships
    the Sauvola symbols used by img2table's threshold helper. The table
    detector still has all of its normal geometry/OCR logic; this local
    compatibility path supplies an existing OpenCV adaptive threshold when
    those optional symbols are absent. It adds no OCR pass or dependency.
    """

    try:
        import cv2
        import numpy as np
        from img2table.tables import extractor as table_extractor
    except ImportError as exc:  # pragma: no cover - normal doctor/provisioning boundary
        raise RuntimeError(f"img2table threshold compatibility is unavailable: {exc}") from exc
    ximgproc = getattr(cv2, "ximgproc", None)
    if ximgproc is not None and hasattr(ximgproc, "niBlackThreshold") and hasattr(ximgproc, "BINARIZATION_SAUVOLA"):
        return

    def adaptive_threshold(*, img: Any, char_length: float) -> Any:
        array = np.asarray(img)
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY) if array.ndim == 3 else array
        if float(np.mean(gray)) <= 127:
            gray = 255 - gray
        block_size = max(3, int(char_length) // 2 * 2 + 1)
        return cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block_size,
            11,
        )

    # TableExtractor imported this function into its own module namespace, so
    # patching only that reference keeps the compatibility boundary local and
    # avoids modifying files under runtime/packages.
    table_extractor.threshold_dark_areas = adaptive_threshold
