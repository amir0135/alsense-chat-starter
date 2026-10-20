import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.runner import FoundryRunner, guard
from alsense_chat.source import MockAlsenseSource
from alsense_chat.store import Store
from alsense_chat.tools import Context, model_definition, safe_link, tool_definitions


class CountingSource(MockAlsenseSource):
    def __init__(self):
        self.lookups = 0
        self.submissions = 0

    def users_me(self, identity):
        self.lookups += 1
        return super().users_me(identity)

    def submit(self, *args):
        self.submissions += 1
        return super().submit(*args)


@pytest.fixture
def setup(tmp_path):
    source = CountingSource()
    settings = Settings(db=str(tmp_path / "sessions.sqlite3"))
    with TestClient(create_app(settings, source)) as client:
        yield client, source, settings


def login(client, persona="alice"):
    response = client.post("/api/demo/session", json={"persona": persona})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def chat(client, headers, message):
    response = client.post("/api/chat", headers=headers, json={"message": message})
    assert response.status_code == 200
    events = []
    for frame in response.text.strip().split("\n\n"):
        name, data = frame.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    assert events[-1][0] == "reply", events
    return events[-1][1]


def report(site="site-a"):
    return {
        "site_id": site,
        "report_type": "alarm-history",
        "format": "pdf",
        "start": "2026-01-01",
        "end": "2026-01-31",
    }


def test_all_tools_are_declared_and_curated():
    assert len(model_definition()["tools"]) == 6
    assert "submit" not in model_definition()["tools"]
    sites = next(tool for tool in tool_definitions() if tool["name"] == "resolve_sites")
    assert '"*"' in sites["description"]
    assert '"*"' in sites["parameters"]["properties"]["query"]["description"]


def test_login_internal_only_and_feature_flag(setup, tmp_path):
    client, _, _ = setup
    assert client.post("/api/demo/session", json={"persona": "external"}).status_code == 403
    assert client.post("/api/demo/session", json={"persona": "admin"}).status_code == 422
    with TestClient(create_app(Settings(enabled=False, db=str(tmp_path / "off.sqlite3")))) as disabled:
        assert disabled.post("/api/demo/session", json={"persona": "alice"}).status_code == 403
        assert disabled.get("/api/session").status_code == 403


def test_auth_required_and_users_me_once_per_session(setup):
    client, source, _ = setup
    assert client.post("/api/chat", json={"message": "/sites *"}).status_code == 401
    headers = login(client)
    chat(client, headers, "/sites *")
    chat(client, headers, "/reports")
    assert source.lookups == 1
    assert client.get("/api/session", headers={"Authorization": "Bearer fake"}).status_code == 401


def test_report_types_require_a_session(setup):
    client, _, _ = setup
    assert client.get("/api/report-types").status_code == 401
    headers = login(client)
    assert [item["id"] for item in client.get("/api/report-types", headers=headers).json()] == [
        "alarm-history",
        "site-status",
    ]


def test_new_conversation_starts_a_fresh_authenticated_session(setup):
    client, _, _ = setup
    assert client.post("/api/session/new").status_code == 401
    headers = login(client)
    chat(client, headers, "Which sites can I access?")
    fresh = client.post("/api/session/new", headers=headers)
    assert fresh.status_code == 200
    new_headers = {"Authorization": "Bearer " + fresh.json()["token"]}
    assert fresh.json()["user"]["id"] == "alice"
    assert client.get("/api/session", headers=new_headers).json()["history"] == []


def test_demo_mode_understands_common_suggested_questions(setup):
    client, _, _ = setup
    headers = login(client)

    sites = chat(client, headers, "Which sites can I access?")
    assert sites["status"] == "answer"
    assert sites["sources"][0]["tool"] == "resolve_sites"
    assert sites["sources"][0]["data"][0]["id"] == "site-a"

    reports = chat(client, headers, "What report types are available?")
    assert reports["status"] == "answer"
    assert reports["sources"][0]["tool"] == "list_report_types"

    guidance = chat(client, headers, "My sensor is offline. What should I check?")
    assert guidance["status"] == "answer"
    assert guidance["sources"][0]["tool"] == "search_docs"


