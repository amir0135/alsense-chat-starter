import json
import logging
import re
from collections.abc import Iterator
from datetime import date
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters.errors import IntegrationError, ModelUnavailable
from .agent import load_agent
from .config import Settings
from .domains import reporting
from .domains.base import Choice
from .explain import explain
from .flows import FlowError, match_trigger, menu_reply, run_flow
from .tools import FEATURE_OFF, Context, evidence_text, internal_identifiers, tool_definitions

log = logging.getLogger(__name__)

MODEL_STATUSES = ("answer", "clarification", "not_in_data", "out_of_scope")
CHECKED = "Passed: sources, numbers, links and wording verified"
SYSTEM = "Fixed system wording"
AsksFor = Literal["site", "report_type", "period", "format", "frequency", "recipients", "start_date", "other"]
# A claim that something now exists or went out. Allowed only after the service confirmed it in this turn.
CLAIM = re.compile(
    r"\b(?:was|were|is|are|has\s+been|have\s+been|now)\s+(?:(?:now|successfully|already)\s+)?"
    r"(?:created|scheduled|submitted|sent|confirmed|generated|set\s+up)\b"
    r"|\b(?:created|scheduled|submitted|sent|generated)\s+successfully\b"
    r"|\b(?:I|we)(?:['’]ve|\s+have)?\s+(?:(?:just|now|successfully|already|gone\s+ahead\s+and)\s+)*"
    r"(?:created|scheduled|submitted|sent|generated|set\s+up)\b",
    re.IGNORECASE,
)
NEGATIONS = {"no", "not", "nothing", "never", "none", "neither", "nor"}
# "...is created once you press Confirm" describes what will happen, not what happened.
CONDITION = (
    r"(?:only\s+)?(?:once|after|when|if|until|before|as\s+soon\s+as)\s+you\s+(?:press|confirm|review|click|approve)\b"
)
NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\b")
PREPARED = "Your draft is ready below. Nothing goes out until you review it and press Confirm."


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # "error" is reserved for the backend: a model reply claiming it is rejected by guard().
    status: Literal["answer", "clarification", "not_in_data", "out_of_scope", "error"]
    text: str = Field(min_length=1, max_length=6000)
    source_ids: list[str] = Field(max_length=20)
    asks_for: AsksFor | None = None


def claim(text: str) -> str | None:
    """The first wording that says something was created, scheduled or sent, unless it is negated or conditional."""
    for match in CLAIM.finditer(text):
        sentence = text[: match.start()]
        sentence = sentence[max(sentence.rfind(mark) for mark in ".!?\n") + 1 :]
        nearby = [word.strip("'\"“”‘’").casefold() for word in re.split(r"[,;:]", sentence)[-1].split()[-3:]]
        if any(word in NEGATIONS or word.endswith(("n't", "n’t")) for word in nearby):
            continue
        if re.search(CONDITION, sentence, re.IGNORECASE) or re.match(rf"\s*{CONDITION}", text[match.end() :], re.I):
            continue
        return match.group(0)
    return None


def numbers(text: str) -> set[str]:
    # "05" in an ISO date and "5" in "5 Nov 2026" are the same number.
    return {re.sub(r"^0+(?=\d)", "", value) for value in NUMBER.findall(text)}


def confirmed_by_service(ctx: Context) -> bool:
    return any(item["tool"] == "get_report_status" and item["data"]["state"] != "prepared" for item in ctx.evidence)


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
    if reply.asks_for and reply.status != "clarification":
        raise ValueError("Only a clarification asks for something.")
    if re.search(r"https?://|www\.", reply.text, re.IGNORECASE):
        raise ValueError("Links must come from approved structured tool results.")
    for value in internal_identifiers(ctx):
        if re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", reply.text, re.IGNORECASE):
            raise ValueError("Internal identifier in user-facing text.")
    if re.search(r"\b(tenant|site|alarm|source)[_ ]ids?\b", reply.text, re.IGNORECASE):
        raise ValueError("Ask in plain language, never for internal ids.")
    prose = re.sub(r"(?m)^\s*\d+[.)]\s+", "", reply.text)
    if not numbers(prose) <= numbers(evidence_text(ctx)):
        raise ValueError("Number absent from tool evidence.")
    if ctx.actions and re.search(r"\b(submitted|sent|created successfully)\b", reply.text, re.IGNORECASE):
        raise ValueError("A draft has not been submitted.")
    if claim(reply.text) and (ctx.actions or not confirmed_by_service(ctx)):
        raise ValueError("Only a result the service confirmed can be reported as created, scheduled or sent.")
    return reply


