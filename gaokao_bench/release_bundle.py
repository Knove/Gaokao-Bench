"""Build and verify reproducible Gaokao-Bench release bundles.

The bundle verifier is intentionally independent from the builder: it reads the
ZIP, validates every path, and recomputes every file hash from ``release.json``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .release import evaluate_release_gate


PROFILES = ("internal-candidate", "public-code", "public-candidate")

# The code-only artifact is curated rather than built from broad repository
# directories.  Several development files embed reviewed answer keys or
# third-party evidence URLs; publishing those would silently turn a code
# release into a dataset/rights release.
PUBLIC_CODE_README_FILES = ("README.md", "README.zh-CN.md")
PUBLIC_CODE_README_ASSET_DIR = ".github/readme"
PUBLIC_CODE_EXACT_FILES = {
    "SCOPE.md",
    "LICENSE",
    "LICENSE-DATA",
    "NOTICE",
    "CITATION.cff",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements-lock.txt",
}
PUBLIC_CODE_SCRIPTS = {
    "build_public_data.py",
    "build_release.py",
    "evaluate.py",
    "export_owner_review.py",
    "export_review_packet.py",
    "import_owner_signoff.py",
    "import_review.py",
    "materialize_review.py",
    "release_gate.py",
    "run_benchmark.py",
    "smoke.py",
    "upload_hf_data.py",
    "validate.py",
    "verify_release_bundle.py",
}
PUBLIC_CODE_TESTS = {"test_public_contract.py"}


class ReleaseBundleError(ValueError):
    """Raised when a bundle cannot be built or fails independent verification."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path, relative: str, *, include_assets: bool) -> List[Tuple[Path, str]]:
    base = root / relative
    if not base.exists():
        return []
    if base.is_file():
        return [(base, relative)] if include_assets or "/assets/" not in "/" + relative else []
    files: List[Tuple[Path, str]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not include_assets and ("/assets/" in "/" + rel or rel.startswith("data/") and rel.endswith(".pdf")):
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        files.append((path, rel))
    return files


def _target_for_profile(profile: str) -> str:
    if profile == "public-candidate":
        return "public"
    if profile == "public-code":
        return "public-code"
    return "internal"


def _public_code_files(project_root: Path) -> List[Tuple[Path, str]]:
    files: List[Tuple[Path, str]] = []
    for relative in sorted(PUBLIC_CODE_EXACT_FILES):
        path = project_root / relative
        if path.is_file():
            files.append((path, relative))
    for relative in PUBLIC_CODE_README_FILES:
        path = project_root / relative
        if path.is_file():
            files.append((path, relative))
    package_root = project_root / "gaokao_bench"
    for path in sorted(package_root.glob("*.py")):
        if path.is_file():
            files.append((path, path.relative_to(project_root).as_posix()))
    for name in sorted(PUBLIC_CODE_SCRIPTS):
        path = project_root / "scripts" / name
        if path.is_file():
            files.append((path, "scripts/" + name))
    for name in sorted(PUBLIC_CODE_TESTS):
        path = project_root / "tests" / name
        if path.is_file():
            files.append((path, "tests/" + name))
    readme_assets = project_root / PUBLIC_CODE_README_ASSET_DIR
    if readme_assets.is_dir():
        for path in sorted(readme_assets.glob("*.svg")):
            if path.is_file():
                files.append((path, path.relative_to(project_root).as_posix()))
    return files


def _public_code_path_allowed(rel: str) -> bool:
    if rel in PUBLIC_CODE_README_FILES or rel in PUBLIC_CODE_EXACT_FILES:
        return True
    if PurePosixPath(rel).parent.as_posix() == "gaokao_bench" and rel.endswith(".py"):
        return True
    if rel.startswith("scripts/") and PurePosixPath(rel).name in PUBLIC_CODE_SCRIPTS:
        return PurePosixPath(rel).parent.as_posix() == "scripts"
    if rel.startswith("tests/") and PurePosixPath(rel).name in PUBLIC_CODE_TESTS:
        return PurePosixPath(rel).parent.as_posix() == "tests"
    if rel.startswith(PUBLIC_CODE_README_ASSET_DIR + "/") and rel.endswith(".svg"):
        return True
    return False


def _bundle_manifest(
    *,
    profile: str,
    target: str,
    gate: Dict[str, Any],
    files: Sequence[Tuple[Path, str]],
    include_assets: bool,
    created_at: str,
) -> Dict[str, Any]:
    file_rows = [
        {"path": rel, "sha256": _sha256_file(path), "bytes": path.stat().st_size}
        for path, rel in files
    ]
    if target == "public-code":
        artifact_scope = "code_and_project_original_documentation"
    elif target == "public":
        artifact_scope = "full_dataset_with_assets" if include_assets else "full_dataset_without_assets"
    else:
        artifact_scope = "internal_dataset_with_local_assets"
    return {
        "schema_version": "2.0",
        "created_at": created_at,
        "profile": profile,
        "target": target,
        "release_status": gate["status"],
        "dataset_version": gate["dataset_version"],
        "harness_version": gate["harness_version"],
        "scorer_version": gate["scorer_version"],
        "prompt_version": gate["prompt_version"],
        "dataset_manifest_sha256": gate["dataset_manifest_sha256"],
        "artifact_scope": artifact_scope,
        "includes_assets": include_assets,
        "files": file_rows,
        "gate": gate,
        "notes": [
            "Internal candidate bundles include local assets; public candidate bundles exclude assets.",
            "A blocked bundle is evidence for review only and must never be presented as a released benchmark.",
        ],
    }


def build_release_bundle(
    root: Path | str,
    *,
    profile: str,
    data_dir: Path | str,
    output_dir: Path | str,
    allow_blocked: bool = False,
    gate_kwargs: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate the gate, select allowed files, and write a deterministic ZIP."""

    if profile not in PROFILES:
        raise ReleaseBundleError("unsupported profile: %s" % profile)
    project_root = Path(root).resolve()
    source_data = Path(data_dir)
    if not source_data.is_absolute():
        source_data = project_root / source_data
    target = _target_for_profile(profile)
    gate = evaluate_release_gate(
        source_data,
        target=target,
        repo_root=project_root,
        **(gate_kwargs or {}),
    )
    if gate["status"] != "pass" and not allow_blocked:
        raise ReleaseBundleError(json.dumps(gate, ensure_ascii=False, sort_keys=True))

    include_assets = target == "internal" or (target == "public" and gate["status"] == "pass")
    if target == "public-code":
        files = _public_code_files(project_root)
    else:
        allowed = [
            "README.md",
            "CHANGELOG.md",
            "LICENSE",
            "LICENSE-DATA",
            "NOTICE",
            "CITATION.cff",
            "pyproject.toml",
            "requirements-dev.txt",
            "requirements-lock.txt",
            "Makefile",
            "gaokao_bench",
            "scripts",
            "tests",
            "docs",
            "review_ui",
            "release",
            source_data.relative_to(project_root).as_posix(),
        ]
        files = []
        for relative in allowed:
            files.extend(_iter_files(project_root, relative, include_assets=include_assets))
        files = [
            (path, rel)
            for path, rel in files
            if not rel.startswith(("runs/", "review_packets/", "reports/", "dist/"))
        ]
    if target == "public-code":
        forbidden = [
            rel
            for _, rel in files
            if not _public_code_path_allowed(rel)
            or rel.startswith("data/")
            or "/assets/" in "/" + rel
            or rel.lower().endswith((".pdf", ".png", ".jpg", ".jpeg"))
        ]
        if forbidden:
            raise ReleaseBundleError("public-code bundle contains restricted material: %s" % ", ".join(forbidden[:5]))
    if target == "public":
        if gate["status"] == "pass" and not include_assets:
            raise ReleaseBundleError("a passing public bundle must include assets covered by the rights attestation")
        forbidden = [rel for _, rel in files if rel.lower().endswith(".pdf")]
        if forbidden:
            raise ReleaseBundleError("public bundle contains excluded PDFs: %s" % ", ".join(forbidden[:5]))

    manifest = _bundle_manifest(
        profile=profile,
        target=target,
        gate=gate,
        files=files,
        include_assets=include_assets,
        created_at=created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    )
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    suffix = "-blocked" if gate["status"] != "pass" else ""
    artifact_prefix = "gaokao-bench-code" if target == "public-code" else "gaokao-bench-data"
    output = output_root / (
        "%s-%s-harness-%s-%s%s.zip"
        % (artifact_prefix, gate["dataset_version"], gate["harness_version"], profile, suffix)
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, rel in files:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        archive.writestr(zipfile.ZipInfo("release.json", date_time=(1980, 1, 1, 0, 0, 0)), payload)

    checksum = _sha256_file(output)
    sidecar = output.with_suffix(output.suffix + ".sha256")
    sidecar.write_text("%s  %s\n" % (checksum, output.name), encoding="utf-8")
    verification = verify_release_bundle(output)
    return {
        "output": str(output.resolve()),
        "sha256": checksum,
        "profile": profile,
        "target": target,
        "release_status": gate["status"],
        "files": len(files) + 1,
        "include_assets": include_assets,
        "verification": verification,
        "sidecar": str(sidecar.resolve()),
    }


def _safe_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name or name.endswith("/"):
        raise ReleaseBundleError("unsafe bundle member: %r" % name)
    return path.as_posix()


def verify_release_bundle(path: Path | str, *, sidecar: Optional[Path | str] = None) -> Dict[str, Any]:
    """Recompute and verify every file in a built ZIP."""

    bundle_path = Path(path)
    if not bundle_path.is_file():
        raise ReleaseBundleError("bundle does not exist: %s" % bundle_path)
    with zipfile.ZipFile(bundle_path, "r") as archive:
        names = [_safe_member_name(info.filename) for info in archive.infolist()]
        if len(names) != len(set(names)):
            raise ReleaseBundleError("bundle contains duplicate member names")
        if "release.json" not in names:
            raise ReleaseBundleError("bundle is missing release.json")
        manifest = json.loads(archive.read("release.json").decode("utf-8"))
        declared = manifest.get("files")
        if not isinstance(declared, list) or not declared:
            raise ReleaseBundleError("release.json has no file manifest")
        declared_rows: Dict[str, Dict[str, Any]] = {}
        for row in declared:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise ReleaseBundleError("release.json contains an invalid file row")
            rel = _safe_member_name(row["path"])
            if rel in declared_rows:
                raise ReleaseBundleError("release.json contains duplicate paths: %s" % rel)
            declared_rows[rel] = row
        actual_names = set(names) - {"release.json"}
        if actual_names != set(declared_rows):
            missing = sorted(set(declared_rows) - actual_names)
            extra = sorted(actual_names - set(declared_rows))
            raise ReleaseBundleError("bundle file set mismatch; missing=%s extra=%s" % (missing[:5], extra[:5]))
        for rel, row in declared_rows.items():
            payload = archive.read(rel)
            if len(payload) != int(row.get("bytes", -1)):
                raise ReleaseBundleError("size mismatch for %s" % rel)
            if _sha256_bytes(payload) != row.get("sha256"):
                raise ReleaseBundleError("sha256 mismatch for %s" % rel)

    target = manifest.get("target")
    if target == "public-code":
        disallowed = [rel for rel in declared_rows if not _public_code_path_allowed(rel)]
        if disallowed:
            raise ReleaseBundleError(
                "public-code bundle contains files outside the curated allowlist: %s"
                % ", ".join(disallowed[:5])
            )
        forbidden = [
            rel
            for rel in declared_rows
            if rel.startswith(("data/", "scripts/evidence/"))
            or "/assets/" in "/" + rel
            or rel.lower().endswith((".pdf", ".png", ".jpg", ".jpeg"))
        ]
        if forbidden:
            raise ReleaseBundleError("public-code bundle contains restricted files: %s" % ", ".join(forbidden[:5]))
        markers = (
            b"ANSWER_" + b"SPECS",
            b"POINT_" + b"OVERRIDES",
            b"src-eol-" + b"2025",
            b"src-zizzs-" + b"2025",
            b"gaokao." + b"eol.cn",
            b"zizzs." + b"com",
            b"2025_math_" + b"answer_sources",
            b"2025_math_" + b"independent_answer_sources",
        )
        with zipfile.ZipFile(bundle_path, "r") as archive:
            leaked = [
                rel
                for rel in declared_rows
                if any(marker in archive.read(rel) for marker in markers)
            ]
        if leaked:
            raise ReleaseBundleError(
                "public-code bundle contains internal answer-build/evidence markers: %s"
                % ", ".join(leaked[:5])
            )
        required = {
            "README.md",
            "README.zh-CN.md",
            "SCOPE.md",
            "LICENSE",
            "LICENSE-DATA",
            "NOTICE",
        }
        missing_required = sorted(required - set(declared_rows))
        if missing_required:
            raise ReleaseBundleError(
                "public-code bundle is missing required public files: %s"
                % ", ".join(missing_required)
            )
    if target == "public":
        if manifest.get("release_status") == "pass" and manifest.get("includes_assets") is not True:
            raise ReleaseBundleError("a passing public bundle must include assets covered by the rights attestation")
        forbidden = [rel for rel in declared_rows if rel.lower().endswith(".pdf")]
        if forbidden:
            raise ReleaseBundleError("public bundle contains excluded PDFs: %s" % ", ".join(forbidden[:5]))

    checksum_path = Path(sidecar) if sidecar else bundle_path.with_suffix(bundle_path.suffix + ".sha256")
    if not checksum_path.is_file():
        raise ReleaseBundleError("bundle checksum sidecar is missing: %s" % checksum_path)
    expected = checksum_path.read_text(encoding="utf-8").split()[0]
    if expected != _sha256_file(bundle_path):
        raise ReleaseBundleError("bundle checksum does not match sidecar")

    return {
        "valid": True,
        "bundle": str(bundle_path.resolve()),
        "sha256": _sha256_file(bundle_path),
        "profile": manifest.get("profile"),
        "target": target,
        "release_status": manifest.get("release_status"),
        "files": len(declared_rows) + 1,
        "artifact_scope": manifest.get("artifact_scope"),
        "includes_assets": manifest.get("includes_assets"),
    }
