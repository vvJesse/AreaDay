const byId = (id) => document.querySelector(`#${id}`);
const all = (selector) => [...document.querySelectorAll(selector)];
const formatCount = (value) => new Intl.NumberFormat("zh-CN").format(value || 0);
const formatPercent = (value) => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value || 0);
const EXPECTED_API_VERSION = 5;
const TERMS_PER_PAGE = 20;
const BRIEFS_PER_PAGE = 2;
const TERM_CHECK_BATCH_SIZE = 5;

const views = {
  vocabulary: byId("vocabularyView"),
  briefs: byId("briefsView"),
  paper: byId("paperView"),
  review: byId("reviewView"),
  schedule: byId("scheduleView"),
};

const answerButtons = all(".answer");
const thresholdSlider = byId("thresholdSlider");
const thresholdValue = byId("thresholdValue");
const thresholdEffect = byId("thresholdEffect");
const recommendedMark = byId("recommendedMark");
const resetThresholdButton = byId("resetThresholdButton");
const domainSelect = byId("domainSelect");

let appState = null;
let currentDomainId = null;
let domainGeneration = 0;
let domainAbortController = new AbortController();
let currentView = "vocabulary";
let currentWord = null;
let currentPaper = null;
let currentReviewWord = null;
let currentTerms = [];
let vocabularySection = "words";
let termPage = 1;
let termSearchQuery = "";
let termStatusFilter = "all";
let currentBriefs = [];
let briefPage = 1;
let briefSearchQuery = "";
let selectedBriefId = "";
let terminologyPromise = null;
let dueReviewItems = [];
let termCheckBatch = [];
let termCheckIndex = 0;
let termCheckResults = { mastered: 0, learning: 0, skipped: 0 };
let termCheckSkippedThisVisit = new Set();
let busy = false;
let thresholdTimer = null;
let thresholdRequestId = 0;
let mutationRevision = 0;
let recommendedThreshold = 90;

class StaleDomainResponse extends Error {}

