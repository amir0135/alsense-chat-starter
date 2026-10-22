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
let lastFlowResult = null;
let features = new Set(["reports", "troubleshooting", "support"]);
let scenarios = [];
let scenariosState = "loading";
let activeScenario = null;
let sessionScenario = "";
const siteAlarms = new Map();
const actionCards = new WeakMap();
const replyChoices = new WeakMap();
const DEFAULT_PLACEHOLDER = document.getElementById("message").placeholder;

const STATUS_LABELS = {
  answer: "Answer",
  clarification: "A question for you",
  not_in_data: "Not in the demo data",
  out_of_scope: "Outside this demo",
  error: "Couldn’t complete",
};
/* Failures worth handing to a person: a support request can carry what was tried. */
const SUPPORT_CATEGORIES = new Set(["unavailable", "timeout", "stale", "not_found"]);
const REPORT_STATES = {
  prepared: "Not confirmed yet",
  processing: "Still being prepared",
  completed: "Ready (synthetic)",
  failed: "Failed",
  scheduled: "Scheduled (synthetic)",
};
const TOOL_PROGRESS = {
  get_permitted_sites: "Checking the sites you can access…",
  search_docs: "Looking through the operations guidance…",
  list_report_types: "Checking the available reports…",
  get_report_status: "Checking the report status…",
  check_site_health: "Checking the site’s health checks…",
  prepare_report: "Preparing the report for review…",
  prepare_scheduled_report: "Preparing the schedule for review…",
  prepare_support_ticket: "Preparing the support request for review…",
};
const ACTION_COPY = {
  report: {
    title: "Report ready for review", confirm: "Confirm synthetic report", retry: "Retry synthetic report",
    final: "Synthetic report ready", link: "Open synthetic report", progress: "Report progress",
    pending: "Nothing has been sent. Confirm below to create this synthetic report.",
    done: "Submitted · synthetic", sent: "Submitted to the synthetic demo. No real report was created.",
  },
  scheduled_report: {
    title: "Schedule ready for review", confirm: "Confirm synthetic schedule", retry: "Retry synthetic schedule",
    final: "Schedule active", link: "Open synthetic schedule", progress: "Schedule progress",
    pending: "Nothing is scheduled yet. Confirm below to create this synthetic schedule.",
    done: "Scheduled · synthetic", sent: "Scheduled in the synthetic demo. No real report will be sent.",
  },
  ticket: {
    title: "Support request ready for review", confirm: "Confirm synthetic request", retry: "Retry synthetic request",
    final: "Synthetic request received", link: "Open synthetic record", progress: "Support request progress",
    pending: "Nothing has been sent. Confirm below to submit this synthetic demo action.",
    done: "Submitted · synthetic", sent: "Submitted to the synthetic demo. No real support request was sent.",
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
  "temperature-log": "Temperature readings",
  "alarm-history": "Alarm events",
  "site-status": "Site status",
};
const FORMAT_LABELS = { pdf: "PDF", csv: "CSV", xlsx: "Excel" };
const FREQUENCY_LABELS = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
const EMAIL = /^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
const STATUS_POLLS = 8;
const STATUS_POLL_MS = 1500;
const DEFAULT_PERIOD = "last-30";
const PERIODS = {
  "last-7": (today) => [addDays(today, -6), today],
  "last-30": (today) => [addDays(today, -29), today],
  "this-month": (today) => [new Date(today.getFullYear(), today.getMonth(), 1), today],
  "last-month": (today) => [new Date(today.getFullYear(), today.getMonth() - 1, 1), new Date(today.getFullYear(), today.getMonth(), 0)],
};
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
});

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
    || ({ "temperature-log": "Temperature log", "alarm-history": "Alarm history", "site-status": "Site status" })[typeId]
    || "Report";
}

function formatLabel(format) {
  return FORMAT_LABELS[format] || String(format || "").toUpperCase();
}

function dayLabel(value) {
  const date = parseIsoDate(value);
  return date ? dateFormat.format(date) : "";
}

function parseRecipients(text) {
  const items = String(text || "").split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean);
  const recipients = [...new Map(items.map((item) => [item.toLowerCase(), item])).values()];
  return { recipients, invalid: recipients.filter((item) => !EMAIL.test(item)) };
}

