# Repository and Public-Code Release Scope

This document distinguishes the development source repository from the
`public-code` release artifact. Both are public, but they do not contain the
same files.

## Source repository

The source repository tracks the harness, tests, release tools, dataset
builders, acceptance-page tooling, and benchmark specs needed to reproduce and
review dataset candidates. The 2026 National I mathematics builder and spec are
intentionally tracked:

- `scripts/build_2026_n1.py`
- `scripts/build_2026_n1_acceptance.py`
- `specs/2026-math-n1.json`

The 2026 spec contains question text, answers, point metadata, and source
locators. It is dataset source material, released under CC BY 4.0 together with
the generated data package. Generated `data/`, local `reports/`, caches, and
review packets are ignored by Git and distributed through immutable dataset
versions rather than through the source checkout.

## Included in the public-code artifact

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
gaokao-bench fetch-data --version 0.0.4
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

## Excluded from the public-code artifact

- exam stems, options, answer keys, per-item points, and answer-audit records;
- source PDFs, page images, crops, screenshots, document previews, and OCR
  evidence;
- internal alpha-corpus builders, answer specs, evidence caches, review
  packets, and reports; these may remain in the source repository when they are
  required for reproducible dataset review, but are not copied into the
  `public-code` artifact;
- changelogs or dashboards that quote item-level answers.

The public-code build uses a curated allowlist. The independent bundle verifier
rejects files outside that allowlist and scans for internal answer-build/source
markers. This is intentionally stricter than a directory-level exclusion and
does not restrict what the source repository may track for dataset review.

## Rights

The software is Apache-2.0 and project-authored documentation is CC BY 4.0.
Dataset packages from version 0.0.3 onward are licensed as a whole under CC BY
4.0, including question text, answers, figures, selection, schema, and scoring
metadata. Source attribution and correction paths are retained in each package.

## Contamination

The 2025 and 2026 Gaokao papers are public and may exist in model training or
retrieval corpora. Any separately distributed 2025 result must be labelled
`retrospective_public_2025`; the 2026 candidate uses
`retrospective_public_2026`. Neither may claim freshness, held-out status, or
absence of contamination. A freshness claim requires a separately controlled
held-out set.

## Answer review

The code supports media cross-checking and independent human review. It does not
certify an exam-authority answer source. Dataset `0.0.4` uses a declared
`owner_attested_single_review` protocol. The 2026 National I candidate uses three
independent audits followed by an explicit owner signoff; it remains an
acceptance candidate until that signoff is recorded. The package records the
source and review limitations explicitly.
