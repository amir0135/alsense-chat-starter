"""Usage per user: request counts, errors, latency, model tokens and estimated cost, from the audit trail. No quota.

Each chat turn and guided-check step records, numbers only, how many Responses API calls it made (failed ones
included), how many of their responses reported usage, and the tokens reported: input, output, total, cached
input and reasoning. Requests, errors and latency come from the same audit records (event, error category and
total time per user).

The cost is an estimate, made only when the model prices are set (MODEL_PRICE_INPUT, MODEL_PRICE_CACHED_INPUT and
MODEL_PRICE_OUTPUT, in US dollars per million tokens, from the price list of the deployment's type and region).
The starter ships no prices. Cached input is part of the input tokens and reasoning part of the output tokens, so
each token is priced once. Calls that reported no usage are left out. Nothing here limits usage.

Summarize what the local demo recorded, per user (standard library only, read-only):

    uv run python scripts/dev.py usage [--db PATH] [--since DAYS] [--user ID] [--json]
        [--price-input USD] [--price-cached-input USD] [--price-output USD]
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Mapping
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
# The model prices, in US dollars per million tokens: none are shipped, so nothing is estimated until all are set.
PRICES = {
    "input": "MODEL_PRICE_INPUT",
    "cached_input": "MODEL_PRICE_CACHED_INPUT",
    "output": "MODEL_PRICE_OUTPUT",
}
PER_TOKENS = 1_000_000


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


@dataclass(frozen=True)
class Prices:
    """US dollars per million tokens, as the price list of the model deployment states them."""

    input: float
    cached_input: float
    output: float

    def estimate(self, usage: Mapping[str, int]) -> float:
        """The estimated cost in US dollars of a usage record's tokens.

        Cached input tokens are part of the input tokens, and reasoning tokens part of the output tokens: each token
        is priced once, as uncached input, cached input or output.
        """
        input_tokens = usage.get("input_tokens", 0)
        cached = min(usage.get("cached_tokens", 0), input_tokens)
        uncached = input_tokens - cached
        output = usage.get("output_tokens", 0)
        return (uncached * self.input + cached * self.cached_input + output * self.output) / PER_TOKENS


def price(name: str, value: str | None) -> float | None:
    """One price setting: empty means not set, otherwise a non-negative number of US dollars per million tokens."""
    if value is None or not value.strip():
        return None
    try:
        amount = float(value)
    except ValueError:
        amount = math.nan
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{name} must be a non-negative number of US dollars per million tokens.")
    return amount


def prices(values: Mapping[str, float | None]) -> Prices | None:
    """The prices when all are set, None when none are, and an error naming the missing ones otherwise."""
    missing = [setting for key, setting in PRICES.items() if values.get(key) is None]
    if len(missing) == len(PRICES):
        return None
    if missing:
        *first, last = PRICES.values()
        raise ValueError(f"Set {', '.join(first)} and {last} together, or none. Missing: {', '.join(missing)}.")
    return Prices(**{key: float(values[key]) for key in PRICES})  # type: ignore[arg-type]


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


def _cost(model: Mapping[str, int], rates: Prices | None) -> dict | None:
    if rates is None:
        return None
    return {
        "usd": round(rates.estimate(model), 6),
        "calls_without_usage": model.get("model_calls", 0) - model.get("calls_with_usage", 0),
    }


def summarize(rows: list[dict], rates: Prices | None = None) -> list[dict]:
    """Per user: requests by event, errors by category, latency per event, model usage and estimated cost."""
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
            "estimated_cost": _cost(user["model"], rates),
        }
        for name, user in sorted(users.items())
    ]


def total(summary: list[dict], rates: Prices | None = None) -> dict:
    """All users together: requests, errors, model usage and estimated cost."""
    model = {key: sum(user["model"][key] for user in summary) for key in KEYS}
    return {
        "requests": sum(user["requests"]["total"] for user in summary),
        "errors": sum(user["errors"]["total"] for user in summary),
        "model": model,
        "estimated_cost": _cost(model, rates),
    }


def report(rows: list[dict], rates: Prices | None, days: float | None = None) -> dict:
    users = summarize(rows, rates)
    return {
        "period_days": days,
        "prices_usd_per_million_tokens": None if rates is None else vars(rates),
        "users": users,
        "total": total(users, rates),
    }


def _estimate(cost: dict | None) -> str:
    if cost is None:
        return "not configured"
    left_out = cost["calls_without_usage"]
    note = f" ({left_out} call{'s' if left_out != 1 else ''} without reported usage left out)" if left_out else ""
    return f"${cost['usd']:,.4f}{note}"


def render(summary: dict, source: str) -> str:
    days, rates = summary["period_days"], summary["prices_usd_per_million_tokens"]
    period = f"the last {days:g} days" if days is not None else "all recorded time"
    lines = [f"Usage per user in {source}, {period}. Requests exclude the widget's timing reports."]
    if rates is None:
        *first, last = PRICES.values()
        lines.append(
            f"Estimated cost: not configured. Set {', '.join(first)} and {last} (US dollars per million tokens),"
            " or pass --price-input, --price-cached-input and --price-output."
        )
    else:
        lines += [
            f"Estimated cost at US dollars per million tokens: input {rates['input']:g}, cached input"
            f" {rates['cached_input']:g}, output {rates['output']:g}.",
            "Each token is priced once: cached input is part of the input and reasoning part of the output.",
        ]
    lines.append("")

    def counts(values: dict[str, int]) -> str:
        return " (" + ", ".join(f"{name} {count}" for name, count in values.items()) + ")" if values else ""

    for user in summary["users"]:
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
            f"  estimated cost  {_estimate(user['estimated_cost'])}",
            "",
        ]
    if not summary["users"]:
        lines.append("No requests recorded for this selection.")
        return "\n".join(lines)
    every = summary["total"]
    lines += [
        "Total",
        f"  requests  {every['requests']}  errors {every['errors']}",
        f"  model     calls {every['model']['model_calls']} (usage reported by {every['model']['calls_with_usage']})"
        f"  tokens: input {every['model']['input_tokens']}  output {every['model']['output_tokens']}",
        f"  estimated cost  {_estimate(every['estimated_cost'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev.py usage", description="Usage per user, from the audit trail.")
    parser.add_argument(
        "--db", default=os.environ.get("SESSION_DB") or "data/sessions.sqlite3", help="session database (SESSION_DB)"
    )
    parser.add_argument("--since", type=float, metavar="DAYS", help="only the last DAYS days")
    parser.add_argument("--user", help="only this user id")
    for key, setting in PRICES.items():
        parser.add_argument(
            f"--price-{key.replace('_', '-')}",
            metavar="USD",
            help=f"US dollars per million {key.replace('_', ' ')} tokens, instead of {setting}",
        )
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if args.since is not None and not args.since > 0:
        parser.error("--since must be a positive number of days")
    try:
        rates = prices(
            {
                key: price(f"--price-{key.replace('_', '-')}", given)
                if (given := getattr(args, f"price_{key}")) is not None
                else price(setting, os.environ.get(setting))
                for key, setting in PRICES.items()
            }
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not Path(args.db).is_file():
        print(f"No demo database at {args.db}: run the demo and ask a few questions first.")
        return 1
    since = time.time() - args.since * 86400 if args.since is not None else 0.0
    try:
        rows = read(args.db, since, args.user)
    except sqlite3.OperationalError:
        print(f"{args.db} has no usage records yet: start the demo once so its database is migrated.")
        return 1
    summary = report(rows, rates, args.since)
    print(json.dumps(summary, indent=2) if args.json else render(summary, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
