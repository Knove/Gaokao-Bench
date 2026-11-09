#!/usr/bin/env python3
"""Publish a verified public data package to Hugging Face."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.scoring import PUBLIC_PACKAGE_PROFILE, load_dataset, validate_dataset  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(data_dir: Path, repo_id: str) -> Dict[str, Any]:
    dataset = load_dataset(data_dir)
    manifest = dataset.manifest
    if manifest.get("package_profile") != PUBLIC_PACKAGE_PROFILE:
        raise ValueError("only a public-data package can be uploaded")
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    if release.get("repo_id") != repo_id:
        raise ValueError(
            "manifest repo_id %s does not match --repo-id %s"
            % (release.get("repo_id"), repo_id)
        )
    audit = validate_dataset(dataset)
    return {
        "dataset_version": manifest.get("dataset_version"),
        "repo_id": repo_id,
        "path": str(data_dir),
        "files": len(manifest.get("files", {})),
        "manifest_sha256": _sha256(data_dir / "manifest.json"),
        "audit": audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default="0.0.1",
        help="dataset version under dist/public-data (default: 0.0.1)",
    )
    parser.add_argument(
        "--data-dir",
        help="explicit package directory; overrides the directory derived from --version",
    )
    parser.add_argument("--repo-id", default="Knove/Gaokao-Bench")
    parser.add_argument("--tag", help="dataset tag to create after upload (default: v<version>)")
    parser.add_argument("--skip-tag", action="store_true")
    parser.add_argument("--replace", action="store_true", help="delete stale files on the main branch")
    parser.add_argument(
        "--commit-message",
        default="Publish Gaokao-Bench 0.0.1 public data",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the upload; without this flag the command is a dry run",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", args.version):
        print("invalid dataset version: %s" % args.version, file=sys.stderr)
        return 2
    data_dir = (
        (ROOT / args.data_dir).resolve()
        if args.data_dir
        else (ROOT / "dist" / "public-data" / args.version).resolve()
    )
    try:
        summary = _summary(data_dir, args.repo_id)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    version = str(summary["dataset_version"])
    tag = args.tag or ("v%s" % version)
    planned = {
        "dry_run": not args.execute,
        **summary,
        "upload": {
            "repo_type": "dataset",
            "repo_id": args.repo_id,
            "replace": args.replace,
            "commit_message": args.commit_message,
        },
        "tag": None if args.skip_tag else tag,
    }
    if not args.execute:
        print(json.dumps(planned, ensure_ascii=False, indent=2, sort_keys=True))
        print("dry run only; rerun with --execute after `hf auth login`", file=sys.stderr)
        return 0

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print(
            'huggingface_hub is required; install with python3 -m pip install "gaokao-bench[data]"',
            file=sys.stderr,
        )
        return 2

    api = HfApi()
    try:
        identity = api.whoami()
    except Exception as exc:
        print("Hugging Face authentication failed; run `hf auth login` first: %s" % exc, file=sys.stderr)
        return 2

    try:
        commit = api.upload_folder(
            repo_id=args.repo_id,
            repo_type="dataset",
            folder_path=str(data_dir),
            commit_message=args.commit_message,
            ignore_patterns=[".cache/**", ".git/**", "**/__pycache__/**", "**/*.pyc"],
            delete_patterns=["*"] if args.replace else None,
        )
    except Exception as exc:
        print("Hugging Face upload failed: %s" % exc, file=sys.stderr)
        return 2

    tag_status = "skipped"
    if not args.skip_tag:
        try:
            refs = api.list_repo_refs(repo_id=args.repo_id, repo_type="dataset")
            existing_tags = {ref.name for ref in refs.tags}
            if tag in existing_tags:
                tag_status = "already_exists"
            else:
                api.create_tag(
                    repo_id=args.repo_id,
                    repo_type="dataset",
                    tag=tag,
                    tag_message="Gaokao-Bench %s public data release" % version,
                )
                tag_status = "created"
        except Exception as exc:
            print("Upload succeeded, but tag creation failed: %s" % exc, file=sys.stderr)
            tag_status = "failed"

    try:
        info = api.dataset_info(args.repo_id)
        remote = {
            "sha": getattr(info, "sha", None),
            "tags": list(getattr(info, "tags", []) or []),
            "card_data": getattr(info, "card_data", None),
        }
    except Exception as exc:
        remote = {"verification_error": str(exc)}

    result = {
        "uploaded": True,
        "authenticated_as": identity.get("name"),
        "repo_id": args.repo_id,
        "repo_url": "https://huggingface.co/datasets/%s" % args.repo_id,
        "dataset_version": version,
        "manifest_sha256": summary["manifest_sha256"],
        "commit_oid": getattr(commit, "oid", None),
        "commit_url": getattr(commit, "commit_url", None),
        "tag": None if args.skip_tag else tag,
        "tag_status": tag_status,
        "remote": remote,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if tag_status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

