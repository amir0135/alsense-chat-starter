import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field, ValidationError

from .adapters.contracts import SupportRequest, User
from .adapters.errors import Forbidden, IntegrationError, ServiceUnavailable, StaleData
from .adapters.interfaces import Services
from .agent import Agent, Tool, load_agent
from .config import FEATURES
from .domains import reporting, sites
from .domains.base import Arguments, Handler, InvalidRequest, safe_link
from .store import MissingRecord, Store

__all__ = ["HANDLERS", "Context", "InvalidRequest", "safe_link", "tool_definitions"]

# Health status older than this is reported as stale rather than trusted.
HEALTH_MAX_AGE = timedelta(minutes=30)
Caller = Literal["model", "scripted", "flow", "user"]


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class Ticket(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    alarm_id: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    steps_tried: str = Field(min_length=1, max_length=2000)


class SiteRef(Arguments):
    site_id: str = Field(min_length=1, max_length=100)


FEATURE_OFF = {
    "reports": "Reports are turned off in this demo.",
    "support": "Support requests are turned off in this demo.",
    "troubleshooting": "Guided checks are turned off in this demo.",
}
FIELD_LABELS = {
    "site_id": "site",
    "report_type": "report type",
    "format": "format",
    "start": "start date",
    "end": "end date",
    "frequency": "frequency",
    "recipients": "recipient list",
    "start_date": "start date",
    "end_date": "end date",
    "alarm_id": "alarm",
    "summary": "summary",
    "steps_tried": "steps tried",
    "query": "question",
    "name": "site name",
    "action_id": "draft",
}
UNVERIFIED_SITE = (
    "Use a site id only from get_permitted_sites in this turn, for one exactly matching site. "
    "Ask the user to choose or confirm the site first. Never guess a site id."
)


def invalid_message(exc: ValidationError) -> str:
    """Explain schema failures with our own wording. Never echo input values or unexpected field names."""
    messages: list[str] = []
    for error in exc.errors():
        loc = error["loc"]
        name = FIELD_LABELS.get(str(loc[0]), "") if loc else ""
        kind = error["type"]
        if kind == "value_error":
            text = str(error["msg"]).removeprefix("Value error, ")
        elif kind == "extra_forbidden":
            text = "Unexpected fields were supplied."
        elif kind in {"missing", "string_too_short", "too_short"}:
            text = f"The {name} is required." if name else "A required field is missing."
        elif kind.startswith("date"):
            text = f"The {name} must be a date (YYYY-MM-DD)." if name else "A date is invalid."
        elif kind in {"string_too_long", "too_long"}:
            text = f"The {name} is too long." if name else "A field is too long."
        else:
            text = f"The {name} has an unsupported value." if name else "A field has an unsupported value."
        if text not in messages:
            messages.append(text)
    return " ".join(messages) or "Some details are invalid."


def search_docs(ctx: "Context", args: dict) -> list[dict]:
    docs = ctx.services.knowledge.search(ctx.user, args["query"])
    if any(not safe_link(doc.url) for doc in docs):
        raise ServiceUnavailable("knowledge", "The guidance search returned an unapproved link, so I withheld it.")
    return [doc.model_dump() for doc in docs]


def validate_ticket(ctx: "Context", payload: dict) -> None:
    ticket = Ticket.model_validate(payload)
    alarms = {alarm.id for alarm in ctx.services.sites.active_alarms(ctx.user, ticket.site_id)}
    if ticket.alarm_id and ticket.alarm_id not in alarms:
        raise InvalidRequest("That alarm isn't active at this site.")


def prepare_support_ticket(ctx: "Context", args: dict) -> dict:
    ctx.check_site(args["site_id"])
    validate_ticket(ctx, args)
    action = ctx.store.prepare(ctx.session_id, "ticket", args)
    ctx.actions.append(action)
    ctx.store.record_unanswered(ctx.session_id, "support_prepared")
    return action


def check_site_health(ctx: "Context", args: dict) -> dict:
    ctx.check_site(args["site_id"])
    report = ctx.services.health.site_health(ctx.user, args["site_id"])
    if report.site_id != args["site_id"]:
        raise ServiceUnavailable("health", "The health-check service answered for a different site.")
    observed = report.observed_at if report.observed_at.tzinfo else report.observed_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - observed > HEALTH_MAX_AGE:
        raise StaleData("health")
    return {"observed_at": observed.isoformat(), "checks": [check.model_dump() for check in report.checks]}


def ticket_audit(args: dict) -> dict:
    inputs = {"site_id": args["site_id"], "summary_chars": len(args["summary"])}
    inputs["steps_tried_chars"] = len(args["steps_tried"])
    if args.get("alarm_id"):
        inputs["alarm_id"] = args["alarm_id"]
    return inputs


def health_summary(result: dict) -> str:
    found = sorted({check["type"] for check in result["checks"] if check["status"] == "open"})
    return "open_checks:" + (",".join(found) or "none")


HANDLERS: dict[str, Handler] = {
    **sites.HANDLERS,
    **reporting.HANDLERS,
    "search_docs": Handler(
        schema=Query,
        run=search_docs,
        summarize=lambda result: f"documents:{len(result)}",
        label="Approved guidance",
        audit=lambda args: {"query_chars": len(args["query"])},
    ),
    "prepare_support_ticket": Handler(
        schema=Ticket,
        run=prepare_support_ticket,
        summarize=lambda result: "draft_prepared",
        label="Support request draft",
        audit=ticket_audit,
    ),
    "check_site_health": Handler(
        schema=SiteRef,
        run=check_site_health,
        summarize=health_summary,
        label="Site health checks",
        audit=lambda args: {"site_id": args["site_id"]},
    ),
}
# Tools whose caller is "user" are not handlers: they are endpoints that run only after an explicit user action.
USER_ACTIONS = {"confirm_report": "POST /api/actions/{id}/submit"}


def check_registry(agent: Agent | None = None) -> Agent:
    """Every declared tool has a handler whose schema matches the declaration, and nothing is undeclared."""
    agent = agent or load_agent()
    declared = agent.tools()
    for name, tool in declared.items():
        if tool.caller == "user":
            if name not in USER_ACTIONS:
                raise ValueError(f"User action {name} has no endpoint.")
            continue
        handler = HANDLERS.get(name)
        if handler is None:
            raise ValueError(f"Declared tool {name} has no handler.")
        generated = handler.schema.model_json_schema()
        parameters = tool.parameters or {}
        if set(parameters["properties"]) != set(generated.get("properties", {})) or set(parameters["required"]) != set(
            generated.get("required", [])
        ):
            raise ValueError(f"Declared parameters disagree with the handler schema: {name}")
    undeclared = set(HANDLERS) - set(declared)
    if undeclared:
        raise ValueError(f"Handlers without a declared tool: {sorted(undeclared)}")
    return agent


def tool_definitions(names: Iterable[str]) -> list[dict]:
    """Function definitions for the routed tools only.

    Pydantic emits the executable constraints; the domain's tools.yaml supplies the wording the model reads.
    """
    tools = load_agent().tools("model")
    definitions = []
    for name in names:
        parameters = HANDLERS[name].schema.model_json_schema()
        declared = (tools[name].parameters or {}).get("properties", {})
        for key, spec in declared.items():
            if spec.get("description"):
                parameters["properties"][key]["description"] = spec["description"]
        definitions.append(
            {"type": "function", "name": name, "description": tools[name].description, "parameters": parameters}
        )
    return definitions


@dataclass
class Context:
    services: Services
    store: Store
    session_id: str
    user: User
    flows: dict = field(default_factory=dict)
    features: frozenset[str] = FEATURES
    faults: frozenset[str] = frozenset()
    session_faults: frozenset[str] = frozenset()
    correlation_id: str = ""
    scenario: str = ""
    assistant: str = "scripted"
    domains: tuple[str, ...] = ()
    offered: tuple[str, ...] = ()
    evidence: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    verified_sites: set[str] = field(default_factory=set)

    def permitted_site_ids(self) -> set[str]:
        return {site.id for site in self.user.sites if site.tenant_id == self.user.tenant_id}

    def check_site(self, site_id: str) -> None:
        if site_id not in self.permitted_site_ids():
            raise Forbidden("sites", "That site isn't one of your permitted sites.")

    def validate_action(self, kind: str, payload: dict) -> None:
        if kind not in {*reporting.KINDS, "ticket"}:
            raise InvalidRequest("Unknown action kind.")
        self.check_site(payload["site_id"])
        if kind == "ticket":
            validate_ticket(self, payload)
        else:
            reporting.validate(self, kind, payload)

    def submit_action(self, action: dict) -> dict:
        """Re-check permissions, then call the configured service. Only the confirmation endpoint calls this."""
        self.validate_action(action["kind"], action["payload"])
        if action["kind"] in reporting.KINDS:
            return reporting.submit(self, action)
        ticket = SupportRequest.model_validate(action["payload"])
        return self.services.support.submit_request(self.user, ticket, action["id"]).model_dump(mode="json")

    @staticmethod
    def may_call(tool: Tool, caller: Caller, allowed: Iterable[str] | None) -> bool:
        """The model gets only the tools routed to this request; backend tools serve guided checks only."""
        if tool.caller == "user":
            return False
        if tool.caller == "backend":
            return caller == "flow"
        if caller == "model":
            return allowed is None or tool.name in set(allowed)
        return True

    def execute(
        self, name: str, args: Any, *, caller: Caller = "scripted", allowed: Iterable[str] | None = None
    ) -> dict:
        tool = load_agent().tools().get(name)
        if tool is None or not self.may_call(tool, caller, allowed):
            return self.failed(name, "unknown_tool", "Unknown tool.")
        if tool.feature and tool.feature not in self.features:
            return self.failed(name, "disabled", FEATURE_OFF[tool.feature])
        handler = HANDLERS[name]
        try:
            parsed = handler.schema.model_validate(args).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            return self.failed(name, "invalid_input", invalid_message(exc))
        site_id = parsed.get("site_id")
        if caller == "model" and site_id is not None and site_id not in self.verified_sites:
            return self.failed(name, "unverified_site", UNVERIFIED_SITE, parsed)
        try:
            result = handler.run(self, parsed)
        except ValidationError as exc:
            return self.failed(name, "invalid_input", invalid_message(exc), parsed)
        except IntegrationError as exc:
            return self.failed(name, exc.category, exc.user_message, parsed, exc.service)
        except MissingRecord:
            return self.failed(name, "not_found", "That draft isn't available in this conversation.", parsed)
        except InvalidRequest as exc:
            return self.failed(name, "invalid_input", str(exc), parsed)
        except ValueError:
            return self.failed(name, "invalid_input", "Some details are invalid or unavailable.", parsed)
        source_id = f"tool-{len(self.evidence) + 1}"
        output = {"source_id": source_id, "tool": name, "data": result}
        self.evidence.append(output)
        self.calls.append({"tool": name, "result": handler.summarize(result), "inputs": handler.audit(parsed)})
        return output

    def failed(self, name: str, category: str, message: str, parsed: dict | None = None, service: str = "") -> dict:
        # Messages are our own wording: never echo invalid input or upstream records into the model.
        self.store.record_unanswered(self.session_id, f"tool_{category}:{name}")
        handler = HANDLERS.get(name)
        inputs = handler.audit(parsed) if handler and parsed else {}
        self.calls.append({"tool": name, "result": f"error:{category}", "inputs": inputs})
        output = {"error": message, "category": category}
        if service:
            output["service"] = service
        return output


ID_KEYS = {"id", "site_id", "action_id", "alarm_id", "report_type"}


def internal_identifiers(ctx: Context) -> set[str]:
    """Values that are fine in tool arguments but must never appear in user-facing text."""
    values = {ctx.user.tenant_id, *load_agent().tools()}
    for site in ctx.user.sites:
        values.update({site.id, site.tenant_id})

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_KEYS and isinstance(value, str):
                    values.add(value)
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    for item in ctx.evidence:
        values.add(item["source_id"])
        collect(item["data"])
    values.update(action["id"] for action in ctx.actions)
    return {value for value in values if len(value) >= 3}


def evidence_text(ctx: Context) -> str:
    return json.dumps(ctx.evidence, ensure_ascii=False)
