"""Materialise a release tag without changing benchmark content.

This path is intentionally narrower than ``materialize_signed_dataset``.  It
does not import reviews or change answer/point status.  It copies a frozen
content baseline, rewrites only release metadata/documentation, and verifies
that every question, answer key, audit ledger, point record, and image is
byte-identical after the new version is written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Union

from .scoring import file_sha256, load_dataset, validate_dataset


VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?$")
CONTENT_FILES = (
    "questions.jsonl",
    "answer_keys.jsonl",
    "catalog/subjective_catalog.jsonl",
    "answer_audit.jsonl",
    "answer_sources.jsonl",
    "point_audit.jsonl",
    "question_audit.jsonl",
    "asset_audit.jsonl",
    "assets.jsonl",
    "coverage.csv",
    "papers.jsonl",
    "sources.jsonl",
    "examples/gold.json",
    "examples/empty.json",
    "examples/malformed.json",
    "reviews.jsonl",
)


class TagMaterializationError(ValueError):
    """Raised when a release tag cannot be materialised safely."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_timestamp(value: Optional[str]) -> str:
    text = str(value or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()).strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TagMaterializationError("created_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise TagMaterializationError("created_at must include a timezone")
    return text


def _copy_immutable(source: Path, destination: Path) -> None:
    if destination.exists():
        raise TagMaterializationError("output directory already exists; immutable versions are never overwritten")
    if source == destination or source in destination.parents or destination in source.parents:
        raise TagMaterializationError("source and destination dataset directories must not overlap")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise TagMaterializationError("dataset contains a symbolic link: %s" % path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".%s-tag-" % destination.name, dir=destination.parent))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def core_content_hashes(data_dir: Union[Path, str]) -> Dict[str, str]:
    """Hash every file that defines questions, answers, points, or assets."""

    root = Path(data_dir).resolve()
    rows: Dict[str, str] = {}
    for relative in CONTENT_FILES:
        path = root / relative
        if not path.is_file():
            raise TagMaterializationError("content file is missing: %s" % relative)
        rows[relative] = file_sha256(path)
    assets_root = root / "assets"
    if not assets_root.is_dir():
        raise TagMaterializationError("assets directory is missing")
    for path in sorted(assets_root.rglob("*")):
        if path.is_file():
            rows[path.relative_to(root).as_posix()] = file_sha256(path)
    return rows


def _tracked_files(root: Path) -> Iterable[str]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json" or relative.startswith("assets/"):
            continue
        yield relative


def _rewrite_data_card(path: Path, source_version: str, target_version: str) -> None:
    text = path.read_text(encoding="utf-8")
    text = text.replace(source_version, target_version)
    text = text.replace(
        "> 替代：`0.1.0-alpha.6`（保留，不覆盖）",
        "> 内容基线：`%s`（保留，不覆盖）  \n> 替代：`%s`（内容零修改，仅重绑版本与发布资料）" % (source_version, source_version),
    )
    text = text.replace("> 状态：内部验收，`public_release=false`", "> 状态：首个发布候选（内部验收），`public_release=false`")
    text = text.replace(
        "本版对 alpha.6 的 74 道题逐题完成了",
        "内容基线（alpha.6.1）的 74 道题已逐题完成",
    )
    source_short = source_version.replace("0.1.0-", "")
    text = text.replace(
        "## 3. %s 全量题目核验" % source_short,
        "## 3. 内容基线 %s 的全量题目核验" % source_short,
    )
    text = text.replace("本版四个显式内容/资产修正：", "内容基线完成的四个显式内容/资产修正（0.0.1 原样继承）：")
    text = text.replace(
        "%s 复核包是本轮唯一\n可签字包；alpha.6 及更早复核包均应视为被替代。" % source_short,
        "内容基线 %s 的复核包是唯一可签字包；`%s` 原样继承该内容，不重新签发复核包。" % (source_short, target_version),
    )
    text = text.replace("`0.1.0-alpha.2-baseline.*` 是历史基线", "`0.1.0-alpha.2-baseline.*` 是更早的历史基线")
    path.write_text(text, encoding="utf-8")


