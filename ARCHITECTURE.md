# Gaokao-Bench Architecture

本文定义 Gaokao-Bench 的长期结构。目标不是把 2025 年数学题写进运行器，而是建立一套能扩展到时序、全学科、主客观题和多模态题目的评测基础设施。

## 1. 设计原则

1. **数据不可变。** 已发布的数据版本只读。任何题目、答案、分值或权利边界的变化都创建新版本。
2. **引擎与版本解耦。** Runner、Provider、评分调度、指标和发布门禁不包含具体年份、科目、卷种或题数。
3. **规范可审计。** 一次成绩必须能绑定到数据哈希、Benchmark Spec、Prompt、评分器、模型路由和运行参数。
4. **分母显式。** 全量、pilot、单卷、文本可达、视觉切片分别报告，任何分数都带 scope，不把局部结果包装成全量成绩。
5. **失败不消失。** 超时、API 失败、格式错误、输出截断和缺图都保留在固定分母中并单独分类。
6. **确定性优先。** 客观题只进入可测试的 validator；主观题未来进入 rubric 和人类裁定流程，不用自由发挥的 LLM 裁判冒充确定评分。

## 2. 三层边界

```text
Engine (code)
  └─ provider I/O, prompt assembly, run protocol, scoring dispatch,
     metric calculation, audit bundle, resume, release checks
        │
        ├── reads Benchmark Spec (versioned configuration)
        │
        └── reads Data Package (immutable questions and evidence)
```

### Engine

负责“怎么跑、怎么记账、怎么复核”，不负责“这一版有哪些题”。

典型代码：

- `gaokao_bench/harness.py`：Provider 适配、请求循环、attempts、resume、summary。
- `gaokao_bench/scoring.py`：答案规范化、validator registry、指标计算。
- `gaokao_bench/public_data.py`：公共数据包脱敏、打包和哈希。
- `scripts/release_gate.py`：发布前门禁。

Engine 不允许新增这类常量：

```python
EXPECTED_TOTAL_POINTS = 353
EXPECTED_QUESTION_COUNT = 74
EXPECTED_REQUIRED_IMAGE_IDS = [...]
```

这些内容只能存在于 Benchmark Spec。

### Benchmark Spec

描述一个可执行评测版本的协议。新数据包应携带 `benchmark_spec.json`，并在 manifest 中声明：

```json
{
  "benchmark_spec": "benchmark_spec.json"
}
```

最小示例：

```json
{
  "schema_version": "1.0",
  "id": "gaokao-math-2025-objective",
  "version": "1.0",
  "subject": "数学",
  "years": [2025],
  "task_types": {
    "single_choice": {"shape": "choice", "min_selections": 1, "max_selections": 1},
    "multi_choice": {"shape": "choice", "min_selections": 2},
    "fill_blank": {"shape": "slots"}
  },
  "choice_labels": ["A", "B", "C", "D"],
  "validators": ["numeric", "interval", "set_tokens", "vector_linear"],
  "custom_validators": ["beijing_q13_trig_pair"],
  "expected": {
    "question_count": 74,
    "paper_count": 5,
    "total_points": 353,
    "paper_points": {"GB25-N1": 73, "GB25-N2": 73, "GB25-BJ": 60, "GB25-SH": 72, "GB25-TJ": 75},
    "task_type_counts": {"single_choice": 39, "multi_choice": 6, "fill_blank": 29},
    "required_image_ids": ["GB25-N1-Q06", "GB25-BJ-Q14", "GB25-SH-Q11", "GB25-TJ-Q03"]
  },
  "views": [
    {"id": "full", "description": "All scored questions", "selector": {"all": true}}
  ],
  "prompt": {
    "version": "gbmath-objective-anonymous-v2",
    "answer_schema": "gaokao-answer-v1",
    "system": "只输出合法 JSON ...",
    "image_crops": {},
    "image_pixel_sha256": {}
  },
  "scoring": {
    "profile": "strict-source-audited-points-all-or-nothing",
    "metric_version": "1.0",
    "primary_metric": "score.weighted_points",
    "partial_credit": false
  }
}
```

