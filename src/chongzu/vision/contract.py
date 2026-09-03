"""Versioned prompt contract for the first Vision extraction route."""

from __future__ import annotations

VISION_CONTRACT_VERSION = "vision-json-v1"
VISION_EXTRACTOR = "vision_llm"
VISION_EXTRACTOR_VERSION = "openai-compatible-vision-v1"
VISION_PIPELINE_VERSION = "phase-m1-vision-image"
VISION_CONFIG_VERSION = VISION_CONTRACT_VERSION

VISION_OUTPUT_CONTRACT = """Return one JSON object and no markdown or commentary.
The object must contain exactly these required fields:
{
  "page_type": "text|table|mixed|other",
  "title": "string",
  "useful_text": [{"text": "string", "role": "title|body|caption|note|other"}],
  "tables": [{"title": "string", "columns": ["string"], "rows": [[scalar]]}]
}
Optional field: "confidence" is either a number from 0 to 1 or null.
Do not invent file IDs, paths, hashes, asset IDs, run IDs, or artifact names.
Preserve observed cell values exactly; do not calculate, correct, or infer
values that are not legible in the image. Use null only when a cell is visibly
blank or unreadable. A table row must have the same width as columns.
"""
