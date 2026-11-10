"""Build a sanitized public data package for Hugging Face distribution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .benchmark_spec import (
    BENCHMARK_SPEC_FILENAME,
    BenchmarkSpec,
    load_benchmark_spec,
    question_requires_vision,
    question_type,
)
from .scoring import (
    PUBLIC_HUB_MANAGED_FILES,
    PUBLIC_PACKAGE_PROFILE,
    load_dataset,
    validate_dataset,
)


SUBJECT_TAGS = {
    "数学": "mathematics",
    "物理": "physics",
    "化学": "chemistry",
    "生物": "biology",
    "语文": "chinese-language",
    "英语": "english-language",
    "历史": "history",
    "地理": "geography",
    "政治": "politics",
}


PUBLIC_QUESTION_FIELDS = (
    "id",
    "paper_id",
    "number",
    "display_number",
    "type",
    "task_type",
    "subject",
    "year",
    "stem",
    "content",
    "content_format",
    "options",
    "points",
    "answer_slot_count",
    "asset_dependency",
    "input_modalities",
    "vision_required",
    "text_sufficient",
    "image_path",
    "image_bytes",
    "image_sha256",
    "review_status",
    "schema_version",
)
PUBLIC_ANSWER_FIELDS = (
    "question_id",
    "paper_id",
    "kind",
    "canonical",
    "correct_options",
    "slots",
    "points",
    "custom_validator",
    "answer_status",
    "notes",
    "schema_version",
)
PUBLIC_PAPER_FIELDS = (
    "id",
    "title",
    "region",
    "subject",
    "year",
    "exam_system",
    "full_question_count",
    "full_paper_points",
    "objective_questions",
    "objective_points",
    "choice_questions",
    "multi_choice_questions",
    "fill_questions",
    "catalog_subjective_questions",
    "catalog_subjective_points",
    "source_id",
    "source_ref",
    "source_url",
    "pdf_url",
    "pdf_sha256",
    "schema_version",
)
PUBLIC_ASSET_FIELDS = (
    "question_id",
    "paper_id",
    "path",
    "bytes",
    "sha256",
    "dependency",
    "page_number",
)
PUBLIC_CATALOG_FIELDS = (
    "id",
    "paper_id",
    "number",
    "display_number",
    "type",
    "content",
    "points",
    "included_in_scoring",
    "deferred_reason",
    "reference_answer_seed",
    "source_id",
    "schema_version",
)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(payload, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_root(source: Path) -> Path:
    return source.parent.parent if source.parent.name == "data" else source.parent


def _project_question(row: Mapping[str, Any]) -> Dict[str, Any]:
    projected = {field: row[field] for field in PUBLIC_QUESTION_FIELDS if field in row}
    if not question_requires_vision(row):
        for field in ("image_path", "image_bytes", "image_sha256"):
            projected.pop(field, None)
    source = row.get("source")
    if isinstance(source, dict):
        projected["source"] = {
            field: source[field]
            for field in ("source_id", "source_url", "page_number", "source_pdf_sha256")
            if field in source
        }
    return projected


def _project_rows(rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> List[Dict[str, Any]]:
    return [{field: row[field] for field in fields if field in row} for row in rows]


def _public_audit(
    *,
    papers: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    spec: Optional[BenchmarkSpec] = None,
) -> Dict[str, Any]:
    paper_points = {
        str(paper["id"]): sum(
            int(question["points"])
            for question in questions
            if question.get("paper_id") == paper["id"]
        )
        for paper in papers
    }
    if spec is not None:
        choice_types = {name for name, shape in spec.task_types.items() if shape.shape == "choice"}
        single_choice_types = {
            name
            for name in choice_types
            if spec.task_types[name].max_selections == 1
        }
        multi_choice_types = choice_types - single_choice_types
        slot_types = {name for name, shape in spec.task_types.items() if shape.shape == "slots"}
    else:
        choice_types = {"single_choice", "multi_choice"}
        single_choice_types = {"single_choice"}
        multi_choice_types = {"multi_choice"}
        slot_types = {"fill_blank"}
    image_questions = [q for q in questions if question_requires_vision(q)]
    return {
        "papers": len(papers),
        "questions": len(questions),
        "choice_questions": len([q for q in questions if question_type(q) in choice_types]),
        "single_choice_questions": len([q for q in questions if question_type(q) in single_choice_types]),
        "multi_choice_questions": len([q for q in questions if question_type(q) in multi_choice_types]),
        "fill_questions": len([q for q in questions if question_type(q) in slot_types]),
        "answer_slots": sum(int(q.get("answer_slot_count") or 0) for q in questions),
        "objective_points": sum(int(q["points"]) for q in questions),
        "image_required_questions": len(image_questions),
        "text_points": sum(
            int(q["points"])
            for q in questions
            if not question_requires_vision(q)
        ),
        "paper_points": paper_points,
        "catalog_subjective_questions": len(catalog),
        "catalog_subjective_points": sum(int(q["points"]) for q in catalog),
    }


def _dataset_card(
    *,
    repo_id: str,
    dataset_version: str,
    audit: Mapping[str, Any],
    public_ranking_allowed: bool = False,
    benchmark_spec: Mapping[str, Any] | None = None,
    spec: Optional[BenchmarkSpec] = None,
    papers: Sequence[Mapping[str, Any]] = (),
    score_label: str = "public_retrospective",
) -> str:
    dataset_url = "https://huggingface.co/datasets/%s" % repo_id
    if spec is not None:
        subject = spec.subject
        years = list(spec.years)
    else:
        subject = str(papers[0].get("subject") or "数学") if papers else "数学"
        years = sorted({int(paper["year"]) for paper in papers if paper.get("year") is not None}) or [2025]
    years_label = "、".join(str(year) for year in years)
    paper_titles = "、".join(str(paper.get("title") or paper.get("id")) for paper in papers) or "多套试卷"
    paper_count = int(audit.get("papers", len(papers) or 0))
    subject_tag = SUBJECT_TAGS.get(subject, "education")
    year_tags = "\n".join('- "%d"' % year for year in years)
    citation_title = "Gaokao-Bench %s: %s Gaokao %s Benchmark" % (
        dataset_version,
        "-".join(str(year) for year in years),
        subject,
    )
    catalog_questions = int(audit.get("catalog_subjective_questions", 0))
    spec_file_line = (
        "`benchmark_spec.json`：题型、答案形状、视图、分母和评分协议；"
        if benchmark_spec
        else "历史兼容：本包不包含 `benchmark_spec.json`，由 Engine 的显式兼容规范执行；新版本必须随包提供。"
    )
    catalog_file_line = (
        "`catalog/subjective_catalog.jsonl`：主观题目录，`included_in_scoring=false`；"
        if catalog_questions
        else "`catalog/`：本数据包没有编目主观题；"
    )
    if public_ranking_allowed:
        ranking_policy = (
            "本数据包 manifest 记录 `public_ranking_allowed=true`。第三方可以使用本版本结果发布公开榜单、"
            "进行模型对比，或在模型介绍中展示评测结果；引用时应注明数据版本、Benchmark Spec 和评测协议，"
            "不得暗示项目或原始素材权利人背书。题目素材的 `exam_material_license` 为 `NOASSERTION`，"
            "原始试卷权利仍归相应权利人；项目只授权自己原创的编排、schema 和评分元数据。"
        )
    else:
        ranking_policy = (
            "本数据包 manifest 记录 `public_ranking_allowed=false`。在书面授权或 manifest 另有明确声明之前，"
            "不应把本数据包的结果用于第三方公开榜单、厂商营销或对其他模型作公开排名。题目素材的 "
            "`exam_material_license` 为 `NOASSERTION`，原始试卷权利仍归相应权利人；项目只授权自己原创的"
            "编排、schema 和评分元数据。"
        )
    return """---