def test_site_and_tenant_isolation(setup):
    client, _, _ = setup
    alice, bob = login(client), login(client, "bob")
    own = chat(client, alice, "/sites *")
    assert own["sources"][0]["data"][0]["id"] == "site-a"
    assert "site-b" not in json.dumps(own)
    assert chat(client, alice, "/sites site-b")["status"] == "not_in_data"
    denied = chat(client, alice, "/report " + json.dumps(report("site-b")))
    assert denied["status"] == "clarification"
    assert denied["actions"] == []
    assert chat(client, bob, "/sites *")["sources"][0]["data"][0]["id"] == "site-b"


def test_explicit_confirmation_and_idempotent_submission(setup):
    client, source, _ = setup
    headers = login(client)
    reply = chat(client, headers, "/report " + json.dumps(report()))
    action_id = reply["actions"][0]["id"]
    assert source.submissions == 0
    path = f"/api/actions/{action_id}/submit"
    assert client.post(path, headers=headers, json={}).status_code == 422
    assert client.post(path, headers=headers, json={"confirmed": False}).status_code == 422
    assert client.post(path, headers=login(client, "bob"), json={"confirmed": True}).status_code == 404
    first = client.post(path, headers=headers, json={"confirmed": True})
    second = client.post(path, headers=headers, json={"confirmed": True})
    assert first.json() == second.json()
    assert first.json()["synthetic"] is True
    assert source.submissions == 1
    status = chat(client, headers, f"/status {action_id}")
    assert status["sources"][0]["data"]["state"] == "submitted"


def test_ticket_alarm_authorization_and_prepare_only(setup):
    client, source, _ = setup
    headers = login(client)
    payload = {
        "site_id": "site-a",
        "alarm_id": "alarm-site-b",
        "summary": "Offline",
        "steps_tried": "Checked connectivity",
    }
    assert chat(client, headers, "/ticket " + json.dumps(payload))["actions"] == []
    payload["alarm_id"] = "alarm-site-a"
    draft = chat(client, headers, "/ticket " + json.dumps(payload))
    assert draft["actions"][0]["kind"] == "ticket"
    assert source.submissions == 0
    action_id = draft["actions"][0]["id"]
    assert (
        client.post(f"/api/actions/{action_id}/submit", headers=headers, json={"confirmed": True}).json()["status"]
        == "mock_received"
    )


def test_sessions_and_drafts_survive_restart(setup):
    client, source, settings = setup
    headers = login(client)
    draft = chat(client, headers, "/report " + json.dumps(report()))
    with TestClient(create_app(settings, source)) as restarted:
        history = restarted.get("/api/session", headers=headers).json()["history"]
        assert history[-1]["reply"]["actions"][0]["id"] == draft["actions"][0]["id"]
        assert (
            restarted.post(
                f"/api/actions/{draft['actions'][0]['id']}/submit", headers=headers, json={"confirmed": True}
            ).status_code
            == 200
        )


def test_expired_session(setup):
    client, _, settings = setup
    headers = login(client)
    with Store(settings.db).connection() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert client.get("/api/session", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"format": "exe"},
        {"start": "yesterday"},
        {"end": "2025-01-01"},
        {"end": "2028-01-01"},
        {"report_type": "other"},
        {"url": "https://evil.example"},
    ],
)
def test_bad_report_inputs_never_prepare(setup, changes):
    client, _, _ = setup
    assert chat(client, login(client), "/report " + json.dumps({**report(), **changes}))["actions"] == []


