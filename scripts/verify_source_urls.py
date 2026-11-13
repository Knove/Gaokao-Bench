#!/usr/bin/env python3
"""Verify that every frozen source URL still resolves to the recorded bytes.

This is an online acceptance check, separate from the offline data validator.
It records the final URL, redirect chain, content type, byte count and SHA-256,
then fails if any response differs from the frozen evidence manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_SPEC = ROOT / "specs" / "2026-math-n1.source-audit.json"
DEFAULT_PAGES_SPEC = ROOT / "specs" / "2026-math-n1.source-pages.json"


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object: %s" % path)
    return value


def _media_type_matches(payload: bytes, declared: str) -> bool:
    if declared == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if declared == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if declared == "image/gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    if declared == "application/pdf":
        return payload.startswith(b"%PDF-")
    if declared == "text/html":
        prefix = payload[:512].lstrip().lower()
        return prefix.startswith((b"<!doctype html", b"<html"))
    return True


def verify(
    *,
    audit_spec: Mapping[str, Any],
    pages_spec: Mapping[str, Any],
    source_ids: set[str],
    timeout: float,
) -> Dict[str, Any]:
    sources = {
        str(source["id"]): source
        for source in audit_spec.get("sources", [])
        if isinstance(source, Mapping) and source.get("id")
    }
    rows: List[Dict[str, Any]] = []
    failures = 0
    exact_hash_verified = 0
    availability_only = 0
    for source_id, entries in pages_spec.get("sources", {}).items():
        if source_ids and str(source_id) not in source_ids:
            continue
        source = sources.get(str(source_id), {})
        referer = str(source.get("url") or "")
        exact_hash = source.get("live_hash_stable") is not False
        for entry in entries:
            url = str(entry["url"])
            record: Dict[str, Any] = {
                "source_id": str(source_id),
                "file": str(entry["file"]),
                "url": url,
                "expected_sha256": str(entry["sha256"]),
                "expected_bytes": int(entry["bytes"]),
                "expected_media_type": str(entry["media_type"]),
                "verification_mode": "exact_hash" if exact_hash else "availability_only",
            }
            try:
                response = requests.get(
                    url,
                    timeout=timeout,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
                        **({"Referer": referer} if referer else {}),
                    },
                )
                record.update(
                    {
                        "status_code": response.status_code,
                        "ok": response.ok,
                        "final_url": response.url,
                        "redirects": [item.url for item in response.history],
                        "content_type": response.headers.get("content-type", ""),
                        "bytes": len(response.content),
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                    }
                )
                if not response.ok:
                    record["error"] = "HTTP status is not successful"
                elif not _media_type_matches(response.content, str(entry["media_type"])):
                    record["error"] = "payload magic does not match frozen media type"
                elif exact_hash and len(response.content) != int(entry["bytes"]):
                    record["error"] = "byte count differs from frozen evidence"
                elif exact_hash and record["sha256"] != str(entry["sha256"]):
                    record["error"] = "SHA-256 differs from frozen evidence"
                record["hash_matches"] = (
                    len(response.content) == int(entry["bytes"])
                    and record["sha256"] == str(entry["sha256"])
                )
            except requests.RequestException as exc:
                record.update({"ok": False, "error": str(exc)})
            record["passed"] = "error" not in record
            failures += int(not record["passed"])
            if record["passed"]:
                if exact_hash:
                    exact_hash_verified += 1
                else:
                    availability_only += 1
            rows.append(record)
    return {
        "schema_version": "1.0",
        "dataset_version": pages_spec.get("dataset_version"),
        "checked": len(rows),
        "passed": len(rows) - failures,
        "failed": failures,
        "exact_hash_verified": exact_hash_verified,
        "availability_only": availability_only,
        "status": "pass" if failures == 0 else "fail",
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-spec", type=Path, default=DEFAULT_AUDIT_SPEC)
    parser.add_argument("--pages-spec", type=Path, default=DEFAULT_PAGES_SPEC)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    report = verify(
        audit_spec=_read_json(args.audit_spec.resolve()),
        pages_spec=_read_json(args.pages_spec.resolve()),
        source_ids=set(args.source_id),
        timeout=args.timeout,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checked": report["checked"],
                "passed": report["passed"],
                "failed": report["failed"],
                "report": str(args.report.resolve()) if args.report else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
