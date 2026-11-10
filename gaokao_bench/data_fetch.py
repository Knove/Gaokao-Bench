"""Download and verify versioned public Gaokao-Bench data packages."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .scoring import PUBLIC_PACKAGE_PROFILE, load_dataset, validate_dataset


DEFAULT_DATA_REPO_ID = "Knove/Gaokao-Bench"
DEFAULT_DATA_VERSION = "0.0.2"


class DataFetchError(RuntimeError):
    """Raised when a versioned public data package cannot be fetched or verified."""


def _manifest_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_public_package(target: Path, repo_id: str) -> Dict[str, Any]:
    dataset = load_dataset(target, validate=False)
    manifest = dataset.manifest
    if manifest.get("package_profile") != PUBLIC_PACKAGE_PROFILE:
        raise DataFetchError(
            "existing directory is not a public data package: %s" % target
        )
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    if release.get("repo_id") != repo_id:
        raise DataFetchError(
            "existing directory belongs to %s, not %s" % (release.get("repo_id"), repo_id)
        )
    audit = validate_dataset(dataset)
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    public_ranking_allowed = bool(release.get("public_ranking_allowed", False))
    return {
        "manifest_version": manifest.get("dataset_version"),
        "manifest_sha256": _manifest_sha256(target / "manifest.json"),
        "public_ranking_allowed": public_ranking_allowed,
        "exam_material_license": rights.get("exam_material_license", "NOASSERTION"),
        "rights_notice": (
            "public_ranking_allowed=false: do not use this dataset for a public leaderboard, "
            "vendor marketing, or comparative ranking without separate authorization"
            if not public_ranking_allowed
            else "public_ranking_allowed=true: public leaderboards and comparative ranking are allowed "
            "with version attribution; underlying exam-material rights still apply"
        ),
        "audit": audit,
    }


def fetch_data(
    *,
    version: str = DEFAULT_DATA_VERSION,
    data_dir: Path | str = "data",
    repo_id: str = DEFAULT_DATA_REPO_ID,
    revision: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Download one immutable dataset revision, then validate its hashes and schema."""

    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version):
        raise DataFetchError("invalid dataset version: %s" % version)
    if not repo_id or "/" not in repo_id:
        raise DataFetchError("invalid Hugging Face dataset repo id: %s" % repo_id)

    base = Path(data_dir)
    target = base / version
    target_revision = revision or ("v%s" % version)

    if target.exists() and not force:
        try:
            existing = _load_public_package(target, repo_id)
        except (DataFetchError, OSError, ValueError) as exc:
            raise DataFetchError(
                "target already exists and is not a verified public package: %s; "
                "use --force to replace it (%s)" % (target, exc)
            ) from exc
        return {
            "status": "already_present",
            "repo_id": repo_id,
            "revision": target_revision,
            "path": str(target.resolve()),
            **existing,
        }

    if target.exists():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    base.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise DataFetchError(
            "huggingface_hub is required for fetch-data; install it with "
            'python3 -m pip install "gaokao-bench[data]"'
        ) from exc

    staging_root = Path(tempfile.mkdtemp(prefix=".gaokao-bench-", dir=str(base)))
    staging_target = staging_root / version
    try:
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=target_revision,
            local_dir=str(staging_target),
        )
        shutil.rmtree(staging_target / ".cache", ignore_errors=True)
        verified = _load_public_package(staging_target, repo_id)
        staging_target.rename(target)
    except Exception as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        if isinstance(exc, DataFetchError):
            raise
        raise DataFetchError(
            "failed to download %s@%s: %s" % (repo_id, target_revision, exc)
        ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return {
        "status": "downloaded",
        "repo_id": repo_id,
        "revision": target_revision,
        "path": str(target.resolve()),
        **verified,
    }

