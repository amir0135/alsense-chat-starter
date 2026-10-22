"""Synthetic adapters: the default, credential-free implementation of every service."""

from ..interfaces import Services
from .faults import FAULTS, unknown_faults, with_faults
from .services import (
    PERSONAS,
    SyntheticHealth,
    SyntheticIdentity,
    SyntheticKnowledge,
    SyntheticReports,
    SyntheticSiteDirectory,
    SyntheticSupport,
)

__all__ = ["FAULTS", "PERSONAS", "build", "unknown_faults", "with_faults"]


def build() -> Services:
    return Services(
        kind="synthetic",
        identity=SyntheticIdentity(),
        sites=SyntheticSiteDirectory(),
        knowledge=SyntheticKnowledge(),
        health=SyntheticHealth(),
        reports=SyntheticReports(),
        support=SyntheticSupport(),
    )
