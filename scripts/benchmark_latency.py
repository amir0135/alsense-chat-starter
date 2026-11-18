"""Reproducible, offline latency benchmark of the real HTTP/runner/tool paths.

The Responses provider is a deterministic replay, NOT Azure or an LLM. Optional fixed sleeps model
network sensitivity only. No credentials, real endpoints, prompts or session tokens are saved.

    uv run python scripts/benchmark_latency.py --output benchmarks/latency-before.json
    uv run python scripts/benchmark_latency.py --model-ms 40 --api-ms 15 --output PATH
"""

import argparse
import json
import platform
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from alsense_chat.adapters import synthetic
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.runner import FoundryRunner
from alsense_chat.timing import percentile

SCENARIOS = {
    "simple_question": "What should I check for an offline sensor?",
    "site_resolution": "Find Demo North",
    "dashboard_navigation_unsupported": "Open the alarms dashboard and filter to critical and high alarms",
    "report_preparation": "Create a temperature report for Demo North for the last seven days",
    "report_lifecycle": "Create a temperature report for Demo North for the last seven days",
}


@dataclass
class FunctionCall:
    name: str
    arguments: str
    call_id: str
    type: str = "function_call"

    def model_dump(self, **_) -> dict:
        return vars(self)


@dataclass
class Replay:
    delay_ms: float
    rounds: list = field(default_factory=list)
    request_bytes: list[int] = field(default_factory=list)

    def reset(self, scenario: str) -> None:
        def call(tool: str, **args) -> FunctionCall:
            return FunctionCall(tool, json.dumps(args), f"call-{tool}")

        def answer(text: str, source: str) -> dict:
            return {"status": "answer", "text": text, "source_ids": [source]}

        if scenario.startswith("report_"):
            today = date.today()
            self.rounds = [
                [call("get_permitted_sites", name="Demo North"), call("list_report_types")],
                [
                    call(
                        "prepare_report",
                        site_id="site-a",
                        report_type="temperature-log",
                        format="pdf",
                        start=(today - timedelta(days=6)).isoformat(),
                        end=today.isoformat(),
                    )
                ],
                answer("Your draft is ready. Review it and press Confirm.", "tool-3"),
            ]
        elif scenario == "simple_question":
            self.rounds = [
                [call("search_docs", query="offline sensor")],
                answer("Check power and connectivity.", "tool-1"),
            ]
        elif scenario == "site_resolution":
            self.rounds = [
                [call("get_permitted_sites", name="Demo North")],
                answer("Demo North is one of your sites.", "tool-1"),
            ]
        else:
            self.rounds = [
                {
                    "status": "out_of_scope",
                    "text": "I can't open or filter the alarms dashboard here.",
                    "source_ids": [],
                }
            ]
        self.request_bytes = []

    def create(self, **request) -> SimpleNamespace:
        self.request_bytes.append(len(json.dumps(request).encode()))
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        output = self.rounds.pop(0)
        return SimpleNamespace(
            status="completed",
            output=output if isinstance(output, list) else [],
            output_text=json.dumps(output) if isinstance(output, dict) else "",
            usage=None,
        )


class CountedService:
    """Benchmark-only adapter boundary probe; delegates every call unchanged, with an optional fixed sleep."""

    def __init__(self, inner: Any, name: str, calls: Counter, delay_ms: float):
        self.inner, self.name, self.calls, self.delay_ms = inner, name, calls, delay_ms

    def __getattr__(self, name: str):
        method = getattr(self.inner, name)

        def invoke(*args, **kwargs):
            self.calls[f"{self.name}.{name}"] += 1
            if self.delay_ms:
                time.sleep(self.delay_ms / 1000)
            return method(*args, **kwargs)

        return invoke


def replay_runner(replay: Replay) -> FoundryRunner:
    # Bypass only SDK construction: run(), create(), routing, validation and retries are the real implementation.
    runner = FoundryRunner.__new__(FoundryRunner)
    runner.deployment, runner.reasoning_effort, runner.max_output_tokens = "offline-replay", "high", 8000
    runner.client = SimpleNamespace(responses=replay, close=lambda: None)
    runner.project = SimpleNamespace(close=lambda: None)
    runner.credential = SimpleNamespace(close=lambda: None)
    return runner


def checked_chat(client: TestClient, headers: dict, message: str) -> dict:
    response = client.post("/api/chat", headers=headers, json={"message": message})
    response.raise_for_status()
    frames = [frame.split("\n", 1) for frame in response.text.strip().split("\n\n")]
    replies = [json.loads(data.removeprefix("data: ")) for name, data in frames if name == "event: reply"]
    if not replies or replies[-1]["status"] == "error":
        raise RuntimeError("Benchmark chat failed; timings would not describe a successful scenario.")
    return replies[-1]


