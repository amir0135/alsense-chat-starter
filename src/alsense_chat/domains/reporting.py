"""Reporting: on-demand and scheduled reports, prepared as drafts and created only after the user confirms.

Tools (declared in `agent/reporting/tools.yaml`): list_report_types, prepare_report, prepare_scheduled_report,
get_report_status, and confirm_report, which is the confirmation endpoint and never a model tool.
`plan` is the scripted assistant's plain-language path: resolve the site, choose a valid report type, prepare.
"""

import re
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator, model_validator

from ..adapters.contracts import ReportRequest, ReportType, ScheduledReportRequest
from ..adapters.errors import ServiceUnavailable
from .base import Arguments, Choice, Handler, InvalidRequest, no_inputs, run_tool, safe_link
from .sites import contains, normalize

if TYPE_CHECKING:
    from ..tools import Context

KINDS = ("report", "scheduled_report")
FORMAT_LABELS = {"pdf": "PDF", "csv": "CSV", "xlsx": "Excel"}
FREQUENCY_LABELS = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
MAX_WINDOW_DAYS = 366
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class OnDemandReport(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    report_type: str = Field(min_length=1, max_length=100)
    format: Literal["pdf", "csv", "xlsx"]
    start: date
    end: date

    @model_validator(mode="after")
    def window(self):
        if self.start > self.end:
            raise ValueError("The start date must be on or before the end date.")
        if (self.end - self.start).days > MAX_WINDOW_DAYS:
            raise ValueError("The report period is too long. Choose a period of one year or less.")
        return self


class ScheduledReport(Arguments):
    site_id: str = Field(min_length=1, max_length=100)
    report_type: str = Field(min_length=1, max_length=100)
    format: Literal["pdf", "csv", "xlsx"]
    frequency: Literal["daily", "weekly", "monthly"]
    recipients: list[str] = Field(min_length=1, max_length=25)
    start_date: date
    end_date: date | None = None

    @field_validator("recipients")
    @classmethod
    def emails(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            address = item.strip()
            if not EMAIL.fullmatch(address):
                raise ValueError("Each recipient must be an email address.")
            if address.casefold() not in {known.casefold() for known in cleaned}:
                cleaned.append(address)
        return cleaned

    @model_validator(mode="after")
    def dates(self):
        if self.start_date < date.today():
            raise ValueError("The start date can't be in the past.")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("The end date must be on or after the start date.")
        return self


class Status(Arguments):
    action_id: str = Field(min_length=1, max_length=100)


class NoArguments(Arguments):
    pass


def catalog(ctx: "Context") -> dict[str, ReportType]:
    return {item.id: item for item in ctx.services.reports.report_types(ctx.user)}


def validate(ctx: "Context", kind: str, payload: dict) -> None:
    """Check a draft against the report service's catalog. Runs when preparing and again when confirming."""
    request: OnDemandReport | ScheduledReport
    request = OnDemandReport.model_validate(payload) if kind == "report" else ScheduledReport.model_validate(payload)
    report_type = catalog(ctx).get(request.report_type)
    if report_type is None or request.format not in report_type.formats:
        raise InvalidRequest("That report type or format isn't available. Choose one from the report list.")
    if isinstance(request, ScheduledReport):
        schedule = report_type.schedule
        if schedule is None:
            raise InvalidRequest(f"{report_type.label} reports can't be scheduled. Prepare one on demand instead.")
        if request.frequency not in schedule.frequencies:
            allowed = [FREQUENCY_LABELS[item].lower() for item in schedule.frequencies]
            listed = allowed[0] if len(allowed) == 1 else ", ".join(allowed[:-1]) + " or " + allowed[-1]
            raise InvalidRequest(f"{report_type.label} reports can be scheduled {listed}, not {request.frequency}.")
        if request.end_date is not None and not schedule.end_date_supported:
            raise InvalidRequest(f"{report_type.label} schedules can't have an end date.")
        if len(request.recipients) > schedule.max_recipients:
            limit = schedule.max_recipients
            raise InvalidRequest(
                f"{report_type.label} reports can go to up to {limit} recipients. Remove some addresses."
            )


def prepare(kind: str):
    def run(ctx: "Context", args: dict) -> dict:
        ctx.check_site(args["site_id"])
        validate(ctx, kind, args)
        action = ctx.store.prepare(ctx.session_id, kind, args)
        ctx.actions.append(action)
        return action

    return run


def submit(ctx: "Context", action: dict) -> dict:
    """Create the report or schedule after the user confirmed. Only the confirmation endpoint calls this."""
    if action["kind"] == "report":
        report = ReportRequest.model_validate(action["payload"])
        submission = ctx.services.reports.submit_report(ctx.user, report, action["id"])
    else:
        schedule = ScheduledReportRequest.model_validate(action["payload"])
        submission = ctx.services.reports.schedule_report(ctx.user, schedule, action["id"])
    return submission.model_dump(mode="json")


def report_status(ctx: "Context", args: dict) -> dict:
    action = ctx.store.action(ctx.session_id, args["action_id"])
    ctx.check_site(action["payload"]["site_id"])
    if action["kind"] not in KINDS:
        raise InvalidRequest("That draft is not a report.")
    if action["result"] is None:
        return {"kind": action["kind"], "state": "prepared", "result": None}
    if action["kind"] == "scheduled_report":
        return {"kind": "scheduled_report", "state": "scheduled", "result": action["result"]}
    job = ctx.services.reports.report_status(ctx.user, action["result"]["id"])
    if job.url and not safe_link(job.url):
        raise ServiceUnavailable("reports", "The report service returned an unapproved link, so I withheld it.")
    return {"kind": "report", "state": job.state, "result": job.model_dump(mode="json")}


def report_audit(args: dict) -> dict:
    return {key: args[key] for key in ("site_id", "report_type", "format", "start", "end")}


def schedule_audit(args: dict) -> dict:
    inputs = {key: args[key] for key in ("site_id", "report_type", "format", "frequency", "start_date")}
    if args.get("end_date"):
        inputs["end_date"] = args["end_date"]
    # Email addresses are personal data: audit how many, never which.
    inputs["recipients"] = len(args["recipients"])
    return inputs


HANDLERS = {
    "list_report_types": Handler(
        schema=NoArguments,
        run=lambda ctx, args: [item.model_dump(mode="json") for item in catalog(ctx).values()],
        summarize=lambda result: f"report_types:{len(result)}",
        label="Report catalogue",
        audit=no_inputs,
    ),
    "prepare_report": Handler(
        schema=OnDemandReport,
        run=prepare("report"),
        summarize=lambda result: "draft_prepared",
        label="Report draft",
        audit=report_audit,
    ),
    "prepare_scheduled_report": Handler(
        schema=ScheduledReport,
        run=prepare("scheduled_report"),
        summarize=lambda result: "draft_prepared",
        label="Scheduled report draft",
        audit=schedule_audit,
    ),
    "get_report_status": Handler(
        schema=Status,
        run=report_status,
        summarize=lambda result: f"state:{result['state']}",
        label="Report status",
        audit=no_inputs,
    ),
}


# Scripted plain-language path ---------------------------------------------------------------------------------

LIST_TYPES = re.compile(
    r"\breport types?\b|\b(?:what|which)\b.*\breports?\b.*\b(?:available|types?|options|can i)\b"
    r"|\b(?:list|available)\b.*\breports\b",
    re.IGNORECASE,
)
SCHEDULED = re.compile(
    r"\bschedul|\brecurring\b|\bevery\b|\beach (?:day|week|month)\b|\b(?:daily|weekly|monthly)\b", re.I
)
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fourteen": 14,
    "twenty": 20,
    "thirty": 30,
    "sixty": 60,
    "ninety": 90,
}
COUNT = r"(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")"
ISO = r"\d{4}-\d{2}-\d{2}"
PLACE = re.compile(r"\b(?:for|at|in|from)\s+((?:[\w'’&.-]+\s*){1,6})", re.IGNORECASE)
PLACE_STOP = {
    "for",
    "from",
    "to",
    "over",
    "during",
    "between",
    "since",
    "until",
    "starting",
    "as",
    "in",
    "on",
    "with",
    "by",
    "and",
    "every",
    "each",
    "the",
    "last",
    "this",
    "past",
    "report",
    "reports",
    "daily",
    "weekly",
    "monthly",
    "pdf",
    "csv",
    "excel",
    "xlsx",
    "please",
    "covering",
    "showing",
    "via",
    "using",
    "at",
}
NOT_A_PLACE = {
    "the",
    "last",
    "this",
    "past",
    "previous",
    "next",
    "yesterday",
    "today",
    "tomorrow",
    "a",
    "an",
    "me",
    "us",
    "pdf",
    "csv",
    "excel",
    "xlsx",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "week",
    "month",
    "day",
    "days",
    "all",
    "every",
    "each",
    *NUMBER_WORDS,
}
TYPE_STOP = {"report", "reports", "site", "data", "history", "log"}


