"""Request timings: numbers per stage in the audit record and in spans, never message text."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from helpers import app_client, events, login, reply, submit
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import alsense_chat.app
from alsense_chat import timing
from alsense_chat.runner import FoundryRunner
from alsense_chat.store import MIGRATIONS, SCHEMA_VERSION, Store

SITES = "Which sites can I access?"
REPORT = "Create a temperature report for Demo North for the last seven days"
SPAN_ATTRIBUTES = {"alsense.correlation_id", "alsense.stage", "alsense.tool", "alsense.result", "alsense.round_kind"}


def trail(tmp_path, *kinds: str) -> list[dict]:
    records = Store(str(tmp_path / "test.sqlite3")).audit_trail()
    return [record for record in records if record["event"] in kinds] if kinds else records


class Call:
    type = "function_call"

    def __init__(self, name: str, arguments: dict):
        self.name, self.arguments, self.call_id = name, json.dumps(arguments), f"call-{name}"

    def model_dump(self, **_):
        return {"type": self.type, "name": self.name, "arguments": self.arguments, "call_id": self.call_id}


def foundry(monkeypatch, outputs: list) -> dict:
    """Model mode with a fake Responses API: a (tool, arguments) pair calls a tool, a dict answers."""

    def create(**_):
        output = outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, dict):
            return SimpleNamespace(status="completed", output=[], output_text=json.dumps(output))
        return SimpleNamespace(status="completed", output=[Call(*output)])

    def runner(settings):
        instance = FoundryRunner.__new__(FoundryRunner)
        instance.deployment = settings.deployment
        instance.client = SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)
        instance.project = SimpleNamespace(close=lambda: None)
        instance.credential = SimpleNamespace(close=lambda: None)
        return instance

    monkeypatch.setattr(alsense_chat.app, "FoundryRunner", runner)
    return {"agent_mode": "foundry", "foundry_endpoint": "https://foundry.example.invalid", "deployment": "test"}


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(timing, "TRACER", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


def test_a_scripted_chat_turn_records_each_stage(tmp_path):
    with app_client(tmp_path) as client:
        events(client, login(client), SITES)
    [record] = trail(tmp_path, "chat")
    ms, count = record["timings"]["ms"], record["timings"]["count"]
    assert record["assistant"] == "scripted"
    assert set(ms) == {
        "session",
        "route",
        "tools",
        "tool.get_permitted_sites",
        "guard",
        "persist",
        "first_event",
        "reply",
        "total",
    }
    assert all(value >= 0 for value in ms.values()) and set(count.values()) == {1}
    assert ms["tools"] == ms["tool.get_permitted_sites"]
    assert ms["first_event"] <= ms["reply"] <= ms["total"]
    assert sum(ms[key] for key in ("session", "route", "tools", "guard", "persist")) <= ms["total"] + 0.1


def test_reports_confirmations_and_status_reads_are_timed(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        action = reply(client, headers, REPORT)["actions"][0]["id"]
        assert submit(client, headers, action).status_code == 200
        assert client.get(f"/api/actions/{action}/status", headers=headers).status_code == 200
        reply(client, headers, '/flow site-connection ["no", "Demo North"]')
    chat, confirmed, status, flow = trail(tmp_path, "chat", "submit", "status", "flow")
    assert chat["timings"]["count"]["tools"] == 3
    assert {"tool.get_permitted_sites", "tool.list_report_types", "tool.prepare_report"} <= set(chat["timings"]["ms"])
    assert set(confirmed["timings"]["ms"]) == {"session", "tools", "tool.submit_report", "total"}
    assert set(status["timings"]["ms"]) == {"session", "tools", "tool.get_report_status", "total"}
    assert (confirmed["assistant"], status["assistant"], flow["assistant"]) == (None, None, "guided")
    assert {"route", "tool.check_site_health", "persist", "total"} <= set(flow["timings"]["ms"])


def test_model_rounds_that_select_tools_are_timed_apart_from_the_answer(tmp_path, monkeypatch):
    settings = foundry(
        monkeypatch,
        [
            ("get_permitted_sites", {}),
            {"status": "answer", "text": "You have access to 42 sites.", "source_ids": ["tool-1"]},
            {"status": "answer", "text": "You have access to Demo North.", "source_ids": ["tool-1"]},
        ],
    )
    with app_client(tmp_path, **settings) as client:
        assert reply(client, login(client), SITES)["status"] == "answer"
    [record] = trail(tmp_path, "chat")
    ms, count = record["timings"]["ms"], record["timings"]["count"]
    assert record["assistant"] == "model"
    # The first answer fails the number check, so a repair round answers again.
    assert (count["model_tool_selection"], count["model_answer"], count["guard"]) == (1, 2, 2)
    assert {"session", "history", "route", "tool.get_permitted_sites", "persist", "total"} <= set(ms)
    assert "model_failed" not in ms


def test_failures_still_record_their_timings(tmp_path, monkeypatch):
    with app_client(tmp_path, "sites_unavailable") as client:
        assert reply(client, login(client), SITES)["status"] == "error"
    [failed_tool] = trail(tmp_path, "chat")
    assert failed_tool["tools"] == [{"tool": "get_permitted_sites", "result": "error:unavailable"}]
    assert failed_tool["timings"]["count"]["tool.get_permitted_sites"] == 1

    settings = foundry(monkeypatch, [ConnectionError("unreachable")])
    with app_client(tmp_path / "model", **settings) as client:
        assert reply(client, login(client), SITES)["error"]["category"] == "unavailable"
    [failed_model] = trail(tmp_path / "model", "chat")
    assert failed_model["timings"]["count"]["model_failed"] == 1

    def broken(message, ctx):
        raise RuntimeError("The run broke.")
        yield

    monkeypatch.setattr(alsense_chat.app, "demo_run", broken)
    with app_client(tmp_path / "broken") as client:
        response = client.post("/api/chat", headers=login(client), json={"message": SITES})
    assert response.text.startswith("event: error")
    [runtime_error] = trail(tmp_path / "broken", "chat")
    assert runtime_error["response_category"] == "runtime_error"
    assert set(runtime_error["timings"]["ms"]) == {"session", "first_event", "total"}


def test_stage_spans_carry_only_ids_and_categories(tmp_path, spans):
    with app_client(tmp_path) as client:
        headers = {**login(client), "X-Correlation-ID": "trace-stage-spans"}
        reply(client, headers, REPORT)
    finished = spans.get_finished_spans()
    assert {"alsense.session", "alsense.route", "alsense.tool", "alsense.guard", "alsense.persist"} <= {
        span.name for span in finished
    }
    for span in finished:
        assert set(span.attributes) <= SPAN_ATTRIBUTES, span.name
        assert span.attributes["alsense.correlation_id"] == "trace-stage-spans"
        assert not span.events, span.name
    assert {span.attributes["alsense.tool"] for span in finished if span.name == "alsense.tool"} == {
        "get_permitted_sites",
        "list_report_types",
        "prepare_report",
    }
    text = json.dumps([dict(span.attributes) for span in finished])
    assert "Demo North" not in text and "temperature" not in text and "seven" not in text


def test_a_failed_stage_names_the_exception_type_only(spans):
    timings = timing.Timings("trace-failed-stage")
    with pytest.raises(ValueError), timings.stage("guard"):
        raise ValueError("The reply said: Demo North has 42 alarms")
    [span] = spans.get_finished_spans()
    assert (span.status.status_code.name, span.status.description) == ("ERROR", "ValueError")
    assert not span.events and timings.count == {"guard": 1}


def test_client_timings_are_numbers_for_a_request_of_the_same_session(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        response = client.post("/api/chat", headers=headers, json={"message": SITES})
        body = {
            "correlation_id": response.headers["x-correlation-id"],
            "headers": 12,
            "first_event": 15,
            "reply": 40,
            "rendered": 55,
        }
        assert client.post("/api/timings", json=body).status_code == 401
        for bad in (
            {**body, "message": SITES},
            {**body, "reply": -1},
            {**body, "reply": 1.5},
            {**body, "reply": "40"},
            {**body, "reply": True},
            {**body, "reply": 600_001},
            {**body, "correlation_id": "bad id!"},
            {key: value for key, value in body.items() if key != "headers"},
        ):
            assert client.post("/api/timings", headers=headers, json=bad).status_code == 422, bad
        unknown = {**body, "correlation_id": "unknown-request-id"}
        assert client.post("/api/timings", headers=headers, json=unknown).status_code == 404
        assert client.post("/api/timings", headers=login(client, "bob"), json=body).status_code == 404
        accepted = client.post("/api/timings", headers=headers, json=body)
        assert (accepted.status_code, accepted.content) == (204, b"")
        assert client.post("/api/timings", headers=headers, json=body).status_code == 409
        reused = {**headers, "X-Correlation-ID": "reused-request-id"}
        reply(client, reused, SITES)
        reply(client, reused, SITES)
        ambiguous = {**body, "correlation_id": "reused-request-id"}
        assert client.post("/api/timings", headers=headers, json=ambiguous).status_code == 409
    record = trail(tmp_path, "client_timing")[-1]
    assert record["correlation_id"] == body["correlation_id"]
    assert record["timings"] == {
        "ms": {"headers": 12, "first_event": 15, "reply": 40, "rendered": 55},
        "count": {"headers": 1, "first_event": 1, "reply": 1, "rendered": 1},
    }
    assert SITES not in json.dumps(trail(tmp_path))


def test_the_audit_refuses_timings_that_are_not_numbers_for_known_stages(tmp_path):
    store = Store(str(tmp_path / "x.sqlite3"))
    for timings in (
        {"ms": {"route": "slow"}, "count": {"route": 1}},
        {"ms": {"route": True}, "count": {"route": 1}},
        {"ms": {"route": float("nan")}, "count": {"route": 1}},
        {"ms": {"route": -1.0}, "count": {"route": 1}},
        {"ms": {"route": 1.0}, "count": {"route": 0}},
        {"ms": {"route": 1.0}, "count": {"route": 1.5}},
        {"ms": {"route": 1.0}, "count": {}},
        {"ms": {"Which sites can I access?": 1.0}, "count": {"Which sites can I access?": 1}},
        {"ms": {"customer.secret": 1.0}, "count": {"customer.secret": 1}},
        {"ms": {"tool.customer_secret": 1.0}, "count": {"tool.customer_secret": 1}},
        {"ms": {"tool.site-a": 1.0}, "count": {"tool.site-a": 1}},
        {"text": {"route": 1}},
        [1, 2],
    ):
        with pytest.raises(ValueError, match="Timings"):
            store.audit(correlation_id="x", event="chat", timings=timings)
    store.audit(
        correlation_id="x",
        event="chat",
        timings={"ms": {"tool.prepare_report": 1.5}, "count": {"tool.prepare_report": 2}},
    )


def test_audit_records_from_before_timings_migrate_without_them(tmp_path):
    path = str(tmp_path / "v3.sqlite3")
    db = sqlite3.connect(path)
    for script in MIGRATIONS[:3]:
        db.executescript(script)
    db.execute("PRAGMA user_version = 3")
    db.execute(
        "INSERT INTO audit (created, correlation_id, event, tools, inputs) "
        "VALUES (0, 'old-request', 'chat', '[]', '[]')"
    )
    db.commit()
    db.close()
    store = Store(path)
    assert store.status()["schema_version"] == SCHEMA_VERSION == 4
    [record] = store.audit_trail()
    assert (record["assistant"], record["timings"]) == (None, {})


def test_the_latency_report_groups_requests_and_joins_client_timings(tmp_path, capsys):
    with app_client(tmp_path) as client:
        alice, bob = login(client), login(client, "bob")
        for rendered in (9, 30, 12):
            response = client.post("/api/chat", headers=alice, json={"message": SITES})
            body = {"correlation_id": response.headers["x-correlation-id"], "headers": 5, "rendered": rendered}
            assert client.post("/api/timings", headers=alice, json=body).status_code == 204
        # A reused correlation id joins the request of the same session.
        shared = {"X-Correlation-ID": "shared-request-id"}
        reply(client, {**alice, **shared}, SITES)
        reply(client, {**bob, **shared}, REPORT)
        body = {"correlation_id": "shared-request-id", "headers": 7}
        assert client.post("/api/timings", headers=alice, json=body).status_code == 204
    assert timing.main(["--db", str(tmp_path / "test.sqlite3")]) == 0
    lines = capsys.readouterr().out.splitlines()
    sections = {}
    for index, line in enumerate(lines):
        if line.startswith("chat · "):
            sections[line] = {row.split()[0]: row.split()[1:] for row in lines[index + 2 : lines.index("", index)]}
    sites, reports = (
        sections["chat · scripted · sites (4 requests)"],
        sections["chat · scripted · sites+reporting (1 request)"],
    )
    assert sites["client.headers"] == ["4", "1.0", "5.0", "7.0", "7.0"]
    assert sites["client.rendered"] == ["3", "1.0", "12.0", "30.0", "30.0"]
    assert list(sites)[:3] == ["session", "route", "tools"] and "(other)" in sites
    assert not any(stage.startswith("client.") for stage in reports)
    assert timing.main(["--db", str(tmp_path / "missing.sqlite3")]) == 1


def test_percentiles_use_the_nearest_rank():
    values = [float(value) for value in range(1, 21)]
    assert (timing.percentile(values, 0.5), timing.percentile(values, 0.95)) == (10.0, 19.0)
    assert timing.percentile([7.0], 0.95) == 7.0