已发布的 `0.0.1` 没有在数据包中携带 spec，因此通过 `gaokao_bench/specs/legacy_2025_math.py` 的显式兼容适配器加载。该适配器是历史边界，不是未来扩展模板。

### Data Package

保存不可变的题目、答案、素材、provenance、审校证据、权利元数据和 manifest 哈希。数据包不包含运行器逻辑。

推荐版本目录：

```text
data/<version>/
  manifest.json
  benchmark_spec.json
  papers.jsonl
  questions.jsonl
  answer_keys.jsonl
  assets.jsonl
  assets/
  catalog/
  examples/
  review evidence ...
```

## 3. 领域模型

长期目标结构：

```text
Benchmark
└─ Release / Version
   └─ Subject Track
      └─ Year
         └─ Paper
            └─ Question
               ├─ task_type
               ├─ stem / content
               ├─ options / assets
               ├─ points
               ├─ answer specification
               ├─ scoring policy
               ├─ provenance
               ├─ rights
               └─ review evidence
```

题目至少包含：

```json
{
  "id": "stable-globally-unique-id",
  "subject": "数学",
  "year": 2025,
  "paper_id": "GB25-N1",
  "task_type": "single_choice",
  "stem": "...",
  "options": [],
  "input_modalities": ["text"],
  "asset_refs": [],
  "points": 5,
  "scoring_ref": "scoring/single_choice-v1",
  "provenance": {},
  "review_status": "verified"
}
```

现有 `0.0.1` 用 `type`、`stem`、`options`、`asset_dependency` 等字段；新 schema 演进时应提供迁移器，不原地改写已发布包。

## 4. 答案结构、空位与 Validator

答案模型分两层，避免“数学数值类型”被误当成所有学科的响应结构：

1. **响应结构 `shape`**：决定模型输出如何被拆开、如何记账。
   - `choice`：单选择、多选择、组合选择。
   - `slots`：一个或多个独立空位，每个空位单独评分。
   - `rubric`：主观题按评分点，未来实现。
2. **空位 validator**：决定一个空位接受什么等价答案。
   - 例如数学数值、区间、集合、向量。
   - 未来物理单位数值、化学方程式、英语规范化短答都属于这一层。

`shape` 不携带学科语义。一个物理填空题和一个数学填空题都使用 `slots`，但分别配置不同的空位 validator。这样新增学科不需要改 Engine 的外层答案结构。

首个通用 validator registry：

| ID | 用途 | 关键配置 |
| --- | --- | --- |
| `numeric` | 数值、分数、根式、LaTeX 数值 | `value`, `tolerance` |
| `exact` | 规范化后精确字符串 | `value` |
| `normalized_text` | 文本短答 | `value`, `case_sensitive` |
| `interval` | 区间、带 `x∈R` 的集合构造式 | `low`, `high`, inclusive |
| `set_tokens` | 枚举集合、圈号 | `tokens` |
| `vector_linear` | 二维基向量线性组合 | 系数 |
| `custom_validator` | 经代码审计的特殊约束 | 白名单 ID |

已实现的 JSON 结构是：

```json
{
  "shape": "slots",
  "slots": [
    {"canonical": "4", "matcher": "numeric", "value": 4.0, "tolerance": 1e-9}
  ]
}
```

答案键负责“每个空位怎么判”，Benchmark Spec 负责声明哪些题型和 validator 被允许。二者都在版本哈希内。

规则：

- Validator 必须确定、可单测、无网络依赖。
- 数学解析器只是 registry 中的一组，不应被物理单位、化学式、英语短答硬复用。
- 新科目优先新增通用 validator；只有无法表达时才新增白名单 custom validator。
- 主观题 rubric 必须包含评分点、允许等价表达和人工复核协议后才能进入打分包。

