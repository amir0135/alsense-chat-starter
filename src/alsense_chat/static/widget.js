/* Session credentials live in memory only; they are never saved or added to chat messages. */
const $ = (id) => document.getElementById(id);
let token = "";
let user = null;
let draftKind = "";
let busy = false;
let cloudMode = false;
let lastMessage = "";
let reportTypes = null;
let replacingCard = null;
let lastReply = null;
let activeFlow = null;
const siteAlarms = new Map();
const actionCards = new WeakMap();
const DEFAULT_PLACEHOLDER = document.getElementById("message").placeholder;

const STATUS_LABELS = {
  answer: "Answer",
  clarification: "A question for you",
  not_in_data: "Not in the demo data",
  out_of_scope: "Outside this demo",
};
const TOOL_PROGRESS = {
  resolve_sites: "Checking the sites available in this session…",
  search_docs: "Looking through the operations guidance…",
  list_report_types: "Checking the available reports…",
  get_report_status: "Checking the report status…",
  check_site_health: "Checking the site’s health checks…",
  prepare_report: "Preparing the report for review…",
  prepare_support_ticket: "Preparing the support request for review…",
};
const ACTION_COPY = {
  report: {
    title: "Report ready for review", confirm: "Confirm synthetic report", retry: "Retry synthetic report",
    final: "Synthetic report ready", link: "Open synthetic report", progress: "Report progress",
  },
  ticket: {
    title: "Support request ready for review", confirm: "Confirm synthetic request", retry: "Retry synthetic request",
    final: "Synthetic request received", link: "Open synthetic record", progress: "Support request progress",
  },
};
const ACTION_STATES = {
  prepared: { badge: "Not sent", steps: ["is-done", "is-current", ""] },
  submitting: { badge: "Submitting…", steps: ["is-done", "is-busy", ""] },
  submitted: { badge: "Submitted · synthetic", steps: ["is-done", "is-done", "is-success"] },
  failed: { badge: "Not sent", steps: ["is-done", "is-error", ""] },
  replaced: { badge: "Replaced", steps: ["is-done", "", ""] },
};
const REPORT_NOTES = {
  "alarm-history": "Alarm events for the chosen period",
  "site-status": "Site status for the chosen period",
};
const DEFAULT_PERIOD = "last-30";
const PERIODS = {
  "last-7": (today) => [addDays(today, -6), today],
  "last-30": (today) => [addDays(today, -29), today],
  "this-month": (today) => [new Date(today.getFullYear(), today.getMonth(), 1), today],
  "last-month": (today) => [new Date(today.getFullYear(), today.getMonth() - 1, 1), new Date(today.getFullYear(), today.getMonth(), 0)],
};
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

function startOfToday() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function addDays(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

/* Local calendar dates; toISOString() would shift the day across time zones. */
function isoDate(date) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");
}

function parseIsoDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.getMonth() === Number(match[2]) - 1 && date.getDate() === Number(match[3]) ? date : null;
}

function dayNumber(date) {
  return Math.round(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
}

function formatPeriod(startValue, endValue) {
  const start = parseIsoDate(startValue);
  const end = parseIsoDate(endValue);
  if (!start || !end || dayNumber(start) > dayNumber(end)) return "";
  try {
    return dateFormat.formatRange(start, end);
  } catch {
    return `${dateFormat.format(start)} – ${dateFormat.format(end)}`;
  }
}

function periodLength(startValue, endValue) {
  const days = dayNumber(parseIsoDate(endValue)) - dayNumber(parseIsoDate(startValue)) + 1;
  return `${days} ${days === 1 ? "day" : "days"}`;
}

/* Mirrors the backend report window so problems surface before preparing. */
function periodProblem(startValue, endValue) {
  const start = parseIsoDate(startValue);
  const end = parseIsoDate(endValue);
  if (!start || !end) return "Choose a start and an end date.";
  if (dayNumber(start) > dayNumber(end)) return "The start date must be on or before the end date.";
  if (dayNumber(end) > dayNumber(startOfToday())) return "The period can’t end after today.";
  if (dayNumber(end) - dayNumber(start) > 366) return "Reports can cover up to 366 days. Choose a shorter period.";
  return "";
}

function matchPeriod(startValue, endValue) {
  const today = startOfToday();
  for (const [key, range] of Object.entries(PERIODS)) {
    const [start, end] = range(today);
    if (isoDate(start) === startValue && isoDate(end) === endValue) return key;
  }
  return "custom";
}

function siteName(siteId) {
  return user?.sites?.find((site) => site.id === siteId)?.name || "";
}

function reportTypeLabel(typeId) {
  return reportTypes?.find((item) => item.id === typeId)?.label
    || ({ "alarm-history": "Alarm history", "site-status": "Site status" })[typeId] || "Report";
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function clearError() {
  $("error").textContent = "";
  $("error").hidden = true;
}

function friendlyRequestError(status, path, detail) {
  if (status === 401 && path === "/api/demo/session") {
    return "That access code didn’t match. Use Show to check the paste, then copy the full code from the deployment computer.";
  }
  if (status === 401) return "This demo session has expired. Start a new session to continue.";
  if (status === 403) return "This demo session is unavailable. Check that the demo is enabled and try again.";
  if (status === 404) return "That draft is no longer available in this session. Prepare it again.";
  if (status >= 500) return "The assistant couldn’t complete that request. Please try again in a moment.";
  return detail || "The request couldn’t be completed. Please try again.";
}

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new Error("The demo service couldn’t be reached. Check your connection and try again.");
  }
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(friendlyRequestError(response.status, path, detail));
  }
  return response;
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function approvedLink(value) {
  if (typeof value !== "string" || !value.startsWith("/reference/") || value.startsWith("//") ||
      value.includes("\\") || /[\x00-\x1f]/.test(value)) return false;
  try {
    return new URL(value, location.origin).origin === location.origin;
  } catch {
    return false;
  }
}

