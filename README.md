<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/logo-dark.svg">
    <img src=".github/readme/logo-light.svg" width="360" alt="Gaokao-Bench logo">
  </picture>
</p>

<h1 align="center">Gaokao-Bench</h1>

<p align="center"><strong>Real exam papers. Fixed denominators. Reproducible scores.</strong></p>

<p align="center">A deterministic, point-weighted benchmark for evaluating AI systems<br>on Chinese Gaokao objective questions.</p>

<p align="center">English&nbsp;&nbsp;·&nbsp;&nbsp;<a href="README.zh-CN.md">简体中文</a></p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-2E7D32?style=flat-square"></a>
  <a href="CITATION.cff"><img alt="Version 0.0.1" src="https://img.shields.io/badge/version-0.0.1-171513?style=flat-square"></a>
  <a href="SCOPE.md"><img alt="Code-only distribution" src="https://img.shields.io/badge/scope-code--only-C83D2B?style=flat-square"></a>
  <a href="#scoring-contract"><img alt="Deterministic scoring" src="https://img.shields.io/badge/scoring-deterministic-20786A?style=flat-square"></a>
</p>

<p align="center">
  <a href="#why-gaokao-bench"><strong>Why Gaokao-Bench</strong></a> &nbsp;·&nbsp;
  <a href="#benchmark-at-a-glance"><strong>Benchmark</strong></a> &nbsp;·&nbsp;
  <a href="#how-it-works"><strong>How it works</strong></a> &nbsp;·&nbsp;
  <a href="#quick-start"><strong>Quick start</strong></a> &nbsp;·&nbsp;
  <a href="#scoring-contract"><strong>Scoring</strong></a> &nbsp;·&nbsp;
  <a href="#code-only-scope"><strong>Scope</strong></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/hero-dark.svg">
    <img src=".github/readme/hero-light.svg" width="100%" alt="Gaokao-Bench: real exams, fixed denominators, reproducible scores">
  </picture>
</p>

> [!IMPORTANT]
> **This repository is code-only.** Exam questions, answer keys, source PDFs, page images, crops, review packets, and answer evidence are distributed separately. The software license does not relicense third-party exam material. See [`SCOPE.md`](SCOPE.md).

Gaokao-Bench treats the exam paper as a measurement instrument, not a prompt collection. Each scored item has a reviewed point value, a canonical answer path, and a fixed place in the denominator. A run can be replayed, a submitted answer can be re-parsed, and every failure remains visible in the score.

The project is built around a simple rule: **a benchmark score should survive re-running, re-parsing, and re-auditing.**

## Why Gaokao-Bench?

| Common benchmark failure | Gaokao-Bench response |
| --- | --- |
| An LLM judge adds another model to the measurement | Deterministic parsers and canonical validators; no judge in the scoring path |
| Percentages hide unequal item values | Score points, not only item counts; the denominator is the sum of reviewed points |
| Failed calls are dropped from the denominator | Missing, malformed, timed-out, and provider-error responses remain as zero |
| Answers cannot be traced to evidence | Source, answer audit, point audit, crop hashes, and review status travel with each item |
| Public questions invite contamination claims | 2025 results must be labelled `retrospective_public_2025`; no freshness claim is allowed |
| A score has no version context | A run binds dataset, harness, scorer, prompt, model snapshot, and repeat count |

## Benchmark at a glance

These are the statistics for the companion **0.0.1 dataset**. The dataset is intentionally not part of this repository.

| | 0.0.1 objective scope |
| --- | --- |
| Year and subject | 2025 · Mathematics |
| Papers | 5: National I, National II, Beijing, Shanghai, Tianjin |
| Scored items | **74** — 39 single choice, 6 multiple choice, 29 fill-in |
| Scored points | **353** |
| Answer slots | **78** |
| Figure-required items | **4** |
| Text profile | **70 items / 334 points** |
| Vision profile | **74 items / 353 points** |
| Catalogued but not scored | 26 subjective items / 397 points |
| Official answer source | Not claimed; review workflow is recorded in the data package |

The 353-point denominator is the sum of reviewed per-question points. It is **not** a linear rescaling of the 750-point paper total, and it must not be presented as one.

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/pipeline-dark.svg">
    <img src=".github/readme/pipeline-light.svg" width="100%" alt="Gaokao-Bench pipeline: a versioned data package is validated, turned into anonymous prompts, sent to a text or vision model, and scored deterministically; release integrity covers audits, human review, immutable versions, and release gates">
  </picture>