def count(word: str) -> int:
    return int(word) if word.isdigit() else NUMBER_WORDS[word.casefold()]


def parse_period(text: str, today: date) -> tuple[date, date] | None:
    lowered = text.casefold()
    explicit = re.search(rf"({ISO})\s*(?:to|until|through|and|-|–)\s*({ISO})", lowered)
    if explicit:
        try:
            return date.fromisoformat(explicit.group(1)), date.fromisoformat(explicit.group(2))
        except ValueError:
            return None
    days = re.search(rf"\b(?:last|past|previous)\s+{COUNT}\s+days?\b", lowered)
    if days:
        return today - timedelta(days=max(count(days.group(1)), 1) - 1), today
    weeks = re.search(rf"\b(?:last|past|previous)\s+{COUNT}\s+weeks?\b", lowered)
    if weeks:
        return today - timedelta(days=7 * max(count(weeks.group(1)), 1) - 1), today
    if re.search(r"\b(?:last|past|previous)\s+week\b", lowered):
        return today - timedelta(days=6), today
    if re.search(r"\bthis\s+week\b", lowered):
        return today - timedelta(days=today.weekday()), today
    if re.search(r"\bthis\s+month\b", lowered):
        return today.replace(day=1), today
    if re.search(r"\b(?:last|previous)\s+month\b", lowered):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if re.search(r"\byesterday\b", lowered):
        return today - timedelta(days=1), today - timedelta(days=1)
    if re.search(r"\btoday\b", lowered):
        return today, today
    return None


