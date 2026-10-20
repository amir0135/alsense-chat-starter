"""Every shipped scenario starts cleanly and produces the result its card promises."""

import json
from datetime import date, timedelta

import pytest
import yaml
from helpers import app_client, bearer, events, login, submit

from alsense_chat.adapters.synthetic.services import PERSONAS
from alsense_chat.scenarios import SAMPLE_SCENARIOS, ScenarioError, load_scenarios

SCENARIOS = yaml.safe_load(SAMPLE_SCENARIOS.read_text())["scenarios"]


def start(client, scenario_id: str, headers: dict | None = None, **body):
    return client.post(f"/api/demo/scenarios/{scenario_id}/start", headers=headers or {}, json=body)


def default_report(site: str) -> dict:
    today = date.today()
    return {
        "report_type": "alarm-history",
        "format": "pdf",
        "start": (today - timedelta(days=29)).isoformat(),
        "end": today.isoformat(),
        "site_id": site,
    }


def drive(client, scenario: dict) -> dict:
    """Run a scenario's steps through the API the way the scenario coach does in the browser."""
    session = start(client, scenario["id"]).json()
    headers = bearer(session["token"])
    site = PERSONAS[scenario["persona"]]["sites"][0][0]
    state: dict = {"replies": [], "flow": None, "draft": None, "action": None, "submissions": [], "replaced": []}

    def say(message: str) -> None:
        result = events(client, headers, message)[-1][1]
        state["replies"].append(result)
        if result.get("flow"):
            state["flow"] = result["flow"]
        if result["actions"]:
            if state["action"]:
                state["replaced"].append(state["action"])
            state["action"] = result["actions"][0]

    for step in scenario["steps"]:
        if "say" in step:
            say(step["say"])
        elif "answer" in step:
            say(f"/flow {state['flow']['id']} {json.dumps([*state['flow']['answers'], step['answer']])}")
        elif "open" in step:
            defaults = default_report(site) if step["open"] == "report" else {"site_id": site}
            state["draft"] = {"kind": step["open"], **defaults, **step.get("prefill", {})}
        elif "set" in step:
            state["draft"].update(step["set"])
        elif step["click"] == "support":
            state["draft"] = {"kind": "ticket", **state["flow"]["ticket"]}
        elif step["click"] == "edit":
            state["draft"] = {"kind": "report", **state["action"]["payload"]}
        elif step["click"] == "prepare":
            draft = dict(state["draft"])
            kind = draft.pop("kind")
            say(f"/{kind} {json.dumps(draft)}")
        elif step["click"] == "confirm":
            state["submissions"].append(submit(client, headers, state["action"]["id"]))
        elif step["click"] == "status":
            say(f"/status {state['action']['id']}")
    state["last"] = state["replies"][-1] if state["replies"] else None
    return state


EXPECTATIONS = {
    "alice-unreachable-site": lambda s: (
        (s["last"]["flow"]["outcome"], s["last"]["flow"]["category"]) == ("resolve", "connectivity_guidance")
    ),
    "bob-support-request": lambda s: (
        s["submissions"][-1].json()["status"] == "received"
        and s["last"]["actions"][0]["payload"]["summary"] == "Site seems disconnected"
    ),
    "report-lifecycle": lambda s: (
        s["last"]["text"] == "Your synthetic report is ready."
        and s["action"]["payload"]["format"] == "csv"
        and s["replaced"][0]["payload"]["format"] == "pdf"
    ),
    "ambiguous-site": lambda s: (
        s["replies"][-2]["text"].startswith("I found more than one matching site")
        and s["last"]["flow"]["trail"][-1]["answer"] == "Secondary Gateway Offline"
    ),
    "invalid-date-range": lambda s: (
        s["last"]["status"] == "clarification"
        and s["last"]["text"].startswith("The start date must be on or before the end date.")
        and s["action"] is None
    ),
    "unavailable-service": lambda s: (
        s["last"]["error"]["category"] == "unavailable" and s["last"]["flow"]["outcome"] == "ticket"
    ),
    "support-unavailable": lambda s: (
        s["submissions"][-1].status_code == 503
        and s["submissions"][-1].json()["detail"].startswith("Your request was prepared but not submitted.")
    ),
    "report-failed": lambda s: s["last"]["text"].startswith("The report job failed"),
    "site-access-revoked": lambda s: s["last"]["text"].startswith("You do not have access to this site."),
    "no-approved-answer": lambda s: (
        s["last"]["status"] == "not_in_data"
        and s["last"]["text"].startswith("No approved guidance matches this issue.")
    ),
    "model-unavailable": lambda s: (
        s["replies"][0]["error"]["service"] == "model" and s["last"]["flow"]["kind"] == "question"
    ),
}