def envelope(
    reply: Reply,
    ctx: Context,
    checks: str = CHECKED,
    error: dict | None = None,
    choices: list[Choice] | None = None,
    builder: dict | None = None,
) -> dict:
    if reply.status in {"not_in_data", "out_of_scope"}:
        ctx.store.record_unanswered(ctx.session_id, reply.status)
    result = {
        **reply.model_dump(exclude_none=True),
        "sources": ctx.evidence,
        "actions": ctx.actions,
        "synthetic": ctx.services.kind == "synthetic",
        "domains": list(ctx.domains),
        "why": explain(ctx, checks=checks),
    }
    if error:
        result["error"] = error
    if choices:
        result["choices"] = [{"label": choice.label, "message": choice.message} for choice in choices]
    if builder:
        result["builder"] = builder
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


def plan_reply(plan: reporting.Plan, ctx: Context) -> dict:
    """Turn the scripted report planner's result into a checked reply."""
    if plan.kind == "failure":
        assert plan.output is not None
        return failure_reply(plan.output, ctx)
    if plan.kind == "prepared":
        assert plan.output is not None
        reply = Reply(status="answer", text=PREPARED, source_ids=[plan.output["source_id"]])
        return envelope(guard(reply.model_dump_json(), ctx), ctx)
    if plan.kind == "not_found":
        reply = Reply(status="not_in_data", text=plan.text, source_ids=[])
        return envelope(guard(reply.model_dump_json(), ctx), ctx, SYSTEM)
    reply = Reply(status="clarification", text=plan.text, source_ids=[], asks_for=cast(Any, plan.asks_for))
    return envelope(guard(reply.model_dump_json(), ctx), ctx, SYSTEM, choices=plan.choices, builder=plan.builder)


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _day(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.day} {parsed:%b %Y}"


REPORT_STATES = {
    "prepared": "This report hasn't been confirmed yet. Review the draft and press Confirm.",
    "processing": "Your report is still being prepared. Check again in a moment.",
    "completed": "Your synthetic report is ready. Download it with the link below before the link expires.",
    "failed": "The report job failed, so no report was produced. Prepare it again or contact support.",
}
EMPTY = {
    "search_docs": "No approved guidance matches this issue. You can prepare a support request.",
    "get_permitted_sites": "You don't have access to any sites in this demo.",
}


def demo_text(name: str, data, ctx: Context) -> str:
    sites = [site.name for site in ctx.user.sites if site.tenant_id == ctx.user.tenant_id]
    if name == "get_permitted_sites":
        found = data["sites"]
        alarms = [f"{alarm['label']} at {site['name']}" for site in found for alarm in site.get("alarms", [])]
        if data["match"] == "all":
            text = f"You have access to {_names([site['name'] for site in found])}."
        else:
            text = f"{found[0]['name']} is one of your sites."
        return text + (f" Active alarm: {_names(alarms)}." if alarms else "")
    if name == "list_report_types":
        text = f"You can prepare {_names([item['label'] for item in data])} reports."
        scheduled = [item["label"] for item in data if item.get("schedule")]
        return text + (f" {_names(scheduled)} reports can also be scheduled." if scheduled else "")
    if name == "search_docs":
        steps = [part.strip().rstrip(".") for doc in data for part in doc["excerpt"].split(". ") if part.strip()]
        listed = "\n".join(f"{index}. {step}." for index, step in enumerate(steps[:4], 1))
        target = sites[0] if len(sites) == 1 else "your site"
        return f"Here is what to check:\n\n{listed}\n\nStill not working? I can prepare a support request for {target}."
    if name.startswith("prepare_"):
        return PREPARED
    if name == "get_report_status":
        if data["state"] == "scheduled":
            next_run = (data.get("result") or {}).get("next_run")
            first = f" The first report goes out on {_day(next_run)}." if next_run else ""
            return "Your schedule is confirmed." + first
        return REPORT_STATES[data["state"]]
    return "Here is the latest status."


