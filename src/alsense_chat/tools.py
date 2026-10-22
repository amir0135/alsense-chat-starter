import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .adapters.contracts import ReportRequest, SupportRequest, User
from .adapters.errors import Forbidden, IntegrationError, ServiceUnavailable, StaleData
from .adapters.interfaces import Services
from .config import FEATURES, ROOT
from .store import MissingRecord, Store

# Health status older than this is reported as stale rather than trusted.
HEALTH_MAX_AGE = timedelta(minutes=30)


class InvalidRequest(ValueError):
    """A request the backend refuses, with a user-safe explanation that never echoes the input."""


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class SitesQuery(Arguments):
    query: str = Field(
        min_length=1,
        max_length=1000,
        description='Use "*" to list every permitted site; otherwise a site name substring or exact site id.',
    )


class Report(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    report_type: str = Field(min_length=1, max_length=100)
    format: str = Field(pattern="^(pdf|csv)$")
    start: date
    end: date

    @model_validator(mode="after")
    def window(self):
        if self.start > self.end:
            raise ValueError("The start date must be on or before the end date.")
        if (self.end - self.start).days > 366:
            raise ValueError("The report period is too long. Choose a period of one year or less.")
        return self


class Ticket(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    alarm_id: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    steps_tried: str = Field(min_length=1, max_length=2000)


class Status(Arguments):
    action_id: str = Field(min_length=1, max_length=100)


class SiteRef(Arguments):
    site_id: str = Field(min_length=1, max_length=100)


SCHEMAS: dict[str, type[Arguments]] = {
    "search_docs": Query,
    "list_report_types": Arguments,
    "resolve_sites": SitesQuery,
    "prepare_report": Report,
    "get_report_status": Status,
    "prepare_support_ticket": Ticket,
}
# Backend-only lookups used by guided troubleshooting flows; never offered to the model.
FLOW_TOOLS: dict[str, type[Arguments]] = {"check_site_health": SiteRef}
TOOL_FEATURES = {
    "list_report_types": "reports",
    "prepare_report": "reports",
    "get_report_status": "reports",
    "prepare_support_ticket": "support",
    "check_site_health": "troubleshooting",
}
FEATURE_OFF = {
    "reports": "Reports are turned off in this demo.",
    "support": "Support requests are turned off in this demo.",
    "troubleshooting": "Guided checks are turned off in this demo.",
}
TOOL_LABELS = {
    "search_docs": "Approved guidance",
    "list_report_types": "Report catalogue",
    "resolve_sites": "Permitted sites",
    "prepare_report": "Report draft",
    "get_report_status": "Report status",
    "prepare_support_ticket": "Support request draft",
    "check_site_health": "Site health checks",
}
FIELD_LABELS = {
    "site_id": "site",
    "report_type": "report type",
    "format": "format",
    "start": "start date",
    "end": "end date",
    "alarm_id": "alarm",
    "summary": "summary",
    "steps_tried": "steps tried",
    "query": "question",
    "action_id": "draft",
}


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
        elif kind in {"missing", "string_too_short"}:
            text = f"The {name} is required." if name else "A required field is missing."
        elif kind.startswith("date"):
            text = f"The {name} must be a date (YYYY-MM-DD)." if name else "A date is invalid."
        elif kind == "string_too_long":
            text = f"The {name} is too long." if name else "A field is too long."
        else:
            text = f"The {name} has an unsupported value." if name else "A field has an unsupported value."
        if text not in messages:
            messages.append(text)
    return " ".join(messages) or "Some details are invalid."


def model_definition() -> dict:
    definition = yaml.safe_load((ROOT / "alsense.semantic.yaml").read_text())
    if set(definition["tools"]) != set(SCHEMAS):
        raise ValueError("Semantic tools and executable handlers must match.")
    for name, schema in SCHEMAS.items():
        declared = definition["tools"][name]["parameters"]
        generated = schema.model_json_schema()
        if (
            set(declared["properties"]) != set(generated["properties"])
            or set(declared["required"]) != set(generated.get("required", []))
            or declared.get("additionalProperties") is not False
        ):
            raise ValueError(f"Semantic schema disagrees with handler: {name}")
    return definition


def safe_link(value: str) -> bool:
    parts = urlsplit(value)
    # Mock links are deliberately same-origin. Configure approved origins in a real adapter.
    return (
        value.startswith("/reference/")
        and not value.startswith("//")
        and not parts.scheme
        and not parts.netloc
        and "\\" not in value
        and all(ord(char) >= 32 for char in value)
    )


def summarize(name: str, result: Any) -> str:
    """Category of a successful synthetic response, for the audit trail."""
    if name == "check_site_health":
        found = sorted({check["type"] for check in result["checks"] if check["status"] == "open"})
        return "open_checks:" + (",".join(found) or "none")
    if name == "get_report_status":
        return f"state:{result['state']}"
    if name in {"prepare_report", "prepare_support_ticket"}:
        return "draft_prepared"
    noun = {"resolve_sites": "sites", "search_docs": "documents", "list_report_types": "report_types"}.get(
        name, "items"
    )
    return f"{noun}:{len(result)}"


def audit_inputs(name: str, args: dict) -> dict:
    """Validated inputs worth auditing. Free text is reduced to its length."""
    if not args:
        return {}
    if name == "resolve_sites":
        return {"query": "*" if args["query"] == "*" else "text"}
    if name == "search_docs":
        return {"query_chars": len(args["query"])}
    if name == "prepare_report":
        return {key: args[key] for key in ("site_id", "report_type", "format", "start", "end")}
    if name == "prepare_support_ticket":
        inputs = {"site_id": args["site_id"], "summary_chars": len(args["summary"])}
        inputs["steps_tried_chars"] = len(args["steps_tried"])
        if args.get("alarm_id"):
            inputs["alarm_id"] = args["alarm_id"]
        return inputs
    if name == "check_site_health":
        return {"site_id": args["site_id"]}
    return {}


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
    evidence: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def permitted_sites(self) -> set[str]:
        return {site.id for site in self.user.sites if site.tenant_id == self.user.tenant_id}

    def check_site(self, site_id: str) -> None:
        if site_id not in self.permitted_sites():
            raise Forbidden("sites", "That site isn't one of your permitted sites.")

    def validate_action(self, kind: str, payload: dict) -> None:
        if kind not in {"report", "ticket"}:
            raise InvalidRequest("Unknown action kind.")
        self.check_site(payload["site_id"])
        if kind == "report":
            report = Report.model_validate(payload)
            types = {item.id: item for item in self.services.reports.report_types(self.user)}
            if report.report_type not in types or report.format not in types[report.report_type].formats:
                raise InvalidRequest("That report type or format isn't available. Choose one from the report list.")
        else:
            ticket = Ticket.model_validate(payload)
            alarms = {alarm.id for alarm in self.services.sites.active_alarms(self.user, ticket.site_id)}
            if ticket.alarm_id and ticket.alarm_id not in alarms:
                raise InvalidRequest("That alarm isn't active at this site.")

    def submit_action(self, action: dict) -> dict:
        """Re-check permissions, then call the configured service. Only the confirmation endpoint calls this."""
        self.validate_action(action["kind"], action["payload"])
        if action["kind"] == "report":
            request = ReportRequest.model_validate(action["payload"])
            submission = self.services.reports.submit_report(self.user, request, action["id"])
        else:
            ticket = SupportRequest.model_validate(action["payload"])
            submission = self.services.support.submit_request(self.user, ticket, action["id"])
        return submission.model_dump(mode="json")

    def execute(self, name: str, args: Any, *, flow: bool = False) -> dict:
        schemas = {**SCHEMAS, **FLOW_TOOLS} if flow else SCHEMAS
        if name not in schemas:
            return self.failed(name, "unknown_tool", "Unknown tool.")
        feature = TOOL_FEATURES.get(name)
        if feature and feature not in self.features:
            return self.failed(name, "disabled", FEATURE_OFF[feature])
        try:
            parsed = schemas[name].model_validate(args).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            return self.failed(name, "invalid_input", invalid_message(exc))
        try:
            result = self.dispatch(name, parsed)
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
        self.calls.append({"tool": name, "result": summarize(name, result), "inputs": audit_inputs(name, parsed)})
        return output

    def failed(self, name: str, category: str, message: str, parsed: dict | None = None, service: str = "") -> dict:
        # Messages are our own wording: never echo invalid input or upstream records into the model.
        self.store.record_unanswered(self.session_id, f"tool_{category}:{name}")
        self.calls.append({"tool": name, "result": f"error:{category}", "inputs": audit_inputs(name, parsed or {})})
        output = {"error": message, "category": category}
        if service:
            output["service"] = service
        return output

    def dispatch(self, name: str, args: dict) -> Any:
        if name == "resolve_sites":
            # Tenant ids stay server-side; alarms let the model prepare a ticket without asking for ids.
            allowed = self.permitted_sites()
            sites = [site for site in self.services.sites.search_sites(self.user, args["query"]) if site.id in allowed]
            return [
                {
                    "id": site.id,
                    "name": site.name,
                    "alarms": [alarm.model_dump() for alarm in self.services.sites.active_alarms(self.user, site.id)],
                }
                for site in sites
            ]
        if name == "list_report_types":
            return [item.model_dump() for item in self.services.reports.report_types(self.user)]
        if name == "check_site_health":
            self.check_site(args["site_id"])
            report = self.services.health.site_health(self.user, args["site_id"])
            if report.site_id != args["site_id"]:
                raise ServiceUnavailable("health", "The health-check service answered for a different site.")
            observed = report.observed_at if report.observed_at.tzinfo else report.observed_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - observed > HEALTH_MAX_AGE:
                raise StaleData("health")
            return {"observed_at": observed.isoformat(), "checks": [check.model_dump() for check in report.checks]}
        if name == "search_docs":
            docs = self.services.knowledge.search(self.user, args["query"])
            if any(not safe_link(doc.url) for doc in docs):
                raise ServiceUnavailable(
                    "knowledge", "The guidance search returned an unapproved link, so I withheld it."
                )
            return [doc.model_dump() for doc in docs]
        if name == "get_report_status":
            action = self.store.action(self.session_id, args["action_id"])
            self.check_site(action["payload"]["site_id"])
            if action["kind"] != "report":
                raise InvalidRequest("That draft is not a report.")
            if action["result"] is None:
                return {"state": "prepared", "result": None}
            job = self.services.reports.report_status(self.user, action["result"]["id"])
            return {"state": job.state, "result": job.model_dump(mode="json")}
        kind = "report" if name == "prepare_report" else "ticket"
        self.validate_action(kind, args)
        action = self.store.prepare(self.session_id, kind, args)
        self.actions.append(action)
        if kind == "ticket":
            self.store.record_unanswered(self.session_id, "support_prepared")
        return action


def tool_definitions(features: frozenset[str] = FEATURES) -> list[dict]:
    model = model_definition()
    # Pydantic emits executable constraints, not hand-maintained copies of them.
    return [
        {
            "type": "function",
            "name": name,
            "description": model["tools"][name]["description"],
            "parameters": schema.model_json_schema(),
        }
        for name, schema in SCHEMAS.items()
        if TOOL_FEATURES.get(name, "") in {"", *features}
    ]


def internal_identifiers(ctx: Context) -> set[str]:
    """Values that are fine in tool arguments but must never appear in user-facing text."""
    values = {ctx.user.tenant_id, *SCHEMAS, *FLOW_TOOLS}
    for site in ctx.user.sites:
        values.update({site.id, site.tenant_id})
    for item in ctx.evidence:
        values.add(item["source_id"])
        data = item["data"] if isinstance(item["data"], list) else [item["data"]]
        for record in data:
            if not isinstance(record, dict):
                continue
            if item["tool"] != "get_report_status":
                values.add(str(record.get("id", "")))
            if isinstance(record.get("result"), dict):
                values.add(str(record["result"].get("id", "")))
            for alarm in record.get("alarms", []):
                values.add(alarm["id"])
    values.update(action["id"] for action in ctx.actions)
    return {value for value in values if len(value) >= 3}


def evidence_text(ctx: Context) -> str:
    return json.dumps(ctx.evidence, ensure_ascii=False)