/* The service sets the expiry; the widget only shows it, so a different lifetime upstream shows correctly. */
function linkExpiry(result) {
  const ready = new Date(result?.completed_at);
  const expires = new Date(result?.expires_at);
  if (Number.isNaN(ready.getTime()) || Number.isNaN(expires.getTime())) {
    return { expired: false, text: "The download link expires after a limited time." };
  }
  if (expires.getTime() <= Date.now()) {
    return { expired: true, text: `This link expired on ${dateTimeFormat.format(expires)}. Prepare the report again for a new link.` };
  }
  const hours = Math.round((expires - ready) / 3600000);
  const lifetime = hours === 1 ? "1 hour" : `${hours} hours`;
  return { expired: false, text: `Expires ${lifetime} after the report was ready · ${dateTimeFormat.format(expires)}` };
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function clearError() {
  $("error").textContent = "";
  $("error").hidden = true;
}

/* Server details are the backend's own user-safe wording, so they are shown when they explain more. */
function friendlyRequestError(status, path, detail) {
  const scenarioStart = path.startsWith("/api/demo/scenarios/");
  if (status === 401 && (path === "/api/demo/session" || scenarioStart)) {
    return "That access code didn’t match. Use Show to check the paste, then copy the full code from the deployment computer.";
  }
  if (status === 401) return "This demo session has expired. Start a new session to continue.";
  if (status === 403) return detail || "This demo session is unavailable. Check that the demo is enabled and try again.";
  if (status === 404) {
    return scenarioStart ? "That scenario isn’t available." : "That draft is no longer available in this session. Prepare it again.";
  }
  if (status === 409) return detail || "That scenario can’t run with this configuration.";
  if ([501, 503, 504].includes(status) && detail) return detail;
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
    /* Categorized integration failures carry the backend's own user-safe wording: show it as is, so an
       upstream 401 or 404 is never mistaken for an expired session or a missing draft. */
    if (detail && response.headers.get("X-Error-Category")) throw new Error(detail);
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

function renderSites(result, parent, text) {
  const data = Array.isArray(result) ? result : Array.isArray(result?.sites) ? result.sites : [];
  if (data.length === 0) return;
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
    const formats = Array.isArray(report.formats) ? report.formats.map(formatLabel).join(", ") : "";
    const frequencies = Array.isArray(report.schedule?.frequencies)
      ? report.schedule.frequencies.map((value) => FREQUENCY_LABELS[value] || value).join(", ") : "";
    const schedule = frequencies ? `Schedule: ${frequencies.toLowerCase()}` : "On demand only";
    item.append(node("span", "", [formats, schedule].filter(Boolean).join(" · ")));
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

/* The confirmed result: a download link with its expiry, a schedule's first run, or why there is none. */
function renderReportResult(container, status, { live = false } = {}) {
  container.querySelector(".report-result")?.remove();
  const box = node("div", "report-result");
  const result = status?.result || {};
  if (status?.state === "completed") {
    const expiry = linkExpiry(result);
    if (approvedLink(result.url) && !expiry.expired) {
      const link = node("a", "download-link");
      const glyph = node("span", "download-glyph", "↓");
      glyph.setAttribute("aria-hidden", "true");
      link.append(glyph, document.createTextNode("Download synthetic report"));
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      box.append(link);
    }
    box.append(node("p", expiry.expired ? "download-note is-expired" : "download-note", expiry.text));
  } else if (status?.state === "processing") {
    const note = node("p", "download-note is-pending");
    if (live) note.append(node("span", "inline-spinner"));
    note.append(document.createTextNode(live
      ? "Preparing the report. The download link appears here when it’s ready."
      : "Still being prepared. Check again in a moment for the download link."));
    box.append(note);
  } else if (status?.state === "failed") {
    box.append(node("p", "download-note is-failed", "The report job failed, so there’s no file to download."));
  } else if (status?.state === "scheduled") {
    const first = dayLabel(result.next_run);
    box.append(node("p", "download-note", first ? `First report on ${first}.` : "The schedule is active."));
    if (approvedLink(result.url)) addLink(box, "Open synthetic schedule", result.url);
  } else {
    return;
  }
  container.append(box);
}

function renderStatus(data, parent) {
  const state = typeof data?.state === "string" ? data.state : "";
  const subject = data?.kind === "scheduled_report" ? "Schedule" : "Report";
  parent.append(node("p", "source-excerpt", `${subject} status: ${REPORT_STATES[state] || "unavailable"}.`));
  if (typeof data?.result?.message === "string") parent.append(node("p", "source-excerpt", data.result.message));
  renderReportResult(parent, data);
}

async function reportStatus(actionId) {
  return (await api(`/api/actions/${encodeURIComponent(actionId)}/status`)).json();
}

/* Shows the result as soon as the service has it; a job still running is checked again a few times. */
async function followReport(actionId, container) {
  for (let attempt = 0; attempt < STATUS_POLLS; attempt += 1) {
    const status = await reportStatus(actionId);
    const last = attempt === STATUS_POLLS - 1;
    renderReportResult(container, status, { live: status.state === "processing" && !last });
    if (status.state !== "processing" || !container.isConnected) return status;
    if (!last) await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
  }
  return null;
}

function renderHealth(data, parent) {
  const checks = Array.isArray(data?.checks) ? data.checks : null;
  if (!checks) return;
  parent.append(node("p", "evidence-caption", "Health checks"));
  const list = node("ul", "data-list");
  const open = checks.filter((check) => check.status === "open");
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
  /* Lookups behind a draft or a question are summarized by the card or the choices; "Why this result?" lists them. */
  const quiet = (reply.actions || []).length > 0 || reply.status === "clarification";
  for (const source of sources) {
    const content = node("div", "answer-data");
    switch (source.tool) {
      case "get_permitted_sites":
        if (!quiet) renderSites(source.data, content, reply.text);
        break;
      case "list_report_types":
        if (!quiet) renderReportTypes(source.data, content);
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

/* Answers to a clarification. Each carries the complete request, so choosing one needs no hidden state. */
function renderChoices(reply, parent) {
  const choices = Array.isArray(reply.choices) ? reply.choices.filter((item) => item?.label && item?.message) : [];
  if (choices.length === 0) return;
  const sites = reply.asks_for === "site";
  const group = node("div", "reply-choices");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", sites ? "Your sites" : "Your answer");
  for (const choice of choices) {
    const button = node("button", sites ? "flow-option site-option" : "flow-option");
    button.type = "button";
    if (sites) {
      const glyph = node("span", "site-glyph", "⌖");
      glyph.setAttribute("aria-hidden", "true");
      button.append(glyph);
    }
    button.append(document.createTextNode(choice.label));
    replyChoices.set(button, choice);
    button.addEventListener("click", () => chooseReply(choice, button));
    group.append(button);
  }
  parent.append(group);
}

function chooseReply(choice, button) {
  if (busy) {
    showError("Wait for the current answer to finish.");
    return Promise.resolve(false);
  }
  button?.closest(".reply-choices")?.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  button?.setAttribute("aria-pressed", "true");
  return send(choice.message, choice.label).catch((error) => { showError(error.message); return false; });
}

/* Details that are easier to give in the form, such as recipients, open the builder with the rest filled in. */
function renderBuilderPrompt(reply, parent) {
  const prefill = reply.builder;
  if (!prefill || typeof prefill !== "object" || !features.has("reports")) return;
  const scheduled = prefill.mode === "scheduled";
  const row = node("div", "follow-ups");
  const label = reply.asks_for === "recipients" ? "Add recipients in the form" : "Finish in the report form";
  const button = node("button", "follow-up", label);
  button.type = "button";
  button.addEventListener("click", () => {
    openDraft(scheduled ? "scheduled_report" : "report", prefill, {
      fresh: true, focus: reply.asks_for === "recipients" ? "recipients" : "",
    }).catch((error) => showError(error.message));
  });
  row.append(button);
  parent.append(row);
}

function renderFollowUps(reply, parent) {
  if ((reply.actions || []).length || !user || reply.status === "out_of_scope") return;
  if ((reply.choices || []).length || reply.builder) return;
  const tools = new Set((reply.sources || []).map((source) => source.tool));
  const options = [];
  const failed = reply.status === "error";
  if (features.has("support") && (failed ? SUPPORT_CATEGORIES.has(reply.error?.category)
    : tools.has("search_docs") || reply.status === "not_in_data")) {
    options.push(["Prepare support request", "ticket"]);
  }
  if (!failed && features.has("reports")
    && (tools.has("search_docs") || tools.has("get_permitted_sites") || tools.has("list_report_types"))) {
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

function scheduleSpan(payload) {
  const frequency = FREQUENCY_LABELS[payload.frequency] || "";
  const start = dayLabel(payload.start_date);
  const end = dayLabel(payload.end_date);
  if (!frequency || !start) return "";
  return `${frequency} from ${start}${end ? ` until ${end}` : ""}`;
}

function recipientCount(count) {
  return `${count} ${count === 1 ? "recipient" : "recipients"}`;
}

function draftRequestLabel(kind, payload) {
  const site = siteName(payload.site_id) || "your permitted site";
  const type = reportTypeLabel(payload.report_type).toLowerCase();
  const article = /^[aeiou]/.test(type) ? "an" : "a";
  if (kind === "report") {
    return `Prepare ${article} ${type} report for ${site} · ${formatPeriod(payload.start, payload.end)} · ${formatLabel(payload.format)}`;
  }
  if (kind === "scheduled_report") {
    const count = recipientCount((payload.recipients || []).length);
    const span = scheduleSpan(payload).replace(/^\w+/, (word) => word.toLowerCase());
    return `Schedule ${article} ${type} report for ${site} · ${span} · ${formatLabel(payload.format)} · ${count}`;
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
      ["Format", formatLabel(payload.format)],
    ];
  }
  if (kind === "scheduled_report") {
    return [
      ["Site", site],
      ["Report", reportTypeLabel(payload.report_type)],
      ["Repeats", scheduleSpan(payload) + (payload.end_date ? "" : " · no end date")],
      ["Format", formatLabel(payload.format)],
      ["Send to", (payload.recipients || []).join("\n")],
    ];
  }
  return [["Site", site], ["Alarm", payload.alarm_id], ["Summary", payload.summary], ["Steps tried", payload.steps_tried]];
}

function renderAction(action, parent) {
  const kind = ACTION_COPY[action.kind] ? action.kind : "ticket";
  const copy = ACTION_COPY[kind];
  const reporting = kind !== "ticket";
  const payload = action.payload || {};
  const card = node("section", "action-card");
  const head = node("div", "action-head");
  const titles = node("div");
  const subtitle = node("p", "", copy.pending);
  titles.append(node("h3", "", copy.title), subtitle);
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
    badge.textContent = name === "submitted" ? copy.done : config.badge;
    subtitle.textContent = name === "submitted" ? copy.sent : copy.pending;
    stepItems.forEach((item, index) => {
      item.className = config.steps[index];
      if (config.steps[index] === "is-current" || config.steps[index] === "is-busy") item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    stepLabels[1].textContent = name === "submitted" ? "Confirmed" : "Confirm";
  };
  setState("prepared");

  const checkStatus = () => send(`/status ${action.id}`,
    kind === "scheduled_report" ? "Check the status of this schedule" : "Check the status of this report");
  const result = node("div", "card-result");

  /* Returns whether the synthetic submission succeeded; failures stay visible on the card. */
  const confirmAction = async () => {
    if (card.dataset.state !== "prepared" && card.dataset.state !== "failed") return false;
    setState("submitting");
    confirm.disabled = true;
    confirm.classList.add("is-busy");
    confirm.replaceChildren(node("span", "button-spinner"), document.createTextNode("Submitting…"));
    edit.disabled = true;
    state.hidden = true;
    state.classList.remove("failed");
    try {
      const submission = await (await api(`/api/actions/${encodeURIComponent(action.id)}/submit`, { confirmed: true })).json();
      setState("submitted");
      state.hidden = true;
      buttons.replaceChildren();
      if (!reporting && approvedLink(submission.url)) {
        const open = node("a", "secondary-action", copy.link);
        const arrow = node("span", "", "↗");
        arrow.setAttribute("aria-hidden", "true");
        open.append(arrow);
        open.href = submission.url;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        buttons.append(open);
      }
      if (reporting) {
        const status = node("button", "secondary-action", "Check status");
        status.type = "button";
        status.addEventListener("click", () => checkStatus().catch((error) => showError(error.message)));
        buttons.append(status);
        if (kind === "scheduled_report") {
          renderReportResult(result, { state: "scheduled", result: submission });
        } else {
          /* The link is the point of the report: fetch it right away instead of waiting for "Check status". */
          followReport(action.id, result).catch((error) => {
            result.replaceChildren(node("p", "download-note is-failed",
              `${error.message} Use Check status to try again.`));
          });
        }
      }
      return true;
    } catch (error) {
      setState("failed");
      state.textContent = error.message;
      state.classList.add("failed");
      state.hidden = false;
      confirm.disabled = false;
      confirm.classList.remove("is-busy");
      confirm.textContent = copy.retry;
      edit.disabled = false;
      return false;
    }
  };
  const editAction = async () => {
    await openDraft(kind, payload);
    replacingCard = card;
    return true;
  };
  confirm.addEventListener("click", confirmAction);
  edit.addEventListener("click", () => editAction().catch((error) => showError(error.message)));
  actionCards.set(card, {
    replace() {
      if (card.dataset.state === "submitted" || card.dataset.state === "submitting") return;
      setState("replaced");
      buttons.replaceChildren();
      state.classList.remove("failed");
      state.textContent = "Replaced by your edited draft below. This one won’t be sent.";
      state.hidden = false;
    },
    confirm: confirmAction,
    edit: editAction,
    status: checkStatus,
  });
  card.append(head, steps, details, result, buttons, state);
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
    return Promise.resolve(false);
  }
  if (button) {
    button.closest(".flow-options")?.querySelectorAll("button").forEach((item) => { item.disabled = true; });
    button.setAttribute("aria-pressed", "true");
  } else {
    /* Typed or scripted answers highlight the matching option, so the trail reads the same either way. */
    const options = [...document.querySelectorAll(".flow-options")].pop();
    const match = [...(options?.querySelectorAll("button") || [])].find((item) => item.dataset.value === value);
    if (match) match.setAttribute("aria-pressed", "true");
  }
  return send(`/flow ${flow.id} ${JSON.stringify([...(flow.answers || []), value])}`, label)
    .catch((error) => { showError(error.message); return false; });
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
  lastFlowResult = flow;
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
  if (ticket && features.has("support")) {
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
    button.dataset.value = option.value;
    button.addEventListener("click", () => answerFlow(flow, option.value, option.label, button));
    group.append(button);
  }
  parent.append(group);
}

/* User-safe evidence assembled by the backend: display names, versions and outcomes, never raw ids. */
function renderWhy(reply, parent) {
  const rows = Array.isArray(reply.why) ? reply.why.filter((row) => row?.label && row?.value) : [];
  if (rows.length === 0) return;
  const details = node("details", "why");
  details.append(node("summary", "", "Why this result?"));
  const list = node("dl", "why-list");
  for (const row of rows) list.append(node("dt", "", row.label), node("dd", "", row.value));
  details.append(list);
  parent.append(details);
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
    renderChoices(reply, body);
    renderBuilderPrompt(reply, body);
    renderFollowUps(reply, body);
  }
  renderWhy(reply, body);
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
  /* Only the newest question can be answered with its buttons. */
  document.querySelectorAll("#messages .reply-choices button").forEach((button) => { button.disabled = true; });
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
  /* Scenario sessions keep their synthetic faults, so say so wherever the identity is shown. */
  const scenarioTitle = sessionScenario
    ? scenarios.find((item) => item.id === sessionScenario)?.title || "Demo scenario" : "";
  $("identity").textContent = active
    ? `${user.name} · ${user.sites.map((site) => site.name).join(", ")}${scenarioTitle ? ` · Scenario: ${scenarioTitle}` : ""}`
    : "";
  $("identity").hidden = !active;
  $("active-user").textContent = active ? `${user.name}${sessionScenario ? " · scenario" : ""}` : "";
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
  document.querySelectorAll("[data-feature]").forEach((item) => { item.hidden = !features.has(item.dataset.feature); });
  $("open-scenarios").disabled = busy;
  $("welcome-scenarios").disabled = busy;
  renderCoach();
  $("welcome-hint").textContent = active
    ? `Signed in as ${user.name}. Your questions stay scoped to ${user.sites.map((site) => site.name).join(", ")}.`
    : "Start a synthetic session in the left panel to begin.";
}

/* A fresh session always starts from a clean conversation. */
function applySession(session) {
  token = session.token;
  user = session.user;
  sessionScenario = session.scenario || "";
  reportTypes = null;
  replacingCard = null;
  lastFlowResult = null;
  siteAlarms.clear();
  $("access-code").value = "";
  $("messages").replaceChildren();
  setActiveFlow(null);
  $("draft-panel").hidden = true;
  $("draft-form").reset();
  closeScenarios();
  $("welcome").hidden = false;
  if ([...$("persona").options].some((option) => option.value === user.id)) $("persona").value = user.id;
  updateSessionUI();
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
    activeScenario = null;
    applySession(session);
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
  activeScenario = null;
  sessionScenario = "";
  lastFlowResult = null;
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
    /* The backend keeps the scenario's setup, so its walkthrough restarts from the first step. */
    if (activeScenario) activeScenario.index = 0;
    applySession(session);
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
    const formats = Array.isArray(report.formats) ? report.formats.map(formatLabel).join(", ") : "";
    const note = node("small");
    if (REPORT_NOTES[report.id]) note.append(document.createTextNode(`${REPORT_NOTES[report.id]}${formats ? " · " : ""}`));
    if (formats) note.append(node("span", "nowrap", formats));
    if (!report.schedule) note.append(node("span", "schedule-note", " · On demand only"));
    if (note.childNodes.length) copy.append(note);
    card.append(input, mark, copy);
    container.append(card);
  }
}

function reportMode() {
  return $("draft-form").querySelector('input[name="mode"]:checked')?.value === "scheduled" ? "scheduled" : "on-demand";
}

function selectedReportType() {
  const typeId = $("draft-form").querySelector('input[name="report_type"]:checked')?.value;
  return reportTypes?.find((item) => item.id === typeId) || null;
}

/* Scheduled mode offers only report types the service can schedule, with their frequencies and end date. */
function applyMode() {
  const form = $("draft-form");
  const scheduled = reportMode() === "scheduled";
  $("report-fields").dataset.mode = scheduled ? "scheduled" : "on-demand";
  $("period-row").hidden = scheduled;
  $("schedule-fields").hidden = !scheduled;
  for (const input of $("period-row").querySelectorAll("input")) input.disabled = scheduled;
  const types = [...form.querySelectorAll('input[name="report_type"]')];
  for (const input of types) {
    input.disabled = scheduled && !reportTypes.find((item) => item.id === input.value)?.schedule;
  }
  if (!types.some((input) => input.checked && !input.disabled)) {
    const first = types.find((input) => !input.disabled);
    if (first) first.checked = true;
  }
  syncFormats();
  syncSchedule();
}

function syncSchedule() {
  const form = $("draft-form");
  const scheduled = reportMode() === "scheduled";
  const schedule = selectedReportType()?.schedule;
  for (const input of $("schedule-fields").querySelectorAll("input")) input.disabled = !scheduled;
  const frequencies = [...form.querySelectorAll('input[name="frequency"]')];
  for (const input of frequencies) input.disabled = !scheduled || !schedule?.frequencies?.includes(input.value);
  if (!frequencies.some((input) => input.checked && !input.disabled)) {
    const first = frequencies.find((input) => !input.disabled);
    if (first) first.checked = true;
  }
  const endDate = Boolean(schedule?.end_date_supported);
  $("end-date-field").hidden = !endDate;
  $("end-date-help").hidden = !endDate;
  form.elements.end_date.disabled = !scheduled || !endDate;
  if (!endDate) form.elements.end_date.value = "";
  const today = isoDate(startOfToday());
  if (scheduled && !form.elements.start_date.value) form.elements.start_date.value = isoDate(addDays(startOfToday(), 1));
  form.elements.start_date.min = today;
  form.elements.end_date.min = form.elements.start_date.value || today;
}

function syncFormats() {
  const form = $("draft-form");
  const typeId = form.querySelector('input[name="report_type"]:checked')?.value;
  const allowed = reportTypes?.find((item) => item.id === typeId)?.formats || ["pdf", "csv", "xlsx"];
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

function readSchedulePayload() {
  const form = $("draft-form");
  const payload = {
    site_id: form.elements.site_id.value,
    report_type: form.querySelector('input[name="report_type"]:checked')?.value || "",
    format: form.querySelector('input[name="format"]:checked')?.value || "",
    frequency: form.querySelector('input[name="frequency"]:checked:not(:disabled)')?.value || "",
    recipients: parseRecipients(form.elements.recipients.value).recipients,
    start_date: form.elements.start_date.value,
  };
  if (!form.elements.end_date.disabled && form.elements.end_date.value) payload.end_date = form.elements.end_date.value;
  return payload;
}

/* Mirrors the backend's schedule checks so problems surface before preparing. */
function scheduleProblems(payload) {
  const { recipients, invalid } = parseRecipients($("draft-form").elements.recipients.value);
  const limit = selectedReportType()?.schedule?.max_recipients || 25;
  let people = "";
  if (recipients.length === 0) people = "Add at least one email address.";
  else if (invalid.length) people = `Check “${invalid[0]}”. It isn’t a complete email address.`;
  else if (recipients.length > limit) people = `This report can go to up to ${limit} recipients.`;
  let dates = "";
  const start = parseIsoDate(payload.start_date);
  const end = payload.end_date ? parseIsoDate(payload.end_date) : null;
  if (!start) dates = "Choose the date of the first report.";
  else if (dayNumber(start) < dayNumber(startOfToday())) dates = "The first report can’t be in the past.";
  else if (payload.end_date && (!end || dayNumber(end) < dayNumber(start))) dates = "“Until” must be on or after the first report.";
  return { people, dates };
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

function updateScheduleBuilder(animate) {
  const form = $("draft-form");
  const payload = readSchedulePayload();
  const { people, dates } = scheduleProblems(payload);
  /* An empty list is explained in the summary; a list with a mistake is explained at the field. */
  const showPeople = Boolean(people) && Boolean(form.elements.recipients.value.trim());
  $("recipients-error").textContent = showPeople ? people : "";
  $("recipients-error").hidden = !showPeople;
  if (showPeople) form.elements.recipients.setAttribute("aria-invalid", "true");
  else form.elements.recipients.removeAttribute("aria-invalid");
  $("schedule-error").textContent = dates;
  $("schedule-error").hidden = !dates;
  for (const input of [form.elements.start_date, form.elements.end_date]) {
    if (dates) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }
  const span = scheduleSpan(payload);
  $("schedule-readout").replaceChildren();
  if (!dates && span) {
    $("schedule-readout").append(node("strong", "", span), document.createTextNode(payload.end_date ? "" : " · no end date"));
  }
  $("schedule-readout").hidden = Boolean(dates) || !span;
  const parts = [reportTypeLabel(payload.report_type), siteName(payload.site_id)];
  parts.push(dates ? "Check the dates" : span, formatLabel(payload.format));
  parts.push(people ? (payload.recipients.length ? "Check the recipients" : "Add recipients") : recipientCount(payload.recipients.length));
  setSummary(parts.filter(Boolean).join(" · "), animate);
  $("prepare-draft").disabled = Boolean(people || dates) || !payload.report_type || !payload.format || !payload.frequency;
}

function updateReportBuilder({ animate = false } = {}) {
  if (reportMode() === "scheduled") {
    updateScheduleBuilder(animate);
    return;
  }
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

async function openDraft(requested, prefill = null, { fromFlow = false, fresh = false, focus = "" } = {}) {
  if (!user) throw new Error("Start a synthetic demo session first.");
  if (busy) throw new Error("Wait for the current answer to finish.");
  clearError();
  const scheduled = requested === "scheduled_report" || prefill?.mode === "scheduled";
  const kind = requested === "ticket" ? "ticket" : "report";
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
    form.elements.mode.value = scheduled ? "scheduled" : "on-demand";
    renderReportChoices(prefill?.report_type);
    applyMode();
    for (const input of form.querySelectorAll('input[name="format"]')) {
      if (input.value === prefill?.format && !input.disabled) input.checked = true;
    }
    if (scheduled) {
      for (const input of form.querySelectorAll('input[name="frequency"]')) {
        if (input.value === prefill?.frequency && !input.disabled) input.checked = true;
      }
      form.elements.recipients.value = (prefill?.recipients || []).join(", ");
      form.elements.start_date.value = prefill?.start_date || isoDate(addDays(startOfToday(), 1));
      if (!form.elements.end_date.disabled) form.elements.end_date.value = prefill?.end_date || "";
      syncSchedule();
      applyPeriod(DEFAULT_PERIOD);
    } else {
      const period = prefill?.start ? matchPeriod(prefill.start, prefill.end) : DEFAULT_PERIOD;
      form.elements.period.value = period;
      if (period === "custom") {
        form.elements.start.value = prefill.start;
        form.elements.end.value = prefill.end;
      }
      applyPeriod(period);
    }
    const editing = prefill && !fresh;
    $("draft-title").textContent = scheduled
      ? (editing ? "Edit schedule details" : "Schedule a report")
      : (editing ? "Edit report details" : "Prepare a report");
    $("draft-description").textContent = multipleSites
      ? "Choose the site, report and delivery for your draft."
      : "Choose the report and its delivery for your draft.";
    $("draft-summary-icon").textContent = "▤";
    updateReportBuilder();
  } else {
    $("draft-title").textContent = prefill && !fromFlow && !fresh ? "Edit support request" : "Prepare a support request";
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
  const focusTarget = focus === "recipients" ? form.elements.recipients
    : multipleSites ? form.elements.site_id
      : kind === "report" ? form.querySelector('input[name="report_type"]:checked') : form.elements.summary;
  focusTarget?.focus();
  scrollToLatest();
}

$("open-scenarios").addEventListener("click", () => openScenarios());
$("welcome-scenarios").addEventListener("click", () => openScenarios());
$("close-scenarios").addEventListener("click", closeScenarios);
$("scenario-gallery").addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeScenarios();
});
$("coach-run").addEventListener("click", runCoachStep);
$("coach-reset").addEventListener("click", () => {
  if (activeScenario) launchScenario(activeScenario.scenario.id);
});
$("coach-exit").addEventListener("click", () => {
  activeScenario = null;
  renderCoach();
  $("message").focus();
});
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
  if (name === "mode") applyMode();
  if (name === "period") applyPeriod(value, { focusCustom: value === "custom" });
  if (name === "report_type") {
    syncFormats();
    syncSchedule();
  }
  if (name === "start_date") syncSchedule();
  updateReportBuilder({ animate: true });
});
$("draft-form").addEventListener("input", (event) => {
  if (draftKind === "report" && ["start", "end", "recipients", "start_date", "end_date"].includes(event.target.name)) {
    updateReportBuilder();
  }
});
/* Returns whether a draft was prepared. Builder problems stay inline and nothing is sent. */
async function prepareDraft() {
  const form = $("draft-form");
  if ($("draft-panel").hidden) throw new Error("Open the report or support form first.");
  let payload;
  let kind = draftKind;
  if (draftKind === "report" && reportMode() === "scheduled") {
    kind = "scheduled_report";
    payload = readSchedulePayload();
    updateReportBuilder();
    const { people, dates } = scheduleProblems(payload);
    if (people || dates || !payload.report_type || !payload.format || !payload.frequency) {
      (people ? form.elements.recipients : form.elements.start_date).focus();
      return false;
    }
  } else if (draftKind === "report") {
    payload = readReportPayload();
    updateReportBuilder();
    if (periodProblem(payload.start, payload.end) || !payload.report_type || !payload.format) {
      if (form.elements.period.value === "custom") form.elements.start.focus();
      return false;
    }
  } else {
    if (!form.reportValidity()) return false;
    payload = {
      site_id: form.elements.site_id.value,
      summary: form.elements.summary.value,
      steps_tried: form.elements.steps_tried.value,
    };
    const alarm = form.elements.alarm_id.value.trim();
    if (alarm) payload.alarm_id = alarm;
  }
  const displayText = draftRequestLabel(kind, payload);
  const command = { report: "report", scheduled_report: "schedule", ticket: "ticket" }[kind];
  const button = $("prepare-draft");
  const replaced = replacingCard;
  clearError();
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const completed = await send(`/${command} ${JSON.stringify(payload)}`, displayText);
    if (completed) {
      $("draft-panel").hidden = true;
      form.reset();
      replacingCard = null;
      if (replaced && lastReply?.actions?.length) actionCards.get(replaced)?.replace();
    }
    return completed;
  } finally {
    button.disabled = false;
    button.textContent = "Prepare for review";
  }
}

$("draft-form").addEventListener("submit", (event) => {
  event.preventDefault();
  prepareDraft().catch((error) => showError(error.message));
});

/* Demo scenarios: each starts a fresh synthetic session as the right user; the coach runs its steps. */
const SCENARIO_GROUPS = [["journey", "Core journeys"], ["failure", "Failure cases"]];

async function loadScenarios() {
  try {
    const response = await fetch("/api/scenarios");
    if (!response.ok) throw new Error("unavailable");
    scenarios = await response.json();
    scenariosState = "ready";
  } catch {
    scenariosState = "error";
    throw new Error("Demo scenarios couldn’t be loaded. Check that the demo service is running, then try again.");
  }
}

async function loadPersonas() {
  const response = await fetch("/api/demo/personas");
  if (!response.ok) return;
  const personas = await response.json();
  if (!Array.isArray(personas) || personas.length === 0) return;
  const select = $("persona");
  const current = select.value;
  select.replaceChildren(...personas.map((persona) => new Option(
    `${String(persona.name).replace(/^Synthetic /, "")} · ${(persona.sites || []).join(", ")}`, persona.id)));
  if (personas.some((persona) => persona.id === current)) select.value = current;
}

function scenarioRow(scenario) {
  const running = Boolean(token && activeScenario?.scenario.id === scenario.id);
  const row = node("li", "scenario-row");
  row.dataset.state = scenario.unavailable_reason ? "unavailable" : running ? "running" : "ready";
  const main = node("div", "scenario-main");
  const title = node("h3", "", scenario.title);
  if (running) title.append(node("span", "scenario-badge", "Running"));
  main.append(title, node("p", "scenario-summary", scenario.demonstrates));
  const sites = (scenario.persona_sites || []).join(", ");
  const details = node("dl", "scenario-details");
  for (const [label, value] of [
    ["Starts as", sites ? `${scenario.persona_name} · ${sites}` : scenario.persona_name],
    ["Expected", scenario.expected],
  ]) details.append(node("dt", "", label), node("dd", "", value));
  main.append(details);
  for (const label of scenario.fault_labels || []) main.append(node("p", "scenario-fault", `Synthetic fault: ${label}`));
  if (scenario.unavailable_reason) main.append(node("p", "scenario-unavailable", scenario.unavailable_reason));
  const buttons = node("div", "scenario-actions");
  const start = node("button", "start-session", "Start scenario");
  start.type = "button";
  start.disabled = Boolean(scenario.unavailable_reason) || busy;
  start.addEventListener("click", () => launchScenario(scenario.id));
  const reset = node("button", "secondary-action", "Reset scenario");
  reset.type = "button";
  reset.disabled = !running || busy;
  reset.addEventListener("click", () => launchScenario(scenario.id));
  buttons.append(start, reset);
  row.append(main, buttons);
  return row;
}

function renderScenarioGallery() {
  const container = $("scenario-groups");
  container.replaceChildren();
  if (scenariosState === "loading") {
    container.append(node("p", "gallery-state", "Loading demo scenarios…"));
    return;
  }
  if (scenariosState === "error") return;
  if (scenarios.length === 0) {
    container.append(node("p", "gallery-state",
      "No demo scenarios are configured. Add them to src/alsense_chat/scenarios.yaml, or point SCENARIOS_FILE at your own file."));
    return;
  }
  for (const [group, title] of SCENARIO_GROUPS) {
    const items = scenarios.filter((scenario) => scenario.group === group);
    if (items.length === 0) continue;
    const section = node("section", "scenario-group");
    const heading = node("h2", "", title);
    heading.id = `scenario-group-${group}`;
    section.setAttribute("aria-labelledby", heading.id);
    const list = node("ul", "scenario-list");
    for (const scenario of items) list.append(scenarioRow(scenario));
    section.append(heading, list);
    container.append(section);
  }
}

async function openScenarios() {
  if (busy) return;
  $("scenario-error").hidden = true;
  $("welcome").hidden = true;
  $("messages").hidden = true;
  $("scenario-gallery").hidden = false;
  renderScenarioGallery();
  $("scenario-gallery-title").focus();
  try {
    await loadScenarios();
    renderScenarioGallery();
  } catch (error) {
    $("scenario-error").textContent = error.message;
    $("scenario-error").hidden = false;
  }
}

function closeScenarios() {
  if ($("scenario-gallery").hidden) return;
  $("scenario-gallery").hidden = true;
  $("messages").hidden = false;
  $("welcome").hidden = $("messages").childElementCount > 0;
}

async function startScenario(id) {
  const scenario = scenarios.find((item) => item.id === id);
  if (!scenario) throw new Error("That scenario isn’t available.");
  if (busy) throw new Error("Wait for the current answer to finish.");
  const session = await (await api(`/api/demo/scenarios/${encodeURIComponent(id)}/start`, {
    access_code: $("access-code").value.trim(),
  })).json();
  activeScenario = { scenario, index: 0, running: false };
  applySession(session);
  $("coach-run").focus();
}

function launchScenario(id) {
  $("scenario-error").hidden = true;
  startScenario(id).catch((error) => {
    if ($("scenario-gallery").hidden) {
      showError(error.message);
      return;
    }
    $("scenario-error").textContent = error.message;
    $("scenario-error").hidden = false;
  });
}

function latestActionHandle() {
  const cards = [...document.querySelectorAll("#messages .action-card")].filter((card) => card.dataset.state !== "replaced");
  const card = cards.pop();
  return card ? actionCards.get(card) : null;
}

function applyDraftChanges(changes) {
  const form = $("draft-form");
  if ($("draft-panel").hidden || draftKind !== "report") throw new Error("Open the report builder first.");
  for (const [name, value] of Object.entries(changes)) {
    const input = [...form.querySelectorAll(`input[name="${name}"]`)].find((item) => item.value === value && !item.disabled);
    if (!input) throw new Error("The report builder doesn’t offer that choice.");
    input.checked = true;
    if (name === "period") applyPeriod(value);
    if (name === "report_type") syncFormats();
  }
  updateReportBuilder({ animate: true });
}

/* Each step uses the same functions as the buttons a presenter would click. */
function chooseOption(label) {
  const wanted = String(label).trim().toLowerCase();
  const group = [...document.querySelectorAll("#messages .reply-choices")].pop();
  const button = [...(group?.querySelectorAll("button") || [])]
    .find((item) => replyChoices.get(item)?.label.trim().toLowerCase() === wanted);
  if (!button || button.disabled) throw new Error("That choice isn’t on offer. Reset the scenario to start again.");
  return chooseReply(replyChoices.get(button), button);
}

async function runScenarioStep(step) {
  if (step.say) return send(step.say, step.display || step.say);
  if (step.choose) return chooseOption(step.choose);
  if (step.answer) {
    if (!activeFlow) throw new Error("There’s no guided-check question waiting. Reset the scenario to start again.");
    return answerFlow(activeFlow, step.answer, step.label || step.answer);
  }
  if (step.open) {
    await openDraft(step.open, step.prefill ? { ...step.prefill } : null, { fresh: true });
    return true;
  }
  if (step.set) {
    applyDraftChanges(step.set);
    return true;
  }
  if (step.click === "prepare") return prepareDraft();
  if (step.click === "support") {
    if (!lastFlowResult?.ticket) throw new Error("No guided check has suggested a support request yet.");
    await openDraft("ticket", lastFlowResult.ticket, { fromFlow: true });
    return true;
  }
  const handle = latestActionHandle();
  if (!handle) throw new Error("There’s no draft to use yet. Reset the scenario to start again.");
  if (step.click === "confirm") return handle.confirm();
  if (step.click === "edit") return handle.edit();
  if (step.click === "status") return handle.status();
  throw new Error("This scenario step isn’t supported.");
}

function renderCoach() {
  const coach = $("scenario-coach");
  if (!activeScenario || !token) {
    coach.hidden = true;
    return;
  }
  const { scenario, index, running } = activeScenario;
  const total = scenario.steps.length;
  const done = index >= total;
  coach.hidden = false;
  coach.dataset.state = done ? "done" : "running";
  $("coach-title").textContent = scenario.title;
  $("coach-progress").textContent = done ? "Complete." : `Step ${index + 1} of ${total}.`;
  $("coach-note").textContent = done ? `Expected: ${scenario.expected}` : scenario.steps[index].note;
  $("coach-run").hidden = done;
  $("coach-run").disabled = busy || running;
  $("coach-run").textContent = running ? "Running…" : "Run step";
  $("coach-reset").disabled = busy || running;
}

async function runCoachStep() {
  if (!activeScenario || activeScenario.running || busy) return;
  const scenario = activeScenario;
  const step = scenario.scenario.steps[scenario.index];
  if (!step) return;
  scenario.running = true;
  clearError();
  renderCoach();
  try {
    await runScenarioStep(step);
    scenario.index += 1;
  } catch (error) {
    showError(error.message);
  } finally {
    scenario.running = false;
    renderCoach();
  }
}

fetch("/api/health")
  .then((response) => {
    if (!response.ok) throw new Error("Health check failed.");
    return response.json();
  })
  .then((health) => {
    cloudMode = Boolean(health.access_code_required);
    features = new Set(Array.isArray(health.features) ? health.features : []);
    $("demo-notice").textContent = health.data_source === "synthetic"
      ? `${cloudMode ? "Hosted" : "Local"} reference demo. Uses synthetic data and does not connect to Alsense.`
      : "Connected to the configured Alsense APIs.";
    $("access-code-field").hidden = !cloudMode;
    $("model-state").textContent = health.mode === "foundry" ? "AI assistant ready" : "Demo assistant ready";
    if (!cloudMode) $("code-help").textContent = "Local demo session · no access code needed.";
    updateSessionUI();
  })
  .catch(() => {
    $("model-state").textContent = "Service unavailable";
    showError("The demo service couldn’t be reached. Start the service or check the hosted demo connection.");
  });

loadPersonas().catch(() => {});
loadScenarios().catch(() => {});

updateSessionUI();
