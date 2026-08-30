"""Registered customer-product support is resolved by Product/Session state."""

from __future__ import annotations

import pytest

from satellite_debug_tool.core.profile import ProfileStore
from satellite_debug_tool.core.protocol import (
    MetaInfo,
    ServiceCapabilities,
    ServiceFastState,
    ServiceIdentity,
)
from satellite_debug_tool.core.session import DeviceSessionCore
from satellite_debug_tool.core.product import customer_ota_product_policy


@pytest.mark.parametrize(
    ("hardware_type", "service_protocol", "supported"),
    (
        ("afd01", 2, True),
        ("afd01", 6, True),
        ("afd01", 8, True),
        ("afd01c", 8, True),
        ("afd01c", 7, False),
        ("esa01", 6, True),
        ("unknown-terminal", 6, False),
    ),
)
def test_session_customer_product_policy_requires_registered_hw_and_protocol(
    qapplication_session,
    hardware_type: str,
    service_protocol: int,
    supported: bool,
) -> None:
    profiles = ProfileStore()
    profiles.apply_meta(MetaInfo(2, "0.1.253", hardware_type, "SN-TEST"))
    session = DeviceSessionCore(profile_store=profiles)
    session.attach_transport(object(), lambda _frame: True)
    session.product_store.feed(
        ServiceIdentity(
            1,
            1,
            0x17,
            hardware_type.upper(),
            "SN-TEST",
            "0.1.253",
            "",
            service_protocol,
        )
    )
    session.product_store.feed(
        ServiceCapabilities(
            1, 1, 0x3F, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x03, 0x01
        )
    )
    session.product_store.feed(
        ServiceFastState(
            1, 1, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    state = session.customer_service_state()

    assert state.hardware_type == hardware_type
    assert state.customer_service_supported is supported
    assert state.customer_service_ready is supported
    assert state.rf_control_ready is supported


def test_esa01_requires_v6_product_service(qapplication_session) -> None:
    profiles = ProfileStore()
    profiles.apply_meta(MetaInfo(2, "0.1.253", "esa01", "SN-TEST"))
    session = DeviceSessionCore(profile_store=profiles)
    session.attach_transport(object(), lambda _frame: True)
    session.product_store.feed(
        ServiceIdentity(1, 1, 0x17, "ESA01", "SN-TEST", "0.1.253", "", 5)
    )
    session.product_store.feed(
        ServiceFastState(
            1, 1, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    state = session.customer_service_state()

    assert not state.customer_service_supported
    assert not state.customer_service_ready
    assert not state.rf_control_ready


def test_debug_and_product_identity_must_match(qapplication_session) -> None:
    profiles = ProfileStore()
    profiles.apply_meta(MetaInfo(2, "0.1.253", "afd01c", "SN-TEST"))
    session = DeviceSessionCore(profile_store=profiles)
    session.attach_transport(object(), lambda _frame: True)
    session.product_store.feed(
        ServiceIdentity(1, 1, 0x17, "AFD01", "SN-TEST", "0.1.253", "", 8)
    )
    session.product_store.feed(
        ServiceFastState(
            1, 1, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    state = session.customer_service_state()

    assert not state.customer_service_supported
    assert not state.customer_service_ready


@pytest.mark.parametrize(
    ("hardware_type", "product_identity"),
    (("afd01", "AFD01"), ("afd01c", "AFD01C")),
)
def test_mount_configuration_requires_v8_and_device_capability(
    qapplication_session,
    hardware_type: str,
    product_identity: str,
) -> None:
    profiles = ProfileStore()
    profiles.apply_meta(MetaInfo(2, "0.1.253", hardware_type, "SN-TEST"))
    session = DeviceSessionCore(profile_store=profiles)
    session.attach_transport(object(), lambda _frame: True)
    session.product_store.feed(
        ServiceIdentity(
            1, 1, 0x17, product_identity, "SN-TEST", "0.1.253", "", 8
        )
    )
    session.product_store.feed(
        ServiceCapabilities(
            1, 1, 0x3F, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x07, 0x01
        )
    )
    session.product_store.feed(
        ServiceFastState(
            1, 1, 0xFFF, 1, 0, False, 3, 3, False,
            0.0, 0.0, 0.0, 0.0, 0.0, 15.0,
        )
    )

    assert session.customer_service_state().mount_configuration_ready

    session.product_store.feed(
        ServiceCapabilities(
            1, 2, 0x3F, 17700.0, 21200.0, 27500.0, 31000.0, 0x0C, 0x03, 0x01
        )
    )
    assert not session.customer_service_state().mount_configuration_ready


@pytest.mark.parametrize(
    ("product_identity", "hardware_type"),
    (("AFD01", "afd01"), ("afd01c", "afd01c")),
)
def test_customer_ota_policy_resolves_only_registered_exact_product(
    product_identity: str, hardware_type: str
) -> None:
    policy = customer_ota_product_policy(product_identity)

    assert policy is not None
    assert policy.hardware_type == hardware_type


@pytest.mark.parametrize("product_identity", (None, "", "ESA01", "UNKNOWN"))
def test_customer_ota_policy_rejects_unregistered_product(product_identity) -> None:
    assert customer_ota_product_policy(product_identity) is None
