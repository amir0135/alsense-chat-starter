"""Synthetic fault switches: make integration failures as demonstrable as the happy paths.

Enable them for the whole process with `SYNTHETIC_FAULTS=api_timeout,report_failed` or per demo session from a
scenario. They wrap the synthetic services only; a real integration is never wrapped.
"""

from dataclasses import replace
from datetime import timedelta

from ..contracts import (
    Document,
    HealthReport,
    ReportJob,
    ReportRequest,
    ScheduledReportRequest,
    Site,
    Submission,
    SupportRequest,
    User,
)
from ..errors import Forbidden, NotFound, ServiceTimeout, ServiceUnavailable, Unauthenticated
from ..interfaces import HealthCheckService, KnowledgeSearch, ReportService, Services, SiteDirectory, SupportService

FAULTS = {
    "api_timeout": "Every synthetic API call times out",
    "unauthenticated": "Synthetic APIs reject the delegated sign-in (401)",
    "site_forbidden": "Site-specific calls return 403: access to the site was revoked",
    "site_not_found": "Site-specific calls return 404: the site no longer exists",
    "sites_unavailable": "The site directory is down (503)",
    "health_unavailable": "The health-check service is down (503)",
    "health_stale": "Health status is hours old",
    "knowledge_empty": "Guidance search finds no approved answer",
    "report_processing": "Report jobs stay processing",
    "report_failed": "Report jobs fail",
    "support_unavailable": "Support submission is down (503)",
    "model_unavailable": "The AI assistant is unavailable; guided checks still work",
}


class _Faulty:
    def __init__(self, inner, faults: frozenset[str], service: str):
        self.inner, self.faults, self.service = inner, faults, service

    def upstream(self, *, site: bool = False) -> None:
        if "api_timeout" in self.faults:
            raise ServiceTimeout(self.service)
        if "unauthenticated" in self.faults:
            raise Unauthenticated(self.service)
        if site and "site_forbidden" in self.faults:
            raise Forbidden(self.service)
        if site and "site_not_found" in self.faults:
            raise NotFound(self.service)


class FaultySites(_Faulty):
    inner: SiteDirectory

    def permitted_sites(self, user: User) -> list[Site]:
        self.upstream()
        if "sites_unavailable" in self.faults:
            raise ServiceUnavailable(self.service)
        return self.inner.permitted_sites(user)

    def active_alarms(self, user: User, site_id: str):
        self.upstream()
        return self.inner.active_alarms(user, site_id)


class FaultyKnowledge(_Faulty):
    inner: KnowledgeSearch

    def search(self, user: User, query: str) -> list[Document]:
        self.upstream()
        return [] if "knowledge_empty" in self.faults else self.inner.search(user, query)


class FaultyHealth(_Faulty):
    inner: HealthCheckService

    def site_health(self, user: User, site_id: str) -> HealthReport:
        self.upstream(site=True)
        if "health_unavailable" in self.faults:
            raise ServiceUnavailable(self.service)
        report = self.inner.site_health(user, site_id)
        if "health_stale" in self.faults:
            report = report.model_copy(update={"observed_at": report.observed_at - timedelta(hours=6)})
        return report


class FaultyReports(_Faulty):
    inner: ReportService

    def report_types(self, user: User):
        self.upstream()
        return self.inner.report_types(user)

    def submit_report(self, user: User, request: ReportRequest, idempotency_key: str) -> Submission:
        self.upstream(site=True)
        return self.inner.submit_report(user, request, idempotency_key)

    def schedule_report(self, user: User, request: ScheduledReportRequest, idempotency_key: str) -> Submission:
        self.upstream(site=True)
        return self.inner.schedule_report(user, request, idempotency_key)

    def report_status(self, user: User, job_id: str) -> ReportJob:
        self.upstream()
        job = self.inner.report_status(user, job_id)
        if "report_failed" in self.faults:
            return job.model_copy(
                update={
                    "state": "failed",
                    "url": None,
                    "completed_at": None,
                    "expires_at": None,
                    "message": "Synthetic failure: the report job did not finish.",
                }
            )
        if "report_processing" in self.faults:
            return job.model_copy(update={"state": "processing", "url": None, "completed_at": None, "expires_at": None})
        return job


class FaultySupport(_Faulty):
    inner: SupportService

    def submit_request(self, user: User, request: SupportRequest, idempotency_key: str) -> Submission:
        self.upstream(site=True)
        if "support_unavailable" in self.faults:
            raise ServiceUnavailable(self.service)
        return self.inner.submit_request(user, request, idempotency_key)


def unknown_faults(names) -> list[str]:
    return sorted(set(names) - set(FAULTS))


def with_faults(services: Services, faults: frozenset[str]) -> Services:
    if not faults:
        return services
    if services.kind != "synthetic":
        raise ValueError("Synthetic faults apply to synthetic services only.")
    return replace(
        services,
        sites=FaultySites(services.sites, faults, "sites"),
        knowledge=FaultyKnowledge(services.knowledge, faults, "knowledge"),
        health=FaultyHealth(services.health, faults, "health"),
        reports=FaultyReports(services.reports, faults, "reports"),
        support=FaultySupport(services.support, faults, "support"),
    )
