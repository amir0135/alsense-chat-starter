"""Opening or filtering the alarms dashboard isn't implemented here: the alarms and frontend-action modules are
planned, with no tools. Nothing may open or filter anything, and no reply may claim to have done so."""

import json
import re

import pytest
from helpers import app_client, foundry, login, reply

from alsense_chat.adapters import synthetic
from alsense_chat.agent import load_agent
from alsense_chat.runner import guard
from alsense_chat.store import Store
from alsense_chat.tools import Context

DASHBOARD = ("Open the alarms dashboard", "Filter critical and high alarms")
REPORT = "Create a temperature report for Demo North for the last seven days"
CLAIMED = re.compile(r"\b(?:opened|filtered|applied)\b", re.IGNORECASE)


def evidence(tmp_path) -> tuple[Context, dict]:
    """A turn that looked up the user's sites and the guidance: what a released claim would cite."""
    services = synthetic.build()
    user = services.identity.users_me("alice")
    store = Store(str(tmp_path / "guard.sqlite3"))
    _, session_id = store.create_session(user, 3600)
    ctx = Context(services, store, session_id, user)
    sources = {"sites": ctx.execute("get_permitted_sites", {}), "docs": ctx.execute("search_docs", {"query": "alarm"})}
    return ctx, {name: output["source_id"] for name, output in sources.items()}


@pytest.mark.parametrize("message", DASHBOARD)
@pytest.mark.parametrize("previous", [(), ("sites", "reporting")])
def test_dashboard_requests_are_routed_to_existing_domains_only(message, previous):
    agent = load_agent()
    routing = agent.route(message, previous)
    assert not {"alarms", "frontend_actions"} & set(routing.domains)
    assert set(routing.tools) <= set(agent.tools("model"))


@pytest.mark.parametrize("message", DASHBOARD)
def test_the_scripted_assistant_neither_opens_nor_filters_anything(tmp_path, message):
    with app_client(tmp_path) as client:
        headers = login(client)
        fresh = reply(client, headers, message)
        reply(client, headers, REPORT)
        # After a report request the same words read as a follow-up to it: still nothing is opened or filtered.
        follow_up = reply(client, headers, message)
    for answer in (fresh, follow_up):
        assert answer["actions"] == [] and not CLAIMED.search(answer["text"])
        assert {source["tool"] for source in answer["sources"]} <= {
            "get_permitted_sites",
            "search_docs",
            "list_report_types",
        }


def test_a_model_call_to_a_dashboard_tool_is_refused(tmp_path, monkeypatch):
    requests: list[dict] = []
    outputs = [
        ("open_alarms_dashboard", {"severity": ["critical", "high"]}),
        {"status": "out_of_scope", "text": "I can't open or filter the alarms dashboard here.", "source_ids": []},
    ]
    with app_client(tmp_path, **foundry(monkeypatch, outputs, requests)) as client:
        answer = reply(client, login(client), DASHBOARD[1])
    offered = [tool["name"] for tool in requests[0]["tools"]]
    assert offered == list(load_agent().route(DASHBOARD[1]).tools)
    [output] = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
    assert json.loads(output["output"]) == {"error": "Unknown tool.", "category": "unknown_tool"}
    assert (answer["status"], answer["actions"], answer["sources"]) == ("out_of_scope", [], [])


@pytest.mark.parametrize(
    "text",
    [
        "I've opened the alarms dashboard for Demo North.",
        "I have filtered the alarms to critical and high.",
        "The critical and high filter is now applied.",
        "The alarms dashboard is now open.",
        "I applied the critical filter to the alarm view.",
        "The alarm list for Demo North has been filtered to critical and high.",
        "Done! I've switched to the alarms view.",
    ],
)
def test_the_reply_guard_refuses_a_claim_that_the_dashboard_was_opened_or_filtered(tmp_path, text):
    ctx, sources = evidence(tmp_path)
    with pytest.raises(ValueError):
        guard(json.dumps({"status": "answer", "text": text, "source_ids": [sources["sites"]]}), ctx)


@pytest.mark.parametrize(
    "text",
    [
        "Check whether the door was opened during the alarm.",
        "If the cabinet door was opened, close it and wait for the temperature to recover.",
        "Check that the filter is clean.",
        "The alarm shows the door has been opened for too long.",
        "Make sure the filter has been applied before you compare the readings.",
        "Open the alarms dashboard and filter by severity to see critical and high alarms.",
        "I have not filtered the alarms.",
    ],
)
def test_the_reply_guard_releases_guidance_and_denials(tmp_path, text):
    ctx, sources = evidence(tmp_path)
    assert guard(json.dumps({"status": "answer", "text": text, "source_ids": [sources["docs"]]}), ctx).text == text
