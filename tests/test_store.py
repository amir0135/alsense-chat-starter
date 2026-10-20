import json
import sqlite3

import pytest
from helpers import app_client, flow, login, reply, submit

from alsense_chat.adapters.synthetic.services import SyntheticIdentity
from alsense_chat.app import create_app
from alsense_chat.config import Settings
from alsense_chat.store import MIGRATIONS, SCHEMA_VERSION, SchemaError, Store


def legacy_database(path) -> str:
    """A database written by the starter before schema versioning (user_version 0)."""
    token = "legacy-token"
    db = sqlite3.connect(path)
    db.executescript(MIGRATIONS[0])
    user = SyntheticIdentity().users_me("alice").model_dump_json()
    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", ("legacy-session", Store.digest(token), user, 4102444800))
    db.execute("INSERT INTO actions VALUES (?, ?, ?, ?, NULL)", ("legacy-action", "legacy-session", "report", "{}"))
    db.commit()
    db.close()
    return token


def test_legacy_databases_migrate_in_place_and_keep_sessions(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    token = legacy_database(path)
    store = Store(path)
    assert store.status()["schema_version"] == SCHEMA_VERSION and store.status()["current"]
    session = store.authenticate(token)
    assert (session.id, session.user.id, session.faults, session.scenario) == (
        "legacy-session",
        "alice",
        frozenset(),
        "",
    )
    Store(path)  # Migrating again is a no-op.
    assert store.status()["counts"]["sessions"] == 1


def test_newer_schemas_are_refused_with_a_reset_hint(tmp_path):
    path = str(tmp_path / "future.sqlite3")
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaError, match="dev.py reset"):
        Store(path)


def test_reset_removes_the_database_and_side_files(tmp_path):
    path = tmp_path / "data" / "demo.sqlite3"
    Store(str(path))
    (tmp_path / "data" / "demo.sqlite3-wal").write_text("")
    removed = Store.reset(str(path))
    assert sorted(removed) == sorted([str(path), str(path) + "-wal"])
    assert not path.exists() and Store.reset(str(path)) == []


def test_reset_on_start_gives_a_clean_demo(tmp_path):
    settings = Settings(db=str(tmp_path / "data" / "demo.sqlite3"))
    with app_client(tmp_path, db=settings.db) as client:
        headers = login(client)
    with app_client(tmp_path, db=settings.db, reset_on_start=True) as client:
        assert client.get("/api/session", headers=headers).status_code == 401


def test_audit_records_each_interaction_without_tokens_or_free_text(tmp_path):
    with app_client(tmp_path) as client:
        response = client.post("/api/demo/session", json={"persona": "bob"})
        token = response.json()["token"]
        headers = {"Authorization": "Bearer " + token, "X-Correlation-ID": "demo-correlation-1"}
        result = flow(client, headers, ["no", "Demo South"])
        secret_summary = "Freezer room 4 smells of ammonia"
        ticket = {**result["flow"]["ticket"], "summary": secret_summary}
        draft = reply(client, headers, "/ticket " + json.dumps(ticket))["actions"][0]
        assert submit(client, headers, draft["id"]).status_code == 200
    trail = Store(str(tmp_path / "test.sqlite3")).audit_trail()
    assert [record["event"] for record in trail] == ["login", "flow", "chat", "submit"]
    flow_record, ticket_record, submit_record = trail[1:]
    assert flow_record["correlation_id"] == "demo-correlation-1"
    assert (flow_record["flow_id"], flow_record["flow_version"]) == ("site-connection", "1.0.0")
    assert flow_record["outcome"] == "ticket:needs_investigation"
    assert {"tool": "check_site_health", "result": "open_checks:none"} in flow_record["tools"]
    assert (ticket_record["confirmation"], ticket_record["outcome"]) == ("prepared", "answer")
    assert ticket_record["inputs"][0]["summary_chars"] == len(secret_summary)
    assert (submit_record["confirmation"], submit_record["outcome"]) == ("confirmed", "received")
    stored = json.dumps(trail)
    assert token not in stored and secret_summary not in stored and "Demo South" not in stored


def test_audit_records_failures_with_their_category(tmp_path):
    with app_client(tmp_path, "health_unavailable") as client:
        flow(client, login(client), ["no", "Demo North"])
    record = Store(str(tmp_path / "test.sqlite3")).audit_trail()[-1]
    assert (record["response_category"], record["error_category"]) == ("error:unavailable", "unavailable")
    assert record["outcome"] == "ticket:lookup_unavailable"


def test_unknown_audit_fields_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown audit fields"):
        Store(str(tmp_path / "x.sqlite3")).audit(correlation_id="x", event="chat", message="free text")


def test_store_creates_its_data_directory(tmp_path):
    create_app(Settings(db=str(tmp_path / "nested" / "data" / "demo.sqlite3")))
    assert (tmp_path / "nested" / "data" / "demo.sqlite3").is_file()
