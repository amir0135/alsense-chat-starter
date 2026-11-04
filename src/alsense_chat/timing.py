"""Request timings per stage: where the time of a chat turn, guided check, confirmation or status read goes.

Each stage is measured with a monotonic clock and summed per request, with how often it ran (a model round or
tool call can repeat). The sums go to the request's audit record, numbers only. With Application Insights
configured, every stage is also an OpenTelemetry span (`alsense.<stage>`) whose attributes are limited to the
correlation id, stage, tool name, result category and model round kind: never message text, tool arguments or
results, tokens or credentials. An exception marks its span as failed with the exception's type name only.

Summarize what the local demo recorded, per event, assistant and routed domains:

    uv run python scripts/dev.py latency [--db PATH] [--limit N]
"""

import argparse
import functools
import json
import math
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .agent import load_agent
from .domains import reporting

TRACER = trace.get_tracer("alsense_chat")
# Stages that run one after another within a request. Whatever they leave of the total is shown as "(other)":
# request handling, the scripted planner's own parsing and assembling the reply.
SEQUENTIAL = (
    "session",
    "history",
    "route",
    "model_tool_selection",
    "model_answer",
    "model_failed",
    "tools",
    "guard",
    "persist",
)
# Offsets from the moment the request arrived, not durations: the first streamed event (a progress step or
# the reply), the reply and the end of the request.
OFFSETS = ("first_event", "reply", "total")
# The widget's own offsets from sending the question, reported to POST /api/timings.
CLIENT = ("headers", "first_event", "reply", "rendered")
ORDER = (*SEQUENTIAL, "(other)", *OFFSETS)
# Confirmations are timed per action kind (tool.submit_report); undeclared tool names as tool.unknown.
ACTION_KINDS = (*reporting.KINDS, "ticket")


@functools.cache
def known_stages() -> frozenset[str]:
    """Every stage a timings record may name: fixed stages, one per declared tool and one per action kind."""
    tools = {*load_agent().tools(), "unknown", *(f"submit_{kind}" for kind in ACTION_KINDS)}
    return frozenset({*SEQUENTIAL, *OFFSETS, *CLIENT, *(f"tool.{name}" for name in tools)})


def valid_record(timings: Any) -> bool:
    """A timings record holds finite, non-negative milliseconds and run counts for known stages, nothing else."""
    if not isinstance(timings, dict) or set(timings) - {"ms", "count"}:
        return False
    ms, count = timings.get("ms", {}), timings.get("count", {})
    return (
        isinstance(ms, dict)
        and isinstance(count, dict)
        and set(ms) == set(count)
        and set(ms) <= known_stages()
        and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in ms.values())
        and all(type(value) is int and value >= 1 for value in count.values())
    )


class Stage:
    """A stage being measured. The caller can name its keys and add a result once the outcome is known."""

    def __init__(self, keys: tuple[str, ...], attributes: dict[str, str]):
        self.keys = keys
        self.attributes = attributes


@dataclass
class Timings:
    correlation_id: str = ""
    started: float = field(default_factory=time.perf_counter)
    ms: dict[str, float] = field(default_factory=dict)
    count: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, ms: float) -> None:
        self.ms[key] = self.ms.get(key, 0.0) + ms
        self.count[key] = self.count.get(key, 0) + 1

    @contextmanager
    def stage(self, name: str, *keys: str, **attributes: str) -> Iterator[Stage]:
        """Time a block as the span alsense.<name> and add its duration to each key (default: the name).

        Never wrap a `yield` of the chat stream in a stage: the next frame may resume in another context.
        """
        current = Stage(keys or (name,), dict(attributes))
        with TRACER.start_as_current_span(
            f"alsense.{name}", record_exception=False, set_status_on_exception=False
        ) as span:
            begin = time.perf_counter()
            try:
                yield current
            except BaseException as exc:
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                raise
            finally:
                elapsed = (time.perf_counter() - begin) * 1000
                for key in current.keys:
                    self.add(key, elapsed)
                span.set_attributes(
                    {
                        "alsense.correlation_id": self.correlation_id,
                        "alsense.stage": name,
                        **{f"alsense.{key}": value for key, value in current.attributes.items()},
                    }
                )

    def mark(self, key: str) -> None:
        """Record the time since the request arrived, the first time only (first_event, total)."""
        if key not in self.ms:
            self.add(key, (time.perf_counter() - self.started) * 1000)

    def record(self) -> dict:
        """Milliseconds and run counts per stage, for the audit record."""
        return {"ms": {key: round(value, 2) for key, value in self.ms.items()}, "count": dict(self.count)}