def parse_format(text: str) -> str | None:
    lowered = text.casefold()
    if re.search(r"\b(?:excel|xlsx)\b", lowered):
        return "xlsx"
    if re.search(r"\bcsv\b", lowered):
        return "csv"
    if re.search(r"\bpdf\b", lowered):
        return "pdf"
    return None


def parse_frequency(text: str) -> str | None:
    lowered = text.casefold()
    if re.search(r"\b(?:daily|every day|each day)\b", lowered):
        return "daily"
    weekday = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    if re.search(rf"\b(?:weekly|every week|each week|every {weekday})\b", lowered):
        return "weekly"
    if re.search(r"\b(?:monthly|every month|each month)\b", lowered):
        return "monthly"
    return None


def parse_day(word: str, today: date) -> date | None:
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError:
        return None


def parse_start(text: str, today: date) -> date | None:
    found = re.search(rf"\b(?:starting|start|from|beginning)\s+(?:on\s+)?({ISO}|today|tomorrow)\b", text.casefold())
    return parse_day(found.group(1), today) if found else None


def parse_end(text: str, today: date) -> date | None:
    found = re.search(rf"\b(?:until|till|through|ending(?:\s+on)?|end(?:s|ing)?\s+on)\s+({ISO})\b", text.casefold())
    return parse_day(found.group(1), today) if found else None


def site_phrase(text: str, site_names: list[str]) -> str | None:
    """The site the user mentioned, in their own words, for the resolver to match.

    First the words after "for", "at", "in" or "from". Otherwise a permitted site's name found in the message, with
    the capitalized words that follow it. Never shorten the user's words to a permitted name: "Demo North Annex"
    must reach the resolver as said, so it is confirmed instead of silently treated as Demo North.
    """
    for found in PLACE.finditer(text):
        words: list[str] = []
        for index, word in enumerate(found.group(1).split()):
            clean = word.strip(".,;:!?")
            folded = clean.casefold()
            if index == 0 and folded == "the":
                continue
            if not clean or folded in PLACE_STOP or EMAIL.fullmatch(clean) or re.fullmatch(ISO, folded):
                break
            words.append(clean)
            if clean != word.rstrip():
                break
        if words and words[0].casefold() not in NOT_A_PLACE and not words[0][0].isdigit():
            return " ".join(words)
    for name in sorted(site_names, key=len, reverse=True):
        found_name = re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.IGNORECASE)
        if not found_name:
            continue
        words = [found_name.group(0)]
        for word in text[found_name.end() :].split():
            clean = word.strip(".,;:!?")
            if not clean or not clean[0].isupper() or clean.casefold() in PLACE_STOP | NOT_A_PLACE:
                break
            words.append(clean)
            if clean != word:
                break
        return " ".join(words)
    return None


