import json
import logging
import re
from collections.abc import Iterator
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters.errors import IntegrationError, ModelUnavailable
from .config import Settings
from .explain import explain
from .flows import FlowError, match_trigger, menu_reply, run_flow
from .tools import Context, evidence_text, internal_identifiers, model_definition, tool_definitions

log = logging.getLogger(__name__)

MODEL_STATUSES = ("answer", "clarification", "not_in_data", "out_of_scope")
CHECKED = "Passed: sources, numbers, links and wording verified"
SYSTEM = "Fixed system wording"


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # "error" is reserved for the backend: a model reply claiming it is rejected by guard().
    status: Literal["answer", "clarification", "not_in_data", "out_of_scope", "error"]
    text: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(max_length=20)


def guard(raw: str, ctx: Context) -> Reply:
    reply = Reply.model_validate_json(raw)
    if reply.status not in MODEL_STATUSES:
        raise ValueError("Only the backend reports errors.")
    known = {item["source_id"] for item in ctx.evidence}
    if not set(reply.source_ids) <= known:
        raise ValueError("Unknown evidence reference.")
    if reply.status == "answer" and (not reply.source_ids or not ctx.evidence):
        raise ValueError("Answer requires tool evidence.")
    if reply.status == "clarification" and "?" not in reply.text:
        raise ValueError("Clarification requires a question.")
    if re.search(r"https?://|www\.", reply.text, re.IGNORECASE):
        raise ValueError("Links must come from approved structured tool results.")
    for value in internal_identifiers(ctx):
        if re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", reply.text, re.IGNORECASE):
            raise ValueError("Internal identifier in user-facing text.")
    if re.search(r"\b(tenant|site|alarm|source)[_ ]ids?\b", reply.text, re.IGNORECASE):
        raise ValueError("Ask in plain language, never for internal ids.")
    numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", evidence_text(ctx)))
    prose = re.sub(r"(?m)^\s*\d+[.)]\s+", "", reply.text)
    if not set(re.findall(r"\b\d+(?:[.,]\d+)?\b", prose)) <= numbers:
        raise ValueError("Number absent from tool evidence.")
    if ctx.actions and re.search(r"\b(submitted|sent|created successfully)\b", reply.text, re.IGNORECASE):
        raise ValueError("A draft has not been submitted.")
    return reply


def envelope(reply: Reply, ctx: Context, checks: str = CHECKED, error: dict | None = None) -> dict:
    if reply.status in {"not_in_data", "out_of_scope"}:
        ctx.store.record_unanswered(ctx.session_id, reply.status)
    result = {
        **reply.model_dump(),
        "sources": ctx.evidence,
        "actions": ctx.actions,
        "synthetic": ctx.services.kind == "synthetic",
        "why": explain(ctx, checks=checks),
    }
    if error:
        result["error"] = error
    return result


def failure_reply(output: dict, ctx: Context) -> dict:
    """Turn a categorized tool failure into a distinct, user-safe reply."""
    category = output.get("category", "invalid_input")
    if category == "invalid_input":
        reply = Reply(
            status="clarification", text=f"{output['error']} Can you correct that and try again?", source_ids=[]
        )
        return envelope(reply, ctx, SYSTEM)
    if category == "disabled":
        return envelope(Reply(status="out_of_scope", text=output["error"], source_ids=[]), ctx, SYSTEM)
    reply = Reply(status="error", text=output["error"], source_ids=[])
    return envelope(reply, ctx, SYSTEM, {"category": category, "service": output.get("service", "")})


def error_reply(exc: IntegrationError, ctx: Context) -> dict:
    ctx.store.record_unanswered(ctx.session_id, f"{exc.service}_{exc.category}")
    reply = Reply(status="error", text=exc.user_message, source_ids=[])
    return envelope(reply, ctx, SYSTEM, {"category": exc.category, "service": exc.service})


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


