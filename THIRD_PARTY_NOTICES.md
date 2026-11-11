# Third-Party Notices

Gaokao-Bench data packages reproduce public examination material and use
independent answer sources for cross-checking. The notices below identify the
upstream publishers and the role each source plays. They do not imply that any
publisher endorses Gaokao-Bench, a benchmark result, or a public ranking.

## 2026 National I Mathematics Candidate

Package version: `0.1.0-alpha.2026-n1`

### Examination paper

- Publisher: `deekur/gaokaomath`
- Role: frozen three-page source PDF. The PDF contains the question paper and no
  answer key.
- URL: <https://github.com/deekur/gaokaomath/tree/main/%E6%99%AE%E9%80%9A%E9%AB%98%E8%80%83/2026>
- Frozen PDF SHA-256:
  `89adefb3506ff19ed4b6800422d8bcd655940fad8f093a94a1e18c6954623f11`

### Answer and solution cross-checks

- 高考100, “2026新高考全国一卷数学答案解析”: complete answer and solution
  images. <https://www.gk100.com/read_122151413.htm>
- 深圳本地宝, “2026全国新高考一卷数学试题及答案”: text answer cross-check.
  <https://sz.bendibao.com/edu/202669/1005149.shtm>
- 自主选拔在线, “2026年全国新高考一卷数学试题及答案解析”: supporting answer
  analysis for questions 1-5.
  <https://www.zizzs.com/gk/shitiku/222593_3.html>
- 搜狐号「一帮N辅导」, “2026年全国新高考一卷数学试题及参考答案”: teacher-reviewed
  answer cross-check. The article states that its answers are for reference only.
  <https://news.sohu.com/a/1034912500_121124035>

The package records exact source identifiers, URLs, locators, image indices, and
retrieval dates in `sources.jsonl` and `answer_sources.jsonl`. Question text is
checked against the frozen PDF. Answers are independently derived and then
cross-checked against the sources above; no answer source is represented as an
official examination-authority key.

## Attribution and corrections

The data package is distributed under CC BY 4.0 as stated in `LICENSE-DATA`.
Reuse must credit Gaokao-Bench, identify the exact dataset version, link to the
license, and indicate whether changes were made. Keep this notice and the source
registers with redistributed copies.

Corrections can be filed at
<https://github.com/Knove/Gaokao-Bench/issues>. Please identify the exact dataset
version and question ID.
