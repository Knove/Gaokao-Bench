<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/logo-dark.svg">
    <img src=".github/readme/logo-light.svg" width="360" alt="Gaokao-Bench 标志">
  </picture>
</p>

<h1 align="center">Gaokao-Bench</h1>

<p align="center"><strong>真实试卷。固定分母。可复现的分数。</strong></p>

<p align="center">面向 AI 系统的确定性、按题分加权基准，<br>题目来自真实中国高考客观题。</p>

<p align="center"><a href="README.md">English</a>&nbsp;&nbsp;·&nbsp;&nbsp;简体中文</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="许可证：Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-2E7D32?style=flat-square"></a>
  <a href="CITATION.cff"><img alt="版本 0.0.1" src="https://img.shields.io/badge/version-0.0.1-171513?style=flat-square"></a>
  <a href="SCOPE.md"><img alt="纯代码发布" src="https://img.shields.io/badge/scope-code--only-C83D2B?style=flat-square"></a>
  <a href="#评分契约"><img alt="确定性评分" src="https://img.shields.io/badge/scoring-deterministic-20786A?style=flat-square"></a>
</p>

<p align="center">
  <a href="#为什么是-gaokao-bench"><strong>项目定位</strong></a> &nbsp;·&nbsp;
  <a href="#基准概览"><strong>基准概览</strong></a> &nbsp;·&nbsp;
  <a href="#工作方式"><strong>工作方式</strong></a> &nbsp;·&nbsp;
  <a href="#快速开始"><strong>快速开始</strong></a> &nbsp;·&nbsp;
  <a href="#评分契约"><strong>评分</strong></a> &nbsp;·&nbsp;
  <a href="#纯代码边界"><strong>边界</strong></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/hero-zh-dark.svg">
    <img src=".github/readme/hero-zh-light.svg" width="100%" alt="Gaokao-Bench：真实试卷、固定分母、可复现分数">
  </picture>
</p>

> [!IMPORTANT]
> **本仓库只包含代码。** 高考题目、答案键、来源 PDF、页面图片、裁图、复核包和答案证据均单独分发。软件许可证不覆盖第三方试卷素材。详见 [`SCOPE.md`](SCOPE.md)。

Gaokao-Bench 把试卷当作测量工具，而不是一组提示词。每道计分题都有经过复核的题分、规范的答案路径和固定的分母位置。一次运行可以重放，一份作答可以重新解析，每一次失败都会留在分数里。

项目遵循一条简单原则：**一个基准分数，应该经得起重跑、重解析和重新审计。**

## 为什么是 Gaokao-Bench？

| 常见基准问题 | Gaokao-Bench 的处理 |
| --- | --- |
| LLM 裁判把另一个模型带进了测量过程 | 使用确定性解析器和规范校验器；评分链路里没有裁判模型 |
| 只用百分比，掩盖了题目分值不同 | 按题分计分，而不只是数题目；分母是全部已复核题分之和 |
| 请求失败后把题目从分母中删掉 | 缺失、格式错误、超时和 provider 错误全部计 0，并保留在分母中 |
| 答案无法回溯到证据 | 来源、答案审计、分值审计、裁图哈希和复核状态随题目一起保存 |
| 公开题目带来污染争议 | 2025 年结果必须标注 `retrospective_public_2025`，禁止声称“新鲜能力” |
| 分数缺少版本上下文 | 每次运行绑定数据集、Harness、评分器、Prompt、模型快照和重复次数 |

## 基准概览

以下是配套 **0.0.1 数据集**的统计。数据集按设计不放在本仓库中。

| | 0.0.1 客观题范围 |
| --- | --- |
| 年份与科目 | 2025 · 数学 |
| 试卷 | 5 套：全国卷 I、全国卷 II、北京卷、上海卷、天津卷 |
| 计分题目 | **74 道**——39 道单选、6 道多选、29 道填空 |
| 计分总分 | **353 分** |
| 答案槽 | **78 个** |
| 图形必需题 | **4 道** |
| 文本模式 | **70 题 / 334 分** |
| 视觉模式 | **74 题 / 353 分** |
| 已编目但不计分 | 26 道主观题 / 397 分 |
| 官方答案来源 | 不作官方来源声明；复核流程记录在数据包中 |

353 分是逐题复核分值的总和，**不是**把 750 分卷面总分线性缩放后的结果，也不能被这样展示。

