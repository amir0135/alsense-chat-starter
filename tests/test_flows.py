import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.flows import FlowError, load_flows, validate_flow
from alsense_chat.source import MockAlsenseSource
from alsense_chat.store import Store
from alsense_chat.tools import Context, model_definition

CUSTOM_FLOWS = """flows:
  custom-check:
    title: Custom check
    triggers:
      - [dashboard, no data]
    start: ready
    nodes:
      ready:
        ask: Ready?
        yes: done
        no: done
      done:
        outcome: Finished.
"""


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(db=str(tmp_path / "flows.sqlite3")))) as client:
        yield client


def login(client, persona="alice"):
    token = client.post("/api/demo/session", json={"persona": persona}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def chat(client, headers, message):
    response = client.post("/api/chat", headers=headers, json={"message": message})
    assert response.status_code == 200
    frames = [frame.split("\n", 1) for frame in response.text.strip().split("\n\n")]
    events = [(name.removeprefix("event: "), json.loads(data.removeprefix("data: "))) for name, data in frames]
    assert events[-1][0] == "reply", events
    return events


def step(client, headers, answers, flow="site-connection"):
    return chat(client, headers, f"/flow {flow} {json.dumps(answers)}")[-1][1]


def test_sample_flows_load_and_the_menu_lists_them(client):
    flows = load_flows()
    menu = chat(client, login(client), "/flows")[-1][1]
    assert menu["status"] == "clarification"
    assert menu["flow"]["kind"] == "menu"
    assert [option["value"] for option in menu["flow"]["options"]] == list(flows) == ["site-connection"]


@pytest.mark.parametrize(
    "broken, problem",
    [
        ({"title": "x", "start": "a", "nodes": {"a": {"ask": "Q?", "yes": "b", "no": "a"}}}, "must name a node"),
        (
            {"title": "x", "start": "a", "nodes": {"a": {"ask": "Q?", "outcome": "Done", "yes": "a", "no": "a"}}},
            "exactly one",
        ),
        (
            {
                "title": "x",
                "start": "a",
                "nodes": {
                    "a": {"check": "database", "any_open": ["x"], "yes": "b", "no": "b"},
                    "b": {"outcome": "Done"},
                },
            },
            "check must be",
        ),
        (
            {
                "title": "x",
                "start": "a",
                "nodes": {
                    "a": {"ask": "Q?", "yes": "b", "no": "b"},
                    "b": {"ask": "Again?", "yes": "a", "no": "c"},
                    "c": {"outcome": "Done"},
                },
            },
            "loops back",
        ),
        ({"title": "x", "start": "a", "nodes": {"a": {"outcome": "Done", "kind": "email"}}}, "resolve or ticket"),
    ],
)
def test_invalid_flows_fail_when_loaded(broken, problem):
    with pytest.raises(FlowError, match=problem):
        validate_flow("broken", broken)


def test_bare_yaml_yes_no_keys_are_accepted(tmp_path):
    path = tmp_path / "flows.local.yaml"
    path.write_text(CUSTOM_FLOWS)
    assert load_flows(str(path))["custom-check"]["nodes"]["ready"]["yes"] == "done"


def test_flow_suggests_a_site_and_checks_its_health_in_the_backend(client):
    alice = login(client)
    first = step(client, alice, [])
    assert (first["status"], first["flow"]["kind"], first["flow"]["step"]) == ("clarification", "question", 1)
    site_prompt = step(client, alice, ["no"])
    assert site_prompt["flow"]["kind"] == "site" and site_prompt["flow"]["free_text"] is True
    assert [option["label"] for option in site_prompt["flow"]["options"]] == ["Demo North"]
    confirm = step(client, alice, ["no", "demo nrth"])
    assert (confirm["flow"]["kind"], confirm["text"]) == ("confirm", "Do you mean site Demo North?")

    events = chat(client, alice, "/flow site-connection " + json.dumps(["no", "demo nrth", "yes"]))
    assert ("step", {"tool": "check_site_health", "state": "done"}) in events
    result = events[-1][1]
    assert (result["flow"]["kind"], result["flow"]["outcome"]) == ("outcome", "resolve")
    assert result["flow"]["trail"][-2:] == [
        {"label": "Site", "answer": "Demo North"},
        {"label": "Open gateway offline health check", "answer": "Primary Gateway Offline"},
    ]
    assert result["source_ids"] == [result["sources"][-1]["source_id"]]
    assert result["sources"][-1]["tool"] == "check_site_health"


def test_ticket_outcome_prefills_a_support_request_without_an_alarm(client, tmp_path):
    bob = login(client, "bob")
    result = step(client, bob, ["no", "Demo South"])
    assert result["flow"]["outcome"] == "ticket"
    ticket = result["flow"]["ticket"]
    assert (ticket["site_id"], ticket["summary"]) == ("site-b", "Site seems disconnected")
    assert "3. Open gateway offline health check: None open" in ticket["steps_tried"]

    draft = chat(client, bob, "/ticket " + json.dumps(ticket))[-1][1]
    assert draft["actions"][0]["kind"] == "ticket"
    assert "alarm_id" not in draft["actions"][0]["payload"]
    with Store(str(tmp_path / "flows.sqlite3")).connection() as db:
        categories = {row["category"] for row in db.execute("SELECT category FROM unanswered")}
    assert "flow_ticket:site-connection:investigate" in categories


def test_site_identification_stops_after_three_attempts_and_stays_in_tenant(client):
    alice = login(client)
    assert step(client, alice, ["no", "Demo South"])["text"] == "Do you mean site Demo North?"
    result = step(client, alice, ["no", "Demo South", "no", "zzz", "qqq"])
    assert result["flow"]["outcome"] == "ticket"
    assert result["text"].startswith("Sorry, I couldn't identify the site")
    assert {"label": "Site", "answer": "Not identified"} in result["flow"]["trail"]
    assert "site-b" not in json.dumps(result)
    assert "Demo South" not in json.dumps(result["sources"])


def test_health_check_is_a_backend_only_flow_tool(tmp_path):
    source = MockAlsenseSource()
    ctx = Context(source, Store(str(tmp_path / "context.sqlite3")), "session", source.users_me("alice"))
    assert "check_site_health" not in model_definition()["tools"]
    assert ctx.execute("check_site_health", {"site_id": "site-a"}) == {"error": "Unknown tool."}
    assert ctx.execute("check_site_health", {"site_id": "site-a"}, flow=True)["data"][0]["status"] == "open"
    assert "error" in ctx.execute("check_site_health", {"site_id": "site-b"}, flow=True)


def test_free_text_triggers_start_a_flow_without_capturing_other_questions(client):
    alice = login(client)
    reply = chat(client, alice, "Our site is unreachable since this morning")[-1][1]
    assert (reply["flow"]["id"], reply["flow"]["step"]) == ("site-connection", 1)
    guidance = chat(client, alice, "My sensor is offline. What should I check?")[-1][1]
    assert "flow" not in guidance
    assert guidance["sources"][0]["tool"] == "search_docs"


def test_bad_flow_requests_ask_again_instead_of_failing(client):
    alice = login(client)
    assert chat(client, alice, "/flow unknown-flow")[-1][1]["status"] == "clarification"
    assert chat(client, alice, "/flow site-connection not-json")[-1][1]["status"] == "clarification"
    assert chat(client, alice, "/flow site-connection " + json.dumps(["x" * 500]))[-1][1]["status"] == "clarification"
    again = step(client, alice, ["maybe"])
    assert (again["flow"]["kind"], again["flow"]["answers"]) == ("question", [])


def test_flows_file_replaces_the_sample_flows(tmp_path):
    path = tmp_path / "flows.local.yaml"
    path.write_text(CUSTOM_FLOWS)
    with TestClient(create_app(Settings(db=str(tmp_path / "custom.sqlite3"), flows_file=str(path)))) as client:
        headers = login(client)
        menu = chat(client, headers, "/flows")[-1][1]
        assert [option["value"] for option in menu["flow"]["options"]] == ["custom-check"]
        assert chat(client, headers, "The dashboard shows no data")[-1][1]["flow"]["id"] == "custom-check"
    with pytest.raises(ValueError, match="FLOWS_FILE"):
        Settings(flows_file=str(tmp_path / "missing.yaml")).validate()


def test_local_flow_files_stay_out_of_git_and_images():
    root = Path(__file__).resolve().parents[1]
    assert "*.local.yaml" in (root / ".gitignore").read_text()
    assert "*.local.yaml" in (root / ".dockerignore").read_text()