</p>

Two paths are deliberately separate:

- **Evaluation** is deterministic. The model sees one anonymous item at a time; the scorer sees structured answers and reviewed points.
- **Release** is human-reviewed. Source evidence, independent derivation, media cross-checks, owner review, and two independent reviewers must close before a new immutable data version is materialised.

The harness does not send the year, paper name, question number, `question_id`, source, or answer to the provider. It asks for a small JSON object and scores the response outside the model.

## Quick start

### 1. Install from source

```bash
git clone <repository-url>
cd gaokao-bench

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

python -m pytest -q
```

Use `.[harness]` instead of `.[dev]` when only the vision dependencies are needed. Python 3.9 or newer is required.

### 2. Obtain a reviewed data package

The code-only repository does not fetch a dataset silently. Point the tools at a separately versioned package:

```text
gaokao-bench-data/
  manifest.json
  papers.jsonl
  questions.jsonl
  answer_keys.jsonl
  assets/
  ...
```

The package owns its own manifest, review lineage, rights notes, and content hashes. Do not mix files from different dataset versions.

### 3. Validate and score

```bash
export GB_DATA=/path/to/gaokao-bench-data

python3 scripts/validate.py --data-dir "$GB_DATA"
python3 scripts/evaluate.py \
  --data-dir "$GB_DATA" \
  --answers submission.json
```

The installed CLI exposes the same core operations:

```bash
gaokao-bench validate --data-dir "$GB_DATA"
gaokao-bench evaluate --data-dir "$GB_DATA" --answers submission.json
```

A submission is keyed by question ID:

```json
{
  "answers": {
    "<question-id>": "C",
    "<multi-choice-id>": "ACD",
    "<two-slot-fill-id>": ["2", "π/4"]
  }
}
```

### 4. Run the benchmark harness

Model calls require an explicit provider choice and API credentials in the environment. Credentials are not written into reports.

```bash
export OPENAI_API_KEY=...

python3 scripts/run_benchmark.py \
  --data-dir "$GB_DATA" \
  --provider openai_compatible \
  --model "<model-snapshot>" \
  --api-key-env OPENAI_API_KEY \
  --modality vision \
  --repeats 3 \
  --temperature 0 \
  --output-dir runs/<run-id>
```

Supported providers are `anthropic`, `openai_compatible`, `gemini_cli`, and `mock`. Use `--prepare-only` to build and audit prompts without making a model call.

## Scoring contract

```text
score = earned_points / fixed_points
```

| Rule | Behavior |
| --- | --- |
| Item credit | All-or-nothing by default; every answer slot must match |
| Point weighting | Each item contributes its reviewed point value |
| Failure handling | Missing, malformed, timeout, and provider error are scored `0` |
| Denominator | Never shrinks; failures stay inside the fixed total |
| Parsing | Deterministic math normalisation and restricted custom validators |
| Judge | No LLM-as-a-judge in the scoring path |
| Reporting | Item accuracy, exact accuracy, point accuracy, and per-paper breakdowns |

Two profiles use the same scorer:

| Profile | What changes | Primary view | Full denominator |
| --- | --- | --- | --- |
| Text | Four figure-required items are not sent to the provider and score `0` | **70 items / 334 points** | **353 points** |
| Vision | Adds only the four required figure crops | **74 items / 353 points** | **353 points** |

Partial credit for multi-select or multi-blank questions is not enabled in 0.0.1. A paper score is never extrapolated to a full 750-point Gaokao score.

## Reproducible by construction

Every evaluation run records:

- dataset version and manifest SHA-256;
- harness, scorer, and prompt versions;
- provider, model snapshot, base URL category, and generation settings;
- repeat count, timeout, retry policy, concurrency, and per-prompt hashes;
- raw responses, parse status, awarded points, and failure reason.

The default protocol is independent, single-item requests at temperature `0`, repeated three times. A failed request is retained as a zero, not removed from the sample.

## Review and release integrity

The data workflow is designed to make silent corrections difficult:

