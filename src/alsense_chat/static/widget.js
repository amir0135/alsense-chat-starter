/* Reference widget: demo credentials stay in memory, never in browser storage or model prompts. */
const $ = (id) => document.getElementById(id);
let token = "";
let user = null;
let draftKind = "";
let busy = false;

function showError(error) { $("error").textContent = error.message; }
function paragraph(parent, text) {
  const node = document.createElement("p");
  node.textContent = text;
  parent.append(node);
}
function approvedLink(value) {
  return typeof value === "string" && value.startsWith("/reference/") &&
    !value.includes("\\") && !/[\x00-\x1f]/.test(value);
}
function link(parent, text, url) {
  if (!approvedLink(url)) return;
  const a = document.createElement("a");
  a.textContent = text;
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  parent.append(a);
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Request failed (${response.status}): ${detail}`);
  }
  return response;
}
function renderReply(reply) {
  const article = document.createElement("article");
  paragraph(article, `${reply.status}: ${reply.text}`);
  for (const source of reply.sources || []) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `Evidence ${source.source_id}: ${source.tool}`;
    details.append(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(source.data, null, 2);
    details.append(pre);
    if (source.tool === "search_docs") {
      for (const doc of source.data) link(details, doc.title, doc.url);
    }
    if (source.tool === "get_report_status" && source.data.result) {
      link(details, "Open synthetic report view", source.data.result.url);
    }
    article.append(details);
  }
  for (const action of reply.actions || []) {
    paragraph(article, `${action.kind} draft - nothing sent yet`);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(action.payload, null, 2);
    article.append(pre);
    const button = document.createElement("button");
    button.textContent = `Submit ${action.kind} to synthetic API`;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await (await api(`/api/actions/${encodeURIComponent(action.id)}/submit`, { confirmed: true })).json();
        paragraph(article, `Synthetic result: ${result.status}`);
        link(article, "Open synthetic view", result.url);
        button.textContent = "Submitted";
      } catch (error) { showError(error); button.disabled = false; }
    });
    article.append(button);
  }
  $("messages").append(article);
}
async function send(message) {
  if (!token) throw new Error("Start a demo session first.");
  if (busy) throw new Error("Wait for the current turn to finish.");
  busy = true;
  $("send").disabled = true;
  $("error").textContent = "";
  const question = document.createElement("article");
  paragraph(question, message);
  $("messages").append(question);
  try {
    const response = await api("/api/chat", { message });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const events = buffer.split("\n\n");
      buffer = events.pop();
      for (const event of events) {
        const lines = event.split("\n");
        const name = lines.find((line) => line.startsWith("event: "))?.slice(7);
        const data = JSON.parse(lines.filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n"));
        if (name === "reply") renderReply(data);
        if (name === "error") throw new Error(data.message);
      }
      if (done) break;
    }
  } finally { busy = false; $("send").disabled = false; }
}
$("login").addEventListener("click", async () => {
  try {
    if (busy) throw new Error("Wait for the current turn before changing users.");
    const session = await (await api("/api/demo/session", {
      persona: $("persona").value, access_code: $("access-code").value,
    })).json();
    $("access-code").value = "";
    token = session.token;
    user = session.user;
    $("messages").replaceChildren();
    $("identity").textContent = user.name;
  } catch (error) { showError(error); }
});
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("message").value;
  try { await send(message); $("message").value = ""; } catch (error) { showError(error); }
});
document.querySelectorAll("[data-message]").forEach((button) => {
  button.addEventListener("click", () => send(button.dataset.message).catch(showError));
});
async function openDraft(kind) {
  if (!user) throw new Error("Start a demo session first.");
  if (busy) throw new Error("Wait for the current turn.");
  draftKind = kind;
  const form = $("draft-form");
  form.reset();
  form.elements.site_id.replaceChildren();
  for (const site of user.sites) form.elements.site_id.add(new Option(site.name, site.id));
  $("report-fields").hidden = kind !== "report";
  $("ticket-fields").hidden = kind !== "ticket";
  for (const input of $("report-fields").querySelectorAll("input, select")) {
    input.disabled = kind !== "report";
    input.required = kind === "report";
  }
  for (const input of $("ticket-fields").querySelectorAll("input, textarea")) {
    input.disabled = kind !== "ticket";
    input.required = kind === "ticket";
  }
  if (kind === "report") {
    const response = await api("/api/chat", { message: "/reports" });
    const text = await response.text();
    const event = text.split("\n\n").find((part) => part.startsWith("event: reply\n"));
    if (!event) throw new Error("Report types unavailable.");
    const reply = JSON.parse(event.split("\ndata: ")[1]);
    const evidence = reply.sources.find((item) => item.tool === "list_report_types");
    if (!evidence) throw new Error("Report types unavailable.");
    form.elements.report_type.replaceChildren();
    for (const item of evidence.data) form.elements.report_type.add(new Option(item.label, item.id));
  }
  $("draft-title").textContent = `Prepare ${kind}`;
  $("draft").showModal();
}
$("report").addEventListener("click", () => openDraft("report").catch(showError));
$("ticket").addEventListener("click", () => openDraft("ticket").catch(showError));
$("cancel-draft").addEventListener("click", () => $("draft").close());
$("draft-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.target));
  $("draft").close();
  try { await send(`/${draftKind} ${JSON.stringify(payload)}`); } catch (error) { showError(error); }
});
