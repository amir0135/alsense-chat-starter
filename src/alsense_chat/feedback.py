"""Answer feedback: a thumbs-up or thumbs-down on a completed answer, with what is needed to investigate it.

A rating is stored against the answer's id, one row per answer (a later rating replaces it), with an optional
reason from a fixed list and a snapshot of what produced the answer: reply status, routed domains, guided-check
id, version and outcome, tools with their result categories, error category, assistant, data source, app version
and total latency. Never the question, the answer text, its sources, guided-check answers or free text. The
conversation stays in the session database's turns, linked by the answer id and the correlation id.

The review starts with the initial KPI, satisfied answers: answers rated helpful / answers given (see DEFINITION),
then lists the ratings, newest first. It reads the audit trail and the feedback table only, never the turns, and
needs only the standard library (read-only):

    uv run python scripts/dev.py feedback [--db PATH] [--since DAYS] [--rating up|down] [--limit N] [--json]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import textwrap
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

Rating = Literal["up", "down"]
Reason = Literal["incorrect", "incomplete", "too_slow", "other"]
# Guided-check steps still waiting for the user's answer, and the menu of checks, are not answers yet.
WAITING = {"menu", "question", "site", "confirm"}
# The values a context may hold. Anything outside them is stored as "other" (an undeclared tool as "unknown").
STATUSES = frozenset({"answer", "clarification", "not_in_data", "out_of_scope", "error"})
ASSISTANTS = frozenset({"scripted", "model", "guided"})
DATA_SOURCES = frozenset({"synthetic", "alsense"})
OUTCOMES = frozenset({"resolve", "ticket"})
TOOL_ERRORS = frozenset({"unknown_tool", "disabled", "invalid_input", "unverified_site", "not_found", "runtime_error"})
# Tool result summaries keep a count or one of these details; any other detail (such as an upstream health-check
# type) is dropped and only the summary's kind is kept.
SUMMARIES = frozenset({"match", "report_types", "draft_prepared", "state", "documents", "open_checks"})
DETAILS = frozenset({"all", "one", "several", "none", "prepared", "processing", "completed", "failed", "scheduled"})
VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}(\+[0-9a-f]{4,40})?")
# The KPI reads replies from the audit trail. Their response category says what rateable() decides from the reply:
# a run that stored no reply, or a guided-check step still waiting for an answer, or the menu, is not an answer.
FAILED = "runtime_error"
NOT_ANSWERS = frozenset(f"flow:{kind}" for kind in WAITING)
REASONS = (*get_args(Reason), "no_reason")
DEFINITION = (
    "Answers given: chat replies and guided-check outcomes stored with an answer id that can be rated; not the menu "
    "of checks or guided-check steps that still wait for an answer.",
    "Helpful: answers whose latest rating is Helpful. A rating counts with its answer, whenever it was given.",
    "KPI, satisfied answers = helpful / answers given. Unrated answers count as not satisfied, so read it with the "
    "rated share.",
    "Failed runs ended without a stored answer. They are counted apart and are not part of the KPI.",
    "The period is when the answer was given.",
)


@dataclass(frozen=True)
class Vocabulary:
    """The code- and configuration-defined names a feedback context may contain."""

    tools: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    errors: frozenset[str] = TOOL_ERRORS
    # Guided check id -> (version, its outcome categories).
    flows: Mapping[str, tuple[str, frozenset[str]]] = field(default_factory=dict)

    @classmethod
    def build(cls, flows: dict[str, dict]) -> "Vocabulary":
        # Imported here: the review command below needs only the standard library.
        from .adapters.errors import ERRORS
        from .agent import load_agent

        agent = load_agent()
        errors = TOOL_ERRORS | set(ERRORS)
        lookups = {f"lookup_{category}" for category in errors}
        return cls(
            tools=frozenset(agent.tools()),
            domains=frozenset(agent.modules),
            errors=frozenset(errors),
            flows={
                flow_id: (
                    flow["version"],
                    frozenset(
                        {
                            node.get("category") or str(node_id).replace("-", "_")
                            for node_id, node in flow["nodes"].items()
                            if "outcome" in node
                        }
                        | lookups
                    ),
                )
                for flow_id, flow in flows.items()
            },
        )


def rateable(reply: dict) -> bool:
    """A completed reply: an answer, clarification, refusal or error, or the outcome of a guided check."""
    return (reply.get("flow") or {}).get("kind") not in WAITING


def _known(value: Any, allowed: frozenset[str] | Mapping[str, Any], unknown: str = "other") -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else unknown


def _result(value: Any, errors: frozenset[str]) -> str:
    kind, _, detail = (value if isinstance(value, str) else "").partition(":")
    if kind == "error":
        return f"error:{_known(detail, errors)}"
    if kind not in SUMMARIES:
        return "ok"
    return f"{kind}:{detail}" if detail.isdecimal() or detail in DETAILS else kind


def answer_context(reply: dict, record: dict | None, vocabulary: Vocabulary) -> dict:
    """What produced an answer, as categories and numbers. `record` is the audit record of its request."""
    flow = reply.get("flow") or {}
    context: dict[str, Any] = {
        "status": _known(reply.get("status"), STATUSES),
        "domains": [_known(domain, vocabulary.domains) for domain in reply.get("domains") or []],
        "error_category": _known((reply.get("error") or {}).get("category"), vocabulary.errors),
    }
    if flow:
        flow_version, categories = vocabulary.flows.get(str(flow.get("id")), ("", frozenset()))
        context["flow"] = {
            "id": _known(flow.get("id"), vocabulary.flows),
            "version": flow_version if flow_version and flow.get("version") == flow_version else "other",
            "outcome": _known(flow.get("outcome"), OUTCOMES),
            "category": _known(flow.get("category"), categories),
        }
    if record:
        total = (record.get("timings") or {}).get("ms", {}).get("total")
        app_version = record.get("app_version")
        context |= {
            "assistant": _known(record.get("assistant"), ASSISTANTS),
            "tools": [
                {
                    "tool": _known(call.get("tool"), vocabulary.tools, "unknown"),
                    "result": _result(call.get("result"), vocabulary.errors),
                }
                for call in record.get("tools") or []
            ],
            "data_source": _known(record.get("data_source"), DATA_SOURCES),
            "app_version": app_version if isinstance(app_version, str) and VERSION.fullmatch(app_version) else None,
            "total_ms": total if isinstance(total, int | float) else None,
        }
        context["error_category"] = context["error_category"] or _known(record.get("error_category"), vocabulary.errors)
    return context


def _connect(path: str) -> sqlite3.Connection:
    """Read-only: the review never migrates or writes."""
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def read(path: str, rating: str | None, limit: int, since: float = 0.0) -> list[dict]:
    """The newest feedback records, of answers given since a time."""
    query = "SELECT answer_id, correlation_id, rating, reason, context, created, updated FROM feedback"
    conditions: list[str] = []
    params: list[Any] = []
    if rating:
        conditions.append("rating = ?")
        params.append(rating)
    if since:
        conditions.append("answer_id IN (SELECT answer_id FROM audit WHERE event IN ('chat', 'flow') AND created >= ?)")
        params.append(since)
    query += (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY updated DESC, answer_id LIMIT ?"
    with closing(_connect(path)) as db:
        rows = db.execute(query, (*params, limit)).fetchall()
    return [{**dict(row), "context": json.loads(row["context"])} for row in rows]


def read_requests(path: str, since: float) -> list[dict]:
    """Chat turns and guided-check steps since a time, each with its answer's latest rating."""
    query = (
        "SELECT a.answer_id, a.response_category, a.assistant, a.domains, f.rating, f.reason FROM audit a "
        "LEFT JOIN feedback f ON f.answer_id = a.answer_id "
        "WHERE a.event IN ('chat', 'flow') AND a.created >= ? ORDER BY a.id"
    )
    with closing(_connect(path)) as db:
        rows = db.execute(query, (since,)).fetchall()
    return [{**dict(row), "domains": json.loads(row["domains"] or "[]")} for row in rows]