1. **Source register** records where each paper and answer claim came from.
2. **Question audit** checks that the stem, options, and source actually agree.
3. **Answer audit** combines independent derivation with media or document cross-checks.
4. **Point audit** reconstructs each paper's scoring structure.
5. **Owner review** signs the adjudicated answer, points, and source locator.
6. **Two independent reviewers** review every answer and point row.
7. **Materialisation** writes a new immutable dataset version with a review ledger.
8. **Release gates** check internal, code-only, and full-public targets separately.

Review imports are read-only. The materialiser refuses proposed changes, unresolved disputes, incomplete rows, stale hashes, and missing signatures.

## Code-only scope

This repository contains the evaluation toolkit:

- Python scorer and parsers;
- text and required-vision harness;
- review-import and materialisation workflow;
- release gates and bundle verifier;
- public contract tests, licenses, and project-authored documentation;
- original README graphics and translations.

It does **not** contain:

- exam stems, options, answer keys, or per-item points;
- source PDFs, page images, crops, screenshots, or document previews;
- answer evidence, source registers, owner packets, or review reports;
- internal dataset builders or item-level changelogs.

The boundary is enforced by a curated release allowlist and an independent bundle verifier. See [`SCOPE.md`](SCOPE.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `gaokao_bench/` | Core scoring, harness, review, materialisation, and release logic |
| `scripts/` | CLI entry points for validation, scoring, benchmarks, and release checks |
| `tests/` | Public contract tests that do not require exam data |
| `.github/readme/` | Project-authored, theme-aware README graphics |
| `SCOPE.md` | Code/data boundary, rights position, and contamination policy |
| `CITATION.cff` | Machine-readable citation metadata |

## Status and roadmap

`0.0.1` proves the measurement loop on a narrow, point-audited scope:

- 2025 mathematics objective questions;
- five papers;
- 74 scored items and 353 fixed points;
- text and required-vision profiles;
- deterministic scoring and auditable release flow.

The code-only release does not certify the companion dataset as a public leaderboard. Full-data publication remains a separate decision under the release gates.

The long-term direction is intentionally incremental:

1. Expand mathematical objective coverage across historical papers.
2. Add other Gaokao subjects without weakening the evidence and point-audit contract.
3. Study subjective scoring separately with rubrics, human adjudication, and explicit uncertainty.
4. Keep code, data rights, and benchmark claims as independent release decisions.

New subjects are not added by scraping first and cleaning later. A subject enters the benchmark only when its source, answer, point, and review workflow can close.

## Responsible use

- **Contamination:** questions from 2025 are public and may exist in training or retrieval corpora. Label results `retrospective_public_2025`; do not claim held-out or fresh ability.
- **Score context:** always report dataset, harness, scorer, prompt, model snapshot, modality, and repeats with a score.
- **Rights:** the Apache-2.0 software license does not grant rights to exam text, answer pages, PDFs, or images.
- **Coverage:** one subject and one year validate the methodology, not general Gaokao ability.
- **Failures:** a provider error is still zero. Do not remove failed items when comparing models.

## Contributing

Contributions are welcome when they preserve the measurement contract:

- do not add third-party exam questions, answer pages, PDFs, or crops to code pull requests;
- add a focused public test for parser, scorer, or release-boundary changes;
- run `python -m pytest -q` before opening a pull request;
- update both `README.md` and `README.zh-CN.md` when user-facing behavior changes;
- describe any change that affects a score, denominator, prompt, or review rule explicitly.

English is the canonical README. Simplified Chinese is maintained in-repo. Additional translations are welcome when a native-speaking maintainer can keep them in sync.

## Citation

If Gaokao-Bench contributes to your research or evaluation work, cite the repository and the exact dataset version:

```bibtex
@software{gaokao_bench_2026,
  author       = {{Gaokao-Bench contributors}},
  title        = {Gaokao-Bench},
  year         = {2026},
  version      = {0.0.1},
  license      = {Apache-2.0},
  url          = {<repository-url>}
}
```

Machine-readable metadata is available in [`CITATION.cff`](CITATION.cff).

## License

| Layer | License |
| --- | --- |
| Code, scorer, harness, and tests | **Apache-2.0** — see [`LICENSE`](LICENSE) |
| Project-authored documentation and metadata | **CC BY 4.0** — see [`LICENSE-DATA`](LICENSE-DATA) |
| Exam questions, answer pages, PDFs, images, and other third-party material | **Not included; not relicensed here** |

---

<p align="center"><strong>Measure the paper, not the prompt.</strong></p>