license: other
license_name: gaokao-bench-data-terms
license_link: {dataset_url}/blob/main/LICENSE-DATA
pretty_name: Gaokao-Bench
language:
- zh
tags:
- gaokao
- benchmark
- evaluation
- {subject_tag}
- education
{year_tags}
task_categories:
- question-answering
size_categories:
- n<1K
configs:
- config_name: questions
  data_files:
  - split: test
    path: questions.jsonl
---

# Gaokao-Bench {version}

![Gaokao-Bench：全国选拔标尺、历年动态演进、真实卷面评测](brand/hero-hf-zh.png)

**全国选拔标尺 · 历年动态演进 · 真实卷面评测**

`v{version}` · {years_label} 高考{subject} · {questions} 题 / {points} 分 · 确定性判分

Gaokao-Bench 用中国高考真题衡量 AI 的真实推理能力。本数据包是 `{version}` 版，覆盖 **{years_label} 年高考{subject} {paper_count} 套试卷**的计分题部分，按真实卷面分值计分，并以固定分母处理漏答、超时和格式失败。

**English:** Gaokao-Bench is a deterministic, point-weighted benchmark built from real Chinese Gaokao papers. Version `{version}` covers the scored sections of {paper_count} {subject} paper(s) from {years_label}, uses a fixed {points}-point denominator, and ships only the figure crops required by the vision profile.

