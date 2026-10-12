/* Session credentials live in memory only; they are never saved or added to chat messages. */
const $ = (id) => document.getElementById(id);
let token = "";
let user = null;
let draftKind = "";
let busy = false;
let cloudMode = false;
let lastMessage = "";

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
  prepare_report: "Preparing the report for review…",
  prepare_support_ticket: "Preparing the support request for review…",
};

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
    button.addEventListener("click", () => openDraft(kind).catch(showError));
    row.append(button);
  }
  parent.append(row);
}

function formatActionValue(key, value) {
  const labels = {
    site_id: user?.sites?.find((site) => site.id === value)?.name || "Permitted site",
    report_type: ({ "alarm-history": "Alarm history", "site-status": "Site status" })[value] || "Report",
    format: typeof value === "string" ? value.toUpperCase() : String(value),
    start: value,
    end: value,
    alarm_id: value,
    summary: value,
    steps_tried: value,
  };
  return labels[key] ?? String(value);
}

function draftRequestLabel(kind, payload) {
  const site = user?.sites?.find((item) => item.id === payload.site_id)?.name || "your permitted site";
  if (kind === "report") {
    const format = payload.format.toUpperCase();
    const article = /^[AEIOU]/.test(format) ? "an" : "a";
    return `Prepare ${article} ${format} ${formatActionValue("report_type", payload.report_type)} report for ${site} · ${payload.start} to ${payload.end}`;
  }
  return `Prepare a support request for ${site}: ${payload.summary}`;
}

function renderAction(action, parent) {
  const card = node("section", "action-card");
  const isReport = action.kind === "report";
  card.append(node("h3", "", isReport ? "Report ready for review" : "Support request ready for review"));
  card.append(node("p", "", "Nothing has been sent. Confirm below to submit this synthetic demo action."));

  const labels = isReport
    ? [["site_id", "Site"], ["report_type", "Report"], ["format", "Format"], ["start", "From"], ["end", "To"]]
    : [["site_id", "Site"], ["alarm_id", "Alarm"], ["summary", "Summary"], ["steps_tried", "Steps tried"]];
  const details = node("dl", "action-details");
  for (const [key, label] of labels) {
    if (action.payload?.[key] === undefined) continue;
    details.append(node("dt", "", label), node("dd", "", formatActionValue(key, action.payload[key])));
  }
  card.append(details);

  const button = node("button", "confirm-action", isReport ? "Confirm synthetic report" : "Confirm synthetic request");
  button.type = "button";
  const state = node("p", "submission-state");
  state.hidden = true;
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Submitting…";
    state.hidden = true;
    try {
      const result = await (await api(`/api/actions/${encodeURIComponent(action.id)}/submit`, { confirmed: true })).json();
      state.textContent = "Submitted to the synthetic demo. No real report or support request was sent.";
      state.hidden = false;
      button.textContent = "Submitted";
      if (result.url) addLink(card, "Open synthetic record", result.url);
    } catch (error) {
      state.textContent = error.message;
      state.classList.add("failed");
      state.hidden = false;
      button.disabled = false;
      button.textContent = isReport ? "Retry synthetic report" : "Retry synthetic request";
    }
  });
  card.append(button, state);
  parent.append(card);
}

function renderReply(reply) {
  const article = node("article", "turn assistant-turn");
  article.append(node("span", "assistant-mark", "A"));
  const body = node("div", "assistant-body");
  const heading = node("div", "answer-heading");
  heading.append(node("strong", "", "Operations assistant"));
  heading.append(node("span", `answer-status ${reply.status || "answer"}`, STATUS_LABELS[reply.status] || "Answer"));
  body.append(heading);
  textParagraphs(body, reply.text, "answer-text");
  renderEvidence(reply, body);
  for (const action of reply.actions || []) renderAction(action, body);
  renderFollowUps(reply, body);
  article.append(body);
  $("messages").append(article);
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
  $("messages").replaceChildren();
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
    $("messages").replaceChildren();
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

async function openDraft(kind) {
  if (!user) throw new Error("Start a synthetic demo session first.");
  if (busy) throw new Error("Wait for the current answer to finish.");
  clearError();
  draftKind = kind;
  const form = $("draft-form");
  form.reset();
  const siteSelect = form.elements.site_id;
  siteSelect.replaceChildren();
  for (const site of user.sites) siteSelect.add(new Option(site.name, site.id));
  const reportFields = $("report-fields");
  const ticketFields = $("ticket-fields");
  reportFields.hidden = kind !== "report";
  ticketFields.hidden = kind !== "ticket";
  for (const input of reportFields.querySelectorAll("input, select")) {
    input.disabled = kind !== "report";
    input.required = kind === "report";
  }
  for (const input of ticketFields.querySelectorAll("input, textarea")) {
    input.disabled = kind !== "ticket";
    input.required = kind === "ticket";
  }
  if (kind === "report") {
    const types = await (await api("/api/report-types")).json();
    form.elements.report_type.replaceChildren();
    for (const report of types) form.elements.report_type.add(new Option(report.label, report.id));
    $("draft-title").textContent = "Prepare a report";
    $("draft-description").textContent = "Choose the site, report and date range for your draft.";
  } else {
    $("draft-title").textContent = "Prepare a support request";
    $("draft-description").textContent = "Describe the issue and what you’ve already tried.";
    form.elements.alarm_id.value = `alarm-${siteSelect.value}`;
  }
  $("draft-panel").hidden = false;
  siteSelect.focus();
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
  send($("message").value).catch(showError);
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
  button.addEventListener("click", () => send(button.dataset.message).catch(showError));
});
$("open-report").addEventListener("click", () => openDraft("report").catch((error) => showError(error.message)));
$("open-ticket").addEventListener("click", () => openDraft("ticket").catch((error) => showError(error.message)));
$("suggest-report").addEventListener("click", () => openDraft("report").catch((error) => showError(error.message)));
$("cancel-draft").addEventListener("click", () => { $("draft-panel").hidden = true; });
$("draft-form").addEventListener("change", (event) => {
  if (event.target.name === "site_id" && draftKind === "ticket") {
    $("draft-form").elements.alarm_id.value = `alarm-${event.target.value}`;
  }
});
$("draft-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const payload = Object.fromEntries(new FormData(form));
  const displayText = draftRequestLabel(draftKind, payload);
  clearError();
  try {
    const completed = await send(`/${draftKind} ${JSON.stringify(payload)}`, displayText);
    if (completed) {
      $("draft-panel").hidden = true;
      form.reset();
    }
  } catch (error) {
    showError(error.message);
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
