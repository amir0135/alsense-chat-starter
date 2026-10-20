from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Site(BaseModel):
    id: str
    name: str
    tenant_id: str


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    name: str
    internal: bool
    sites: list[Site]


class Document(BaseModel):
    id: str
    title: str
    excerpt: str
    url: str


class Alarm(BaseModel):
    id: str
    label: str
    status: str


class ReportType(BaseModel):
    id: str
    label: str
    formats: list[str]


class HealthCheck(BaseModel):
    id: str
    type: str
    check: str
    status: str


class AlsenseSource(Protocol):
    """API adapter boundary. A real implementation must validate users/me and use delegated auth.

    The credential belongs only in the adapter's request context, never in tool arguments/results.
    These reference schemas are synthetic contracts, not documented Alsense API schemas.
    """

    def users_me(self, identity: str) -> User: ...
    def report_types(self, user: User) -> list[ReportType]: ...
    def search_docs(self, user: User, query: str) -> list[Document]: ...
    def site_alarms(self, user: User, site_id: str) -> list[Alarm]: ...
    def site_health_checks(self, user: User, site_id: str) -> list[HealthCheck]: ...
    def alarm_exists(self, user: User, site_id: str, alarm_id: str) -> bool: ...
    def submit(self, user: User, kind: str, payload: dict, idempotency_key: str) -> dict: ...


class MockAlsenseSource:
    """All names, customers, sites and records below are synthetic."""

    def users_me(self, identity: str) -> User:
        personas = {
            "alice": ("customer-a", "Synthetic Alice", True, "site-a", "Demo North"),
            "bob": ("customer-b", "Synthetic Bob", True, "site-b", "Demo South"),
            "external": ("customer-a", "Synthetic Guest", False, "site-a", "Demo North"),
        }
        tenant, name, internal, site_id, site_name = personas[identity]
        return User(id=identity, tenant_id=tenant, name=name, internal=internal,
                    sites=[Site(id=site_id, name=site_name, tenant_id=tenant)])

    def report_types(self, user: User) -> list[ReportType]:
        return [
            ReportType(id="alarm-history", label="Alarm history", formats=["pdf", "csv"]),
            ReportType(id="site-status", label="Site status", formats=["pdf", "csv"]),
        ]

    def search_docs(self, user: User, query: str) -> list[Document]:
        if not any(term in query.lower() for term in ("alarm", "report", "offline", "sensor")):
            return []
        documents = [Document(
            id="doc-operations",
            title="Synthetic alarm and report guide",
            excerpt="For an offline sensor, check power and connectivity. Review the alarm history report. "
                    "If the issue remains, prepare a support ticket with the steps already tried.",
            url="/reference/operations",
        ), Document(
            id="doc-reports",
            title="Synthetic report preparation guide",
            excerpt="Choose a permitted site, report type, format and date range. Preparing a report does not "
                    "submit it. Review the prepared form before using Submit.",
            url="/reference/reports",
        )]
        return documents[:1] if "offline" in query.lower() or "sensor" in query.lower() else documents

    def site_alarms(self, user: User, site_id: str) -> list[Alarm]:
        allowed = {site.id for site in user.sites if site.tenant_id == user.tenant_id}
        if site_id not in allowed:
            return []
        return [Alarm(id=f"alarm-{site_id}", label="Sensor offline", status="active")]

    def site_health_checks(self, user: User, site_id: str) -> list[HealthCheck]:
        allowed = {site.id for site in user.sites if site.tenant_id == user.tenant_id}
        if site_id not in allowed:
            return []
        # Synthetic: Demo North's gateway is unreachable, Demo South has no open health checks.
        if site_id == "site-a":
            return [HealthCheck(id=f"hc-{site_id}-gateway", type="primary-gateway-offline",
                                check="Primary Gateway Offline", status="open")]
        return []

    def alarm_exists(self, user: User, site_id: str, alarm_id: str) -> bool:
        return any(alarm.id == alarm_id for alarm in self.site_alarms(user, site_id))

    def submit(self, user: User, kind: str, payload: dict, idempotency_key: str) -> dict:
        if payload["site_id"] not in {site.id for site in user.sites if site.tenant_id == user.tenant_id}:
            raise ValueError("Site unavailable.")
        return {
            "id": idempotency_key,
            "status": "mock_completed" if kind == "report" else "mock_received",
            "url": f"/reference/{kind}/{idempotency_key}",
            "synthetic": True,
        }
