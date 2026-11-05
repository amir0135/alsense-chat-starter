"""Usage per user: request counts, errors, latency and model tokens, from the audit trail. No cost and no quota.

Each chat turn and guided-check step records, numbers only, how many Responses API calls it made (failed ones
included), how many of their responses reported usage, and the tokens reported: input, output, total, cached
input and reasoning. Requests, errors and latency come from the same audit records (event, error category and
total time per user).

Cost is not calculated: the starter has no approved price source. Cost = tokens x the approved rate for the model
deployment, from a source the owners choose. Nothing here limits usage.

Summarize what the local demo recorded, per user (standard library only, read-only):

    uv run python scripts/dev.py usage [--db PATH] [--since DAYS] [--user ID] [--json]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKENS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
KEYS = ("model_calls", "calls_with_usage", *TOKENS)
# Where each count sits in the Responses API usage object: at the top or in a details object.
PLACES = {
    "input_tokens": None,
    "output_tokens": None,
    "total_tokens": None,
    "cached_tokens": "input_tokens_details",
    "reasoning_tokens": "output_tokens_details",
}


def _field(source: Any, name: str) -> Any:
    if source is None:
        return None
    return source.get(name) if isinstance(source, dict) else getattr(source, name, None)


@dataclass
class Usage:
    """Model calls and the tokens their responses reported, summed over one request."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KEYS, 0))

    def call(self) -> None:
        self.counts["model_calls"] += 1

    def add(self, usage: Any) -> dict[str, int]:
        """Add one response's usage and return the counts it reported. Missing or invalid counts are skipped."""
        found = {}
        for key, parent in PLACES.items():
            value = _field(_field(usage, parent) if parent else usage, key)
            if type(value) is int and value >= 0:
                found[key] = value
        if found:
            self.counts["calls_with_usage"] += 1
            for key, value in found.items():
                self.counts[key] += value
        return found

    def record(self) -> dict[str, int]:
        return dict(self.counts)


def valid_usage(usage: Any) -> bool:
    """A usage record holds non-negative whole numbers for known keys only."""
    return (
        isinstance(usage, dict)
        and set(usage) <= set(KEYS)
        and all(type(value) is int and value >= 0 for value in usage.values())
    )


def percentile(values: list[float], share: float) -> float:
    """Nearest rank: the smallest recorded value with at least `share` of all values at or below it."""
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)), 1) - 1]


def read(path: str, since: float, user: str | None) -> list[dict]:
    """Audit records since a time, without the widget's timing reports. Read-only."""
    query = (
        "SELECT persona, event, error_category, timings, usage FROM audit "
        "WHERE event != 'client_timing' AND created >= ?" + (" AND persona = ?" if user else "")
    )
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(query, (since, user) if user else (since,)).fetchall()
    return [{**dict(row), "timings": json.loads(row["timings"]), "usage": json.loads(row["usage"])} for row in rows]


def summarize(rows: list[dict]) -> list[dict]:
    """Per user: requests by event, errors by category, latency per event and model usage."""
    users: dict[str, dict] = {}
    for row in rows:
        user = users.setdefault(
            row["persona"] or "(no user)",
            {"requests": Counter(), "errors": Counter(), "latency": {}, "model": dict.fromkeys(KEYS, 0)},
        )
        user["requests"][row["event"]] += 1
        if row["error_category"]:
            user["errors"][row["error_category"]] += 1
        total = row["timings"].get("ms", {}).get("total")
        if isinstance(total, int | float):
            user["latency"].setdefault(row["event"], []).append(total)
        for key, value in row["usage"].items():
            if key in user["model"] and type(value) is int:
                user["model"][key] += value
    return [
        {
            "user": name,
            "requests": {"total": sum(user["requests"].values()), "by_event": dict(sorted(user["requests"].items()))},
            "errors": {"total": sum(user["errors"].values()), "by_category": dict(sorted(user["errors"].items()))},
            "latency_ms": {
                event: {
                    "n": len(values),
                    "p50": round(percentile(values, 0.5), 1),
                    "p95": round(percentile(values, 0.95), 1),
                }
                for event, values in sorted(user["latency"].items())
            },
            "model": user["model"],
            "cost": None,
        }
        for name, user in sorted(users.items())
    ]


def render(summary: list[dict], source: str, days: float | None) -> str:
    period = f"the last {days:g} days" if days is not None else "all recorded time"
    lines = [
        f"Usage per user in {source}, {period}. Requests exclude the widget's timing reports.",
        "Cost: not available. No approved price source; cost = tokens x the approved rate for the deployment.",
        "",
    ]

    def counts(values: dict[str, int]) -> str:
        return " (" + ", ".join(f"{name} {count}" for name, count in values.items()) + ")" if values else ""

    for user in summary:
        model = user["model"]
        latency = ", ".join(
            f"{event} {values['p50']:g}/{values['p95']:g} (n={values['n']})"
            for event, values in user["latency_ms"].items()
        )
        tokens = "  ".join(f"{key.removesuffix('_tokens')} {model[key]}" for key in TOKENS)
        calls = f"calls {model['model_calls']} (usage reported by {model['calls_with_usage']})"
        lines += [
            user["user"],
            f"  requests  {user['requests']['total']}{counts(user['requests']['by_event'])}",
            f"  errors    {user['errors']['total']}{counts(user['errors']['by_category'])}",
            f"  latency   ms p50/p95: {latency or '-'}",
            f"  model     {calls}  tokens: {tokens}",
            "",
        ]
    if not summary:
        lines.append("No requests recorded for this selection.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev.py usage", description="Usage per user, from the audit trail.")
    parser.add_argument(
        "--db", default=os.environ.get("SESSION_DB") or "data/sessions.sqlite3", help="session database (SESSION_DB)"
    )
    parser.add_argument("--since", type=float, metavar="DAYS", help="only the last DAYS days")
    parser.add_argument("--user", help="only this user id")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if args.since is not None and not args.since > 0:
        parser.error("--since must be a positive number of days")
    if not Path(args.db).is_file():
        print(f"No demo database at {args.db}: run the demo and ask a few questions first.")
        return 1
    since = time.time() - args.since * 86400 if args.since is not None else 0.0
    try:
        rows = read(args.db, since, args.user)
    except sqlite3.OperationalError:
        print(f"{args.db} has no usage records yet: start the demo once so its database is migrated.")
        return 1
    summary = summarize(rows)
    print(json.dumps(summary, indent=2) if args.json else render(summary, args.db, args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
