"""External-INS production applicability gates."""

from satellite_debug_tool.core.product import (
    NavigationSource,
    NavigationSourceInfo,
    ProductSnapshot,
    ProductValue,
)
from satellite_debug_tool.core.production import (
    ExternalInsApplicability,
    evaluate_external_ins,
)


def _snapshot(
    *,
    supported: bool,
    configured: bool,
    role_mask: int,
    source: NavigationSource,
) -> ProductSnapshot:
    return ProductSnapshot(
        navigation_sources=NavigationSourceInfo(
            external_ins_supported=ProductValue.valid(supported),
            external_ins_configured=ProductValue.valid(configured),
            external_role_mask=ProductValue.valid(role_mask),
            external_ins_source=ProductValue.valid(source),
        )
    )


def test_external_ins_gate_distinguishes_na_applicable_and_unknown() -> None:
    not_installed = evaluate_external_ins(
        _snapshot(
            supported=True,
            configured=False,
            role_mask=0,
            source=NavigationSource.NONE,
        )
    )
    assert not_installed.applicability == ExternalInsApplicability.NOT_APPLICABLE
    assert not_installed.reason == "external_ins_not_configured"

    bynav = evaluate_external_ins(
        _snapshot(
            supported=True,
            configured=True,
            role_mask=0x07,
            source=NavigationSource.BYNAV,
        )
    )
    assert bynav.applicability == ExternalInsApplicability.APPLICABLE

    assert (
        evaluate_external_ins(ProductSnapshot()).applicability
        == ExternalInsApplicability.UNKNOWN
    )


def test_external_ins_gate_does_not_hide_inconsistent_configuration() -> None:
    inconsistent = evaluate_external_ins(
        _snapshot(
            supported=True,
            configured=True,
            role_mask=0x04,
            source=NavigationSource.NONE,
        )
    )
    assert inconsistent.applicability == ExternalInsApplicability.UNKNOWN
    assert inconsistent.reason == "source_configuration_inconsistent"
