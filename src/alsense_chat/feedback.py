"""Answer feedback: a thumbs-up or thumbs-down on a completed answer, with what is needed to investigate it.

A rating is stored against the answer's id, one row per answer (a later rating replaces it), with an optional
reason from a fixed list and a snapshot of what produced the answer: reply status, routed domains, guided-check
id, version and outcome, tools with their result categories, error category, assistant, data source, app version
and total latency. Never the question, the answer text, its sources, guided-check answers or free text. The
conversation stays in the session database's turns, linked by the answer id and the correlation id.

Review what the local demo recorded, newest first (standard library only, read-only):

    uv run python scripts/dev.py feedback [--db PATH] [--rating up|down] [--limit N] [--json]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
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


def read(path: str, rating: str | None, limit: int) -> list[dict]:
    """The newest feedback records. Read-only: the review never migrates or writes."""
    query = "SELECT answer_id, correlation_id, rating, reason, context, created, updated FROM feedback"
    query += (" WHERE rating=?" if rating else "") + " ORDER BY updated DESC, answer_id LIMIT ?"
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(query, (rating, limit) if rating else (limit,)).fetchall()
    return [{**dict(row), "context": json.loads(row["context"])} for row in rows]


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


def render(entries: list[dict], source: str) -> str:
    down = sum(item["rating"] == "down" for item in entries)
    lines = [
        f"Feedback in {source}: {len(entries)} shown ({len(entries) - down} up, {down} down), newest first.",
        "Categories and numbers only: the question and the answer text are not shown.",
        "",
    ]
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
    parser.add_argument("--rating", choices=get_args(Rating), help="only thumbs-up or only thumbs-down")
    parser.add_argument("--limit", type=int, default=50, help="newest records to show (default 50)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if not Path(args.db).is_file():
        print(f"No demo database at {args.db}: run the demo and rate a few answers first.")
        return 1
    try:
        rows = read(args.db, args.rating, args.limit)
    except sqlite3.OperationalError:
        print(f"{args.db} has no feedback yet: start the demo once so its database is migrated.")
        return 1
    entries = [entry(row) for row in rows]
    print(json.dumps(entries, indent=2) if args.json else render(entries, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
