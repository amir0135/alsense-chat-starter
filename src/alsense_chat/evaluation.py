"""Evaluation harness: behaviour cases for the assistant, in scripted or model mode.

    uv run python scripts/dev.py evals                          # every synthetic case, scripted assistant
    uv run python scripts/dev.py evals --mode model evals/local # your own cases against your Foundry model

A case states the question, the user and their sites, and what must happen: the tools, the prepared
parameters, any clarification, the result, whether the user must confirm, and what must never happen. Each
case runs in a fresh app on synthetic services; tool calls are read from the audit trail, so a passing case
also proves the interaction was audited. The format is documented in evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .adapters import synthetic
from .adapters.contracts import Site, User
from .adapters.interfaces import Services
from .adapters.synthetic.faults import FAULTS
from .adapters.synthetic.services import PERSONAS, SyntheticIdentity
from .agent import load_agent
from .app import create_app
from .config import FEATURES, ROOT, Settings, load_settings
from .domains.base import safe_link
from .runner import claim
from .store import Store
from .tools import HANDLERS

REPOSITORY = ROOT.parents[1]
SUITES = REPOSITORY / "evals" / "synthetic"
Mode = Literal["scripted", "model"]
MODES: tuple[Mode, ...] = ("scripted", "model")
INVARIANTS = {
    "invent_report_type": "Every report type came from list_report_types",
    "submit_before_confirmation": "Nothing is submitted without the user's confirm step",
    "use_unauthorized_site": "No site outside the user's permitted sites is used or shown",
    "guess_site_id": "Every site id came from get_permitted_sites in the same turn",
    "claim_without_confirmation": "Nothing is called created, scheduled or sent unless the service confirmed it",
    "offer_unrelated_tools": "Each request is offered only the tools of the domains it needs",
    "submit": "Nothing is submitted at all, even after a confirm step",
}
PREPARE_TOOLS = ("prepare_report", "prepare_scheduled_report")
DATE_FIELDS = ("start", "end", "start_date", "end_date")
RELATIVE_DAY = re.compile(r"^(today|tomorrow|yesterday)\s*(?:([+-])\s*(\d+))?$")
NAME = r"^[a-z0-9][a-z0-9_-]*$"


class CaseError(ValueError):
    """A case file that can't be loaded or run as written."""


class _Strict(BaseModel):
    # Typos in a case must fail loudly instead of silently skipping a check.
    model_config = ConfigDict(extra="forbid")


class Step(_Strict):
    say: str | None = None
    flow: str | None = None
    answers: list[str] = []
    choose: str | None = None
    confirm: Literal["last"] | None = None
    status: Literal["last"] | None = None

    @model_validator(mode="after")
    def one_action(self) -> Step:
        if sum(value is not None for value in (self.say, self.flow, self.choose, self.confirm, self.status)) != 1:
            raise ValueError("each step needs exactly one of say, flow, choose, confirm or status")
        if self.answers and self.flow is None:
            raise ValueError("answers belong to a flow step")
        return self

    @property
    def kind(self) -> str:
        return next(name for name in ("say", "flow", "choose", "confirm", "status") if getattr(self, name) is not None)


class SiteSpec(_Strict):
    name: str = Field(min_length=1)
    id: str | None = None