`GB25-SH-Q01` 的集合构造式假阴性是明确回归案例：`{x | 4 ≤ x ≤ 5, x ∈ R}`、`{x ∈ R | 4 ≤ x ≤ 5}` 与 `{x | 4 ≤ x ≤ 5}` 必须等价。

## 5. 视图、覆盖范围与指标

### 视图

`View` 是题目集合，如全量、某科目、某年份、某卷、文本可达、视觉必需。视图由 Spec 的声明式 selector 选出，不在 Engine 写死。

`0.0.1` 当前只有一个 `full` 视图。Text/Vision 的差异通过 modality 和切片表示，避免同一分母被解释成两套成绩。

### 运行覆盖

- `full_dataset`：选中了数据包全部题目。
- `full_view`：选中了某个 view 的全部题目。
- `selected_subset`：pilot、单卷或 `--limit` 子集。

Pilot 只报告 `selected_subset`。不会再用 74 题全量分母计算 5 题 pilot。

### 主指标

唯一主指标：

```text
score.weighted_points = awarded_points / possible_points
```

每个 metric 必须包含：

```json
{
  "id": "score.weighted_points",
  "version": "1.0",
  "unit": "ratio",
  "value": 0.625,
  "numerator": 5,
  "denominator": 8,
  "numerator_unit": "points",
  "scope": "selected_subset",
  "coverage": {
    "selected_questions": 2,
    "view_questions": 74,
    "selected_possible_points": 8,
    "view_possible_points": 353
  }
}
```

次级指标：

- `diagnostic.exact_accuracy`：全对题数 / 题目数。
- `diagnostic.paper_equal_weighted_points`：卷级等权均值，只作诊断。
- `slice.text_answerable.weighted_points`：无需图片的题目得分率。
- `slice.vision_required.weighted_points`：必须图片的题目得分率。
- `slice.text_on_scope_denominator.weighted_points`：文本可达得分除以当前完整 scope 分母。

禁止把 0–1 ratio、0–100 percentage 和不同分母的分数混在同一个 `summary` 键里。对外展示只使用一个明确的主指标和带 ID 的次级指标。

## 6. Evaluation Protocol

每次正式运行生成可哈希协议：

```text
dataset version + manifest SHA
benchmark spec id/version/SHA
prompt version + system prompt SHA + answer schema
scorer version + scoring profile + metric version
provider + requested model + base URL + provider pin
seed + allow_fallbacks
modality + repeats + temperature
max_tokens + reasoning_budget + timeout + concurrency
selection (papers / limit / exact question IDs)
```

协议 SHA 同时写入 `manifest.json` 和 `summary.json`。Resume 只有在协议完全一致时才允许；改变预算、模型、Prompt、数据、选择范围或评分器都必须新建 run。

## 7. Provider 契约

Provider 返回：

- final text
- requested / effective model
- finish reason
- response id
- upstream provider / routing
- system fingerprint
- effective seed
- usage，包括 input/output/reasoning/cache tokens
- 失败请求的 usage

OpenAI-compatible adapter 支持：

- `max_tokens` / `max_completion_tokens`
- `seed`
- OpenRouter `provider.order` 与 `allow_fallbacks=false`
- reasoning budget
- `--extra-body` 白名单式扩展
- 动态 provider 配置写入协议

推理模型的 completion 预算包含 hidden reasoning。默认 `max_tokens=32768`；`finish_reason=length/max_tokens` 统一记为 `truncated`，不把不完整输出当普通答错或解析错误。若模型需要更长预算，使用新的协议重跑，不要直接比较不同预算。

## 8. 运行审计与恢复

每次运行写出：

```text
manifest.json
prompts.jsonl
attempts.jsonl
submissions/repeat-NN.json
reports/repeat-NN.json
summary.json
```

`attempts.jsonl` 以 `(repeat, question_id)` 为唯一键。中断后使用 `--resume` 复用已完成 attempt；`--retry-failed` 可以选择重试 provider/harness/truncated。进度事件以 JSON 行输出到 stderr。

