# Code-Only Release Scope

This document applies to the `public-code` artifact and the public source
repository.

## Included

- Python harness, scorer, validation, review-import, materialisation, and
  release-gate code;
- public package builder and Hugging Face upload tooling;
- package metadata, licenses, and a small contract test suite;
- project-authored documentation, translations, and theme-aware SVG graphics;
- a clear statement of the public software boundary.

## Companion data distribution

The `public-code` artifact remains code-only. The separately versioned
objective-question package is published at
<https://huggingface.co/datasets/Knove/Gaokao-Bench> and is fetched by release
tag:

```bash
gaokao-bench fetch-data --version 0.0.2
```

The public data package carries its own manifest, file hashes, third-party
notice, and pinned citation. It is not part of the Apache-2.0 code bundle.

Maintainer release flow for every new version:

1. Build the new immutable directory with `scripts/build_public_data.py`.
2. Validate every file and hash with `scripts/validate.py`.
3. Upload that version directory with `scripts/upload_hf_data.py --version <version> --execute`.
4. Let the uploader create the matching `v<version>` tag.

Do not edit a published version directory in place. Add a new version instead.

The harness also contains reviewed crop coordinates and pixel hashes for the
four 2025 figure prompts. Those constants are project-authored calibration
metadata used to reproduce a sanitised prompt; they are not question stems,
options, answer keys, source images, or a dataset license grant.

## Excluded

- exam stems, options, answer keys, per-item points, and answer-audit records;
- source PDFs, page images, crops, screenshots, document previews, and OCR
  evidence;
- internal alpha-corpus builders, answer specs, evidence caches, review
  packets, and reports;
- changelogs or dashboards that quote item-level answers.

The code-only build uses a curated allowlist. The independent bundle verifier
rejects files outside that allowlist and scans for internal answer-build/source
markers. This is intentionally stricter than a directory-level exclusion.

## Rights

The software is Apache-2.0 and project-authored documentation is CC BY 4.0.
Third-party exam material remains outside this artifact and is not relicensed by
the project. The public data package uses layered data terms: project-authored
selection and scoring metadata are CC BY 4.0, while exam material remains
`NOASSERTION`. Its public distribution records the owner's release decision and
retains source links, correction, and takedown paths.

## Contamination

The 2025 Gaokao papers are public and may exist in model training or retrieval
corpora. Any separately distributed 2025 dataset result must be labelled
`retrospective_public_2025`; it must not claim freshness, held-out status, or
absence of contamination. A freshness claim requires a separately controlled
held-out set.

## Answer review

The code supports media cross-checking and independent human review. It does not
certify an exam-authority answer source. The materialiser only writes a new
dataset version after an owner and two distinct independent reviewers complete
every answer and point row without unresolved disputes or proposed changes.
