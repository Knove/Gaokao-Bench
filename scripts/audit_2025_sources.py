#!/usr/bin/env python3
"""Build the 2025 per-question source coverage matrix from local frozen material.

This script answers one question only: for every 2025 question, which
*independent source groups* actually carry its stem, and which of those groups
also carry a usable answer.  It deliberately does not decide the gate -- it
produces the numbers that make the gate decision auditable, so that nobody has
to claim "9 stem / 6 joint" without evidence.

Inputs are the local caches that were harvested for the 2025 papers:

* ``.cache/gaokao-2025-sources`` -- frozen PDFs, preview HTML, page images
* ``.tmp_audit/agents/sartre/work/page_texts`` -- per-page OCR/extracted text
* ``.tmp_audit/agents/sartre/work/corpus.json`` -- document-level corpus rows

Outputs:

* ``reports/audit-2025/coverage-matrix.json`` -- machine readable
* ``reports/audit-2025/coverage-matrix.md`` -- human readable gap report

The independence grouping follows ``strict_dual_source_v4``: every page of one
publisher collapses into a single group, so 55 齐齐文库 documents still count as
one group.
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import subprocess
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / ".cache" / "gaokao-2025-sources"
PAGE_TEXT_ROOT = ROOT / ".tmp_audit" / "agents" / "sartre" / "work" / "page_texts"
CORPUS_PATH = ROOT / ".tmp_audit" / "agents" / "sartre" / "work" / "corpus.json"
SPEC_DIR = ROOT / "specs"
OUT_DIR = ROOT / "reports" / "audit-2025"
TEXT_CACHE = ROOT / ".cache" / "audit-2025" / "text"

PAPERS: Tuple[str, ...] = ("n1", "n2", "bj", "sh", "tj")
PAPER_LABEL = {"n1": "N1", "n2": "N2", "bj": "BJ", "sh": "SH", "tj": "TJ"}

def strip_html(payload: str) -> str:
    payload = re.sub(r"<script.*?</script>", " ", payload, flags=re.S | re.I)
    payload = re.sub(r"<style.*?</style>", " ", payload, flags=re.S | re.I)
    payload = re.sub(r"<[^>]+>", " ", payload)
    return html_module.unescape(payload)


def read_pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def cached_text(path: Path) -> str:
    """Extract text from a frozen artifact, memoized by path and mtime."""

    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    path = path if path.is_absolute() else (ROOT / path)
    try:
        label = str(path.resolve().relative_to(ROOT))
    except ValueError:
        label = str(path)
    key = re.sub(r"[^A-Za-z0-9]+", "_", label)[-120:]
    cache_file = TEXT_CACHE / (key + ".txt")
    stamp = "%d:%d" % (path.stat().st_size, int(path.stat().st_mtime))
    if cache_file.is_file():
        head = cache_file.read_text(encoding="utf-8", errors="ignore").split("\n", 1)
        if head and head[0].strip() == stamp:
            return head[1] if len(head) > 1 else ""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = read_pdf_text(path)
    elif suffix in {".html", ".htm"}:
        text = strip_html(path.read_text(encoding="utf-8", errors="ignore"))
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    cache_file.write_text(stamp + "\n" + text, encoding="utf-8")
    return text


def resolve_paper(*hints: str) -> Optional[str]:
    joined = " ".join(str(hint or "") for hint in hints).lower()
    patterns = (
        ("n1", (r"\bn1\b", r"全国一卷", r"全国卷1", r"新课标1", r"新高考1", r"全国i卷", r"全国 i", r"一卷")),
        ("n2", (r"\bn2\b", r"全国二卷", r"全国卷2", r"新课标2", r"新高考2", r"全国ii卷", r"全国 ii", r"二卷")),
        ("bj", (r"\bbj\b", r"北京")),
        ("sh", (r"\bsh\b", r"上海")),
        ("tj", (r"\btj\b", r"天津")),
    )
    for paper, needles in patterns:
        for needle in needles:
            if re.search(needle, joined):
                return paper
    return None


class Source:
    """One frozen artifact inside one publisher independence group."""

    __slots__ = ("source_id", "publisher", "group", "paper", "url", "path", "scope", "text")

    def __init__(
        self,
        *,
        source_id: str,
        publisher: str,
        group: str,
        paper: Optional[str],
        url: str,
        path: Path,
        scope: str,
    ) -> None:
        self.source_id = source_id
        self.publisher = publisher
        self.group = group
        self.paper = paper
        self.url = url
        self.path = path
        self.scope = scope
        self.text = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "publisher": self.publisher,
            "independence_group": self.group,
            "paper": self.paper,
            "url": self.url,
            "artifact": str(self.path.relative_to(ROOT)) if self.path.is_absolute() else str(self.path),
            "scope": self.scope,
            "chars": len(self.text),
        }


def _prefix_paper(name: str) -> Optional[str]:
    lowered = name.lower()
    for paper in PAPERS:
        if re.search(r"(^|[^a-z])%s([^a-z]|$)" % paper, lowered):
            return paper
    return None


PAGE_PUBLISHERS: Sequence[Tuple[str, str, str, str]] = (
    # (filename prefix, publisher, independence group, scope)
    ("eol-", "中国教育在线", "eol", "stem_and_answer"),
    ("zizzs-", "自主招生在线", "zizzs", "stem_and_answer"),
    ("zizhiok-", "自主招生在线", "zizzs", "stem_and_answer"),
    ("51test-", "无忧考网", "51test", "stem_and_answer"),
    ("sohu-", "搜狐", "sohu", "stem_and_answer"),
    ("book118-", "原创力文档", "book118", "stem_and_answer"),
    ("doc88-", "道客巴巴", "doc88", "stem_and_answer"),
    ("jinchutou-", "金锄头文库", "jinchutou", "stem_and_answer"),
    ("gk100-", "高考100", "gk100", "stem_and_answer"),
    ("qq-", "腾讯新闻·掌上高考", "qq-zhangshanggaokao", "stem_and_answer"),
    ("zhihu-", "知乎", "zhihu", "stem_and_answer"),
    ("51jiaoxi-", "51教学网", "jiaoxi", "stem_and_answer"),
    ("jiaoxi-", "51教学网", "jiaoxi", "stem_and_answer"),
    ("bili-", "哔哩哔哩", "bilibili", "stem_and_answer"),
    ("baidu-mobile-", "百度文库", "baidu-wenku", "stem_and_answer"),
    ("rendoc-", "人人文库", "rendoc", "stem_and_answer"),
)


def discover_sources() -> List[Source]:
    sources: List[Source] = []

    # 1. 齐齐文库 document PDFs (one publisher group regardless of uploader count)
    for pdf in sorted((CACHE_ROOT / "qiqi-docs").glob("*.pdf")):
        meta_path = pdf.with_suffix(".json")
        url = ""
        title = ""
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            url = str(meta.get("url") or "")
            title = str(meta.get("title") or "")
        paper = resolve_paper(pdf.stem, title)
        sources.append(
            Source(
                source_id="qiqi-" + pdf.stem,
                publisher="齐齐文库",
                group="qiqiwenku",
                paper=paper,
                url=url,
                path=pdf,
                scope="stem_and_answer",
            )
        )

    # 2. 百度文库 document PDFs
    meta_by_paper: Dict[str, Mapping[str, Any]] = {}
    baidu_meta = CACHE_ROOT / "baidu-wenku" / "meta.json"
    if baidu_meta.is_file():
        for row in json.loads(baidu_meta.read_text(encoding="utf-8")):
            meta_by_paper[str(row.get("paper") or "").lower()] = row
    for pdf in sorted((CACHE_ROOT / "baidu-wenku").glob("*.pdf")):
        paper = resolve_paper(pdf.stem)
        row = meta_by_paper.get((paper or "").upper().lower(), {})
        sources.append(
            Source(
                source_id="baidu-wenku-" + pdf.stem.lower(),
                publisher="百度文库",
                group="baidu-wenku",
                paper=paper,
                url=str(row.get("url") or ""),
                path=pdf,
                scope="stem_and_answer",
            )
        )

    # 3. deekur 原卷 PDFs -- frozen paper, no answers, therefore stem-only
    for pdf in sorted((CACHE_ROOT / "deekur").glob("*.pdf")):
        paper = resolve_paper(pdf.stem)
        sources.append(
            Source(
                source_id="deekur-" + (paper or pdf.stem),
                publisher="deekur/gaokaomath",
                group="deekur",
                paper=paper,
                url="https://github.com/deekur/gaokaomath",
                path=pdf,
                scope="stem",
            )
        )

    # 4. 人人文库 preview HTML and extracted text
    for path in sorted((CACHE_ROOT / "rendoc").glob("*")):
        if not path.is_file() or path.suffix.lower() not in {".html", ".txt"}:
            continue
        if path.name.endswith("-m.html") or path.name.endswith("-www.html"):
            continue
        title = ""
        if path.suffix.lower() == ".html":
            match = re.search(r"<title>(.*?)</title>", path.read_text(encoding="utf-8", errors="ignore"), re.S)
            title = match.group(1) if match else ""
        paper = resolve_paper(path.stem, title)
        sources.append(
            Source(
                source_id="rendoc-" + path.stem,
                publisher="人人文库",
                group="rendoc",
                paper=paper,
                url="https://www.renrendoc.com/paper/%s.html" % re.sub(r"\D", "", path.stem),
                path=path,
                scope="stem_and_answer",
            )
        )

    # 5. publisher article/detail pages harvested into pages/
    for path in sorted((CACHE_ROOT / "pages").glob("*.html")):
        name = path.name
        if name.startswith(("sohu-all-", "zhihu-all", "bili-all", "gaokaohub", "book118-mobile-index")):
            continue
        for prefix, publisher, group, scope in PAGE_PUBLISHERS:
            if name.startswith(prefix):
                paper = _prefix_paper(name[len(prefix) :] + " " + name)
                sources.append(
                    Source(
                        source_id=group + "-" + path.stem,
                        publisher=publisher,
                        group=group,
                        paper=paper,
                        url="",
                        path=path,
                        scope=scope,
                    )
                )
                break

    # 6. per-page OCR/extracted text harvested by the earlier audit agents
    if PAGE_TEXT_ROOT.is_dir():
        grouped: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
        for path in sorted(PAGE_TEXT_ROOT.glob("*.txt")):
            stem = path.name
            if stem.startswith("360__"):
                group, paper_raw = "360wenku", stem.split("__")[1]
                publisher, scope = "360文库", "stem_and_answer"
            elif stem.startswith("51test__"):
                group, paper_raw = "51test", stem.split("__")[1].split("-")[0]
                publisher, scope = "无忧考网", "stem_and_answer"
            elif stem.startswith("book118__"):
                parts = stem.split("__")
                group, publisher, scope = "book118", "原创力文档", "stem_and_answer"
                paper_raw = parts[2] if len(parts) > 2 else ""
            elif stem.startswith("sohu-b__"):
                group, publisher, scope = "sohu", "搜狐", "stem_and_answer"
                paper_raw = "sh"
            elif stem.startswith("src-eol-2025-"):
                group, publisher, scope = "eol", "中国教育在线", "stem_and_answer"
                paper_raw = stem.split("-")[3]
            elif stem.startswith("src-zizzs-2025-"):
                group, publisher, scope = "zizzs", "自主招生在线", "stem_and_answer"
                paper_raw = stem.split("-")[3]
            else:
                continue
            grouped[(group, paper_raw)].append(path)
        for (group, paper_raw), paths in sorted(grouped.items()):
            joined = "\n\n".join(cached_text(path) for path in paths)
            publisher_name = {
                "360wenku": "360文库",
                "51test": "无忧考网",
                "book118": "原创力文档",
                "sohu": "搜狐",
                "eol": "中国教育在线",
                "zizzs": "自主招生在线",
            }[group]
            source = Source(
                source_id="%s-%s-pages" % (group, paper_raw),
                publisher=publisher_name,
                group=group,
                paper=resolve_paper(paper_raw),
                url="",
                path=paths[0],
                scope="stem_and_answer" if group != "deekur" else "stem",
            )
            source.text = joined
            sources.append(source)

    # 7. document corpus rows that are not already represented by a file above
    if CORPUS_PATH.is_file():
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        known = {source.source_id for source in sources}
        for row in corpus:
            source_id = str(row.get("source_id") or "")
            if not source_id or source_id in known:
                continue
            publisher = str(row.get("publisher") or "")
            group = {
                "360文库聚合": "360wenku",
                "齐齐文库": "qiqiwenku",
                "人人文库": "rendoc",
                "自主招生在线": "zizzs",
                "中国教育在线": "eol",
                "搜狐": "sohu",
                "无忧考网": "51test",
                "百度文库": "baidu-wenku",
                "deekur/gaokaomath": "deekur",
            }.get(publisher, re.sub(r"\W+", "-", publisher) or "unknown")
            artifact = str(row.get("artifact") or "")
            path = (ROOT / artifact) if artifact else Path(artifact)
            source = Source(
                source_id=source_id,
                publisher=publisher or "unknown",
                group=group,
                paper=resolve_paper(source_id, str(row.get("title") or ""), str(row.get("paper_hint") or "")),
                url=str(row.get("url") or ""),
                path=path if path.is_file() else Path(artifact or "corpus"),
                scope="stem_and_answer",
            )
            source.text = str(row.get("text") or "")
            sources.append(source)

    return sources


def load_questions() -> Dict[str, List[Dict[str, Any]]]:
    """Load the questions from the built package, not from a draft spec.

    Reading the package keeps this structural report tied to the content that
    actually shipped; a spec file can drift from it without anyone noticing.
    """

    package = ROOT / "data" / "0.1.0"
    if not (package / "questions.jsonl").is_file():
        raise SystemExit("build data/0.1.0 first: %s is missing" % package)
    rows_by_paper: Dict[str, List[Dict[str, Any]]] = {}
    for line in (package / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        paper_id = str(item["paper_id"])
        label = next((key for key, value in PAPER_LABEL.items() if "GB25-" + value == paper_id), None)
        if label is None:
            continue
        rows_by_paper.setdefault(label, []).append(
            {
                "id": item["id"],
                "number": int(item["number"]),
                "kind": "objective",
                "points": int(item["points"]),
                "stem": item.get("stem") or item.get("content") or "",
            }
        )
    for line in (package / "catalog" / "subjective_catalog.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        paper_id = str(item["paper_id"])
        label = next((key for key, value in PAPER_LABEL.items() if "GB25-" + value == paper_id), None)
        if label is None:
            continue
        rows_by_paper.setdefault(label, []).append(
            {
                "id": item["id"],
                "number": int(item["number"]),
                "kind": "subjective",
                "points": int(item["points"]),
                "stem": item.get("content") or item.get("stem") or "",
            }
        )
    for label, rows in rows_by_paper.items():
        rows.sort(key=lambda row: row["number"])
    return rows_by_paper


QUESTION_START = re.compile(r"(?:^|\n)\s*(?:第\s*)?(\d{1,2})\s*[.、．]\s*\S")
ANSWER_MARKERS = ("【答案】", "答案：", "参考答案", "【解析】", "详解", "答案及解析")


def question_numbers_present(text: str, limit: int) -> List[int]:
    """Return the question numbers that appear as ``N.`` block starts."""

    found = sorted({int(match) for match in QUESTION_START.findall(text or "")})
    return [number for number in found if 1 <= number <= limit]


def has_answer_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in ANSWER_MARKERS)


def build_matrix(
    sources: Sequence[Source], questions: Mapping[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Summarize structural coverage per publisher group.

    The numbers here are intentionally *structural*: they say which publisher
    groups carry a document that contains every question number and an answer
    section.  They are not the gate.  The gate requires a per-question locator,
    excerpt and observed answer, which is what the audit spec records.
    """

    for source in sources:
        if not source.text and source.path.is_file():
            source.text = cached_text(source.path)

    result: Dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "structural_coverage_not_a_gate",
        "papers": {},
    }
    for paper, rows in questions.items():
        limit = max(row["number"] for row in rows)
        paper_sources = [source for source in sources if source.paper == paper]
        group_rows: Dict[str, Dict[str, Any]] = {}
        for source in paper_sources:
            numbers = question_numbers_present(source.text, limit)
            answer_ready = source.scope == "stem_and_answer" and has_answer_marker(source.text)
            entry = group_rows.setdefault(
                source.group,
                {
                    "publisher": source.publisher,
                    "artifacts": [],
                    "numbers_seen": set(),
                    "answer_ready": False,
                    "chars": 0,
                },
            )
            entry["artifacts"].append(source.source_id)
            entry["numbers_seen"].update(numbers)
            entry["answer_ready"] = entry["answer_ready"] or answer_ready
            entry["chars"] += len(source.text)
        groups = []
        for group, entry in sorted(group_rows.items()):
            seen = sorted(entry["numbers_seen"])
            complete = set(range(1, limit + 1)).issubset(entry["numbers_seen"])
            groups.append(
                {
                    "group": group,
                    "publisher": entry["publisher"],
                    "artifacts": sorted(entry["artifacts"]),
                    "chars": entry["chars"],
                    "questions_seen": len(seen),
                    "covers_all_numbers": complete,
                    "answer_ready": entry["answer_ready"],
                    "missing_numbers": sorted(set(range(1, limit + 1)) - entry["numbers_seen"]),
                }
            )
        complete_groups = [row["group"] for row in groups if row["covers_all_numbers"]]
        joint_candidates = [
            row["group"] for row in groups if row["covers_all_numbers"] and row["answer_ready"]
        ]
        result["papers"][PAPER_LABEL[paper]] = {
            "questions": len(rows),
            "points": sum(row["points"] for row in rows),
            "groups": groups,
            "group_count": len(groups),
            "complete_group_count": len(complete_groups),
            "joint_candidate_group_count": len(joint_candidates),
            "complete_groups": complete_groups,
            "joint_candidate_groups": joint_candidates,
            "reaches_stem_floor_structurally": len(complete_groups) >= 9,
            "reaches_joint_floor_structurally": len(joint_candidates) >= 6,
        }
    return result


