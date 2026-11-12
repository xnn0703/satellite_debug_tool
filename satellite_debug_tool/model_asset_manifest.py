"""Authoritative byte identities for bundled device model assets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelAssetIdentity:
    """Immutable file identity of one bundled STL model."""

    size: int
    sha256: str


BUILTIN_MODEL_ASSETS: dict[str, ModelAssetIdentity] = {
    "afd01.stl": ModelAssetIdentity(
        size=8_066_184,
        sha256="f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37",
    ),
    "afd01b2.stl": ModelAssetIdentity(
        size=3_795_084,
        sha256="344e230e10584f72edc83a26d0faad625c231ffbb92249990fe670754da51b49",
    ),
    "afd01c.stl": ModelAssetIdentity(
        size=2_593_784,
        sha256="03cde446036f2f3686fdbcb14bd7586aefd84cf3bd95acaef8867f058837c6d4",
    ),
    "afd01d.stl": ModelAssetIdentity(
        size=2_584_084,
        sha256="0ef8cb2b7c0cf9fe073c9323c5601c6a36578ed4940aa8c55dd1abca5963c3c9",
    ),
    "esa01.stl": ModelAssetIdentity(
        size=8_066_184,
        sha256="f58706d3b6fa1ac67f67fea50b8c9c4aa10b50716c8aedab8ef529290f0d5b37",
    ),
}