REPORT_STATES = {
    "prepared": "This report hasn't been submitted yet. Review the draft and press Confirm.",
    "processing": "Your report is still being prepared. Check again in a moment.",
    "completed": "Your synthetic report is ready.",
    "failed": "The report job failed, so no report was produced. Prepare it again or contact support.",
}
EMPTY = {
    "search_docs": "No approved guidance matches this issue. You can prepare a support request.",
    "resolve_sites": "None of your permitted sites match that. Ask which sites you can access to see them all.",
}


def demo_text(name: str, data, ctx: Context) -> str:
    sites = [site.name for site in ctx.user.sites if site.tenant_id == ctx.user.tenant_id]
    if name == "resolve_sites":
        alarms = [f"{alarm['label']} at {site['name']}" for site in data for alarm in site.get("alarms", [])]
        text = f"You have access to {_names([site['name'] for site in data])}."
        return text + (f" Active alarm: {_names(alarms)}." if alarms else "")
    if name == "list_report_types":
        return f"You can prepare {_names([item['label'] for item in data])} reports as PDF or CSV."
    if name == "search_docs":
        steps = [part.strip().rstrip(".") for doc in data for part in doc["excerpt"].split(". ") if part.strip()]
        listed = "\n".join(f"{index}. {step}." for index, step in enumerate(steps[:4], 1))
        target = sites[0] if len(sites) == 1 else "your site"
        return f"Here is what to check:\n\n{listed}\n\nStill not working? I can prepare a support request for {target}."
    if name.startswith("prepare_"):
        return "Your draft is ready below. Nothing goes out until you review it and press Confirm."
    if name == "get_report_status":
        return REPORT_STATES[data["state"]]
    return "Here is the latest status."


def demo_run(message: str, ctx: Context) -> Iterator[tuple[str, dict]]:
    """Deterministic local harness, not an AI model. Slash commands exercise real tool handlers."""
    commands = {
        "/sites": "resolve_sites",
        "/reports": "list_report_types",
        "/docs": "search_docs",
        "/report": "prepare_report",
        "/ticket": "prepare_support_ticket",
        "/status": "get_report_status",
    }
    command, _, rest = message.partition(" ")
    if command.lower() == "/flows":
        yield "reply", menu_reply(ctx)
        return
    if command.lower() == "/flow":
        flow_id, _, raw_answers = rest.strip().partition(" ")
        try:
            yield from run_flow(flow_id, json.loads(raw_answers) if raw_answers.strip() else [], ctx)
        except (json.JSONDecodeError, FlowError):
            reply = Reply(
                status="clarification",
                text="That guided check couldn't continue. Can you start it again?",
                source_ids=[],
            )
            yield "reply", envelope(reply, ctx, SYSTEM)
        return
    name: str | None = commands.get(command.lower())
    args: dict[str, Any]
    if name is None:
        triggered = match_trigger(ctx.flows, message) if "troubleshooting" in ctx.features else None
        if triggered:
            yield from run_flow(triggered, [], ctx)
            return
        if "model_unavailable" in ctx.faults:
            # The scripted router stands in for the model in demo mode, so it is what "goes down".
            raise ModelUnavailable()
        normalized = message.casefold()
        if "site" in normalized and any(
            word in normalized for word in ("which", "access", "available", "list", "my sites")
        ):
            name, args = "resolve_sites", {"query": "*"}
        elif "report" in normalized and any(word in normalized for word in ("available", "types", "options", "list")):
            name, args = "list_report_types", {}
        elif any(word in normalized for word in ("alarm", "report", "sensor", "offline")):
            name, args = "search_docs", {"query": message}
        else:
            reply = Reply(
                status="out_of_scope",
                text="This demo covers operations documentation, sites, reports and support drafts. "
                "Use the buttons or slash commands.",
                source_ids=[],
            )
            yield "reply", envelope(reply, ctx, SYSTEM)
            return
    elif name == "list_report_types":
        args = {}
    elif name in {"prepare_report", "prepare_support_ticket"}:
        try:
            args = json.loads(rest)
        except json.JSONDecodeError:
            reply = Reply(
                status="clarification",
                text="Can you supply all form fields using the Report or Support button?",
                source_ids=[],
            )
            yield "reply", envelope(reply, ctx, SYSTEM)
            return
    elif name == "get_report_status":
        args = {"action_id": rest.strip()}
    else:
        args = {"query": rest.strip() or ("*" if name == "resolve_sites" else "")}
    yield "step", {"tool": name, "state": "started"}
    output = ctx.execute(name, args)
    yield "step", {"tool": name, "state": "failed" if "error" in output else "done"}
    if "error" in output:
        yield "reply", failure_reply(output, ctx)
        return
    if not output["data"]:
        text = EMPTY.get(name, "The permitted data does not answer that lookup. You can prepare a support ticket.")
        reply = Reply(status="not_in_data", text=text, source_ids=[])
    else:
        reply = Reply(status="answer", text=demo_text(name, output["data"], ctx), source_ids=[output["source_id"]])
    yield "reply", envelope(guard(reply.model_dump_json(), ctx), ctx)


