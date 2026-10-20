"""Synthetic services. Every name, customer, site, alarm, document and result here is invented."""

from datetime import UTC, datetime, timedelta

from ..contracts import (
    Alarm,
    Document,
    HealthCheck,
    HealthReport,
    ReportJob,
    ReportRequest,
    ReportType,
    Site,
    Submission,
    SupportRequest,
    User,
)
from ..errors import Forbidden

PERSONAS: dict[str, dict] = {
    "alice": {"tenant": "customer-a", "name": "Synthetic Alice", "internal": True, "sites": [("site-a", "Demo North")]},
    "bob": {"tenant": "customer-b", "name": "Synthetic Bob", "internal": True, "sites": [("site-b", "Demo South")]},
    "carol": {
        "tenant": "customer-c",
        "name": "Synthetic Carol",
        "internal": True,
        "sites": [("site-c1", "Demo East Hall"), ("site-c2", "Demo East Depot")],
    },
    "external": {
        "tenant": "customer-a",
        "name": "Synthetic Guest",
        "internal": False,
        "sites": [("site-a", "Demo North")],
    },
}

DOCUMENTS = [
    Document(
        id="doc-operations",
        title="Synthetic alarm and report guide",
        excerpt="For an offline sensor, check power and connectivity. Review the alarm history report. "
        "If the issue remains, prepare a support ticket with the steps already tried.",
        url="/reference/operations",
    ),
    Document(
        id="doc-reports",
        title="Synthetic report preparation guide",
        excerpt="Choose a permitted site, report type, format and date range. Preparing a report does not "
        "submit it. Review the prepared form before using Submit.",
        url="/reference/reports",
    ),
]

REPORT_TYPES = [
    ReportType(id="alarm-history", label="Alarm history", formats=["pdf", "csv"]),
    ReportType(id="site-status", label="Site status", formats=["pdf", "csv"]),
]

# Demo North and Demo East Hall have an offline gateway; the other sites have no open health checks.
OPEN_CHECKS = {
    "site-a": [("primary-gateway-offline", "Primary Gateway Offline")],
    "site-c1": [("secondary-gateway-offline", "Secondary Gateway Offline")],
}


def permitted(user: User) -> set[str]:
    return {site.id for site in user.sites if site.tenant_id == user.tenant_id}


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class SyntheticIdentity:
    def users_me(self, identity: str) -> User:
        persona = PERSONAS[identity]
        tenant = persona["tenant"]
        return User(
            id=identity,
            tenant_id=tenant,
            name=persona["name"],
            internal=persona["internal"],
            sites=[Site(id=site_id, name=name, tenant_id=tenant) for site_id, name in persona["sites"]],
        )

    def personas(self) -> list[dict]:
        return [
            {"id": key, "name": value["name"], "sites": [name for _, name in value["sites"]]}
            for key, value in PERSONAS.items()
            if value["internal"]
        ]


class SyntheticSiteDirectory:
    def search_sites(self, user: User, query: str) -> list[Site]:
        folded = query.strip().casefold()
        allowed = permitted(user)
        return [
            site
            for site in user.sites
            if site.id in allowed and (folded == "*" or folded in site.name.casefold() or folded == site.id)
        ]

    def active_alarms(self, user: User, site_id: str) -> list[Alarm]:
        if site_id not in permitted(user):
            return []
        return [Alarm(id=f"alarm-{site_id}", label="Sensor offline", status="active")]


class SyntheticKnowledge:
    def search(self, user: User, query: str) -> list[Document]:
        folded = query.casefold()
        if not any(term in folded for term in ("alarm", "report", "offline", "sensor")):
            return []
        return DOCUMENTS[:1] if "offline" in folded or "sensor" in folded else list(DOCUMENTS)


class SyntheticHealth:
    def __init__(self, clock=now):
        self.clock = clock

    def site_health(self, user: User, site_id: str) -> HealthReport:
        if site_id not in permitted(user):
            raise Forbidden("health")
        checks = [
            HealthCheck(id=f"hc-{site_id}-{kind}", type=kind, check=label, status="open")
            for kind, label in OPEN_CHECKS.get(site_id, [])
        ]
        return HealthReport(site_id=site_id, observed_at=self.clock() - timedelta(minutes=2), checks=checks)


class SyntheticReports:
    def report_types(self, user: User) -> list[ReportType]:
        return list(REPORT_TYPES)

    def submit_report(self, user: User, request: ReportRequest, idempotency_key: str) -> Submission:
        if request.site_id not in permitted(user):
            raise Forbidden("reports")
        return Submission(
            id=idempotency_key,
            kind="report",
            status="accepted",
            url=f"/reference/report/{idempotency_key}",
            synthetic=True,
        )

    def report_status(self, user: User, job_id: str) -> ReportJob:
        return ReportJob(id=job_id, state="completed", url=f"/reference/report/{job_id}", synthetic=True)


class SyntheticSupport:
    def submit_request(self, user: User, request: SupportRequest, idempotency_key: str) -> Submission:
        if request.site_id not in permitted(user):
            raise Forbidden("support")
        return Submission(
            id=idempotency_key,
            kind="ticket",
            status="received",
            url=f"/reference/ticket/{idempotency_key}",
            synthetic=True,
        )
