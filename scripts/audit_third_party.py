"""Audit the production payload and materialize local license evidence.

The audit deliberately reads the provisioned runtime and the built frontend
from the checkout.  It does not query a package index, download anything, or
make a legal determination.  Missing/ambiguous local evidence is preserved as
REVIEW_REQUIRED in the generated component manifest.
"""

from __future__ import annotations

import argparse
import csv
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable


SCHEMA_VERSION = "phase10.1-third-party-v1"
PYTHON_DIR_NAME = "cpython-3.11.15-windows-x86_64-none"
MODEL_NAMES = {
    "PP-OCRv6_det_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "PP-OCRv6_rec_small.onnx",
}
LICENSE_NAME_RE = re.compile(
    r"^(?:license|licence|copying|notice|third.?party)(?:[._-].*)?$",
    re.IGNORECASE,
)
UNKNOWN_LICENSES = {"", "unknown", "proprietary", "none", "n/a"}
FRONTEND_DISTRIBUTED = {"react", "react-dom", "scheduler"}


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_license_file(path: Path) -> bool:
    return bool(LICENSE_NAME_RE.match(path.name))


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve()).casefold()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        result.append(path)
    return sorted(result, key=lambda item: str(item).casefold())


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug or "component"


def _clean_license_value(value: str | None) -> str:
    value = (value or "").strip()
    if value.casefold() in UNKNOWN_LICENSES:
        return ""
    if value.casefold().startswith("copyright"):
        return ""
    return value


def _infer_license_from_text(paths: Iterable[Path]) -> str:
    """Identify only licenses whose canonical text is present locally."""

    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)
    upper = text.upper()
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in upper:
        return "AGPL-3.0"
    if "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in upper:
        return "PSF-2.0"
    if "APACHE LICENSE" in upper:
        return "Apache-2.0"
    if "BSD 3-CLAUSE LICENSE" in upper or "REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS" in upper and "NEITHER THE NAME" in upper:
        return "BSD-3-Clause"
    if "BSD 2-CLAUSE LICENSE" in upper:
        return "BSD-2-Clause"
    if "MIT LICENSE" in upper or "THE MIT LICENSE" in upper or (
        "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in upper
        and "THE SOFTWARE IS PROVIDED" in upper
        and "AUTHORS OR COPYRIGHT HOLDERS" in upper
    ):
        return "MIT"
    if "MOZILLA PUBLIC LICENSE" in upper:
        return "MPL-2.0"
    return ""


def _homepage(metadata: Any) -> str:
    for value in metadata.get_all("Project-URL", []):
        if "," in value:
            label, url = value.split(",", 1)
            if label.strip().casefold() in {"homepage", "source", "repository", "home"}:
                return url.strip()
        elif value.strip():
            return value.strip()
    return (metadata.get("Home-page") or "").strip()