def sample(client: TestClient, scenario: str, replay: Replay, calls: Counter) -> dict:
    login = client.post("/api/demo/session", json={"persona": "alice"})
    login.raise_for_status()
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    session_id = login.json()["session_id"]
    replay.reset(scenario)
    calls.clear()
    started = time.perf_counter()
    reply = checked_chat(client, headers, SCENARIOS[scenario])
    expected = "out_of_scope" if scenario == "dashboard_navigation_unsupported" else "answer"
    if reply["status"] != expected:
        raise RuntimeError(f"Unexpected benchmark result for {scenario}.")
    if scenario.startswith("report_") and len(reply["actions"]) != 1:
        raise RuntimeError("Benchmark did not prepare exactly one report.")
    if scenario == "report_lifecycle":
        action_id = reply["actions"][0]["id"]
        response = client.post(f"/api/actions/{action_id}/submit", headers=headers, json={"confirmed": True})
        response.raise_for_status()
        status = client.get(f"/api/actions/{action_id}/status", headers=headers)
        status.raise_for_status()
        if status.json()["state"] != "completed":
            raise RuntimeError("Synthetic report did not complete.")
    elapsed = (time.perf_counter() - started) * 1000
    records = [
        row for row in client.app.state.store.audit_trail(session_id) if row["event"] in {"chat", "submit", "status"}
    ]
    stages: Counter = Counter()
    for row in records:
        stages.update(row["timings"].get("ms", {}))
    if replay.rounds:
        raise RuntimeError("Benchmark replay was not fully consumed.")
    return {
        "elapsed_ms": round(elapsed, 3),
        "model_calls": sum(row["usage"].get("model_calls", 0) for row in records),
        "tool_calls": sum(len(row["tools"]) for row in records),
        "adapter_calls": sum(calls.values()),
        "adapter_calls_by_operation": dict(calls),
        "model_request_bytes": list(replay.request_bytes),
        "stage_ms": dict(stages),
        "correlation_ids": [row["correlation_id"] for row in records],
    }


def benchmark(runs: int, warmups: int, model_ms: float, api_ms: float) -> dict:
    calls: Counter = Counter()
    services = synthetic.build()
    services = replace(
        services,
        sites=CountedService(services.sites, "sites", calls, api_ms),
        knowledge=CountedService(services.knowledge, "knowledge", calls, api_ms),
        reports=CountedService(services.reports, "reports", calls, api_ms),
    )
    replay = Replay(model_ms)
    results = {}
    with tempfile.TemporaryDirectory() as folder:
        settings = Settings(db=str(Path(folder) / "benchmark.sqlite3"), log_level="WARNING")
        with TestClient(create_app(settings, services)) as client:
            client.app.state.runner = replay_runner(replay)
            for scenario in SCENARIOS:
                for _ in range(warmups):
                    sample(client, scenario, replay, calls)
                samples = [sample(client, scenario, replay, calls) for _ in range(runs)]
                elapsed = [row["elapsed_ms"] for row in samples]
                results[scenario] = {
                    "median_ms": median(elapsed),
                    "p95_nearest_rank_ms": percentile(elapsed, 0.95),
                    "samples": samples,
                }
    return {
        "schema_version": 1,
        "provider": "offline deterministic Responses replay; NOT Azure/LLM measurements",
        "transport": "FastAPI TestClient in-process ASGI; no browser, TCP, TLS or iframe",
        "data": "existing synthetic adapters; Alice; fresh session per sample; sign-in excluded",
        "profile": {"model_delay_ms_per_call": model_ms, "adapter_delay_ms_per_call": api_ms},
        "runs_per_scenario": runs,
        "warmups_per_scenario": warmups,
        "p95_note": "Nearest rank; with fewer than 20 samples this is the maximum, not a stable tail estimate.",
        "python": platform.python_version(),
        "platform": platform.system() + " " + platform.machine(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--model-ms", type=float, default=0)
    parser.add_argument("--api-ms", type=float, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 5 or args.warmups < 0 or not all(0 <= value <= 10_000 for value in (args.model_ms, args.api_ms)):
        parser.error("Use at least five runs, non-negative warmups and finite delays between 0 and 10000 ms.")
    result = benchmark(args.runs, args.warmups, args.model_ms, args.api_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for name, values in result["results"].items():
        counts = values["samples"][0]
        print(
            f"{name}: median {values['median_ms']:.2f} ms, "
            f"p95 {values['p95_nearest_rank_ms']:.2f} ms; "
            f"model/tool/adapter calls {counts['model_calls']}/{counts['tool_calls']}/{counts['adapter_calls']}"
        )


if __name__ == "__main__":
    main()
