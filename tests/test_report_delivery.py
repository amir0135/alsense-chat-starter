"""Reports at the report service's boundary: what the service receives, job states over time, outages and links."""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from helpers import RecordingReports, login, reply, submit

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.contracts import ReportRequest, ScheduledReportRequest
from alsense_chat.adapters.errors import ServiceTimeout, ServiceUnavailable, Unauthenticated
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.store import Store

REPORT = "Create a temperature report for Demo North for the last seven days"
OFF_PATH = "https://reports.example.invalid/report.pdf"
STATUS_CODES = (
    "Finding: GET /api/actions/{id}/status answers 503 for every upstream category the map doesn't name, while "
    "confirming answers the documented status (504 for a timeout, 401 for a rejected sign-in)."
)
OFF_PATH_LINKS = (
    "Finding: only an on-demand job's status withholds a link outside /reference/; confirming a report or schedule "
    "and a schedule's status return the service's link as it is."
)


def app(tmp_path, reports: RecordingReports) -> TestClient:
    services = replace(synthetic.build(), reports=reports)
    return TestClient(create_app(Settings(db=str(tmp_path / "test.sqlite3")), services))


def stored(tmp_path, action_id: str) -> dict:
    """The draft as the backend keeps it: its payload and the service's submission, if any."""
    with Store(str(tmp_path / "test.sqlite3")).connection() as db:
        return dict(db.execute("SELECT payload, result FROM actions WHERE id=?", (action_id,)).fetchone())


def test_the_confirmed_draft_reaches_the_report_service_unchanged(tmp_path):
    start = date.today() + timedelta(days=3)
    schedule = {
        "site_id": "site-a",
        "report_type": "alarm-history",
        "format": "csv",
        "frequency": "monthly",
        "recipients": ["operations@example.com", "quality@example.com"],
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=90)).isoformat(),
    }
    reports = RecordingReports()
    with app(tmp_path, reports) as client:
        headers = login(client)
        on_demand = reply(client, headers, REPORT)["actions"][0]
        scheduled = reply(client, headers, "/schedule " + json.dumps(schedule))["actions"][0]
        for action in (on_demand, scheduled):
            assert submit(client, headers, action["id"]).status_code == 200
    today = date.today()
    assert on_demand["payload"] == {
        "site_id": "site-a",
        "report_type": "temperature-log",
        "format": "pdf",
        "start": (today - timedelta(days=6)).isoformat(),
        "end": today.isoformat(),
    }
    # Exactly the drafts the user confirmed, field for field, keyed by their action ids.
    assert reports.received == [
        ("report", on_demand["payload"], on_demand["id"]),
        ("scheduled_report", scheduled["payload"], scheduled["id"]),
    ]
    assert set(on_demand["payload"]) == set(ReportRequest.model_fields)
    assert set(scheduled["payload"]) == set(ScheduledReportRequest.model_fields) and scheduled["payload"] == schedule


def test_a_processing_job_shows_its_link_only_once_it_completes(tmp_path):
    reports = RecordingReports()
    reports.states = ["processing", "processing"]
    with app(tmp_path, reports) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        assert submit(client, headers, action).status_code == 200
        path = f"/api/actions/{action}/status"
        reads = [client.get(path, headers=headers).json() for _ in range(3)]
    assert [read["state"] for read in reads] == ["processing", "processing", "completed"]
    assert [read["result"]["url"] for read in reads[:2]] == [None, None]
    assert reads[2]["result"]["url"] == f"/reference/report/{action}/download"
    assert reads[2]["result"]["expires_at"] and reads[2]["result"]["completed_at"]
    assert len(reports.received) == 1, "reading the status never submits again"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ServiceUnavailable("reports"), 503),
        pytest.param(ServiceTimeout("reports"), 504, marks=pytest.mark.xfail(strict=True, reason=STATUS_CODES)),
        pytest.param(Unauthenticated("reports"), 401, marks=pytest.mark.xfail(strict=True, reason=STATUS_CODES)),
    ],
)
def test_a_status_read_during_an_outage_says_why_and_leaves_the_report_alone(tmp_path, error, status):
    reports = RecordingReports()
    with app(tmp_path, reports) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        assert submit(client, headers, action).status_code == 200
        before = stored(tmp_path, action)
        reports.failure = error
        response = client.get(f"/api/actions/{action}/status", headers=headers)
        assert (response.headers["X-Error-Category"], response.json()["detail"]) == (error.category, error.user_message)
        assert stored(tmp_path, action) == before
        reports.failure = None
        assert client.get(f"/api/actions/{action}/status", headers=headers).json()["state"] == "completed"
    assert len(reports.received) == 1
    assert response.status_code == status


def test_confirming_while_the_report_service_is_down_keeps_the_draft_and_submits_once_later(tmp_path):
    reports = RecordingReports()
    with app(tmp_path, reports) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        reports.failure = ServiceUnavailable("reports")
        refused = submit(client, headers, action)
        assert (refused.status_code, refused.headers["X-Error-Category"]) == (503, "unavailable")
        assert refused.json()["detail"] == (
            "Your request was prepared but not submitted. The report service is temporarily unavailable. "
            "Try again in a moment."
        )
        assert stored(tmp_path, action)["result"] is None
        assert client.get(f"/api/actions/{action}/status", headers=headers).json()["state"] == "prepared"
        reports.failure = None
        accepted = submit(client, headers, action)
        again = submit(client, headers, action)
    assert (accepted.status_code, accepted.json()["status"]) == (200, "accepted")
    assert again.json() == accepted.json()
    assert [(kind, key) for kind, _, key in reports.received] == [("report", action)], "submitted exactly once"


def test_a_job_link_outside_the_approved_path_is_withheld(tmp_path):
    reports = RecordingReports()
    with app(tmp_path, reports) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        assert submit(client, headers, action).status_code == 200
        reports.link = OFF_PATH
        response = client.get(f"/api/actions/{action}/status", headers=headers)
        status = reply(client, headers, f"/status {action}")
    assert (response.status_code, response.headers["X-Error-Category"]) == (503, "unavailable")
    assert response.json()["detail"] == "The report service returned an unapproved link, so I withheld it."
    assert (status["status"], status["error"]["category"]) == ("error", "unavailable")
    assert OFF_PATH not in response.text + json.dumps(status)


@pytest.mark.xfail(strict=True, reason=OFF_PATH_LINKS)
def test_a_link_outside_the_approved_path_is_never_returned(tmp_path):
    reports = RecordingReports()
    reports.link = OFF_PATH
    schedule = {
        "site_id": "site-a",
        "report_type": "temperature-log",
        "format": "pdf",
        "frequency": "weekly",
        "recipients": ["operations@example.com"],
        "start_date": (date.today() + timedelta(days=1)).isoformat(),
    }
    with app(tmp_path, reports) as client:
        headers = login(client)
        report = reply(client, headers, REPORT)["actions"][0]["id"]
        scheduled = reply(client, headers, "/schedule " + json.dumps(schedule))["actions"][0]["id"]
        returned = [
            submit(client, headers, report).text,
            submit(client, headers, scheduled).text,
            client.get(f"/api/actions/{scheduled}/status", headers=headers).text,
            json.dumps(reply(client, headers, f"/status {scheduled}")),
        ]
    assert [OFF_PATH in text for text in returned] == [False] * 4
