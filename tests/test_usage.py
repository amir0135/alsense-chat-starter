"""Usage per user: model calls and tokens per request, numbers only, a per-user summary and, when prices are set,
an estimated cost. No quota."""

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
# Made-up round prices, US dollars per million tokens: no real rate is ever shipped.
PRICES = {"price_input": 2.0, "price_cached_input": 0.5, "price_output": 8.0}
FLAGS = ["--price-input", "2", "--price-cached-input", "0.5", "--price-output", "8"]


@pytest.fixture
def no_prices(monkeypatch):
    """The report reads prices from the environment: none, unless a test sets them."""
    for name in usage.PRICES.values():
        monkeypatch.delenv(name, raising=False)


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
    assert not hasattr(chat, "estimated_cost_usd"), "no prices are set, so nothing is estimated"
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


def test_the_summary_counts_requests_errors_latency_and_tokens_per_user(tmp_path, capsys, no_prices):
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
    summary = {entry["user"]: entry for entry in json.loads(capsys.readouterr().out)["users"]}
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
    assert alice_usage["model"] == dict.fromkeys(usage.KEYS, 0) and alice_usage["estimated_cost"] is None
    assert summary["bob"]["requests"]["by_event"] == {"chat": 2, "login": 1}
    assert summary["carol"]["model"]["total_tokens"] == 340

    assert usage.main(["--db", path, "--since", "5", "--user", "bob"]) == 0
    text = capsys.readouterr().out
    assert "the last 5 days" in text and "bob\n  requests  2 (chat 2)" in text and "alice" not in text
    assert "Estimated cost: not configured" in text and SITES not in text
    assert usage.main(["--db", path, "--user", "carol"]) == 0
    assert "calls 2 (usage reported by 2)  tokens: input 300  output 40  total 340" in capsys.readouterr().out
    assert usage.main(["--db", str(tmp_path / "missing.sqlite3")]) == 1


def test_the_estimated_cost_prices_each_token_once():
    rates = usage.Prices(input=2.0, cached_input=0.5, output=8.0)
    # 600 uncached and 400 cached input tokens; the 200 reasoning tokens are part of the 300 output tokens.
    record = {"input_tokens": 1000, "cached_tokens": 400, "output_tokens": 300, "reasoning_tokens": 200}
    assert rates.estimate({**record, "total_tokens": 1300}) == pytest.approx((600 * 2 + 400 * 0.5 + 300 * 8) / 1e6)
    assert rates.estimate({}) == 0.0
    assert rates.estimate({"input_tokens": 100, "cached_tokens": 500}) == pytest.approx(100 * 0.5 / 1e6)
    assert usage.prices(dict.fromkeys(("input", "cached_input", "output"))) is None
    with pytest.raises(ValueError, match="Missing: MODEL_PRICE_CACHED_INPUT, MODEL_PRICE_OUTPUT"):
        usage.prices({"input": 2.0, "cached_input": None, "output": None})
    for value in ("cheap", "-1", "nan", "inf"):
        with pytest.raises(ValueError, match="non-negative number of US dollars per million tokens"):
            usage.price("MODEL_PRICE_INPUT", value)
    assert usage.price("MODEL_PRICE_INPUT", " ") is None and usage.price("MODEL_PRICE_INPUT", "0") == 0.0


def test_the_report_estimates_cost_per_user_and_in_total_only_with_prices(tmp_path, capsys, monkeypatch, no_prices):
    path = str(tmp_path / "usage.sqlite3")
    store = Store(path)
    for user, model in (
        # One of carol's three calls reported no usage: the estimate leaves it out and says so.
        ("carol", {"model_calls": 3, "calls_with_usage": 2, "input_tokens": 3000, "cached_tokens": 1000}),
        ("dave", {"model_calls": 1, "calls_with_usage": 1, "input_tokens": 1000}),
    ):
        output = {"carol": {"output_tokens": 500, "reasoning_tokens": 300}, "dave": {"output_tokens": 100}}[user]
        store.audit(correlation_id=f"{user}-request", persona=user, event="chat", usage={**model, **output})

    def report(*args: str) -> dict:
        assert usage.main(["--db", path, "--json", *args]) == 0
        return json.loads(capsys.readouterr().out)

    unset = report()
    assert unset["prices_usd_per_million_tokens"] is None and unset["total"]["estimated_cost"] is None
    assert [user["estimated_cost"] for user in unset["users"]] == [None, None]
    assert usage.main(["--db", path]) == 0
    text = capsys.readouterr().out
    assert "Estimated cost: not configured. Set MODEL_PRICE_INPUT" in text
    assert text.count("estimated cost  not configured") == 3

    carol = (2000 * 2 + 1000 * 0.5 + 500 * 8) / 1e6
    dave = (1000 * 2 + 100 * 8) / 1e6
    priced = report(*FLAGS)
    assert priced["prices_usd_per_million_tokens"] == {"input": 2.0, "cached_input": 0.5, "output": 8.0}
    assert [user["estimated_cost"] for user in priced["users"]] == [
        {"usd": pytest.approx(carol), "calls_without_usage": 1},
        {"usd": pytest.approx(dave), "calls_without_usage": 0},
    ]
    assert priced["total"]["estimated_cost"] == {"usd": pytest.approx(carol + dave), "calls_without_usage": 1}
    assert usage.main(["--db", path, *FLAGS]) == 0
    text = capsys.readouterr().out
    assert "Estimated cost at US dollars per million tokens: input 2, cached input 0.5, output 8." in text
    assert "estimated cost  $0.0085 (1 call without reported usage left out)" in text
    assert "estimated cost  $0.0028\n" in text and "estimated cost  $0.0113 (1 call" in text

    for name, value in (("MODEL_PRICE_INPUT", "2"), ("MODEL_PRICE_CACHED_INPUT", "0.5"), ("MODEL_PRICE_OUTPUT", "8")):
        monkeypatch.setenv(name, value)
    assert report()["total"]["estimated_cost"]["usd"] == pytest.approx(carol + dave)
    # A price on the command line re-estimates recorded usage at that price.
    cheaper = report("--price-output", "4")
    assert cheaper["users"][0]["estimated_cost"]["usd"] == pytest.approx(carol - 500 * 4 / 1e6)
    monkeypatch.delenv("MODEL_PRICE_OUTPUT")
    with pytest.raises(SystemExit):
        usage.main(["--db", path])
    with pytest.raises(SystemExit):
        usage.main(["--db", path, "--price-output", "free"])


def test_a_request_logs_its_estimated_cost_when_prices_are_set(tmp_path, monkeypatch):
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("alsense_chat")
    settings = foundry(
        monkeypatch,
        [Round(("get_permitted_sites", {}), tokens(100, 20)), Round(ANSWER, tokens(120, 10, cached=100))],
    )
    logger.addHandler(handler)
    try:
        with app_client(tmp_path, **settings, **PRICES) as client:
            headers = login(client)
            answer = reply(client, headers, SITES)
            client.post(f"/api/answers/{answer['answer_id']}/feedback", headers=headers, json={"rating": "up"})
    finally:
        logger.removeHandler(handler)
    lines = {getattr(record, "event", ""): record for record in records if hasattr(record, "user_id")}
    # 220 input tokens, 100 of them cached, and 30 output tokens.
    assert lines["chat"].estimated_cost_usd == pytest.approx((120 * 2 + 100 * 0.5 + 30 * 8) / 1e6)
    assert not hasattr(lines["feedback"], "estimated_cost_usd")
    [record] = trail(tmp_path, "chat")
    assert "estimated_cost_usd" not in json.dumps(record), "the audit keeps the tokens, not an estimate"
