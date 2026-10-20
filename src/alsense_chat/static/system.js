/* Renders the non-secret readiness report from /api/system. */
const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function renderItems(items) {
  const list = $("status-rows");
  list.replaceChildren();
  for (const item of items) {
    const row = node("li", "status-row");
    const badge = node("span", "state-badge", item.state_label);
    badge.dataset.state = item.state;
    row.append(node("span", "status-label", item.label), node("span", "status-value", item.value), badge);
    if (item.detail) row.append(node("span", "status-detail", item.detail));
    list.append(row);
  }
}

function renderFlows(flows) {
  const body = $("flow-rows");
  body.replaceChildren();
  if (flows.length === 0) {
    const row = node("tr");
    const cell = node("td", "", "No flows are loaded.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const flow of flows) {
    const row = node("tr");
    for (const value of [flow.title, flow.version, flow.status, flow.owner, flow.offered ? "Yes" : "No"]) {
      row.append(node("td", "", value));
    }
    body.append(row);
  }
}

async function load() {
  try {
    const response = await fetch("/api/system", { cache: "no-store" });
    if (!response.ok) throw new Error("The status report is unavailable.");
    const status = await response.json();
    $("demo-notice").textContent = status.notice;
    $("overall").textContent = status.overall.label;
    $("overall").dataset.state = status.overall.state;
    $("system-meta").textContent = `Version ${status.version} · commit ${status.commit}`;
    renderItems(status.items);
    renderFlows(status.flows);
  } catch {
    $("overall").textContent = "Unavailable";
    $("overall").dataset.state = "attention";
    $("system-error").textContent = "The demo service couldn’t be reached. Start it, then reload this page.";
    $("system-error").hidden = false;
  }
}

load();
