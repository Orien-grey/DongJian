# Phase 5A: RapidOCR local OCR foundation

Phase 5A adds the first offline visual-text path without changing the native
PDF or native table extractors:

```text
JPG/JPEG/PNG --------------------> RapidOCR + ONNX Runtime -> TextAsset/TextChunk
PDF -> Phase 4A profile -> scanned page -> PyMuPDF render -> RapidOCR -> TextAsset
```

The source file remains read-only. OCR writes the same workspace-contained raw,
normalized, and metadata text artifacts used by native PDF extraction. Each
asset records the source file, SHA-256, image/page, extractor/version, run ID,
relative path, bounding boxes, and per-block confidence when the engine
returns it. Image coordinates are pixels; PDF coordinates are mapped back to
PDF points. OCR text never replaces a native `pymupdf-native-text` asset and
does not imply a `TableAsset`.

## Offline runtime contract

The production payload is pinned to:

- `rapidocr==3.9.2`;
- `onnxruntime==1.29.0` (CPU, Windows x64, CPython 3.11);
- `omegaconf==2.0.6`, plus the wheel-only transitive dependencies in
  `uv.lock`.

The three PP-OCR models are copied to:

```text
runtime/models/ocr/
  PP-OCRv6_det_small.onnx
  ch_ppocr_mobile_v2.0_cls_mobile.onnx
  PP-OCRv6_rec_small.onnx
  manifest.json
```

`manifest.json` stores each model's byte size and SHA-256 and sets
`runtime_download_disabled=true`. The adapter passes every model path as an
explicit string. This is required for the Windows OmegaConf compatibility
behavior observed with RapidOCR 3.9.2 and, more importantly, prevents a
missing model from selecting RapidOCR's download fallback. Doctor fails if a
model is missing, altered, or the manifest does not disable downloads. No
Tesseract, PaddleOCR, CUDA, cloud OCR, or runtime model download is used.

Bootstrap uses the project-local uv and a temporary project-local staging venv
to publish the wheel payload into `runtime/packages`. The development venv is
only a test/provisioning environment; production launchers use standalone
CPython plus `runtime/packages`.

## Routing and cache identity

Images are always OCR candidates, including webpage screenshots. A PDF first
runs/reuses Phase 4A profiling. Pages with reliable native text are not OCR'd;
only pages marked `suspected_scanned` or pages without native text in a `mixed`
profile are sent to RapidOCR. A native page and an OCR page can therefore
coexist in one PDF. Unknown or missing profiles are conservatively deferred.

OCR reuse is independent from native text and table extraction. The identity
contains file ID, source SHA-256, business format, extractor/version, OCR
configuration/pipeline versions, and the selected image/page targets. `--force`
creates a new run. A failed run does not pre-delete a prior successful artifact.

## Deterministic processing

Raw OCR text is the ordered engine output. Normalized text only applies Unicode
NFC, line-ending normalization, and removal of NUL/control garbage. Blocks and
their geometry remain in metadata. Chunks are page/image-local, use the shared
deterministic chunk configuration, and carry the same provenance chain. No
semantic cleanup, deduplication, table reconstruction, embedding, or model
summary occurs in this phase.

The runner uses at most two process workers by default and configures one ONNX
intra/inter-op thread per worker. DuckDB writes are centralized in the parent
process. Rendering, OCR, artifact, registry, and total wall-clock timings are
recorded per run.

## Commands

```text
.\chongzu.cmd extract ocr "D:\Research Data\Project" --workers 2
.\chongzu.cmd benchmark ocr "D:\Research Data\Project" --workers 2
```

The command automatically performs an incremental Registry scan and establishes
PDF profiles when PDF candidates exist. Unsupported/corrupt files remain
isolated in the Registry; one bad image or page does not terminate the batch.

## Known limitations and next step

RapidOCR returns text blocks, not a guaranteed table structure. OCR table text
is retained as evidence and a future table candidate may consume its blocks;
complex table reconstruction is deliberately deferred. CJK accuracy depends on
the bundled model and source resolution. Confidence is only reported when the
engine returns a score and is not treated as ground truth. Phase 5A's real
corpus smoke run is limited to a small number of profile-selected scanned pages;
full corpus OCR and Phase 5B table integration require explicit review.
