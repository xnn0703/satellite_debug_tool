"""Process-wide, race-free recording path reservations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
import unicodedata
from typing import Optional


class RecordingPathError(RuntimeError):
    """Raised when one path cannot be reserved without overwrite risk."""


def canonical_recording_key(path: Path | str) -> str:
    candidate = Path(path)
    parent = candidate.parent.resolve(strict=False)
    name = unicodedata.normalize("NFC", candidate.name)
    value = str(parent / name)
    if sys.platform in {"darwin", "win32"}:
        value = value.casefold()
    return value


@dataclass
class RecordingReservation:
    """Own one exclusive file descriptor and one process path lease."""

    _registry: "RecordingPathRegistry"
    path: Path
    key: str
    _fd: Optional[int]
    device: int
    inode: int
    _released: bool = False

    @property
    def released(self) -> bool:
        return self._released

    def take_fd(self) -> int:
        if self._released or self._fd is None:
            raise RecordingPathError("recording reservation has no available descriptor")
        fd = self._fd
        self._fd = None
        return fd

    def release(self) -> None:
        if self._released:
            return
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._registry._release(self.key)
        self._released = True

    def discard_failed_file(self) -> Path | None:
        """Remove only our inode; isolate it as ``.failed`` when unlink fails."""

        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        isolated: Path | None = None
        try:
            current = self.path.stat()
        except OSError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (
            self.device,
            self.inode,
        ):
            try:
                self.path.unlink()
            except OSError:
                isolated = self._failed_path()
                try:
                    os.replace(self.path, isolated)
                except OSError:
                    isolated = self.path
        self.release()
        return isolated

    def _failed_path(self) -> Path:
        base = self.path.with_name(f"{self.path.name}.failed")
        if not base.exists():
            return base
        for index in range(1, 10000):
            candidate = self.path.with_name(f"{self.path.name}.failed.{index:03d}")
            if not candidate.exists():
                return candidate
        raise RecordingPathError("recording failure quarantine is exhausted")

    def __enter__(self) -> "RecordingReservation":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class RecordingPathRegistry:
    """One affirmative owner for every canonical recording path."""

    _default_instance: Optional["RecordingPathRegistry"] = None
    _default_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: set[str] = set()

    @classmethod
    def default(cls) -> "RecordingPathRegistry":
        with cls._default_lock:
            if cls._default_instance is None:
                cls._default_instance = cls()
            return cls._default_instance

    def reserve_exact(self, path: Path | str) -> RecordingReservation:
        requested = Path(path)
        if not requested.name:
            raise RecordingPathError("recording path must name a file")
        candidate = requested.parent.resolve(strict=False) / unicodedata.normalize(
            "NFC", requested.name
        )
        key = canonical_recording_key(candidate)
        with self._lock:
            if key in self._active:
                raise RecordingPathError("recording path already has an active owner")
            self._active.add(key)
        fd: Optional[int] = None
        created = False
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            created = True
            stat = os.fstat(fd)
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if created:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            self._release(key)
            raise
        return RecordingReservation(
            self,
            candidate,
            key,
            fd,
            int(stat.st_dev),
            int(stat.st_ino),
        )

    def reserve_unique(self, requested: Path | str) -> RecordingReservation:
        base = Path(requested)
        for index in range(0, 10000):
            candidate = base if index == 0 else base.with_name(
                f"{base.stem}_{index:03d}{base.suffix}"
            )
            try:
                return self.reserve_exact(candidate)
            except FileExistsError:
                continue
            except RecordingPathError:
                if canonical_recording_key(candidate) in self.active_keys:
                    continue
                raise
        raise RecordingPathError("recording filename sequence is exhausted")

    @property
    def active_keys(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._active)

    def _release(self, key: str) -> None:
        with self._lock:
            self._active.discard(key)


__all__ = [
    "RecordingPathError",
    "RecordingPathRegistry",
    "RecordingReservation",
    "canonical_recording_key",
]
