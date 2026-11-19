"""Measure the real widget against a loopback server and deterministic provider; no dashboard capability.

Requires an installed Chrome and the locked app dependencies, not Selenium or Playwright. A new temporary
browser profile is used; existing browser profiles, sessions and credentials are never opened.

    uv run --no-sync python scripts/benchmark_dashboard_browser.py --output benchmarks/dashboard-browser-local.json
"""

import argparse
import json
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from statistics import median

import httpx
import uvicorn
from benchmark_latency import DASHBOARD_MESSAGE, DASHBOARD_SCENARIOS, CountedService, Replay, replay_runner
from websockets.sync.client import connect

from alsense_chat.adapters import synthetic
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.timing import percentile


def wait_until(check, description: str):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for {description}.")


@contextmanager
def local_server(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            wait_until(lambda: server.started, "local benchmark server startup")
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("The benchmark server did not stop.")


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0

    def command(self, method: str, **params):
        self.sequence += 1
        self.connection.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            response = json.loads(self.connection.recv(timeout=30))
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeError(f"Chrome protocol command failed: {method}")
            return response.get("result", {})

    def evaluate(self, expression: str):
        response = self.command("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in response:
            raise RuntimeError("Browser benchmark expression failed; no successful timing recorded.")
        return response["result"].get("value")


@contextmanager
def headless_chrome(binary: str, folder: Path):
    profile = folder / "chrome"
    process = subprocess.Popen(
        [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        wait_until(lambda: port_file.is_file() or process.poll() is not None, "headless Chrome startup")
        if process.poll() is not None:
            raise RuntimeError("Headless Chrome exited before its debugging endpoint became ready.")
        port = int(port_file.read_text().splitlines()[0])
        targets = httpx.get(f"http://127.0.0.1:{port}/json/list", trust_env=False).json()
        target = next(item for item in targets if item["type"] == "page")
        with connect(target["webSocketDebuggerUrl"]) as connection:
            browser = Browser(connection)
            browser.version = browser.command("Browser.getVersion")["product"]
            yield browser
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def sample(browser: Browser, app, replay: Replay, calls: Counter, scenario: str) -> dict:
    # Drive the actual widget's session and send functions. No fabricated UI actions or replacement SSE handler.
    browser.evaluate("startSession()")
    if not browser.evaluate("Boolean(token && user && user.id === 'alice')"):
        raise RuntimeError("The synthetic browser session did not start.")
    replay.reset(scenario)
    app.state.runner = None if scenario == "dashboard_scripted" else replay_runner(replay)
    calls.clear()
    browser.evaluate("performance.clearMeasures(); performance.clearResourceTimings()")
    sent = browser.evaluate(
        "(async () => { const started = performance.now(); "
        f"const ok = await send({json.dumps(DASHBOARD_MESSAGE)}); "
        "return {ok, send_to_stream_end_ms: performance.now() - started}; })()"
    )
    if not sent["ok"]:
        raise RuntimeError("Widget chat failed; timings would not describe a successful request.")
    # The widget schedules its paint mark and telemetry asynchronously after the stream closes.
    measures = wait_until(
        lambda: browser.evaluate(
            "performance.getEntriesByName('alsense chat rendered').length "
            "? performance.getEntriesByType('measure').map(m => "
            "({name:m.name, duration_ms:m.duration, detail:m.detail})) : null"
        ),
        "reply paint opportunity",
    )
    correlation_id = next(item["detail"]["correlation_id"] for item in measures if item["name"] == "alsense chat reply")
    records = wait_until(
        lambda: [
            record
            for record in app.state.store.audit_trail(limit=1000)
            if record["correlation_id"] == correlation_id and record["event"] == "chat"
        ],
        "completed chat audit",
    )
    [record] = records
    expected_tools = (
        []
        if scenario in {"dashboard_direct", "dashboard_repair_direct"}
        else ["get_permitted_sites" if scenario == "dashboard_sites" else "search_docs"]
    )
    if replay.rounds or [item["tool"] for item in record["tools"]] != expected_tools:
        raise RuntimeError("The browser benchmark did not execute the expected real path.")
    reply = browser.evaluate("({status:lastReply.status, actions:lastReply.actions.length})")
    expected_status = (
        "answer" if scenario in {"dashboard_scripted", "dashboard_sites", "dashboard_docs"} else "out_of_scope"
    )
    if reply["actions"] or reply["status"] != expected_status:
        raise RuntimeError("The browser benchmark crossed the current capability boundary.")
    resources = browser.evaluate(
        "performance.getEntriesByType('resource').filter(r => new URL(r.name).pathname === '/api/chat')"
        ".map(r => ({fetch_to_headers_ms:r.responseStart-r.startTime, "
        "fetch_to_end_ms:r.responseEnd-r.startTime, request_start_ms:r.requestStart-r.startTime}))"
    )
    wait_until(
        lambda: any(
            record["correlation_id"] == correlation_id and record["event"] == "client_timing"
            for record in app.state.store.audit_trail(limit=1000)
        ),
        "widget client timing audit",
    )
    return {
        **sent,
        "correlation_id": correlation_id,
        "browser_measures": measures,
        "resource_timing": resources,
        "backend_ms": record["timings"]["ms"],
        "backend_count": record["timings"]["count"],
        "model_calls": record["usage"]["model_calls"],
        "tool_calls": len(record["tools"]),
        "adapter_calls": sum(calls.values()),
        "adapter_calls_by_operation": dict(calls),
        "model_request_bytes": list(replay.request_bytes),
        "model_request_metrics": list(replay.request_metrics),
        "ui_command_count": reply["actions"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chrome",
        default=shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--model-ms", type=float, default=0)
    parser.add_argument("--api-ms", type=float, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 5 or not all(0 <= value <= 10_000 for value in (args.model_ms, args.api_ms)):
        parser.error("Use at least five runs and finite delays between 0 and 10000 ms.")
    if not Path(args.chrome).is_file():
        parser.error("An installed Chrome binary is required; supply --chrome PATH.")
    calls: Counter = Counter()
    replay = Replay(args.model_ms)
    services = synthetic.build()
    services = replace(
        services,
        sites=CountedService(services.sites, "sites", calls, args.api_ms),
        knowledge=CountedService(services.knowledge, "knowledge", calls, args.api_ms),
    )
    results = {}
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        app = create_app(Settings(db=str(folder / "browser.sqlite3"), log_level="ERROR"), services)
        with local_server(app) as url, headless_chrome(args.chrome, folder) as browser:
            browser.command("Page.navigate", url=url)
            wait_until(
                lambda: browser.evaluate(
                    "typeof startSession === 'function' && typeof dataSourceKnown !== 'undefined' "
                    "&& dataSourceKnown && [...document.querySelector('#persona').options].some(o=>o.value==='alice')"
                ),
                "widget bootstrap",
            )
            for scenario in DASHBOARD_SCENARIOS:
                sample(browser, app, replay, calls, scenario)
                samples = [sample(browser, app, replay, calls, scenario) for _ in range(args.runs)]
                elapsed = [row["send_to_stream_end_ms"] for row in samples]
                results[scenario] = {
                    "median_send_to_stream_end_ms": median(elapsed),
                    "p95_nearest_rank_send_to_stream_end_ms": percentile(elapsed, 0.95),
                    "samples": samples,
                }
            version = browser.version
    result = {
        "provider": "offline deterministic Responses replay; NOT Azure/LLM measurements",
        "transport": "real widget in headless Chrome over loopback HTTP; NOT iframe/dashboard execution",
        "browser": version,
        "python": platform.python_version(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "production_source_revision": subprocess.check_output(["git", "rev-parse", "HEAD:src"], text=True).strip(),
        "production_source_dirty": subprocess.call(["git", "diff", "--quiet", "HEAD", "--", "src"]) != 0,
        "profile": {"model_delay_ms_per_call": args.model_ms, "adapter_delay_ms_per_call": args.api_ms},
        "runs_per_scenario": args.runs,
        "warmups_per_scenario": 1,
        "scenario": DASHBOARD_MESSAGE,
        "p95_note": "Nearest rank; with five samples this is the maximum, not a stable tail estimate.",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for name, row in results.items():
        print(f"{name}: browser send-to-stream-end median {row['median_send_to_stream_end_ms']:.2f} ms")


if __name__ == "__main__":
    main()
