"""Usage per user: model calls and tokens per request, numbers only, and a per-user summary. No cost, no quota."""

import json
import logging
import sqlite3
import time

import pytest
from helpers import Round, app_client, foundry, login, reply, submit
from openai.types.responses import ResponseUsage

from alsense_chat import usage
from alsense_chat.store import MIGRATIONS, SCHEMA_VERSION, Store

SITES = "Which sites can I access?"
REPORT = "Create a temperature report for Demo North for the last seven days"
ANSWER = {"status": "answer", "text": "You have access to Demo North.", "source_ids": ["tool-1"]}
LOGGED = ("correlation_id", "user_id", "event", "assistant", "outcome", "error_category", "total_ms", "stage_ms")


def trail(tmp_path, *events: str) -> list[dict]:
    records = Store(str(tmp_path / "test.sqlite3")).audit_trail(limit=1000)
    return [record for record in records if record["event"] in events] if events else records


def tokens(input_tokens: int, output_tokens: int, cached: int | None = None) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_tokens_details": None if cached is None else {"cached_tokens": cached},
    }


def test_model_calls_and_their_tokens_are_summed_per_request(tmp_path, monkeypatch, spans):
    first = ResponseUsage(
        input_tokens=100,
        input_tokens_details={"cached_tokens": 40, "cache_write_tokens": 0},
        output_tokens=20,
        output_tokens_details={"reasoning_tokens": 5},
        total_tokens=120,
    )
    rejected = {"status": "answer", "text": "You have access to 42 sites.", "source_ids": ["tool-1"]}
    settings = foundry(
        monkeypatch,
        [
            Round(("get_permitted_sites", {}), first),
            Round(rejected, tokens(150, 30)),
            Round(ANSWER, tokens(170, 10, cached=100)),
        ],
    )
    with app_client(tmp_path, **settings) as client:
        assert reply(client, login(client), SITES)["status"] == "answer"
    [record] = trail(tmp_path, "chat")
    # The first answer fails the number check, so the repair round's tokens count too.
    assert record["usage"] == {
        "model_calls": 3,
        "calls_with_usage": 3,
        "input_tokens": 420,
        "output_tokens": 60,
        "total_tokens": 480,
        "cached_tokens": 140,
        "reasoning_tokens": 5,
    }
    rounds = [span for span in spans.get_finished_spans() if span.name == "alsense.model"]
    assert [
        (span.attributes["gen_ai.usage.input_tokens"], span.attributes["gen_ai.usage.output_tokens"]) for span in rounds
    ] == [(100, 20), (150, 30), (170, 10)]


def test_missing_usage_failed_calls_and_incomplete_rounds_are_counted(tmp_path, monkeypatch):
    settings = foundry(
        monkeypatch,
        [
            ("get_permitted_sites", {}),
            Round(ANSWER, {"input_tokens": 90, "output_tokens": "many", "total_tokens": -1}, status="incomplete"),
        ],
    )
    with app_client(tmp_path, **settings) as client:
        response = client.post("/api/chat", headers=login(client), json={"message": SITES})
    assert response.text.startswith("event: step") and "event: error" in response.text
    [record] = trail(tmp_path, "chat")
    assert record["error_category"] == "runtime_error"
    assert record["usage"] == {
        **dict.fromkeys(usage.KEYS, 0),
        "model_calls": 2,
        "calls_with_usage": 1,
        "input_tokens": 90,
    }

    settings = foundry(monkeypatch, [ConnectionError("unreachable")])
    with app_client(tmp_path / "down", **settings) as client:
        assert reply(client, login(client), SITES)["error"]["category"] == "unavailable"
    [failed] = trail(tmp_path / "down", "chat")
    assert (failed["usage"]["model_calls"], failed["usage"]["calls_with_usage"]) == (1, 0)


def test_usage_reads_objects_and_dicts_and_skips_what_is_not_a_count():
    counted = usage.Usage()
    assert counted.add(None) == {} and counted.add({}) == {}
    assert counted.add({"input_tokens": True, "output_tokens": 2.5, "total_tokens": "7"}) == {}
    reported = counted.add(
        ResponseUsage(
            input_tokens=10,
            input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens=3,
            output_tokens_details={"reasoning_tokens": 2},
            total_tokens=13,
        )
    )
    assert reported == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "cached_tokens": 0,
        "reasoning_tokens": 2,
    }
    assert counted.record()["calls_with_usage"] == 1