function addLink(parent, label, href) {
  if (!approvedLink(href)) return;
  const anchor = node("a", "source-link", label);
  anchor.href = href;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  parent.append(anchor);
}

function renderInline(parent, text) {
  const segments = String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  for (const segment of segments) {
    if (segment.startsWith("**") && segment.endsWith("**")) {
      parent.append(node("strong", "", segment.slice(2, -2)));
    } else if (segment.startsWith("`") && segment.endsWith("`")) {
      parent.append(node("code", "", segment.slice(1, -1)));
    } else {
      parent.append(document.createTextNode(segment));
    }
  }
}

function textParagraphs(parent, text, className) {
  for (const block of String(text || "").split(/\n{2,}/).filter(Boolean)) {
    const lines = block.split("\n").filter(Boolean);
    const bullet = lines.length > 0 && lines.every((line) => /^\s*[-*]\s+/.test(line));
    const numbered = lines.length > 0 && lines.every((line) => /^\s*\d+[.)]\s+/.test(line));
    if (bullet || numbered) {
      const list = node(bullet ? "ul" : "ol", className);
      for (const line of lines) {
        const item = node("li");
        renderInline(item, line.replace(/^\s*(?:[-*]|\d+[.)])\s+/, ""));
        list.append(item);
      }
      parent.append(list);
    } else {
      const paragraph = node("p", className);
      renderInline(paragraph, block.replace(/\n/g, " "));
      parent.append(paragraph);
    }
  }
}

function renderSites(data, parent, text) {
  if (!Array.isArray(data) || data.length === 0) return;
  for (const site of data) {
    if (site?.id && Array.isArray(site.alarms)) {
      siteAlarms.set(site.id, site.alarms.filter((alarm) => alarm.status === "active").map((alarm) => alarm.label).filter(Boolean));
    }
  }
  const lowered = String(text || "").toLowerCase();
  const named = data.every((site) => site.name && lowered.includes(site.name.toLowerCase()));
  const alarms = data.flatMap((site) => (Array.isArray(site.alarms) ? site.alarms : []).map((alarm) => ({ site, alarm })));
  if (named && alarms.length === 0) return;
  const list = node("ul", "data-list");
  for (const site of data) {
    const item = node("li");
    item.append(node("strong", "", site.name || "Permitted site"));
    const active = Array.isArray(site.alarms) ? site.alarms.filter((alarm) => alarm.status === "active") : [];
    item.append(node("span", active.length ? "alarm-badge" : "", active.length
      ? active.map((alarm) => alarm.label).join(", ")
      : "No active alarms"));
    list.append(item);
  }
  parent.append(list);
}

function renderReportTypes(data, parent) {
  if (!Array.isArray(data) || data.length === 0) return;
  const list = node("ul", "data-list");
  for (const report of data) {
    const item = node("li");
    item.append(node("strong", "", report.label || "Report"));
    const formats = Array.isArray(report.formats) ? report.formats.map((format) => format.toUpperCase()).join(", ") : "";
    if (formats) item.append(node("span", "", formats));
    list.append(item);
  }
  parent.append(list);
}

function renderDocuments(data, parent) {
  if (!Array.isArray(data) || data.length === 0) return;
  const row = node("div", "source-chips");
  row.append(node("span", "source-chips-label", "Source"));
  for (const document of data) {
    const title = document.title || "Operations guidance";
    if (approvedLink(document.url)) addLink(row, title, document.url);
    else row.append(node("span", "source-link", title));
  }
  parent.append(row);
}

function renderStatus(data, parent) {
  const state = typeof data?.state === "string" ? data.state.replaceAll("_", " ") : "Status unavailable";
  parent.append(node("p", "source-excerpt", `Report status: ${state}.`));
  if (data?.result?.url) addLink(parent, "Open synthetic report view", data.result.url);
}