def render_markdown(matrix: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# 2025 来源覆盖结构矩阵")
    lines.append("")
    lines.append("本文件由 `scripts/audit_2025_sources.py` 生成，只做**结构性统计**：")
    lines.append("统计每个 publisher 独立组是否持有覆盖全卷题号、且带答案区的文档。")
    lines.append("这不是门禁结论；门禁要求逐题的 locator、摘录与观察答案。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 卷 | 题数 | 总分 | 独立组数 | 覆盖全卷题号的组 | 其中带答案区 | 结构上可达 9/6 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for label, block in matrix["papers"].items():
        verdict = "是" if (
            block["reaches_stem_floor_structurally"] and block["reaches_joint_floor_structurally"]
        ) else "否"
        lines.append(
            "| %s | %d | %d | %d | %d | %d | %s |"
            % (
                label,
                block["questions"],
                block["points"],
                block["group_count"],
                block["complete_group_count"],
                block["joint_candidate_group_count"],
                verdict,
            )
        )
    lines.append("")
    for label, block in matrix["papers"].items():
        lines.append("## %s" % label)
        lines.append("")
        lines.append("| 独立组 | publisher | 文档数 | 字符数 | 题号命中 | 全卷题号 | 答案区 | 缺失题号 |")
        lines.append("|---|---|---:|---:|---:|---|---|---|")
        for row in block["groups"]:
            lines.append(
                "| %s | %s | %d | %d | %d | %s | %s | %s |"
                % (
                    row["group"],
                    row["publisher"],
                    len(row["artifacts"]),
                    row["chars"],
                    row["questions_seen"],
                    "是" if row["covers_all_numbers"] else "否",
                    "是" if row["answer_ready"] else "否",
                    ", ".join(str(number) for number in row["missing_numbers"])[:60] or "—",
                )
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    sources = discover_sources()
    questions = load_questions()
    matrix = build_matrix(sources, questions)
    matrix["sources"] = [source.as_dict() for source in sorted(sources, key=lambda item: item.source_id)]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "coverage-matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.out_dir / "coverage-matrix.md").write_text(render_markdown(matrix), encoding="utf-8")

    print("sources:", len(sources))
    for label, block in matrix["papers"].items():
        print(
            "%s groups=%d complete=%d joint_candidates=%d stem9=%s joint6=%s"
            % (
                label,
                block["group_count"],
                block["complete_group_count"],
                block["joint_candidate_group_count"],
                block["reaches_stem_floor_structurally"],
                block["reaches_joint_floor_structurally"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