## 为什么用高考

- **国家级选拔尺度：** 高考是中国最具代表性的高区分度选拔考试，题目经过命题专家反复打磨，天然区分多步推理与表层模仿。
- **持续演进：** 每年都有全新的全国卷与自主命题卷，命题趋势持续强化反套路、创新情境和跨模块综合。
- **真实卷面评测：** Gaokao-Bench 保留真实题分、固定总分分母和确定性判分，不使用 LLM 裁判，也不剔除失败样本。

| 项目 | 规格 |
| --- | --- |
| 年份与科目 | {years_label} · {subject} |
| 覆盖试卷 | {paper_titles} |
| 计分题目 | **{questions} 题** |
| 卷面总分 | **{points} 分** |
| 答案槽 | **{slots} 个** |
| 图形必需题 | **{image_questions} 题** |
| 文本模式 | **{text_questions} 题 / {text_points} 分可达**，以 {points} 分为固定分母 |
| 视觉模式 | **{questions} 题 / {points} 分** |
| 主观题目录 | {catalog_questions} 题 / {catalog_points} 分，编目但不计分 |

## 快速开始

始终固定 `revision="v{version}"`，不要直接跟随可能变化的 `main`：

```bash
python -m pip install "huggingface_hub>=0.23"
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="{repo_id}",
    repo_type="dataset",
    revision="v{version}",
    local_dir="data/{version}",
)
PY

python3 scripts/validate.py --data-dir data/{version}
```

如果已经安装 Gaokao-Bench CLI，也可以一条命令完成下载与校验：

```bash
gaokao-bench fetch-data --version {version}
gaokao-bench validate --data-dir data/{version}
```

数据目录以 `manifest.json` 为版本事实源；`GB_DATA` 应指向 `data/{version}`，不要混用不同版本的文件。

## 文件结构

- `manifest.json`：数据集版本、统计、文件哈希和发布信息；
- `papers.jsonl`：{paper_count} 套试卷元数据；
- `questions.jsonl`：{questions} 道计分题及题面结构；
- {spec_file_line}
- `answer_keys.jsonl`：答案键和空位定义；
- `assets.jsonl` 与 `assets/`：{image_questions} 道图形必需题的裁图、字节数与哈希；
- {catalog_file_line}
- `examples/`：空提交、标准答案和非法提交示例。
- `CITATION.cff`：与数据版本绑定的引用信息。

## 字段说明

