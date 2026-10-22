"""Shared API helpers for tests and evaluations."""

import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from alsense_chat.app import create_app
from alsense_chat.config import Settings


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
