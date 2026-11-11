#!/usr/bin/env python3
"""Build a self-contained owner acceptance page for the 2026 National I paper."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" / "0.1.0-alpha.2026-n1"

KIND_LABELS = {
    "single_choice": "单选题",
    "multi_choice": "多选题",
    "fill_blank": "填空题",
    "catalog_subjective": "解答题",
}


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: %s" % path)
    return value


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return "data:%s;base64,%s" % (
        mime,
        base64.b64encode(path.read_bytes()).decode("ascii"),
    )


def e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def display_answer(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    return str(value)


def option_rows(options: Sequence[Mapping[str, Any]]) -> str:
    if not options:
        return ""
    return '<div class="options">' + "".join(
        '<div class="option"><span class="option-label">%s</span><span>%s</span></div>'
        % (e(option.get("label")), e(option.get("text")))
        for option in options
    ) + "</div>"


def source_links(
    answer_audit: Optional[Mapping[str, Any]],
    answer_sources: Mapping[str, Mapping[str, Any]],
) -> str:
    if not answer_audit:
        return '<span class="muted">解答题参考结论；来源见 SOURCE_REGISTER.md</span>'
    ids: List[str] = []
    for key in ("external_source_id", "secondary_source_id"):
        value = answer_audit.get(key)
        if value and value not in ids:
            ids.append(str(value))
    for value in answer_audit.get("supporting_source_ids", []):
        if value and value not in ids:
            ids.append(str(value))
    links = []
    for source_id in ids:
        source = answer_sources.get(source_id, {})
        if source:
            links.append(
                '<a href="%s" target="_blank" rel="noreferrer">%s</a>'
                % (e(source.get("url")), e(source.get("publisher") or source_id))
            )
        else:
            links.append('<span class="muted">%s</span>' % e(source_id))
    return " · ".join(links)


def audit_cards(manifest: Mapping[str, Any], data_dir: Path) -> str:
    reports = []
    for audit_id in ("A", "B", "C"):
        path = data_dir / "audit_reports" / ("audit-%s.json" % audit_id.lower())
        reports.append(read_json(path) if path.is_file() else None)
    names = {"A": "题面与裁图", "B": "答案与推导", "C": "工程与判分"}
    cards = []
    for audit_id, report in zip(("A", "B", "C"), reports):
        if report is None:
            cards.append(
                '<div class="audit-card pending"><div class="audit-title">审计 %s · %s</div>'
                '<div class="audit-value">待完成</div><p>审计报告尚未写入数据包。</p></div>'
                % (audit_id, names[audit_id])
            )
            continue
        passed = report.get("verdict") == "pass"
        cards.append(
            '<div class="audit-card %s"><div class="audit-title">审计 %s · %s</div>'
            '<div class="audit-value">%s</div><p>覆盖 %s 题 · 报告 SHA <code>%s</code></p></div>'
            % (
                "pass" if passed else "fail",
                audit_id,
                names[audit_id],
                "通过" if passed else "未通过",
                e(len(report.get("checked_question_ids", []))),
                e(sha256(data_dir / "audit_reports" / ("audit-%s.json" % audit_id.lower()))[:12]),
            )
        )
    pending = manifest.get("quality", {}).get("owner_signoff") == "pending"
    cards.append(
        '<div class="audit-card owner"><div class="audit-title">Owner 最终验收</div>'
        '<div class="audit-value">%s</div><p>最终签收由项目 owner 完成；本页面不会代签。</p></div>'
        % ("待你确认" if pending else "已完成")
    )
    return '<div class="audit-grid">' + "".join(cards) + "</div>"


def build_page(data_dir: Path) -> str:
    manifest = read_json(data_dir / "manifest.json")
    questions = read_jsonl(data_dir / "questions.jsonl")
    keys = {row["question_id"]: row for row in read_jsonl(data_dir / "answer_keys.jsonl")}
    subjective = read_jsonl(data_dir / "catalog" / "subjective_catalog.jsonl")
    papers = {row["id"]: row for row in read_jsonl(data_dir / "papers.jsonl")}
    paper = next(iter(papers.values()))
    answer_audit = {
        row["question_id"]: row for row in read_jsonl(data_dir / "answer_audit.jsonl")
    }
    question_audit = {
        row["question_id"]: row for row in read_jsonl(data_dir / "question_audit.jsonl")
    }
    answer_sources = {
        row["id"]: row for row in read_jsonl(data_dir / "answer_sources.jsonl")
    }

    rows = []
    for question in questions:
        qid = question["id"]
        rows.append(
            {
                "id": qid,
                "number": question["number"],
                "kind": question["type"],
                "points": question["points"],
                "content": question["content"],
                "options": question.get("options", []),
                "answer": keys[qid].get("canonical"),
                "derivation": question_audit.get(qid, {}).get("derivation", ""),
                "image": data_uri(data_dir / question["image_path"]),
                "auto_scored": True,
                "sources": source_links(answer_audit.get(qid), answer_sources),
            }
        )
    for item in subjective:
        item_sources = {
            "external_source_id": item.get("answer_source_id"),
            "secondary_source_id": item.get("secondary_answer_source_id"),
        }
        rows.append(
            {
                "id": item["id"],
                "number": item["number"],
                "kind": "catalog_subjective",
                "points": item["points"],
                "content": item["content"],
                "options": [],
                "answer": item.get("reference_answer"),
                "derivation": item.get("solution_outline", ""),
                "image": data_uri(data_dir / item["image_path"]),
                "auto_scored": False,
                "sources": source_links(item_sources, answer_sources),
            }
        )
    rows.sort(key=lambda row: int(row["number"]))

    cards = []
    for row in rows:
        label = KIND_LABELS.get(row["kind"], row["kind"])
        scored_badge = (
            '<span class="badge scored">自动评分</span>'
            if row["auto_scored"]
            else '<span class="badge catalog">目录收录 · 暂不自动评分</span>'
        )
        answer_label = "参考答案" if not row["auto_scored"] else "标准答案"
        outline_label = "解答要点" if not row["auto_scored"] else "独立推导"
        cards.append(
            f'''<article class="question-card" id="q-{e(row["id"])}" data-type="{e(row["kind"])}" data-search="{e((row["id"] + ' ' + row["content"] + ' ' + display_answer(row["answer"])).lower())}">
  <header class="q-head">
    <div class="q-id"><span class="q-index">{int(row["number"]):02d}</span><div><div class="q-title">{e(row["id"])}</div><div class="q-sub">{e(paper.get("title", "2026 全国卷 I 数学"))}</div></div></div>
    <div class="q-meta"><span class="badge kind">{e(label)}</span><span class="badge points">{e(row["points"])} 分</span>{scored_badge}</div>
  </header>
  <div class="q-grid">
    <figure class="paper-crop"><img src="{row["image"]}" alt="{e(row["id"])} 原卷裁图" loading="lazy"><figcaption>原卷裁图 · 可逐字对照题面</figcaption></figure>
    <section class="q-detail">
      <div class="block"><h3>题面</h3><div class="content">{e(row["content"]).replace(chr(10), '<br>')}</div>{option_rows(row["options"])}</div>
      <div class="answer-box"><div class="answer-kicker">{e(answer_label)}</div><div class="answer-value">{e(display_answer(row["answer"]))}</div></div>
      <div class="block outline"><h3>{e(outline_label)}</h3><p>{e(row["derivation"]).replace(chr(10), '<br>')}</p></div>
      <div class="sources"><span class="source-label">答案来源</span>{row["sources"]}</div>
      <label class="check"><input type="checkbox" data-qid="{e(row["id"])}"><span>我已核对这一题</span></label>
    </section>
  </div>
</article>'''
        )

    counts = manifest["counts"]
    points = manifest["points"]
    manifest_sha = sha256(data_dir / "manifest.json")
    generated = manifest.get("created_at", "")
    rights = manifest.get("rights", {})
    review_status = manifest.get("quality", {}).get("review_status", "")
    audit_html = audit_cards(manifest, data_dir)
    toc = "".join(
        '<a class="toc-item" href="#q-%s" data-target="%s">%02d</a>'
        % (e(row["id"]), e(row["kind"]), int(row["number"]))
        for row in rows
    )
    document = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaokao-Bench · 2026 全国卷 I 数学验收</title>
<style>
:root{--paper:#f3efe3;--paper-2:#e9e2d1;--card:#fffdf7;--card-2:#faf6ea;--ink:#171513;--muted:#6d675b;--line:#d8d0bd;--accent:#1f7767;--accent-2:#15594e;--accent-soft:#dcece6;--warm:#bd7b2a;--danger:#a74b43;--shadow:0 18px 50px rgba(49,39,23,.09);} 
@media(prefers-color-scheme:dark){:root{--paper:#151816;--paper-2:#1b211e;--card:#202622;--card-2:#252d28;--ink:#f2eee2;--muted:#b5ad9d;--line:#3a443d;--accent:#72b7a5;--accent-2:#9ad4c5;--accent-soft:#203b34;--warm:#e1ac65;--danger:#df8b82;--shadow:0 18px 50px rgba(0,0,0,.28);}}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--paper);font-family:"Noto Sans CJK SC","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;line-height:1.65}body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(80,70,48,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(80,70,48,.045) 1px,transparent 1px);background-size:32px 32px;mask-image:linear-gradient(to bottom,black,transparent 80%)}a{color:var(--accent-2);text-decoration:none}a:hover{text-decoration:underline}.wrap{width:min(1180px,calc(100% - 40px));margin:0 auto}.hero{padding:64px 0 38px}.brand{display:flex;align-items:center;gap:12px;font-size:12px;letter-spacing:.24em;font-weight:800;text-transform:uppercase;color:var(--accent-2)}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border:1px solid var(--accent);border-radius:50%;font-size:11px;letter-spacing:.08em}.eyebrow{margin-top:34px;color:var(--muted);font-size:13px;letter-spacing:.08em}.hero h1{max-width:850px;margin:10px 0 16px;font-family:"Noto Serif CJK SC","Songti SC",serif;font-size:clamp(38px,6vw,72px);line-height:1.06;letter-spacing:-.035em;font-weight:650}.lede{max-width:740px;color:var(--muted);font-size:18px}.status-line{display:flex;flex-wrap:wrap;gap:8px;margin-top:22px}.status-pill{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid var(--line);border-radius:999px;background:var(--card);font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:var(--warm)}.dot.green{background:var(--accent)}.grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:22px 0 30px}.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:var(--shadow)}.stat strong{display:block;font-family:"Noto Serif CJK SC",serif;font-size:30px;line-height:1.15}.stat span{display:block;margin-top:4px;color:var(--muted);font-size:12px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:40px 0 14px}.section-head h2{margin:0;font-family:"Noto Serif CJK SC",serif;font-size:28px}.section-head p{margin:0;color:var(--muted);font-size:13px}.audit-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.audit-card{border:1px solid var(--line);background:var(--card);border-radius:16px;padding:18px;box-shadow:var(--shadow)}.audit-card.pass{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}.audit-card.fail{border-color:var(--danger)}.audit-card.owner{border-style:dashed}.audit-title{font-size:12px;color:var(--muted);letter-spacing:.04em}.audit-value{margin:5px 0;font-size:21px;font-weight:700}.audit-card p{margin:0;color:var(--muted);font-size:12px}.audit-card code{font-size:11px}.notice{margin:18px 0 0;padding:16px 18px;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:10px;background:var(--card-2);color:var(--muted);font-size:13px}.notice strong{color:var(--ink)}.toolbar{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:12px 0;margin-top:24px;background:color-mix(in srgb,var(--paper) 92%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}.filters{display:flex;flex-wrap:wrap;gap:6px}.filter{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:7px 11px;font:inherit;font-size:12px;cursor:pointer}.filter.active{background:var(--accent);color:#fff;border-color:var(--accent)}.search{margin-left:auto;width:min(300px,100%);border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);padding:9px 11px;font:inherit;font-size:13px}.progress{font-size:12px;color:var(--muted);white-space:nowrap}.toc{display:grid;grid-template-columns:repeat(19,minmax(0,1fr));gap:5px;margin:18px 0 24px}.toc-item{display:grid;place-items:center;height:32px;border:1px solid var(--line);border-radius:8px;background:var(--card);font-size:11px;font-weight:700;color:var(--ink)}.toc-item:hover{border-color:var(--accent);color:var(--accent-2);text-decoration:none}.question-card{margin:18px 0;background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);overflow:hidden;scroll-margin-top:90px}.q-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card-2)}.q-id{display:flex;align-items:center;gap:13px}.q-index{display:grid;place-items:center;width:40px;height:40px;border-radius:50%;border:1px solid var(--accent);color:var(--accent-2);font-family:"Noto Serif CJK SC",serif;font-weight:700}.q-title{font-weight:700;letter-spacing:.025em}.q-sub{color:var(--muted);font-size:11px}.q-meta{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.badge{display:inline-flex;align-items:center;padding:4px 8px;border:1px solid var(--line);border-radius:999px;font-size:11px;color:var(--muted);background:var(--card)}.badge.scored{border-color:var(--accent);background:var(--accent-soft);color:var(--accent-2)}.badge.catalog{border-color:var(--warm);color:var(--warm)}.q-grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(330px,.9fr);gap:0}.paper-crop{margin:0;padding:20px;background:var(--paper-2);border-right:1px solid var(--line)}.paper-crop img{display:block;width:100%;height:auto;background:#fff;border:1px solid var(--line);border-radius:8px}.paper-crop figcaption{padding-top:8px;color:var(--muted);font-size:11px}.q-detail{padding:20px}.block h3,.outline h3{margin:0 0 7px;font-size:12px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase}.content{white-space:normal;font-family:"Noto Serif CJK SC","Songti SC",serif;font-size:16px;line-height:1.8}.options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:12px}.option{display:flex;gap:8px;padding:8px 10px;border:1px solid var(--line);border-radius:9px;font-size:13px;background:var(--card-2)}.option-label{font-weight:800;color:var(--accent-2)}.answer-box{margin:16px 0;padding:14px 16px;border-radius:12px;background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 35%,var(--line))}.answer-kicker{font-size:11px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase}.answer-value{margin-top:2px;font-family:"Noto Serif CJK SC",serif;font-size:24px;font-weight:700;color:var(--accent-2)}.outline p{margin:0;color:var(--muted);font-size:13px}.sources{padding-top:13px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}.source-label{margin-right:7px;font-weight:700;color:var(--ink)}.muted{color:var(--muted)}.check{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:12px;color:var(--muted);cursor:pointer}.check input{accent-color:var(--accent)}.hidden{display:none!important}.foot{margin:48px 0 70px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.foot code{word-break:break-all}@media(max-width:900px){.grid{grid-template-columns:repeat(3,1fr)}.audit-grid{grid-template-columns:repeat(2,1fr)}.q-grid{grid-template-columns:1fr}.paper-crop{border-right:0;border-bottom:1px solid var(--line)}.toc{grid-template-columns:repeat(10,1fr)}}@media(max-width:560px){.wrap{width:min(100% - 24px,1180px)}.hero{padding-top:38px}.grid{grid-template-columns:repeat(2,1fr)}.audit-grid{grid-template-columns:1fr}.q-head{align-items:flex-start;flex-direction:column}.q-meta{justify-content:flex-start}.options{grid-template-columns:1fr}.toc{grid-template-columns:repeat(7,1fr)}.search{margin-left:0;width:100%}}
</style>
</head>
<body>
<main class="wrap">
  <header class="hero">
    <div class="brand"><span class="brand-mark">GB</span><span>Gaokao-Bench</span></div>
    <div class="eyebrow">OWNER ACCEPTANCE · 2026 NATIONAL I MATHEMATICS</div>
    <h1>2026 全国卷 I<br>数学验收台</h1>
    <p class="lede">一套卷，19 题，150 分。这里同时展示原卷裁图、转录题面、标准答案、独立推导和来源证据。最终签收由你完成。</p>
    <div class="status-line"><span class="status-pill"><span class="dot"></span>验收候选 · 未发布</span><span class="status-pill"><span class="dot green"></span>三方审计</span><span class="status-pill">Owner 签字：待确认</span><span class="status-pill">CC BY 4.0</span></div>
  </header>
  <section class="grid" aria-label="范围摘要">
    <div class="stat"><strong>19</strong><span>全卷题目</span></div>
    <div class="stat"><strong>150</strong><span>全卷分值</span></div>
    <div class="stat"><strong>14 / 73</strong><span>自动评分题 / 分</span></div>
    <div class="stat"><strong>5 / 77</strong><span>解答题 / 分</span></div>
    <div class="stat"><strong>4</strong><span>答案来源</span></div>
    <div class="stat"><strong>19</strong><span>原卷裁图</span></div>
  </section>
  <div class="section-head"><div><h2>审计状态</h2><p>A 题面保真 · B 答案推导 · C 工程判分</p></div><p>当前状态：__REVIEW_STATUS__</p></div>
  __AUDIT_HTML__
  <div class="notice"><strong>怎么验收：</strong>先看左侧原卷裁图和右侧题面是否一致；再核对答案与推导。若有问题，直接记下题号和你的判断。勾选进度只保存在本机浏览器，不会自动生成签字，也不会修改数据包。</div>
  <div class="section-head"><div><h2>逐题验收</h2><p>点击题号可直达；最后五题是解答题目录，不进入当前自动评分分母。</p></div><p class="progress" id="progress">已核对 0 / 19</p></div>
  <div class="toolbar"><div class="filters"><button class="filter active" data-filter="all">全部</button><button class="filter" data-filter="single_choice">单选题</button><button class="filter" data-filter="multi_choice">多选题</button><button class="filter" data-filter="fill_blank">填空题</button><button class="filter" data-filter="catalog_subjective">解答题</button></div><input class="search" id="search" type="search" placeholder="搜索题号、题干或答案"></div>
  <nav class="toc" aria-label="题目导航">__TOC__</nav>
  <div id="cards">__CARDS__</div>
  <footer class="foot"><p>数据版本 <code>__VERSION__</code> · manifest SHA-256 <code>__MANIFEST_SHA__</code></p><p>生成时间 __GENERATED__ · 自动评分 <code>score.weighted_points</code> · 数据许可 __DATA_LICENSE__ · 代码许可 __CODE_LICENSE__</p><p>本页面不调用模型 API，不读取 API Key，不替代 owner 签收。</p></footer>
</main>
<script>
const cards=[...document.querySelectorAll('.question-card')];const buttons=[...document.querySelectorAll('.filter')];const search=document.querySelector('#search');let active='all';function apply(){const q=(search.value||'').trim().toLowerCase();cards.forEach(c=>{const okType=active==='all'||c.dataset.type===active;const okText=!q||c.dataset.search.includes(q);c.classList.toggle('hidden',!(okType&&okText));});}buttons.forEach(b=>b.addEventListener('click',()=>{active=b.dataset.filter;buttons.forEach(x=>x.classList.toggle('active',x===b));apply();}));search.addEventListener('input',apply);function updateProgress(){const boxes=[...document.querySelectorAll('.check input')];const n=boxes.filter(x=>x.checked).length;document.querySelector('#progress').textContent=`已核对 ${n} / ${boxes.length}`;}document.querySelectorAll('.check input').forEach(box=>{const key='gb-2026-n1-check-'+box.dataset.qid;box.checked=localStorage.getItem(key)==='1';box.addEventListener('change',()=>{localStorage.setItem(key,box.checked?'1':'0');updateProgress();});});updateProgress();function jump(){let h=location.hash.slice(1);if(h.startsWith('q='))h='q-'+h.slice(2);const el=h?document.getElementById(h):null;if(el){const root=document.documentElement;const prior=root.style.scrollBehavior;root.style.scrollBehavior='auto';el.scrollIntoView({block:'start',behavior:'instant'});root.style.scrollBehavior=prior;el.animate([{outline:'2px solid transparent'},{outline:'2px solid #1f7767'},{outline:'2px solid transparent'}],{duration:1500});}}window.addEventListener('hashchange',jump);window.addEventListener('load',()=>requestAnimationFrame(jump));requestAnimationFrame(jump);
</script>
</body>
</html>
"""
    replacements = {
        "__REVIEW_STATUS__": e(review_status),
        "__AUDIT_HTML__": audit_html,
        "__CARDS__": "".join(cards),
        "__TOC__": toc,
        "__VERSION__": e(manifest.get("dataset_version")),
        "__MANIFEST_SHA__": e(manifest_sha),
        "__GENERATED__": e(generated),
        "__DATA_LICENSE__": e(rights.get("data_license", "")),
        "__CODE_LICENSE__": e(rights.get("code_license", "")),
    }
    for key, value in replacements.items():
        document = document.replace(key, value)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    version = read_json(data_dir / "manifest.json")["dataset_version"]
    output = (args.output or ROOT / "reports" / "acceptance" / version / "index.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_page(data_dir), encoding="utf-8")
    print(json.dumps({"output": str(output), "bytes": output.stat().st_size}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