所有失败、缺图和截断都保留在分母中，同时进入 `attempts.status_counts`、`finish_reasons` 和 usage 汇总。

## 9. 全学科数据生产流水线

### 9.1 构造单元与准入层级

测试集按以下单元构造，不能让“年份”“科目”“试卷”“题目”变成同一种东西：

```text
Benchmark
└─ Data Release（不可变）
   └─ Subject Track（可执行 spec）
      └─ Year
         └─ Paper（行政意义上的完整试卷）
            └─ Question（唯一计分原子）
               ├─ Answer Key（可替换的 evaluator 输入）
               ├─ Assets
               ├─ Provenance / Rights
               └─ Review Evidence
```

构造规则：

- **Question 是唯一计分原子。** 一道多空题仍是一条 Question，使用 `slots`；一道主观题仍是一条 Question，未来使用 `rubric`。不要把一道题拆成多条独立计分记录来规避整题规则。
- **官方题分只属于 Question。** `source_points` 不因文本模式、视觉模式、抽样或模型失败而改变。
- **完整试卷与计分范围分开。** 只发布客观题时，Paper 仍保留 `full_paper_*`，另外声明 `scored_scope`；不能把 353 分包装成 750 分，也不能把客观题子集改称完整试卷。
- **主观题先编目、后计分。** 没有 locked rubric、等价策略和人工裁定流程时，只进入 catalog 且 `included_in_scoring=false`。
- **来源身份与题目身份分开。** 同一道题可以有多个来源证据；修订不创建新题，只有真正新增题目才使用新 ID。
- **组合视图不复制题目。** 跨年份、跨科目视图引用已有 Release 和 Question ID，不产生第二套答案键。

每道题有四个证据等级，只有最高等级可进入公开计分分母：

| 等级 | 状态 | 可用于计分 |
| --- | --- | --- |
| L0 | 已编目，来源与题号已知 | 否 |
| L1 | 题面、选项、分值和图形已结构化 | 否 |
| L2 | 独立推导答案，且逐项校验来源一致性 | 仅内部研究 |
| L3 | 按声明协议完成复核（当前为 Owner 逐题签字；独立复核轨另需双人复核）、gold/negative 回归通过 | 是 |

### 9.2 发布证据流水线

每一版数据都必须经过：

1. 来源登记：URL、发布者、获取时间、PDF/网页哈希、使用边界。
2. 试卷获取与版本哈希。
3. 题目结构化：题干、选项、空位、题型、题分。
4. 题面、选项、公式、单位和图形核对。
5. 从零独立推导答案。
6. 题分与卷面结构核对。
7. 权利元数据登记：原创编排、第三方素材、可再分发状态。
8. 污染状态标注：公开年份、检索风险、是否可声称 held-out。
9. 按 manifest 声明的 `review_protocol` 完成复核：`owner_attested_single_review`
    要求 Owner 逐题确认；`two_independent_reviewers_plus_owner_signoff` 另需
    两名独立 reviewer。
10. 将复核协议、覆盖账本、签字身份和输入哈希写入不可变版本。
11. Benchmark Spec 与数据 manifest 冻结。
12. 全量 gold/empty/malformed/validator 回归。
13. 发布门禁与版本标签。

每个阶段都要有覆盖账本和拒绝/纠偏记录。不能用“看起来没问题”代替证据。

## 10. 全学科扩展策略

### 数学

- 保持确定性数值、区间、集合、向量 validator。
- 继续补足官方 partial-credit 规则。
- 先做 2015–2024，再做 1977–2014 历史卷。

### 物理、化学、生物

- 物理：单位感知数值、有效数字、方向/符号、图形依赖。
- 化学：化学式、方程式配平、条件、上下标、同分异构文本。
- 生物：术语短答、组合选择、图表依赖；主观题进入 rubric 流程。

### 语文、英语、历史、地理、政治

