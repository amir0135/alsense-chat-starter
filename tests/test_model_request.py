"""What each model call asks for: the configured reasoning effort and output limit, and the model's own reasoning back
in encrypted form. A response that doesn't complete is logged with the service's reason and fails as before."""

import json

import pytest
from helpers import Round, app_client, foundry, login, reply
from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem

from alsense_chat.store import Store

SITES = "Which sites can I access?"
ANSWER = {"status": "answer", "text": "You have access to Demo North.", "source_ids": ["tool-1"]}


@pytest.mark.parametrize(
    ("overrides", "effort", "limit"),
    [({}, "high", 8000), ({"reasoning_effort": "medium", "max_output_tokens": 4000}, "medium", 4000)],
)
def test_every_model_call_uses_the_configured_reasoning_and_output_limit(
    tmp_path, monkeypatch, overrides, effort, limit
):
    requests: list[dict] = []
    settings = foundry(monkeypatch, [("get_permitted_sites", {}), ANSWER], requests)
    with app_client(tmp_path, **settings, **overrides) as client:
        assert reply(client, login(client), SITES)["status"] == "answer"
    assert len(requests) == 2
    for request in requests:
        assert request["reasoning"] == {"effort": effort}
        assert request["include"] == ["reasoning.encrypted_content"]
        assert (request["max_output_tokens"], request["store"]) == (limit, False)


def test_the_model_gets_its_encrypted_reasoning_back_with_the_tool_results(tmp_path, monkeypatch):
    reasoning = ResponseReasoningItem(id="rs_1", type="reasoning", summary=[], encrypted_content="sealed-reasoning")
    call = ResponseFunctionToolCall(
        type="function_call", id="fc_1", call_id="call_1", name="get_permitted_sites", arguments="{}"
    )
    requests: list[dict] = []
    with app_client(tmp_path, **foundry(monkeypatch, [[reasoning, call], ANSWER], requests)) as client:
        assert reply(client, login(client), SITES)["status"] == "answer"
    passed_back = requests[1]["input"][-3:]
    assert [item["type"] for item in passed_back] == ["reasoning", "function_call", "function_call_output"]
    assert passed_back[0] == {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "sealed-reasoning"}
    assert passed_back[2]["call_id"] == "call_1"


@pytest.mark.parametrize(
    ("reason", "logged"),
    [
        ("max_output_tokens", "max_output_tokens"),
        ("content_filter", "content_filter"),
        ("The service explains itself in free text", "unknown"),
    ],
)
def test_a_response_that_does_not_complete_logs_its_reason_and_fails_as_before(
    tmp_path, monkeypatch, warnings, spans, reason, logged
):
    usage = {"input_tokens": 900, "output_tokens": 8000, "output_tokens_details": {"reasoning_tokens": 7900}}
    with app_client(tmp_path, **foundry(monkeypatch, [Round(ANSWER, usage, "incomplete", reason)])) as client:
        response = client.post("/api/chat", headers=login(client), json={"message": SITES})
    # As before: no answer is released or stored.
    assert response.text.startswith("event: error") and ANSWER["text"] not in response.text
    store = Store(str(tmp_path / "test.sqlite3"))
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    [record] = [record for record in store.audit_trail() if record["event"] == "chat"]
    assert record["timings"]["count"]["model_failed"] == 1 and "model_answer" not in record["timings"]["ms"]
    [warning] = [entry for entry in warnings if getattr(entry, "event", "") == "model_incomplete"]
    assert (warning.response_status, warning.incomplete_reason, warning.max_output_tokens) == (
        "incomplete",
        logged,
        8000,
    )
    assert (warning.output_tokens, warning.reasoning_tokens) == (8000, 7900)
    assert warning.correlation_id == record["correlation_id"] == response.headers["x-correlation-id"]
    assert warning.getMessage() == (
        f"Model response {warning.correlation_id} not completed (incomplete, {logged}): "
        "output tokens 8000 of 8000, reasoning tokens 7900"
    )
    logged_text = warning.getMessage() + json.dumps(vars(warning), default=str)
    assert reason == logged or reason not in logged_text
    assert ANSWER["text"] not in logged_text and SITES not in logged_text
    [attempt] = [span for span in spans.get_finished_spans() if span.name == "alsense.model"]
    assert (attempt.status.status_code.name, attempt.attributes["alsense.result"]) == ("ERROR", f"incomplete_{logged}")
