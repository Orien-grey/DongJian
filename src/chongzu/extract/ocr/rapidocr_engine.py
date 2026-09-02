"""Small, lazy RapidOCR adapter with an explicit offline model contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable

from chongzu import paths


MODEL_FILENAMES = {
    "det": "PP-OCRv6_det_small.onnx",
    "cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "rec": "PP-OCRv6_rec_small.onnx",
}


class OCREngineError(RuntimeError):
    """Raised when the local OCR runtime or its model bundle is incomplete."""


@dataclass(frozen=True)
class OCRBlock:
    text: str
    confidence: float | None
    bbox: tuple[tuple[float, float], ...]
    page_number: int | None = None
    image: str | None = None
    block_index: int = 0
    extractor: str = "rapidocr-onnx"
    extractor_version: str = paths.RAPIDOCR_VERSION

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("OCRBlock text must not be empty")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("OCRBlock page_number must be one-based")
        if self.block_index < 0:
            raise ValueError("OCRBlock block_index must be non-negative")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("OCRBlock confidence must be between 0 and 1")


def required_model_paths(model_root: Path | str | None = None) -> dict[str, Path]:
    root = Path(model_root or paths.OCR_MODELS_ROOT).resolve()
    return {name: root / filename for name, filename in MODEL_FILENAMES.items()}


def validate_ocr_models(model_root: Path | str | None = None) -> dict[str, Path]:
    """Validate models without creating directories or downloading anything."""

    root = Path(model_root or paths.OCR_MODELS_ROOT).resolve()
    model_paths = required_model_paths(root)
    missing = [str(path) for path in model_paths.values() if not path.is_file()]
    if missing:
        raise OCREngineError(
            "RapidOCR model bundle is incomplete; missing local files: " + ", ".join(missing)
        )
    outside = [str(path) for path in model_paths.values() if not paths.is_within_project(path)]
    if outside:
        raise OCREngineError("RapidOCR model path escapes the project: " + ", ".join(outside))
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise OCREngineError(f"RapidOCR model manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("runtime_download_disabled") is not True:
            raise ValueError("runtime_download_disabled is not true")
        entries = {str(item.get("name")): item for item in manifest.get("files", []) if isinstance(item, dict)}
        for path in model_paths.values():
            entry = entries.get(path.name)
            if entry is None or int(entry.get("size_bytes", -1)) != path.stat().st_size:
                raise ValueError(f"manifest size mismatch for {path.name}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if str(entry.get("sha256", "")).casefold() != digest:
                raise ValueError(f"manifest SHA-256 mismatch for {path.name}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise OCREngineError(f"RapidOCR model manifest is invalid: {exc}") from exc
    return model_paths


def _as_float(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if 0.0 <= converted <= 1.0 else None


def _as_bbox(value: Any) -> tuple[tuple[float, float], ...]:
    try:
        points = tuple((float(point[0]), float(point[1])) for point in value)
    except (TypeError, ValueError, IndexError):
        return ()
    return points if len(points) >= 4 else ()


def _iter_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


class RapidOCREngine:
    """Process-local OCR engine; model paths are always explicit strings.

    RapidOCR 3.9.x uses ``pathlib.Path`` in its default OmegaConf update,
    which is rejected by the bundled OmegaConf 2.0.x on Windows.  Passing all
    paths as strings both avoids that incompatibility and prevents its
    download fallback from being selected.
    """

    extractor = "rapidocr-onnx"
    extractor_version = paths.RAPIDOCR_VERSION

    def __init__(self, model_root: Path | str | None = None) -> None:
        model_paths = validate_ocr_models(model_root)
        try:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on runtime payload
            raise OCREngineError(f"RapidOCR import failed: {exc}") from exc
        # Do not call RapidOCR() with defaults: that can attempt an online model
        # download when a package model is absent.
        root = next(iter(model_paths.values())).parent
        self._engine = RapidOCR(
            params={
                "Global.model_root_dir": str(root),
                "Det.model_path": str(model_paths["det"]),
                "Cls.model_path": str(model_paths["cls"]),
                "Rec.model_path": str(model_paths["rec"]),
                # Keep process-level concurrency bounded on CPU.  The runner
                # itself bounds worker processes separately.
                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "warning",
            }
        )

    def recognize(
        self,
        image: Any,
        *,
        page_number: int | None = None,
        image_id: str | None = None,
    ) -> tuple[list[OCRBlock], float]:
        """Recognize one image and return only the internal OCR contract.

        The RapidOCR result object never leaves this adapter.  ``page_number``
        and ``image_id`` are caller-owned provenance because the same engine is
        used for a standalone image and for a rendered PDF page.
        """

        started = time.perf_counter_ns()
        try:
            output = self._engine(image)
        except Exception as exc:
            raise OCREngineError(f"RapidOCR inference failed: {exc}") from exc
        boxes = tuple(_iter_values(getattr(output, "boxes", None)))
        texts = tuple(_iter_values(getattr(output, "txts", None)))
        scores = tuple(_iter_values(getattr(output, "scores", None)))
        blocks: list[OCRBlock] = []
        for index, text in enumerate(texts):
            value = str(text)
            if not value.strip():
                continue
            score = _as_float(scores[index]) if index < len(scores) else None
            bbox = _as_bbox(boxes[index]) if index < len(boxes) else ()
            blocks.append(
                OCRBlock(
                    text=value,
                    confidence=score,
                    bbox=bbox,
                    page_number=page_number,
                    image=image_id,
                    block_index=index,
                    extractor=self.extractor,
                    extractor_version=self.extractor_version,
                )
            )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        return blocks, elapsed_ms