- 客观题使用 choice/slot validator。
- 文本答案使用 `normalized_text`，但必须定义大小写、标点、同义表达和人工复核边界。
- 主观题不能直接交给自由 LLM judge；需要 locked rubric、评分点证据和抽样人审。

### 主观题

未来新增 `rubric` shape：

```json
{
  "shape": "rubric",
  "rubric_version": "1.0",
  "points": [
    {"id": "method", "points": 4, "description": "..."},
    {"id": "result", "points": 2, "description": "..."}
  ],
  "equivalence_policy": "human-review-v1",
  "adjudication": "required"
}
```

在主客观混合评分实现前，主观题目录继续 `included_in_scoring=false`，不进入分母。

## 11. 发布与版本

- 已发布 `v0.0.1` 数据不可变，不覆盖 Hugging Face tag。
- Engine 结构升级使用新的代码版本；数据 schema 变化使用新的数据版本。
- Git tag 标识代码版本；Hugging Face `v<version>` 标识数据版本；二者不必相同。
- 新数据包必须携带 `benchmark_spec.json`，manifest 引用其相对路径，`public_ranking_allowed` 必须明确。
- 文件名和内容哈希由 manifest 固定；上传后再次校验。
- `public_ranking_allowed=true` 时，第三方可以发布公开榜单和模型比较；引用时必须注明数据版本、评测协议和 CC BY 4.0 署名，且不得暗示项目背书。
- 数据集发布与模型成绩发布是两个独立动作。没有 canonical 模型运行时仍可发布不可变题目包，但不得发布该模型在本题集上的公开分数或榜单名次。

## 12. 长期数据构造模型

### 12.1 发布单元

Gaokao-Bench 不维护一个不断被覆盖的“大题库”。每次发布是一个不可变 Release：

```text
Release
├─ Benchmark Spec        本次评测协议、题型、视图、评分规则
├─ Papers                试卷集合（可按年份/地区/科目组合）
├─ Questions             计分题目
├─ Answer Keys           确定性答案和 validator 配置
├─ Rubrics               主观题评分点（未来，默认不入分母）
├─ Assets                题图、表格、听力、实验图等输入素材
├─ Provenance            来源、获取时间、哈希、片段定位
├─ Rights                代码、编排、题目素材的权利边界
├─ Review Evidence       独立推导、来源核对、双人复核、Owner 签字
├─ Examples              gold / empty / malformed
└─ Manifest             文件哈希、统计、版本、组合关系
```

一个可执行数据包绑定一个 Subject Track（例如“2025 数学客观题”或“2024 物理客观题”）。这里的 `subject` 是单值，Prompt、validators、期望结构和分母都只属于这个 Track。全学科 Gaokao-Bench 由多个 Track 组合而成，不把不同科目的原始题目塞进同一个单 Prompt 数据包。

Release 可以是一个很小的 pilot，也可以是由多个 Track 组成的年度索引；但必须做到：

- 题目、答案、分值、素材和权利元数据在同一版本内闭合；
- Manifest 能回答“这一版由哪些旧版本、哪些来源、哪些审校记录组成”；
- 任何内容改动都生成新版本，旧版本保持可下载、可复现、可归档。

### 12.2 稳定 ID

ID 是跨版本、跨年份、跨数据集组合的主键，不能在修订中复用：

```text
paper_id    = GB-2025-N1-MATH
question_id = GB-2025-N1-MATH-Q06
asset_id    = GB-2025-N1-MATH-Q06-ASSET-01
answer_id   = GB-2025-N1-MATH-Q06-ANS
```

规则：

- `paper_id` 包含年份、卷种/地区、科目，避免全学科后发生命名碰撞；
- `question_id` 在首次发布后永不复用；
- 题目修正只改变内容字段和版本关系，不改变 ID；
- 删除题目不物理消失，改为在新版本标记 `retired=true`，并保留旧版本；
- 组合 Release（如 `2015-2025-math`）通过 `constituents` 引用子版本，不复制出新的题目身份。

### 12.3 题目记录 v2

