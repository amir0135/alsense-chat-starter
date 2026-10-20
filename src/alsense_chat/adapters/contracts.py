"""Request and response shapes at the integration boundary.

These are synthetic reference contracts, not documented Alsense API schemas. A real adapter maps the Alsense API
onto them. Change them deliberately, together with `fixtures/` and the contract tests, once the owning teams
publish their contracts.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Site(Contract):
    id: str
    name: str
    tenant_id: str


class User(Contract):
    id: str
    tenant_id: str
    name: str
    internal: bool
    sites: list[Site]


class Alarm(Contract):
    id: str
    label: str
    status: Literal["active", "cleared"]


class Document(Contract):
    id: str
    title: str
    excerpt: str
    url: str


class ReportType(Contract):
    id: str
    label: str
    formats: list[Literal["pdf", "csv"]]


class HealthCheck(Contract):
    id: str
    type: str
    check: str
    status: Literal["open", "closed"]


class HealthReport(Contract):
    site_id: str
    observed_at: datetime
    checks: list[HealthCheck]


class ReportRequest(Contract):
    site_id: str
    report_type: str
    format: Literal["pdf", "csv"]
    start: date
    end: date


class SupportRequest(Contract):
    site_id: str
    alarm_id: str | None = None
    summary: str
    steps_tried: str


class Submission(Contract):
    id: str
    kind: Literal["report", "ticket"]
    status: Literal["accepted", "received"]
    url: str
    synthetic: bool


class ReportJob(Contract):
    id: str
    state: Literal["processing", "completed", "failed"]
    url: str | None = None
    message: str | None = None
    synthetic: bool


class ErrorResponse(Contract):
    """What an adapter raises for a failed upstream call: a category, never the upstream body."""

    service: Literal["identity", "sites", "knowledge", "health", "reports", "support", "model"]
    category: Literal["not_configured", "unauthenticated", "forbidden", "not_found", "unavailable", "timeout", "stale"]
    http_status: int