class CaseContext(_Strict):
    user: str = "alice"
    sites: list[str | SiteSpec] | None = None
    inaccessible_sites: list[str] = []
    faults: list[str] = []
    features: list[str] | None = None

    @field_validator("user")
    @classmethod
    def known_user(cls, value: str) -> str:
        if value not in PERSONAS:
            raise ValueError(f"user must be a synthetic persona: {', '.join(PERSONAS)}")
        return value

    @field_validator("faults")
    @classmethod
    def known_faults(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(FAULTS))
        if unknown:
            raise ValueError(f"unknown faults {unknown}; choose from {', '.join(FAULTS)}")
        return value

    @field_validator("features")
    @classmethod
    def known_features(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not set(value) <= FEATURES:
            raise ValueError(f"features must be among {', '.join(sorted(FEATURES))}")
        return value

    def site_specs(self) -> list[SiteSpec] | None:
        if self.sites is None:
            return None
        specs = [SiteSpec(name=item) if isinstance(item, str) else item for item in self.sites]
        return [
            SiteSpec(
                name=spec.name, id=spec.id or "site-" + re.sub(r"[^a-z0-9]+", "-", spec.name.casefold()).strip("-")
            )
            for spec in specs
        ]


class Clarification(_Strict):
    asks_for: str | None = None
    options: list[str] | None = None
    text_includes: list[str] = []


class FlowExpectation(_Strict):
    outcome: Literal["resolve", "ticket"] | None = None
    resolved_site: str | None = None
    step: str | None = None


class DownloadLink(_Strict):
    expires_after_hours: int | None = None


class Expected(_Strict):
    # The questions every case answers.
    tools: list[str] | None = None
    allowed_tools: list[str] | None = None
    parameters: dict[str, Any] | None = None
    clarification_required: bool | None = None
    clarification: Clarification | None = None
    confirmation_required: bool | None = None
    result: str | list[str] | None = None
    must_not: list[str] = []
    # Routing: which domains and tools the request was offered.
    domains: list[str] | None = None
    offered_tools: list[str] | None = None
    # Details.
    download_link: DownloadLink | None = None
    status: str | None = None
    error_category: str | None = None
    drafts: int | None = None
    evidence: list[str] = []
    text_includes: list[str] = []
    must_not_mention: list[str] = []
    submission: str | None = None
    flow: FlowExpectation | None = None

    @model_validator(mode="after")
    def known_names(self) -> Expected:
        agent = load_agent()
        tools = set(agent.tools())
        for label, names in (
            ("tools", self.tools),
            ("allowed_tools", self.allowed_tools),
            ("offered_tools", self.offered_tools),
        ):
            unknown = sorted(set(names or []) - tools)
            if unknown:
                raise ValueError(f"{label} names unknown tools {unknown}")
        unknown = sorted(set(self.must_not) - tools - set(INVARIANTS))
        if unknown:
            raise ValueError(f"must_not entries {unknown} are neither tools nor invariants ({', '.join(INVARIANTS)})")
        domains = {module.name for module in agent.active()}
        if self.domains is not None and not set(self.domains) <= domains:
            raise ValueError(f"domains must be among {', '.join(sorted(domains))}")
        if "offer_unrelated_tools" in self.must_not and not (self.tools or self.domains):
            raise ValueError("offer_unrelated_tools needs expected tools or domains to know what is related")
        return self


class Case(_Strict):
    name: str = Field(pattern=NAME)
    description: str = ""
    question: str | None = None
    steps: list[Step] = []
    context: CaseContext = CaseContext()
    modes: list[Mode] = list(MODES)
    expected: Expected

    @model_validator(mode="after")
    def has_steps(self) -> Case:
        if not self.question and not self.steps:
            raise ValueError("a case needs a question, steps or both")
        return self

    def all_steps(self) -> list[Step]:
        return ([Step(say=" ".join(self.question.split()))] if self.question else []) + self.steps


@dataclass
class LoadedCase:
    suite: str
    case: Case

    @property
    def id(self) -> str:
        return f"{self.suite}:{self.case.name}"


def case_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        elif path.is_file():
            files.append(path)
        else:
            raise CaseError(f"No case file or folder at {path}")
    return files


def load_cases(paths: Iterable[Path] = (SUITES,)) -> list[LoadedCase]:
    loaded: list[LoadedCase] = []
    for path in case_files(paths):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise CaseError(f"{path}: not valid YAML: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise CaseError(f"{path}: expected a top-level 'cases:' list")
        for index, raw in enumerate(data["cases"], 1):
            label = raw.get("name", f"case {index}") if isinstance(raw, dict) else f"case {index}"
            try:
                loaded.append(LoadedCase(path.stem, Case.model_validate(raw)))
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc']) or 'case'}: {error['msg']}"
                    for error in exc.errors()
                )
                raise CaseError(f"{path}: {label}: {problems}") from None
    names = [item.case.name for item in loaded]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise CaseError(f"Case names must be unique across suites: {duplicates}")
    return loaded


@dataclass
class Run:
    """Everything a case produced: replies, drafts, confirmations and the audit trail."""

    user: User
    catalog: set[str]
    foreign_sites: list[tuple[str | None, str]]
    messages: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    submissions: list[str] = field(default_factory=list)
    confirm_steps: int = 0
    last_step: str = ""
    trail: list[dict] = field(default_factory=list)
    aborted: str | None = None

    @property
    def reply(self) -> dict:
        return self.replies[-1] if self.replies else {}

    @property
    def turns(self) -> list[dict]:
        return [record for record in self.trail if record["event"] in {"chat", "flow"}]

    @property
    def calls(self) -> list[dict]:
        return [call for record in self.turns for call in record["tools"]]

    @property
    def tools(self) -> list[str]:
        return [call["tool"] for call in self.calls]

    def inputs(self, *tools: str) -> list[dict]:
        return [entry for record in self.turns for entry in record["inputs"] if entry["tool"] in tools]

    @property
    def submit_attempts(self) -> list[dict]:
        return [record for record in self.trail if record["event"] == "submit"]

    @property
    def submitted(self) -> list[dict]:
        return [record for record in self.trail if record["event"] == "submit" and not record["error_category"]]

    @property
    def error_category(self) -> str | None:
        failures = [call["result"].removeprefix("error:") for call in self.calls if call["result"].startswith("error:")]
        return (self.reply.get("error") or {}).get("category") or (failures[-1] if failures else None)

    @property
    def result(self) -> str:
        """The case outcome in one word: what the user ended up with."""
        if self.aborted:
            return "aborted"
        if self.last_step == "confirm":
            return "not_submitted" if self.submissions[-1] == "not_submitted" else f"submitted:{self.submissions[-1]}"
        reply = self.reply
        flow = reply.get("flow") or {}
        if flow.get("kind") == "outcome":
            return f"flow:{flow['category']}"
        if reply.get("error"):
            return f"error:{reply['error']['category']}"
        statuses = [source for source in reply.get("sources", []) if source["tool"] == "get_report_status"]
        if statuses:
            state = statuses[-1]["data"]["state"]
            return "report_not_confirmed" if state == "prepared" else f"report_{state}"
        if reply.get("actions"):
            return "draft_prepared"
        return {"not_in_data": "not_found"}.get(reply.get("status", ""), reply.get("status", "none"))


def case_services(context: CaseContext) -> Services:
    """Synthetic services, with the case's own permitted sites for its user when it lists them."""
    services = synthetic.build()
    found = context.site_specs()
    if found is None:
        return services
    specs: list[SiteSpec] = found

    class CaseIdentity(SyntheticIdentity):
        def users_me(self, identity: str) -> User:
            user = super().users_me(identity)
            if identity != context.user:
                return user
            sites = [Site(id=spec.id or "", name=spec.name, tenant_id=user.tenant_id) for spec in specs]
            return user.model_copy(update={"sites": sites})

    return replace(services, identity=CaseIdentity())


def case_settings(case: Case, mode: Mode, db: str, base: Settings | None) -> Settings:
    settings = replace(
        base or Settings(),
        db=db,
        data_source="synthetic",
        synthetic_faults=frozenset(case.context.faults),
        features=frozenset(case.context.features if case.context.features is not None else FEATURES),
        demo_login=True,
        enabled=True,
        cloud_demo=False,
        telemetry_connection="",
        scenarios_file="",
        reset_on_start=False,
    )
    if mode == "scripted":
        return replace(settings, agent_mode="demo")
    if settings.agent_mode != "foundry":
        raise CaseError("Model mode needs AGENT_MODE=foundry, FOUNDRY_PROJECT_ENDPOINT and MODEL_DEPLOYMENT.")
    return settings


def chat(client: TestClient, headers: dict, message: str) -> dict:
    response = client.post("/api/chat", headers=headers, json={"message": message})
    if response.status_code != 200:
        category = f"http_{response.status_code}"
        return {"status": "error", "text": response.text, "error": {"category": category}, "sources": [], "actions": []}
    frames = [frame.split("\n", 1) for frame in response.text.strip().split("\n\n")]
    events = [(name.removeprefix("event: "), json.loads(data.removeprefix("data: "))) for name, data in frames]
    name, data = events[-1]
    if name != "reply":
        return {
            "status": "error",
            "text": data.get("message", ""),
            "error": {"category": "runtime_error"},
            "sources": [],
            "actions": [],
        }
    return data


def run_case(case: Case, mode: Mode = "scripted", base: Settings | None = None) -> Run:
    services = case_services(case.context)
    user = services.identity.users_me(case.context.user)
    permitted = {site.id for site in user.sites}
    foreign: list[tuple[str | None, str]] = [
        (site_id, name)
        for persona in PERSONAS.values()
        for site_id, name in persona["sites"]
        if site_id not in permitted and name not in {site.name for site in user.sites}
    ]
    foreign += [(None, name) for name in case.context.inaccessible_sites]
    run = Run(user=user, catalog={item.id for item in services.reports.report_types(user)}, foreign_sites=foreign)
    with tempfile.TemporaryDirectory() as folder:
        db = str(Path(folder) / "evaluation.sqlite3")
        with TestClient(create_app(case_settings(case, mode, db, base), services)) as client:
            login = client.post("/api/demo/session", json={"persona": case.context.user})
            if login.status_code != 200:
                raise CaseError(f"{case.name}: couldn't sign in as {case.context.user} ({login.status_code}).")
            headers = {"Authorization": "Bearer " + login.json()["token"]}
            for number, step in enumerate(case.all_steps(), 1):
                run.last_step = step.kind
                if step.kind in {"confirm", "status"} and not run.drafts:
                    run.aborted = f"step {number} ({step.kind}): no draft was prepared to {step.kind}"
                    break
                if step.kind == "confirm":
                    run.confirm_steps += 1
                    response = client.post(
                        f"/api/actions/{run.drafts[-1]['id']}/submit", headers=headers, json={"confirmed": True}
                    )
                    run.submissions.append(
                        response.json()["status"] if response.status_code == 200 else "not_submitted"
                    )
                    continue
                if step.kind == "choose":
                    choices = run.reply.get("choices") or []
                    chosen = next((c for c in choices if c["label"].casefold() == str(step.choose).casefold()), None)
                    if chosen is None:
                        offered = [choice["label"] for choice in choices]
                        run.aborted = f"step {number} (choose): '{step.choose}' wasn't offered; choices were {offered}"
                        break
                    message = chosen["message"]
                elif step.kind == "flow":
                    message = f"/flow {step.flow} {json.dumps(step.answers)}"
                elif step.kind == "status":
                    message = f"/status {run.drafts[-1]['id']}"
                else:
                    message = str(step.say)
                run.messages.append(message)
                reply = chat(client, headers, message)
                run.replies.append(reply)
                run.drafts.extend(reply.get("actions", []))
        run.trail = Store(db).audit_trail(limit=10_000)
    return run


def _day(value: Any, today: date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    match = RELATIVE_DAY.match(str(value).strip().casefold())
    if not match:
        return str(value)
    day = {"today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1)}[match[1]]
    if match[2]:
        day += timedelta(days=int(match[3]) * (1 if match[2] == "+" else -1))
    return day.isoformat()


def parameter_failures(expected: dict[str, Any], payload: dict, run: Run, today: date) -> list[str]:
    failures = []
    names = {site.id: site.name for site in run.user.sites}
    for key, want in expected.items():
        if key == "site":
            got = names.get(payload.get("site_id", ""), payload.get("site_id"))
            if str(got).casefold() != str(want).casefold():
                failures.append(f"parameters.site: expected {want}, the draft is for {got}")
            continue
        if key == "period_days":
            try:
                got = (date.fromisoformat(payload["end"]) - date.fromisoformat(payload["start"])).days + 1
            except (KeyError, ValueError):
                got = None
        elif key in DATE_FIELDS:
            want = _day(want, today)
            got = payload.get(key)
        elif key == "recipients" and isinstance(want, int):
            got = len(payload.get("recipients", []))
        elif key == "recipients":
            want, got = sorted(want), sorted(payload.get("recipients", []))
        else:
            got = payload.get(key)
        if got != want:
            failures.append(f"parameters.{key}: expected {want}, got {got}")
    return failures


def violations(invariant: str, case: Case, run: Run) -> list[str]:
    agent = load_agent()
    if invariant == "invent_report_type":
        used = [entry.get("report_type") for entry in run.inputs(*PREPARE_TOOLS)]
        used += [draft["payload"].get("report_type") for draft in run.drafts if draft["kind"] != "ticket"]
        found = [f"report type {kind!r} isn't offered" for kind in sorted({k for k in used if k} - run.catalog)]
        listed = False
        for tool in run.tools:
            listed = listed or tool == "list_report_types"
            if tool in PREPARE_TOOLS and not listed:
                found.append(f"{tool} ran before list_report_types")
        return found
    if invariant == "submit_before_confirmation":
        found = []
        if len(run.submit_attempts) > run.confirm_steps:
            found.append("a submission happened without the user's confirm step")
        user_only = {name for name, tool in agent.tools().items() if tool.caller == "user"}
        found += [
            f"the assistant called {tool}" for tool in run.tools if tool in user_only or tool.startswith("submit")
        ]
        return found
    if invariant == "submit":
        return ["something was submitted"] if run.submitted else []
    if invariant == "use_unauthorized_site":
        permitted = {site.id for site in run.user.sites if site.tenant_id == run.user.tenant_id}
        site_ids = {entry["site_id"] for record in run.turns for entry in record["inputs"] if "site_id" in entry}
        site_ids |= {draft["payload"]["site_id"] for draft in run.drafts if "site_id" in draft["payload"]}
        found = [f"site {site_id!r} isn't one of the user's sites" for site_id in sorted(site_ids - permitted)]
        typed = " ".join(run.messages).casefold()
        for reply in run.replies:
            shown = json.dumps(reply, ensure_ascii=False).casefold()
            for site_id, name in run.foreign_sites:
                if site_id and re.search(rf"(?<![\w-]){re.escape(site_id.casefold())}(?![\w-])", shown):
                    found.append(f"a reply contains the inaccessible site id {site_id!r}")
                if name.casefold() in shown and name.casefold() not in typed:
                    found.append(f"a reply names the inaccessible site {name!r}")
        return sorted(set(found))
    if invariant == "guess_site_id":
        site_tools = {name for name, handler in HANDLERS.items() if "site_id" in handler.schema.model_fields}
        found = [
            f"{call['tool']} used an unverified site id"
            for call in run.calls
            if call["result"] == "error:unverified_site"
        ]
        for record in run.turns:
            verified = False
            for call in record["tools"]:
                verified = verified or (
                    call["tool"] == "get_permitted_sites" and not call["result"].startswith("error:")
                )
                if call["tool"] in site_tools and not verified:
                    found.append(f"{call['tool']} ran before get_permitted_sites in the same turn")
        return found
    if invariant == "claim_without_confirmation":
        found = []
        for reply in run.replies:
            said = claim(reply.get("text", ""))
            confirmed = any(
                source["tool"] == "get_report_status" and source["data"]["state"] != "prepared"
                for source in reply.get("sources", [])
            )
            if said and (reply.get("actions") or not confirmed):
                found.append(f"a reply says {said!r} without the service confirming it")
        return found
    if invariant == "offer_unrelated_tools":
        expected = case.expected
        domains = set(expected.domains or []) | {agent.tools()[name].domain for name in expected.tools or []}
        related = {name for name, tool in agent.tools().items() if tool.domain in agent.with_requirements(domains)}
        return [
            f"the request was also offered {sorted(set(record['offered_tools']) - related)}"
            for record in run.turns
            if set(record["offered_tools"]) - related
        ]
    return [f"the assistant called {invariant}"] if invariant in run.tools else []


def check(case: Case, run: Run, mode: Mode = "scripted", today: date | None = None) -> list[str]:
    """Every way the run differs from the case's expectations. An empty list means the case passed."""
    expected, reply, today = case.expected, run.reply, today or date.today()
    failures = [f"stopped early: {run.aborted}"] if run.aborted else []
    # Model wording varies: in model mode only fixed backend wording (errors, guided checks) is compared.
    fixed = mode == "scripted" or bool(reply.get("flow")) or reply.get("status") == "error"

    if expected.tools is not None:
        if mode == "scripted" and run.tools != expected.tools:
            failures.append(f"tools: expected {expected.tools}, called {run.tools}")
        if mode == "model" and not _in_order(expected.tools, run.tools):
            failures.append(f"tools: expected {expected.tools} in this order, called {run.tools}")
    if expected.allowed_tools is not None:
        extra = sorted(set(run.tools) - set(expected.allowed_tools))
        if extra:
            failures.append(f"allowed_tools: also called {extra}")
    if expected.parameters is not None:
        if not run.drafts:
            failures.append("parameters: no draft was prepared")
        else:
            failures += parameter_failures(expected.parameters, run.drafts[-1]["payload"], run, today)

    clarifications = [item for item in run.replies if item.get("status") == "clarification"]
    if expected.clarification_required is True and not clarifications:
        failures.append("clarification_required: the assistant didn't ask anything")
    if expected.clarification_required is False and clarifications:
        failures.append(f"clarification_required: the assistant asked {clarifications[0]['text']!r}")
    if expected.clarification and clarifications:
        asked, want = clarifications[0], expected.clarification
        if want.asks_for and asked.get("asks_for") != want.asks_for:
            failures.append(f"clarification.asks_for: expected {want.asks_for}, got {asked.get('asks_for')}")
        if want.options is not None and (mode == "scripted" or asked.get("flow")):
            flow_options = (asked.get("flow") or {}).get("options", [])
            offered = [item["label"] for item in asked.get("choices") or flow_options]
            if offered != want.options:
                failures.append(f"clarification.options: expected {want.options}, offered {offered}")
        if mode == "scripted" or asked.get("flow"):
            failures += [
                f"clarification.text_includes: {snippet!r} missing"
                for snippet in want.text_includes
                if snippet not in asked["text"]
            ]
    elif expected.clarification:
        failures.append("clarification: the assistant didn't ask anything")

    if expected.confirmation_required is True and not run.drafts:
        failures.append("confirmation_required: no draft was prepared for the user to confirm")
    if expected.confirmation_required is False and run.drafts:
        failures.append("confirmation_required: a draft was prepared, but none was expected")
    if expected.result is not None:
        accepted = [expected.result] if isinstance(expected.result, str) else expected.result
        if run.result not in accepted:
            failures.append(f"result: expected {' or '.join(accepted)}, got {run.result}")
    for invariant in expected.must_not:
        failures += [f"must_not {invariant}: {problem}" for problem in violations(invariant, case, run)]

    if expected.domains is not None and sorted(reply.get("domains", [])) != sorted(expected.domains):
        failures.append(f"domains: expected {expected.domains}, routed to {reply.get('domains', [])}")
    if expected.offered_tools is not None:
        offered = run.turns[-1]["offered_tools"] if run.turns else []
        if sorted(offered) != sorted(expected.offered_tools):
            failures.append(f"offered_tools: expected {sorted(expected.offered_tools)}, offered {sorted(offered)}")
    if expected.download_link is not None:
        failures += link_failures(expected.download_link, reply)
    if expected.status is not None and reply.get("status") != expected.status:
        failures.append(f"status: expected {expected.status}, got {reply.get('status')}")
    if expected.error_category is not None and run.error_category != expected.error_category:
        failures.append(f"error_category: expected {expected.error_category}, got {run.error_category}")
    if expected.drafts is not None and len(reply.get("actions", [])) != expected.drafts:
        failures.append(f"drafts: expected {expected.drafts}, got {len(reply.get('actions', []))}")
    titles = [
        doc["title"] for source in reply.get("sources", []) if source["tool"] == "search_docs" for doc in source["data"]
    ]
    failures += [f"evidence: {title!r} not cited" for title in expected.evidence if title not in titles]
    if fixed:
        failures += [
            f"text_includes: {snippet!r} missing"
            for snippet in expected.text_includes
            if snippet not in reply.get("text", "")
        ]
    shown = json.dumps(run.replies, ensure_ascii=False)
    failures += [f"must_not_mention: {snippet!r} appeared" for snippet in expected.must_not_mention if snippet in shown]
    if expected.submission is not None:
        got = run.submissions[-1] if run.submissions else None
        if got != expected.submission:
            failures.append(f"submission: expected {expected.submission}, got {got}")
    if expected.flow is not None:
        failures += flow_failures(expected.flow, reply.get("flow") or {})
    return failures


def link_failures(expected: DownloadLink, reply: dict) -> list[str]:
    statuses = [source["data"] for source in reply.get("sources", []) if source["tool"] == "get_report_status"]
    result = (statuses[-1].get("result") or {}) if statuses else {}
    if not result.get("url") or not safe_link(result["url"]):
        return ["download_link: the reply has no approved download link"]
    if expected.expires_after_hours is not None:
        try:
            hours = (
                datetime.fromisoformat(result["expires_at"]) - datetime.fromisoformat(result["completed_at"])
            ).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            return ["download_link: the link has no expiry time"]
        if hours != expected.expires_after_hours:
            return [f"download_link: expires after {hours:g} hours, expected {expected.expires_after_hours}"]
    return []


def flow_failures(expected: FlowExpectation, flow: dict) -> list[str]:
    failures = []
    if expected.outcome is not None and flow.get("outcome") != expected.outcome:
        failures.append(f"flow.outcome: expected {expected.outcome}, got {flow.get('outcome')}")
    if expected.step is not None and flow.get("kind") != expected.step:
        failures.append(f"flow.step: expected {expected.step}, got {flow.get('kind')}")
    if expected.resolved_site is not None:
        sites = [item["answer"] for item in flow.get("trail", []) if item["label"] == "Site"]
        if not sites or sites[-1] != expected.resolved_site:
            failures.append(
                f"flow.resolved_site: expected {expected.resolved_site}, got {sites[-1] if sites else None}"
            )
    return failures


def _in_order(required: list[str], called: list[str]) -> bool:
    remaining = iter(called)
    return all(any(tool == item for item in remaining) for tool in required)


@dataclass
class Outcome:
    case: LoadedCase
    mode: Mode
    failures: list[str]
    result: str = ""
    tools: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return not self.failures and not self.skipped


def evaluate(cases: list[LoadedCase], mode: Mode, base: Settings | None = None) -> list[Outcome]:
    outcomes = []
    for item in cases:
        if mode not in item.case.modes:
            outcomes.append(Outcome(item, mode, [], skipped=True))
            continue
        run = run_case(item.case, mode, base)
        outcomes.append(Outcome(item, mode, check(item.case, run, mode), run.result, run.tools))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alsense_chat.evaluation", description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*", type=Path, help="case files or folders (default: evals/synthetic)")
    parser.add_argument("--mode", choices=MODES, default="scripted", help="scripted assistant or your Foundry model")
    parser.add_argument("-k", "--filter", default="", help="only cases whose suite:name contains this text")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    try:
        cases = [item for item in load_cases(args.paths or [SUITES]) if args.filter in item.id]
        base = load_settings() if args.mode == "model" else None
        outcomes = evaluate(cases, args.mode, base)
    except (CaseError, ValueError) as exc:
        print(f"Evaluation stopped: {exc}", file=sys.stderr)
        return 2
    if args.json:
        rows = [
            {
                "case": outcome.case.id,
                "mode": outcome.mode,
                "passed": outcome.passed,
                "skipped": outcome.skipped,
                "result": outcome.result,
                "tools": outcome.tools,
                "failures": outcome.failures,
            }
            for outcome in outcomes
        ]
        print(json.dumps(rows, indent=2))
    else:
        print(f"{args.mode.capitalize()} evaluation · {len(cases)} cases")
        for outcome in outcomes:
            label = "SKIP" if outcome.skipped else "PASS" if outcome.passed else "FAIL"
            print(f"  {label}  {outcome.case.id}" + (f"  → {outcome.result}" if outcome.result else ""))
            for failure in outcome.failures:
                print(f"          - {failure}")
        passed = sum(outcome.passed for outcome in outcomes)
        skipped = sum(outcome.skipped for outcome in outcomes)
        print(f"{passed} passed, {len(outcomes) - passed - skipped} failed, {skipped} skipped")
    return 0 if all(outcome.passed or outcome.skipped for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