| 文件 | 关键字段 | 用途 |
| --- | --- | --- |
| `questions.jsonl` | `id`、`paper_id`、`type`、`stem`、`options`、`points`、`answer_slot_count`、`asset_dependency`、`source` | {questions} 道计分题及匿名化评测所需的题面结构 |
| `answer_keys.jsonl` | `question_id`、`kind`、`canonical`、`correct_options`、`slots`、`points` | 确定性判分使用的标准答案 |
| `papers.jsonl` | `id`、`title`、`year`、`region`、`objective_points`、`pdf_url` | {paper_count} 套试卷元数据与来源 |
| `assets.jsonl` | `question_id`、`path`、`bytes`、`sha256`、`dependency` | {image_questions} 道图形必需题的裁图索引 |
| `catalog/subjective_catalog.jsonl` | `id`、`type`、`content`、`points`、`included_in_scoring` | 主观题目录，仅作后续扩展记录 |

Dataset Viewer 使用单一 `questions` config 展示 {questions} 道计分题；其他 JSONL 保留为直接下载文件，避免不同字段结构混表。评分答案键 `answer_keys.jsonl` 供 Harness 读取，其中 `canonical` 同时包含单值和多空数组，不交给 Viewer 自动推断。

## 使用与排名政策

{ranking_policy}

## 评分规则

$$
\\mathrm{{score.weighted\\_points}} = \\frac{{\\text{{Earned Points}}}}{{{points}}}
$$

- 单题默认全对得分，多选和多空题在 `{version}` 中不启用部分分；
- 缺失、格式错误、超时和 API 错误计 `0` 分；
- 判分完全确定，不调用 LLM 裁判；
- 失败样本保留在分母内，分母不会因为接口失败而缩水；
- 文本模式跳过 {image_questions} 道图形必需题，按 `0` 分计入固定的 {points} 分分母；
- 视觉模式只为 {image_questions} 道图形必需题附加题图；
- 本数据集不把 {points} 分外推为 750 分高考总分。

| 模式 | 实际可达 | 固定分母 | 说明 |
| --- | --- | --- | --- |
| Text | {text_questions} 题 / {text_points} 分 | {points} 分 | {image_questions} 道图形题不发送，直接记 0 分 |
| Vision | {questions} 题 / {points} 分 | {points} 分 | 仅为 {image_questions} 道图形题附带必需裁图 |

## 污染说明

{years_label} 年高考题目已经公开，可能存在于模型训练或检索语料中。所有结果必须标注 `{score_label}`，不得声称这是 held-out、零污染或“新鲜能力”评测。

## 许可与来源

项目原创的题目编排、答案键结构、评分元数据采用 **CC BY 4.0**。原始高考题干、答案原文、题图和其他第三方素材的权利仍归原权利人，本项目不通过这些文件重新许可第三方内容。公开分发采用项目 owner 的发布决定，并保留逐来源权利说明、署名和纠错/下架流程。详见 `LICENSE-DATA` 和 `THIRD_PARTY_NOTICES.md`。

代码和 Harness 使用 **Apache License 2.0**。公开排名与营销授权边界见上节和 `manifest.json` 的 `release.public_ranking_allowed`。

## 相关文件

- [数据卡主页]({dataset_url})
- [数据许可](LICENSE-DATA)
- [第三方来源说明](THIRD_PARTY_NOTICES.md)
- [引用元数据](CITATION.cff)
- [讨论与纠错]({dataset_url}/discussions)

## 版本记录

- `{version}`：{years_label} 高考{subject} {paper_count} 套试卷计分题，{questions} 题 / {points} 分，{image_questions} 道图形必需题，主观题目录不计分。

数据版本：`{version}` · 生成时间：`{created_at}` · 分数标签：`{score_label}`

## 引用

```bibtex
@misc{{gaokao_bench_{citation_key},
  author       = {{{{Gaokao-Bench contributors}}}},
  title        = {{{citation_title}}},
  year         = {{2026}},
  publisher    = {{Hugging Face}},
  howpublished = {{\\url{{{dataset_url}}}}},
  note         = {{Data version {version}; fixed {points}-point denominator}}
}}
```

