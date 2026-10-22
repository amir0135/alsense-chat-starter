"""Integration adapters. `build_services` is the one place that decides which implementation runs."""

from .contracts import User
from .errors import IntegrationError, NotConfiguredError
from .interfaces import Services

__all__ = ["IntegrationError", "NotConfiguredError", "Services", "User", "build_services"]


def build_services(data_source: str) -> Services:
    """Return the configured services. Never fall back from a real integration to synthetic data."""
    if data_source == "synthetic":
        from . import synthetic

        return synthetic.build()
    if data_source == "alsense":
        from . import alsense

        return alsense.build()
    raise ValueError("DATA_SOURCE must be synthetic or alsense.")
