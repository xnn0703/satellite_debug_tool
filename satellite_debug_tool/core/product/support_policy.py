"""Registered product policy and capability-derived customer service state."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from satellite_debug_tool.core.protocol import (
    SERVICE_FEATURE_DEVICE_MOUNT,
    ServiceCapabilities,
)


@dataclass(frozen=True)
class CustomerProductPolicy:
    """One explicit product compatibility contract for a hardware type."""

    hardware_type: str
    product_identity: str
    supported_service_protocols: frozenset[int]
    customer_ota_product: Optional[str] = None
    production_recipe_product: Optional[str] = None


@dataclass(frozen=True)
class CustomerServiceState:
    """Positive customer-service facts computed by the Product/Session domain."""

    hardware_type: str = ""
    customer_service_supported: bool = False
    customer_service_ready: bool = False
    rf_control_ready: bool = False
    mount_configuration_ready: bool = False


_CUSTOMER_PRODUCT_POLICIES = {
    # AFD01 v2-v8 use the same envelope; v8 adds typed device-mount control.
    "afd01": CustomerProductPolicy(
        "afd01",
        "AFD01",
        frozenset(range(2, 9)),
        customer_ota_product="AFD01",
        production_recipe_product="afd01",
    ),
    # AFD01C is a separate product/firmware identity introduced at protocol v8.
    "afd01c": CustomerProductPolicy(
        "afd01c",
        "AFD01C",
        frozenset({8}),
        customer_ota_product="AFD01C",
        production_recipe_product="afd01c",
    ),
    # ESA01 is introduced with the complete v6 Product Service contract.
    "esa01": CustomerProductPolicy("esa01", "ESA01", frozenset({6})),
}


def customer_product_policy(hardware_type: Optional[str]) -> Optional[CustomerProductPolicy]:
    """Return the registered policy only; unknown hardware is never inferred."""

    if not isinstance(hardware_type, str):
        return None
    return _CUSTOMER_PRODUCT_POLICIES.get(hardware_type.strip().lower())


def production_product_policy(hardware_type: Optional[str]) -> Optional[CustomerProductPolicy]:
    """Return an explicitly production-registered product policy."""

    policy = customer_product_policy(hardware_type)
    if policy is None or policy.production_recipe_product is None:
        return None
    return policy


def production_recipe_product_policy(
    product: Optional[str],
) -> Optional[CustomerProductPolicy]:
    """Return the production policy registered for a recipe product name."""

    if not isinstance(product, str):
        return None
    normalized = product.strip().lower()
    matches = tuple(
        policy
        for policy in _CUSTOMER_PRODUCT_POLICIES.values()
        if policy.production_recipe_product
        and policy.production_recipe_product.lower() == normalized
    )
    return matches[0] if len(matches) == 1 else None


def customer_ota_product_policy(
    product_identity: Optional[str],
) -> Optional[CustomerProductPolicy]:
    """Return the one registered OTA policy for an exact signed product name."""

    if not isinstance(product_identity, str):
        return None
    normalized = product_identity.strip().lower()
    matches = tuple(
        policy
        for policy in _CUSTOMER_PRODUCT_POLICIES.values()
        if policy.customer_ota_product
        and policy.customer_ota_product.lower() == normalized
    )
    return matches[0] if len(matches) == 1 else None


def product_identity_matches(
    policy: Optional[CustomerProductPolicy],
    product_identity: Optional[str],
) -> bool:
    """Require the device-declared product identity to match the registered type."""

    if policy is None or not isinstance(product_identity, str):
        return False
    return product_identity.strip().lower() == policy.product_identity.lower()


def customer_service_state(
    *,
    hardware_type: Optional[str],
    product_identity: Optional[str],
    service_protocol: Optional[int],
    capabilities: Optional[ServiceCapabilities],
    connected: bool,
    telemetry_ready: bool,
) -> CustomerServiceState:
    """Resolve support and RF readiness from registered, device-proved facts."""

    normalized_type = hardware_type.strip().lower() if isinstance(hardware_type, str) else ""
    policy = customer_product_policy(normalized_type)
    service_supported = (
        policy is not None
        and product_identity_matches(policy, product_identity)
        and service_protocol is not None
        and int(service_protocol) in policy.supported_service_protocols
    )
    service_ready = bool(service_supported and connected and telemetry_ready)
    return CustomerServiceState(
        hardware_type=normalized_type,
        customer_service_supported=service_supported,
        customer_service_ready=service_ready,
        rf_control_ready=service_ready and _rf_capabilities_ready(capabilities),
        mount_configuration_ready=(
            service_ready
            and int(service_protocol or 0) >= 8
            and capabilities is not None
            and bool(capabilities.feature_flags & SERVICE_FEATURE_DEVICE_MOUNT)
        ),
    )


def _rf_capabilities_ready(capabilities: Optional[ServiceCapabilities]) -> bool:
    if capabilities is None:
        return False
    # bit0..5 prove RX/TX ranges, polarization values, and independent RX/TX
    # polarization. The feature flag alone is not authoritative without bit5.
    if capabilities.valid_mask & 0x3F != 0x3F:
        return False
    if not (capabilities.feature_flags & 0x01):
        return False
    bounds = (
        capabilities.rx_frequency_min_mhz,
        capabilities.rx_frequency_max_mhz,
        capabilities.tx_frequency_min_mhz,
        capabilities.tx_frequency_max_mhz,
    )
    if not all(math.isfinite(float(value)) for value in bounds):
        return False
    if capabilities.rx_frequency_min_mhz > capabilities.rx_frequency_max_mhz:
        return False
    if capabilities.tx_frequency_min_mhz > capabilities.tx_frequency_max_mhz:
        return False
    return bool(int(capabilities.polarization_mask) & 0x0F)