function renderHealth(data, parent) {
  if (!Array.isArray(data)) return;
  parent.append(node("p", "evidence-caption", "Health checks"));
  const list = node("ul", "data-list");
  const open = data.filter((check) => check.status === "open");
  if (open.length === 0) list.append(node("li", "", "No open health checks"));
  for (const check of open) {
    const item = node("li");
    item.append(node("strong", "", check.check || "Health check"), node("span", "alarm-badge", "Open"));
    list.append(item);
  }
  parent.append(list);
}

function renderEvidence(reply, parent) {
  const sources = Array.isArray(reply.sources) ? reply.sources : [];
  for (const source of sources) {
    const content = node("div", "answer-data");
    switch (source.tool) {
      case "resolve_sites":
        renderSites(source.data, content, reply.text);
        break;
      case "list_report_types":
        renderReportTypes(source.data, content);
        break;
      case "search_docs":
        renderDocuments(source.data, content);
        break;
      case "get_report_status":
        renderStatus(source.data, content);
        break;
      case "check_site_health":
        renderHealth(source.data, content);
        break;
      default:
        break;
    }
    if (content.childElementCount) parent.append(content);
  }
}

function renderFollowUps(reply, parent) {
  if ((reply.actions || []).length || !user || reply.status === "out_of_scope") return;
  const tools = new Set((reply.sources || []).map((source) => source.tool));
  const options = [];
  if (tools.has("search_docs") || reply.status === "not_in_data") {
    options.push(["Prepare support request", "ticket"]);
  }
  if (tools.has("search_docs") || tools.has("resolve_sites") || tools.has("list_report_types")) {
    options.push(["Prepare a report", "report"]);
  }
  if (options.length === 0) return;
  const row = node("div", "follow-ups");
  for (const [label, kind] of options) {
    const button = node("button", "follow-up", label);
    button.type = "button";
    button.addEventListener("click", () => openDraft(kind).catch((error) => showError(error.message)));
    row.append(button);
  }
  parent.append(row);
}

function draftRequestLabel(kind, payload) {
  const site = siteName(payload.site_id) || "your permitted site";
  if (kind === "report") {
    const type = reportTypeLabel(payload.report_type).toLowerCase();
    const article = /^[aeiou]/.test(type) ? "an" : "a";
    return `Prepare ${article} ${type} report for ${site} · ${formatPeriod(payload.start, payload.end)} · ${payload.format.toUpperCase()}`;
  }
  return `Prepare a support request for ${site}: ${payload.summary}`;
}

function actionRows(kind, payload) {
  const site = siteName(payload.site_id) || "Permitted site";
  if (kind === "report") {
    const period = formatPeriod(payload.start, payload.end);
    return [
      ["Site", site],
      ["Report", reportTypeLabel(payload.report_type)],
      ["Period", period ? `${period} · ${periodLength(payload.start, payload.end)}` : ""],
      ["Format", String(payload.format || "").toUpperCase()],
    ];
  }
  return [["Site", site], ["Alarm", payload.alarm_id], ["Summary", payload.summary], ["Steps tried", payload.steps_tried]];
}

