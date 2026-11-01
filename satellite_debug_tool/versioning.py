"""发行版本的单一比较规则；构建 metadata 保留身份，版本优先级遵循 SemVer。"""

from __future__ import annotations

from dataclasses import dataclass
import re


_VERSION = re.compile(
    r"^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True)
class ReleaseVersion:
    release: tuple[int, int, int]
    prerelease: tuple[str, ...]


def parse_version(value: str, *, complete: bool = False) -> ReleaseVersion:
    match = _VERSION.fullmatch(str(value).strip())
    if match is None or (complete and match.group(3) is None):
        raise ValueError(f"invalid release version: {value!r}")
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in pre):
        raise ValueError(f"numeric prerelease identifier has a leading zero: {value!r}")
    return ReleaseVersion(
        (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)), pre
    )


def compare_versions(a: str, b: str) -> int:
    """返回 -1/0/1；两段旧版本按补零比较，正式清单另要求完整三段版本。"""
    va, vb = parse_version(a), parse_version(b)
    if va.release != vb.release:
        return 1 if va.release > vb.release else -1
    if va.prerelease == vb.prerelease:
        return 0
    if not va.prerelease:
        return 1
    if not vb.prerelease:
        return -1
    for pa, pb in zip(va.prerelease, vb.prerelease):
        if pa == pb:
            continue
        if pa.isdigit() and pb.isdigit():
            return 1 if int(pa) > int(pb) else -1
        if pa.isdigit() != pb.isdigit():
            return -1 if pa.isdigit() else 1
        return 1 if pa > pb else -1
    return 1 if len(va.prerelease) > len(vb.prerelease) else -1
