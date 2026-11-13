# Third-Party Notices

Gaokao-Bench data packages reproduce public examination material and use
independent answer sources for cross-checking. The notices below identify the
upstream publishers and the role each source plays. They do not imply that any
publisher endorses Gaokao-Bench, a benchmark result, or a public ranking.

## 2026 National I Mathematics Candidate

Package version: `0.1.0-alpha.2026-n1.4`

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
  complete answer and solution images. Every answer image is also frozen as
  stem evidence because the question text is visible on the same page; the
  removed six-page long image is not used as page evidence.
  <https://www.gk100.com/read_122151413.htm>
- 中国教育在线, “2026年新高考一卷数学答案及解析”: seven answer-and-solution
  pages that also reproduce the question stems. Questions 13, 14, 15, 18 and 19
  contain reproducible answer errors, so those rows are recorded as rejected and
  do not count as joint-answer evidence.
  <https://www.eol.cn/m/gaokao/202606/t20260608_2742417.shtml>
- 原创力文档（文档 7162100005011121）, “2026年全国新高考一卷数学试题及答案”:
  a 17-page document whose public preview exposes pages 1-11. Pages 1-4 contain
  the complete paper and pages 5-11 contain complete solutions for questions
  1-16. The preview cutoff does not support a claim for questions 17-19.
  <https://max.book118.com/html/2026/0607/7162100005011121.shtm>
- 人人文库（上传文档 528467279）, “2026年全国新高考一卷数学试题及答案”:
  16-page document preview containing question text, answers, and worked
  solutions.
  <https://www.renrendoc.com/paper/528467279.html>
- 网易号／新浪财经（原文：数海漫游）, “2026年全国一卷（数学）真题及全解析”:
  15 pages of handwritten solutions plus four complete question-paper pages;
  <https://m.163.com/dy/article/KUSEQ8DV05568W0A.html>
- 自主选拔在线, “2026年全国新高考一卷数学试题及答案解析”: four question-paper
  images plus 30 answer pages. It is a joint stem-and-answer source, not an
  answer-only check. <https://www.zizzs.com/gk/shitiku/222593_3.html>

### Answer-only checks

These sources are recorded as supplementary answer checks. They do not count
toward the joint stem-and-answer gate.

- 深圳本地宝, “2026全国新高考一卷数学试题及答案”: text answer check.
  <https://sz.bendibao.com/edu/202669/1005149.shtm>
- 搜狐号「一帮N辅导」, “2026年全国新高考一卷数学试题及参考答案”: teacher-reviewed
  answer-only check. The article states that its answers are for reference only.
  <https://news.sohu.com/a/1034912500_121124035>

The package records exact source identifiers, URLs, locators, image indices, and
retrieval dates in `sources.jsonl`, `answer_sources.jsonl`, and
`source_audit.jsonl`. Every catalogued question has at least eight independent
stem-source groups and at least four independent sources that contain both the
matching stem and the correct answer. Those are the enforced n1.4 thresholds,
not decorative counts. Chinese Education Online and the other China Education
Online paper page share one independence group even though they are separate
URLs. Wrong-answer rows remain visible with their observed answer and rejection
reason. Answers are independently derived and then cross-checked; no source is
represented as an official examination-authority key.

## Attribution and corrections

The data package is distributed under CC BY 4.0 as stated in `LICENSE-DATA`.
Reuse must credit Gaokao-Bench, identify the exact dataset version, link to the
license, and indicate whether changes were made. Keep this notice and the source
registers with redistributed copies.

Corrections can be filed at
<https://github.com/Knove/Gaokao-Bench/issues>. Please identify the exact dataset
version and question ID.