def test_every_shipped_scenario_has_an_expectation():
    assert {scenario["id"] for scenario in SCENARIOS} == set(EXPECTATIONS)
    groups = {scenario["group"] for scenario in SCENARIOS}
    assert groups == {"journey", "failure"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_scenario_produces_its_expected_result(tmp_path, scenario):
    with app_client(tmp_path) as client:
        state = drive(client, scenario)
    assert EXPECTATIONS[scenario["id"]](state), state["last"]


def test_scenarios_list_who_starts_and_what_to_expect(tmp_path):
    with app_client(tmp_path) as client:
        listed = {item["id"]: item for item in client.get("/api/scenarios").json()}
    alice = listed["alice-unreachable-site"]
    assert (alice["persona_name"], alice["persona_sites"]) == ("Synthetic Alice", ["Demo North"])
    assert alice["demonstrates"] and alice["expected"] and alice["unavailable_reason"] == ""
    assert listed["unavailable-service"]["faults"] == ["health_unavailable"]


def test_reset_starts_a_fresh_session_with_the_same_setup(tmp_path):
    with app_client(tmp_path) as client:
        first = bearer(start(client, "unavailable-service").json()["token"])
        events(client, first, "/flow site-connection []")
        again = start(client, "unavailable-service").json()
        assert (again["user"]["id"], again["scenario"]) == ("alice", "unavailable-service")
        assert client.get("/api/session", headers=bearer(again["token"])).json()["history"] == []
        fresh = client.post("/api/session/new", headers=bearer(again["token"])).json()
        result = events(client, bearer(fresh["token"]), '/flow site-connection ["no", "Demo North"]')[-1][1]
        assert result["error"]["category"] == "unavailable"
        assert client.get("/api/session", headers=bearer(fresh["token"])).json()["scenario"] == "unavailable-service"


def test_faults_stay_in_their_scenario_session(tmp_path):
    with app_client(tmp_path) as client:
        start(client, "unavailable-service")
        plain = login(client)
        result = events(client, plain, '/flow site-connection ["no", "Demo North"]')[-1][1]
    assert result["flow"]["outcome"] == "resolve"


def test_scenarios_that_need_missing_flows_are_unavailable(tmp_path):
    custom = tmp_path / "flows.local.yaml"
    custom.write_text(
        'flows:\n  other:\n    title: Other\n    version: "1.0.0"\n    status: draft\n    owner: Ops\n'
        "    approved_for_demo: true\n    start: done\n    nodes:\n      done:\n        outcome: Done.\n"
        "        kind: resolve\n"
    )
    with app_client(tmp_path, flows_file=str(custom)) as client:
        listed = {item["id"]: item for item in client.get("/api/scenarios").json()}
        assert "site-connection" in listed["alice-unreachable-site"]["unavailable_reason"]
        assert listed["report-lifecycle"]["unavailable_reason"] == ""
        assert start(client, "alice-unreachable-site").status_code == 409
        assert start(client, "no-such-scenario").status_code == 404


def test_hosted_demo_scenarios_need_the_access_code_or_a_session(tmp_path):
    code = "synthetic-code-with-at-least-32-characters"
    with app_client(tmp_path, cloud_demo=True, demo_access_code=code) as client:
        assert start(client, "report-lifecycle").status_code == 401
        assert start(client, "report-lifecycle", bearer("not-a-session")).status_code == 401
        session = start(client, "report-lifecycle", access_code=code)
        assert session.status_code == 200 and code not in session.text
        assert start(client, "bob-support-request", bearer(session.json()["token"])).status_code == 200


def test_bad_scenario_files_fail_with_every_problem(tmp_path):
    path = tmp_path / "scenarios.local.yaml"
    path.write_text(
        "scenarios:\n"
        "  - {id: one, title: One, group: journey, demonstrates: x, persona: mallory, expected: y,"
        " faults: [gremlins], steps: [{note: n, say: hi}]}\n"
        "  - {id: two, title: Two, group: journey, demonstrates: x, persona: alice, expected: y,"
        " steps: [{note: n, say: hi, click: confirm}]}\n"
    )
    with pytest.raises(ScenarioError) as error:
        load_scenarios(path, {"alice"})
    message = str(error.value)
    assert "unknown persona 'mallory'" in message and "unknown faults ['gremlins']" in message
    assert "exactly one of" in message
