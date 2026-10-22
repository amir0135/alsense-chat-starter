"""Contract fixtures: one example per integration call, generated from the synthetic adapters.

Each fixture is what an adapter method returns (or the categorized error it raises) for a named case. Compare
them with the real Alsense responses when the contracts arrive. Regenerate after changing a contract or the
synthetic data:

    uv run python scripts/dev.py fixtures

tests/test_contracts.py fails when the committed files drift from what this script produces.
"""

import dataclasses
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from alsense_chat.adapters import contracts, synthetic
from alsense_chat.adapters.contracts import (
    Alarm,
    Document,
    ErrorResponse,
    HealthReport,
    ReportJob,
    ReportRequest,
    ReportType,
    ScheduledReportRequest,
    Site,
    Submission,
    SupportRequest,
    User,
)
from alsense_chat.adapters.errors import Forbidden, IntegrationError
from alsense_chat.adapters.synthetic.services import SyntheticHealth, SyntheticReports

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
SCHEMAS = FIXTURES / "schemas"
OBSERVED = datetime(2026, 1, 15, 8, 58, tzinfo=UTC)
KEY = "example-idempotency-key"
CONTRACTS = [
    User,
    Site,
    Alarm,
    Document,
    ReportType,
    HealthReport,
    ReportRequest,
    ScheduledReportRequest,
    SupportRequest,
    Submission,
    ReportJob,
    ErrorResponse,
]


def _error(call: Callable[[], Any]) -> ErrorResponse:
    try:
        call()
    except IntegrationError as exc:
        return ErrorResponse(service=exc.service, category=exc.category, http_status=exc.http_status)  # type: ignore[arg-type]
    raise AssertionError("Expected an integration error.")


def _denied() -> None:
    raise Forbidden("sites", "That site isn't one of your permitted sites.")


def cases() -> dict[str, tuple[Any, Callable[[], Any]]]:
    """Fixture path -> (contract type, producer)."""
    services = dataclasses.replace(synthetic.build(), reports=SyntheticReports(clock=lambda: OBSERVED))
    health = SyntheticHealth(clock=lambda: OBSERVED + timedelta(minutes=2))
    alice, bob, carol = (services.identity.users_me(name) for name in ("alice", "bob", "carol"))

    def faulty(*names: str):
        return synthetic.with_faults(services, frozenset(names))

    report = ReportRequest(
        site_id="site-a", report_type="alarm-history", format="pdf", start=date(2026, 1, 1), end=date(2026, 1, 31)
    )
    schedule = ScheduledReportRequest(
        site_id="site-a",
        report_type="temperature-log",
        format="xlsx",
        frequency="weekly",
        recipients=["operations@example.com", "quality@example.com"],
        start_date=date(2026, 2, 2),
        end_date=date(2026, 6, 29),
    )
    ticket = SupportRequest(
        site_id="site-b",
        summary="Site seems disconnected",
        steps_tried="Guided check: Site seems disconnected\n1. Recent readings visible: No\n2. Site: Demo South",
    )
    return {
        "identity/users-me.json": (User, lambda: alice),
        "sites/permitted.json": (list[Site], lambda: services.sites.permitted_sites(alice)),
        "sites/permitted-similar-names.json": (list[Site], lambda: services.sites.permitted_sites(carol)),
        "sites/unavailable.json": (
            ErrorResponse,
            lambda: _error(lambda: faulty("sites_unavailable").sites.permitted_sites(alice)),
        ),
        "sites/alarms-active.json": (list[Alarm], lambda: services.sites.active_alarms(alice, "site-a")),
        "sites/forbidden.json": (ErrorResponse, lambda: _error(_denied)),
        "health/unreachable.json": (HealthReport, lambda: health.site_health(alice, "site-a")),
        "health/reachable.json": (HealthReport, lambda: health.site_health(bob, "site-b")),
        "health/unavailable.json": (
            ErrorResponse,
            lambda: _error(lambda: faulty("health_unavailable").health.site_health(alice, "site-a")),
        ),
        "health/timeout.json": (
            ErrorResponse,
            lambda: _error(lambda: faulty("api_timeout").health.site_health(alice, "site-a")),
        ),
        "knowledge/search-results.json": (list[Document], lambda: services.knowledge.search(alice, "offline sensor")),
        "knowledge/no-approved-answer.json": (list[Document], lambda: services.knowledge.search(bob, "compressor")),
        "reports/report-types.json": (list[ReportType], lambda: services.reports.report_types(alice)),
        "reports/request.json": (ReportRequest, lambda: report),
        "reports/submitted.json": (Submission, lambda: services.reports.submit_report(alice, report, KEY)),
        "reports/processing.json": (ReportJob, lambda: faulty("report_processing").reports.report_status(alice, KEY)),
        "reports/completed.json": (ReportJob, lambda: services.reports.report_status(alice, KEY)),
        "reports/failed.json": (ReportJob, lambda: faulty("report_failed").reports.report_status(alice, KEY)),
        "reports/schedule-request.json": (ScheduledReportRequest, lambda: schedule),
        "reports/scheduled.json": (Submission, lambda: services.reports.schedule_report(alice, schedule, KEY)),
        "support/request.json": (SupportRequest, lambda: ticket),
        "support/received.json": (Submission, lambda: services.support.submit_request(bob, ticket, KEY)),
        "support/unavailable.json": (
            ErrorResponse,
            lambda: _error(lambda: faulty("support_unavailable").support.submit_request(bob, ticket, KEY)),
        ),
    }


def render(contract: Any, value: Any) -> str:
    data = TypeAdapter(contract).dump_python(value, mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def schema_files() -> dict[str, str]:
    files = {}
    for model in CONTRACTS:
        assert issubclass(model, BaseModel) and model.__module__ == contracts.__name__
        files[f"schemas/{model.__name__}.schema.json"] = json.dumps(model.model_json_schema(), indent=2) + "\n"
    return files


def generate() -> dict[str, str]:
    files = {path: render(contract, produce()) for path, (contract, produce) in cases().items()}
    return {**files, **schema_files()}


def write() -> list[str]:
    changed = []
    for relative, content in generate().items():
        path = FIXTURES / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed.append(relative)
    return changed


if __name__ == "__main__":
    updated = write()
    print("\n".join(f"updated fixtures/{name}" for name in updated) or "Fixtures are up to date.")
    sys.exit(0)
