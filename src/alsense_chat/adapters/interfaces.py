"""The services the backend needs from the outside world.

Conversation, flow and UI code depend only on these interfaces. Swap an implementation in
`alsense_chat.adapters.build_services` without touching them. Every method receives the session's `User` so that
an implementation can enforce permissions; a real adapter also receives a request-scoped delegated credential
through its own constructor, never through these arguments.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .contracts import (
    Alarm,
    Document,
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


class IdentityProvider(Protocol):
    def users_me(self, identity: str) -> User:
        """Return the signed-in user with tenant and permitted sites (`users/me`)."""
        ...


@runtime_checkable
class PersonaDirectory(Protocol):
    """Demo-only: the synthetic identities offered on the sign-in panel."""

    def personas(self) -> list[dict]: ...


class SiteDirectory(Protocol):
    def permitted_sites(self, user: User) -> list[Site]:
        """Every site the signed-in user may access, with stable ids and display names.

        Matching a friendly name to these sites happens in the backend (`domains/sites.py`), so the model never
        guesses a site id and sites without access are never returned.
        """
        ...

    def active_alarms(self, user: User, site_id: str) -> list[Alarm]: ...


class KnowledgeSearch(Protocol):
    def search(self, user: User, query: str) -> list[Document]:
        """Approved, permission-filtered guidance excerpts with approved links."""
        ...


class HealthCheckService(Protocol):
    def site_health(self, user: User, site_id: str) -> HealthReport:
        """Health checks for one permitted site, with the time they were observed."""
        ...


class ReportService(Protocol):
    def report_types(self, user: User) -> list[ReportType]: ...

    def submit_report(self, user: User, request: ReportRequest, idempotency_key: str) -> Submission:
        """Start an on-demand report job after the user confirmed the draft."""
        ...

    def schedule_report(self, user: User, request: ScheduledReportRequest, idempotency_key: str) -> Submission:
        """Create a recurring report after the user confirmed the draft."""
        ...

    def report_status(self, user: User, job_id: str) -> ReportJob:
        """Job state; a completed job has a download link and the time the link expires."""
        ...


class SupportService(Protocol):
    def submit_request(self, user: User, request: SupportRequest, idempotency_key: str) -> Submission: ...


@dataclass(frozen=True)
class Services:
    kind: Literal["synthetic", "alsense"]
    identity: IdentityProvider
    sites: SiteDirectory
    knowledge: KnowledgeSearch
    health: HealthCheckService
    reports: ReportService
    support: SupportService
