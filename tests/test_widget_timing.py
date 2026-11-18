"""Execute the widget's real timing/polling functions with a fake clock and transport (not browser timings)."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_widget_timing_and_correlation_preserve_polling_and_failure_boundaries():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the widget timing checks.")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [node, "-e", JAVASCRIPT],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


JAVASCRIPT = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync("src/alsense_chat/static/widget.js", "utf8");
const between = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

function harness(states) {
  let now = 0;
  const measures = [], requests = [], renders = [];
  const context = vm.createContext({
    performance: {
      now: () => now,
      measure: (name, options) => measures.push({ name, ...options }),
    },
    crypto: { randomUUID: () => "trace-widget-latency" },
    document: { hidden: false },
    requestAnimationFrame: (fn) => fn(),
    setTimeout: (fn, delay = 0) => { now += delay; fn(); },
    fetch: async (path, options) => {
      requests.push({ path, options });
      now += 3;
      const state = states.shift();
      if (state instanceof Error) throw state;
      return {
        ok: true,
        headers: { get: () => "trace-widget-latency" },
        json: async () => ({ state }),
      };
    },
    renderReportResult: (...args) => renders.push(args),
    friendlyRequestError: () => "Safe request error",
  });
  vm.runInContext(`
    let token = "secret-session-token";
    const followedReports = new WeakSet();
    const STATUS_POLL = { first: 10, growth: 1.5, longest: 50, limit: 100 };
    ${between("function newCorrelationId(", "function node(")}
    ${between("async function reportStatus(", "function renderHealth(")}
    ${between("function chatTiming(", "async function send(")}
  `, context);
  return { context, measures, requests, renders };
}

(async () => {
  const h = harness(["processing", "completed"]);
  const card = { isConnected: true };
  const status = await h.context.followReport("private-action-id", card, true, () => {}, "trace-widget-latency");
  assert.equal(status.state, "completed");
  assert.equal(h.requests.length, 2);
  assert.equal(h.renders.length, 2);
  for (const request of h.requests) {
    assert.equal(request.options.headers["X-Correlation-ID"], "trace-widget-latency");
    assert.equal(request.options.headers.Authorization, "Bearer secret-session-token");
  }
  const poll = h.measures.find((m) => m.name === "alsense report_poll");
  assert.equal(poll.detail.poll_count, 2);
  assert.equal(poll.detail.outcome, "finished");
  assert.equal(poll.duration, 16);
  assert.equal(h.measures.filter((m) => m.name === "alsense report_status").length, 2);
  assert.equal(h.measures.filter((m) => m.name === "alsense report_render").length, 2);
  assert(!JSON.stringify(h.measures).includes("secret-session-token"));
  assert(!JSON.stringify(h.measures).includes("private-action-id"));

  const failed = harness([new Error("secret upstream error")]);
  await assert.rejects(() => failed.context.followReport("action", card), /couldn’t be reached/);
  const failure = failed.measures.find((m) => m.name === "alsense report_poll");
  assert.equal(failure.detail.outcome, "error");
  assert.equal(failure.detail.poll_count, 1);
  assert.equal(failed.requests.length, 1);
  assert(!JSON.stringify(failed.measures).includes("secret upstream error"));

  const detached = harness(["completed"]);
  assert.equal(await detached.context.followReport("action", { isConnected: false }), null);
  assert.equal(detached.renders.length, 0);
  assert.equal(detached.measures.find((m) => m.name === "alsense report_poll").detail.outcome, "cancelled");

  const chat = harness(["unused"]);
  const timing = chat.context.chatTiming();
  assert.equal(timing.correlationId, "trace-widget-latency");
  timing.received({ headers: { get: () => "trace-widget-latency" } });
  timing.mark("first_event");
  timing.mark("reply");
  timing.rendered(0);
  timing.report();
  await new Promise(setImmediate);
  const report = JSON.parse(chat.requests[0].options.body);
  assert.equal(chat.requests[0].path, "/api/timings");
  assert.equal(report.correlation_id, "trace-widget-latency");
  for (const key of ["headers", "first_event", "reply", "rendered"]) assert(Number.isInteger(report[key]));
  assert(chat.measures.some((m) => m.name === "alsense chat_render"));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