def test_source_citations_fallback_and_scope(setup):
    client, _, settings = setup
    headers = login(client)
    docs = chat(client, headers, "/docs offline sensor")
    assert docs["source_ids"] == ["tool-1"]
    assert docs["sources"][0]["data"][0]["url"].startswith("/reference/")
    assert chat(client, headers, "/docs unknown keyword")["status"] == "not_in_data"
    assert chat(client, headers, "Write a poem")["status"] == "out_of_scope"
    with Store(settings.db).connection() as db:
        categories = {row["category"] for row in db.execute("SELECT category FROM unanswered")}
    assert {"not_in_data", "out_of_scope"} <= categories


@pytest.mark.parametrize("url", ["https://evil.example", "//evil.example", "/reference/\\evil", "/reference/\n"])
def test_unapproved_links(url):
    assert not safe_link(url)


def test_reply_guard_blocks_inventions(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    user = MockAlsenseSource().users_me("alice")
    _, session_id = store.create_session(user, 3600)
    ctx = Context(MockAlsenseSource(), store, session_id, user)
    ctx.execute("resolve_sites", {"query": "*"})
    for reply in (
        {"status": "answer", "text": "There are 987 alarms.", "source_ids": ["tool-1"]},
        {"status": "answer", "text": "Done.", "source_ids": ["made-up"]},
        {"status": "answer", "text": "See https://evil.example.", "source_ids": ["tool-1"]},
    ):
        with pytest.raises(ValueError):
            guard(json.dumps(reply), ctx)
    ctx.execute("prepare_report", report())
    with pytest.raises(ValueError):
        guard(json.dumps({"status": "answer", "text": "Report submitted.", "source_ids": ["tool-2"]}), ctx)


def test_reply_guard_keeps_internal_identifiers_out_of_text(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    user = MockAlsenseSource().users_me("alice")
    _, session_id = store.create_session(user, 3600)
    ctx = Context(MockAlsenseSource(), store, session_id, user)
    sites = ctx.execute("resolve_sites", {"query": "*"})
    docs = ctx.execute("search_docs", {"query": "sensor offline"})
    assert "tenant_id" not in json.dumps(sites)
    assert sites["data"][0]["alarms"][0]["label"] == "Sensor offline"
    for text in (
        "You can access Demo North (id: site-a).",
        "Demo North belongs to customer-a.",
        "I found an alarm, alarm-site-a, at Demo North.",
        "I used resolve_sites to check.",
        "Which site id and alarm id should I use?",
    ):
        with pytest.raises(ValueError):
            guard(json.dumps({"status": "answer", "text": text, "source_ids": [sites["source_id"]]}), ctx)
    steps = "Here is what to check:\n\n1. Check power.\n2. Check connectivity.\n3. Review the alarm history report."
    assert guard(json.dumps({"status": "answer", "text": steps, "source_ids": [docs["source_id"]]}), ctx)


def test_demo_answers_are_concise_and_use_display_names(setup):
    client, _, _ = setup
    alice = login(client)
    for question in ("Which sites can I access?", "My sensor is offline. What should I check?"):
        reply = chat(client, alice, question)
        assert reply["status"] == "answer"
        assert "Demo North" in reply["text"]
        assert not any(value in reply["text"] for value in ("site-a", "customer-a", "tool-", "alarm-site"))
        assert len(reply["text"].split()) <= 80


def test_foundry_backend_executes_tools_and_never_receives_credentials(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    source = MockAlsenseSource()
    user = source.users_me("alice")
    token, session_id = store.create_session(user, 3600)
    ctx = Context(source, store, session_id, user)
    requests = []

    class Call:
        type = "function_call"
        name = "resolve_sites"
        arguments = '{"query":"*"}'
        call_id = "call-1"

        def model_dump(self, **kwargs):
            return {"type": self.type, "name": self.name, "arguments": self.arguments, "call_id": self.call_id}

    def create(**kwargs):
        requests.append(json.loads(json.dumps(kwargs)))
        if len(requests) == 1:
            return SimpleNamespace(status="completed", output=[Call()])
        return SimpleNamespace(
            status="completed",
            output=[],
            output_text=json.dumps(
                {
                    "status": "answer",
                    "text": "Your permitted site is Demo North.",
                    "source_ids": ["tool-1"],
                }
            ),
        )

    runner = FoundryRunner.__new__(FoundryRunner)
    runner.deployment = "test"
    runner.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    events = list(runner.run("My sites?", ctx))
    assert events[-1][1]["sources"][0]["data"][0]["id"] == "site-a"
    assert token not in json.dumps(requests)
    assert "site-b" not in json.dumps(requests)
    assert requests[1]["input"][-1]["type"] == "function_call_output"


def test_real_auth_not_silently_mocked():
    with pytest.raises(ValueError, match="Real sign-in"):
        Settings(demo_login=False).validate()


def test_cloud_demo_requires_strong_code_and_validates_before_identity_lookup(tmp_path):
    with pytest.raises(ValueError, match="high-entropy"):
        Settings(cloud_demo=True).validate()
    code = "synthetic-code-with-at-least-32-characters"
    source = CountingSource()
    settings = Settings(cloud_demo=True, demo_access_code=code, db=str(tmp_path / "cloud.sqlite3"))
    with TestClient(create_app(settings, source)) as client:
        for supplied in ("", "incorrect", "非ASCII"):
            response = client.post("/api/demo/session", json={"persona": "alice", "access_code": supplied})
            assert response.status_code == 401
        assert source.lookups == 0
        response = client.post("/api/demo/session", json={"persona": "alice", "access_code": code})
        assert response.status_code == 200
        assert code not in response.text
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        result = chat(client, headers, "/sites *")
        assert code not in json.dumps(result)
        assert source.lookups == 1


def test_unknown_site_does_not_leak_other_tenant_in_support(setup):
    client, source, _ = setup
    reply = chat(
        client,
        login(client),
        '/ticket {"site_id":"site-b","alarm_id":"alarm-site-b","summary":"Offline","steps_tried":"Checked power"}',
    )
    assert reply["actions"] == []
    assert "Demo South" not in json.dumps(reply)
    assert source.submissions == 0


def test_concurrent_confirmations_do_not_duplicate_submissions(setup):
    client, source, _ = setup
    headers = login(client)
    draft = chat(client, headers, "/report " + json.dumps(report()))
    path = f"/api/actions/{draft['actions'][0]['id']}/submit"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path, headers=headers, json={"confirmed": True}), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert source.submissions == 1


def test_submission_rechecks_revoked_site_permissions(setup):
    client, source, settings = setup
    headers = login(client)
    draft = chat(client, headers, "/report " + json.dumps(report()))
    path = f"/api/actions/{draft['actions'][0]['id']}/submit"
    assert client.post(path, headers=headers, json={"confirmed": True}).status_code == 200
    with Store(settings.db).connection() as db:
        row = db.execute("SELECT id, user_json FROM sessions").fetchone()
        user = json.loads(row["user_json"])
        user["sites"] = []
        db.execute("UPDATE sessions SET user_json=? WHERE id=?", (json.dumps(user), row["id"]))
    assert client.post(path, headers=headers, json={"confirmed": True}).status_code == 403
    assert source.submissions == 1


def test_export_excludes_credentials_databases_and_parent_repo(tmp_path):
    import importlib.util
    from zipfile import ZipFile

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("starter_export", root / "export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "starter.zip"
    module.export(target)
    with ZipFile(target) as archive:
        names = archive.namelist()
        assert "alsense-chat-starter/PRODUCT.md" in names
        assert "alsense-chat-starter/DESIGN.md" in names
        assert "alsense-chat-starter/.env.example" in names
        assert "alsense-chat-starter/.github/workflows/tests.yml" in names
        assert all("demo-data" not in name and "/.git/" not in name and not name.endswith(".sqlite3") for name in names)
        assert "alsense-chat-starter/.env" not in names
    with pytest.raises(FileExistsError):
        module.export(target)


def test_no_imports_of_lhu():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "src").rglob("*.py"):
        assert "lhu_insights" not in path.read_text()
