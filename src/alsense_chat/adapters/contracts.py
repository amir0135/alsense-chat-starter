"""Request and response shapes at the integration boundary.

These are synthetic reference contracts, not documented Alsense API schemas. A real adapter maps the Alsense API
onto them. Change them deliberately, together with `fixtures/` and the contract tests, once the owning teams
publish their contracts.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ReportFormat = Literal["pdf", "csv", "xlsx"]
Frequency = Literal["daily", "weekly", "monthly"]


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


class ScheduleSupport(Contract):
    """How a report type can be scheduled. Absent when the type is on demand only."""

    frequencies: list[Frequency]
    end_date_supported: bool
    max_recipients: int


class ReportType(Contract):
    id: str
    label: str
    formats: list[ReportFormat]
    schedule: ScheduleSupport | None = None


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
    """An on-demand report for one period."""

    site_id: str
    report_type: str
    format: ReportFormat
    start: date
    end: date


class ScheduledReportRequest(Contract):
    """A recurring report sent to recipients until it is cancelled or reaches its end date."""

    site_id: str
    report_type: str
    format: ReportFormat
    frequency: Frequency
    recipients: list[str]
    start_date: date
    end_date: date | None = None


class SupportRequest(Contract):
    site_id: str
    alarm_id: str | None = None
    summary: str
    steps_tried: str


class Submission(Contract):
    """The service's confirmation. Only after this may the assistant say something was created or scheduled."""

    id: str
    kind: Literal["report", "scheduled_report", "ticket"]
    status: Literal["accepted", "scheduled", "received"]
    url: str
    synthetic: bool
    next_run: date | None = None


class ReportJob(Contract):
    id: str
    state: Literal["processing", "completed", "failed"]
    url: str | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    message: str | None = None
    synthetic: bool


class ErrorResponse(Contract):
    """What an adapter raises for a failed upstream call: a category, never the upstream body."""

    service: Literal["identity", "sites", "knowledge", "health", "reports", "support", "model"]
    category: Literal["not_configured", "unauthenticated", "forbidden", "not_found", "unavailable", "timeout", "stale"]
    http_status: int
