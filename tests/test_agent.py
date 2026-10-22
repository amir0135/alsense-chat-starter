"""Modular agent configuration: each request is offered only the instructions and tools of its domains."""

import shutil
from pathlib import Path

import pytest
import yaml
from helpers import app_client, events, login

from alsense_chat.agent import AGENT_ROOT, AgentConfigError, load_agent
from alsense_chat.store import Store
from alsense_chat.tools import check_registry

REPORTING = (
    "get_permitted_sites",
    "list_report_types",
    "prepare_report",
    "prepare_scheduled_report",
    "get_report_status",
)


@pytest.mark.parametrize(
    ("message", "domains", "tools"),
    [
        ("Create a temperature report for Store North for the last seven days.", ("sites", "reporting"), REPORTING),
        ("Schedule a weekly alarm history report", ("sites", "reporting"), REPORTING),
        ("Which sites can I access?", ("sites",), ("get_permitted_sites",)),
        ("My sensor is offline. What should I check?", ("sites", "troubleshooting"), None),
        ('/report {"site_id":"site-a"}', ("sites", "reporting"), REPORTING),
        ("/sites Demo North", ("sites",), ("get_permitted_sites",)),
    ],
)
def test_requests_are_routed_to_their_domains(message, domains, tools):
    routing = load_agent().route(message)
    assert routing.domains == domains
    if tools:
        assert routing.tools == tools
    assert "prepare_support_ticket" not in routing.tools or "troubleshooting" in domains


def test_follow_ups_keep_the_previous_domains_and_new_conversations_use_the_defaults():
    agent = load_agent()
    assert agent.route("Make it Excel instead", ["sites", "reporting"]).reason == "previous"
    assert agent.route("Make it Excel instead", ["sites", "reporting"]).domains == ("sites", "reporting")
    fresh = agent.route("Hello there")
    assert (fresh.reason, fresh.domains) == ("default", ("sites", "troubleshooting"))


def test_report_requests_never_see_other_domains_tools():
    agent = load_agent()
    routing = agent.route("Create a temperature report for Demo North for the last seven days")
    other = {name for name, tool in agent.tools().items() if tool.domain not in routing.domains}
    assert other and not other & set(routing.tools)
    assert "confirm_report" not in routing.tools, "confirmation is the user's action, never a model tool"
    instructions = agent.instructions(routing.domains)
    assert "Reports" in instructions and "Sites" in instructions and "guided check" not in instructions


def test_switched_off_features_remove_their_tools():
    routing = load_agent().route("Create a temperature report", features=frozenset({"troubleshooting", "support"}))
    assert routing.domains == ("sites", "reporting") and routing.tools == ("get_permitted_sites",)


def test_future_modules_are_described_but_never_offered():
    agent = load_agent()
    assert [module.name for module in agent.future()] == ["alarms", "energy", "frontend_actions"]
    assert all(not module.tools for module in agent.future())
    assert agent.route("Show me the energy use and alarms").domains == ("sites", "troubleshooting")


def test_every_declared_tool_has_a_matching_handler():
    agent = check_registry()
    assert {tool.domain for tool in agent.tools().values()} == {"sites", "reporting", "troubleshooting"}


@pytest.fixture
def agent_copy(tmp_path) -> Path:
    root = tmp_path / "agent"
    shutil.copytree(AGENT_ROOT, root, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return root


def edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def duplicate_tool(data: dict) -> None:
    parameters = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    data["tools"]["search_docs"] = {"caller": "model", "description": "Duplicate.", "parameters": parameters}


def test_an_unchanged_copy_of_the_configuration_loads(agent_copy):
    assert [module.name for module in load_agent(agent_copy).active()] == ["sites", "reporting", "troubleshooting"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda root: shutil.rmtree(root / "energy"), "must list each domain folder once"),
        (lambda root: edit(root / "sites" / "tools.yaml", duplicate_tool), "already declared"),
        (
            lambda root: edit(root / "reporting" / "instructions.yaml", lambda data: data.update(requires=["alarms"])),
            "unknown or inactive",
        ),
        (lambda root: (root / "alarms" / "tools.yaml").write_text("tools: {}\n"), "description only"),
        (
            lambda root: edit(
                root / "sites" / "tools.yaml", lambda data: data["tools"]["get_permitted_sites"].update(caller="robot")
            ),
            "caller",
        ),
        (
            lambda root: edit(
                root / "core" / "instructions.yaml", lambda data: data.update(default_domains=["energy"])
            ),
            "default_domains",
        ),
    ],
)
def test_configuration_mistakes_stop_the_app_with_a_clear_message(agent_copy, change, message):
    change(agent_copy)
    with pytest.raises(AgentConfigError, match=message):
        load_agent(agent_copy)


def test_each_chat_turn_audits_its_domains_and_offered_tools(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        events(client, headers, "Create a temperature report for Demo North for the last seven days")
        events(client, headers, "Which sites can I access?")
    turns = [record for record in Store(str(tmp_path / "test.sqlite3")).audit_trail() if record["event"] == "chat"]
    assert [record["domains"] for record in turns] == [["sites", "reporting"], ["sites"]]
    assert turns[0]["offered_tools"] == list(REPORTING)
    assert turns[1]["offered_tools"] == ["get_permitted_sites"]