如果这个数据集对你的研究或评测有帮助，请引用 Gaokao-Bench 的对应代码和精确数据版本。
""".format(
        dataset_url=dataset_url,
        repo_id=repo_id,
        version=dataset_version,
        subject=subject,
        years_label=years_label,
        paper_titles=paper_titles,
        paper_count=paper_count,
        spec_file_line=spec_file_line,
        catalog_file_line=catalog_file_line,
        subject_tag=subject_tag,
        year_tags=year_tags,
        citation_title=citation_title,
        citation_key=("%s-%s" % ("-".join(str(year) for year in years), subject_tag)).replace("-", "_"),
        questions=int(audit["questions"]),
        points=int(audit["objective_points"]),
        text_points=int(audit["text_points"]),
        slots=int(audit["answer_slots"]),
        image_questions=int(audit["image_required_questions"]),
        text_questions=int(audit["questions"]) - int(audit["image_required_questions"]),
        catalog_questions=catalog_questions,
        catalog_points=int(audit.get("catalog_subjective_points", 0)),
        created_at=audit.get("created_at", ""),
        public_ranking_allowed=str(public_ranking_allowed).lower(),
        ranking_policy=ranking_policy,
        score_label=score_label,
    )


def _third_party_notice(
    repo_id: str,
    papers: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
) -> str:
    source_lines = "\n".join(
        "- %s: %s" % (paper.get("title", paper.get("id", "")), paper.get("pdf_url", ""))
        for paper in papers
    )
    image_questions = int(audit.get("image_required_questions", 0))
    material = (
        "%d reviewed question crop(s)" % image_questions
        if image_questions
        else "no question crops"
    )
    return """# Third-Party Notices

Gaokao-Bench includes exam questions, answer text, and %s derived from public
Gaokao papers and third-party source material. Those materials remain subject
to their original rights and are not relicensed by the project's Apache-2.0
code license or the project's CC BY 4.0 editorial license.

The paper sources retained in this package are:

%s

Source identifiers and links are kept in `papers.jsonl`, `questions.jsonl`,
`answer_keys.jsonl`, and `assets.jsonl` for attribution and auditability. If you
believe material in this package should be corrected, attributed differently,
or removed, open a discussion at https://huggingface.co/datasets/%s/discussions.
""" % (material, source_lines, repo_id)


def _citation(
    *,
    repo_id: str,
    dataset_version: str,
    created_at: str,
    subject: str,
    years: Sequence[int],
    audit: Mapping[str, Any],
) -> str:
    dataset_url = "https://huggingface.co/datasets/%s" % repo_id
    years_label = "-".join(str(year) for year in years)
    subject_tag = SUBJECT_TAGS.get(subject, "education")
    abstract = (
        "A deterministic benchmark containing %d scored %s questions from %d Gaokao paper(s) (%s). "
        "The fixed objective denominator is %d points; %d question(s) require figures."
        % (
            int(audit.get("questions", 0)),
            subject,
            int(audit.get("papers", 0)),
            years_label,
            int(audit.get("objective_points", 0)),
            int(audit.get("image_required_questions", 0)),
        )
    )
    return """cff-version: 1.2.0
title: "Gaokao-Bench {version}: {years_label} Gaokao {subject} Objective Benchmark"
message: "Please cite this exact dataset version. Underlying exam material has separate rights and is not relicensed by the project."
type: dataset
version: {version}
date-released: {date}
license: other
license-url: {url}/blob/main/LICENSE-DATA
url: {url}
authors:
  - name: Gaokao-Bench contributors
keywords:
  - gaokao
  - benchmark
  - evaluation
  - {subject_tag}
  - education
abstract: "{abstract}"
""".format(
        version=dataset_version,
        date=created_at[:10],
        url=dataset_url,
        years_label=years_label,
        subject=subject,
        subject_tag=subject_tag,
        abstract=abstract,
    )


def _gitattributes() -> str:
    return """*.jsonl text eol=lf