## 工作方式

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/readme/pipeline-zh-dark.svg">
    <img src=".github/readme/pipeline-zh-light.svg" width="100%" alt="Gaokao-Bench 流程：版本化数据包经过校验、匿名化提示、文本或视觉模型，再由确定性评分器计分；发布完整性覆盖审计、人工复核、不可变版本与发布门禁">
  </picture>
</p>

两条路径被刻意分开：

- **评测路径是确定性的。** 模型每次只看到一道匿名题；评分器看到的是结构化答案和已复核题分。
- **发布路径必须经过人工复核。** 来源证据、独立推导、媒体交叉核对、owner 复核和两名独立审校全部闭合后，才能材料化新的不可变数据版本。

Harness 不会把年份、卷种、题号、`question_id`、来源或答案发给 provider。它只要求模型输出一个小型 JSON 对象，评分在模型之外完成。

## 快速开始

### 1. 从源码安装

```bash
git clone <repository-url>
cd gaokao-bench

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

python -m pytest -q
```

只需要视觉依赖时，可安装 `.[harness]`。运行环境要求 Python 3.9 或更高版本。

### 2. 获取已复核的数据包

纯代码仓库不会偷偷下载数据集。请把工具指向单独版本化的数据包：

```text
gaokao-bench-data/
  manifest.json
  papers.jsonl
  questions.jsonl
  answer_keys.jsonl
  assets/
  ...
```

数据包自带 manifest、复核 lineage、权利说明和内容哈希。不要把不同数据集版本的文件混在一起。

### 3. 校验与评分

```bash
export GB_DATA=/path/to/gaokao-bench-data

python3 scripts/validate.py --data-dir "$GB_DATA"
python3 scripts/evaluate.py \
  --data-dir "$GB_DATA" \
  --answers submission.json
```

安装后的 CLI 提供同样的核心操作：

```bash
gaokao-bench validate --data-dir "$GB_DATA"
gaokao-bench evaluate --data-dir "$GB_DATA" --answers submission.json
```

提交文件以题目 ID 为键：

```json
{
  "answers": {
    "<question-id>": "C",
    "<multi-choice-id>": "ACD",
    "<two-slot-fill-id>": ["2", "π/4"]
  }
}
```

### 4. 运行评测 Harness

调用真实模型需要显式选择 provider，并在环境变量中提供凭据。凭据不会写入报告。

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

支持的 provider 有 `anthropic`、`openai_compatible`、`gemini_cli` 和 `mock`。使用 `--prepare-only` 可以在不调用模型的情况下生成并审计 Prompt。

## 评分契约

```text
score = earned_points / fixed_points
```

| 规则 | 行为 |
| --- | --- |
| 单题得分 | 默认全对得分，否则为 0；所有答案槽必须匹配 |
| 分值权重 | 每道题按已复核题分计入 |
| 失败处理 | 缺失、格式错误、超时和 provider 错误均计 `0` |
| 分母 | 永不收缩；失败样本保留在固定总分里 |
| 解析 | 确定性数学规范化与受限的自定义校验器 |
| 裁判 | 评分链路不使用 LLM 裁判 |
| 报告 | 同时报告题目正确率、完全正确率、题分正确率和各卷拆分 |

两种模式使用同一套评分器：

| 模式 | 变化 | 主指标 | 固定分母 |
| --- | --- | --- | --- |
| 文本 | 4 道图形必需题不发送给 provider，按策略计 `0` | **70 题 / 334 分** | **353 分** |
| 视觉 | 只额外提供 4 张必需图形裁块 | **74 题 / 353 分** | **353 分** |

0.0.1 暂不启用多选、多空的部分分。任何试卷分数都不会外推成完整 750 分高考成绩。

## 可复现性

每次评测运行都会记录：

- 数据集版本和 manifest SHA-256；
- Harness、评分器和 Prompt 版本；
- provider、模型快照、base URL 类别和生成参数；
- 重复次数、超时、重试策略、并发数和每条 Prompt 的哈希；
- 原始回答、解析状态、获得分数和失败原因。

默认协议是独立单题请求，temperature 为 `0`，重复 3 次。失败请求按 0 分保留，不会被从样本中删除。

## 复核与发布完整性

数据工作流的设计目标是让“静默修改”变得困难：