def answered(row: dict) -> bool:
    """A stored reply that can be rated: the same answers rateable() accepts, read from the request's record."""
    category = row["response_category"]
    return bool(row["answer_id"]) and isinstance(category, str) and category not in {FAILED, *NOT_ANSWERS}


def counted(requests: list[dict]) -> list[dict]:
    """The answers given and the failed runs, once each."""
    seen: set[str] = set()
    found = []
    for row in requests:
        if answered(row) and row["answer_id"] not in seen:
            seen.add(row["answer_id"])
            found.append(row)
        elif row["response_category"] == FAILED:
            found.append(row)
    return found


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def tally(rows: list[dict]) -> dict:
    """The KPI and its parts for answers given and failed runs."""
    given = helpful = rated = failed = 0
    reasons = dict.fromkeys(REASONS, 0)
    for row in rows:
        if not answered(row):
            if row["response_category"] == FAILED:
                failed += 1
            continue
        given += 1
        if row["rating"] == "up":
            helpful += 1
            rated += 1
        elif row["rating"] == "down":
            rated += 1
            reason = row["reason"]
            reasons["no_reason" if reason is None else reason if reason in get_args(Reason) else "other"] += 1
    return {
        "answers_given": given,
        "helpful": helpful,
        "satisfied_rate": _share(helpful, given),
        "rated": rated,
        "rated_share": _share(rated, given),
        "helpful_share_of_rated": _share(helpful, rated),
        "not_helpful": {"total": rated - helpful, "by_reason": reasons},
        "failed_without_answer": failed,
    }