新数据包建议采用以下最小结构（字段名可以迁移，但语义必须保留）：

```json
{
  "id": "GB-2025-N1-MATH-Q06",
  "paper_id": "GB-2025-N1-MATH",
  "subject": "数学",
  "year": 2025,
  "type": "single_choice",
  "stem": "...",
  "options": [{"label": "A", "text": "..."}],
  "points": 5,
  "answer": {"shape": "choice", "correct_options": ["A"]},
  "scoring": {"mode": "all_or_nothing", "validator": "choice"},
  "input_modalities": ["text", "image"],
  "text_sufficient": false,
  "asset_refs": [".../q-06.svg"],
  "provenance": {"source_id": "...", "page": 1, "bbox": []},
  "rights": {"dataset_license": "CC-BY-4.0"},
  "review": {"answer_status": "verified", "reviewers": 2},
  "tags": ["vector", "wind", "diagram"]
}
```

题目内容、答案和评分配置必须能分离：

- 题干/选项/素材属于内容；
- 答案键属于可替换的 evaluator 输入；
- rubric 属于评分协议；
- review/provenance/rights 属于发布证据；
- `tags` 只用于视图和研究，不暗含难度权重。

### 12.4 答案与评分形状

响应结构只有三类，语义差异放在 validator：

| 响应结构 | 用途 | Engine 行为 |
| --- | --- | --- |
| `choice` | 全部科目的选择题 | 比较选项集合与选择数量 |
| `slots` | 数学填空、物理数值、英语拼写、化学方程式等 | 按空位数量拆分，并逐空位调用 validator |
| `rubric` | 主观解答、作文、实验设计、材料论述 | 评分点、等价策略、人审，未来实现 |

| 空位 validator | 典型科目 | 当前状态 |
| --- | --- | --- |
| `numeric` | 数学、物理、化学、地理 | 已实现 |
| `interval` | 数学、物理、地理 | 已实现 |
| `set_tokens` | 数学、化学、生物 | 已实现 |
| `vector_linear` | 数学、物理 | 已实现 |
| `exact` | 全科精确短答 | 已实现 |
| `normalized_text` | 语文、英语、历史、地理、政治 | 已实现基础规范化；同义表达仍需版本化配置 |
| `custom_validator` | 经过审计的特殊题型 | 已实现白名单机制 |
| `unit_numeric` | 物理、化学、地理 | 计划中，必须同时校验数值、单位和有效数字 |
| `symbolic` | 数学、物理、化学 | 计划中，必须定义等价变换边界 |
| `chemistry_equation` | 化学 | 计划中，必须校验元素、电荷、配平、条件和状态 |
| `structured` | 生物、物理实验 | 计划中，多个字段组合后整题判定 |
| `rubric` | 语文、英语、历史、地理、政治、数学解答 | 计划中，不能由自由 LLM 裁判替代 |

原则：

- 能用确定性 validator 表达的，不引入 LLM judge；
- 一个 validator 必须有独立单测、失败样例和版本；
- 同义表达必须在 spec 中声明，不能在评分时临时猜测；
- 单位、有效数字、方向、正负号属于该科目 validator，不塞进数学 parser；
- 主观题只有 rubric、等价策略、抽样人审和争议处理都完成后才进入分母。

### 12.5 分值、分母和展示

长期只用三层量纲：

1. **官方题分（source_points）**：卷面真实分值，是主指标的唯一加权依据；
2. **作用域可能分（possible_points）**：当前 view/subset 的官方题分总和；
3. **展示值（percentage）**：`100 × weighted_points`，仅用于人读，不另存为第二个主指标。

不采用：

- 把不同科目、不同卷种直接线性拉伸成 750 分；
- 用固定题数百分比替代官方题分；
- 对同一结果同时发布 `mean_full_test_metric`、`mean_selected_point_accuracy` 等多个“主指标”；
- 为了排名删除超时、截断、缺图或 API 失败样本。