def site_lookup_reply(output: dict, ctx: Context) -> dict:
    """Reply to /sites <name>: one site, a question when several or a near match fit, or not found."""
    data = output["data"]
    names = [site["name"] for site in data["sites"]]
    if data["match"] == "none":
        alternatives = [site["name"] for site in data.get("alternatives", [])]
        text = "I couldn't find that site among the sites you can access."
        if alternatives:
            text += f" You can access {_names(alternatives)}."
        reply = Reply(status="not_in_data", text=text, source_ids=[output["source_id"]])
        return envelope(guard(reply.model_dump_json(), ctx), ctx)
    if data["match"] == "several" or (data["match"] == "one" and not data["exact"]):
        question = (
            f"Do you mean {names[0]}?"
            if len(names) == 1
            else f"Several of your sites match: {_names(names)}. Which one do you mean?"
        )
        reply = Reply(status="clarification", text=question, source_ids=[output["source_id"]], asks_for="site")
        choices = [Choice(name, f"/sites {name}") for name in names]
        return envelope(guard(reply.model_dump_json(), ctx), ctx, choices=choices)
    reply = Reply(status="answer", text=demo_text("get_permitted_sites", data, ctx), source_ids=[output["source_id"]])
    return envelope(guard(reply.model_dump_json(), ctx), ctx)


def previous_domains(ctx: Context) -> list[str]:
    history = ctx.store.history(ctx.session_id)
    return list(history[-1]["reply"].get("domains", [])) if history else []


def route(message: str, ctx: Context) -> None:
    routing = load_agent().route(message, previous_domains(ctx), ctx.features)
    ctx.domains, ctx.offered = routing.domains, routing.tools


COMMANDS = {
    "/sites": "get_permitted_sites",
    "/reports": "list_report_types",
    "/docs": "search_docs",
    "/report": "prepare_report",
    "/schedule": "prepare_scheduled_report",
    "/ticket": "prepare_support_ticket",
    "/status": "get_report_status",
}
JSON_COMMANDS = {"prepare_report", "prepare_scheduled_report", "prepare_support_ticket"}


def demo_run(message: str, ctx: Context) -> Iterator[tuple[str, dict]]:
    """Deterministic local harness, not an AI model. Slash commands exercise the real tool handlers."""
    route(message, ctx)
    command, _, rest = message.partition(" ")
    command = command.lower()
    if command == "/flows":
        yield "reply", menu_reply(ctx)
        return
    if command == "/flow":
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
    name: str | None = COMMANDS.get(command)
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
        if "reporting" in ctx.domains and not reporting.wants_report_list(message):
            if "reports" not in ctx.features:
                yield (
                    "reply",
                    envelope(Reply(status="out_of_scope", text=FEATURE_OFF["reports"], source_ids=[]), ctx, SYSTEM),
                )
                return
            plan = yield from reporting.plan(message, ctx)
            yield "reply", plan_reply(plan, ctx)
            return
        if "reporting" in ctx.domains:
            name, args = "list_report_types", {}
        elif "site" in normalized and any(
            word in normalized for word in ("which", "access", "available", "list", "my sites")
        ):
            name, args = "get_permitted_sites", {}
        elif any(word in normalized for word in ("alarm", "sensor", "offline")):
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
    elif name in JSON_COMMANDS:
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
    elif name == "get_permitted_sites":
        args = {} if rest.strip() in {"", "*"} else {"name": rest.strip()}
    else:
        args = {"query": rest.strip()}
    yield "step", {"tool": name, "state": "started"}
    output = ctx.execute(name, args)
    yield "step", {"tool": name, "state": "failed" if "error" in output else "done"}
    if "error" in output:
        yield "reply", failure_reply(output, ctx)
        return
    if name == "get_permitted_sites" and args:
        yield "reply", site_lookup_reply(output, ctx)
        return
    data = output["data"]
    if not data or (name == "get_permitted_sites" and not data["sites"]):
        text = EMPTY.get(name, "The permitted data does not answer that lookup. You can prepare a support ticket.")
        reply = Reply(status="not_in_data", text=text, source_ids=[])
    else:
        reply = Reply(status="answer", text=demo_text(name, data, ctx), source_ids=[output["source_id"]])
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
        route(message, ctx)
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
        # Only the domains this request needs: their instructions and their model tools, nothing else.
        instructions = load_agent().instructions(ctx.domains)
        tools = tool_definitions(ctx.offered)
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
                        output = ctx.execute(call.name, args, caller="model", allowed=ctx.offered)
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
                            "and never say a draft was created, scheduled or sent unless get_report_status "
                            "confirmed it. Otherwise return not_in_data.",
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
