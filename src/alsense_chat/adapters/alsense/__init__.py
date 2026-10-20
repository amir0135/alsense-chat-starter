"""Placeholders for the real Alsense integration.

Each class names the interface it must implement and raises `NotConfiguredError` until it is written. Use
docs/integration-map.md for what each method needs from Alsense, and the synthetic adapters plus `fixtures/` for
the expected shapes. Keep delegated credentials inside these adapters (request-scoped), never in tool arguments.
"""

from ..contracts import (
    Alarm,
    Document,
    HealthReport,
    ReportJob,
    ReportRequest,
    ReportType,
    Site,
    Submission,
    SupportRequest,
    User,
)
from ..errors import NotConfiguredError
from ..interfaces import Services

# Flip to True once every adapter below calls the real APIs and its tests pass.
IMPLEMENTED = False
PLACEHOLDER_MESSAGE = (
    "DATA_SOURCE=alsense selects placeholder adapters in src/alsense_chat/adapters/alsense that are not "
    "implemented yet. Implement them (see docs/integration-map.md) or use DATA_SOURCE=synthetic."
)


class AlsenseIdentity:
    """IdentityProvider: validate the user's token, then call `users/me` for tenant and permitted sites."""

    def users_me(self, identity: str) -> User:
        raise NotConfiguredError("identity")


class AlsenseSiteDirectory:
    """SiteDirectory: search the user's permitted sites; return stable ids, display names and active alarms."""

    def search_sites(self, user: User, query: str) -> list[Site]:
        raise NotConfiguredError("sites")

    def active_alarms(self, user: User, site_id: str) -> list[Alarm]:
        raise NotConfiguredError("sites")


class AlsenseKnowledge:
    """KnowledgeSearch: query the approved Azure AI Search index with document-level permissions."""

    def search(self, user: User, query: str) -> list[Document]:
        raise NotConfiguredError("knowledge")


class AlsenseHealth:
    """HealthCheckService: open health checks for one permitted site, with the observation time."""

    def site_health(self, user: User, site_id: str) -> HealthReport:
        raise NotConfiguredError("health")


class AlsenseReports:
    """ReportService: report metadata, idempotent submission and job status."""

    def report_types(self, user: User) -> list[ReportType]:
        raise NotConfiguredError("reports")

    def submit_report(self, user: User, request: ReportRequest, idempotency_key: str) -> Submission:
        raise NotConfiguredError("reports")

    def report_status(self, user: User, job_id: str) -> ReportJob:
        raise NotConfiguredError("reports")


class AlsenseSupport:
    """SupportService: create a ticket through the approved support workflow, honoring the idempotency key."""

    def submit_request(self, user: User, request: SupportRequest, idempotency_key: str) -> Submission:
        raise NotConfiguredError("support")


def build() -> Services:
    if not IMPLEMENTED:
        raise NotConfiguredError("identity", PLACEHOLDER_MESSAGE)
    return Services(
        kind="alsense",
        identity=AlsenseIdentity(),
        sites=AlsenseSiteDirectory(),
        knowledge=AlsenseKnowledge(),
        health=AlsenseHealth(),
        reports=AlsenseReports(),
        support=AlsenseSupport(),
    )