如果未来需要跨科目总分，必须单独定义“全科组合视图”，写清各科权重、分母和缺失科目处理，并作为新的 benchmark spec，而不是修改现有 `score.weighted_points`。

### 12.6 抽样与视图

题池扩大后，推荐先全量、后分层子集：

- 全量视图：所有已审校题目，作为发布事实；
- 年度视图：单年全部试卷；
- 科目视图：单科全部题目；
- 文本可达视图：不需要图的题目；
- 视觉必需视图：必须图的题目；
- 研究视图：按题型、地区、年份、能力标签分层。

任何抽样必须：

- 使用固定 selector/seed，并在 manifest 记录；
- 先定义抽样框，再抽样，不能事后挑题；
- 同时报告 `selected_subset` 和 `full_view` 的分母与覆盖；
- 不把 pilot 分数写成全量分数，不做跨抽样集直接排名。

## 13. 全学科扩展设计

### 13.1 科目适配层

每个科目是一个 Track，包含：

```text
subject track
├─ task_type registry
├─ answer shape registry
├─ validator bundle
├─ prompt template
├─ figure/table/audio policy
├─ rubric policy（主观题）
└─ review checklist
```

Engine 只认识 shape 和 validator 接口；科目差异放在 Track。

| 科目 | 第一批能力 | 后续难点 |
| --- | --- | --- |
| 数学 | 数值、区间、集合、向量、选择/填空 | 解答题步骤分、证明等价性 |
| 物理 | 数值+单位、方向、有效数字、图像 | 实验设计、作图、误差分析 |
| 化学 | 化学式、方程式、条件、结构式 | 同分异构、配平、实验现象 |
| 生物 | 术语短答、组合选择、图表 | 遗传推导、实验设计、长答 |
| 语文 | 选择、默写、短答 | 阅读主观题、作文 rubric |
| 英语 | 选择、填空、拼写、翻译 | 写作、听力、口语评测 |
| 历史 | 选择、材料短答 | 史料论述、开放题 |
| 地理 | 选择、图表、数值 | 区域综合、绘图、材料分析 |
| 政治 | 选择、术语短答 | 时政材料、开放论述 |

### 13.2 分阶段扩展顺序

**Phase A（现在）**：2025 数学客观题，完成 Engine/Spec/Data 分层、指标 scope、恢复、审计和发布闭环。

**Phase B**：2015–2024 数学客观题；每个年份独立数据版本，再生成年度组合视图。先做同科目纵向，验证跨年份可比性。

**Phase C**：物理、化学、生物、英语客观题；先做无需主观裁定的题型，建立单位和结构化答案 validator。

**Phase D**：语文、历史、地理、政治客观题；再逐步加入 text 短答。

**Phase E**：1977–2014 历史卷数字化；先扫描/OCR/结构抽取，再做来源与题干一致性审核。

**Phase F**：rubric 主观题、实验题、作文和完整 750 分卷。

**Phase G**：每年 6 月 Live Gaokao；在题目公开后、进入大模型训练/检索语料前尽快冻结。

每个 Phase 独立发布，不等待全科齐全。发布粒度可以是“2025 数学单卷”“2025 物理客观题”“2024 全科组合”，但必须带完整 spec 和 manifest。

## 14. 发布组合与版本治理

建议把“数据版本”和“评测视图”彻底分开：

```text
data/0.0.1/                 # 已发布，不可变
data/0.1.0/                 # 新题目/答案/分值
data/2025-math-objective/   # 从版本组合生成的研究视图
```

组合视图不复制题目，只记录：

```json
{
  "composition": [
    {"dataset_version": "0.0.1", "manifest_sha256": "..."},
    {"dataset_version": "0.1.0", "manifest_sha256": "..."}
  ],
  "selection": {"subject": "数学", "years": [2025]},
  "spec": "benchmark_spec.json"
}
```

组合层只负责选择、计分汇总和展示，不重新定义题目内容。跨科目组合必须保留每个 Track 的独立分母和切片；在组合评分协议正式定义前，不输出跨科目单一分数，也不把各科题分线性拼成 750 分。

