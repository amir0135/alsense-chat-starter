"""Shared API helpers for tests and evaluations."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

import alsense_chat.app
from alsense_chat.adapters.synthetic.services import SyntheticReports
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.runner import FoundryRunner


def app_client(tmp_path, *faults: str, **overrides) -> TestClient:
    overrides.setdefault("db", str(tmp_path / "test.sqlite3"))
    settings = Settings(synthetic_faults=frozenset(faults), **overrides)
    return TestClient(create_app(settings))


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def login(client: TestClient, persona: str = "alice") -> dict:
    response = client.post("/api/demo/session", json={"persona": persona})
    assert response.status_code == 200, response.text
    return bearer(response.json()["token"])


def events(client: TestClient, headers: dict, message: str) -> list[tuple[str, dict]]:
    response = client.post("/api/chat", headers=headers, json={"message": message})
    assert response.status_code == 200, response.text
    frames = [frame.split("\n", 1) for frame in response.text.strip().split("\n\n")]
    parsed = [(name.removeprefix("event: "), json.loads(data.removeprefix("data: "))) for name, data in frames]
    assert parsed[-1][0] == "reply", parsed
    return parsed


def reply(client: TestClient, headers: dict, message: str) -> dict:
    return events(client, headers, message)[-1][1]


def flow(client: TestClient, headers: dict, answers: list[str], flow_id: str = "site-connection") -> dict:
    return reply(client, headers, f"/flow {flow_id} {json.dumps(answers)}")


def report_payload(site: str = "site-a", **changes) -> dict:
    today = date.today()
    payload = {
        "site_id": site,
        "report_type": "alarm-history",
        "format": "pdf",
        "start": (today - timedelta(days=29)).isoformat(),
        "end": today.isoformat(),
    }
    return {**payload, **changes}


def submit(client: TestClient, headers: dict, action_id: str):
    return client.post(f"/api/actions/{action_id}/submit", headers=headers, json={"confirmed": True})


class RecordingReports(SyntheticReports):
    """A report service that records what it receives, plays back job states and fails on demand.

    `states` are the job states report_status returns in turn before "completed". `failure`, while set, is raised by
    every submission and status call. `link`, when set, replaces the links the service returns.
    """

    def __init__(self) -> None:
        super().__init__()
        self.received: list[tuple[str, dict, str]] = []
        self.states: list[str] = []
        self.failure: Exception | None = None
        self.link: str | None = None

    def submit_report(self, user, request, idempotency_key):
        self.check()
        self.received.append(("report", request.model_dump(mode="json"), idempotency_key))
        return self.linked(super().submit_report(user, request, idempotency_key))

    def schedule_report(self, user, request, idempotency_key):
        self.check()
        self.received.append(("scheduled_report", request.model_dump(mode="json"), idempotency_key))
        return self.linked(super().schedule_report(user, request, idempotency_key))

    def report_status(self, user, job_id):
        self.check()
        job = self.linked(super().report_status(user, job_id))
        if self.states and self.states.pop(0) == "processing":
            return job.model_copy(update={"state": "processing", "url": None, "completed_at": None, "expires_at": None})
        return job

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def linked(self, record):
        return record.model_copy(update={"url": self.link}) if self.link else record


@dataclass
class Round:
    """One fake Responses API call: what it returns, the usage it reports, its status and why it didn't complete."""

    output: Any
    usage: Any = None
    status: str = "completed"
    reason: str | None = None


class Call:
    type = "function_call"

    def __init__(self, name: str, arguments: dict):
        self.name, self.arguments, self.call_id = name, json.dumps(arguments), f"call-{name}"

    def model_dump(self, **_):
        return {"type": self.type, "name": self.name, "arguments": self.arguments, "call_id": self.call_id}


def foundry(monkeypatch, outputs: list, requests: list | None = None) -> dict:
    """Model mode with a fake Responses API. Returns the settings that select it.

    Each output is a (tool, arguments) pair that calls a tool, a dict that answers, a list of output items returned
    as they are, an exception the call raises, or a Round that wraps one of these with the usage it reports and
    its status. `requests` receives a copy of each call's arguments.
    """

    def create(**request):
        if requests is not None:
            requests.append(json.loads(json.dumps(request)))
        call = outputs.pop(0)
        call = call if isinstance(call, Round) else Round(call)
        if isinstance(call.output, Exception):
            raise call.output
        details = SimpleNamespace(reason=call.reason) if call.reason else None
        if isinstance(call.output, dict):
            text = json.dumps(call.output)
            return SimpleNamespace(
                status=call.status, output=[], output_text=text, usage=call.usage, incomplete_details=details
            )
        items = call.output if isinstance(call.output, list) else [Call(*call.output)]
        return SimpleNamespace(status=call.status, output=items, usage=call.usage, incomplete_details=details)

    def runner(settings):
        instance = FoundryRunner.__new__(FoundryRunner)
        instance.deployment = settings.deployment
        instance.reasoning_effort = settings.reasoning_effort
        instance.max_output_tokens = settings.max_output_tokens
        instance.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
        instance.project = SimpleNamespace(close=lambda: None)
        instance.credential = SimpleNamespace(close=lambda: None)
        return instance

    monkeypatch.setattr(alsense_chat.app, "FoundryRunner", runner)
    return {"agent_mode": "foundry", "foundry_endpoint": "https://foundry.example.invalid", "deployment": "test"}
