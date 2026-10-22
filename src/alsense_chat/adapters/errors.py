"""Categorized integration failures.

Adapters translate upstream failures into these errors. Each carries a user-safe message, so the UI can say
exactly what went wrong ("You do not have access to this site.") without exposing upstream bodies, tokens or
internal identifiers.
"""

SERVICE_NAMES = {
    "identity": "sign-in service",
    "sites": "site directory",
    "knowledge": "guidance search",
    "health": "health-check service",
    "reports": "report service",
    "support": "support service",
    "model": "AI assistant",
}


class IntegrationError(Exception):
    category = "unavailable"
    http_status = 503
    template = "The {service} is temporarily unavailable. Try again in a moment."

    def __init__(self, service: str, message: str | None = None):
        self.service = service
        name = SERVICE_NAMES.get(service, "service")
        self.user_message = message or self.template.format(service=name)
        super().__init__(self.user_message)


class NotConfiguredError(IntegrationError):
    category = "not_configured"
    http_status = 501
    template = "The {service} isn't connected in this environment."


class Unauthenticated(IntegrationError):
    category = "unauthenticated"
    http_status = 401
    template = "Your sign-in is no longer valid for the {service}. Start a new session."


class Forbidden(IntegrationError):
    category = "forbidden"
    http_status = 403
    template = "You do not have access to this site."


class NotFound(IntegrationError):
    category = "not_found"
    http_status = 404
    template = "The {service} couldn't find that site."


class ServiceUnavailable(IntegrationError):
    category = "unavailable"
    http_status = 503


class ServiceTimeout(ServiceUnavailable):
    category = "timeout"
    http_status = 504
    template = "The {service} didn't respond in time. Try again in a moment."


class StaleData(IntegrationError):
    category = "stale"
    http_status = 503
    template = "The latest status from the {service} is out of date, so I can't rely on it."


class ModelUnavailable(ServiceUnavailable):
    template = "The AI assistant is unavailable right now. Guided checks and the quick actions still work."

    def __init__(self, message: str | None = None):
        super().__init__("model", message)


ERRORS: dict[str, type[IntegrationError]] = {
    cls.category: cls
    for cls in (NotConfiguredError, Unauthenticated, Forbidden, NotFound, ServiceUnavailable, ServiceTimeout, StaleData)
}