版本规则：

- 代码版本、Benchmark Spec 版本、数据版本彼此独立；
- 已发布数据只追加，不覆盖；
- 任何答案修正都保留 `before/after` 和审校记录；
- 组合视图必须能回到每个组成版本的 manifest；
- 公开上传后再次校验文件哈希和 Hugging Face tag；
- 数据包与 Engine 的最低兼容版本写入 manifest。

## 15. 质量门槛与污染

每道计分题至少通过：

1. 来源登记与 PDF/页面哈希；
2. 题干、选项、公式、单位、图形逐项核对；
3. 独立推导答案；
4. 官方/权威来源交叉核对；
5. 按 manifest 声明的复核协议完成复核；当前 `owner_attested_single_review`
   使用 Owner 逐题签字，独立复核轨使用两名独立 reviewer；
6. 将签字和输入哈希固化到不可变版本；
7. 分值闭合；
8. gold / empty / malformed / 单题 validator 回归。

发布门禁：

- gold 必须满分；
- empty / malformed 必须 0；
- 每题至少一个错误答案回归；
- 题图存在、可解码、哈希正确、裁切不截断内容；
- 题目总数、卷数、题型数、答案槽、各卷分值全部与 Spec 一致；
- 公开 manifest 的 `public_ranking_allowed`、素材权利和污染标签明确；
- 公开榜单必须遵守 manifest 的 `public_ranking_allowed`、榜单分类和署名规则；历史题成绩不得包装成新鲜能力。

污染策略：

- 公开年份默认 `retrospective_public_*`；
- 不声称 held-out、零污染或新鲜能力；
- 允许研究使用，但不能把旧题成绩包装成实时泛化能力；
- 每年 Live Gaokao 单独建立发布窗口和检索披露；
- 训练数据、检索工具、联网能力必须写入运行协议。

## 16. 当前实现状态

Engine `0.1.4` 已完成：

- Spec-driven validation and legacy adapter.
- `score.weighted_points` primary metric with explicit scopes.
- Text/vision slices and paper-equal diagnostic.
- Set-builder `, x ∈ R` regression fix.
- 32k default output budget and reasoning-budget accounting.
- `truncated` classification and failure usage retention.
- Provider routing, fingerprint, seed, protocol hash.
- Progress events, resume, retry-failed.
- Rights/ranking notice in README, HF card, fetch output, and run summary.
- Single canonical metric contract (`scoring.contract=single_primary_metric_v1`);
  legacy `0.0.1`/`0.0.3` remain readable through their historical adapter.
- Canonical profile defaults: 5 repeats, 900-second timeout, 32768 output tokens,
  provider pin, fallbacks disabled, seed/fingerprint verification, bootstrap CI,
  and per-question flip rate.
- Separate `retrospective_public_2025` and `post_cutoff_live` leaderboard tracks.
- Visual slices below 30 questions are diagnostic-only; Shanghai Q11 records
  explicit decimal-rounding and borderline semantics.
- Owner-attested `audited_passed` release path; independent review is marked
  not applicable rather than left pending or silently claimed.

本轮同时清理了 Engine 中遗留的 2025 数学常量边界：

- 公共数据卡、引用、manifest 和审计统计从 Spec 推导；
- 旧 `0.0.1` 的 Q13 证据门禁移入显式 legacy audit adapter；
- smoke/release/baseline 工具不再把 74/353 当作通用常量；
- `task_type` 与 legacy `type` 统一由 `question_type()` 解析；
- 运行摘要只保留一个主指标和带 scope 的切片。
- 数据版本 `0.0.4` 保持题目不变，将 74 道计分题全部收口为
  `audited_passed`，并把唯一主指标、兼容矩阵、双榜协议和审计证据随包发布。

尚未实现、但结构已预留：

- 主观题 rubric engine 与人工裁定。
- 单位感知、化学式和语言学科 validator。
- 跨版本 longitudinal report。