def _rewrite_reproduce(path: Path, source_version: str, target_version: str) -> None:
    text = path.read_text(encoding="utf-8").replace(source_version, target_version)
    replacement = """## 6. 0.0.1 版本材料化

`0.0.1` 不重新构建题目、答案或裁图，而是从冻结内容基线
`{source_version}` 做不可变复制，只更新 manifest、发布文档和版本号：

```bash
python3 scripts/materialize_release_tag.py \\
  --source-dir data/{source_version} \\
  --output-dir data/{target_version} \\
  --version {target_version} \\
  --created-at 2026-11-06T18:00:00+08:00
```

脚本会重新计算并比对全部题目、答案键、审计账本、题分、示例和 74 张裁图的
SHA-256；任一内容文件变化都会失败。内容基线本身仍由
`scripts/build_alpha.py` 从冻结 PDF 和本地种子重建，该历史构建流程不属于
`0.0.1` 的发布操作。

""".format(source_version=source_version, target_version=target_version)
    text = re.sub(r"## 6\. 重新构建.*?(?=\n## 7\.)", replacement, text, count=1, flags=re.DOTALL)
    path.write_text(text, encoding="utf-8")


def _rewrite_rights(path: Path, source_version: str, target_version: str) -> None:
    text = path.read_text(encoding="utf-8").replace(source_version, target_version)
    text = text.replace(
        "项目 owner 的 0.1 内部使用决定",
        "项目 owner 的 0.1 内部试用决定；本发布候选继承该决定，但公开再分发仍需单独签字",
    )
    text = text.replace("## 3. 本 Alpha 的分发规则与 owner 决定", "## 3. 本发布候选的分发规则与 owner 决定")
    text = text.replace(
        "- 无法确认的素材可以保留在内部包，但不得把“owner 接受风险”写成“已取得第三方许可”。",
        "- 无法确认的素材可以保留在内部包，但不得把“owner 接受风险”写成“已取得第三方许可”；\n"
        "- 逐来源渠道和已知错误记录见同目录 `SOURCE_REGISTER.md`。",
    )
    text = text.replace(
        "0.1 内部试用记录 `rights_status=owner_accepted_risk_for_0.1_provenance_retained`；",
        "内部试用记录 `rights_status=owner_accepted_internal_risk_provenance_retained`；",
    )
    path.write_text(text, encoding="utf-8")


def _write_materialization_record(
    path: Path,
    *,
    source_version: str,
    target_version: str,
    source_manifest_sha256: str,
    created_at: str,
    hashes: Mapping[str, str],
) -> None:
    rows = "\n".join(
        "| `%s` | `%s` |" % (relative, digest) for relative, digest in sorted(hashes.items())
    )
    text = """# {target_version} 内容材料化记录

> 源版本：`{source_version}`  
> 目标版本：`{target_version}`  
> 源 manifest SHA-256：`{source_manifest_sha256}`  
> 材料化时间：`{created_at}`  
> 内容变化：**0**

本版本只做发布版号映射和文档重绑。脚本在复制前后分别计算题目、答案键、
答案/分值/题面/裁图审计、示例文件和全部 74 张裁图的 SHA-256；下表源文件与
目标文件逐项一致。没有修改 canonical answer、答案槽、题分、图形依赖或评分器行为。

## 内容指纹

| 文件 | SHA-256 |
|---|---|
{rows}

## 审核边界

- owner 签字：仍为 `pending`；
- 两名独立审校：仍为 `pending`；
- 考试院官方答案直证：未找到；
- 第三方媒体答案页的 63 条视觉待核对记录原样保留；
- `public_release=false`，本文件不构成公开再分发授权。
""".format(
        target_version=target_version,
        source_version=source_version,
        source_manifest_sha256=source_manifest_sha256,
        created_at=created_at,
        rows=rows,
    )
    path.write_text(text, encoding="utf-8")


def _release_policy(
    source_policy: Mapping[str, Any],
    *,
    target_version: str,
    target_manifest_sha256: str,
) -> Dict[str, Any]:
    payload = json.loads(json.dumps(source_policy))
    payload["dataset_version"] = target_version
    payload["dataset_manifest_sha256"] = target_manifest_sha256
    full_public = payload.get("full_public_data_release")
    if isinstance(full_public, dict):
        full_public["rights_attestation"] = "release/%s/rights-attestation.json" % target_version
    answer = payload.get("answer_validation")
    if isinstance(answer, dict):
        answer["answer_protocol"] = "release/%s/answer-protocol.json" % target_version
    return payload