def _assistant(row: dict) -> str:
    return row["assistant"] if row["assistant"] in ASSISTANTS else "other" if row["assistant"] else "(not recorded)"


def _domains(row: dict) -> str:
    return "+".join(str(domain) for domain in row["domains"]) or "(none)"


def kpi_report(requests: list[dict]) -> dict:
    """The KPI overall, by assistant and by routed domains."""
    rows = counted(requests)

    def by(key) -> dict[str, dict]:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(key(row), []).append(row)
        return {name: tally(items) for name, items in sorted(groups.items())}

    return {"kpi": tally(rows), "by_assistant": by(_assistant), "by_domains": by(_domains)}


CONTEXT = (
    "status",
    "domains",
    "flow",
    "tools",
    "error_category",
    "assistant",
    "data_source",
    "app_version",
    "total_ms",
)


def entry(row: dict) -> dict:
    """One feedback record for review: ids, categories and numbers only."""
    return {
        "updated": datetime.fromtimestamp(row["updated"], UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "rating": row["rating"],
        "reason": row["reason"],
        "answer_id": row["answer_id"],
        "correlation_id": row["correlation_id"],
        **{key: row["context"].get(key) for key in CONTEXT},
    }


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _table(title: str, groups: dict[str, dict]) -> list[str]:
    width = max([len(title), *(len(name) + 2 for name in groups)])
    columns = ("given", "helpful", "KPI", "rated", "not helpful", "failed")
    lines = [title.ljust(width) + "".join(f"  {column:>{max(len(column), 6)}}" for column in columns)]
    for name, kpi in groups.items():
        values = (
            kpi["answers_given"],
            kpi["helpful"],
            _percent(kpi["satisfied_rate"]),
            kpi["rated"],
            kpi["not_helpful"]["total"],
            kpi["failed_without_answer"],
        )
        lines.append(
            f"  {name}".ljust(width)
            + "".join(f"  {value!s:>{max(len(column), 6)}}" for column, value in zip(columns, values, strict=True))
        )
    return lines


def render(report: dict) -> str:
    days, kpi, entries = report["period"]["days"], report["kpi"], report["ratings"]
    period = f"answers given in the last {days:g} days" if days is not None else "all recorded answers"
    reasons = kpi["not_helpful"]["by_reason"]
    lines = [
        f"Answer feedback in {report['source']}, {period}.",
        "Categories and numbers only: the question and the answer text are not shown.",
        "",
        "Initial KPI, satisfied answers = answers rated helpful / answers given",
        f"  answers given      {kpi['answers_given']}",
        f"  rated helpful      {kpi['helpful']}",
        f"  KPI                {_percent(kpi['satisfied_rate'])} ({kpi['helpful']} of {kpi['answers_given']})",
        f"  rated              {kpi['rated']} ({_percent(kpi['rated_share'])} of answers given)",
        f"  helpful of rated   {_percent(kpi['helpful_share_of_rated'])}",
        f"  not helpful        {kpi['not_helpful']['total']} ("
        + ", ".join(f"{reason.replace('_', ' ')} {count}" for reason, count in reasons.items())
        + ")",
        f"  failed, no answer  {kpi['failed_without_answer']} (not part of the KPI)",
        "",
        *(
            wrapped
            for line in report["definition"]
            for wrapped in textwrap.wrap(line, 112, initial_indent="  ", subsequent_indent="    ")
        ),
        "",
    ]
    if kpi["answers_given"] or kpi["failed_without_answer"]:
        lines += [*_table("By assistant", report["by_assistant"]), "", *_table("By domains", report["by_domains"]), ""]
    down = sum(item["rating"] == "down" for item in entries)
    lines += [f"Ratings: {len(entries)} shown ({len(entries) - down} up, {down} down), newest first.", ""]
    for item in entries:
        flow = item["flow"] or {}
        total = item["total_ms"]
        details = [
            f"status={item['status'] or '-'}",
            f"assistant={item['assistant'] or '-'}",
            f"domains={'+'.join(item['domains'] or []) or '-'}",
            f"total_ms={total:.1f}" if isinstance(total, int | float) else "total_ms=-",
            f"error={item['error_category'] or '-'}",
            f"data={item['data_source'] or '-'}",
            f"version={item['app_version'] or '-'}",
        ]
        if flow:
            details.insert(3, f"flow={flow['id']}@{flow['version']}:{flow['outcome']}/{flow['category']}")
        tools = ", ".join(f"{call['tool']}={call['result']}" for call in item["tools"] or []) or "-"
        lines += [
            f"{item['updated']}  {item['rating']}" + (f" · {item['reason']}" if item["reason"] else ""),
            "  " + "  ".join(details),
            f"  tools: {tools}",
            f"  answer={item['answer_id']}  request={item['correlation_id'] or '-'}",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev.py feedback", description="Review answer feedback, without question or answer text."
    )
    parser.add_argument(
        "--db", default=os.environ.get("SESSION_DB") or "data/sessions.sqlite3", help="session database (SESSION_DB)"
    )
    parser.add_argument(
        "--since", type=float, metavar="DAYS", help="only answers given in the last DAYS days (KPI and ratings)"
    )
    parser.add_argument(
        "--rating", choices=get_args(Rating), help="list only thumbs-up or only thumbs-down (the KPI counts both)"
    )
    parser.add_argument("--limit", type=int, default=50, help="newest ratings to list (default 50)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.since is not None and not args.since > 0:
        parser.error("--since must be a positive number of days")
    if not Path(args.db).is_file():
        print(f"No demo database at {args.db}: run the demo and rate a few answers first.")
        return 1
    since = time.time() - args.since * 86400 if args.since is not None else 0.0
    try:
        rows = read(args.db, args.rating, args.limit, since)
        requests = read_requests(args.db, since)
    except sqlite3.OperationalError:
        print(f"{args.db} has no feedback yet: start the demo once so its database is migrated.")
        return 1
    report = {
        "source": args.db,
        "period": {
            "days": args.since,
            "from": datetime.fromtimestamp(since, UTC).isoformat(timespec="seconds") if args.since else None,
        },
        "definition": list(DEFINITION),
        **kpi_report(requests),
        "ratings": [entry(row) for row in rows],
    }
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