def match_types(text: str, types: list[ReportType]) -> list[ReportType]:
    folded = normalize(text)
    found = []
    for item in types:
        phrases = {normalize(item.label), normalize(item.id.replace("-", " "))}
        first = normalize(item.label).split()[0]
        if first not in TYPE_STOP:
            phrases |= {first, first + "s"}
        if first == "temperature":
            phrases.add("temp")
        if any(contains(folded, phrase) for phrase in phrases):
            found.append(item)
    return found


def replace_site(text: str, phrase: str | None, name: str) -> str:
    if phrase:
        pattern = r"\s+".join(re.escape(word) for word in phrase.split())
        replaced, count_ = re.subn(pattern, name, text, count=1, flags=re.IGNORECASE)
        if count_:
            return replaced
    return f"{text.rstrip('.!? ')} for {name}"


@dataclass
class Request:
    """What the user asked for, as far as it is known. `describe` writes it back as a request the planner parses."""

    scheduled: bool
    site: str = ""
    report_type: ReportType | None = None
    format: str = ""
    start: date | None = None
    end: date | None = None
    frequency: str = ""
    recipients: list[str] = field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None

    def describe(self, **changes) -> str:
        values = {**self.__dict__, **changes}
        label = values["report_type"].label.casefold() if values["report_type"] else ""
        noun = " ".join(word for word in ([values["frequency"], label] if values["scheduled"] else [label]) if word)
        article = "an" if noun[:1] in tuple("aeiou") else "a"
        parts = ["Schedule" if values["scheduled"] else "Create", article, noun, "report"]
        if values["site"]:
            parts += ["for", values["site"]]
        if not values["scheduled"] and values["start"] and values["end"]:
            parts += ["from", values["start"].isoformat(), "to", values["end"].isoformat()]
        if values["format"]:
            parts += ["as", FORMAT_LABELS[values["format"]]]
        if values["scheduled"]:
            if values["recipients"]:
                parts += ["to", ", ".join(values["recipients"])]
            if values["start_date"]:
                parts += ["starting", values["start_date"].isoformat()]
            if values["end_date"]:
                parts += ["until", values["end_date"].isoformat()]
        return " ".join(part for part in parts if part)


@dataclass
class Plan:
    """The planner's result. The runner turns it into a checked reply."""

    kind: Literal["failure", "clarify", "not_found", "prepared"]
    text: str = ""
    asks_for: str | None = None
    choices: list[Choice] = field(default_factory=list)
    builder: dict | None = None
    output: dict | None = None


def wants_report_list(message: str) -> bool:
    return bool(LIST_TYPES.search(message)) and not re.search(
        r"\b(?:create|prepare|make|generate|schedule|send)\b", message, re.I
    )


