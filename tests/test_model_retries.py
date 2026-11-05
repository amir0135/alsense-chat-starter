"""Model calls that fail: the OpenAI SDK's retry policy, applied by the runner so each wait is measured."""

import logging
import time

import httpx2
import openai._constants as sdk
import pytest
from helpers import app_client, foundry, login, reply
from openai import APIConnectionError, BadRequestError, InternalServerError, RateLimitError

import alsense_chat.runner
from alsense_chat import measure
from alsense_chat.runner import INITIAL_RETRY_DELAY, MAX_RETRIES, MAX_RETRY_AFTER, MAX_RETRY_DELAY, retry_wait
from alsense_chat.store import Store

SITES = "Which sites can I access?"
ANSWER = {"status": "answer", "text": "You have access to Demo North.", "source_ids": ["tool-1"]}
REQUEST = httpx2.Request("POST", "https://foundry.example.invalid/openai/v1/responses")


def failed(cls, status: int, headers: dict | None = None):
    return cls("Synthetic failure.", response=httpx2.Response(status, headers=headers, request=REQUEST), body=None)


@pytest.fixture
def waits(monkeypatch):
    """Requested waits; each real wait is 50 ms so the time is measurable without slowing the tests down."""
    requested: list[float] = []

    def sleep(seconds: float) -> None:
        requested.append(seconds)
        time.sleep(0.05)

    monkeypatch.setattr(alsense_chat.runner, "sleep", sleep)
    return requested


@pytest.fixture
def warnings():
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("alsense_chat")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def chat_record(tmp_path) -> dict:
    [record] = [record for record in Store(str(tmp_path / "test.sqlite3")).audit_trail() if record["event"] == "chat"]
    return record


def ask(tmp_path, monkeypatch, outputs: list) -> dict:
    with app_client(tmp_path, **foundry(monkeypatch, outputs)) as client:
        return reply(client, login(client), SITES)


def test_the_policy_matches_the_openai_sdk_defaults():
    assert (MAX_RETRIES, MAX_RETRY_AFTER) == (sdk.DEFAULT_MAX_RETRIES, sdk.MAX_RETRY_AFTER_DELAY)
    assert (INITIAL_RETRY_DELAY, MAX_RETRY_DELAY) == (sdk.INITIAL_RETRY_DELAY, sdk.MAX_RETRY_DELAY)


def test_a_throttled_call_waits_apart_from_the_model_time(tmp_path, monkeypatch, waits, warnings):
    throttled = failed(
        RateLimitError,
        429,
        {"retry-after": "2", "x-ratelimit-remaining-tokens": "0", "x-ratelimit-remaining-requests": "7"},
    )
    assert ask(tmp_path, monkeypatch, [throttled, ("get_permitted_sites", {}), ANSWER])["status"] == "answer"
    assert waits == [2.0]
    record = chat_record(tmp_path)
    ms, count = record["timings"]["ms"], record["timings"]["count"]
    assert (count["model_failed"], count["model_throttled"], count["model_tool_selection"], count["model_answer"]) == (
        1,
        1,
        1,
        1,
    )
    assert "model_retry" not in ms
    assert ms["model_throttled"] >= 45 and ms["model_tool_selection"] < 45 and ms["model_answer"] < 45
    assert record["usage"]["model_calls"] == 3
    [warning] = [entry for entry in warnings if getattr(entry, "event", "") == "model_retry"]
    assert (warning.status_code, warning.error_category, warning.wait_ms, warning.retry) == (429, "throttled", 2000, 1)
    assert (warning.ratelimit_remaining_tokens, warning.ratelimit_remaining_requests) == (0, 7)
    assert warning.correlation_id == record["correlation_id"]


