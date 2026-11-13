# Third-Party Notices

Gaokao-Bench data packages reproduce public examination material and use
independent answer sources for cross-checking. The notices below identify the
upstream publishers and the role each source plays. They do not imply that any
publisher endorses Gaokao-Bench, a benchmark result, or a public ranking.

## 2026 National I Mathematics Candidate

Package version: `0.1.0-alpha.2026-n1.2`

### Question-paper sources

- Publisher: `deekur/gaokaomath`
- Role: frozen three-page question-paper PDF, used as the primary stem source.
  The PDF contains no answer key.
- URL: <https://github.com/deekur/gaokaomath/tree/main/%E6%99%AE%E9%80%9A%E9%AB%98%E8%80%83/2026>
- Frozen PDF SHA-256:
  `89adefb3506ff19ed4b6800422d8bcd655940fad8f093a94a1e18c6954623f11`
- 无忧考网, “2026年全国新高考一卷数学试题”: independent stem cross-check via
  a page-by-page Word preview.
  <https://www.51test.net/show/11298327.html>
- 中国教育在线, “2026年新高考一卷数学试题”: independent stem cross-check via
  four full-paper images.
  <https://www.eol.cn/kaoshi/gaokao/gkst/202606/t20260607_2742333.shtml>

### Combined stem-and-answer sources

- 高考100, “2026新高考全国一卷数学答案解析”: question-paper images plus
  complete answer and solution images; one of three independent joint sources
  used for the source gate. <https://www.gk100.com/read_122151413.htm>
- 人人文库（上传文档 528467279）, “2026年全国新高考一卷数学试题及答案”:
  16-page document preview containing question text, answers, and worked
  solutions; one of three independent joint sources used for the source gate.
  <https://www.renrendoc.com/paper/528467279.html>
- 网易号／新浪财经（原文：数海漫游）, “2026年全国一卷（数学）真题及全解析”:
  15 pages of handwritten solutions plus four complete question-paper pages;
  one of three independent joint sources used for the source gate.
  <https://m.163.com/dy/article/KUSEQ8DV05568W0A.html>

### Answer-only checks

These sources are recorded as supplementary answer checks. They do not count
toward the required two independent sources that contain both the matching stem
and the answer.

- 深圳本地宝, “2026全国新高考一卷数学试题及答案”: text answer check.
  <https://sz.bendibao.com/edu/202669/1005149.shtm>
- 自主选拔在线, “2026年全国新高考一卷数学试题及答案解析”: answer-only check for
  questions 1-5.
  <https://www.zizzs.com/gk/shitiku/222593_3.html>
- 搜狐号「一帮N辅导」, “2026年全国新高考一卷数学试题及参考答案”: teacher-reviewed
  answer-only check. The article states that its answers are for reference only.
  <https://news.sohu.com/a/1034912500_121124035>

The package records exact source identifiers, URLs, locators, image indices, and
retrieval dates in `sources.jsonl`, `answer_sources.jsonl`, and
`source_audit.jsonl`. Every catalogued question has six independent stem-source
groups and three independent sources that contain both the matching stem and the
answer. The hard gate requires at least two of each; the additional source is an
independence margin, not a claim about any publisher's authority. Answers are
independently derived and then cross-checked; no source is represented as an
official examination-authority key.

## Attribution and corrections

The data package is distributed under CC BY 4.0 as stated in `LICENSE-DATA`.
Reuse must credit Gaokao-Bench, identify the exact dataset version, link to the
license, and indicate whether changes were made. Keep this notice and the source
registers with redistributed copies.

Corrections can be filed at
<https://github.com/Knove/Gaokao-Bench/issues>. Please identify the exact dataset
version and question ID.
