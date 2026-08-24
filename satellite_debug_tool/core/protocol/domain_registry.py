"""Immutable command-to-domain decoder registry."""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Optional

from .domains import debug, orbit, product


Decoder = Callable[[bytes], object]

_DOMAIN_DECODERS = MappingProxyType({
    "debug": debug.DECODERS,
    "product": product.DECODERS,
    "orbit": orbit.DECODERS,
})

_DECODERS: dict[int, Decoder] = {}
_DOMAINS: dict[int, str] = {}
for domain_name, domain_decoders in _DOMAIN_DECODERS.items():
    for command, decoder in domain_decoders.items():
        if command in _DECODERS:
            raise RuntimeError(f"protocol command 0x{command:02X} has multiple domains")
        _DECODERS[command] = decoder
        _DOMAINS[command] = domain_name

DECODERS = MappingProxyType(_DECODERS)
COMMAND_DOMAINS = MappingProxyType(_DOMAINS)
DOMAIN_DECODERS = _DOMAIN_DECODERS


def decoder_for(command: int) -> Optional[Decoder]:
    return DECODERS.get(int(command))


def domain_for(command: int) -> Optional[str]:
    return COMMAND_DOMAINS.get(int(command))


__all__ = [
    "COMMAND_DOMAINS",
    "DECODERS",
    "DOMAIN_DECODERS",
    "Decoder",
    "decoder_for",
    "domain_for",
]
