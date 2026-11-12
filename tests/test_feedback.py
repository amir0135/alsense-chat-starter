"""Answer feedback: one rating per answer id, with categories and numbers only, reviewable later."""

import json
import sqlite3

import pytest
from helpers import app_client, flow, login, reply

import alsense_chat.app
from alsense_chat import feedback
from alsense_chat.flows import load_flows
from alsense_chat.store import MIGRATIONS, SCHEMA_VERSION, MissingRecord, Store

SITES = "Which sites can I access?"
REPORT = "Create a temperature report for Demo North for the last seven days"


def store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.sqlite3"))


def rows(tmp_path, table: str) -> list[dict]:
    with store(tmp_path).connection() as db:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def audit(tmp_path, *events: str) -> list[dict]:
    return [record for record in store(tmp_path).audit_trail() if record["event"] in events]


def rate(client, headers: dict, answer_id: str, **body):
    return client.post(f"/api/answers/{answer_id}/feedback", headers=headers, json=body)


def test_every_stored_reply_carries_a_unique_answer_id(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        answer = reply(client, headers, SITES)
        clarification = reply(client, headers, "Create a report for Demo North")
        question = flow(client, headers, [])
        outcome = flow(client, headers, ["no", "Demo North"])
    with app_client(tmp_path / "model-down", "model_unavailable") as client:
        failed = reply(client, login(client), "Hello, what can you do?")
    assert (answer["status"], clarification["status"], question["flow"]["kind"]) == (
        "answer",
        "clarification",
        "question",
    )
    assert (outcome["flow"]["kind"], failed["status"], failed["error"]["category"]) == (
        "outcome",
        "error",
        "unavailable",
    )
    ids = [item["answer_id"] for item in (answer, clarification, question, outcome)]
    assert len(set(ids)) == 4 and all(len(value) >= 16 for value in ids)
    turns = rows(tmp_path, "turns")
    assert [turn["answer_id"] for turn in turns] == ids
    assert all(json.loads(turn["reply"])["answer_id"] == turn["answer_id"] for turn in turns)
    requests = audit(tmp_path, "chat", "flow")
    assert [turn["correlation_id"] for turn in turns] == [record["correlation_id"] for record in requests]
    [failed_turn] = rows(tmp_path / "model-down", "turns")
    assert failed_turn["answer_id"] == failed["answer_id"]


def test_a_run_that_fails_stores_no_answer_and_gets_no_id(tmp_path, monkeypatch):
    def broken(message, ctx):
        raise RuntimeError("The run broke.")
        yield

    monkeypatch.setattr(alsense_chat.app, "demo_run", broken)
    with app_client(tmp_path) as client:
        response = client.post("/api/chat", headers=login(client), json={"message": SITES})
    assert response.text.startswith("event: error") and "answer_id" not in response.text
    assert rows(tmp_path, "turns") == []


def test_one_rating_per_answer_that_can_change(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        answer_id = reply(client, headers, REPORT)["answer_id"]
        first = rate(client, headers, answer_id, rating="up")
        assert (first.status_code, first.json()) == (200, {"answer_id": answer_id, "rating": "up", "reason": None})
        changed = rate(client, headers, answer_id, rating="down", reason="too_slow").json()
        assert (changed["rating"], changed["reason"]) == ("down", "too_slow")
        assert rate(client, headers, answer_id, rating="up").json()["reason"] is None
    [row] = rows(tmp_path, "feedback")
    assert (row["answer_id"], row["user_id"], row["rating"], row["reason"]) == (answer_id, "alice", "up", None)
    assert row["updated"] >= row["created"]
    [chat] = audit(tmp_path, "chat")
    assert row["correlation_id"] == chat["correlation_id"]
    context = json.loads(row["context"])
    assert context == {
        "status": "answer",
        "domains": ["sites", "reporting"],
        "error_category": None,
        "assistant": "scripted",
        "tools": chat["tools"],
        "data_source": "synthetic",
        "app_version": chat["app_version"],
        "total_ms": chat["timings"]["ms"]["total"],
    }
    assert [tool["tool"] for tool in context["tools"]] == ["get_permitted_sites", "list_report_types", "prepare_report"]
    assert chat["app_version"].startswith("0.1.0") and chat["data_source"] == "synthetic"
    ratings = audit(tmp_path, "feedback")
    assert [record["outcome"] for record in ratings] == ["up", "down", "up"]
    assert ratings[1]["inputs"] == [{"answer_id": answer_id, "rating": "down", "reason": "too_slow"}]
    assert set(ratings[0]["timings"]["ms"]) == {"session", "persist", "total"}


def test_a_rating_survives_a_restart_and_can_still_change(tmp_path, capsys):
    with app_client(tmp_path) as client:
        headers = login(client)
        answer_id = reply(client, headers, SITES)["answer_id"]
        assert rate(client, headers, answer_id, rating="down", reason="incorrect").status_code == 200
    [before] = rows(tmp_path, "feedback")
    with app_client(tmp_path) as restarted:
        changed = rate(restarted, headers, answer_id, rating="up")
        assert (changed.status_code, changed.json()["rating"]) == (200, "up")
    [after] = rows(tmp_path, "feedback")
    assert (after["answer_id"], after["rating"], after["reason"]) == (answer_id, "up", None)
    assert after["created"] == before["created"] and after["updated"] >= before["updated"]
    assert feedback.main(["--db", str(tmp_path / "test.sqlite3"), "--json"]) == 0
    [reviewed] = json.loads(capsys.readouterr().out)["ratings"]
    assert (reviewed["answer_id"], reviewed["rating"], reviewed["reason"]) == (answer_id, "up", None)


def test_the_context_comes_from_the_request_that_produced_the_answer(tmp_path):
    with app_client(tmp_path) as client:
        headers = {**login(client), "X-Correlation-ID": "reused-request-id"}
        first = reply(client, headers, SITES)
        reply(client, headers, REPORT)
        assert rate(client, headers, first["answer_id"], rating="down").status_code == 200
    [row] = rows(tmp_path, "feedback")
    context = json.loads(row["context"])
    assert (context["domains"], [tool["tool"] for tool in context["tools"]]) == (["sites"], ["get_permitted_sites"])
    assert [record["answer_id"] for record in audit(tmp_path, "chat")] == [
        first["answer_id"],
        rows(tmp_path, "turns")[1]["answer_id"],
    ]


def test_the_context_keeps_known_categories_only():
    vocabulary = feedback.Vocabulary.build(load_flows())
    reply_ = {
        "status": "answer",
        "text": "Call the night shift on 555 0100.",
        "domains": ["sites", "customer_4711"],
        "error": {"category": "freezer-4"},
        "flow": {"id": "site-connection", "version": "1.0.0", "outcome": "resolve", "category": "call_ann_4711"},
    }
    record = {
        "assistant": "model",
        "tools": [
            {"tool": "get_permitted_sites", "result": "match:one"},
            {"tool": "email_the_site_manager", "result": "error:unknown_tool"},
            {"tool": "check_site_health", "result": "open_checks:freezer_4_customer_4711"},
            {"tool": "list_report_types", "result": "report_types:3"},
            {"tool": "search_docs", "result": "customer_4711:ok"},
        ],
        "data_source": "synthetic",
        "app_version": "0.1.0+abc1234",
        "timings": {"ms": {"total": 12.5}},
    }
    context = feedback.answer_context(reply_, record, vocabulary)
    assert context["domains"] == ["sites", "other"] and context["error_category"] == "other"
    assert context["flow"] == {"id": "site-connection", "version": "1.0.0", "outcome": "resolve", "category": "other"}
    assert context["tools"] == [
        {"tool": "get_permitted_sites", "result": "match:one"},
        {"tool": "unknown", "result": "error:unknown_tool"},
        {"tool": "check_site_health", "result": "open_checks"},
        {"tool": "list_report_types", "result": "report_types:3"},
        {"tool": "search_docs", "result": "ok"},
    ]
    assert (context["app_version"], context["total_ms"]) == ("0.1.0+abc1234", 12.5)
    assert not any(text in json.dumps(context) for text in ("555", "4711", "ann", "freezer", "email"))
    assert feedback.answer_context({"status": "answer"}, None, vocabulary) == {
        "status": "answer",
        "domains": [],
        "error_category": None,
    }


def test_an_answer_is_rated_only_once_its_request_is_recorded(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        answer_id = reply(client, headers, SITES)["answer_id"]
        # As if the stream were still open: the request's audit record isn't written yet.
        with store(tmp_path).connection() as db:
            db.execute("UPDATE audit SET answer_id=NULL WHERE answer_id=?", (answer_id,))
        assert rate(client, headers, answer_id, rating="up").status_code == 409
        with store(tmp_path).connection() as db:
            db.execute("UPDATE audit SET answer_id=? WHERE event='chat'", (answer_id,))
        assert rate(client, headers, answer_id, rating="up").status_code == 200
    assert [record["error_category"] for record in audit(tmp_path, "feedback")] == ["pending", None]


def test_only_the_session_that_received_an_answer_can_rate_it(tmp_path):
    with app_client(tmp_path) as client:
        alice = login(client)
        answer_id = reply(client, alice, SITES)["answer_id"]
        assert rate(client, {}, answer_id, rating="up").status_code == 401
        assert rate(client, alice, "unknown-answer-id", rating="up").status_code == 404
        new_conversation = client.post("/api/session/new", headers=alice).json()["token"]
        assert rate(client, {"Authorization": "Bearer " + new_conversation}, answer_id, rating="up").status_code == 404
        assert rate(client, login(client, "bob"), answer_id, rating="up").status_code == 404
        for body in (
            {"rating": "sideways"},
            {"rating": "down", "reason": "rude"},
            {"rating": "up", "reason": "too_slow"},
            {"rating": "down", "comment": "It was wrong."},
            {},
        ):
            assert client.post(f"/api/answers/{answer_id}/feedback", headers=alice, json=body).status_code == 422, body
        waiting = flow(client, alice, [])
        assert rate(client, alice, waiting["answer_id"], rating="down").status_code == 409
    assert rows(tmp_path, "feedback") == []
    failures = audit(tmp_path, "feedback")
    assert [record["error_category"] for record in failures] == ["not_found", "not_found", "not_found", "not_rateable"]
    assert all(record["inputs"] == [] and record["outcome"] == "not_recorded" for record in failures)
    assert "unknown-answer-id" not in json.dumps(failures)


def test_feedback_holds_no_question_answer_or_flow_text(tmp_path):
    question = "Which sites can I access before the freezer audit in room 4711?"
    with app_client(tmp_path) as client:
        headers = login(client, "bob")
        answer = reply(client, headers, question)
        assert rate(client, headers, answer["answer_id"], rating="down", reason="incorrect").status_code == 200
        outcome = flow(client, headers, ["no", "Demo South"])
        assert rate(client, headers, outcome["answer_id"], rating="up").status_code == 200
    stored = json.dumps(rows(tmp_path, "feedback")) + json.dumps(audit(tmp_path, "feedback"))
    for text in (question, "4711", answer["text"], outcome["text"], "Demo South"):
        assert text not in stored, text
    flow_context = json.loads(rows(tmp_path, "feedback")[1]["context"])
    assert flow_context["flow"] == {
        "id": "site-connection",
        "version": "1.0.0",
        "outcome": outcome["flow"]["outcome"],
        "category": outcome["flow"]["category"],
    }
    assert flow_context["assistant"] == "guided"


def test_a_v4_database_migrates_and_keeps_its_turns(tmp_path):
    path = str(tmp_path / "v4.sqlite3")
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:4]:
        db.executescript(script)
    db.execute("PRAGMA user_version = 4")
    db.execute("INSERT INTO turns (session_id, question, reply, created) VALUES ('old-session', 'q', '{}', 0)")
    db.commit()
    db.close()
    migrated = Store(path)
    assert migrated.status()["schema_version"] == SCHEMA_VERSION
    with migrated.connection() as connection:
        assert tuple(connection.execute("SELECT answer_id, correlation_id FROM turns").fetchone()) == (None, None)
        assert connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    with pytest.raises(MissingRecord):
        migrated.answer("old-session", "any-answer-id")


def test_the_review_lists_ratings_with_their_context_and_no_text(tmp_path, capsys):
    with app_client(tmp_path) as client:
        headers = login(client)
        sites, report = reply(client, headers, SITES), reply(client, headers, REPORT)
        assert rate(client, headers, sites["answer_id"], rating="up").status_code == 200
        assert rate(client, headers, report["answer_id"], rating="down", reason="too_slow").status_code == 200
    path = str(tmp_path / "test.sqlite3")
    assert feedback.main(["--db", path]) == 0
    out = capsys.readouterr().out
    assert "2 shown (1 up, 1 down)" in out
    assert "down · too_slow" in out and "domains=sites+reporting" in out and "assistant=scripted" in out
    assert "tools: get_permitted_sites=match:" in out and f"answer={report['answer_id']}" in out
    for text in (SITES, REPORT, sites["text"], report["text"]):
        assert text not in out
    assert feedback.main(["--db", path, "--rating", "down", "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)["ratings"]
    assert (row["rating"], row["reason"], row["status"], row["data_source"]) == (
        "down",
        "too_slow",
        "answer",
        "synthetic",
    )
    assert row["answer_id"] == report["answer_id"] and row["total_ms"] > 0 and row["correlation_id"]
    assert feedback.main(["--db", str(tmp_path / "missing.sqlite3")]) == 1


def kpi_run(tmp_path, monkeypatch) -> dict:
    """Answers of each kind, rated or not, a guided-check step and the menu (not answers) and one failed run."""
    real = alsense_chat.app.demo_run

    def flaky(message, ctx):
        if message == "Break this run":
            raise RuntimeError("The run broke.")
        yield from real(message, ctx)

    monkeypatch.setattr(alsense_chat.app, "demo_run", flaky)
    with app_client(tmp_path) as client:
        headers = login(client)
        answers = {
            "sites": reply(client, headers, SITES),
            "report": reply(client, headers, REPORT),
            "clarification": reply(client, headers, "Create a report for Demo North"),
            "waiting": flow(client, headers, []),
            "outcome": flow(client, headers, ["no", "Demo North"]),
            "menu": reply(client, headers, "/flows"),
            "unrated": reply(client, headers, SITES),
        }
        assert client.post("/api/chat", headers=headers, json={"message": "Break this run"}).text.startswith(
            "event: error"
        )
        for name, body in (
            ("sites", {"rating": "up"}),
            ("report", {"rating": "down", "reason": "incorrect"}),
            ("clarification", {"rating": "down"}),
            ("outcome", {"rating": "up"}),
        ):
            assert rate(client, headers, answers[name]["answer_id"], **body).status_code == 200
    return answers


def test_the_kpi_counts_satisfied_answers_against_answers_given(tmp_path, monkeypatch, capsys):
    answers = kpi_run(tmp_path, monkeypatch)
    path = str(tmp_path / "test.sqlite3")
    assert feedback.main(["--db", path, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kpi"] == {
        "answers_given": 5,
        "helpful": 2,
        "satisfied_rate": 0.4,
        "rated": 4,
        "rated_share": 0.8,
        "helpful_share_of_rated": 0.5,
        "not_helpful": {
            "total": 2,
            "by_reason": {"incorrect": 1, "incomplete": 0, "too_slow": 0, "other": 0, "no_reason": 1},
        },
        "failed_without_answer": 1,
    }
    assert {
        name: (kpi["answers_given"], kpi["helpful"], kpi["failed_without_answer"])
        for name, kpi in report["by_assistant"].items()
    } == {"guided": (1, 1, 0), "scripted": (4, 1, 1)}
    domains = report["by_domains"]
    assert sum(kpi["answers_given"] for kpi in domains.values()) == 5
    assert (domains["sites"]["answers_given"], domains["sites+reporting"]["not_helpful"]["total"]) == (2, 2)
    assert report["definition"] == list(feedback.DEFINITION) and report["period"] == {"days": None, "from": None}
    # The audit trail counts exactly the replies the feedback endpoint accepts as answers.
    with store(tmp_path).connection() as db:
        turns = db.execute("SELECT answer_id, reply FROM turns").fetchall()
    rateable = {turn["answer_id"] for turn in turns if feedback.rateable(json.loads(turn["reply"]))}
    counted = feedback.counted(feedback.read_requests(path, 0.0))
    assert {row["answer_id"] for row in counted if feedback.answered(row)} == rateable
    assert answers["waiting"]["answer_id"] not in rateable and answers["menu"]["answer_id"] not in rateable

    assert feedback.main(["--db", path]) == 0
    out = capsys.readouterr().out
    assert "  KPI                40.0% (2 of 5)" in out and "  failed, no answer  1 (not part of the KPI)" in out
    assert "(incorrect 1, incomplete 0, too slow 0, other 0, no reason 1)" in out
    assert "KPI, satisfied answers = helpful / answers given." in out
    assert "Ratings: 4 shown (2 up, 2 down), newest first." in out
    for answer in answers.values():
        assert answer["text"] not in out
    for text in (SITES, REPORT, "Create a report for Demo North", "Break this run"):
        assert text not in out


def test_the_kpi_period_is_when_the_answer_was_given(tmp_path, capsys):
    with app_client(tmp_path) as client:
        headers = login(client)
        old, recent = reply(client, headers, SITES), reply(client, headers, REPORT)
        assert rate(client, headers, old["answer_id"], rating="up").status_code == 200
        assert rate(client, headers, recent["answer_id"], rating="down", reason="too_slow").status_code == 200
    with store(tmp_path).connection() as db:
        db.execute("UPDATE audit SET created = created - 10 * 86400 WHERE answer_id = ?", (old["answer_id"],))
    path = str(tmp_path / "test.sqlite3")

    def review(*args: str) -> dict:
        assert feedback.main(["--db", path, "--json", *args]) == 0
        return json.loads(capsys.readouterr().out)

    everything, week = review(), review("--since", "7")
    assert (everything["kpi"]["answers_given"], everything["kpi"]["helpful"]) == (2, 1)
    assert (week["kpi"]["answers_given"], week["kpi"]["helpful"], week["kpi"]["satisfied_rate"]) == (1, 0, 0.0)
    assert [row["answer_id"] for row in week["ratings"]] == [recent["answer_id"]]
    assert week["period"]["days"] == 7 and week["period"]["from"]
    with store(tmp_path).connection() as db:
        db.execute("UPDATE audit SET created = created - 10 * 86400 WHERE answer_id = ?", (recent["answer_id"],))
    empty = review("--since", "1")
    assert (empty["kpi"]["answers_given"], empty["kpi"]["satisfied_rate"], empty["ratings"]) == (0, None, [])
    assert feedback.main(["--db", path, "--since", "1"]) == 0
    out = capsys.readouterr().out
    assert "  KPI                n/a (0 of 0)" in out and "By assistant" not in out
    with pytest.raises(SystemExit):
        feedback.main(["--db", path, "--since", "0"])
