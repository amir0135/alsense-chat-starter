"""Integration failures are as demonstrable as the happy paths, and each one says exactly what went wrong."""

import json
from types import SimpleNamespace

import pytest
from helpers import app_client, flow, login, reply, report_payload, submit

from alsense_chat.adapters import build_services, synthetic
from alsense_chat.adapters.errors import ModelUnavailable, NotConfiguredError
from alsense_chat.adapters.interfaces import Services
from alsense_chat.adapters.synthetic import FAULTS, with_faults
from alsense_chat.agent import load_agent
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.runner import FoundryRunner
from alsense_chat.store import Store
from alsense_chat.tools import Context


@pytest.mark.parametrize(
    "fault, category, wording, kind",
    [
        ("health_unavailable", "unavailable", "The health-check service is temporarily unavailable.", "ticket"),
        ("api_timeout", "timeout", "The site directory didn't respond in time.", "ticket"),
        ("site_forbidden", "forbidden", "You do not have access to this site.", "resolve"),
        ("site_not_found", "not_found", "The health-check service couldn't find that site.", "ticket"),
        ("health_stale", "stale", "is out of date, so I can't rely on it.", "ticket"),
        ("unauthenticated", "unauthenticated", "Your sign-in is no longer valid", "resolve"),
    ],
)
def test_failed_lookups_stop_the_flow_with_an_explicit_reason(tmp_path, fault, category, wording, kind):
    with app_client(tmp_path, fault) as client:
        result = flow(client, login(client), ["no", "Demo North"])
    assert (result["status"], result["error"]["category"]) == ("error", category)
    assert wording in result["text"]
    assert (result["flow"]["kind"], result["flow"]["outcome"]) == ("outcome", kind)
    # Never pretend the gateway was checked and found fine.
    assert "None open" not in json.dumps(result["flow"]["trail"])
    assert result["text"] != "The gateway looks online, so this needs a closer look. Please prepare a support request."


def test_failure_ticket_is_prefilled_with_what_was_tried(tmp_path):
    with app_client(tmp_path, "health_unavailable") as client:
        result = flow(client, login(client), ["no", "Demo North"])
    assert result["flow"]["ticket"]["site_id"] == "site-a"
    assert "Open gateway offline health check: Couldn't check" in result["flow"]["ticket"]["steps_tried"]


