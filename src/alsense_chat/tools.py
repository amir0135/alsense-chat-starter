import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import ROOT
from .source import AlsenseSource, User
from .store import MissingRecord, Store


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class SitesQuery(Arguments):
    query: str = Field(
        min_length=1, max_length=1000,
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
            raise ValueError("Start must be on or before end.")
        if (self.end - self.start).days > 366:
            raise ValueError("Use a report window of at most 366 days.")
        return self


class Ticket(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    alarm_id: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    steps_tried: str = Field(min_length=1, max_length=2000)


class Status(Arguments):
    action_id: str = Field(min_length=1, max_length=100)


SCHEMAS = {
    "search_docs": Query, "list_report_types": Arguments, "resolve_sites": SitesQuery,
    "prepare_report": Report, "get_report_status": Status, "prepare_support_ticket": Ticket,
}


def model_definition() -> dict:
    definition = yaml.safe_load((ROOT / "alsense.semantic.yaml").read_text())
    if set(definition["tools"]) != set(SCHEMAS):
        raise ValueError("Semantic tools and executable handlers must match.")
    for name, schema in SCHEMAS.items():
        declared = definition["tools"][name]["parameters"]
        generated = schema.model_json_schema()
        if (set(declared["properties"]) != set(generated["properties"])
                or set(declared["required"]) != set(generated.get("required", []))
                or declared.get("additionalProperties") is not False):
            raise ValueError(f"Semantic schema disagrees with handler: {name}")
    return definition


def safe_link(value: str) -> bool:
    parts = urlsplit(value)
    # Mock links are deliberately same-origin. Configure approved origins in a real adapter.
    return (value.startswith("/reference/") and not value.startswith("//")
            and not parts.scheme and not parts.netloc and "\\" not in value
            and all(ord(char) >= 32 for char in value))


@dataclass
class Context:
    source: AlsenseSource
    store: Store
    session_id: str
    user: User
    evidence: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    def check_site(self, site_id: str) -> None:
        if site_id not in {s.id for s in self.user.sites if s.tenant_id == self.user.tenant_id}:
            raise ValueError("Site unavailable. Choose from resolve_sites.")

    def validate_action(self, kind: str, payload: dict) -> None:
        self.check_site(payload["site_id"])
        if kind == "report":
            report = Report.model_validate(payload)
            types = {item.id: item for item in self.source.report_types(self.user)}
            if report.report_type not in types or report.format not in types[report.report_type].formats:
                raise ValueError("Report type or format unavailable. Use list_report_types.")
        elif kind == "ticket":
            ticket = Ticket.model_validate(payload)
            if not self.source.alarm_exists(self.user, ticket.site_id, ticket.alarm_id):
                raise ValueError("Alarm unavailable for this site.")
        else:
            raise ValueError("Unknown action kind.")

    def execute(self, name: str, args: Any) -> dict:
        if name not in SCHEMAS:
            return {"error": "Unknown tool."}
        try:
            parsed = SCHEMAS[name].model_validate(args).model_dump(mode="json")
            result = self.dispatch(name, parsed)
        except (ValidationError, ValueError, MissingRecord):
            # Do not echo invalid input or upstream records into the model.
            self.store.record_unanswered(self.session_id, f"tool_invalid:{name}")
            return {"error": "Invalid or unavailable tool inputs. Check the required fields and permitted sites."}
        source_id = f"tool-{len(self.evidence) + 1}"
        output = {"source_id": source_id, "tool": name, "data": result}
        self.evidence.append(output)
        return output

    def dispatch(self, name: str, args: dict) -> Any:
        if name == "resolve_sites":
            query = args["query"].casefold()
            return [site.model_dump() for site in self.user.sites
                    if site.tenant_id == self.user.tenant_id and
                    (query == "*" or query in site.name.casefold() or query == site.id)]
        if name == "list_report_types":
            return [item.model_dump() for item in self.source.report_types(self.user)]
        if name == "search_docs":
            docs = self.source.search_docs(self.user, args["query"])
            if any(not safe_link(doc.url) for doc in docs):
                raise ValueError("Knowledge source returned an unapproved link.")
            return [doc.model_dump() for doc in docs]
        if name == "get_report_status":
            action = self.store.action(self.session_id, args["action_id"])
            self.check_site(action["payload"]["site_id"])
            if action["kind"] != "report":
                raise ValueError("Not a report.")
            return {"state": action["state"], "result": action["result"]}
        kind = "report" if name == "prepare_report" else "ticket"
        self.validate_action(kind, args)
        action = self.store.prepare(self.session_id, kind, args)
        self.actions.append(action)
        if kind == "ticket":
            self.store.record_unanswered(self.session_id, "support_prepared")
        return action


def tool_definitions() -> list[dict]:
    model = model_definition()
    # Pydantic emits executable constraints, not hand-maintained copies of them.
    return [{"type": "function", "name": name, "description": model["tools"][name]["description"],
             "parameters": schema.model_json_schema()} for name, schema in SCHEMAS.items()]


def evidence_text(ctx: Context) -> str:
    return json.dumps(ctx.evidence, ensure_ascii=False)