def write_release_evidence(
    project_root: Union[Path, str],
    *,
    source_version: str,
    target_version: str,
    target_manifest_sha256: str,
    content_hashes: Mapping[str, str],
) -> Dict[str, Any]:
    """Create unsigned, hash-bound release evidence for a tag candidate."""

    root = Path(project_root).resolve()
    source_dir = root / "release" / source_version
    destination = root / "release" / target_version
    if not source_dir.is_dir():
        raise TagMaterializationError("source release evidence is missing: %s" % source_dir)
    if destination.exists():
        raise TagMaterializationError("release evidence directory already exists: %s" % destination)
    destination.mkdir(parents=True)

    policy_source = json.loads((source_dir / "release-policy.json").read_text(encoding="utf-8"))
    policy = _release_policy(
        policy_source,
        target_version=target_version,
        target_manifest_sha256=target_manifest_sha256,
    )
    _write_json(destination / "release-policy.json", policy)

    for name in ("rights-attestation.template.json", "answer-protocol.template.json"):
        payload = json.loads((source_dir / name).read_text(encoding="utf-8"))
        payload["dataset_version"] = target_version
        payload["dataset_manifest_sha256"] = target_manifest_sha256
        payload["owner_attested"] = False
        if name == "rights-attestation.template.json":
            payload["legal_clearance_claimed"] = False
        for field in ("reviewer_id", "reviewer_name", "signature", "reviewed_at"):
            payload[field] = ""
        _write_json(destination / name, payload)

    readme = """# Gaokao-Bench {target_version} 发布证据

本目录绑定 `data/{target_version}/manifest.json`，不包含任何签字或第三方授权。

| 项目 | 状态 |
|---|---|
| 数据版本 | `{target_version}` |
| 内容基线 | `{source_version}`，题目/答案/题分/裁图零修改 |
| 发布类型 | 内部发布候选（unsigned candidate） |
| `internal` | 阻塞：owner 尚未完成 74 题签字 |
| `public-code` | 阻塞：仓库尚无干净 commit/tag |
| `public` | NO-GO：独立复核、官方答案或替代协议、权利决定、清洁运行未完成 |

manifest SHA-256：

```text
{target_manifest_sha256}
```

两个 JSON 模板故意保持空白。程序不会代填姓名、签名或时间；`rights-attestation.json`
和 `answer-protocol.json` 只能由 owner 本人明确决定后另行创建。

内容来源与渠道见 `data/{target_version}/SOURCE_REGISTER.md`，复盘见
`docs/20-first-release-retrospective.md`。
""".format(
        target_version=target_version,
        source_version=source_version,
        target_manifest_sha256=target_manifest_sha256,
    )
    (destination / "README.md").write_text(readme, encoding="utf-8")

    notes_source = source_dir / "owner-review-notes.md"
    if notes_source.is_file():
        notes = notes_source.read_text(encoding="utf-8").replace(source_version, target_version)
        notes = notes.replace("## 本轮修正", "## 内容基线修正（0.0.1 原样继承）")
        notes = notes.replace(
            "## 结论",
            "## 结论\n\n- 本文件由内容基线 `%s` 重绑版本号生成，不新增审校结论；" % source_version,
        )
        notes = notes.replace(
            "- 题面、选项、图形依赖和评分容差共修正 4 处；",
            "- 内容基线累计修正题面、选项、图形依赖和评分容差 4 处；0.0.1 未修改题目内容；",
        )
        notes += (
            "\n## 0.0.1 发布边界\n\n"
            "`%s` 只做版号映射和发布材料化，不修改内容基线的答案、答案槽、题分、"
            "图形依赖或裁图。复核时仍以逐题验收台和内容指纹为准。\n" % target_version
        )
        (destination / "owner-review-notes.md").write_text(notes, encoding="utf-8")

    evidence = {
        "schema_version": "1.0",
        "source_dataset_version": source_version,
        "target_dataset_version": target_version,
        "target_dataset_manifest_sha256": target_manifest_sha256,
        "content_changed": False,
        "content_hashes": dict(sorted(content_hashes.items())),
        "signed_files_created": [],
        "unsigned_templates": [
            "rights-attestation.template.json",
            "answer-protocol.template.json",
        ],
    }
    _write_json(destination / "materialization-evidence.json", evidence)
    return {
        "release_evidence_dir": str(destination),
        "release_policy": str(destination / "release-policy.json"),
        "rights_template": str(destination / "rights-attestation.template.json"),
        "answer_protocol_template": str(destination / "answer-protocol.template.json"),
        "materialization_evidence": str(destination / "materialization-evidence.json"),
        "signed_files_created": [],
    }