def test_scripted_turns_record_no_model_calls(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        assert submit(client, headers, action).status_code == 200
    [chat] = trail(tmp_path, "chat")
    assert chat["usage"] == dict.fromkeys(usage.KEYS, 0)
    assert [record["usage"] for record in trail(tmp_path, "login", "submit")] == [{}, {}]


def test_the_audit_refuses_usage_that_is_not_whole_numbers(tmp_path):
    store = Store(str(tmp_path / "x.sqlite3"))
    for bad in (
        {"model_calls": 1.5},
        {"model_calls": -1},
        {"model_calls": True},
        {"input_tokens": "120"},
        {"prompt": 3},
        {"Which sites can I access?": 1},
        [1],
    ):
        with pytest.raises(ValueError, match="Usage"):
            store.audit(correlation_id="x", event="chat", usage=bad)


def test_log_lines_carry_the_user_categories_and_numbers_only(tmp_path, monkeypatch):
    question = "Which sites can I access before the audit in room 4711?"
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("alsense_chat")
    settings = foundry(
        monkeypatch, [Round(("get_permitted_sites", {}), tokens(100, 20)), Round(ANSWER, tokens(120, 10))]
    )
    logger.addHandler(handler)
    try:
        with app_client(tmp_path, **settings) as client:
            headers = login(client)
            answer = reply(client, headers, question)
            client.post(f"/api/answers/{answer['answer_id']}/feedback", headers=headers, json={"rating": "up"})
            client.get("/api/actions/unknown-action/status", headers=headers)
    finally:
        logger.removeHandler(handler)
    lines = {getattr(record, "event", ""): record for record in records if hasattr(record, "user_id")}
    chat = lines["chat"]
    assert (chat.user_id, chat.assistant, chat.error_category) == ("alice", "model", "")
    assert (chat.model_calls, chat.calls_with_usage, chat.input_tokens, chat.output_tokens) == (2, 2, 220, 30)
    assert (lines["feedback"].user_id, lines["status"].error_category) == ("alice", "not_found")
    for record in lines.values():
        text = record.getMessage() + json.dumps([str(getattr(record, key, "")) for key in LOGGED])
        assert "4711" not in text and "Demo North" not in text


def test_a_v5_database_migrates_and_keeps_its_audit_records(tmp_path):
    path = str(tmp_path / "v5.sqlite3")
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:5]:
        db.executescript(script)
    db.execute("PRAGMA user_version = 5")
    db.execute(
        "INSERT INTO audit (created, correlation_id, persona, event, tools, inputs) "
        "VALUES (0, 'old-request', 'alice', 'chat', '[]', '[]')"
    )
    db.commit()
    db.close()
    migrated = Store(path)
    assert migrated.status()["schema_version"] == SCHEMA_VERSION == 6
    [record] = migrated.audit_trail()
    assert (record["persona"], record["usage"]) == ("alice", {})


def test_the_summary_counts_requests_errors_latency_and_tokens_per_user(tmp_path, capsys):
    with app_client(tmp_path) as client:
        alice = login(client)
        reply(client, alice, SITES)
        action = reply(client, alice, REPORT)["actions"][0]["id"]
        submit(client, alice, action)
        client.get(f"/api/actions/{action}/status", headers=alice)
        client.get("/api/actions/unknown-action/status", headers=alice)
        bob = login(client, "bob")
        reply(client, bob, SITES)
        response = client.post("/api/chat", headers=bob, json={"message": SITES})
        timing = {"correlation_id": response.headers["x-correlation-id"], "headers": 5}
        assert client.post("/api/timings", headers=bob, json=timing).status_code == 204
    path = str(tmp_path / "test.sqlite3")
    store = Store(path)
    model_usage = {**dict.fromkeys(usage.KEYS, 0), "model_calls": 2, "calls_with_usage": 2, "input_tokens": 300}
    store.audit(
        correlation_id="model-request",
        session_id="carol-session",
        persona="carol",
        event="chat",
        timings={"ms": {"total": 900.0}, "count": {"total": 1}},
        usage={**model_usage, "output_tokens": 40, "total_tokens": 340},
    )
    with store.connection() as db:
        db.execute(
            "UPDATE audit SET created = ? WHERE persona = 'bob' AND event = 'login'", (time.time() - 10 * 86400,)
        )

    assert usage.main(["--db", path, "--json"]) == 0
    summary = {entry["user"]: entry for entry in json.loads(capsys.readouterr().out)}
    alice_usage = summary["alice"]
    assert alice_usage["requests"] == {"total": 6, "by_event": {"chat": 2, "login": 1, "status": 2, "submit": 1}}
    assert alice_usage["errors"] == {"total": 1, "by_category": {"not_found": 1}}
    assert {event: value["n"] for event, value in alice_usage["latency_ms"].items()} == {
        "chat": 2,
        "status": 2,
        "submit": 1,
    }
    chat_latency = alice_usage["latency_ms"]["chat"]
    assert 0 < chat_latency["p50"] <= chat_latency["p95"]
    assert alice_usage["model"] == dict.fromkeys(usage.KEYS, 0) and alice_usage["cost"] is None
    assert summary["bob"]["requests"]["by_event"] == {"chat": 2, "login": 1}
    assert summary["carol"]["model"]["total_tokens"] == 340

    assert usage.main(["--db", path, "--since", "5", "--user", "bob"]) == 0
    text = capsys.readouterr().out
    assert "the last 5 days" in text and "bob\n  requests  2 (chat 2)" in text and "alice" not in text
    assert "Cost: not available" in text and SITES not in text
    assert usage.main(["--db", path, "--user", "carol"]) == 0
    assert "calls 2 (usage reported by 2)  tokens: input 300  output 40  total 340" in capsys.readouterr().out
    assert usage.main(["--db", str(tmp_path / "missing.sqlite3")]) == 1