def percentile(values: list[float], share: float) -> float:
    """Nearest rank: the smallest recorded value with at least `share` of all values at or below it."""
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)), 1) - 1]


def read(path: str, limit: int) -> list[dict]:
    """The latest timed audit records, oldest first. Read-only: the report never migrates or writes."""
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT session_id, correlation_id, event, assistant, domains, timings FROM audit "
            "WHERE timings != '{}' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {**dict(row), "domains": json.loads(row["domains"]), "timings": json.loads(row["timings"])}
        for row in reversed(rows)
    ]


def summarize(records: list[dict]) -> dict[str, dict[str, list[tuple[float, int]]]]:
    """Per group (event · assistant · domains), each stage's (milliseconds, runs) per request.

    Client timings join the latest request of their session with the same correlation id; records are oldest first.
    """
    groups: dict[str, dict[str, list[tuple[float, int]]]] = {}
    requests: dict[tuple[str, str], str] = {}
    for record in records:
        ms, count = record["timings"].get("ms", {}), record["timings"].get("count", {})
        request = (record["session_id"] or "", record["correlation_id"])
        if record["event"] == "client_timing":
            label = requests.get(request, "")
            stages = {f"client.{key}": (value, 1) for key, value in ms.items()}
        else:
            parts = (record["event"], record["assistant"] or "", "+".join(record["domains"]))
            label = requests[request] = " · ".join(part for part in parts if part)
            stages = {key: (value, count.get(key, 1)) for key, value in ms.items()}
            if "total" in ms:
                stages["(other)"] = (max(ms["total"] - sum(ms.get(key, 0) for key in SEQUENTIAL), 0.0), 1)
        if label:
            group = groups.setdefault(label, {})
            for key, sample in stages.items():
                group.setdefault(key, []).append(sample)
    return groups


def _rank(key: str) -> tuple[int, int, str]:
    if key.startswith("tool."):
        return ORDER.index("tools"), 1, key
    if key.startswith("client."):
        name = key.removeprefix("client.")
        return len(ORDER) + 1, CLIENT.index(name) if name in CLIENT else len(CLIENT), key
    return (ORDER.index(key), 0, key) if key in ORDER else (len(ORDER), 0, key)


def report(records: list[dict], source: str) -> str:
    lines = [
        f"Milliseconds per stage from {len(records)} timed audit records in {source} (nearest-rank percentiles).",
        "first_event, reply, total and client.* are offsets from the start; the other stages are durations.",
        "Local measurements describe this machine, data source and assistant mode only.",
        "",
    ]
    for label, stages in sorted(summarize(records).items()):
        requests = len(stages.get("total", []))
        lines.append(f"{label} ({requests} request{'' if requests == 1 else 's'})")
        lines.append(f"  {'stage':<32}{'n':>5}{'runs':>6}{'p50':>10}{'p95':>10}{'max':>10}")
        for key in sorted(stages, key=_rank):
            values = [ms for ms, _ in stages[key]]
            runs = sum(count for _, count in stages[key]) / len(values)
            lines.append(
                f"  {key:<32}{len(values):>5}{runs:>6.1f}"
                f"{percentile(values, 0.5):>10.1f}{percentile(values, 0.95):>10.1f}{max(values):>10.1f}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev.py latency", description="Summarize recorded request timings.")
    parser.add_argument(
        "--db", default=os.environ.get("SESSION_DB") or "data/sessions.sqlite3", help="session database (SESSION_DB)"
    )
    parser.add_argument("--limit", type=int, default=1000, help="latest timed records to include (default 1000)")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if not Path(args.db).is_file():
        print(f"No demo database at {args.db}: run the demo and ask a few questions first.")
        return 1
    try:
        records = read(args.db, args.limit)
    except sqlite3.OperationalError:
        print(f"{args.db} has no timings yet: start the demo once so its database is migrated.")
        return 1
    if not records:
        print(f"No timed requests in {args.db} yet: ask a few questions in the demo first.")
        return 1
    print(report(records, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