def materialize_release_tag(
    source_dir: Union[Path, str],
    output_dir: Union[Path, str],
    *,
    target_version: str,
    created_at: Optional[str] = None,
    source_register: Optional[Union[Path, str]] = None,
    release_notes: Optional[Union[Path, str]] = None,
    release_evidence: bool = False,
) -> Dict[str, Any]:
    """Copy a frozen dataset to a release tag without content changes."""

    if not VERSION_PATTERN.fullmatch(target_version):
        raise TagMaterializationError("target version is not a valid immutable version: %s" % target_version)
    source = Path(source_dir).resolve()
    destination = Path(output_dir).resolve()
    source_dataset = load_dataset(source)
    validate_dataset(source_dataset)
    source_version = str(source_dataset.manifest["dataset_version"])
    if source_version == target_version:
        raise TagMaterializationError("target version must differ from the source version")
    if destination.name != target_version:
        raise TagMaterializationError("output directory name must equal target version")
    created_at = _parse_timestamp(created_at)
    source_manifest_sha = file_sha256(source / "manifest.json")
    before = core_content_hashes(source)

    _copy_immutable(source, destination)
    try:
        for name, rewrite in (
            ("DATA_CARD.md", _rewrite_data_card),
            ("REPRODUCE.md", _rewrite_reproduce),
            ("LICENSE_AND_RIGHTS.md", _rewrite_rights),
        ):
            path = destination / name
            if not path.is_file():
                raise TagMaterializationError("required release document is missing: %s" % name)
            rewrite(path, source_version, target_version)

        if source_register:
            shutil.copy2(Path(source_register), destination / "SOURCE_REGISTER.md")
        if release_notes:
            shutil.copy2(Path(release_notes), destination / "RELEASE_NOTES.md")

        _write_materialization_record(
            destination / "RELEASE_MATERIALIZATION.md",
            source_version=source_version,
            target_version=target_version,
            source_manifest_sha256=source_manifest_sha,
            created_at=created_at,
            hashes=before,
        )

        after = core_content_hashes(destination)
        if before != after:
            changed = sorted(set(before) | set(after))
            changed = [path for path in changed if before.get(path) != after.get(path)]
            raise TagMaterializationError("content changed during tag materialisation: %s" % ", ".join(changed[:8]))

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest["dataset_version"] = target_version
        manifest["created_at"] = created_at
        manifest["status"] = "release_candidate"
        manifest["release_channel"] = "internal_release_candidate"
        manifest["public_release"] = False
        manifest["content_baseline"] = source_version
        manifest["content_baseline_manifest_sha256"] = source_manifest_sha
        manifest["supersedes"] = source_version
        manifest["release_notes"] = "RELEASE_NOTES.md"
        manifest["source_register"] = "SOURCE_REGISTER.md"
        manifest["content_lineage"] = {
            "source_dataset_version": source_version,
            "source_manifest_sha256": source_manifest_sha,
            "materialization_document": "RELEASE_MATERIALIZATION.md",
            "materialization_script": "scripts/materialize_release_tag.py",
            "content_changed": False,
            "content_files": before,
        }
        source_release = (
            source_dataset.manifest.get("release")
            if isinstance(source_dataset.manifest.get("release"), Mapping)
            else {}
        )
        manifest["release"] = {
            "candidate": True,
            "channel": "internal_release_candidate",
            "public_ranking_allowed": bool(source_release.get("public_ranking_allowed", False)),
            "owner_signoff_required": True,
            "independent_review_required": True,
            "clean_model_run_required": True,
            "rights_decision_required_for_full_public_data": True,
            "score_label": "retrospective_public_2025",
        }
        quality = manifest.setdefault("quality", {})
        quality["release_candidate"] = True
        quality["content_baseline"] = source_version
        quality["content_corrections_this_version"] = 0
        quality["answer_corrections_this_version"] = 0
        quality["point_corrections_this_version"] = 0
        quality["asset_corrections_this_version"] = 0
        quality["image_dependency_corrections_this_version"] = 0
        quality["paper_total_corrections_this_version"] = 0
        manifest["rights"]["public_redistribution"] = "not_cleared_for_0.0.1_public_release"
        manifest["rights"]["rights_status"] = "owner_accepted_internal_risk_provenance_retained"

        manifest_files: Dict[str, Dict[str, Any]] = {}
        for relative in _tracked_files(destination):
            path = destination / relative
            manifest_files[relative] = {
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        manifest["files"] = manifest_files
        _write_json(destination / "manifest.json", manifest)

        target_dataset = load_dataset(destination)
        audit = validate_dataset(target_dataset)
        output_manifest_sha = file_sha256(destination / "manifest.json")
        result: Dict[str, Any] = {
            "status": "materialized",
            "source_version": source_version,
            "target_version": target_version,
            "source_manifest_sha256": source_manifest_sha,
            "output_manifest_sha256": output_manifest_sha,
            "content_changed": False,
            "content_file_count": len(before),
            "audit": audit,
        }
        if release_evidence:
            result["release_evidence"] = write_release_evidence(
                source.parent.parent,
                source_version=source_version,
                target_version=target_version,
                target_manifest_sha256=output_manifest_sha,
                content_hashes=before,
            )
        return result
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