def _top_levels(dist_info: Path, metadata_name: str) -> list[str]:
    path = dist_info / "top_level.txt"
    values: list[str] = []
    if path.is_file():
        values.extend(line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
    values.append(metadata_name.replace("-", "_"))
    values.append(metadata_name.replace("-", ""))
    return sorted({value for value in values if value}, key=str.casefold)


def _record_has_native_payload(dist_info: Path) -> bool:
    record = dist_info / "RECORD"
    if not record.is_file():
        return False
    with record.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            relative = row[0].casefold()
            if relative.endswith((".pyd", ".dll", ".so", ".dylib")) or ".libs/" in relative:
                return True
    return False


def _package_evidence(dist_info: Path, packages_root: Path, metadata: Any) -> list[Path]:
    candidates: list[Path] = []
    for value in metadata.get_all("License-File", []):
        relative = value.strip().replace("/", "\\")
        # Wheel metadata records license files relative to the optional
        # ``.dist-info/licenses`` directory in current installers.  Accept
        # the older direct .dist-info form as well, but only if the local file
        # actually exists.
        candidates.append(dist_info / "licenses" / relative)
        candidates.append(dist_info / relative)
    for path in dist_info.rglob("*"):
        if path.is_file() and _is_license_file(path):
            candidates.append(path)
    for top_level in _top_levels(dist_info, metadata.get("Name", dist_info.name)):
        root = packages_root / top_level
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and _is_license_file(path):
                candidates.append(path)
    return _unique_paths(candidates)


def _license_destination(component_slug: str, source: Path, source_root: Path) -> str:
    relative = _relative(source, source_root)
    flattened = "__".join(part for part in relative.split("/") if part)
    return f"licenses/{component_slug}/{flattened}"


def _license_status(
    *,
    license_value: str,
    evidence: list[Path],
    source_root: Path,
    metadata_path: str,
    force_review: str | None = None,
) -> tuple[str, str, list[str]]:
    if force_review:
        status = force_review
    elif not license_value:
        status = "REVIEW_REQUIRED"
    elif not evidence:
        status = "REVIEW_REQUIRED"
    else:
        status = "identified"
    redistribution = "found in local license/notice evidence" if evidence else "not found in local license/notice evidence"
    sources = [metadata_path]
    sources.extend(_relative(path, source_root) for path in evidence)
    return status, redistribution, sources


def _materialize_license_evidence(
    *,
    output_root: Path,
    source_root: Path,
    component_slug: str,
    evidence: list[Path],
) -> list[str]:
    paths: list[str] = []
    for source in evidence:
        relative = _license_destination(component_slug, source, source_root)
        destination = output_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        paths.append(relative)
    return paths


def _package_component(dist_info: Path, packages_root: Path, source_root: Path, output_root: Path | None) -> dict[str, Any]:
    metadata_path = dist_info / "METADATA"
    if not metadata_path.is_file():
        raise RuntimeError(f"missing package metadata: {metadata_path}")
    metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8", errors="replace"))
    name = (metadata.get("Name") or dist_info.name.removesuffix(".dist-info")).strip()
    version = (metadata.get("Version") or "not recorded").strip()
    evidence = _package_evidence(dist_info, packages_root, metadata)
    metadata_license = _clean_license_value(metadata.get("License-Expression"))
    if not metadata_license:
        metadata_license = _clean_license_value(metadata.get("License"))
    license_value = metadata_license or _infer_license_from_text(evidence)
    force_review = None
    if name.casefold() == "pymupdf":
        force_review = "RELEASE LEGAL REVIEW REQUIRED"
    status, redistribution, sources = _license_status(
        license_value=license_value,
        evidence=evidence,
        source_root=source_root,
        metadata_path=_relative(metadata_path, source_root),
        force_review=force_review,
    )
    component_slug = _safe_slug(name)
    bundled = []
    if output_root is not None and evidence:
        bundled = _materialize_license_evidence(
            output_root=output_root,
            source_root=source_root,
            component_slug=component_slug,
            evidence=evidence,
        )
    else:
        bundled = [_license_destination(component_slug, path, source_root) for path in evidence]
    if name.casefold() == "pymupdf":
        redistribution = "partial: local COPYING identifies dual licensing but does not record the applicable selection or commercial terms"
    homepage = _homepage(metadata)
    return {
        "name": name,
        "version": version,
        "component_type": "native-library" if _record_has_native_payload(dist_info) else "python-package",
        "distributed": True,
        "license": license_value or "REVIEW_REQUIRED",
        "license_source": sources,
        "homepage/source": homepage or "REVIEW_REQUIRED: no homepage/source in local package metadata",
        "notice_required": True,
        "bundled_license_path": bundled,
        "review_status": status,
        "redistribution_evidence": redistribution,
        "distribution_metadata": _relative(dist_info, source_root),
        "notes": "All files in this runtime distribution are included in the audit scope; native payload classification is based on local RECORD entries.",
    }


def _cpython_component(source_root: Path, output_root: Path | None) -> dict[str, Any]:
    runtime = source_root / "runtime" / "python" / PYTHON_DIR_NAME
    license_path = runtime / "LICENSE.txt"
    if not license_path.is_file():
        raise RuntimeError(f"standalone CPython license is missing: {license_path}")
    tcl_paths = sorted(
        (path for path in runtime.joinpath("tcl").rglob("*") if path.is_file() and path.name.casefold() == "license.terms"),
        key=lambda path: str(path).casefold(),
    )
    evidence = _unique_paths([license_path, *tcl_paths])
    bundled = []
    if output_root is not None:
        for source in evidence:
            if source == license_path:
                destination = output_root / "licenses" / "CPython.txt"
            else:
                destination = output_root / "licenses" / "CPython" / "tcl" / source.relative_to(runtime / "tcl")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            bundled.append(_relative(destination, output_root))
    else:
        bundled = ["licenses/CPython.txt"]
        bundled.extend(
            f"licenses/CPython/tcl/{source.relative_to(runtime / 'tcl').as_posix()}" for source in tcl_paths
        )
    return {
        "name": "CPython",
        "version": "3.11.15",
        "component_type": "python-runtime",
        "distributed": True,
        "license": "PSF-2.0 plus bundled third-party notices",
        "license_source": [_relative(path, source_root) for path in evidence],
        "homepage/source": "runtime/python/cpython-3.11.15-windows-x86_64-none/LICENSE.txt",
        "notice_required": True,
        "bundled_license_path": bundled,
        "review_status": "identified",
        "redistribution_evidence": "found in the standalone runtime LICENSE.txt and Tcl/Tk license terms",
        "distribution_metadata": "runtime/python/cpython-3.11.15-windows-x86_64-none",
        "notes": "The release builder removes interpreter provisioning site-packages, Scripts, ensurepip, and venv payloads; the standalone CPython license remains included.",
    }


def _model_components(source_root: Path) -> list[dict[str, Any]]:
    model_root = source_root / "runtime" / "models" / "ocr"
    manifest_path = model_root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"OCR model manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RuntimeError("OCR model manifest has no files list")
    names = {str(item.get("name")) for item in entries if isinstance(item, dict)}
    missing = MODEL_NAMES - names
    if missing:
        raise RuntimeError(f"OCR model manifest is missing expected files: {sorted(missing)}")
    components: list[dict[str, Any]] = []
    for item in sorted(entries, key=lambda value: str(value.get("name", "")).casefold()):
        name = str(item.get("name", ""))
        path = model_root / name
        if not path.is_file() or _sha256(path) != str(item.get("sha256", "")).casefold():
            raise RuntimeError(f"OCR model hash mismatch: {path}")
        components.append(
            {
                "name": name,
                "version": "not recorded in local model manifest",
                "component_type": "model",
                "distributed": True,
                "license": "RELEASE LEGAL REVIEW REQUIRED",
                "license_source": [_relative(manifest_path, source_root)],
                "homepage/source": "REVIEW_REQUIRED: upstream model project/source is not recorded locally",
                "notice_required": True,
                "bundled_license_path": [],
                "review_status": "RELEASE LEGAL REVIEW REQUIRED",
                "redistribution_evidence": "not found in local model manifest or adjacent model metadata",
                "distribution_metadata": _relative(manifest_path, source_root),
                "model_source": "not recorded locally",
                "upstream_project": "not recorded locally",
                "extractor": f"{manifest.get('extractor', 'not recorded')} {manifest.get('extractor_version', 'not recorded')}",
                "size_bytes": int(item.get("size_bytes", path.stat().st_size)),
                "sha256": _sha256(path),
                "notes": "The Python package license is not used as a model-weight license conclusion.",
            }
        )
    return components


def _node_package_paths(node_modules: Path) -> list[Path]:
    paths: list[Path] = []
    for child in node_modules.iterdir() if node_modules.is_dir() else []:
        if child.name == ".bin":
            continue
        if child.name.startswith("@") and child.is_dir():
            paths.extend(grandchild for grandchild in child.iterdir() if grandchild.is_dir())
        elif child.is_dir():
            paths.append(child)
    return sorted((path for path in paths if (path / "package.json").is_file()), key=lambda path: str(path).casefold())


def _node_license_files(package_root: Path) -> list[Path]:
    return _unique_paths(path for path in package_root.iterdir() if path.is_file() and _is_license_file(path))


def _node_version(node_root: Path) -> str:
    node = node_root / "node.exe"
    if not node.is_file():
        return "not found locally"
    try:
        result = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "not reported locally"
    value = result.stdout.strip()
    return value.removeprefix("v") if result.returncode == 0 and value else "not reported locally"


def _frontend_components(source_root: Path, output_root: Path | None) -> list[dict[str, Any]]:
    node_modules = source_root / "frontend" / "node_modules"
    dist = source_root / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError(f"frontend production bundle is missing: {dist / 'index.html'}")
    components: list[dict[str, Any]] = []
    for package_root in _node_package_paths(node_modules):
        package_path = package_root / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        name = str(package.get("name") or package_root.name)
        version = str(package.get("version") or "not recorded")
        license_value = _clean_license_value(str(package.get("license") or ""))
        evidence = _node_license_files(package_root)
        distributed = name in FRONTEND_DISTRIBUTED
        component_slug = _safe_slug(name)
        bundled: list[str] = []
        if distributed and output_root is not None:
            bundled = _materialize_license_evidence(
                output_root=output_root,
                source_root=source_root,
                component_slug=f"frontend_{component_slug}",
                evidence=evidence,
            )
        elif distributed:
            bundled = [
                _license_destination(f"frontend_{component_slug}", path, source_root) for path in evidence
            ]
        if distributed:
            status = "identified" if license_value and evidence else "REVIEW_REQUIRED"
            redistribution = "found in local package metadata/license text" if evidence else "not found in local package license text"
            note = "Code is bundled into frontend/dist; the package directory is not distributed."
            notice_required = True
        else:
            status = "identified" if license_value else "REVIEW_REQUIRED"
            redistribution = "not applicable: build-only package is not distributed"
            note = "Build-only package; no package files are copied into frontend/dist as a standalone dependency."
            notice_required = False
        repository = package.get("homepage") or package.get("repository")
        if isinstance(repository, dict):
            repository = repository.get("url")
        components.append(
            {
                "name": name,
                "version": version,
                "component_type": "frontend-runtime" if distributed else "build-tool",
                "distributed": distributed,
                "license": license_value or "REVIEW_REQUIRED",
                "license_source": [_relative(package_path, source_root)]
                + [_relative(path, source_root) for path in evidence],
                "homepage/source": str(repository or "REVIEW_REQUIRED: no homepage/source in local package metadata"),
                "notice_required": notice_required,
                "bundled_license_path": bundled,
                "review_status": status,
                "redistribution_evidence": redistribution,
                "distribution_metadata": _relative(package_path.parent, source_root),
                "notes": note,
            }
        )

    node_root = source_root / "runtime" / "node-dev"
    node_license = node_root / "LICENSE"
    node_evidence = [node_license] if node_license.is_file() else []
    components.append(
        {
            "name": "Node.js",
            "version": _node_version(node_root),
            "component_type": "build-tool",
            "distributed": False,
            "license": "MIT plus bundled third-party notices" if node_evidence else "REVIEW_REQUIRED",
            "license_source": [_relative(node_license, source_root)] if node_evidence else [],
            "homepage/source": "runtime/node-dev/README.md",
            "notice_required": False,
            "bundled_license_path": [],
            "review_status": "identified" if node_evidence else "REVIEW_REQUIRED",
            "redistribution_evidence": "not applicable: build-only runtime is not distributed",
            "distribution_metadata": "runtime/node-dev",
            "notes": "Node/npm are build-only and are intentionally excluded from the release bundle.",
        }
    )
    return sorted(components, key=lambda item: (not bool(item["distributed"]), str(item["name"]).casefold()))


def _components(source_root: Path, output_root: Path | None) -> list[dict[str, Any]]:
    packages_root = source_root / "runtime" / "packages"
    dist_infos = sorted(packages_root.glob("*.dist-info"), key=lambda path: path.name.casefold())
    if not dist_infos:
        raise RuntimeError(f"no runtime package distributions found under {packages_root}")
    components: list[dict[str, Any]] = [_cpython_component(source_root, output_root)]
    components.extend(_package_component(path, packages_root, source_root, output_root) for path in dist_infos)
    components.extend(_model_components(source_root))
    components.extend(_frontend_components(source_root, output_root))
    return sorted(components, key=lambda item: (not bool(item["distributed"]), str(item["name"]).casefold(), str(item["version"])))


def _summary(components: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "component_count": len(components),
        "distributed_runtime_component_count": sum(1 for item in components if item["distributed"]),
        "build_only_component_count": sum(1 for item in components if not item["distributed"]),
        "license_identified_count": sum(1 for item in components if item["license"] not in {"REVIEW_REQUIRED", "RELEASE LEGAL REVIEW REQUIRED"}),
        "review_required_count": sum(1 for item in components if item["review_status"] != "identified"),
    }


def _render_notices(components: list[dict[str, Any]], summary: dict[str, int], *, version: str) -> str:
    lines = [
        f"ChongZu {version} third-party notices",
        "",
        "Engineering audit record for the actual production payload and the",
        "frontend production bundle. This file records local evidence only; it",
        "does not make a legal determination or certify redistribution rights.",
        "Missing or ambiguous evidence is explicitly marked REVIEW_REQUIRED.",
        "",
        f"Component count: {summary['component_count']}",
        f"Distributed runtime components: {summary['distributed_runtime_component_count']}",
        f"Build-only components: {summary['build_only_component_count']}",
        f"License identified: {summary['license_identified_count']}",
        f"Review required: {summary['review_required_count']}",
        "",
    ]
    for component in components:
        lines.extend(
            [
                "-------------------------------------------------------------------------------",
                f"Name: {component['name']}",
                f"Version: {component['version']}",
                f"Component type: {component['component_type']}",
                f"Distributed: {'yes' if component['distributed'] else 'no'}",
                f"License: {component['license']}",
                f"License source: {', '.join(component['license_source']) or 'not found'}",
                f"Homepage/source: {component['homepage/source']}",
                f"Notice required: {'yes' if component['notice_required'] else 'no'}",
                f"Redistribution evidence: {component['redistribution_evidence']}",
                f"Review status: {component['review_status']}",
                f"Bundled license path: {', '.join(component['bundled_license_path']) or 'none'}",
                f"Notes: {component['notes']}",
                "",
            ]
        )
    lines.extend(
        [
            "OCR model audit boundary:",
            "The three PP-OCRv6/mobile ONNX files are audited as model components",
            "separately from the RapidOCR Python package. Their current local",
            "manifest records hashes but no upstream license or redistribution",
            "terms, so each is RELEASE LEGAL REVIEW REQUIRED.",
            "",
        ]
    )
    return "\n".join(lines)


def _payload(components: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_scope": "standalone CPython, runtime/packages distributions, OCR models, frontend/dist runtime code, and build-only frontend tooling",
        "components": components,
        "summary": _summary(components),
    }


def _check_output(root: Path, expected: dict[str, Any], *, version: str) -> None:
    manifest_path = root / "third-party-components.json"
    notices_path = root / "THIRD_PARTY_NOTICES.txt"
    if not manifest_path.is_file() or not notices_path.is_file():
        raise RuntimeError("third-party audit outputs are missing")
    actual = json.loads(manifest_path.read_text(encoding="utf-8"))
    if actual != expected:
        raise RuntimeError("third-party-components.json is stale or differs from the local payload audit")
    expected_notices = _render_notices(expected["components"], expected["summary"], version=version)
    if notices_path.read_text(encoding="utf-8") != expected_notices:
        raise RuntimeError("THIRD_PARTY_NOTICES.txt is stale or differs from the local payload audit")
    for component in expected["components"]:
        for relative in component["bundled_license_path"]:
            destination = root / relative
            if not destination.is_file():
                raise RuntimeError(f"bundled license evidence is missing: {relative}")


def audit(root: Path, output_root: Path, *, check: bool) -> dict[str, Any]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if check:
        expected_components = _components(root, None)
        expected = _payload(expected_components)
        _check_output(output_root, expected, version=version)
        return expected
    licenses_root = output_root / "licenses"
    if licenses_root.exists():
        shutil.rmtree(licenses_root)
    output_root.mkdir(parents=True, exist_ok=True)
    components = _components(root, output_root)
    payload = _payload(components)
    (output_root / "third-party-components.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_root / "THIRD_PARTY_NOTICES.txt").write_text(
        _render_notices(components, payload["summary"], version=version),
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--check", action="store_true", help="verify existing audit outputs without writing")
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    output_root = (args.output_root or root).resolve()
    try:
        payload = audit(root, output_root, check=args.check)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"THIRD-PARTY AUDIT: FAIL: {exc}", file=sys.stderr)
        return 1
    action = "checked" if args.check else "written"
    print(json.dumps({"action": action, **payload["summary"]}, indent=2))
    if payload["summary"]["review_required_count"]:
        print("THIRD-PARTY AUDIT: PASS WITH REVIEW_REQUIRED ITEMS")
    else:
        print("THIRD-PARTY AUDIT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