def test_failed_attempts_are_failed_spans(tmp_path, monkeypatch, waits, spans):
    assert ask(tmp_path, monkeypatch, [failed(RateLimitError, 429), ("get_permitted_sites", {}), ANSWER])
    attempts = [span for span in spans.get_finished_spans() if span.name == "alsense.model"]
    assert [(span.status.status_code.name, span.status.description) for span in attempts] == [
        ("ERROR", "RateLimitError"),
        ("UNSET", None),
        ("UNSET", None),
    ]
    assert attempts[0].attributes["alsense.result"] == "status_429"
    [wait] = [span for span in spans.get_finished_spans() if span.name == "alsense.model_wait"]
    assert wait.attributes["alsense.result"] == "status_429"


def test_exhausted_retries_still_say_the_model_is_unavailable(tmp_path, monkeypatch, waits):
    throttled = [failed(RateLimitError, 429, {"retry-after-ms": "1500"}) for _ in range(3)]
    result = ask(tmp_path, monkeypatch, throttled)
    assert (result["status"], result["error"]["category"], result["error"]["service"]) == (
        "error",
        "unavailable",
        "model",
    )
    assert waits == [1.5, 1.5]
    record = chat_record(tmp_path)
    count = record["timings"]["count"]
    assert (count["model_failed"], count["model_throttled"]) == (3, 2)
    assert (record["usage"]["model_calls"], record["usage"]["calls_with_usage"]) == (3, 0)


def test_a_bad_request_is_not_retried(tmp_path, monkeypatch, waits):
    result = ask(tmp_path, monkeypatch, [failed(BadRequestError, 400)])
    assert result["error"]["category"] == "unavailable" and waits == []
    timings = chat_record(tmp_path)["timings"]
    assert timings["count"]["model_failed"] == 1
    assert not {"model_throttled", "model_retry"} & set(timings["ms"])


def test_server_and_connection_errors_are_retried_with_backoff(tmp_path, monkeypatch, waits):
    outputs = [
        failed(InternalServerError, 503),
        APIConnectionError(request=REQUEST),
        ("get_permitted_sites", {}),
        ANSWER,
    ]
    assert ask(tmp_path, monkeypatch, outputs)["status"] == "answer"
    assert 0.375 <= waits[0] <= 0.5 and 0.75 <= waits[1] <= 1.0
    count = chat_record(tmp_path)["timings"]["count"]
    assert (count["model_retry"], count["model_failed"]) == (2, 2)
    assert "model_throttled" not in count


def test_a_wait_over_two_minutes_is_not_retried(tmp_path, monkeypatch, waits):
    result = ask(tmp_path, monkeypatch, [failed(RateLimitError, 429, {"retry-after": "121"})])
    assert result["error"]["category"] == "unavailable" and waits == []
    assert "model_throttled" not in chat_record(tmp_path)["timings"]["ms"]


def test_the_service_can_say_whether_to_retry():
    assert retry_wait(failed(RateLimitError, 429, {"x-should-retry": "false"}), 0) is None
    assert retry_wait(failed(BadRequestError, 400, {"x-should-retry": "true", "retry-after": "3"}), 0) == 3.0
    assert retry_wait(failed(RateLimitError, 429, {"retry-after-ms": "250", "retry-after": "9"}), 1) == 0.25
    assert retry_wait(failed(InternalServerError, 500, {"retry-after": "0"}), 3) == pytest.approx(4.0, rel=0.25)
    assert retry_wait(ValueError("not a model call"), 0) is None


def test_the_measure_command_reports_a_scripted_run(tmp_path, monkeypatch, capsys):
    for name in ("AGENT_MODE", "SESSION_DB", "SYNTHETIC_FAULTS", "CLOUD_DEMO", "FLOWS_FILE", "SCENARIOS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_SOURCE", "synthetic")
    assert measure.main(["--runs", "2", "--db", str(tmp_path / "measure.sqlite3")]) == 0
    out = capsys.readouterr().out
    assert "Measuring 2 run(s) with the scripted assistant on synthetic data." in out
    assert out.count("sites: answer, report: answer, confirm: 200, status: completed") == 2
    assert "chat · scripted · sites (2 requests)" in out and "Usage per user in" in out
    monkeypatch.setenv("DATA_SOURCE", "alsense")
    assert measure.main(["--runs", "1"]) == 2
    assert "synthetic data only" in capsys.readouterr().out
