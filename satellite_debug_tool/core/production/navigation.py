"""Typed production gates for optional external navigation modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from satellite_debug_tool.core.product import (
    Availability,
    NavigationSource,
    ProductSnapshot,
)


class ExternalInsApplicability(str, Enum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExternalInsGate:
    applicability: ExternalInsApplicability
    reason: str


def evaluate_external_ins(snapshot: ProductSnapshot) -> ExternalInsGate:
    """Classify whether the configured device must run external-INS tests."""

    sources = snapshot.navigation_sources
    supported = sources.external_ins_supported
    configured = sources.external_ins_configured
    role_mask = sources.external_role_mask
    source = sources.external_ins_source

    required = (supported, configured, role_mask, source)
    if any(
        value.availability in {Availability.PENDING, Availability.UNSUPPORTED}
        for value in required
    ):
        return ExternalInsGate(ExternalInsApplicability.UNKNOWN, "capability_unknown")
    if supported.value is False:
        return ExternalInsGate(
            ExternalInsApplicability.NOT_APPLICABLE,
            "firmware_not_supported",
        )
    if configured.value is False or int(role_mask.value or 0) == 0:
        return ExternalInsGate(
            ExternalInsApplicability.NOT_APPLICABLE,
            "external_ins_not_configured",
        )
    if source.value in {None, NavigationSource.NONE, NavigationSource.UNKNOWN}:
        return ExternalInsGate(
            ExternalInsApplicability.UNKNOWN,
            "source_configuration_inconsistent",
        )
    return ExternalInsGate(ExternalInsApplicability.APPLICABLE, "configured")