def test_search_without_an_approved_answer_says_so(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        assert reply(client, headers, "/docs compressor calibration")["text"].startswith(
            "No approved guidance matches this issue."
        )
    with app_client(tmp_path, "knowledge_empty") as client:
        empty = reply(client, login(client), "/docs offline sensor")
    assert (empty["status"], empty["sources"][0]["data"]) == ("not_in_data", [])


def test_model_unavailable_is_explicit_and_guided_checks_still_work(tmp_path):
    with app_client(tmp_path, "model_unavailable") as client:
        headers = login(client)
        down = reply(client, headers, "Which sites can I access?")
        assert (down["status"], down["error"]) == ("error", {"category": "unavailable", "service": "model"})
        assert down["text"].startswith("The AI assistant is unavailable right now.")
        assert reply(client, headers, "/flows")["flow"]["kind"] == "menu"
        assert flow(client, headers, [])["flow"]["kind"] == "question"
        history = client.get("/api/session", headers=headers).json()["history"]
        assert history[0]["reply"]["status"] == "error"


def test_foundry_runner_reports_an_unreachable_model_instead_of_falling_back(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    services = synthetic.build()
    user = services.identity.users_me("alice")
    _, session_id = store.create_session(user, 3600)

    def broken(**_):
        raise ConnectionError("network down")

    runner = FoundryRunner.__new__(FoundryRunner)
    runner.deployment = "test"
    runner.client = SimpleNamespace(responses=SimpleNamespace(create=broken))
    with pytest.raises(ModelUnavailable):
        list(runner.run("My sites?", Context(services, store, session_id, user)))


def test_support_outage_leaves_the_draft_prepared_and_says_so(tmp_path):
    with app_client(tmp_path, "support_unavailable") as client:
        headers = login(client, "bob")
        ticket = {"site_id": "site-b", "summary": "Offline", "steps_tried": "Checked power"}
        draft = reply(client, headers, "/ticket " + json.dumps(ticket))["actions"][0]
        response = submit(client, headers, draft["id"])
        assert response.status_code == 503
        assert response.headers["x-error-category"] == "unavailable"
        assert response.json()["detail"] == (
            "Your request was prepared but not submitted. "
            "The support service is temporarily unavailable. Try again in a moment."
        )
        with Store(str(tmp_path / "test.sqlite3")).connection() as db:
            assert db.execute("SELECT result FROM actions WHERE id=?", (draft["id"],)).fetchone()[0] is None


@pytest.mark.parametrize(
    "fault, state, wording",
    [
        ("", "completed", "Your synthetic report is ready."),
        ("report_processing", "processing", "Your report is still being prepared."),
        ("report_failed", "failed", "The report job failed, so no report was produced."),
    ],
)
def test_report_job_states_are_distinct(tmp_path, fault, state, wording):
    faults = (fault,) if fault else ()
    with app_client(tmp_path, *faults) as client:
        headers = login(client)
        draft = reply(client, headers, "/report " + json.dumps(report_payload()))["actions"][0]
        accepted = submit(client, headers, draft["id"]).json()
        assert (accepted["status"], accepted["synthetic"]) == ("accepted", True)
        status = reply(client, headers, f"/status {draft['id']}")
    assert status["sources"][0]["data"]["state"] == state
    assert status["text"].startswith(wording)


def test_status_of_an_unsubmitted_report_says_it_is_not_sent(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        draft = reply(client, headers, "/report " + json.dumps(report_payload()))["actions"][0]
        assert reply(client, headers, f"/status {draft['id']}")["text"].startswith("This report hasn't been confirmed")


@pytest.mark.parametrize(
    "changes, wording",
    [
        ({"start": "2026-02-10", "end": "2026-02-01"}, "The start date must be on or before the end date."),
        ({"start": "2024-01-01", "end": "2026-01-01"}, "The report period is too long."),
        ({"format": "exe"}, "The format has an unsupported value."),
        ({"start": "yesterday"}, "The start date must be a date (YYYY-MM-DD)."),
        ({"url": "https://evil.example"}, "Unexpected fields were supplied."),
    ],
)
def test_invalid_inputs_get_specific_safe_explanations(tmp_path, changes, wording):
    with app_client(tmp_path) as client:
        result = reply(client, login(client), "/report " + json.dumps(report_payload(**changes)))
    assert result["status"] == "clarification"
    assert result["text"].startswith(wording) and result["text"].endswith("Can you correct that and try again?")
    assert result["actions"] == []
    assert "evil.example" not in json.dumps(result) and "yesterday" not in result["text"]


def test_ambiguous_site_asks_which_one_and_continues_with_the_choice(tmp_path):
    with app_client(tmp_path) as client:
        carol = login(client, "carol")
        prompt = flow(client, carol, ["no", "demo east"])
        assert prompt["text"] == "I found more than one matching site. Which one do you mean?"
        assert [option["label"] for option in prompt["flow"]["options"]] == ["Demo East Hall", "Demo East Depot"]
        hall = flow(client, carol, ["no", "demo east", "Demo East Hall"])
        depot = flow(client, carol, ["no", "demo east", "Demo East Depot"])
    assert (hall["flow"]["outcome"], hall["flow"]["trail"][-1]["answer"]) == ("resolve", "Secondary Gateway Offline")
    assert (depot["flow"]["outcome"], depot["flow"]["category"]) == ("ticket", "needs_investigation")


def test_specific_descriptions_still_get_a_single_suggestion(tmp_path):
    with app_client(tmp_path) as client:
        prompt = flow(client, login(client, "carol"), ["no", "east hall"])
    assert (prompt["flow"]["kind"], prompt["text"]) == ("confirm", "Do you mean site Demo East Hall?")


def test_feature_flags_turn_capabilities_off_everywhere(tmp_path):
    with app_client(tmp_path, features=frozenset({"troubleshooting"})) as client:
        headers = login(client)
        assert client.get("/api/health").json()["features"] == ["troubleshooting"]
        assert reply(client, headers, "/reports")["text"] == "Reports are turned off in this demo."
        assert client.get("/api/report-types", headers=headers).status_code == 403
        ticket = {"site_id": "site-a", "summary": "Offline", "steps_tried": "Checked power"}
        assert reply(client, headers, "/ticket " + json.dumps(ticket))["status"] == "out_of_scope"
    with app_client(tmp_path, features=frozenset({"reports"})) as client:
        assert reply(client, login(client), "/flows")["text"] == "Guided checks are turned off in this demo."
    every_domain = [module.name for module in load_agent().active()]
    assert load_agent().model_tools(every_domain, frozenset({"troubleshooting"})) == (
        "get_permitted_sites",
        "search_docs",
    )


def test_faults_are_validated_and_only_wrap_synthetic_services(tmp_path):
    with pytest.raises(ValueError, match="unknown names"):
        create_app(Settings(db=str(tmp_path / "x.sqlite3"), synthetic_faults=frozenset({"meteor_strike"})))
    with pytest.raises(ValueError, match="only applies to DATA_SOURCE=synthetic"):
        Settings(data_source="alsense", synthetic_faults=frozenset({"api_timeout"})).validate()
    base = synthetic.build()
    real = Services("alsense", base.identity, base.sites, base.knowledge, base.health, base.reports, base.support)
    with pytest.raises(ValueError, match="synthetic services only"):
        with_faults(real, frozenset({"api_timeout"}))
    assert with_faults(base, frozenset()) is base
    assert set(FAULTS) >= {"api_timeout", "site_forbidden", "health_stale", "report_failed", "model_unavailable"}


def test_real_adapters_fail_clearly_instead_of_falling_back(tmp_path):
    with pytest.raises(NotConfiguredError, match="placeholder adapters"):
        build_services("alsense")
    with pytest.raises(NotConfiguredError, match="placeholder adapters"):
        create_app(Settings(db=str(tmp_path / "x.sqlite3"), data_source="alsense"))
    with pytest.raises(ValueError, match="DATA_SOURCE"):
        build_services("mainframe")


def test_categorized_failures_are_distinguishable_from_missing_drafts_and_expired_sessions(tmp_path):
    with app_client(tmp_path, "site_not_found") as client:
        headers = login(client)
        draft = reply(client, headers, "/report " + json.dumps(report_payload()))["actions"][0]
        upstream = submit(client, headers, draft["id"])
        missing = submit(client, headers, "no-such-draft")
    assert upstream.status_code == missing.status_code == 404
    assert upstream.headers["x-error-category"] == "not_found"
    assert upstream.json()["detail"].startswith("Your request was prepared but not submitted.")
    assert "x-error-category" not in missing.headers
    with app_client(tmp_path, "unauthenticated") as client:
        response = client.get("/api/report-types", headers=login(client))
    assert (response.status_code, response.headers["x-error-category"]) == (401, "unauthenticated")


def test_a_partial_answer_picks_one_of_several_matching_sites(tmp_path):
    with app_client(tmp_path) as client:
        carol = login(client, "carol")
        depot = flow(client, carol, ["no", "demo east", "the depot"])
        still_both = flow(client, carol, ["no", "demo east", "east"])
        hall = flow(client, carol, ["no", "demo east", "east", "hall"])
    assert (depot["flow"]["outcome"], depot["flow"]["trail"][1]["answer"]) == ("ticket", "Demo East Depot")
    assert still_both["text"] == "I found more than one matching site. Which one do you mean?"
    assert hall["flow"]["trail"][1]["answer"] == "Demo East Hall"
