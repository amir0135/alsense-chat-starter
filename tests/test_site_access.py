"""Site access between users of the same customer, and for each site of a user with several sites.

Every synthetic persona has its own customer (tenant), so the other tests show isolation between customers. Here
two users share a customer but not their sites, and the site directory answers with every site of the customer,
as one that ignored per-user permissions would: the backend must still keep each user to their own sites.
"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import RecordingReports, flow, login, reply, report_payload, submit

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.contracts import Site
from alsense_chat.adapters.synthetic.services import SyntheticIdentity, SyntheticSiteDirectory
from alsense_chat.app import create_app
from alsense_chat.config import Settings

SITES = "Which sites can I access?"
NORTH = Site(id="site-a", name="Demo North", tenant_id="customer-a")
WEST = Site(id="site-west", name="Demo West", tenant_id="customer-a")


class SameCustomer(SyntheticIdentity):
    """Bob works for Alice's customer, at another site."""

    def users_me(self, identity):
        user = super().users_me(identity)
        return user.model_copy(update={"tenant_id": NORTH.tenant_id, "sites": [WEST]}) if identity == "bob" else user


class WholeCustomer(SyntheticSiteDirectory):
    def permitted_sites(self, user):
        return [NORTH, WEST]


def same_customer(tmp_path, reports: RecordingReports) -> TestClient:
    services = replace(synthetic.build(), identity=SameCustomer(), sites=WholeCustomer(), reports=reports)
    return TestClient(create_app(Settings(db=str(tmp_path / "test.sqlite3")), services))


@pytest.mark.parametrize(("persona", "own", "other"), [("alice", NORTH, WEST), ("bob", WEST, NORTH)])
def test_users_of_the_same_customer_reach_only_their_own_sites(tmp_path, persona, own, other):
    reports = RecordingReports()
    with same_customer(tmp_path, reports) as client:
        headers = login(client, persona)
        listed = reply(client, headers, SITES)
        resolved = reply(client, headers, f"/sites {other.name}")
        asked = reply(client, headers, f"Create a temperature report for {other.name} for the last seven days")
        refused = reply(client, headers, "/report " + json.dumps(report_payload(other.id)))
        checked = flow(client, headers, ["no", other.name])
    assert [site["id"] for site in listed["sources"][0]["data"]["sites"]] == [own.id]
    assert resolved["status"] == "not_in_data"
    assert resolved["sources"][0]["data"] == {
        "match": "none",
        "sites": [],
        "alternatives": [own.model_dump(include={"id", "name"})],
    }
    assert (asked["status"], asked["actions"]) == ("clarification", [])
    assert (refused["status"], refused["error"]["category"], refused["actions"]) == ("error", "forbidden", [])
    assert checked["flow"]["kind"] != "outcome", "a guided check can't identify the other user's site"
    replies = json.dumps([listed, resolved, asked, refused, checked])
    assert other.id not in replies
    assert reports.received == [], "nothing reached the report service"


def test_another_users_session_gets_404_from_every_session_scoped_endpoint(tmp_path):
    reports = RecordingReports()
    with same_customer(tmp_path, reports) as client:
        alice, bob = login(client), login(client, "bob")
        answer = reply(client, alice, "Create a temperature report for Demo North for the last seven days")
        action = answer["actions"][0]["id"]
        request = client.post("/api/chat", headers=alice, json={"message": SITES}).headers["x-correlation-id"]
        timing = {"correlation_id": request, "headers": 5, "first_event": 8, "reply": 20, "rendered": 25}
        attempts = {
            "status": client.get(f"/api/actions/{action}/status", headers=bob),
            "confirm": submit(client, bob, action),
            "feedback": client.post(f"/api/answers/{answer['answer_id']}/feedback", headers=bob, json={"rating": "up"}),
            "timings": client.post("/api/timings", headers=bob, json=timing),
        }
        assert {name: response.status_code for name, response in attempts.items()} == dict.fromkeys(attempts, 404)
        assert reports.received == []
        # Everything still works for the user it belongs to.
        assert submit(client, alice, action).status_code == 200
        assert client.get(f"/api/actions/{action}/status", headers=alice).json()["state"] == "completed"
        rated = client.post(f"/api/answers/{answer['answer_id']}/feedback", headers=alice, json={"rating": "up"})
        assert rated.status_code == 200
        assert client.post("/api/timings", headers=alice, json=timing).status_code == 204
    assert [(kind, received["site_id"]) for kind, received, _ in reports.received] == [("report", NORTH.id)]


def test_each_site_of_a_user_with_several_sites_can_be_used(tmp_path):
    reports = RecordingReports()
    settings = Settings(db=str(tmp_path / "test.sqlite3"))
    with TestClient(create_app(settings, replace(synthetic.build(), reports=reports))) as client:
        carol = login(client, "carol")
        listed = reply(client, carol, SITES)["sources"][0]["data"]["sites"]
        assert [site["name"] for site in listed] == ["Demo East Hall", "Demo East Depot"]
        for site_id, name in (("site-c1", "Demo East Hall"), ("site-c2", "Demo East Depot")):
            resolved = reply(client, carol, f"/sites {name}")["sources"][0]["data"]
            assert (resolved["match"], [site["id"] for site in resolved["sites"]]) == ("one", [site_id])
            draft = reply(client, carol, f"Create a temperature report for {name} for the last seven days")
            [action] = draft["actions"]
            assert action["payload"]["site_id"] == site_id
            assert submit(client, carol, action["id"]).status_code == 200
            assert client.get(f"/api/actions/{action['id']}/status", headers=carol).json()["state"] == "completed"
    assert [received["site_id"] for _, received, _ in reports.received] == ["site-c1", "site-c2"]
