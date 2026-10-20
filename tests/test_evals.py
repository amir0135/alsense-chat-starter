"""Evaluation harness: runs evals/synthetic/*.yaml against the scripted assistant.

Scripted mode asserts exact tool-call sequences and outcome categories. The same case format supports a future
model mode through `allowed_tool_calls` (a subset check) plus required evidence, forbidden actions and outcome
categories, never exact wording. See evals/README.md.
"""

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from helpers import bearer, events, submit

from alsense_chat.app import create_app
from alsense_chat.config import FEATURES, Settings
from alsense_chat.store import Store

ROOT = Path(__file__).resolve().parents[1]
KNOWN_EXPECTATIONS = {
    "status",
    "tool_calls",
    "allowed_tool_calls",
    "must_not",
    "outcome",
    "outcome_kind",
    "resolved_site",
    "error_category",
    "drafts",
    "evidence",
    "text_includes",
    "must_not_mention",
    "submission",
    "options",
    "flow_step",
}


def load_cases() -> list[dict]:
    cases = []
    for path in sorted((ROOT / "evals" / "synthetic").glob("*.yaml")):
        for case in yaml.safe_load(path.read_text())["cases"]:
            cases.append({**case, "suite": path.stem})
    return cases


CASES = load_cases()


def run_case(case: dict, tmp_path) -> dict:
    db = str(tmp_path / f"{case['id']}.sqlite3")
    settings = Settings(
        db=db,
        synthetic_faults=frozenset(case.get("faults", [])),
        features=frozenset(case.get("features", FEATURES)),
    )
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/demo/session", json={"persona": case["user"]})
        assert login.status_code == 200, login.text
        headers = bearer(login.json()["token"])
        replies, drafts, submission = [], [], None
        for step in case["steps"]:
            if "flow" in step:
                message = f"/flow {step['flow']} {json.dumps(step.get('answers', []))}"
            elif "confirm" in step:
                response = submit(client, headers, drafts[-1]["id"])
                submission = response.json()["status"] if response.status_code == 200 else "not_submitted"
                continue
            elif "status" in step:
                message = f"/status {drafts[-1]['id']}"
            else:
                message = step["say"]
            reply = events(client, headers, message)[-1][1]
            replies.append(reply)
            drafts.extend(reply["actions"])
    trail = Store(db).audit_trail()
    calls = [call for record in trail if record["event"] in {"chat", "flow"} for call in record["tools"]]
    submitted = [record for record in trail if record["event"] == "submit" and not record["error_category"]]
    last = replies[-1] if replies else {}
    failures = [call["result"].removeprefix("error:") for call in calls if call["result"].startswith("error:")]
    return {
        "reply": last,
        "tool_calls": [call["tool"] for call in calls],
        "submitted": submitted,
        "submission": submission,
        "error_category": (last.get("error") or {}).get("category") or (failures[-1] if failures else None),
    }


@pytest.mark.parametrize("case", CASES, ids=[f"{case['suite']}:{case['id']}" for case in CASES])
def test_eval_case(case, tmp_path):
    expected = case["expected"]
    assert set(expected) <= KNOWN_EXPECTATIONS, set(expected) - KNOWN_EXPECTATIONS
    result = run_case(case, tmp_path)
    reply, flow = result["reply"], result["reply"].get("flow") or {}
    text = json.dumps(reply, ensure_ascii=False)
    if "status" in expected:
        assert reply["status"] == expected["status"]
    if "tool_calls" in expected:
        assert result["tool_calls"] == expected["tool_calls"]
    if "allowed_tool_calls" in expected:
        assert set(result["tool_calls"]) <= set(expected["allowed_tool_calls"])
    for forbidden in expected.get("must_not", []):
        if forbidden == "submit":
            assert not result["submitted"], "A submission happened without an explicit, successful confirm step."
        else:
            assert forbidden not in result["tool_calls"]
    if "outcome" in expected:
        assert flow.get("category") == expected["outcome"]
    if "outcome_kind" in expected:
        assert flow.get("outcome") == expected["outcome_kind"]
    if "resolved_site" in expected:
        sites = [item["answer"] for item in flow.get("trail", []) if item["label"] == "Site"]
        assert sites and sites[-1] == expected["resolved_site"]
    if "error_category" in expected:
        assert result["error_category"] == expected["error_category"]
    if "drafts" in expected:
        assert len(reply["actions"]) == expected["drafts"]
    for title in expected.get("evidence", []):
        assert title in [
            doc["title"] for source in reply["sources"] if source["tool"] == "search_docs" for doc in source["data"]
        ]
    for snippet in expected.get("text_includes", []):
        assert snippet in reply["text"]
    for snippet in expected.get("must_not_mention", []):
        assert snippet not in text
    if "submission" in expected:
        assert result["submission"] == expected["submission"]
    if "options" in expected:
        assert [option["label"] for option in flow.get("options", [])] == expected["options"]
    if "flow_step" in expected:
        assert flow.get("kind") == expected["flow_step"]


def test_case_ids_are_unique():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
