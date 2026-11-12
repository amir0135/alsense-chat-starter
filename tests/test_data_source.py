"""Wording that follows the backend's data source: synthetic results say so, other data gets neutral wording."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import login, reply, submit

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.synthetic.services import SyntheticSiteDirectory
from alsense_chat.app import create_app
from alsense_chat.config import Settings

REPORT_REQUEST = "Create a temperature report for Demo North for the last seven days"


def client_for(tmp_path, kind: str, **services) -> TestClient:
    """The synthetic implementations reported as `kind`, so only the wording can differ between the two."""
    settings = Settings(db=str(tmp_path / f"{kind}.sqlite3"))
    return TestClient(create_app(settings, replace(synthetic.build(), kind=kind, **services)))


@pytest.mark.parametrize("kind", ["synthetic", "alsense"])
def test_health_and_every_reply_say_whether_the_data_is_synthetic(tmp_path, kind):
    with client_for(tmp_path, kind) as client:
        assert client.get("/api/health").json()["data_source"] == kind
        headers = login(client)
        for message in ("Which sites can I access?", "/flows", REPORT_REQUEST):
            assert reply(client, headers, message)["synthetic"] is (kind == "synthetic"), message


def test_a_completed_report_is_called_synthetic_only_when_the_data_is(tmp_path):
    texts = {}
    for kind in ("synthetic", "alsense"):
        with client_for(tmp_path, kind) as client:
            headers = login(client)
            action = reply(client, headers, REPORT_REQUEST)["actions"][0]["id"]
            assert submit(client, headers, action).status_code == 200
            status = reply(client, headers, f"/status {action}")
            assert status["sources"][0]["data"]["state"] == "completed"
            texts[kind] = status["text"]
    assert texts == {
        "synthetic": "Your synthetic report is ready. Download it with the link below before the link expires.",
        "alsense": "Your report is ready. Download it with the link below before the link expires.",
    }


def test_no_permitted_sites_is_called_a_demo_only_when_the_data_is_synthetic(tmp_path):
    class NoSites(SyntheticSiteDirectory):
        def permitted_sites(self, user):
            return []

    texts = {}
    for kind in ("synthetic", "alsense"):
        with client_for(tmp_path, kind, sites=NoSites()) as client:
            answer = reply(client, login(client), "Which sites can I access?")
            assert answer["status"] == "not_in_data"
            texts[kind] = answer["text"]
    assert texts == {
        "synthetic": "You don't have access to any sites in this demo.",
        "alsense": "You don't have access to any sites.",
    }
