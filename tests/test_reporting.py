"""On-demand and scheduled reports: validation against the catalog, confirmation, status and link expiry."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from helpers import app_client, events, login, submit

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.contracts import ReportType, ScheduleSupport
from alsense_chat.adapters.synthetic.services import SyntheticReports
from alsense_chat.runner import guard
from alsense_chat.store import Store
from alsense_chat.tools import Context

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def schedule(**changes) -> dict:
    return {
        "site_id": "site-a",
        "report_type": "temperature-log",
        "format": "xlsx",
        "frequency": "weekly",
        "recipients": ["operations@example.com", "quality@example.com"],
        "start_date": TOMORROW,
        **changes,
    }


def context(tmp_path, services=None) -> Context:
    source = services or synthetic.build()
    user = source.identity.users_me("alice")
    store = Store(str(tmp_path / "reports.sqlite3"))
    _, session_id = store.create_session(user, 3600)
    return Context(source, store, session_id, user)


def test_a_valid_schedule_becomes_a_draft_and_nothing_else(tmp_path):
    ctx = context(tmp_path)
    output = ctx.execute("prepare_scheduled_report", schedule(recipients=["Ops@example.com", "ops@example.com"]))
    [action] = ctx.actions
    assert action["kind"] == "scheduled_report"
    assert action["payload"]["recipients"] == ["Ops@example.com"]
    assert output["data"]["id"] == action["id"]
    assert ctx.calls[-1]["inputs"]["recipients"] == 1, "the audit counts recipients and never stores addresses"


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"start_date": (date.today() - timedelta(days=1)).isoformat()}, "past"),
        ({"end_date": date.today().isoformat(), "start_date": TOMORROW}, "end"),
        ({"report_type": "site-status", "format": "pdf"}, "can't be scheduled"),
        ({"report_type": "alarm-history", "format": "pdf", "frequency": "daily"}, "daily"),
        ({"report_type": "pressure-log"}, "report type"),
        ({"format": "docx"}, ""),
        ({"recipients": []}, ""),
        ({"recipients": ["not-an-address"]}, ""),
        ({"recipients": [f"person{index}@example.com" for index in range(11)]}, "10"),
        ({"site_id": "site-b"}, ""),
    ],
)
def test_invalid_schedules_are_refused_without_a_draft(tmp_path, changes, problem):
    ctx = context(tmp_path)
    output = ctx.execute("prepare_scheduled_report", schedule(**changes))
    assert output["category"] in {"invalid_input", "forbidden"}, output
    assert problem.casefold() in output["error"].casefold()
    assert ctx.actions == []


def test_an_end_date_is_refused_when_the_report_type_does_not_support_one(tmp_path):
    class Reports(SyntheticReports):
        def report_types(self, user):
            plain = ScheduleSupport(frequencies=["weekly"], end_date_supported=False, max_recipients=5)
            return [ReportType(id="temperature-log", label="Temperature log", formats=["pdf"], schedule=plain)]

    ctx = context(tmp_path, replace(synthetic.build(), reports=Reports()))
    end = (date.today() + timedelta(days=30)).isoformat()
    output = ctx.execute("prepare_scheduled_report", schedule(format="pdf", end_date=end))
    assert output["category"] == "invalid_input" and "end date" in output["error"]
    assert "error" not in ctx.execute("prepare_scheduled_report", schedule(format="pdf"))


def test_the_report_format_must_be_offered_by_its_type(tmp_path):
    ctx = context(tmp_path)
    period = {"site_id": "site-a", "start": "2026-01-01", "end": "2026-01-31"}
    assert ctx.execute("prepare_report", {**period, "report_type": "site-status", "format": "xlsx"})["category"] == (
        "invalid_input"
    )
    assert "error" not in ctx.execute("prepare_report", {**period, "report_type": "temperature-log", "format": "xlsx"})


def test_confirmed_report_has_a_download_link_that_expires_after_a_day(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        draft = events(client, headers, "Create a temperature report for Demo North for the last seven days")[-1][1]
        action = draft["actions"][0]["id"]
        before = client.get(f"/api/actions/{action}/status", headers=headers)
        assert before.status_code == 200 and before.json()["state"] == "prepared"
        assert submit(client, headers, action).status_code == 200
        status = client.get(f"/api/actions/{action}/status", headers=headers).json()
        assert status["state"] == "completed"
        result = status["result"]
        ready, expires = (datetime.fromisoformat(result[key]) for key in ("completed_at", "expires_at"))
        assert expires - ready == timedelta(hours=24)
        assert result["url"] == f"/reference/report/{action}/download"
        again = client.get(f"/api/actions/{action}/status", headers=headers).json()
        assert again["result"]["expires_at"] == result["expires_at"], "the expiry is fixed when the report is ready"
        assert client.get(result["url"]).json()["synthetic"] is True


def test_status_endpoint_is_private_audited_and_categorized(tmp_path):
    with app_client(tmp_path, "report_processing") as client:
        alice, bob = login(client), login(client, "bob")
        draft = events(client, alice, "Create a temperature report for Demo North for the last seven days")[-1][1]
        action = draft["actions"][0]["id"]
        submit(client, alice, action)
        processing = client.get(f"/api/actions/{action}/status", headers=alice).json()
        assert processing["state"] == "processing" and processing["result"]["url"] is None
        hidden = client.get(f"/api/actions/{action}/status", headers=bob)
        assert (hidden.status_code, hidden.headers["X-Error-Category"]) == (404, "not_found")
        assert client.get("/api/actions/unknown/status", headers=alice).status_code == 404
        assert client.get(f"/api/actions/{action}/status").status_code == 401
    trail = Store(str(tmp_path / "test.sqlite3")).audit_trail()
    statuses = [record for record in trail if record["event"] == "status"]
    assert [record["tools"][0]["tool"] for record in statuses] == ["get_report_status"] * 3


def test_status_endpoint_respects_the_reports_switch(tmp_path):
    with app_client(tmp_path, features=frozenset({"troubleshooting"})) as client:
        response = client.get("/api/actions/anything/status", headers=login(client))
    assert (response.status_code, response.headers["X-Error-Category"]) == (403, "disabled")


def test_a_confirmed_schedule_reports_its_first_run(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        draft = events(client, headers, "/schedule " + json.dumps(schedule()))[-1][1]
        action = draft["actions"][0]["id"]
        confirmed = submit(client, headers, action).json()
        assert (confirmed["status"], confirmed["next_run"]) == ("scheduled", TOMORROW)
        status = client.get(f"/api/actions/{action}/status", headers=headers).json()
        assert (status["kind"], status["state"], status["result"]["next_run"]) == (
            "scheduled_report",
            "scheduled",
            TOMORROW,
        )
        reply = events(client, headers, f"/status {action}")[-1][1]
        assert reply["text"].startswith("Your schedule is confirmed. The first report goes out on")
    trail = Store(str(tmp_path / "test.sqlite3")).audit_trail()
    inputs = next(record["inputs"] for record in trail if record["event"] == "chat" and record["inputs"])
    assert inputs[0]["recipients"] == 2 and "example.com" not in json.dumps(trail), "addresses are never audited"


def test_schedules_need_the_reports_feature_at_confirmation(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        draft = events(client, headers, "/schedule " + json.dumps(schedule()))[-1][1]
    with app_client(tmp_path, features=frozenset({"troubleshooting", "support"})) as client:
        refused = submit(client, headers, draft["actions"][0]["id"])
    assert refused.status_code == 403


def guard_context(tmp_path) -> Context:
    ctx = context(tmp_path)
    ctx.execute("get_permitted_sites", {})
    return ctx


@pytest.mark.parametrize(
    "text",
    [
        "Your report was created.",
        "The report has been submitted successfully.",
        "Your weekly report is now scheduled.",
        "I've sent the report to your team. It was sent successfully.",
        "The schedule is confirmed.",
        "I've scheduled your weekly temperature log report for Demo North.",
        "I have created your temperature log report.",
        "Done! I scheduled the weekly report.",
        "We've sent the report to your team.",
        "Your weekly report has been set up.",
        "No problem, your report was created.",
    ],
)
def test_the_reply_guard_refuses_claims_the_service_did_not_confirm(tmp_path, text):
    ctx = guard_context(tmp_path)
    with pytest.raises(ValueError):
        guard(json.dumps({"status": "answer", "text": text, "source_ids": ["tool-1"]}), ctx)


@pytest.mark.parametrize(
    "text",
    [
        "Nothing has been scheduled yet.",
        "No report was created.",
        "Once you press Confirm, the report is created.",
        "It is sent only after you press Confirm.",
        "I can schedule it once you add recipients.",
    ],
)
def test_the_reply_guard_allows_wording_that_claims_nothing(tmp_path, text):
    ctx = guard_context(tmp_path)
    assert guard(json.dumps({"status": "answer", "text": text, "source_ids": ["tool-1"]}), ctx).text == text


def test_a_first_run_on_a_single_digit_day_passes_the_number_check(tmp_path):
    first = date(date.today().year + 1, 1, 5)
    with app_client(tmp_path) as client:
        headers = login(client)
        draft = events(client, headers, "/schedule " + json.dumps(schedule(start_date=first.isoformat())))[-1][1]
        action = draft["actions"][0]["id"]
        submit(client, headers, action)
        reply = events(client, headers, f"/status {action}")[-1][1]
    assert reply["status"] == "answer"
    assert reply["text"].endswith(f"The first report goes out on 5 Jan {first.year}.")


def test_answer_buttons_keep_every_detail_the_user_gave(tmp_path):
    later = date.today() + timedelta(days=40)
    until = later + timedelta(days=90)
    with app_client(tmp_path) as client:
        headers = login(client)
        question = events(
            client, headers, "Create a pressure report for Demo North as CSV from 2026-09-01 to 2026-09-30"
        )
        [choice] = [item for item in question[-1][1]["choices"] if item["label"] == "Temperature log"]
        draft = events(client, headers, choice["message"])[-1][1]["actions"][0]["payload"]
        assert (draft["format"], draft["start"], draft["end"]) == ("csv", "2026-09-01", "2026-09-30")
        details = f"as Excel to ops@example.com starting {later} until {until}"
        message = f"Schedule a pressure report for Demo North weekly {details}"
        question = events(client, headers, message)
        [choice] = [item for item in question[-1][1]["choices"] if item["label"] == "Temperature log"]
        draft = events(client, headers, choice["message"])[-1][1]["actions"][0]["payload"]
    assert draft["recipients"] == ["ops@example.com"] and draft["format"] == "xlsx"
    assert (draft["start_date"], draft["end_date"]) == (later.isoformat(), until.isoformat())


def test_a_site_name_with_extra_words_is_confirmed_before_any_draft(tmp_path):
    with app_client(tmp_path) as client:
        reply = events(
            client, login(client), "Create a temperature report for Demo North Annex for the last seven days"
        )
    reply = reply[-1][1]
    assert (reply["status"], reply["asks_for"], reply["text"]) == ("clarification", "site", "Do you mean Demo North?")
    assert reply["actions"] == []
    assert reply["choices"][0]["message"] == "Create a temperature report for Demo North for the last seven days"


def test_the_reply_guard_allows_a_claim_after_the_service_confirmed_it(tmp_path):
    ctx = guard_context(tmp_path)
    draft = {
        "site_id": "site-a",
        "report_type": "temperature-log",
        "format": "pdf",
        "start": "2026-01-01",
        "end": "2026-01-07",
    }
    action = ctx.execute("prepare_report", draft)["data"]["id"]
    with pytest.raises(ValueError):
        guard(json.dumps({"status": "answer", "text": "Your report was created.", "source_ids": ["tool-2"]}), ctx)
    ctx.store.submit(ctx.session_id, action, ctx.submit_action)
    confirmed = context(tmp_path)
    confirmed.session_id = ctx.session_id
    status = confirmed.execute("get_report_status", {"action_id": action})
    assert status["data"]["state"] == "completed"
    reply = guard(
        json.dumps({"status": "answer", "text": "Your report was created.", "source_ids": [status["source_id"]]}),
        confirmed,
    )
    assert reply.text == "Your report was created."


def test_asks_for_is_only_for_clarifications(tmp_path):
    ctx = guard_context(tmp_path)
    question = {
        "status": "clarification",
        "text": "Which period should it cover?",
        "source_ids": [],
        "asks_for": "period",
    }
    assert guard(json.dumps(question), ctx).asks_for == "period"
    with pytest.raises(ValueError):
        guard(json.dumps({**question, "status": "answer", "source_ids": ["tool-1"]}), ctx)
    with pytest.raises(ValueError):
        guard(json.dumps({**question, "asks_for": "site_id"}), ctx)
