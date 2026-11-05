"""Shared API helpers for tests and evaluations."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

import alsense_chat.app
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


@dataclass
class Round:
    """One fake Responses API call: what it returns, the usage it reports and its status."""

    output: Any
    usage: Any = None
    status: str = "completed"


class Call:
    type = "function_call"

    def __init__(self, name: str, arguments: dict):
        self.name, self.arguments, self.call_id = name, json.dumps(arguments), f"call-{name}"

    def model_dump(self, **_):
        return {"type": self.type, "name": self.name, "arguments": self.arguments, "call_id": self.call_id}


def foundry(monkeypatch, outputs: list) -> dict:
    """Model mode with a fake Responses API. Returns the settings that select it.

    Each output is a (tool, arguments) pair that calls a tool, a dict that answers, an exception the call raises,
    or a Round that wraps one of these with the usage it reports and its status.
    """

    def create(**_):
        call = outputs.pop(0)
        call = call if isinstance(call, Round) else Round(call)
        if isinstance(call.output, Exception):
            raise call.output
        if isinstance(call.output, dict):
            text = json.dumps(call.output)
            return SimpleNamespace(status=call.status, output=[], output_text=text, usage=call.usage)
        return SimpleNamespace(status=call.status, output=[Call(*call.output)], usage=call.usage)

    def runner(settings):
        instance = FoundryRunner.__new__(FoundryRunner)
        instance.deployment = settings.deployment
        instance.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
        instance.project = SimpleNamespace(close=lambda: None)
        instance.credential = SimpleNamespace(close=lambda: None)
        return instance

    monkeypatch.setattr(alsense_chat.app, "FoundryRunner", runner)
    return {"agent_mode": "foundry", "foundry_endpoint": "https://foundry.example.invalid", "deployment": "test"}
