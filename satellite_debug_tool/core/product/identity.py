"""Canonical validation for device identity evidence."""

from __future__ import annotations


_IDENTITY_PLACEHOLDERS = frozenset({
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "unsupported",
    "not supported",
    "afd01-dev",
    "afd01c-dev",
    "esa01-dev",
    "不支持",
    "未配置",
    "待配置",
})


def verified_identity_text(value: object) -> str:
    """Return a usable immutable identity fact or an empty string."""

    text = str(value or "").strip()
    return "" if text.casefold() in _IDENTITY_PLACEHOLDERS else text


def verified_device_uid(value: object) -> str:
    """Return a non-zero device UID in canonical comparison form."""

    text = verified_identity_text(value).upper()
    compact = "".join(character for character in text if character not in " :-")
    if not compact or set(compact) == {"0"}:
        return ""
    return compact


__all__ = ["verified_device_uid", "verified_identity_text"]