def plan(message: str, ctx: "Context") -> Generator[tuple[str, dict], None, Plan]:
    """Resolve an authorized site, choose a valid report type, then prepare the draft (scripted assistant)."""
    today = date.today()
    phrase = site_phrase(message, [site.name for site in ctx.user.sites])
    sites = yield from run_tool(ctx, "get_permitted_sites", {"name": phrase} if phrase else {})
    if "error" in sites:
        return Plan("failure", output=sites)
    match, found = sites["data"]["match"], sites["data"]["sites"]
    if match == "one" and not sites["data"]["exact"]:
        name = found[0]["name"]
        return Plan(
            "clarify", f"Do you mean {name}?", "site", [Choice(f"Yes, {name}", replace_site(message, phrase, name))]
        )
    if match == "several" or (match == "all" and len(found) > 1):
        question = "Which site do you mean?" if match == "several" else "Which site should the report cover?"
        return Plan(
            "clarify",
            question,
            "site",
            [Choice(item["name"], replace_site(message, phrase, item["name"])) for item in found],
        )
    if match == "none" or not found:
        alternatives = sites["data"].get("alternatives", [])
        if not alternatives:
            return Plan("not_found", "You don't have access to any sites, so I can't prepare a report.")
        choices = [Choice(item["name"], replace_site(message, phrase, item["name"])) for item in alternatives]
        return Plan(
            "clarify",
            "I couldn't find that site among the sites you can access. Which site should the report cover?",
            "site",
            choices,
        )
    site = found[0]
    scheduled = bool(SCHEDULED.search(message))
    # Read every detail now, so each answer button below carries everything the user already said.
    request = Request(
        scheduled=scheduled,
        site=site["name"],
        format=parse_format(message) or "",
        frequency=(parse_frequency(message) or "") if scheduled else "",
        recipients=list(dict.fromkeys(EMAIL.findall(message))) if scheduled else [],
        start_date=parse_start(message, today) if scheduled else None,
        end_date=parse_end(message, today) if scheduled else None,
    )
    period = None if scheduled else parse_period(message, today)
    if period:
        request.start, request.end = period
    types = yield from run_tool(ctx, "list_report_types", {})
    if "error" in types:
        return Plan("failure", output=types)
    catalog_ = [ReportType.model_validate(item) for item in types["data"]]
    offered = [item for item in catalog_ if item.schedule or not request.scheduled]
    candidates = match_types(message, catalog_)
    if len(candidates) != 1:
        question = "Which report do you need?" if not candidates else "Which of these reports do you mean?"
        options = [item for item in (candidates or offered)]
        return Plan(
            "clarify",
            question,
            "report_type",
            [Choice(item.label, request.describe(report_type=item)) for item in options],
        )
    request.report_type = report_type = candidates[0]
    if request.scheduled and report_type.schedule is None:
        choices = [Choice(item.label, request.describe(report_type=item)) for item in offered]
        choices.append(Choice(f"{report_type.label} once", request.describe(scheduled=False, report_type=report_type)))
        return Plan(
            "clarify",
            f"{report_type.label} reports can't be scheduled. Which report should I schedule?",
            "report_type",
            choices,
        )
    if request.format and request.format not in report_type.formats:
        labels = " or ".join(FORMAT_LABELS[item] for item in report_type.formats)
        choices = [Choice(FORMAT_LABELS[item], request.describe(format=item)) for item in report_type.formats]
        return Plan(
            "clarify", f"{report_type.label} reports come as {labels}. Which format do you want?", "format", choices
        )
    request.format = request.format or ("pdf" if "pdf" in report_type.formats else report_type.formats[0])
    if not request.scheduled:
        if period is None:
            presets = {
                "Last 7 days": (today - timedelta(days=6), today),
                "Last 30 days": (today - timedelta(days=29), today),
                "This month": (today.replace(day=1), today),
            }
            choices = [Choice(label, request.describe(start=start, end=end)) for label, (start, end) in presets.items()]
            return Plan("clarify", "Which period should the report cover?", "period", choices)
        args = {
            "site_id": site["id"],
            "report_type": report_type.id,
            "format": request.format,
            "start": period[0].isoformat(),
            "end": period[1].isoformat(),
        }
        output = yield from run_tool(ctx, "prepare_report", args)
        return Plan("failure", output=output) if "error" in output else Plan("prepared", output=output)
    schedule = report_type.schedule
    assert schedule is not None
    if request.frequency not in schedule.frequencies:
        question = "How often should it be sent?"
        if request.frequency:
            question = f"{report_type.label} reports can't be sent {request.frequency}. {question}"
        choices = [Choice(FREQUENCY_LABELS[item], request.describe(frequency=item)) for item in schedule.frequencies]
        return Plan("clarify", question, "frequency", choices)
    request.start_date = request.start_date or today + timedelta(days=1)
    if not request.recipients:
        builder = {
            "mode": "scheduled",
            "site_id": site["id"],
            "report_type": report_type.id,
            "format": request.format,
            "frequency": request.frequency,
            "start_date": request.start_date.isoformat(),
        }
        if request.end_date:
            builder["end_date"] = request.end_date.isoformat()
        return Plan(
            "clarify",
            "Who should receive it? Add their email addresses to the report details.",
            "recipients",
            builder=builder,
        )
    args = {
        "site_id": site["id"],
        "report_type": report_type.id,
        "format": request.format,
        "frequency": request.frequency,
        "recipients": request.recipients,
        "start_date": request.start_date.isoformat(),
    }
    if request.end_date:
        args["end_date"] = request.end_date.isoformat()
    output = yield from run_tool(ctx, "prepare_scheduled_report", args)
    return Plan("failure", output=output) if "error" in output else Plan("prepared", output=output)
