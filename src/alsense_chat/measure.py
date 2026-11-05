"""One measured run of the proxy flows, in-process, followed by the latency and usage reports.

    uv run python scripts/dev.py measure [--runs N] [--db PATH]

Each run signs in a fresh synthetic session, asks "Which sites can I access?", asks for a temperature report for
Demo North for the last seven days, confirms the draft and reads its status. The assistant is the configured one
(AGENT_MODE): the scripted assistant by default. With AGENT_MODE=foundry every question makes real, billed calls to
the configured model deployment; that needs FOUNDRY_PROJECT_ENDPOINT, MODEL_DEPLOYMENT and an Azure sign-in such as
`az login`. Data is always synthetic: DATA_SOURCE=alsense is refused. Without --db the run uses a fresh temporary
database, so the reports show this run only.
"""

import argparse
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from . import timing, usage
from .app import create_app
from .config import load_settings

SITES = "Which sites can I access?"
REPORT = "Create a temperature report for Demo North for the last seven days"
PERSONA = "alice"


def chat(client: TestClient, headers: dict, message: str) -> dict | None:
    """The reply to one question, or None when the run failed."""
    response = client.post("/api/chat", headers=headers, json={"message": message})
    if response.status_code != 200:
        return None
    frames = [frame.split("\n", 1) for frame in response.text.strip().split("\n\n") if "\n" in frame]
    replies = [json.loads(data.removeprefix("data: ")) for name, data in frames if name == "event: reply"]
    return replies[-1] if replies else None


def run_once(client: TestClient, access_code: str) -> str:
    """One pass through the flows; returns a one-line outcome."""
    login = client.post("/api/demo/session", json={"persona": PERSONA, "access_code": access_code})
    if login.status_code != 200:
        return f"sign-in failed ({login.status_code})"
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    sites = chat(client, headers, SITES)
    report = chat(client, headers, REPORT)
    outcome = f"sites: {(sites or {}).get('status', 'failed')}, report: {(report or {}).get('status', 'failed')}"
    actions = (report or {}).get("actions") or []
    if not actions:
        return outcome + ", no draft to confirm"
    path = f"/api/actions/{actions[0]['id']}"
    confirmed = client.post(f"{path}/submit", headers=headers, json={"confirmed": True})
    status = client.get(f"{path}/status", headers=headers)
    state = status.json().get("state", "unavailable") if status.status_code == 200 else f"error {status.status_code}"
    return outcome + f", confirm: {confirmed.status_code}, status: {state}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev.py measure", description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=5, help="passes through the flows (default 5)")
    parser.add_argument("--db", help="session database to write and report on (default: a fresh temporary one)")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    if settings.data_source != "synthetic":
        print("Measurements use synthetic data only. Set DATA_SOURCE=synthetic.")
        return 2
    if not settings.demo_login:
        print("Measurements sign in with a synthetic identity. Set DEMO_LOGIN=true.")
        return 2
    with tempfile.TemporaryDirectory() as folder:
        db = args.db or str(Path(folder) / "measure.sqlite3")
        # Warnings, such as a throttled model call being retried, still show while the run is in progress.
        settings = dataclasses.replace(settings, db=db, reset_on_start=False, log_level="WARNING")
        model = settings.agent_mode == "foundry"
        print(f"Measuring {args.runs} run(s) with the {'model' if model else 'scripted'} assistant on synthetic data.")
        if model:
            print(
                f"Model mode: every question makes real, billed calls to the deployment {settings.deployment}. "
                "It needs FOUNDRY_PROJECT_ENDPOINT, MODEL_DEPLOYMENT and an Azure sign-in (az login)."
            )
        with TestClient(create_app(settings)) as client:
            for number in range(1, args.runs + 1):
                print(f"run {number}: {run_once(client, settings.demo_access_code)}", flush=True)
        print()
        label = args.db or "this run's temporary database"
        print(timing.report(timing.read(db, 100_000), label))
        print(usage.render(usage.summarize(usage.read(db, 0.0, None)), label, None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