*.json text eol=lf
*.md text eol=lf
*.cff text eol=lf
*.png binary
"""


def _build_manifest(
    *,
    source_manifest: Mapping[str, Any],
    output: Path,
    audit: Mapping[str, Any],
    repo_id: str,
    created_at: str,
    spec: Optional[BenchmarkSpec] = None,
    benchmark_spec_ref: Optional[str] = None,
) -> Dict[str, Any]:
    file_map: Dict[str, Dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(output).as_posix()
        if relative in PUBLIC_HUB_MANAGED_FILES:
            file_map[relative] = {"bytes": None, "sha256": None, "managed_by": "huggingface"}
        else:
            file_map[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    source_release = source_manifest.get("release") if isinstance(source_manifest.get("release"), dict) else {}
    years = list(spec.years) if spec is not None else []
    score_label = str(
        source_release.get("score_label")
        or ("retrospective_public_%s" % "-".join(str(year) for year in years) if years else "public_retrospective")
    )
    manifest = {
        "dataset": "Gaokao-Bench",
        "dataset_version": output.name,
        "schema_version": "1.0",
        "package_profile": PUBLIC_PACKAGE_PROFILE,
        "status": "public_data_release",
        "release_channel": "huggingface",
        "public_release": True,
        "created_at": created_at,
        "subject": spec.subject if spec is not None else None,
        "years": years,
        "supersedes": source_manifest.get("supersedes"),
        "scope": source_manifest.get("scope"),
        "counts": {
            "papers": audit["papers"],
            "objective_questions": audit["questions"],
            "single_choice_questions": audit["single_choice_questions"],
            "multi_choice_questions": audit["multi_choice_questions"],
            "fill_questions": audit["fill_questions"],
            "choice_questions": audit["choice_questions"],
            "answer_slots": audit["answer_slots"],
            "image_required_questions": audit["image_required_questions"],
            "text_reachable_points": audit["text_points"],
            "catalog_subjective_questions": audit["catalog_subjective_questions"],
            "catalog_subjective_points": audit["catalog_subjective_points"],
        },
        "points": source_manifest.get("points"),
        "scoring": source_manifest.get("scoring"),
        "benchmark_spec": benchmark_spec_ref,
        "quality": source_manifest.get("quality"),
        "rights": {
            "code_license": "Apache-2.0",
            "original_metadata_license": "CC-BY-4.0",
            "exam_material_license": "NOASSERTION",
            "public_redistribution": "owner_decision_public_release_%s" % created_at[:10],
            "rights_status": "public_with_third_party_notice",
        },
        "release": {
            "channel": "huggingface",
            "repo_id": repo_id,
            "revision": "v%s" % output.name,
            "public_ranking_allowed": bool(source_release.get("public_ranking_allowed", False)),
            "public_ranking_policy": (
                "allowed_with_attribution_and_version_pin"
                if source_release.get("public_ranking_allowed", False)
                else "not_allowed_without_written_authorization"
            ),
            "score_label": score_label,
        },
        "files": file_map,
    }
    return manifest


def build_public_data_package(
    source_dir: Path | str,
    output_dir: Path | str,
    *,
    repo_id: str = "Knove/Gaokao-Bench",
    created_at: str | None = None,
) -> Dict[str, Any]:
    source = Path(source_dir).resolve()
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("public data output already exists: %s" % output)
    output.mkdir(parents=True)
    project_root = _project_root(source)
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    spec = load_benchmark_spec(source, source_manifest)
    source_version = str(source_manifest.get("dataset_version", ""))
    if output.name != source_version:
        raise ValueError(
            "public output directory must end with the dataset version: %s != %s"
            % (output.name, source_version)
        )

    papers = _project_rows(_read_jsonl(source / "papers.jsonl"), PUBLIC_PAPER_FIELDS)
    questions = [_project_question(row) for row in _read_jsonl(source / "questions.jsonl")]
    answers = _project_rows(_read_jsonl(source / "answer_keys.jsonl"), PUBLIC_ANSWER_FIELDS)
    required_ids = {
        str(question["id"])
        for question in questions
        if question.get("asset_dependency") == "required"
    }
    source_assets = _read_jsonl(source / "assets.jsonl")
    selected_assets = [
        asset
        for asset in source_assets
        if asset.get("dependency") == "required" and str(asset.get("question_id")) in required_ids
    ]
    asset_ids = {str(asset.get("question_id")) for asset in selected_assets}
    if asset_ids != required_ids:
        raise ValueError(
            "required figure assets do not match required questions: %s != %s"
            % (sorted(asset_ids), sorted(required_ids))
        )
    assets = _project_rows(selected_assets, PUBLIC_ASSET_FIELDS)
    catalog_path = source / "catalog" / "subjective_catalog.jsonl"
    catalog = _project_rows(_read_jsonl(catalog_path) if catalog_path.is_file() else [], PUBLIC_CATALOG_FIELDS)
    audit = _public_audit(papers=papers, questions=questions, catalog=catalog, spec=spec)
    created = created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    audit["created_at"] = created

    _write_jsonl(output / "papers.jsonl", papers)
    _write_jsonl(output / "questions.jsonl", questions)
    _write_jsonl(output / "answer_keys.jsonl", answers)
    _write_jsonl(output / "assets.jsonl", assets)
    (output / "catalog").mkdir()
    _write_jsonl(output / "catalog" / "subjective_catalog.jsonl", catalog)
    declared_spec = source_manifest.get("benchmark_spec")
    benchmark_spec_ref: Optional[str] = None
    if isinstance(declared_spec, Mapping):
        spec_relative = Path(BENCHMARK_SPEC_FILENAME)
        (output / spec_relative).write_text(
            json.dumps(declared_spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        benchmark_spec_ref = spec_relative.as_posix()
    elif isinstance(declared_spec, str) and declared_spec.strip():
        spec_relative = Path(declared_spec)
        if spec_relative.is_absolute() or ".." in spec_relative.parts:
            raise ValueError("unsafe benchmark_spec path: %s" % declared_spec)
        shutil.copy2(source / spec_relative, output / spec_relative)
        benchmark_spec_ref = spec_relative.as_posix()

    for asset in assets:
        relative = str(asset["path"])
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("unsafe public asset path: %s" % relative)
        destination = output / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative_path, destination)
    shutil.copytree(source / "examples", output / "examples")
    shutil.copy2(project_root / "LICENSE-DATA", output / "LICENSE-DATA")
    (output / "brand").mkdir()
    shutil.copy2(
        project_root / ".github" / "readme" / "hero-hf-zh.png",
        output / "brand" / "hero-hf-zh.png",
    )
    (output / "THIRD_PARTY_NOTICES.md").write_text(
        _third_party_notice(repo_id, papers, audit), encoding="utf-8"
    )
    (output / "CITATION.cff").write_text(
        _citation(
            repo_id=repo_id,
            dataset_version=output.name,
            created_at=created,
            subject=spec.subject,
            years=spec.years,
            audit=audit,
        ),
        encoding="utf-8",
    )
    (output / ".gitattributes").write_text(_gitattributes(), encoding="utf-8")
    (output / "README.md").write_text(
        _dataset_card(
            repo_id=repo_id,
            dataset_version=output.name,
            audit=audit,
            public_ranking_allowed=bool(
                (source_manifest.get("release") or {}).get("public_ranking_allowed", False)
            ),
            benchmark_spec=benchmark_spec_ref,
            spec=spec,
            papers=papers,
            score_label=str(
                (source_manifest.get("release") or {}).get("score_label")
                or "retrospective_public_%s" % "-".join(str(year) for year in spec.years)
            ),
        ),
        encoding="utf-8",
    )
    manifest = _build_manifest(
        source_manifest=source_manifest,
        output=output,
        audit=audit,
        repo_id=repo_id,
        created_at=created,
        spec=spec,
        benchmark_spec_ref=benchmark_spec_ref,
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit = validate_dataset(load_dataset(output))
    return {
        "output": str(output.resolve()),
        "dataset_version": output.name,
        "repo_id": repo_id,
        "files": len(manifest["files"]),
        "sha256": _sha256(output / "manifest.json"),
        "audit": audit,
    }
