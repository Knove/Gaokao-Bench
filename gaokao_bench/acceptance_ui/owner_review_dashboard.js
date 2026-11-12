(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("review-data").textContent);
  const VERSION_INDEX = Array.isArray(DATA.versions) ? DATA.versions : [];
  const DATASETS = DATA.datasets && typeof DATA.datasets === "object" ? DATA.datasets : {};
  const ASSETS = DATA.assets && typeof DATA.assets === "object" ? DATA.assets : {};
  let META = null;
  let WORKSPACE = null;
  let ROWS = [];
  let ROW_BY_ID = new Map();
  let FIELDS = [];
  let STORAGE_KEY = "";
  let saved = {};
  const DECISION_LABELS = {
    agree: "同意",
    correct: "纠错",
    dispute: "争议",
  };
  const VALID_DECISIONS = new Set(Object.keys(DECISION_LABELS));

  function $(selector, root = document) {
    return root.querySelector(selector);
  }

  function $$(selector, root = document) {
    return Array.from(root.querySelectorAll(selector));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function normalizeWhitespace(value) {
    return String(value ?? "").replace(/\s+/g, " ").trim();
  }

  function decisionFor(itemId) {
    return state.decisions[itemId] || null;
  }

  function decisionState(itemId) {
    return decisionFor(itemId)?.decision || "pending";
  }

  function seedDecisions() {
    const seeded = {};
    for (const row of ROWS) {
      const raw = row.raw || {};
      const decision = normalizeWhitespace(raw.decision).toLowerCase();
      if (!VALID_DECISIONS.has(decision)) continue;
      seeded[row.item_id] = {
        decision,
        reviewed_answer: raw.reviewed_answer || "",
        reviewed_points: raw.reviewed_points || "",
        notes: raw.notes || "",
        updated_at: "",
      };
    }
    return seeded;
  }

  function loadSavedState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (error) {
      console.warn("Cannot read saved review state", error);
      return {};
    }
  }

  const state = {
    decisions: {},
    activeVersion: null,
    selectedId: null,
    filters: {
      search: "",
      paper: "all",
      type: "all",
      status: "all",
      priority: false,
      visual: false,
    },
    autoNext: true,
    lastSavedAt: "",
  };

  function persistState() {
    state.lastSavedAt = new Date().toISOString();
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          schema_version: 1,
          dataset_version: META.dataset_version,
          manifest_sha256: META.manifest_sha256,
          decisions: state.decisions,
          autoNext: state.autoNext,
          saved_at: state.lastSavedAt,
        })
      );
    } catch (error) {
      console.warn("Cannot persist review state", error);
      showToast("浏览器无法保存本地进度，请立即导出当前 CSV 备份。", true);
    }
  }

  function storageKeyFor(meta) {
    return [
      "gaokao-bench",
      "owner-review",
      meta.dataset_version,
      String(meta.manifest_sha256 || "").slice(0, 16),
    ].join(":");
  }

  function inferVersionForItem(itemId) {
    if (!itemId) return null;
    if (state.activeVersion && DATASETS[state.activeVersion]?.rows?.some((row) => row.item_id === itemId)) {
      return state.activeVersion;
    }
    const preferred = itemId.startsWith("GB26")
      ? "0.1.0-alpha.2026-n1"
      : "0.0.1";
    const candidates = [preferred, ...VERSION_INDEX.map((item) => item.id), DATA.default_version];
    for (const version of candidates) {
      if (version && DATASETS[version]?.rows?.some((row) => row.item_id === itemId)) return version;
    }
    return null;
  }

  function parseHashState() {
    const raw = window.location.hash.replace(/^#/, "");
    if (!raw) return { version: null, itemId: null };
    const params = new URLSearchParams(raw);
    let version = params.get("v");
    let itemId = params.get("q");
    if (!itemId && raw.startsWith("q=")) itemId = decodeURIComponent(raw.slice(2));
    if (!version && itemId) version = inferVersionForItem(itemId);
    return { version, itemId };
  }

  function setHash(itemId) {
    if (!META || !itemId) return;
    const nextHash = `#v=${encodeURIComponent(META.dataset_version)}&q=${encodeURIComponent(itemId)}`;
    if (window.location.hash === nextHash) return;
    try {
      history.replaceState(null, "", nextHash);
    } catch (error) {
      window.location.hash = nextHash;
    }
  }

  function filteredRows() {
    const query = state.filters.search.toLowerCase().trim();
    return ROWS.filter((row) => {
      if (state.filters.paper !== "all" && row.paper_id !== state.filters.paper) return false;
      if (state.filters.type !== "all" && row.type !== state.filters.type) return false;
      if (state.filters.status !== "all" && decisionState(row.item_id) !== state.filters.status) return false;
      if (state.filters.priority && !row.priority) return false;
      if (state.filters.visual && row.image_dependency !== "required") return false;
      if (!query) return true;
      const haystack = [
        row.item_id,
        row.paper_id,
        row.paper_title,
        row.display_number,
        row.stem,
        row.canonical_display,
        row.answer_status_label,
        row.answer_source.publisher,
        row.answer_source.excerpt,
        row.secondary_source.publisher,
        row.secondary_source.excerpt,
        row.independent_audit.derivation,
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  function statusCounts() {
    const counts = { pending: 0, agree: 0, correct: 0, dispute: 0, total: ROWS.length };
    for (const row of ROWS) counts[decisionState(row.item_id)] += 1;
    return counts;
  }

  function formatStem(value) {
    let text = escapeHtml(value);
    text = text.replace(/_{2,}/g, '<span class="blank"></span>');
    return text.replace(/\n/g, "<br>");
  }

  function formatBbox(values) {
    if (!Array.isArray(values) || values.length !== 4) return "未记录";
    return values.map((value) => Number(value).toFixed(1)).join(", ");
  }

  function shortHash(value) {
    const text = String(value || "");
    return text ? `${text.slice(0, 12)}…` : "未记录";
  }

  function statusClass(itemId) {
    return decisionState(itemId);
  }

  function stateLabel(itemId) {
    const status = decisionState(itemId);
    return status === "pending" ? "待复核" : DECISION_LABELS[status];
  }

  function versionAuditRibbonHtml() {
    const audits = META.audits || [];
    if (!audits.length) return "";
    return `<div class="version-audit-ribbon">${audits
      .map(
        (audit) => `<span class="version-audit-pill ${escapeHtml(audit.tone || "neutral")}" title="${escapeHtml(audit.detail || "")}"><span class="pill-dot"></span><b>${escapeHtml(audit.id)}</b>${escapeHtml(audit.title)} · ${escapeHtml(audit.label)}</span>`
      )
      .join("")}</div>`;
  }

  function renderPaperOptions() {
    const select = $("#paperFilter");
    const current = select.value || "all";
    select.innerHTML = `<option value="all">全部 ${Object.keys(META.by_paper || {}).length} 套</option>`;
    for (const [paperId, paper] of Object.entries(META.by_paper)) {
      const option = document.createElement("option");
      option.value = paperId;
      option.textContent = `${paper.region} · ${paper.questions}题/${paper.points}分`;
      select.appendChild(option);
    }
    select.value = current;
  }

  function renderPriorityHelp() {
    const container = $("#priorityHelpList");
    const items = ROWS.filter((row) => row.priority);
    container.innerHTML = items
      .map(
        (row) => `
          <button type="button" class="priority-help-item" data-focus-id="${escapeHtml(row.item_id)}">
            <span class="priority-help-id">${escapeHtml(row.paper_region)} · ${escapeHtml(row.display_number)}</span>
            <span class="priority-help-reason"><b>${escapeHtml(row.priority.label)}</b>：${escapeHtml(row.priority.reason)}</span>
          </button>`
      )
      .join("");
  }

  function renderList() {
    const list = $("#questionList");
    const rows = filteredRows();
    $("#listCount").textContent = `${rows.length} / ${ROWS.length} 题`;
    if (!rows.length) {
      list.innerHTML = '<div class="empty-list">没有匹配的题目。<br>试试清除搜索或筛选条件。</div>';
      return;
    }
    list.innerHTML = rows
      .map((row) => {
        const status = statusClass(row.item_id);
        const tags = [
          `<span class="q-tag">${escapeHtml(row.type_label)}</span>`,
          row.priority ? '<span class="q-tag priority">重点</span>' : "",
          row.image_dependency === "required" ? '<span class="q-tag visual">视觉</span>' : "",
        ].join("");
        return `
          <button type="button" class="question-item${row.item_id === state.selectedId ? " selected" : ""}"
            data-item-id="${escapeHtml(row.item_id)}" data-state="${status}"
            role="option" aria-selected="${row.item_id === state.selectedId}">
            <span class="q-item-top">
              <span class="q-item-main">
                <span class="q-item-number">${escapeHtml(row.paper_region)} · ${escapeHtml(row.display_number.replace("第", ""))}</span>
                <span class="q-item-id">${escapeHtml(row.item_id)}</span>
              </span>
              <span class="q-item-points">${row.points} 分</span>
            </span>
            <span class="q-item-bottom">
              <span class="q-item-tags">${tags}</span>
              <span class="q-state ${status === "pending" ? "" : status}">${escapeHtml(stateLabel(row.item_id))}</span>
            </span>
          </button>`;
      })
      .join("");
  }

  function currentFilteredRows() {
    return filteredRows();
  }

  function selectItem(itemId, options = {}) {
    if (!ROW_BY_ID.has(itemId)) return;
    state.selectedId = itemId;
    setHash(itemId);
    persistState();
    renderList();
    renderCenter();
    renderReviewPane();
    if (!options.keepCenterScroll) $("#center").scrollTop = 0;
    if (!options.fromKeyboard) {
      const active = $(`.question-item[data-item-id="${CSS.escape(itemId)}"]`);
      if (active) active.scrollIntoView({ block: "nearest" });
    }
  }

  function nextPrevious(direction) {
    const rows = currentFilteredRows();
    if (!rows.length) return;
    let index = rows.findIndex((row) => row.item_id === state.selectedId);
    if (index < 0) index = 0;
    else index = (index + direction + rows.length) % rows.length;
    selectItem(rows[index].item_id, { fromKeyboard: true });
  }

  function nextPending() {
    const pending = ROWS.filter((row) => !decisionFor(row.item_id));
    if (!pending.length) {
      showToast(`${META.total_questions} 道题都已经有判定了；可以打开“完成验收”检查并签字。`);
      return;
    }
    const currentIndex = ROWS.findIndex((row) => row.item_id === state.selectedId);
    const ordered = ROWS.slice(currentIndex + 1).concat(ROWS.slice(0, currentIndex + 1));
    const next = ordered.find((row) => !decisionFor(row.item_id)) || pending[0];
    selectItem(next.item_id);
  }

  function renderCenter() {
    const center = $("#center");
    const row = ROW_BY_ID.get(state.selectedId);
    if (!row) {
      center.innerHTML = `
        <div class="center-empty">
          <div><strong>没有选中的题目</strong><p>从左侧目录选择题目，或清除筛选条件。</p></div>
        </div>`;
      return;
    }

    const rows = currentFilteredRows();
    let filteredIndex = rows.findIndex((item) => item.item_id === row.item_id);
    if (filteredIndex < 0) filteredIndex = 0;
    const position = `${filteredIndex + 1} / ${rows.length || 1}`;
    const conflictStatus = ["external_source_self_conflict", "external_answer_error", "missing_answer_key"].includes(row.answer_status);
    const statusTagClass = conflictStatus ? "amber" : row.answer_status === "source_and_derivation_confirmed" ? "green" : "blue";
    const independent = row.independent_audit || {};
    const externalPending = row.answer_status === "ocr_match_pending_visual";
    const verdictLabels = {
      pdf_exact: "与 PDF 一致",
      pdf_corrected: "已按 PDF 修正",
      text_sufficient: "文字自足",
      figure_required: "图形必需",
      confirmed: "已独立推导确认",
      not_applicable: "不适用",
    };
    const ownerReceipt = META.mode === "signed"
      ? { label: "已签收", tone: "green", title: META.signed_reviewer ? `签收人：${META.signed_reviewer}` : "签字文件已归档" }
      : META.decision_enabled
        ? { label: "待完成", tone: "gray", title: "最终签字由项目 owner 在本页完成" }
        : { label: "不适用", tone: "gray", title: "当前版本为只读历史快照" };
    const auditStatusHtml = `
      <div class="audit-separation">
        <div class="audit-check"><span>独立推导</span><strong><span class="tag green">${escapeHtml(verdictLabels[independent.answer_verdict] || independent.answer_verdict || "未记录")}</span></strong></div>
        <div class="audit-check"><span>外部媒体视觉核对</span><strong><span class="tag ${externalPending ? "amber" : statusTagClass}">${escapeHtml(externalPending ? "待人工视觉复核" : row.answer_status_label)}</span></strong></div>
        <div class="audit-check"><span>Owner 签字</span><strong title="${escapeHtml(ownerReceipt.title)}"><span class="tag ${ownerReceipt.tone}">${escapeHtml(ownerReceipt.label)}</span></strong></div>
      </div>
      <div class="derivation-card">
        <p class="derivation-text">${escapeHtml(independent.derivation || "该题缺少独立推导记录。")}</p>
        <div class="derivation-meta">
          <span>题面：${escapeHtml(verdictLabels[independent.stem_verdict] || independent.stem_verdict || "未记录")}</span>
          <span>选项：${escapeHtml(verdictLabels[independent.option_verdict] || independent.option_verdict || "未记录")}</span>
          <span>图形：${escapeHtml(verdictLabels[independent.asset_verdict] || independent.asset_verdict || "未记录")}</span>
          <span>审校：<code>${escapeHtml(independent.reviewer || "未记录")}</code></span>
          <span>${escapeHtml(independent.reviewed_at || "时间未记录")}</span>
        </div>
      </div>`;
    const options = row.options || [];
    const optionsHtml = options.length
      ? `<div class="options">${options
          .map(
            (option) => `
              <div class="option">
                <span class="option-label">${escapeHtml(option.label)}</span>
                <span class="option-text">${escapeHtml(option.text)}</span>
              </div>`
          )
          .join("")}</div>`
      : "";
    const priorityHtml = row.priority
      ? `<div class="focus-callout">
          <div class="focus-icon">!</div>
          <div><strong>重点复核 · ${escapeHtml(row.priority.label)}</strong><p>${escapeHtml(row.priority.reason)}</p></div>
        </div>`
      : "";
    const secondary = row.secondary_source;
    const secondaryHtml = secondary.id
      ? sourceCardHtml(row, secondary, "第二来源", "secondary")
      : `<article class="source-card secondary missing">
          <div class="source-card-head"><div><div class="source-type">SECOND SOURCE</div><div class="source-publisher">没有独立第二来源</div></div><span class="tag amber">重点</span></div>
          <p class="source-excerpt">${escapeHtml(secondary.excerpt || "该题未配置独立第二来源，请以主来源、题干和独立推导复核。")}</p>
          <div class="source-meta"><span>仅主来源 + 推导</span><span>签字时请重点确认</span></div>
        </article>`;
    const answerSlotText = row.answer_slot_count > 1 ? `${row.answer_slot_count} 个答案槽` : "单一答案";
    const imageNote = row.image_dependency === "required" ? "视觉必需 · 离开裁图会缺失信息" : "文字可答；裁图用于核对结构化题面";
    const asset = row.asset || {};
    const pageLabel = asset.page_number ? `PDF 第 ${asset.page_number} 页` : "PDF 页码未记录";
    const bboxLabel = formatBbox(asset.bbox);
    const afterHash = shortHash(asset.after_sha256 || row.image_sha256);
    const assetProof = `
      <div class="asset-proof" title="裁图来源：${escapeHtml(asset.crop_method || "未记录")} · after SHA-256 ${escapeHtml(asset.after_sha256 || row.image_sha256 || "未记录")}">
        <span class="tag green">本版已从 PDF 重建</span>
        <span>${escapeHtml(pageLabel)}</span>
        <span class="mono">bbox ${escapeHtml(bboxLabel)}</span>
        <span class="mono">SHA ${escapeHtml(afterHash)}</span>
      </div>`;

    center.innerHTML = `
      <div class="question-page">
        <div class="page-head">
          <div>
            <div class="breadcrumb"><span>${escapeHtml(row.paper_title)}</span><span>›</span><b>${escapeHtml(row.display_number)}</b><span>·</span><span>${position}</span></div>
            <div class="page-title-row">
              <h2>${escapeHtml(row.paper_region)} · ${escapeHtml(row.display_number)}</h2>
              <span class="tag blue">${escapeHtml(row.type_label)}</span>
              <span class="tag gray">${row.points} 分</span>
              ${row.image_dependency === "required" ? '<span class="tag violet">视觉必需</span>' : ""}
              ${row.priority ? '<span class="tag amber">重点复核</span>' : ""}
            </div>
            <div class="page-id">${escapeHtml(row.item_id)}</div>
          </div>
          <div class="pager">
            <button type="button" data-nav="prev" title="上一题 (K)">←</button>
            <button type="button" data-nav="next" title="下一题 (J)">→</button>
          </div>
        </div>

        ${versionAuditRibbonHtml()}
        ${priorityHtml}

        <article class="question-card">
          <div class="question-card-head">
            <h3>结构化题面 ↔ 原卷裁图</h3>
            <span>${escapeHtml(imageNote)}</span>
          </div>
          <div class="question-grid">
            <div class="question-copy">
              <p class="stem">${formatStem(row.stem)}</p>
              ${optionsHtml}
              <div class="answer-band">
                <div>
                  <div class="answer-label">当前规范答案</div>
                  <div class="answer-value">${escapeHtml(row.canonical_display)}</div>
                </div>
                <div class="answer-meta"><b>${row.points} 分</b><span>${escapeHtml(answerSlotText)}</span><span>${escapeHtml(row.answer_status_label)}</span></div>
              </div>
            </div>
            <figure class="crop-panel">
              <div class="crop-head"><strong>原卷裁图</strong><span>点击放大核对</span></div>
              <div class="crop-frame" data-zoom-id="${escapeHtml(row.item_id)}" title="点击放大">
                ${row.image_data_uri ? `<img src="${row.image_data_uri}" alt="${escapeHtml(row.item_id)} 原卷裁图" loading="eager">` : '<div class="image-missing">当前版本没有原卷裁图；请以结构化题面、来源和审计记录核对。</div>'}
              </div>
              ${row.image_data_uri ? assetProof : ""}
              <figcaption class="crop-meta">
                <span>${escapeHtml(row.image_dependency === "required" ? "信息必需图形" : "题面核对裁图")}</span>
                ${row.pdf_url ? `<a href="${escapeHtml(row.pdf_url)}" target="_blank" rel="noopener noreferrer" title="打开原始试卷 PDF">原始试卷 PDF ↗</a>` : "<span>原始 PDF 未记录</span>"}
              </figcaption>
            </figure>
          </div>
        </article>

        <section class="evidence-section">
          <div class="section-heading"><h3>独立推导核验</h3><span>项目内可复算推导；与外部媒体核对、owner 签字分开记录</span></div>
          ${auditStatusHtml}
        </section>

        <section class="evidence-section">
          <div class="section-heading"><h3>答案与来源</h3><span>主来源与独立第二来源分开展示，非官方来源</span></div>
          <div class="evidence-grid">
            ${sourceCardHtml(row, row.answer_source, "主来源", "primary")}
            ${secondaryHtml}
          </div>
          ${sourceAssessmentHtml(row)}
          <div class="evidence-foot">
            <div class="evidence-mini"><span>答案审计状态</span><strong><span class="tag ${statusTagClass}">${escapeHtml(row.answer_status_label)}</span></strong></div>
            <div class="evidence-mini"><span>分值依据</span><strong>试卷结构对账 · ${row.points} 分</strong></div>
          </div>
        </section>
      </div>`;

    const zoomFrame = $(".crop-frame", center);
    if (zoomFrame) zoomFrame.addEventListener("click", () => openZoom(row));
    $$("[data-nav]", center).forEach((button) => {
      button.addEventListener("click", () => nextPrevious(button.dataset.nav === "next" ? 1 : -1));
    });
  }

  function sourceCardHtml(row, source, label, kind) {
    const imageLink = source.image_url
      ? `<a class="source-open primary-link" href="${escapeHtml(source.image_url)}" target="_blank" rel="noopener noreferrer">打开答案图 ↗</a>`
      : "";
    const pageLink = source.url
      ? `<a class="source-open" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" title="打开来源文章；正文可能不含题目文字">文章入口 ↗</a>`
      : "";
    const links = imageLink || pageLink
      ? `<div class="source-links">${imageLink}${pageLink}</div>`
      : '<span class="tag gray">无链接</span>';
    return `
      <article class="source-card ${kind === "secondary" ? "secondary" : ""}">
        <div class="source-card-head">
          <div><div class="source-type">${kind === "secondary" ? "SECOND SOURCE" : "PRIMARY SOURCE"}</div><div class="source-publisher">${escapeHtml(label)} · ${escapeHtml(source.publisher)}</div></div>
          ${links}
        </div>
        <p class="source-excerpt">${escapeHtml(source.excerpt || "来源未提供摘录。")}</p>
        ${source.image_url ? '<p class="source-hint">答案在图片中，网页正文可能搜不到题号；请先点“打开答案图”，再按下方定位核对。</p>' : ''}
        <div class="source-meta">
          <span>定位：${escapeHtml(source.locator || "未记录")}</span>
          <span><code>${escapeHtml(source.id || "—")}</code></span>
        </div>
      </article>`;
  }

  function sourceAssessmentHtml(row) {
    const items = row.source_assessments || [];
    if (!items.length) return "";
    const labels = {
      exact_stem_match: ["题干一致", "green"],
      equivalent_stem_match: ["等价正项表述", "green"],
      variant_missing_positive_condition: ["题面缺少正项条件", "amber"],
      stem_not_visible: ["题干不可见", "amber"],
    };
    const card = (item, rejected) => {
      const [stemLabel, stemClass] = labels[item.stem_match] || ["未判定", "gray"];
      const link = item.evidence_url || item.url;
      return `
        <article class="assessment-card ${rejected ? "rejected" : "supporting"}">
          <div class="assessment-head">
            <div>
              <div class="source-type">${rejected ? "REJECTED EVIDENCE" : "SUPPORTING EVIDENCE"}</div>
              <div class="source-publisher">${escapeHtml(item.publisher || item.source_id)}</div>
            </div>
            <span class="tag ${stemClass}">${escapeHtml(stemLabel)}</span>
          </div>
          <p class="assessment-stem">${escapeHtml(item.stem_excerpt || "未记录题面摘录")}</p>
          <div class="assessment-answer"><b>来源答案</b><span>${escapeHtml(item.answer_value || "—")}</span></div>
          ${rejected ? `<p class="assessment-reject"><b>不计入支持证据：</b>${escapeHtml(item.rejection_reason || "未记录拒绝理由")}</p>` : ""}
          <div class="assessment-foot">
            ${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>` : "<span>无链接</span>"}
            <span>${escapeHtml(item.locator || "定位未记录")}</span>
            <span><code>${escapeHtml(item.source_id || "—")}</code></span>
          </div>
        </article>`;
    };
    const supporting = items.filter((item) => item.role === "supporting");
    const rejected = items.filter((item) => item.role === "rejected");
    return `
      <div class="source-assessment-panel">
        <div class="assessment-heading">
          <h4>题干一致性证据</h4>
          <span>支持证据必须匹配原题题面；缺条件或题干不可见的来源不计入。</span>
        </div>
        <div class="assessment-group">
          <div class="assessment-group-title">支持证据 · ${supporting.length}</div>
          <div class="assessment-grid">${supporting.map((item) => card(item, false)).join("")}</div>
        </div>
        ${rejected.length ? `
          <div class="assessment-group rejected-group">
            <div class="assessment-group-title">错误证据 · ${rejected.length} · 保留但不计入支持</div>
            <div class="assessment-grid">${rejected.map((item) => card(item, true)).join("")}</div>
          </div>` : ""}
      </div>`;
  }

  function renderReviewPane() {
    const pane = $("#reviewPane");
    const row = ROW_BY_ID.get(state.selectedId);
    if (!row) {
      pane.innerHTML = "";
      return;
    }
    const record = decisionFor(row.item_id);
    const decision = record?.decision || "";
    const notes = record?.notes || "";
    const reviewedAnswer = record?.reviewed_answer || "";
    const reviewedPoints = record?.reviewed_points || "";
    const statusText = decision ? DECISION_LABELS[decision] : "待复核";
    const statusClassText = decision || "pending";
    const readonly = !META.decision_enabled;
    const disabled = readonly ? "disabled" : "";
    const readonlyNotice = readonly
      ? `<div class="readonly-notice"><strong>${META.mode === "signed" ? "已签收版本 · 只读" : "历史快照 · 只读"}</strong><br>${escapeHtml(
          META.mode === "signed"
            ? "该版本已有签收记录，本页仅用于回看证据，不允许改写历史判定。"
            : "该版本没有可写的 owner review packet；可以浏览题面、答案、来源和审计，但不能在这里签收。"
        )}</div>`
      : "";
    const decisionCaption = readonly
      ? META.mode === "signed" ? "历史判定记录" : "只读字段"
      : "逐题判定";
    const saveStatusText = readonly ? "历史版本不回写本机判定" : "判定和备注自动保存到本机浏览器";
    const primaryActionLabel = readonly ? "下一道题" : "保存并下一道待复核";
    const optionQuick = row.options.length
      ? `<div class="option-quick">${row.options
          .map(
            (option) =>
              `<button type="button" class="quick-chip" data-option-label="${escapeHtml(option.label)}" title="点选 ${escapeHtml(option.label)}" ${disabled}>${escapeHtml(option.label)}</button>`
          )
          .join("")}<button type="button" class="quick-chip" id="copyCanonicalAnswer" ${disabled}>填入规范答案</button></div>`
      : `<div class="option-quick"><button type="button" class="quick-chip" id="copyCanonicalAnswer" ${disabled}>填入规范答案</button></div>`;

    pane.innerHTML = `
      <div class="review-head">
        <div>
          <span class="eyebrow">YOUR VERDICT</span>
          <h2>我的判定</h2>
          <div class="selected-status">${escapeHtml(row.paper_region)} · ${escapeHtml(row.display_number)} · ${row.points} 分</div>
        </div>
        <span class="status-pill ${statusClassText === "pending" ? "" : statusClassText}">${escapeHtml(statusText)}</span>
      </div>

      ${readonlyNotice}

      <div class="decision-caption">${decisionCaption}</div>
      <div class="decision-grid">
        <button type="button" class="decision-btn agree${decision === "agree" ? " active" : ""}" data-decision="agree" ${disabled}>
          <span class="key-hint">1</span><span class="decision-icon">✓</span><span class="decision-name">同意</span>
        </button>
        <button type="button" class="decision-btn correct${decision === "correct" ? " active" : ""}" data-decision="correct" ${disabled}>
          <span class="key-hint">2</span><span class="decision-icon">±</span><span class="decision-name">纠错</span>
        </button>
        <button type="button" class="decision-btn dispute${decision === "dispute" ? " active" : ""}" data-decision="dispute" ${disabled}>
          <span class="key-hint">3</span><span class="decision-icon">!</span><span class="decision-name">争议</span>
        </button>
      </div>

      <div class="review-fields" id="correctionFields" ${decision === "correct" ? "" : "hidden"}>
        <label class="field-label" for="reviewedAnswer">修改后的答案</label>
        <input class="field-input" id="reviewedAnswer" type="text" value="${escapeHtml(reviewedAnswer)}" placeholder="例如 C 或 [&quot;0.6&quot;,&quot;3.2&quot;]" autocomplete="off" ${disabled}>
        <div class="field-hint">多空填空题用 JSON 数组，如 <code>[&quot;1&quot;,&quot;15&quot;]</code>；若只改分值，可留空。</div>
        ${optionQuick}

        <label class="field-label" for="reviewedPoints">修改后的分值</label>
        <input class="field-input" id="reviewedPoints" type="number" min="0" max="50" step="1" value="${escapeHtml(reviewedPoints)}" placeholder="例如 5；若只改答案，可留空" ${disabled}>
        <div class="field-hint">当前值：${row.points} 分。答案和分值至少修改一项。</div>
      </div>

      ${decision === "dispute" ? '<div class="validation-note show">争议判定必须在下方备注中写明具体争议点，否则无法通过导入校验。</div>' : ""}
      ${decision === "correct" ? '<div class="validation-note show">纠错必须填写依据；修改答案或分值至少一项。</div>' : ""}
      ${!decision ? '<div class="review-guide"><strong>建议核对顺序</strong>先看原卷裁图与结构化题面是否一致，再看规范答案和两条来源，最后确认题分。</div>' : ""}

      <div class="note-block">
        <label class="field-label" for="reviewNotes">复核备注 ${decision === "correct" || decision === "dispute" ? "（必填）" : "（可选）"}</label>
        <textarea class="field-textarea" id="reviewNotes" placeholder="写下你确认或修改的依据…" ${disabled}>${escapeHtml(notes)}</textarea>
      </div>

      <div class="autosave-note" id="saveStatus"><span class="autosave-dot"></span><span>${saveStatusText}</span></div>

      ${readonly ? "" : `<div class="auto-next-row">
        <span>选“同意”后自动进入下一题</span>
        <label class="switch"><input id="autoNextToggle" type="checkbox" ${state.autoNext ? "checked" : ""} ${disabled}><span></span></label>
      </div>`}

      <div class="review-actions">
        <button class="btn ${readonly ? "btn-secondary" : "btn-primary"}" type="button" id="nextPendingBtn">${primaryActionLabel}</button>
      </div>
      <div class="review-nav">
        <button class="btn btn-secondary" type="button" id="prevBtn">← 上一题</button>
        <button class="btn btn-secondary" type="button" id="nextBtn">下一题 →</button>
      </div>

      <div class="review-guide">
        <strong>当前题的检查点</strong>
        ${row.priority ? `重点：${escapeHtml(row.priority.reason)}<br>` : ""}
        ${row.image_dependency === "required" ? "此题必须对照原卷图形，文本不足以完整表达。" : "此题文字自足；裁图用于校对题干和选项。"}
        <br>第二来源：${row.secondary_source.id ? escapeHtml(row.secondary_source.publisher) : "缺失，需以主来源和推导复核"}。
      </div>`;

    bindReviewPane(row, decision);
  }

  function bindReviewPane(row, decision) {
    $$("[data-decision]", $("#reviewPane")).forEach((button) => {
      button.addEventListener("click", () => setDecision(row.item_id, button.dataset.decision));
    });
    $("#prevBtn").addEventListener("click", () => nextPrevious(-1));
    $("#nextBtn").addEventListener("click", () => nextPrevious(1));
    $("#nextPendingBtn").addEventListener("click", () => {
      if (readonly) nextPrevious(1);
      else nextPending();
    });
    $("#autoNextToggle")?.addEventListener("change", (event) => {
      state.autoNext = event.target.checked;
      persistState();
    });
    $("#reviewNotes").addEventListener("input", (event) => {
      ensureDecisionRecord(row.item_id).notes = event.target.value;
      persistState();
    });
    const answerInput = $("#reviewedAnswer");
    if (answerInput) {
      answerInput.addEventListener("input", (event) => {
        ensureDecisionRecord(row.item_id).reviewed_answer = event.target.value;
        persistState();
      });
    }
    const pointsInput = $("#reviewedPoints");
    if (pointsInput) {
      pointsInput.addEventListener("input", (event) => {
        ensureDecisionRecord(row.item_id).reviewed_points = event.target.value;
        persistState();
      });
    }
    const copyButton = $("#copyCanonicalAnswer");
    if (copyButton) {
      copyButton.addEventListener("click", () => {
        if (!answerInput) return;
        answerInput.value = canonicalInputValue(row);
        ensureDecisionRecord(row.item_id).reviewed_answer = answerInput.value;
        persistState();
        answerInput.focus();
      });
    }
    $$('[data-option-label]', $("#reviewPane")).forEach((button) => {
      button.addEventListener("click", () => {
        if (!answerInput) return;
        if (row.type === "multi_choice") {
          const current = new Set(
            answerInput.value
              .toUpperCase()
              .replace(/[^A-Z]/g, "")
              .split("")
              .filter(Boolean)
          );
          const label = button.dataset.optionLabel.toUpperCase();
          if (current.has(label)) current.delete(label);
          else current.add(label);
          answerInput.value = Array.from(current).sort().join("");
        } else {
          answerInput.value = button.dataset.optionLabel;
        }
        ensureDecisionRecord(row.item_id).reviewed_answer = answerInput.value;
        persistState();
        answerInput.focus();
      });
    });
  }

  function ensureDecisionRecord(itemId) {
    if (!state.decisions[itemId]) {
      state.decisions[itemId] = {
        decision: "",
        reviewed_answer: "",
        reviewed_points: "",
        notes: "",
        updated_at: "",
      };
    }
    return state.decisions[itemId];
  }

  function canonicalInputValue(row) {
    return Array.isArray(row.canonical) ? JSON.stringify(row.canonical) : String(row.canonical ?? "");
  }

  function setDecision(itemId, decision) {
    if (!VALID_DECISIONS.has(decision)) return;
    if (!META.decision_enabled) {
      showToast(
        META.mode === "signed"
          ? "该版本已经签收，历史文件只读。"
          : "该版本没有可写的 owner review packet，当前只读。",
        true
      );
      return;
    }
    const record = ensureDecisionRecord(itemId);
    if (record.decision === decision) {
      renderReviewPane();
      return;
    }
    record.decision = decision;
    record.updated_at = new Date().toISOString();
    if (decision === "agree") {
      record.reviewed_answer = "";
      record.reviewed_points = "";
    }
    persistState();
    renderList();
    renderReviewPane();
    renderProgress();
    if (decision === "agree" && state.autoNext) {
      window.setTimeout(() => {
        const current = state.selectedId;
        nextPrevious(1);
        if (state.selectedId === current) showToast("已同意；当前筛选下没有下一题。");
      }, 180);
    } else if (decision === "agree") {
      showToast("已记录为同意。");
    }
  }

  function renderProgress() {
    const counts = statusCounts();
    const decided = counts.total - counts.pending;
    const percent = counts.total ? (decided / counts.total) * 100 : 0;
    $("#progressFill").style.width = `${percent.toFixed(2)}%`;
    if (!META.decision_enabled) {
      $("#progressFill").style.width = META.mode === "signed" && counts.total ? "100%" : "0%";
      $("#progressHeadline").textContent = META.mode === "signed"
        ? `${counts.total} 题 · 签收记录已归档`
        : `${counts.total} 题 · 只读回看`;
      $("#progressDetail").textContent =
        META.mode === "signed"
          ? "该版本已签收；当前为历史证据回看，判定字段只读。"
          : "该版本为历史快照；当前可浏览题面、答案、来源和审计记录，判定字段只读。";
    } else {
      $("#progressHeadline").textContent = `${decided} / ${counts.total} 已复核`;
      $("#progressDetail").textContent = counts.pending
        ? `还剩 ${counts.pending} 题；先看重点题，再完成剩余题目。`
        : "全部题目已判定，可以检查并生成签字文件。";
    }
    $("#countAgree").textContent = counts.agree;
    $("#countCorrect").textContent = counts.correct;
    $("#countDispute").textContent = counts.dispute;
    $("#countPending").textContent = counts.pending;
    $("#filterCountAll").textContent = counts.total;
    $("#filterCountPending").textContent = counts.pending;
    $("#filterCountAgree").textContent = counts.agree;
    $("#filterCountCorrect").textContent = counts.correct;
    $("#filterCountDispute").textContent = counts.dispute;
    $("#signAgree").textContent = counts.agree;
    $("#signCorrect").textContent = counts.correct;
    $("#signDispute").textContent = counts.dispute;
    $("#signPending").textContent = counts.pending;
    $("#signBtn").disabled = !META.decision_enabled;
    $("#signBtn").textContent = !META.decision_enabled
      ? META.mode === "signed"
        ? "已签收 · 只读"
        : "历史快照 · 只读"
      : counts.pending
        ? `完成验收 · ${counts.pending} 待复核`
        : "完成验收";
  }

  function applyFilters() {
    const filtered = filteredRows();
    if (!filtered.length) {
      state.selectedId = null;
    } else if (!filtered.some((row) => row.item_id === state.selectedId)) {
      state.selectedId = filtered[0].item_id;
      setHash(state.selectedId);
    }
    renderList();
    renderCenter();
    renderReviewPane();
  }

  function resetFilters() {
    state.filters = {
      search: "",
      paper: "all",
      type: "all",
      status: "all",
      priority: false,
      visual: false,
    };
    if ($("#searchInput")) $("#searchInput").value = "";
    if ($("#paperFilter")) $("#paperFilter").value = "all";
    if ($("#typeFilter")) $("#typeFilter").value = "all";
    if ($("#priorityFilter")) $("#priorityFilter").checked = false;
    if ($("#visualFilter")) $("#visualFilter").checked = false;
    $$(".filter-chip", $("#statusFilters")).forEach((chip, index) => chip.classList.toggle("active", index === 0));
  }

  function renderVersionSelect() {
    const select = $("#versionSelect");
    if (!select) return;
    const groups = [
      ["candidate", "当前候选"],
      ["signed", "已签收版本"],
      ["historical_candidate", "历史候选"],
      ["archive", "存档快照"],
    ];
    select.innerHTML = "";
    for (const [mode, label] of groups) {
      const versions = VERSION_INDEX.filter((item) => item.mode === mode);
      if (!versions.length) continue;
      const group = document.createElement("optgroup");
      group.label = label;
      for (const item of versions) {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.coverage} · ${item.id}`;
        option.title = `${item.mode_label} · ${item.status_label}`;
        group.appendChild(option);
      }
      select.appendChild(group);
    }
    select.value = state.activeVersion || DATA.default_version;
  }

  function modeBadgeClass(mode) {
    return ["candidate", "signed", "historical_candidate", "archive"].includes(mode) ? mode : "archive";
  }

  function updateHeader() {
    if (!META) return;
    const badgeVersion = META.dataset_version.replace("0.1.0-alpha.", "α.");
    $("#versionBadge").textContent = badgeVersion;
    $("#versionBadge").title = META.dataset_version;
    $("#versionSubtitle").textContent = META.subtitle;
    document.title = `Gaokao-Bench 验收台 · ${META.dataset_version} · ${META.mode_label}`;
    $("#statQuestions").textContent = META.questions;
    $("#statPoints").textContent = META.points;
    $("#statScored").textContent = `${META.scored_questions} / ${META.scored_points}`;
    $("#statScored").title = `自动评分 ${META.scored_questions} 题，共 ${META.scored_points} 分`;
    $("#statVisual").textContent = META.visual_required;
    $("#statVisual").title = `视觉必需 ${META.visual_required} 题 / ${META.visual_points} 分`;
    $("#statSources").textContent = META.source_count;
    $("#statSources").title = `主来源与第二来源共 ${META.source_count} 个唯一来源`;
    const pill = $("#versionStatusPill");
    pill.textContent = META.mode_label;
    pill.className = `version-status-pill ${modeBadgeClass(META.mode)}`;
    pill.title = `数据状态：${META.status_label}`;
    $("#helpBoundary p").textContent = META.boundary;
    $("#helpVisual p").textContent = META.visual_explanation + " " + META.source_note;
    $("#priorityCount").textContent = META.priority_count;
    $("#priorityToggleText").textContent = META.priority_count ? `只看重点 ${META.priority_count} 题` : "本版本无重点标注";
    $("#priorityFilter").disabled = !META.priority_count;
    const paperCount = Object.keys(META.by_paper || {}).length;
    $("#paperFilter").setAttribute("aria-label", `试卷筛选，共 ${paperCount} 套`);
  }

  function renderCurrentVersionAudit() {
    const container = $("#currentVersionAudit");
    if (!container) return;
    container.innerHTML = (META.audits || [])
      .map(
        (audit) => `
          <article class="current-audit-card ${escapeHtml(audit.tone || "neutral")}">
            <span>${escapeHtml(audit.id)} · ${escapeHtml(audit.title)}</span>
            <strong>${escapeHtml(audit.label)}</strong>
            <p>${escapeHtml(audit.detail || "")}</p>
          </article>`
      )
      .join("");
  }

  function renderVersionCatalog() {
    const container = $("#versionCatalog");
    if (!container) return;
    container.innerHTML = VERSION_INDEX.map((item) => {
      const active = item.id === state.activeVersion;
      const auditTags = (item.audits || [])
        .map(
          (audit) =>
            `<span class="version-mini-tag ${escapeHtml(audit.tone || "neutral")}">${escapeHtml(audit.id)} · ${escapeHtml(audit.label)}</span>`
        )
        .join("");
      const signedBy = item.signed_reviewer ? ` · 签收人 ${item.signed_reviewer}` : "";
      const created = item.created_at ? item.created_at.slice(0, 10) : "日期未记录";
      return `
        <button type="button" class="version-card${active ? " active" : ""}" data-version-id="${escapeHtml(item.id)}">
          <div class="version-card-head">
            <div class="version-card-title">
              <strong>${escapeHtml(item.coverage)}</strong>
              <code>${escapeHtml(item.id)}</code>
              ${active ? '<span class="version-mini-tag pass">当前</span>' : ""}
            </div>
            <span class="version-card-status ${modeBadgeClass(item.mode)}">${escapeHtml(item.mode_label)}</span>
          </div>
          <div class="version-card-meta">
            <span>复核 ${item.questions} 题 / ${item.points} 分</span>
            <span>自动评分 ${item.scored_questions} 题 / ${item.scored_points} 分</span>
            <span>视觉必需 ${item.visual_required} 题</span>
            <span>${escapeHtml(created)}${escapeHtml(signedBy)}</span>
          </div>
          <div class="version-card-audits">${auditTags}</div>
          <div class="version-card-footer">
            <span>manifest <code>${escapeHtml(String(item.manifest_sha256 || "").slice(0, 16))}</code></span>
            <span>许可 ${escapeHtml(item.license || "未声明")}</span>
            <span>${item.review_enabled ? (item.decision_enabled ? "可逐题判定并生成签字文件" : "已签收/只读回看") : "无可写 review packet · 只读"}</span>
          </div>
        </button>`;
    }).join("");
  }

  function openVersionsDialog() {
    renderCurrentVersionAudit();
    renderVersionCatalog();
    openDialog("versionsDialog");
  }

  function mergeSavedDecisionState() {
    state.decisions = seedDecisions();
    saved = loadSavedState();
    state.autoNext = saved.autoNext !== false;
    if (saved.decisions && typeof saved.decisions === "object") {
      for (const [itemId, decision] of Object.entries(saved.decisions)) {
        if (!ROW_BY_ID.has(itemId)) continue;
        const cleanDecision = normalizeWhitespace(decision?.decision).toLowerCase();
        if (!VALID_DECISIONS.has(cleanDecision)) continue;
        state.decisions[itemId] = {
          decision: cleanDecision,
          reviewed_answer: String(decision.reviewed_answer ?? ""),
          reviewed_points: String(decision.reviewed_points ?? ""),
          notes: String(decision.notes ?? ""),
          updated_at: String(decision.updated_at ?? ""),
        };
      }
    }
    let migrated = false;
    if (META.dataset_version === "0.1.0-alpha.2026-n1") {
      for (const row of ROWS) {
        const key = `gb-2026-n1-check-${row.item_id}`;
        if (localStorage.getItem(key) === "1" && !state.decisions[row.item_id]) {
          state.decisions[row.item_id] = {
            decision: "agree",
            reviewed_answer: "",
            reviewed_points: "",
            notes: "从 2026 单卷验收页的勾选进度迁移。",
            updated_at: new Date().toISOString(),
          };
          migrated = true;
        }
      }
    }
    if (migrated) persistState();
  }

  function activateVersion(versionId, options = {}) {
    const workspace = DATASETS[versionId];
    if (!workspace) {
      showToast(`找不到数据版本 ${versionId}。`, true);
      return;
    }
    WORKSPACE = workspace;
    META = workspace.meta;
    ROWS = workspace.rows || [];
    ROWS.forEach((row) => {
      row.image_data_uri = ASSETS[row.image_asset] || "";
    });
    ROW_BY_ID = new Map(ROWS.map((row) => [row.item_id, row]));
    FIELDS = workspace.export_fields || [];
    STORAGE_KEY = storageKeyFor(META);
    state.activeVersion = versionId;
    state.lastSavedAt = "";
    resetFilters();
    mergeSavedDecisionState();
    state.selectedId = options.itemId && ROW_BY_ID.has(options.itemId) ? options.itemId : null;
    if (!state.selectedId) {
      const firstPending = ROWS.find((row) => !decisionFor(row.item_id)) || ROWS[0];
      state.selectedId = firstPending?.item_id || null;
    }
    renderVersionSelect();
    updateHeader();
    renderAll();
    if (state.selectedId) {
      setHash(state.selectedId);
      requestAnimationFrame(() => {
        const active = $(`.question-item[data-item-id="${CSS.escape(state.selectedId)}"]`);
        if (active) active.scrollIntoView({ block: "nearest" });
        $("#center").scrollTop = 0;
      });
    }
  }

  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
  }

  function closeDialog(id) {
    const dialog = document.getElementById(id);
    if (dialog?.open) dialog.close();
  }

  function openZoom(row) {
    if (!row.image_data_uri) {
      showToast("当前版本没有可放大的原卷裁图。", true);
      return;
    }
    $("#zoomTitle").textContent = `${row.item_id} · 原卷裁图`;
    $("#zoomImage").src = row.image_data_uri;
    $("#zoomImage").alt = `${row.item_id} 原卷裁图放大`;
    openDialog("zoomDialog");
  }

  let toastTimer = null;
  function showToast(message, isError = false) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 3200);
  }

  function csvEscape(value) {
    const text = String(value ?? "");
    if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
    return text;
  }

  function buildCsv() {
    const lines = [FIELDS.map(csvEscape).join(",")];
    for (const row of ROWS) {
      const record = decisionFor(row.item_id);
      const output = { ...row.raw };
      output.decision = record?.decision || "";
      output.reviewed_answer = record?.decision === "correct" ? record.reviewed_answer || "" : "";
      output.reviewed_points = record?.decision === "correct" ? record.reviewed_points || "" : "";
      output.notes = record?.notes || "";
      lines.push(FIELDS.map((field) => csvEscape(output[field] ?? "")).join(","));
    }
    return `${lines.join("\n")}\n`;
  }

  function parseReviewAnswer(value) {
    const text = String(value ?? "").trim();
    if (!text) return null;
    if (/^[\[{\"]/.test(text)) {
      try {
        return JSON.parse(text);
      } catch (error) {
        return text;
      }
    }
    return text;
  }

  function reviewIssues() {
    const issues = [];
    for (const row of ROWS) {
      const record = decisionFor(row.item_id);
      if (!record) {
        issues.push({ itemId: row.item_id, kind: "pending", message: `${row.item_id} 尚未判定` });
        continue;
      }
      const notes = normalizeWhitespace(record.notes);
      if (record.decision === "dispute" && !notes) {
        issues.push({ itemId: row.item_id, kind: "dispute-notes", message: `${row.item_id} 争议判定缺少备注` });
      }
      if (record.decision === "correct") {
        const answerText = String(record.reviewed_answer ?? "").trim();
        const pointsText = String(record.reviewed_points ?? "").trim();
        if (!notes) issues.push({ itemId: row.item_id, kind: "correct-notes", message: `${row.item_id} 纠错缺少依据备注` });
        if (!answerText && !pointsText) {
          issues.push({ itemId: row.item_id, kind: "correct-empty", message: `${row.item_id} 纠错未修改答案或分值` });
        }
        if (answerText) {
          const parsed = parseReviewAnswer(answerText);
          if (JSON.stringify(parsed) === JSON.stringify(row.canonical)) {
            if (!pointsText || Number(pointsText) === Number(row.points)) {
              issues.push({ itemId: row.item_id, kind: "correct-same", message: `${row.item_id} 纠错值与当前答案/分值相同` });
            }
          }
        }
        if (pointsText) {
          const points = Number(pointsText);
          if (!Number.isInteger(points) || points < 0 || points > 50) {
            issues.push({ itemId: row.item_id, kind: "points-invalid", message: `${row.item_id} 分值必须是 0–50 的整数` });
          }
        }
      }
    }
    return issues;
  }

  function downloadText(filename, text, mime = "text/plain;charset=utf-8") {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function exportProgress() {
    downloadText("owner_review-progress.csv", buildCsv(), "text/csv;charset=utf-8");
    const counts = statusCounts();
    showToast(`已导出当前进度：${counts.total - counts.pending} / ${counts.total} 题已判定。`);
  }

  async function sha256Hex(text) {
    if (!window.crypto?.subtle) return "";
    try {
      const bytes = new TextEncoder().encode(text);
      const digest = await window.crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest))
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
    } catch (error) {
      console.warn("Cannot compute SHA-256 in this browser", error);
      return "";
    }
  }

  function localIsoWithOffset() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    const offsetMinutes = -now.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const abs = Math.abs(offsetMinutes);
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
  }

  function signFormReadyForCredentials() {
    const ready = $("#signReadiness").classList.contains("ready");
    const name = $("#reviewerName").value.trim();
    const reviewerId = $("#reviewerId").value.trim();
    const signature = $("#signature").value.trim();
    const reviewedAt = $("#reviewedAt").value.trim();
    const attested = $("#attestationCheck").checked;
    return Boolean(ready && name && reviewerId && signature === name && reviewedAt && attested);
  }

  function refreshSignButtons() {
    const enabled = signFormReadyForCredentials();
    $("#downloadReviewBtn").disabled = !enabled;
    $("#downloadSignoffBtn").disabled = !enabled;
  }

  function updateSignReadiness() {
    const container = $("#signReadiness");
    if (!container) return;
    if (!META.decision_enabled) {
      container.classList.remove("ready");
      container.classList.add("problem");
      container.innerHTML = `<strong>当前版本只读。</strong> ${
        META.mode === "signed" ? "该版本已经签收，不能再次生成签字文件。" : "该版本没有可写的 owner review packet。"
      }`;
      refreshSignButtons();
      return;
    }
    const counts = statusCounts();
    const issues = reviewIssues();
    container.classList.remove("ready", "problem");
    if (!counts.pending && !issues.length) {
      container.classList.add("ready");
      container.innerHTML = `<strong>可以签字。</strong> ${META.total_questions} 道题已全部判定，纠错/争议字段校验通过。请填写真实身份并主动下载两个文件。`;
    } else {
      container.classList.add("problem");
      const pending = issues.filter((issue) => issue.kind === "pending").length;
      const other = issues.length - pending;
      const firstIssues = issues.slice(0, 3);
      container.innerHTML = `
        <strong>还不能签字。</strong>
        ${pending ? `还有 ${pending} 道题未判定；` : ""}
        ${other ? `${other} 个纠错/争议字段未填完整；` : ""}
        请处理后返回。
        ${firstIssues.length ? `<div style="margin-top:5px;color:#6f7380;font-size:10px">${firstIssues.map((issue) => escapeHtml(issue.message)).join("；")}</div>` : ""}`;
    }
    refreshSignButtons();
  }

  function openSignDialog() {
    if (!META.decision_enabled) {
      showToast(
        META.mode === "signed"
          ? "该版本已经签收，历史文件只读。"
          : "该版本没有可写的 owner review packet，当前只读。",
        true
      );
      return;
    }
    updateSignReadiness();
    if (!$("#reviewedAt").value.trim()) $("#reviewedAt").value = localIsoWithOffset();
    openDialog("signDialog");
    window.setTimeout(() => $("#reviewerName").focus(), 80);
  }

  async function downloadReviewCsv() {
    const issues = reviewIssues();
    if (issues.length) {
      showToast("还有未完成或不合法的判定，不能生成签字文件。", true);
      updateSignReadiness();
      return;
    }
    downloadText("owner_review.csv", buildCsv(), "text/csv;charset=utf-8");
    showToast("复核 CSV 已下载：owner_review.csv");
  }

  async function downloadSignoffJson() {
    const issues = reviewIssues();
    if (issues.length) {
      showToast("还有未完成或不合法的判定，不能生成签字文件。", true);
      updateSignReadiness();
      return;
    }
    const name = $("#reviewerName").value.trim();
    const reviewerId = $("#reviewerId").value.trim();
    const signature = $("#signature").value.trim();
    const reviewedAt = $("#reviewedAt").value.trim();
    if (!name || !reviewerId || signature !== name || !reviewedAt) {
      showToast("请完整填写姓名、标识、签名和时间；签名必须与姓名完全一致。", true);
      return;
    }
    if (!$("#attestationCheck").checked) {
      showToast("请勾选验收声明后再签字。", true);
      return;
    }
    const csv = buildCsv();
    const worksheetSha = await sha256Hex(csv);
    const signoff = {
      ...(WORKSPACE.signoff_template || {}),
      reviewer_id: reviewerId,
      reviewer_name: name,
      signature,
      reviewed_at: reviewedAt,
      owner_attested: true,
      notes: $("#signNotes").value.trim(),
      review_worksheet_sha256: worksheetSha,
    };
    downloadText("owner_signoff.json", `${JSON.stringify(signoff, null, 2)}\n`, "application/json;charset=utf-8");
    showToast(worksheetSha ? "签字 JSON 已下载，并已绑定复核 CSV 哈希。" : "签字 JSON 已下载；浏览器未生成 CSV 哈希。");
  }

  function clearAllLocalProgress() {
    const confirmed = window.confirm("清空本浏览器里的全部判定？已下载的 CSV/JSON 不会受影响。");
    if (!confirmed) return;
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
      console.warn(error);
    }
    state.decisions = seedDecisions();
    state.selectedId = ROWS[0]?.item_id || null;
    persistState();
    renderAll();
    showToast("本地判定已清空。");
  }

  function bindGlobalEvents() {
    $("#versionSelect").addEventListener("change", (event) => {
      activateVersion(event.target.value);
    });
    $("#versionsBtn").addEventListener("click", openVersionsDialog);
    $("#versionCatalog").addEventListener("click", (event) => {
      const card = event.target.closest("[data-version-id]");
      if (!card) return;
      closeDialog("versionsDialog");
      activateVersion(card.dataset.versionId);
    });
    $("#helpBtn").addEventListener("click", () => openDialog("helpDialog"));
    $("#signBtn").addEventListener("click", openSignDialog);
    $("#exportProgressBtn").addEventListener("click", exportProgress);
    $("#nextPendingBtnSmall").addEventListener("click", nextPending);
    $("#focusPriorityBtn").addEventListener("click", () => {
      if (!META.priority_count) {
        showToast("当前版本没有重点标注。");
        return;
      }
      $("#priorityFilter").checked = true;
      state.filters.priority = true;
      closeDialog("helpDialog");
      applyFilters();
      showToast(`已切换到重点 ${META.priority_count} 题。`);
    });
    $$("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.dataset.closeDialog));
    });
    $$(".modal, .zoom-modal").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });
    $("#signDialog").addEventListener("close", () => {
      $("#downloadReviewBtn").disabled = true;
      $("#downloadSignoffBtn").disabled = true;
    });
    ["reviewerName", "reviewerId", "signature", "reviewedAt", "attestationCheck"].forEach((id) => {
      document.getElementById(id).addEventListener("input", refreshSignButtons);
      document.getElementById(id).addEventListener("change", refreshSignButtons);
    });
    $("#fillNowBtn").addEventListener("click", () => {
      $("#reviewedAt").value = localIsoWithOffset();
      refreshSignButtons();
    });
    $("#jumpIncompleteBtn").addEventListener("click", () => {
      closeDialog("signDialog");
      nextPending();
    });
    $("#downloadReviewBtn").addEventListener("click", downloadReviewCsv);
    $("#downloadSignoffBtn").addEventListener("click", downloadSignoffJson);
    $("#priorityHelpList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-focus-id]");
      if (!button) return;
      closeDialog("helpDialog");
      selectItem(button.dataset.focusId);
    });

    $("#searchInput").addEventListener("input", (event) => {
      state.filters.search = event.target.value;
      applyFilters();
    });
    $("#paperFilter").addEventListener("change", (event) => {
      state.filters.paper = event.target.value;
      applyFilters();
    });
    $("#typeFilter").addEventListener("change", (event) => {
      state.filters.type = event.target.value;
      applyFilters();
    });
    $("#statusFilters").addEventListener("click", (event) => {
      const button = event.target.closest("[data-status]");
      if (!button) return;
      state.filters.status = button.dataset.status;
      $$(".filter-chip", $("#statusFilters")).forEach((chip) => chip.classList.toggle("active", chip === button));
      applyFilters();
    });
    $("#priorityFilter").addEventListener("change", (event) => {
      state.filters.priority = event.target.checked;
      applyFilters();
    });
    $("#visualFilter").addEventListener("change", (event) => {
      state.filters.visual = event.target.checked;
      applyFilters();
    });
    $("#questionList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-item-id]");
      if (button) selectItem(button.dataset.itemId);
    });
    window.addEventListener("hashchange", () => {
      const target = parseHashState();
      if (target.version && target.version !== state.activeVersion) {
        activateVersion(target.version, { itemId: target.itemId });
      } else if (target.itemId && target.itemId !== state.selectedId && ROW_BY_ID.has(target.itemId)) {
        selectItem(target.itemId);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      const active = document.activeElement;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
      if (document.querySelector("dialog[open]")) return;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        nextPrevious(1);
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        nextPrevious(-1);
      } else if (event.key === "1" && state.selectedId) {
        setDecision(state.selectedId, "agree");
      } else if (event.key === "2" && state.selectedId) {
        setDecision(state.selectedId, "correct");
      } else if (event.key === "3" && state.selectedId) {
        setDecision(state.selectedId, "dispute");
      }
    });
    const helpFooter = document.createElement("div");
    helpFooter.className = "callout warning compact";
    helpFooter.style.marginTop = "14px";
    helpFooter.innerHTML = '<strong>进度保存在浏览器本地</strong><p>刷新页面不会丢失。若换浏览器或清理缓存，进度会丢失；建议中途用“导出进度”备份。页面不会自行签字或调用模型。</p>';
    $("#helpDialog .modal-body").appendChild(helpFooter);
    const resetButton = document.createElement("button");
    resetButton.type = "button";
    resetButton.className = "btn btn-secondary";
    resetButton.style.marginTop = "9px";
    resetButton.textContent = "清空本地判定";
    resetButton.addEventListener("click", clearAllLocalProgress);
    $("#helpDialog .modal-body").appendChild(resetButton);
  }

  function renderAll() {
    updateHeader();
    renderPaperOptions();
    renderPriorityHelp();
    renderProgress();
    renderList();
    renderCenter();
    renderReviewPane();
  }

  function init() {
    bindGlobalEvents();
    const target = parseHashState();
    const version = target.version && DATASETS[target.version] ? target.version : DATA.default_version;
    activateVersion(version, { itemId: target.itemId });
    const focus = new URLSearchParams(window.location.search).get("focus");
    if (focus === "priority" && META.priority_count) {
      $("#priorityFilter").checked = true;
      state.filters.priority = true;
      applyFilters();
    } else if (focus === "visual") {
      $("#visualFilter").checked = true;
      state.filters.visual = true;
      applyFilters();
    }
    const view = new URLSearchParams(window.location.search).get("view");
    if (view === "versions") {
      window.setTimeout(openVersionsDialog, 0);
    } else if (view === "help") {
      window.setTimeout(() => openDialog("helpDialog"), 0);
    }
    if (!saved.decisions || !Object.keys(saved.decisions).length) {
      showToast(
        META.decision_enabled
          ? "已载入全部版本。点击“版本档案”切换历史版本；当前候选可直接逐题判定。"
          : "已载入历史版本。当前版本只读；点击“版本档案”查看全部版本与审计状态。"
      );
    }
  }

  init();
})();