1. **来源台账**记录每套试卷和每条答案声明的来源。
2. **题目审计**检查题干、选项和来源是否真正一致。
3. **答案审计**结合独立推导与媒体或文档交叉核对。
4. **分值审计**重建每套试卷的评分结构。
5. **Owner 复核**对裁定后的答案、分值和来源定位签字。
6. **两名独立审校**复核每一行答案和分值。
7. **材料化**写入新的不可变数据版本及复核账本。
8. **发布门禁**分别检查 internal、纯代码和完整公开三个目标。

复核导入是只读的。材料化器会拒绝提议修改、未解决争议、不完整行、过期哈希和缺失签字。

## 纯代码边界

本仓库包含评测工具链：

- Python 评分器与解析器；
- 文本和视觉必需题 Harness；
- 复核导入与材料化工作流；
- 发布门禁与发布包校验器；
- 公开契约测试、许可证和项目原创文档；
- README 原创图形与翻译。

本仓库不包含：

- 题干、选项、答案键或逐题分值；
- 来源 PDF、页面图片、裁图、截图或文档预览；
- 答案证据、来源台账、owner 复核包或复核报告；
- 内部数据集构建器或逐题变更日志。

这条边界由发布白名单和独立发布包校验器强制检查。详见 [`SCOPE.md`](SCOPE.md)。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `gaokao_bench/` | 评分、Harness、复核、材料化和发布核心逻辑 |
| `scripts/` | 校验、评分、评测运行和发布检查 CLI |
| `tests/` | 不依赖试卷数据的公开契约测试 |
| `.github/readme/` | 项目原创、随主题切换的 README 图形 |
| `SCOPE.md` | 代码/数据边界、权利立场与污染策略 |
| `CITATION.cff` | 机器可读引用元数据 |

## 当前状态与路线图

`0.0.1` 在一个范围很窄、已完成题分审计的集合上，验证了整套测量闭环：

- 2025 年数学客观题；
- 5 套试卷；
- 74 道计分题、353 分固定总分；
- 文本和视觉必需题两种模式；
- 确定性评分与可审计发布流程。

纯代码发布不构成对配套数据集作为公开排行榜的认证。完整数据是否发布，仍由发布门禁下的独立决策决定。

长期扩展会按顺序推进：

1. 扩展到更多年份的数学客观题。
2. 增加其他高考科目，但不削弱来源、答案和分值审计契约。
3. 单独研究主观题评分，引入评分细则、人工裁定和明确的不确定性。
4. 让代码发布、数据权利和基准声明继续保持为彼此独立的决策。

新科目不会采用“先抓取、再清理”的方式加入。只有当来源、答案、分值和复核流程都能闭合时，一个科目才会进入基准。

## 负责任使用

- **污染风险：** 2025 年题目已经公开，可能存在于训练或检索语料中。结果必须标注 `retrospective_public_2025`，不得声称 held-out 或“新鲜能力”。
- **分数上下文：** 报告分数时始终同时报告数据集、Harness、评分器、Prompt、模型快照、模态和重复次数。
- **权利：** Apache-2.0 软件许可证不授予试卷文本、答案页、PDF 或图片的使用权。
- **覆盖范围：** 一个科目、一个年份验证的是方法学，不代表通用高考能力。
- **失败样本：** provider 错误仍然是 0 分。比较模型时不能删除失败题。

## 参与贡献

欢迎在不破坏测量契约的前提下参与贡献：

- 不要向代码 PR 中加入第三方高考题目、答案页、PDF 或裁图；
- 解析器、评分器或发布边界变更需要配套的公开测试；
- 提交 PR 前运行 `python -m pytest -q`；
- 用户可见行为变更时，同时更新 `README.md` 与 `README.zh-CN.md`；
- 明确说明任何会影响分数、分母、Prompt 或复核规则的改动。

英文 README 是规范版本，简体中文在仓库内同步维护。新增语言需要有能够长期同步的母语维护者。

## 引用

如果 Gaokao-Bench 对你的研究或评测工作有帮助，请引用本仓库和精确的数据集版本：

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

机器可读的元数据见 [`CITATION.cff`](CITATION.cff)。

## 许可

| 层级 | 许可 |
| --- | --- |
| 代码、评分器、Harness 和测试 | **Apache-2.0**——见 [`LICENSE`](LICENSE) |
| 项目原创文档与元数据 | **CC BY 4.0**——见 [`LICENSE-DATA`](LICENSE-DATA) |
| 高考题目、答案页、PDF、图片及其他第三方素材 | **不包含在本仓库中，也不由本仓库重新许可** |

---

<p align="center"><strong>测量试卷，而不是测量提示词。</strong></p>