function domainUrl(path, domainId) {
  const target = new URL(path, window.location.origin);
  if (domainId && target.pathname.startsWith("/api/")) {
    target.searchParams.set("domain_id", domainId);
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

async function request(path, options = {}, requestContext = {}) {
  const domainId = requestContext.domainId === undefined ? currentDomainId : requestContext.domainId;
  const generation = requestContext.generation === undefined ? domainGeneration : requestContext.generation;
  const headers = {
    "Content-Type": "application/json",
    "X-ResearchRamp-API-Version": String(EXPECTED_API_VERSION),
    ...(options.headers || {}),
  };
  if (domainId) headers["X-ResearchRamp-Domain"] = domainId;
  const response = await fetch(domainUrl(path, domainId), {
    ...options,
    headers,
    signal: requestContext.signal || options.signal || domainAbortController.signal,
  });
  const data = await response.json();
  if (data.api_version !== EXPECTED_API_VERSION) {
    throw new Error("本地服务版本已经更新，请关闭旧页面并重新启动 AreaDay。");
  }
  if (!response.ok) throw new Error(data.error || "请求失败");
  if (domainId && data.domain_id !== domainId) {
    throw new Error("研究领域响应缺失或不匹配，已停止显示这次结果。");
  }
  if (generation !== domainGeneration) throw new StaleDomainResponse();
  return data;
}

function setText(id, value) {
  byId(id).textContent = value;
}

function clearViews() {
  byId("questionView").hidden = true;
  byId("errorView").hidden = true;
  Object.values(views).forEach((view) => { view.hidden = true; });
}

function showView(name, { updateHash = true } = {}) {
  if (appState?.standalone) name = "vocabulary";
  if (!views[name]) name = "vocabulary";
  currentView = name;
  clearViews();
  views[name].hidden = false;
  all("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  if (updateHash) {
    const url = new URL(window.location.href);
    if (currentDomainId) url.searchParams.set("domain", currentDomainId);
    url.hash = name;
    history.replaceState(null, "", url);
  }
  if (name === "review") loadReviewPage().catch(showError);
  if (name === "schedule") renderSchedule(appState.settings);
  window.scrollTo({ top: 0, behavior: "instant" });
}

function renderWordList(elementId, words) {
  const element = byId(elementId);
  element.replaceChildren();
  for (const word of words) {
    const chip = document.createElement("span");
    chip.className = "word-chip";
    chip.textContent = word.lemma;
    element.appendChild(chip);
  }
}

function renderImportance(importance, retainedCount) {
  const priorityCount = importance.priority_word_count;
  setText("priorityCount", formatCount(priorityCount));
  setText("importanceHeadline", `真正值得优先学习的是 ${formatCount(priorityCount)} 个词`);
  setText(
    "importanceSummary",
    `系统从 ${formatCount(importance.corpus_document_count)} 篇论文中找出了 ${formatCount(retainedCount)} 个可能仍会影响你阅读的词，并将它们加入个人词表。其中 ${formatCount(priorityCount)} 个会反复出现，建议优先掌握；另外 ${formatCount(importance.occasional_word_count)} 个只在少数论文中出现，阅读相关文章时再按需处理。`,
  );
  const tierColors = { A: "var(--tier-a)", B: "var(--tier-b)", C: "var(--tier-c)", D: "var(--tier-d)" };
  let position = 0;
  const stops = [];
  for (const tier of importance.tiers) {
    const start = position;
    position += retainedCount ? (tier.count / retainedCount) * 100 : 0;
    stops.push(`${tierColors[tier.key]} ${start}% ${position}%`);
  }
  const donut = byId("importanceDonut");
  donut.style.background = `conic-gradient(${stops.join(", ")})`;
  donut.setAttribute("aria-label", importance.tiers.map((tier) => `${tier.key}级${tier.name} ${tier.count}个`).join("，"));
  const legend = byId("importanceLegend");
  legend.replaceChildren();
  for (const tier of importance.tiers) {
    const row = document.createElement("div");
    row.className = `tier-row tier-${tier.key.toLowerCase()}`;
    const badge = document.createElement("span");
    badge.className = "tier-badge";
    badge.textContent = tier.key;
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = tier.name;
    const range = document.createElement("small");
    range.textContent = tier.range;
    copy.append(name, range);
    const count = document.createElement("b");
    count.textContent = formatCount(tier.count);
    row.append(badge, copy, count);
    legend.appendChild(row);
  }
}

function renderMastery(mastery) {
  const overview = byId("masteryOverview");
  const groups = mastery?.groups || [];
  const priority = groups.find((group) => group.key === "priority");
  const other = groups.find((group) => group.key === "other");
  if (!priority || !other) {
    overview.hidden = true;
    return;
  }
  overview.hidden = false;
  for (const [prefix, group] of [["priority", priority], ["other", other]]) {
    const percent = Math.max(0, Math.min(100, Number(group.mastery_percent) || 0));
    setText(`${prefix}MasteryPercent`, `${formatPercent(percent)}%`);
    setText(
      `${prefix}MasteryCount`,
      `${formatCount(group.mastered_count)} / ${formatCount(group.total_count)}`,
    );
    byId(`${prefix}MasteryBar`).style.width = `${percent}%`;
    byId(`${prefix}MasteryTrack`).setAttribute("aria-valuenow", String(percent));
  }
}

function renderThreshold(result) {
  const { threshold, counts } = result;
  thresholdSlider.min = threshold.minimum_percent;
  thresholdSlider.max = threshold.maximum_percent;
  thresholdSlider.step = threshold.step_percent;
  thresholdSlider.value = threshold.selected_percent;
  recommendedThreshold = threshold.default_percent;
  const recommendedPosition = ((threshold.default_percent - threshold.minimum_percent) / (threshold.maximum_percent - threshold.minimum_percent)) * 100;
  recommendedMark.style.left = `${recommendedPosition}%`;
  thresholdSlider.setAttribute("aria-valuetext", `当前词表预计收入 ${counts.remaining_after_conservative_exclusion} 个词`);
  thresholdValue.textContent = `${formatCount(counts.remaining_after_conservative_exclusion)} 个词`;
  resetThresholdButton.disabled = threshold.selected_percent === threshold.default_percent;
  const protectionCopy = counts.important_boundary_protected
    ? `其中 ${formatCount(counts.important_boundary_protected)} 个 A/B 级词因领域重要性被优先加入。`
    : "当前没有需要额外保护的 A/B 级边界词。";
  thresholdEffect.textContent = `${formatCount(counts.likely_known)} 个候选词暂不加入个人词表。${protectionCopy}`;
}

function renderCalibration(calibration) {
  mutationRevision = Number(calibration.mutation_revision || 0);
  const calibrationNotice = byId("calibrationNotice");
  calibrationNotice.hidden = !calibration.recovery_notice;
  calibrationNotice.textContent = calibration.recovery_notice || "";
  setText("corpusLabel", calibration.corpus_label ? `· ${calibration.corpus_label}` : "");
  setText("progressText", `${calibration.answered} / ${calibration.question_limit}`);
  if (!calibration.complete) {
    clearViews();
    byId("mainNav").hidden = true;
    byId("questionView").hidden = false;
    currentWord = calibration.word.lemma;
    setText("word", calibration.word.lemma);
    setText("partOfSpeech", calibration.word.part_of_speech.toLowerCase());
    byId("progressBar").style.width = `${(calibration.answered / calibration.question_limit) * 100}%`;
    return false;
  }
  currentWord = null;
  byId("mainNav").hidden = Boolean(appState?.standalone);
  setText("progressText", "词表已建立");
  const counts = calibration.result.counts;
  setText("totalCount", formatCount(counts.total));
  setText("knownCount", formatCount(counts.likely_known));
  setText("remainingCount", formatCount(counts.remaining_after_conservative_exclusion));
  setText("knownLegend", formatCount(counts.likely_known));
  setText("uncertainLegend", formatCount(counts.uncertain));
  setText("unknownLegend", formatCount(counts.likely_unknown));
  byId("knownBar").style.width = `${(counts.likely_known / counts.total) * 100}%`;
  byId("uncertainBar").style.width = `${(counts.uncertain / counts.total) * 100}%`;
  byId("unknownBar").style.width = `${(counts.likely_unknown / counts.total) * 100}%`;
  renderThreshold(calibration.result);
  renderMastery(calibration.result.mastery);
  renderImportance(calibration.result.importance, counts.remaining_after_conservative_exclusion);
  renderWordList("knownBoundary", calibration.result.known_boundary);
  renderWordList("remainingBoundary", calibration.result.remaining_boundary);
  const exportLink = document.querySelector(".download");
  exportLink.href = domainUrl("/api/export.tsv", currentDomainId);
  return true;
}

function renderDomainControls(state) {
  const switcher = byId("domainSwitcher");
  domainSelect.replaceChildren();
  for (const domain of state.domains) {
    const option = document.createElement("option");
    option.value = domain.domain_id;
    option.textContent = domain.display_name;
    option.selected = domain.domain_id === state.domain_id;
    domainSelect.appendChild(option);
  }
  switcher.hidden = state.standalone || state.domains.length < 2;
  domainSelect.disabled = state.domains.length < 2;
}

function renderAppState(state, preferredView = null) {
  appState = state;
  currentDomainId = state.domain_id;
  currentPaper = null;
  currentReviewWord = null;
  renderDomainControls(state);
  if (!state.standalone) {
    const terminology = state.terminology || { count: 0, terms: [] };
    currentTerms = terminology.terms;
    terminologyPromise = Promise.resolve(terminology);
    setText("termTabCount", `(${terminology.count})`);
    setText("terminologyHeadline", `${terminology.count} 个经过质量审核的领域术语`);
    renderTerminologyLibrary();
    renderTermDiscoveryCard();
  }
  if (!renderCalibration(state.calibration)) return;

  if (state.standalone) {
    byId("mainNav").hidden = true;
    showView("vocabulary", { updateHash: true });
    return;
  }

  renderBriefs(state.briefs);
  const due = state.continuous?.due_count || 0;
  byId("navDueCount").hidden = due === 0;
  byId("navDueCount").textContent = due;
  renderSchedule(state.settings);
  let nextView = preferredView || state.initial_view;
  if (nextView === "paper") nextView = "briefs";
  showView(views[nextView] ? nextView : "vocabulary", { updateHash: true });
}

function showVocabularySection(section) {
  vocabularySection = section === "terms" ? "terms" : "words";
  byId("wordVocabularyPanel").hidden = vocabularySection !== "words";
  byId("terminologyPanel").hidden = vocabularySection !== "terms";
  byId("wordTab").classList.toggle("active", vocabularySection === "words");
  byId("termTab").classList.toggle("active", vocabularySection === "terms");
  byId("wordTab").setAttribute("aria-selected", String(vocabularySection === "words"));
  byId("termTab").setAttribute("aria-selected", String(vocabularySection === "terms"));
}

function termStatusLabel(status) {
  if (status === "learning") return "学习中";
  if (status === "mastered") return "已经理解";
  return "尚未确认";
}

function normalizeTermSearch(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function levenshteinDistance(left, right) {
  if (left === right) return 0;
  if (!left.length) return right.length;
  if (!right.length) return left.length;
  let previous = [...Array(right.length + 1).keys()];
  for (let row = 1; row <= left.length; row += 1) {
    const current = [row];
    for (let column = 1; column <= right.length; column += 1) {
      current[column] = Math.min(
        current[column - 1] + 1,
        previous[column] + 1,
        previous[column - 1] + (left[row - 1] === right[column - 1] ? 0 : 1),
      );
    }
    previous = current;
  }
  return previous[right.length];
}

function fuzzyTermMatch(term, query) {
  const normalizedQuery = normalizeTermSearch(query);
  if (!normalizedQuery) return true;
  const queryTokens = normalizedQuery.split(" ");
  const termName = normalizeTermSearch(term.term);
  if (termName.includes(normalizedQuery)) return true;
  const termTokens = termName.split(" ").filter(Boolean);
  const fuzzyNameMatch = queryTokens.every((token) => termTokens.some((candidate) => {
    if (candidate.includes(token) || token.includes(candidate)) return true;
    const allowance = token.length >= 8 ? 2 : token.length >= 4 ? 1 : 0;
    return allowance > 0 && Math.abs(candidate.length - token.length) <= allowance
      && levenshteinDistance(candidate, token) <= allowance;
  }));
  if (fuzzyNameMatch) return true;
  const referenceFields = [term.meaning_en, term.meaning_zh, term.concept_role, term.source_title]
    .map(normalizeTermSearch);
  return referenceFields.some((field) => (
    field.includes(normalizedQuery) || queryTokens.every((token) => field.includes(token))
  ));
}

function termActionButtons(term) {
  const actions = document.createElement("div");
  actions.className = "term-actions";
  if (term.global_status === "new") {
    const understood = document.createElement("button");
    understood.type = "button";
    understood.textContent = "已经会了，不加入复习";
    understood.addEventListener("click", () => updateTerm(term.item_id, "mastered"));
    const learn = document.createElement("button");
    learn.type = "button";
    learn.className = "learn-action";
    learn.textContent = "需要学习，加入复习";
    learn.addEventListener("click", () => updateTerm(term.item_id, "learning"));
    actions.append(understood, learn);
  } else if (term.global_status === "learning") {
    const mastered = document.createElement("button");
    mastered.type = "button";
    mastered.textContent = "我已理解，不再复习";
    mastered.addEventListener("click", () => updateTerm(term.item_id, "mastered"));
    actions.appendChild(mastered);
  } else {
    const restore = document.createElement("button");
    restore.type = "button";
    restore.textContent = "恢复复习";
    restore.addEventListener("click", () => updateTerm(term.item_id, "restore"));
    actions.appendChild(restore);
  }
  return actions;
}

function renderTermRow(term) {
  const row = document.createElement("article");
  row.className = `term-row${term.global_status === "mastered" ? " term-row-mastered" : ""}`;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const copy = document.createElement("div"); copy.className = "term-row-copy";
  const title = document.createElement("h3"); title.textContent = term.term;
  const meaning = document.createElement("p"); meaning.textContent = term.meaning_zh;
  copy.append(title, meaning);
  const meta = document.createElement("div"); meta.className = "term-row-meta";
  const coverage = document.createElement("span"); coverage.textContent = `${term.document_count} 篇论文`;
  const state = document.createElement("span"); state.className = "status-pill"; state.textContent = termStatusLabel(term.global_status);
  const expand = document.createElement("span"); expand.className = "term-expand"; expand.textContent = "查看语境";
  meta.append(coverage, state, expand);
  summary.append(copy, meta);
  const body = document.createElement("div"); body.className = "term-row-body";
  const meaningEn = document.createElement("p"); meaningEn.className = "term-definition-en"; meaningEn.textContent = term.meaning_en;
  const role = document.createElement("p"); role.className = "term-role"; role.textContent = term.concept_role;
  const context = document.createElement("p"); context.className = "term-context"; context.textContent = term.context;
  const source = document.createElement("a"); source.className = "term-source"; source.href = term.source_url; source.target = "_blank"; source.rel = "noreferrer"; source.textContent = `来源：${term.source_title} ↗`;
  body.append(meaningEn, role, context, source, termActionButtons(term));
  details.append(summary, body);
  row.appendChild(details);
  return row;
}

function filteredTerms() {
  return currentTerms
    .filter((term) => termStatusFilter === "all" || term.global_status === termStatusFilter)
    .filter((term) => fuzzyTermMatch(term, termSearchQuery));
}

function renderTermPagination(pageCount) {
  const pagination = byId("termPagination");
  pagination.hidden = pageCount <= 1;
  byId("termPreviousPage").disabled = termPage <= 1;
  byId("termNextPage").disabled = termPage >= pageCount;
  const buttons = byId("termPageButtons");
  buttons.replaceChildren();
  for (let page = 1; page <= pageCount; page += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(page);
    button.classList.toggle("active", page === termPage);
    button.setAttribute("aria-label", `第 ${page} 页`);
    if (page === termPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => {
      termPage = page;
      renderTerminologyLibrary();
      byId("terminologyPanel").scrollIntoView({ block: "start", behavior: "smooth" });
    });
    buttons.appendChild(button);
  }
}

function renderTerminologyLibrary() {
  const counts = { all: currentTerms.length, new: 0, learning: 0, mastered: 0 };
  currentTerms.forEach((term) => { counts[term.global_status] += 1; });
  setText("termFilterAll", counts.all);
  setText("termFilterNew", counts.new);
  setText("termFilterLearning", counts.learning);
  setText("termFilterMastered", counts.mastered);
  all("[data-term-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.termFilter === termStatusFilter);
  });
  const terms = filteredTerms();
  const pageCount = Math.max(1, Math.ceil(terms.length / TERMS_PER_PAGE));
  termPage = Math.min(Math.max(1, termPage), pageCount);
  const start = (termPage - 1) * TERMS_PER_PAGE;
  const pageTerms = terms.slice(start, start + TERMS_PER_PAGE);
  byId("terminologyList").replaceChildren(...pageTerms.map(renderTermRow));
  byId("termLibraryEmpty").hidden = terms.length > 0;
  const range = terms.length ? `${start + 1}–${start + pageTerms.length}` : "0";
  const searchCopy = termSearchQuery ? `，搜索“${termSearchQuery}”` : "";
  setText("termLibrarySummary", `共找到 ${terms.length} 个术语${searchCopy}，当前显示 ${range}。`);
  renderTermPagination(pageCount);
}

function setLocalTermStatus(itemId, status) {
  currentTerms = currentTerms.map((term) => (
    term.item_id === itemId ? { ...term, global_status: status } : term
  ));
}

function updateNavDueCount(count) {
  const navCount = byId("navDueCount");
  navCount.hidden = count === 0;
  navCount.textContent = count;
}

async function persistTermStatus(itemId, status) {
  const result = status === "restore"
    ? await request("/api/learning/restore", { method: "POST", body: JSON.stringify({ item_id: itemId }) })
    : await request("/api/terms/status", { method: "POST", body: JSON.stringify({ item_id: itemId, status }) });
  const resolvedStatus = status === "restore" ? "learning" : status;
  setLocalTermStatus(itemId, resolvedStatus);
  if (result.continuous) {
    appState.continuous = result.continuous;
    updateNavDueCount(result.continuous.due_count || 0);
  }
  renderTerminologyLibrary();
  renderTermDiscoveryCard();
  return result;
}

async function updateTerm(itemId, status) {
  if (busy) return;
  busy = true;
  try {
    await persistTermStatus(itemId, status);
    if (currentPaper) currentPaper = await request(`/api/paper?id=${encodeURIComponent(currentPaper.item_id)}`);
    if (currentPaper && currentView === "paper") renderPaperTerminology(currentPaper);
    await loadReview({ continueSession: false });
  } catch (error) { showError(error); }
  finally { busy = false; }
}

async function loadTerminology({ force = false } = {}) {
  if (appState?.standalone) return null;
  if (!force && terminologyPromise) return terminologyPromise;
  terminologyPromise = (async () => {
    const data = await request("/api/terms");
    currentTerms = data.terms;
    setText("termTabCount", `(${data.count})`);
    setText("terminologyHeadline", `${data.count} 个经过质量审核的领域术语`);
    renderTerminologyLibrary();
    renderTermDiscoveryCard();
    return data;
  })();
  try {
    return await terminologyPromise;
  } catch (error) {
    terminologyPromise = null;
    throw error;
  }
}

async function switchDomain(domainId) {
  if (!domainId || domainId === currentDomainId) return;
  window.clearTimeout(thresholdTimer);
  thresholdRequestId += 1;
  const generation = ++domainGeneration;
  domainAbortController.abort();
  domainAbortController = new AbortController();
  const preferredView = currentView === "paper" ? "briefs" : currentView;
  currentDomainId = domainId;
  currentWord = null;
  currentPaper = null;
  currentReviewWord = null;
  currentTerms = [];
  currentBriefs = [];
  briefPage = 1;
  briefSearchQuery = "";
  selectedBriefId = "";
  terminologyPromise = null;
  termPage = 1;
  termSearchQuery = "";
  termStatusFilter = "all";
  byId("termSearch").value = "";
  byId("briefSearch").value = "";
  byId("briefEditionSelect").value = "";
  dueReviewItems = [];
  termCheckBatch = [];
  termCheckIndex = 0;
  termCheckSkippedThisVisit = new Set();
  busy = true;
  answerButtons.forEach((button) => { button.disabled = true; });
  clearViews();
  byId("mainNav").hidden = true;
  setText("progressText", "正在切换研究领域…");
  try {
    const state = await request(
      "/api/app-state",
      {},
      { domainId, generation, signal: domainAbortController.signal },
    );
    busy = false;
    answerButtons.forEach((button) => { button.disabled = false; });
    renderAppState(state, preferredView);
  } catch (error) {
    if (error instanceof StaleDomainResponse || error.name === "AbortError") return;
    busy = false;
    answerButtons.forEach((button) => { button.disabled = false; });
    showError(error);
  }
}

const itemTypeLabels = {
  new_paper: "近期新论文",
  recent_paper: "近期值得补读",
  classic_paper: "经典论文",
  backlog_paper: "值得补读",
  public_report: "公开报告",
  research_update: "研究资讯",
};

function itemTypeLabel(itemType) {
  const label = itemTypeLabels[itemType];
  if (!label) throw new Error(`发现不受支持的简报内容类型：${itemType || "未标注"}`);
  return label;
}

function renderBriefMetrics(brief) {
  const container = byId("briefMetrics");
  container.replaceChildren();
  if (!brief) return;
  const paperCount = brief.items.filter((item) => ["new_paper", "recent_paper", "classic_paper", "backlog_paper"].includes(item.item_type)).length;
  const supplementalCount = brief.items.length - paperCount;
  const wordCount = brief.items.reduce((sum, item) => sum + (item.estimated_unfamiliar_words || 0), 0);
  const metrics = [
    ["本期推荐", `${brief.items.length} 项`],
    ["论文", `${paperCount} 篇`],
    ...(supplementalCount ? [["公开资料", `${supplementalCount} 项`]] : []),
    ["预计生词", `${wordCount} 个`],
  ];
  for (const [label, value] of metrics) {
    const metric = document.createElement("article");
    metric.className = "metric card";
    const span = document.createElement("span"); span.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    metric.append(span, strong);
    container.appendChild(metric);
  }
}

function briefSearchFields(brief) {
  const itemFields = brief.items.flatMap((item) => [
    item.title,
    item.venue,
    item.value_reason,
    item.shadow_preview,
    item.publication_date,
    ...(item.vocabulary || []).flatMap((word) => [word.lemma, word.meaning_en, word.meaning_zh]),
  ]);
  return [brief.headline, brief.summary, brief.period_start, brief.period_end, ...itemFields]
    .map(normalizeTermSearch)
    .filter(Boolean);
}

function fuzzySearchToken(field, token) {
  if (field.includes(token)) return true;
  const allowance = token.length >= 8 ? 2 : token.length >= 4 ? 1 : 0;
  if (!allowance) return false;
  const words = field.split(" ").filter(Boolean);
  if (words.some((word) => (
    Math.abs(word.length - token.length) <= allowance
    && levenshteinDistance(word, token) <= allowance
  ))) return true;
  if (!/[\p{Script=Han}]/u.test(token)) return false;
  const compact = field.replaceAll(" ", "");
  const minimum = Math.max(1, token.length - allowance);
  const maximum = Math.min(compact.length, token.length + allowance);
  for (let length = minimum; length <= maximum; length += 1) {
    for (let index = 0; index <= compact.length - length; index += 1) {
      const candidate = compact.slice(index, index + length);
      if (levenshteinDistance(candidate, token) <= allowance) return true;
    }
  }
  return false;
}

function fuzzyBriefMatch(brief, query) {
  const normalizedQuery = normalizeTermSearch(query);
  if (!normalizedQuery) return true;
  const fields = briefSearchFields(brief);
  if (fields.some((field) => field.includes(normalizedQuery))) return true;
  return normalizedQuery.split(" ").every((token) => (
    fields.some((field) => fuzzySearchToken(field, token))
  ));
}

function filteredBriefs() {
  return currentBriefs
    .filter((brief) => !selectedBriefId || brief.brief_id === selectedBriefId)
    .filter((brief) => fuzzyBriefMatch(brief, briefSearchQuery));
}

function renderBriefEdition(brief) {
  const edition = document.createElement("section");
  edition.className = "brief-edition card";
  edition.dataset.briefId = brief.brief_id;
  const header = document.createElement("div");
  header.className = "brief-edition-header";
  const copy = document.createElement("div");
  const eyebrow = document.createElement("p"); eyebrow.className = "eyebrow"; eyebrow.textContent = "研究简报";
  const title = document.createElement("h2"); title.textContent = brief.headline;
  const summary = document.createElement("p"); summary.textContent = brief.summary;
  copy.append(eyebrow, title, summary);
  const period = document.createElement("span"); period.className = "brief-period"; period.textContent = `${brief.period_start} — ${brief.period_end}`;
  header.append(copy, period);
  const items = document.createElement("div"); items.className = "brief-items";
  brief.items.forEach((item, index) => {
    const button = document.createElement("button"); button.type = "button"; button.className = "brief-item";
    button.addEventListener("click", () => openPaper(item.item_id));
    const rank = document.createElement("span"); rank.className = "brief-rank"; rank.textContent = index === 0 ? "首" : String(index + 1);
    const itemCopy = document.createElement("span"); itemCopy.className = "brief-item-copy";
    const itemTitle = document.createElement("strong"); itemTitle.textContent = item.title;
    const reason = document.createElement("small"); reason.textContent = item.value_reason;
    itemCopy.append(itemTitle, reason);
    const meta = document.createElement("span"); meta.className = "brief-item-meta";
    const badge = document.createElement("span"); badge.className = "source-badge"; badge.textContent = itemTypeLabel(item.item_type);
    const effort = document.createElement("span"); effort.textContent = `${item.estimated_minutes} 分钟 · ${item.estimated_unfamiliar_words || 0} 个生词 · ${item.estimated_terms || 0} 个术语`;
    meta.append(badge, effort);
    button.append(rank, itemCopy, meta);
    items.appendChild(button);
  });
  edition.append(header, items);
  return edition;
}

function populateBriefSelector(briefs) {
  const selector = byId("briefEditionSelect");
  selector.replaceChildren();
  const allBriefs = document.createElement("option");
  allBriefs.value = "";
  allBriefs.textContent = `全部简报（${briefs.length}）`;
  selector.appendChild(allBriefs);
  for (const brief of briefs) {
    const option = document.createElement("option");
    option.value = brief.brief_id;
    option.textContent = `${brief.period_start} — ${brief.period_end} · ${brief.headline}`;
    option.title = brief.headline;
    option.selected = brief.brief_id === selectedBriefId;
    selector.appendChild(option);
  }
}

function briefPaginationTokens(pageCount) {
  const pages = [...new Set([1, pageCount, briefPage - 1, briefPage, briefPage + 1])]
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((left, right) => left - right);
  const tokens = [];
  pages.forEach((page, index) => {
    if (index && page - pages[index - 1] > 1) tokens.push(`ellipsis-${page}`);
    tokens.push(page);
  });
  return tokens;
}

function renderBriefPagination(pageCount) {
  const pagination = byId("briefPagination");
  pagination.hidden = pageCount <= 1;
  byId("briefPreviousPage").disabled = briefPage <= 1;
  byId("briefNextPage").disabled = briefPage >= pageCount;
  const buttons = byId("briefPageButtons");
  buttons.replaceChildren();
  for (const token of briefPaginationTokens(pageCount)) {
    if (typeof token === "string") {
      const ellipsis = document.createElement("span");
      ellipsis.className = "brief-page-ellipsis";
      ellipsis.textContent = "…";
      ellipsis.setAttribute("aria-hidden", "true");
      buttons.appendChild(ellipsis);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = String(token);
    button.classList.toggle("active", token === briefPage);
    button.setAttribute("aria-label", `第 ${token} 页`);
    if (token === briefPage) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => {
      briefPage = token;
      renderBriefResults({ scroll: true });
    });
    buttons.appendChild(button);
  }
}

function renderBriefResults({ scroll = false } = {}) {
  const briefs = filteredBriefs();
  const pageCount = Math.max(1, Math.ceil(briefs.length / BRIEFS_PER_PAGE));
  briefPage = Math.min(Math.max(1, briefPage), pageCount);
  const start = (briefPage - 1) * BRIEFS_PER_PAGE;
  const pageBriefs = briefs.slice(start, start + BRIEFS_PER_PAGE);
  byId("briefsList").replaceChildren(...pageBriefs.map(renderBriefEdition));
  byId("briefNoResults").hidden = briefs.length > 0;
  const range = briefs.length ? `${start + 1}–${start + pageBriefs.length}` : "0";
  const searchCopy = briefSearchQuery ? `，搜索“${briefSearchQuery}”` : "";
  const selectedCopy = selectedBriefId ? "，已切换到指定简报" : "";
  setText("briefResultSummary", `共找到 ${briefs.length} 份简报${searchCopy}${selectedCopy}，当前显示 ${range}。`);
  byId("briefClearFilters").hidden = !briefSearchQuery && !selectedBriefId;
  renderBriefMetrics(pageBriefs[0] || null);
  renderBriefPagination(pageCount);
  if (scroll) byId("briefExplorer").scrollIntoView({ block: "start", behavior: "smooth" });
}

function renderBriefs(briefs) {
  currentBriefs = briefs;
  const hasBriefs = briefs.length > 0;
  byId("briefEmpty").hidden = hasBriefs;
  byId("briefExplorer").hidden = !hasBriefs;
  byId("briefNoResults").hidden = true;
  byId("briefPagination").hidden = true;
  if (!hasBriefs) {
    byId("briefsList").replaceChildren();
    renderBriefMetrics(null);
    return;
  }
  if (selectedBriefId && !briefs.some((brief) => brief.brief_id === selectedBriefId)) selectedBriefId = "";
  populateBriefSelector(briefs);
  renderBriefResults();
}

function clearBriefFilters() {
  briefSearchQuery = "";
  selectedBriefId = "";
  briefPage = 1;
  byId("briefSearch").value = "";
  byId("briefEditionSelect").value = "";
  renderBriefResults();
}

async function openPaper(paperId) {
  currentPaper = await request(`/api/paper?id=${encodeURIComponent(paperId)}`);
  const paper = currentPaper;
  setText("paperType", itemTypeLabel(paper.item_type));
  setText("paperDate", paper.publication_date || "");
  setText("paperVenue", paper.venue || "");
  setText("paperTitle", paper.title);
  setText("paperValue", paper.value_reason);
  setText("paperMinutes", paper.estimated_minutes);
  setText("paperWordCount", paper.vocabulary.length);
  setText("paperTermCount", paper.terminology.length);
  byId("openSourceLink").href = paper.source_url;
  const paragraphs = String(paper.shadow_preview).split(/\n\s*\n/).filter(Boolean);
  byId("shadowPreview").replaceChildren(...paragraphs.map((text) => {
    const p = document.createElement("p"); p.textContent = text; return p;
  }));
  renderPaperVocabulary(paper);
  renderPaperTerminology(paper);
  showView("paper");
}

function renderPaperVocabulary(paper) {
  const container = byId("paperVocabulary");
  container.replaceChildren();
  setText("preheatStatus", paper.preheat_started ? "已经加入全局词表" : "尚未开始");
  byId("startPreheatButton").disabled = paper.preheat_started;
  byId("startPreheatButton").textContent = paper.preheat_started ? "已开始预热" : "开始预热并安排生词";
  for (const word of paper.vocabulary) {
    if (!word.meaning_en || !word.meaning_zh || !word.context) {
      throw new Error(`论文词汇 ${word.lemma || "未命名"} 缺少来源对齐的解释或语境`);
    }
    const row = document.createElement("article");
    row.className = `vocabulary-row${word.global_status === "mastered" ? " vocabulary-known" : ""}`;
    const head = document.createElement("div"); head.className = "vocabulary-row-head";
    const lemma = document.createElement("strong"); lemma.textContent = word.lemma;
    const pos = document.createElement("small"); pos.textContent = word.part_of_speech || word.kind || "";
    head.append(lemma, pos);
    const meaningEn = document.createElement("p"); meaningEn.className = "meaning-en"; meaningEn.textContent = word.meaning_en;
    const meaning = document.createElement("p"); meaning.textContent = word.meaning_zh;
    const context = document.createElement("div"); context.className = "vocabulary-context"; context.textContent = word.context;
    const actions = document.createElement("div"); actions.className = "vocabulary-row-actions";
    row.append(head, meaningEn, meaning, context);
    if (word.global_status !== "mastered") {
      const knownButton = document.createElement("button"); knownButton.className = "text-button"; knownButton.type = "button"; knownButton.textContent = "我已掌握，不再复习";
      knownButton.addEventListener("click", async () => {
        await request("/api/learning/mastered", { method: "POST", body: JSON.stringify({ item_type: "word", item_id: word.item_id, paper_id: paper.item_id }) });
        await openPaper(paper.item_id);
      });
      actions.appendChild(knownButton);
    } else {
      const restore = document.createElement("button"); restore.className = "text-button"; restore.type = "button"; restore.textContent = "恢复复习";
      restore.addEventListener("click", async () => {
        await request("/api/learning/restore", { method: "POST", body: JSON.stringify({ item_id: word.item_id }) });
        await openPaper(paper.item_id);
      });
      actions.appendChild(restore);
    }
    row.appendChild(actions);
    container.appendChild(row);
  }
}

function renderPaperTerminology(paper) {
  const container = byId("paperTerminology");
  container.replaceChildren();
  setText("paperTermSummary", `${paper.terminology.length} 个术语`);
  for (const term of paper.terminology) {
    const row = document.createElement("article");
    row.className = `vocabulary-row${term.global_status === "mastered" ? " vocabulary-known" : ""}`;
    const head = document.createElement("div"); head.className = "vocabulary-row-head";
    const name = document.createElement("strong"); name.textContent = term.term;
    const status = document.createElement("small"); status.textContent = termStatusLabel(term.global_status);
    head.append(name, status);
    const meaningEn = document.createElement("p"); meaningEn.className = "meaning-en"; meaningEn.textContent = term.meaning_en;
    const meaningZh = document.createElement("p"); meaningZh.textContent = term.meaning_zh;
    const context = document.createElement("div"); context.className = "vocabulary-context"; context.textContent = term.context;
    const actions = document.createElement("div"); actions.className = "vocabulary-row-actions";
    if (term.global_status === "new") {
      const understood = document.createElement("button"); understood.className = "text-button"; understood.type = "button"; understood.textContent = "我已经理解"; understood.addEventListener("click", () => updateTerm(term.item_id, "mastered"));
      const learn = document.createElement("button"); learn.className = "text-button"; learn.type = "button"; learn.textContent = "需要学习"; learn.addEventListener("click", () => updateTerm(term.item_id, "learning"));
      actions.append(understood, learn);
    } else if (term.global_status === "learning") {
      const mastered = document.createElement("button"); mastered.className = "text-button"; mastered.type = "button"; mastered.textContent = "我已理解，不再复习"; mastered.addEventListener("click", () => updateTerm(term.item_id, "mastered")); actions.appendChild(mastered);
    } else {
      const restore = document.createElement("button"); restore.className = "text-button"; restore.type = "button"; restore.textContent = "恢复复习"; restore.addEventListener("click", () => updateTerm(term.item_id, "restore")); actions.appendChild(restore);
    }
    row.append(head, meaningEn, meaningZh, context, actions);
    container.appendChild(row);
  }
}

function showReviewHub() {
  byId("reviewHub").hidden = false;
  byId("reviewSession").hidden = true;
  byId("termCheckSession").hidden = true;
  byId("termCheckComplete").hidden = true;
}

function renderDueReviewEntry(count) {
  if (count > 0) {
    setText("dueReviewHeadline", `${count} 个学习项目已经到期`);
    setText("dueReviewDescription", "这里包含已经进入学习计划并在今天到期的生词和术语。");
    byId("startDueReviewButton").disabled = false;
    byId("startDueReviewButton").textContent = "开始今日复习";
  } else {
    setText("dueReviewHeadline", "今天的到期复习已经完成");
    setText("dueReviewDescription", "现在没有到期内容。新的术语不会自动加入这里，除非你选择需要学习。");
    byId("startDueReviewButton").disabled = true;
    byId("startDueReviewButton").textContent = "暂无到期内容";
  }
}

function newTermCandidates() {
  const latestPaperIds = new Set(
    (appState?.briefs?.[0]?.items || []).map((item) => item.item_id),
  );
  return currentTerms
    .filter((term) => term.global_status === "new" && !termCheckSkippedThisVisit.has(term.item_id))
    .sort((left, right) => {
      const leftCurrent = left.source_paper_ids.some((id) => latestPaperIds.has(id)) ? 1 : 0;
      const rightCurrent = right.source_paper_ids.some((id) => latestPaperIds.has(id)) ? 1 : 0;
      return rightCurrent - leftCurrent
        || right.document_count - left.document_count
        || right.total_count - left.total_count
        || left.term.localeCompare(right.term);
    });
}

function renderTermDiscoveryCard() {
  const remaining = currentTerms.filter((term) => term.global_status === "new").length;
  const available = newTermCandidates().length;
  if (remaining === 0) {
    setText("termDiscoveryHeadline", "这个领域的术语已经全部确认");
    setText("termDiscoveryDescription", "你可以在领域词表中搜索术语，并随时恢复需要重新学习的内容。");
    byId("startTermCheckButton").disabled = true;
    byId("startTermCheckButton").textContent = "没有待确认术语";
    return;
  }
  setText("termDiscoveryHeadline", `还有 ${remaining} 个术语尚未确认`);
  setText("termDiscoveryDescription", "今天可以花一两分钟看几个。只有你选择“需要学习”的术语才会进入复习。");
  byId("startTermCheckButton").disabled = available === 0;
  byId("startTermCheckButton").textContent = available
    ? `看看今天的 ${Math.min(TERM_CHECK_BATCH_SIZE, available)} 个术语`
    : "这次已经看完可选术语";
}

function renderCurrentReviewItem() {
  currentReviewWord = dueReviewItems[0] || null;
  if (!currentReviewWord) {
    showReviewHub();
    return;
  }
  if (!currentReviewWord.context || !currentReviewWord.source_title) {
    throw new Error("这条复习记录缺少可核验的原文语境，已停止显示，请重新生成对应简报。");
  }
  byId("reviewHub").hidden = true;
  byId("reviewSession").hidden = false;
  byId("termCheckSession").hidden = true;
  byId("termCheckComplete").hidden = true;
  setText("reviewKind", currentReviewWord.item_type === "term" ? "领域术语" : "领域生词");
  setText("reviewWord", currentReviewWord.display_form);
  setText("reviewPos", currentReviewWord.part_of_speech || "");
  setText("reviewMeaningEn", currentReviewWord.meaning_en);
  setText("reviewMeaning", currentReviewWord.meaning_zh);
  setText("reviewContext", currentReviewWord.context);
  setText("reviewSource", `来源：${currentReviewWord.source_title} ↗`);
  byId("reviewSource").href = currentReviewWord.source_url;
  byId("masterReviewButton").textContent = currentReviewWord.item_type === "term" ? "我已理解，不再复习" : "我已掌握，不再复习";
  byId("reviewAnswer").hidden = true;
  byId("reviewRatings").hidden = true;
  byId("masterReviewButton").hidden = true;
  byId("revealReviewButton").hidden = false;
}

async function loadReview({ continueSession = false } = {}) {
  const data = await request("/api/review/due");
  dueReviewItems = data.words;
  updateNavDueCount(data.count);
  renderDueReviewEntry(data.count);
  renderTermDiscoveryCard();
  setText(
    "reviewSummary",
    data.count
      ? `今天有 ${data.count} 个学习项目到期，也可以顺便确认几个新术语。`
      : "今天没有到期内容，也可以顺便认识几个新术语。",
  );
  if (continueSession && data.count > 0) {
    renderCurrentReviewItem();
  } else {
    showReviewHub();
  }
  return data;
}

async function loadReviewPage() {
  await loadTerminology();
  await loadReview({ continueSession: false });
}

function renderTermCheckCurrent() {
  const term = termCheckBatch[termCheckIndex];
  if (!term) {
    showTermCheckComplete();
    return;
  }
  setText("termCheckProgress", `${termCheckIndex + 1} / ${termCheckBatch.length}`);
  setText("termCheckName", term.term);
  setText("termCheckMeaningEn", term.meaning_en);
  setText("termCheckMeaningZh", term.meaning_zh);
  setText("termCheckRole", term.concept_role);
  setText("termCheckContext", term.context);
  setText("termCheckSource", `来源：${term.source_title} ↗`);
  byId("termCheckSource").href = term.source_url;
}

function startTermCheck() {
  const candidates = newTermCandidates();
  if (!candidates.length) return;
  termCheckBatch = candidates.slice(0, TERM_CHECK_BATCH_SIZE);
  termCheckIndex = 0;
  termCheckResults = { mastered: 0, learning: 0, skipped: 0 };
  byId("reviewHub").hidden = true;
  byId("reviewSession").hidden = true;
  byId("termCheckComplete").hidden = true;
  byId("termCheckSession").hidden = false;
  renderTermCheckCurrent();
}

function showTermCheckComplete() {
  byId("reviewHub").hidden = true;
  byId("reviewSession").hidden = true;
  byId("termCheckSession").hidden = true;
  byId("termCheckComplete").hidden = false;
  setText(
    "termCheckResult",
    `${termCheckResults.learning} 个加入复习 · ${termCheckResults.mastered} 个已经掌握 · ${termCheckResults.skipped} 个这次跳过`,
  );
  const remaining = newTermCandidates().length;
  byId("checkMoreTermsButton").hidden = remaining === 0;
  byId("checkMoreTermsButton").textContent = `今天再看 ${Math.min(TERM_CHECK_BATCH_SIZE, remaining)} 个`;
}

async function answerTermCheck(action) {
  const term = termCheckBatch[termCheckIndex];
  if (!term || busy) return;
  busy = true;
  all("#termCheckSession button").forEach((button) => { button.disabled = true; });
  try {
    if (action === "mastered" || action === "learning") {
      await persistTermStatus(term.item_id, action);
      termCheckResults[action] += 1;
    } else {
      termCheckSkippedThisVisit.add(term.item_id);
      termCheckResults.skipped += 1;
    }
    termCheckIndex += 1;
    renderTermCheckCurrent();
  } catch (error) { showError(error); }
  finally {
    busy = false;
    all("#termCheckSession button").forEach((button) => { button.disabled = false; });
  }
}

function renderSchedule(settings) {
  if (!settings) return;
  byId("weeklyEnabled").checked = settings.weekly_brief.enabled;
  byId("weeklyWeekday").value = String(settings.weekly_brief.weekday);
  byId("weeklyTime").value = settings.weekly_brief.time;
  byId("reviewEnabled").checked = settings.daily_review.enabled;
  byId("reviewTime").value = settings.daily_review.time;
}

async function submitAnswer(response) {
  if (busy || !currentWord) return;
  busy = true;
  answerButtons.forEach((button) => { button.disabled = true; });
  try {
    const calibration = await request("/api/answer", { method: "POST", body: JSON.stringify({ lemma: currentWord, response }) });
    appState.calibration = calibration;
    if (renderCalibration(calibration)) showView("vocabulary");
  } catch (error) { showError(error); }
  finally { busy = false; answerButtons.forEach((button) => { button.disabled = false; }); }
}

function showError(error) {
  if (error instanceof StaleDomainResponse || error?.name === "AbortError") return;
  clearViews();
  byId("errorView").hidden = false;
  setText("errorMessage", error.message || String(error));
}

answerButtons.forEach((button) => button.addEventListener("click", () => submitAnswer(button.dataset.answer)));
document.addEventListener("keydown", (event) => {
  const mapping = { "1": "known", "2": "unsure", "3": "unknown" };
  if (!byId("questionView").hidden && mapping[event.key]) submitAnswer(mapping[event.key]);
});

all("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
byId("briefSearch").addEventListener("input", (event) => {
  briefSearchQuery = event.target.value.trim();
  briefPage = 1;
  renderBriefResults();
});
byId("briefEditionSelect").addEventListener("change", (event) => {
  selectedBriefId = event.target.value;
  briefPage = 1;
  renderBriefResults();
});
byId("briefClearFilters").addEventListener("click", clearBriefFilters);
byId("briefNoResultsClear").addEventListener("click", clearBriefFilters);
byId("briefPreviousPage").addEventListener("click", () => {
  briefPage = Math.max(1, briefPage - 1);
  renderBriefResults({ scroll: true });
});
byId("briefNextPage").addEventListener("click", () => {
  briefPage += 1;
  renderBriefResults({ scroll: true });
});
byId("wordTab").addEventListener("click", () => showVocabularySection("words"));
byId("termTab").addEventListener("click", () => showVocabularySection("terms"));
byId("termSearch").addEventListener("input", (event) => {
  termSearchQuery = event.target.value.trim();
  termPage = 1;
  renderTerminologyLibrary();
});
all("[data-term-filter]").forEach((button) => button.addEventListener("click", () => {
  termStatusFilter = button.dataset.termFilter;
  termPage = 1;
  renderTerminologyLibrary();
}));
byId("termPreviousPage").addEventListener("click", () => {
  termPage = Math.max(1, termPage - 1);
  renderTerminologyLibrary();
  byId("terminologyPanel").scrollIntoView({ block: "start", behavior: "smooth" });
});
byId("termNextPage").addEventListener("click", () => {
  termPage += 1;
  renderTerminologyLibrary();
  byId("terminologyPanel").scrollIntoView({ block: "start", behavior: "smooth" });
});
domainSelect.addEventListener("change", () => switchDomain(domainSelect.value));
byId("backToBriefs").addEventListener("click", () => showView("briefs"));
byId("startPreheatButton").addEventListener("click", async () => {
  if (!currentPaper || busy) return;
  busy = true; byId("startPreheatButton").disabled = true;
  try {
    const result = await request("/api/preheat/start", { method: "POST", body: JSON.stringify({ paper_id: currentPaper.item_id }) });
    currentPaper = result.paper;
    appState.continuous = result.continuous;
    renderPaperVocabulary(currentPaper);
    await loadReview();
  } catch (error) { showError(error); }
  finally { busy = false; }
});

byId("revealReviewButton").addEventListener("click", () => {
  byId("revealReviewButton").hidden = true;
  byId("reviewAnswer").hidden = false;
  byId("reviewRatings").hidden = false;
  byId("masterReviewButton").hidden = false;
});
byId("startDueReviewButton").addEventListener("click", () => {
  if (!dueReviewItems.length) return;
  renderCurrentReviewItem();
});
byId("startTermCheckButton").addEventListener("click", startTermCheck);
byId("termCheckMastered").addEventListener("click", () => answerTermCheck("mastered"));
byId("termCheckLearning").addEventListener("click", () => answerTermCheck("learning"));
byId("termCheckSkip").addEventListener("click", () => answerTermCheck("skipped"));
byId("exitTermCheckButton").addEventListener("click", () => loadReview({ continueSession: false }).catch(showError));
byId("checkMoreTermsButton").addEventListener("click", startTermCheck);
byId("finishTermCheckButton").addEventListener("click", () => loadReview({ continueSession: false }).catch(showError));
all("[data-rating]").forEach((button) => button.addEventListener("click", async () => {
  if (!currentReviewWord || busy) return;
  busy = true;
  all("[data-rating]").forEach((item) => { item.disabled = true; });
  try {
    await request("/api/review/answer", { method: "POST", body: JSON.stringify({ item_id: currentReviewWord.item_id, rating: button.dataset.rating }) });
    await loadReview({ continueSession: true });
  } catch (error) { showError(error); }
  finally { busy = false; all("[data-rating]").forEach((item) => { item.disabled = false; }); }
}));

byId("masterReviewButton").addEventListener("click", async () => {
  if (!currentReviewWord || busy) return;
  busy = true;
  try {
    await request("/api/learning/mastered", {
      method: "POST",
      body: JSON.stringify({ item_type: currentReviewWord.item_type, item_id: currentReviewWord.item_id }),
    });
    if (currentReviewWord.item_type === "term") {
      setLocalTermStatus(currentReviewWord.item_id, "mastered");
      renderTerminologyLibrary();
      renderTermDiscoveryCard();
    }
    await loadReview({ continueSession: true });
  } catch (error) { showError(error); }
  finally { busy = false; }
});

byId("resetButton").addEventListener("click", async () => {
  if (!window.confirm("确定要清空这次回答并重新开始吗？")) return;
  window.clearTimeout(thresholdTimer);
  thresholdRequestId += 1;
  const resetRevision = ++mutationRevision;
  try {
    const calibration = await request("/api/reset", {
      method: "POST",
      body: JSON.stringify({ mutation_revision: resetRevision }),
    });
    appState.calibration = calibration;
    renderCalibration(calibration);
  } catch (error) { showError(error); }
});

thresholdSlider.addEventListener("input", () => {
  const thresholdPercent = Number(thresholdSlider.value);
  const mutation = ++mutationRevision;
  thresholdValue.textContent = "重新计算…";
  thresholdEffect.textContent = "正在更新边界词和领域优先级…";
  resetThresholdButton.disabled = thresholdPercent === recommendedThreshold;
  window.clearTimeout(thresholdTimer);
  const requestId = ++thresholdRequestId;
  thresholdTimer = window.setTimeout(async () => {
    try {
      const calibration = await request("/api/threshold", {
        method: "POST",
        body: JSON.stringify({
          threshold_percent: thresholdPercent,
          mutation_revision: mutation,
        }),
      });
      if (requestId !== thresholdRequestId) return;
      appState.calibration = calibration;
      renderCalibration(calibration);
    } catch (error) { if (requestId === thresholdRequestId) showError(error); }
  }, 160);
});

resetThresholdButton.addEventListener("click", () => {
  thresholdSlider.value = String(recommendedThreshold);
  thresholdSlider.dispatchEvent(new Event("input"));
});

byId("weeklyScheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setText("weeklyScheduleStatus", "正在保存每周简报设置…");
  try {
    const payload = {
      section: "weekly_brief",
      settings: { enabled: byId("weeklyEnabled").checked, weekday: Number(byId("weeklyWeekday").value), time: byId("weeklyTime").value },
    };
    const result = await request("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    appState.settings = result.settings;
    byId("weeklyScheduleStatus").className = "saved";
    setText("weeklyScheduleStatus", "每周简报设置已保存。请让 Codex / Work Buddy 根据这一项设置创建或更新定时任务。");
    button.disabled = false;
  } catch (error) {
    button.disabled = false;
    showError(error);
  }
});

byId("reviewScheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setText("reviewScheduleStatus", "正在保存每日提醒设置…");
  try {
    const payload = {
      section: "daily_review",
      settings: { enabled: byId("reviewEnabled").checked, time: byId("reviewTime").value, only_when_due: true },
    };
    const result = await request("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    appState.settings = result.settings;
    byId("reviewScheduleStatus").className = "saved";
    setText("reviewScheduleStatus", "每日复习提醒设置已保存。请让 Codex / Work Buddy 根据这一项设置创建或更新提醒任务。");
    button.disabled = false;
  } catch (error) {
    button.disabled = false;
    showError(error);
  }
});

async function start() {
  const requestedDomain = new URL(window.location.href).searchParams.get("domain");
  const state = await request(
    "/api/app-state",
    {},
    { domainId: requestedDomain, generation: domainGeneration },
  );
  currentDomainId = state.domain_id;
  const requested = location.hash.replace(/^#/, "");
  const initial = views[requested] ? requested : state.initial_view;
  renderAppState(state, initial);
}

start().catch(showError);