class FoundryRunner:
    def __init__(self, settings: Settings):
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        self.credential = DefaultAzureCredential()
        self.project = AIProjectClient(endpoint=settings.foundry_endpoint, credential=self.credential)
        self.client = self.project.get_openai_client()
        self.deployment = settings.deployment

    def close(self) -> None:
        self.client.close()
        self.project.close()
        self.credential.close()

    def create(self, **request: Any) -> Any:
        try:
            return self.client.responses.create(**request)
        except Exception as exc:
            # Never fall back to the scripted assistant: say the model is unavailable.
            log.warning("Model call failed (%s)", type(exc).__name__)
            raise ModelUnavailable() from exc

    def run(self, message: str, ctx: Context) -> Iterator[tuple[str, dict]]:
        ctx.assistant = "model"
        if "model_unavailable" in ctx.faults:
            raise ModelUnavailable()
        history = ctx.store.history(ctx.session_id)
        items: list[dict[str, Any]] = []
        for turn in history:
            items.extend(
                [
                    {"role": "user", "content": turn["question"]},
                    {
                        "role": "assistant",
                        "content": json.dumps({key: turn["reply"][key] for key in ("status", "text", "source_ids")}),
                    },
                ]
            )
        items.append({"role": "user", "content": message})
        instructions = model_definition()["instructions"]
        tools = tool_definitions(ctx.features)
        repaired = False
        for _ in range(12):
            response = self.create(
                model=self.deployment,
                instructions=instructions,
                input=cast(Any, items),
                tools=cast(Any, tools),
                store=False,
                max_output_tokens=3000,
            )
            if response.status != "completed":
                raise RuntimeError("Model response incomplete or blocked; no answer released.")
            calls = [item for item in response.output if item.type == "function_call"]
            if calls:
                items.extend(item.model_dump(exclude_none=True) for item in response.output)
                for call in calls:
                    yield "step", {"tool": call.name, "state": "started"}
                    try:
                        args = json.loads(call.arguments)
                    except json.JSONDecodeError:
                        output = {"error": "Invalid JSON arguments.", "category": "invalid_input"}
                    else:
                        output = ctx.execute(call.name, args)
                    yield "step", {"tool": call.name, "state": "failed" if "error" in output else "done"}
                    items.append(
                        {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(output)}
                    )
                continue
            try:
                reply = guard(response.output_text, ctx)
            except (ValidationError, ValueError):
                log.warning("Final reply failed structural or evidence checks")
                if not repaired:
                    repaired = True
                    items.extend(item.model_dump(exclude_none=True) for item in response.output)
                    items.append(
                        {
                            "role": "user",
                            "content": "Your reply failed validation. Return the required "
                            "JSON, reference this turn's evidence, use no unsupported numbers or links, "
                            "never show internal ids, tenant ids or tool names (use display names), "
                            "and never claim a draft was sent. Otherwise return not_in_data.",
                        }
                    )
                    continue
                reply = Reply(
                    status="not_in_data",
                    text="I could not verify an answer from the tool evidence. You can prepare a support ticket.",
                    source_ids=[],
                )
            yield "reply", envelope(reply, ctx)
            return
        raise RuntimeError("Tool round limit reached; ask a narrower question.")