function renderAction(action, parent) {
  const kind = action.kind === "report" ? "report" : "ticket";
  const copy = ACTION_COPY[kind];
  const payload = action.payload || {};
  const card = node("section", "action-card");
  const head = node("div", "action-head");
  const titles = node("div");
  titles.append(node("h3", "", copy.title), node("p", "", "Nothing has been sent. Confirm below to submit this synthetic demo action."));
  const badge = node("span", "action-badge");
  head.append(titles, badge);

  const steps = node("ol", "action-steps");
  steps.setAttribute("aria-label", copy.progress);
  const stepItems = [];
  const stepLabels = [];
  for (const label of ["Prepared", "Confirm", copy.final]) {
    const item = node("li");
    const dot = node("span", "step-dot");
    dot.setAttribute("aria-hidden", "true");
    const text = node("span", "", label);
    item.append(dot, text);
    steps.append(item);
    stepItems.push(item);
    stepLabels.push(text);
  }

  const details = node("dl", "action-details");
  for (const [label, value] of actionRows(kind, payload)) {
    if (value) details.append(node("dt", "", label), node("dd", "", value));
  }

  const buttons = node("div", "action-buttons");
  const confirm = node("button", "confirm-action", copy.confirm);
  confirm.type = "button";
  const edit = node("button", "secondary-action", "Edit details");
  edit.type = "button";
  buttons.append(confirm, edit);
  const state = node("p", "submission-state");
  state.setAttribute("role", "status");
  state.hidden = true;

  const setState = (name) => {
    const config = ACTION_STATES[name];
    card.dataset.state = name;
    badge.textContent = config.badge;
    stepItems.forEach((item, index) => {
      item.className = config.steps[index];
      if (config.steps[index] === "is-current" || config.steps[index] === "is-busy") item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    stepLabels[1].textContent = name === "submitted" ? "Confirmed" : "Confirm";
  };
  setState("prepared");

  confirm.addEventListener("click", async () => {
    setState("submitting");
    confirm.disabled = true;
    confirm.classList.add("is-busy");
    confirm.replaceChildren(node("span", "button-spinner"), document.createTextNode("Submitting…"));
    edit.disabled = true;
    state.hidden = true;
    state.classList.remove("failed");
    try {
      const result = await (await api(`/api/actions/${encodeURIComponent(action.id)}/submit`, { confirmed: true })).json();
      setState("submitted");
      state.textContent = "Submitted to the synthetic demo. No real report or support request was sent.";
      state.hidden = false;
      buttons.replaceChildren();
      if (approvedLink(result.url)) {
        const open = node("a", "secondary-action", copy.link);
        const arrow = node("span", "", "↗");
        arrow.setAttribute("aria-hidden", "true");
        open.append(arrow);
        open.href = result.url;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        buttons.append(open);
      }
      if (kind === "report") {
        const status = node("button", "secondary-action", "Check status");
        status.type = "button";
        status.addEventListener("click", () => {
          send(`/status ${action.id}`, "Check the status of this report").catch((error) => showError(error.message));
        });
        buttons.append(status);
      }
    } catch (error) {
      setState("failed");
      state.textContent = error.message;
      state.classList.add("failed");
      state.hidden = false;
      confirm.disabled = false;
      confirm.classList.remove("is-busy");
      confirm.textContent = copy.retry;
      edit.disabled = false;
    }
  });
  edit.addEventListener("click", () => {
    openDraft(kind, payload).then(() => { replacingCard = card; }).catch((error) => showError(error.message));
  });
  actionCards.set(card, {
    replace() {
      if (card.dataset.state === "submitted" || card.dataset.state === "submitting") return;
      setState("replaced");
      buttons.replaceChildren();
      state.classList.remove("failed");
      state.textContent = "Replaced by your edited draft below. This one won’t be sent.";
      state.hidden = false;
    },
  });
  card.append(head, steps, details, buttons, state);
  parent.append(card);
}

function flowMeta(flow) {
  const meta = node("p", "flow-meta");
  const glyph = node("span", "flow-glyph", "⌁");
  glyph.setAttribute("aria-hidden", "true");
  meta.append(glyph, document.createTextNode(
    `Guided check · ${flow.title} · ${flow.kind === "outcome" ? "Result" : `Step ${flow.step}`}`));
  return meta;
}

function startFlow(id, label) {
  send(`/flow ${id} []`, label).catch((error) => showError(error.message));
}

function answerFlow(flow, value, label, button) {
  if (busy) {
    showError("Wait for the current answer to finish.");
    return;
  }
  if (button) {
    button.closest(".flow-options")?.querySelectorAll("button").forEach((item) => { item.disabled = true; });
    button.setAttribute("aria-pressed", "true");
  }
  send(`/flow ${flow.id} ${JSON.stringify([...(flow.answers || []), value])}`, label)
    .catch((error) => showError(error.message));
}

/* Only the newest step can be answered; the composer answers steps that ask for a site description. */
function setActiveFlow(flow) {
  const waiting = Boolean(flow && ["question", "confirm", "site"].includes(flow.kind));
  activeFlow = waiting ? { id: flow.id, answers: flow.answers || [], expectsText: flow.kind === "site" } : null;
  const groups = [...document.querySelectorAll(".flow-options")];
  for (const group of waiting ? groups.slice(0, -1) : groups) {
    group.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  }
  $("flow-hint").hidden = !activeFlow?.expectsText;
  $("message").placeholder = activeFlow?.expectsText ? "Company and site name…" : DEFAULT_PLACEHOLDER;
}

function renderFlowMenu(flow, parent) {
  const menu = node("div", "flow-menu");
  for (const option of flow.options || []) {
    const card = node("button", "flow-card");
    card.type = "button";
    const copy = node("span", "flow-card-copy");
    copy.append(node("strong", "", option.label));
    if (option.description) copy.append(node("small", "", option.description));
    const arrow = node("span", "suggestion-arrow", "↗");
    arrow.setAttribute("aria-hidden", "true");
    card.append(copy, arrow);
    card.addEventListener("click", () => startFlow(option.value, option.label));
    menu.append(card);
  }
  parent.append(menu);
}

function renderFlowResult(flow, parent) {
  const ticket = flow.outcome === "ticket";
  const result = node("section", "flow-result");
  result.dataset.kind = ticket ? "ticket" : "resolve";
  result.append(node("p", "flow-result-label", ticket ? "Needs a support request" : "Next step for you"));
  if (flow.trail?.length) {
    result.append(node("h3", "", "What we checked"));
    const list = node("ol", "flow-trail");
    for (const item of flow.trail) {
      const row = node("li");
      row.append(node("span", "", item.label), node("strong", "", item.answer));
      list.append(row);
    }
    result.append(list);
  }
  const buttons = node("div", "action-buttons");
  if (ticket) {
    const support = node("button", "confirm-action", "Prepare support request");
    support.type = "button";
    support.addEventListener("click", () => {
      openDraft("ticket", flow.ticket, { fromFlow: true }).catch((error) => showError(error.message));
    });
    buttons.append(support);
  }
  const restart = node("button", "secondary-action", "Start over");
  restart.type = "button";
  restart.addEventListener("click", () => startFlow(flow.id, "Start over"));
  buttons.append(restart);
  result.append(buttons);
  parent.append(result);
}

function renderFlow(flow, parent) {
  if (flow.kind === "menu") {
    renderFlowMenu(flow, parent);
    return;
  }
  if (flow.kind === "outcome") {
    renderFlowResult(flow, parent);
    return;
  }
  const group = node("div", "flow-options");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", flow.kind === "site" ? "Your sites" : "Your answer");
  for (const option of flow.options || []) {
    const button = node("button", flow.kind === "site" ? "flow-option site-option" : "flow-option");
    button.type = "button";
    if (flow.kind === "site") {
      const glyph = node("span", "site-glyph", "⌖");
      glyph.setAttribute("aria-hidden", "true");
      button.append(glyph);
    }
    button.append(document.createTextNode(option.label));
    button.addEventListener("click", () => answerFlow(flow, option.value, option.label, button));
    group.append(button);
  }
  parent.append(group);
}

function renderReply(reply) {
  lastReply = reply;
  const flow = reply.flow || null;
  const article = node("article", "turn assistant-turn");
  article.append(node("span", "assistant-mark", "A"));
  const body = node("div", "assistant-body");
  const heading = node("div", "answer-heading");
  heading.append(node("strong", "", "Operations assistant"));
  heading.append(node("span", `answer-status ${reply.status || "answer"}`, STATUS_LABELS[reply.status] || "Answer"));
  body.append(heading);
  if (flow && flow.kind !== "menu") body.append(flowMeta(flow));
  textParagraphs(body, reply.text, "answer-text");
  if (flow) {
    const cited = new Set(reply.source_ids || []);
    renderEvidence({ ...reply, sources: (reply.sources || []).filter((source) => cited.has(source.source_id)) }, body);
    renderFlow(flow, body);
  } else {
    renderEvidence(reply, body);
    for (const action of reply.actions || []) renderAction(action, body);
    renderFollowUps(reply, body);
  }
  article.append(body);
  $("messages").append(article);
  setActiveFlow(flow);
  scrollToLatest();
}

function scrollToLatest() {
  requestAnimationFrame(() => {
    $("conversation").scrollTop = $("conversation").scrollHeight;
  });
}

function setProgress(container, message) {
  const row = container.querySelector(".progress-row");
  const label = row?.querySelector("span:last-child");
  if (label) label.textContent = message;
}

function renderFailure(container, message, retryText, retryDisplayText) {
  container.replaceChildren();
  const heading = node("div", "answer-heading");
  heading.append(node("strong", "", "The answer couldn’t be completed"));
  container.append(heading, node("p", "answer-text", message));
  if (retryText) {
    const retry = node("button", "retry-button", "Try again");
    retry.type = "button";
    retry.addEventListener("click", () => send(retryText, retryDisplayText).catch(showError));
    container.append(retry);
  }
}

async function send(message, displayText = message) {
  const questionText = message.trim();
  if (!token) throw new Error("Start a synthetic demo session before asking a question.");
  if (busy) throw new Error("Wait for the current answer to finish.");
  if (!questionText) return;
  if (!/^\/flows?(\s|$)/.test(questionText)) setActiveFlow(null);
  busy = true;
  lastMessage = questionText;
  clearError();
  updateSessionUI();
  $("welcome").hidden = true;
  $("send").disabled = true;
  document.querySelectorAll("[data-message]").forEach((button) => { button.disabled = true; });

  const question = node("article", "turn user-turn");
  question.append(node("p", "user-bubble", displayText));
  $("messages").append(question);
  const assistant = node("article", "turn assistant-turn");
  assistant.append(node("span", "assistant-mark", "A"));
  const body = node("div", "assistant-body");
  const progress = node("div", "progress-row");
  progress.append(node("span", "progress-indicator"), node("span", "", "Thinking through your question…"));
  body.append(progress);
  assistant.append(body);
  $("messages").append(assistant);
  scrollToLatest();

  try {
    const response = await api("/api/chat", { message: questionText });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let receivedReply = false;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      for (const frame of frames) {
        const lines = frame.split("\n");
        const eventName = lines.find((line) => line.startsWith("event: "))?.slice(7);
        const dataLine = lines.filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (!dataLine) continue;
        const data = JSON.parse(dataLine);
        if (eventName === "step") setProgress(assistant, TOOL_PROGRESS[data.tool] || "Checking the available information…");
        if (eventName === "reply") {
          receivedReply = true;
          assistant.remove();
          renderReply(data);
        }
        if (eventName === "error") throw new Error("The assistant couldn’t complete that answer. Please try again.");
      }
      if (done) break;
    }
    if (!receivedReply) throw new Error("The answer stream ended early. Please try again.");
    $("message").value = "";
    $("message").style.height = "auto";
    return true;
  } catch (error) {
    renderFailure(body, error.message, questionText, displayText);
    scrollToLatest();
    return false;
  } finally {
    busy = false;
    updateSessionUI();
  }
}

function updateSessionUI() {
  const active = Boolean(token && user);
  $("session-status").textContent = active ? "Active" : "Not started";
  $("session-status").classList.toggle("is-active", active);
  $("identity").textContent = active ? `${user.name} · ${user.sites.map((site) => site.name).join(", ")}` : "";
  $("identity").hidden = !active;
  $("active-user").textContent = active ? user.name : "";
  $("active-user").hidden = !active;
  $("login").textContent = active ? "Switch user" : "Start demo session";
  $("persona").disabled = active;
  $("access-code-field").hidden = active || !cloudMode;
  $("new-conversation").disabled = !active || busy;
  $("login").disabled = busy;
  $("message").disabled = !active;
  $("send").disabled = !active || busy;
  $("open-report").disabled = !active || busy;
  $("open-ticket").disabled = !active || busy;
  document.querySelectorAll("[data-message], #suggest-report").forEach((button) => { button.disabled = !active || busy; });
  $("welcome-hint").textContent = active
    ? `Signed in as ${user.name}. Your questions stay scoped to ${user.sites.map((site) => site.name).join(", ")}.`
    : "Start a synthetic session in the left panel to begin.";
}

async function startSession() {
  $("login").disabled = true;
  $("login").textContent = "Starting session…";
  clearError();
  $("session-error").hidden = true;
  try {
    const session = await (await api("/api/demo/session", {
      persona: $("persona").value,
      access_code: $("access-code").value.trim(),
    })).json();
    token = session.token;
    user = session.user;
    reportTypes = null;
    siteAlarms.clear();
    $("access-code").value = "";
    $("welcome").hidden = false;
    $("messages").replaceChildren();
    updateSessionUI();
    $("message").focus();
  } catch (error) {
    $("session-error").textContent = error.message;
    $("session-error").hidden = false;
    $("login").textContent = "Try again";
  } finally {
    $("login").disabled = false;
  }
}

function switchUser() {
  if (busy) return;
  token = "";
  user = null;
  reportTypes = null;
  replacingCard = null;
  siteAlarms.clear();
  $("messages").replaceChildren();
  setActiveFlow(null);
  $("welcome").hidden = false;
  $("draft-panel").hidden = true;
  $("draft-form").reset();
  clearError();
  $("session-error").hidden = true;
  updateSessionUI();
}

async function newConversation() {
  if (!token || busy) return;
  $("new-conversation").disabled = true;
  clearError();
  try {
    const session = await (await api("/api/session/new", {})).json();
    token = session.token;
    user = session.user;
    replacingCard = null;
    siteAlarms.clear();
    $("messages").replaceChildren();
    setActiveFlow(null);
    $("welcome").hidden = false;
    $("draft-panel").hidden = true;
    $("draft-form").reset();
    updateSessionUI();
    $("message").focus();
  } catch (error) {
    showError(error.message);
  } finally {
    $("new-conversation").disabled = !token;
  }
}

function renderSiteField(selectedId) {
  const select = $("draft-site");
  select.replaceChildren();
  for (const site of user.sites) select.add(new Option(site.name, site.id));
  if (user.sites.some((site) => site.id === selectedId)) select.value = selectedId;
  const single = user.sites.length === 1;
  const pill = $("site-fixed");
  select.hidden = single;
  pill.hidden = !single;
  pill.replaceChildren();
  if (!single) return;
  const glyph = node("span", "site-glyph", "⌖");
  glyph.setAttribute("aria-hidden", "true");
  pill.append(glyph, node("span", "", user.sites[0].name));
  for (const label of siteAlarms.get(user.sites[0].id) || []) pill.append(node("span", "alarm-chip", label));
}

function renderReportChoices(selectedId) {
  const container = $("report-type-options");
  const selected = reportTypes.some((item) => item.id === selectedId) ? selectedId : reportTypes[0]?.id;
  container.replaceChildren();
  for (const report of reportTypes) {
    const card = node("label", "choice-card");
    const input = node("input");
    input.type = "radio";
    input.name = "report_type";
    input.value = report.id;
    input.checked = report.id === selected;
    const mark = node("span", "choice-mark");
    mark.setAttribute("aria-hidden", "true");
    const copy = node("span", "choice-copy");
    copy.append(node("strong", "", report.label || "Report"));
    const formats = Array.isArray(report.formats) ? report.formats.map((format) => format.toUpperCase()).join(" or ") : "";
    const note = node("small");
    if (REPORT_NOTES[report.id]) note.append(document.createTextNode(`${REPORT_NOTES[report.id]}${formats ? " · " : ""}`));
    if (formats) note.append(node("span", "nowrap", formats));
    if (note.childNodes.length) copy.append(note);
    card.append(input, mark, copy);
    container.append(card);
  }
}

function syncFormats() {
  const form = $("draft-form");
  const typeId = form.querySelector('input[name="report_type"]:checked')?.value;
  const allowed = reportTypes?.find((item) => item.id === typeId)?.formats || ["pdf", "csv"];
  const inputs = [...form.querySelectorAll('input[name="format"]')];
  for (const input of inputs) input.disabled = !allowed.includes(input.value);
  if (!inputs.some((input) => input.checked && !input.disabled)) {
    const first = inputs.find((input) => !input.disabled);
    if (first) first.checked = true;
  }
}

function applyPeriod(key, { focusCustom = false } = {}) {
  const form = $("draft-form");
  const custom = key === "custom";
  if (!custom) {
    const [start, end] = PERIODS[key](startOfToday());
    form.elements.start.value = isoDate(start);
    form.elements.end.value = isoDate(end);
  }
  $("custom-range").hidden = !custom;
  if (custom && focusCustom) form.elements.start.focus();
}

function readReportPayload() {
  const form = $("draft-form");
  return {
    site_id: form.elements.site_id.value,
    report_type: form.querySelector('input[name="report_type"]:checked')?.value || "",
    format: form.querySelector('input[name="format"]:checked')?.value || "",
    start: form.elements.start.value,
    end: form.elements.end.value,
  };
}

function setSummary(text, animate) {
  const summary = $("draft-summary-text");
  if (summary.textContent === text) return;
  summary.textContent = text;
  if (!animate) return;
  summary.classList.remove("is-updated");
  void summary.offsetWidth;
  summary.classList.add("is-updated");
}

function updateReportBuilder({ animate = false } = {}) {
  const form = $("draft-form");
  const payload = readReportPayload();
  const problem = periodProblem(payload.start, payload.end);
  const custom = form.elements.period.value === "custom";
  const readout = $("range-readout");
  readout.replaceChildren();
  if (!problem) {
    readout.append(node("strong", "", formatPeriod(payload.start, payload.end)),
      document.createTextNode(` · ${periodLength(payload.start, payload.end)}`));
  }
  readout.hidden = Boolean(problem);
  $("range-error").textContent = problem;
  $("range-error").hidden = !problem;
  for (const input of [form.elements.start, form.elements.end]) {
    if (problem && custom) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }
  const parts = [reportTypeLabel(payload.report_type), siteName(payload.site_id)];
  parts.push(...(problem ? ["Check the period"] : [formatPeriod(payload.start, payload.end), payload.format.toUpperCase()]));
  setSummary(parts.filter(Boolean).join(" · "), animate);
  $("prepare-draft").disabled = Boolean(problem) || !payload.report_type || !payload.format;
}

function closeDraft() {
  $("draft-panel").hidden = true;
  replacingCard = null;
  if (!$("message").disabled) $("message").focus();
}

async function openDraft(kind, prefill = null, { fromFlow = false } = {}) {
  if (!user) throw new Error("Start a synthetic demo session first.");
  if (busy) throw new Error("Wait for the current answer to finish.");
  clearError();
  if (kind === "report" && !reportTypes) reportTypes = await (await api("/api/report-types")).json();
  draftKind = kind;
  replacingCard = null;
  const panel = $("draft-panel");
  const form = $("draft-form");
  panel.dataset.kind = kind;
  form.reset();
  renderSiteField(prefill?.site_id);
  const reportFields = $("report-fields");
  const ticketFields = $("ticket-fields");
  reportFields.hidden = kind !== "report";
  ticketFields.hidden = kind !== "ticket";
  for (const input of reportFields.querySelectorAll("input")) input.disabled = kind !== "report";
  for (const input of ticketFields.querySelectorAll("input, textarea")) {
    input.disabled = kind !== "ticket";
    input.required = kind === "ticket" && input.name !== "alarm_id";
  }
  const multipleSites = user.sites.length > 1;
  if (kind === "report") {
    renderReportChoices(prefill?.report_type);
    syncFormats();
    for (const input of form.querySelectorAll('input[name="format"]')) {
      if (input.value === prefill?.format && !input.disabled) input.checked = true;
    }
    const period = prefill ? matchPeriod(prefill.start, prefill.end) : DEFAULT_PERIOD;
    form.elements.period.value = period;
    if (period === "custom") {
      form.elements.start.value = prefill.start;
      form.elements.end.value = prefill.end;
    }
    applyPeriod(period);
    $("draft-title").textContent = prefill ? "Edit report details" : "Prepare a report";
    $("draft-description").textContent = multipleSites
      ? "Choose the site, report, period and format for your draft."
      : "Choose the report, period and format for your draft.";
    $("draft-summary-icon").textContent = "▤";
    updateReportBuilder();
  } else {
    $("draft-title").textContent = prefill && !fromFlow ? "Edit support request" : "Prepare a support request";
    $("draft-description").textContent = fromFlow
      ? "Filled in from your guided check. Review it before preparing."
      : "Describe the issue and what you’ve already tried.";
    form.elements.alarm_id.value = prefill ? prefill.alarm_id || "" : `alarm-${form.elements.site_id.value}`;
    form.elements.summary.value = prefill?.summary || "";
    form.elements.steps_tried.value = prefill?.steps_tried || "";
    $("draft-summary-icon").textContent = "↗";
    setSummary(`Support request for ${siteName(form.elements.site_id.value)}`, false);
    $("prepare-draft").disabled = false;
  }
  panel.hidden = false;
  const focusTarget = multipleSites ? form.elements.site_id
    : kind === "report" ? form.querySelector('input[name="report_type"]:checked') : form.elements.summary;
  focusTarget?.focus();
  scrollToLatest();
}

$("login").addEventListener("click", () => {
  if (token) switchUser();
  else startSession();
});
$("new-conversation").addEventListener("click", newConversation);
$("toggle-code").addEventListener("click", () => {
  const input = $("access-code");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  $("toggle-code").textContent = visible ? "Hide" : "Show";
  $("toggle-code").setAttribute("aria-label", visible ? "Hide access code" : "Show access code");
  $("toggle-code").setAttribute("aria-pressed", String(visible));
});
$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("message").value.trim();
  if (activeFlow?.expectsText && text) {
    if (text.length > 200) showError("Keep the company and site name under 200 characters.");
    else answerFlow(activeFlow, text, text);
    return;
  }
  send($("message").value).catch((error) => showError(error.message));
});
$("flow-cancel").addEventListener("click", () => {
  setActiveFlow(null);
  $("message").focus();
});
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("message").addEventListener("input", () => {
  const input = $("message");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
});
document.querySelectorAll("[data-message]").forEach((button) => {
  button.addEventListener("click", () => {
    send(button.dataset.message, button.dataset.display || button.dataset.message)
      .catch((error) => showError(error.message));
  });
});
$("open-report").addEventListener("click", () => openDraft("report").catch((error) => showError(error.message)));
$("open-ticket").addEventListener("click", () => openDraft("ticket").catch((error) => showError(error.message)));
$("suggest-report").addEventListener("click", () => openDraft("report").catch((error) => showError(error.message)));
$("cancel-draft").addEventListener("click", closeDraft);
$("draft-panel").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !event.defaultPrevented) {
    event.preventDefault();
    closeDraft();
  }
});
$("draft-form").addEventListener("change", (event) => {
  const { name, value } = event.target;
  const form = $("draft-form");
  if (name === "site_id" && draftKind === "ticket") {
    if (form.elements.alarm_id.value) form.elements.alarm_id.value = `alarm-${value}`;
    setSummary(`Support request for ${siteName(value)}`, true);
  }
  if (draftKind !== "report") return;
  if (name === "period") applyPeriod(value, { focusCustom: value === "custom" });
  if (name === "report_type") syncFormats();
  updateReportBuilder({ animate: true });
});
$("draft-form").addEventListener("input", (event) => {
  if (draftKind === "report" && (event.target.name === "start" || event.target.name === "end")) updateReportBuilder();
});
$("draft-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  let payload;
  if (draftKind === "report") {
    payload = readReportPayload();
    updateReportBuilder();
    if (periodProblem(payload.start, payload.end) || !payload.report_type || !payload.format) {
      if (form.elements.period.value === "custom") form.elements.start.focus();
      return;
    }
  } else {
    if (!form.reportValidity()) return;
    payload = {
      site_id: form.elements.site_id.value,
      summary: form.elements.summary.value,
      steps_tried: form.elements.steps_tried.value,
    };
    const alarm = form.elements.alarm_id.value.trim();
    if (alarm) payload.alarm_id = alarm;
  }
  const displayText = draftRequestLabel(draftKind, payload);
  const button = $("prepare-draft");
  const replaced = replacingCard;
  clearError();
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const completed = await send(`/${draftKind} ${JSON.stringify(payload)}`, displayText);
    if (completed) {
      $("draft-panel").hidden = true;
      form.reset();
      replacingCard = null;
      if (replaced && lastReply?.actions?.length) actionCards.get(replaced)?.replace();
    }
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Prepare for review";
  }
});

fetch("/api/health")
  .then((response) => {
    if (!response.ok) throw new Error("Health check failed.");
    return response.json();
  })
  .then((health) => {
    cloudMode = Boolean(health.access_code_required);
    $("access-code-field").hidden = !cloudMode;
    $("model-state").textContent = health.mode === "foundry" ? "AI assistant ready" : "Demo assistant ready";
    if (!cloudMode) $("code-help").textContent = "Local demo session · no access code needed.";
  })
  .catch(() => {
    $("model-state").textContent = "Service unavailable";
    showError("The demo service couldn’t be reached. Start the service or check the hosted demo connection.");
  });

updateSessionUI();
