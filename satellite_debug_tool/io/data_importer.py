"""Streaming SDB v2/v3 reader with a rebuildable sparse sidecar index."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
import json
from pathlib import Path
import struct
from typing import Generic, Optional, TypeVar, Union, overload
import warnings

from satellite_debug_tool.core.protocol import (
    DataReport,
    FrameReceiverV2,
    FrameV2Record,
)
from satellite_debug_tool.io.sdb_schema import (
    SDB_FOOTER,
    SDB_HEADER_CORE,
    SDB_HEADER_RESERVED_SIZE,
    SDB_MAGIC,
    SDB_PROFILE_LENGTH,
    SDB_RECORD_CONTROL_TX,
    SDB_RECORD_GAP,
    SDB_RECORD_METADATA,
    SDB_RECORD_RX_CHUNK,
    SDB_RECORD_SUMMARY,
    SDB_SUPPORTED_VERSIONS,
    SDB_VERSION_V2,
    SDB_VERSION_V3,
    iter_record_envelopes,
    read_exact,
)


_INDEX_SCHEMA = "satellite.sdb-sparse-index"
_INDEX_VERSION = 1
_INDEX_STRIDE = 1024
_STREAM_CHUNK_SIZE = 64 * 1024


class SdbFormatError(ValueError):
    """The SDB file is malformed or uses an unsupported version."""


@dataclass(frozen=True)
class SdbRawRecord:
    host_timestamp_ns: int
    data: bytes


@dataclass(frozen=True)
class SdbControlRecord:
    host_timestamp_ns: int
    data: bytes


@dataclass(frozen=True)
class SdbGapMarker:
    host_timestamp_ns: int
    dropped_chunks: int


@dataclass(frozen=True)
class SdbIndexEntry:
    host_timestamp_ns: int
    record_offset: int


_RecordT = TypeVar("_RecordT", SdbRawRecord, SdbControlRecord, SdbGapMarker)


class _TypedRecordSequence(Sequence[_RecordT], Generic[_RecordT]):
    """Compatibility sequence backed by streaming file iteration."""

    def __init__(self, owner: "SdbFile", record_type: int, count: int) -> None:
        self._owner = owner
        self._record_type = int(record_type)
        self._count = int(count)

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[_RecordT]:
        yield from self._owner._iter_typed_records(self._record_type)

    @overload
    def __getitem__(self, index: int) -> _RecordT: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[_RecordT, ...]: ...

    def __getitem__(self, index: int | slice) -> _RecordT | tuple[_RecordT, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._count)
            if step > 0:
                return tuple(islice(self, start, stop, step))
            return tuple(self[position] for position in range(start, stop, step))
        normalized = int(index)
        if normalized < 0:
            normalized += self._count
        if normalized < 0 or normalized >= self._count:
            raise IndexError(index)
        for current, record in enumerate(self):
            if current == normalized:
                return record
        raise IndexError(index)


class SdbFile:
    """Parsed SDB header plus lazy record views over the source file."""

    def __init__(
        self,
        *,
        path: Path,
        version: int,
        timestamp: int,
        profile: Optional[dict],
        metadata: dict,
        quality: dict,
        data_offset: int,
        data_end: int,
        counts: dict[int, int],
        metadata_events: tuple[dict, ...],
        sparse_index: tuple[SdbIndexEntry, ...],
    ) -> None:
        self.path = Path(path)
        self.version = int(version)
        self.timestamp = int(timestamp)
        self.profile = profile
        self.metadata = dict(metadata)
        self.quality = dict(quality)
        self.metadata_events = tuple(metadata_events)
        self.sparse_index = tuple(sparse_index)
        self._data_offset = int(data_offset)
        self._data_end = int(data_end)
        self.raw_records: Sequence[SdbRawRecord] = _TypedRecordSequence(
            self, SDB_RECORD_RX_CHUNK, counts.get(SDB_RECORD_RX_CHUNK, 0)
        )
        self.control_records: Sequence[SdbControlRecord] = _TypedRecordSequence(
            self, SDB_RECORD_CONTROL_TX, counts.get(SDB_RECORD_CONTROL_TX, 0)
        )
        self.gap_markers: Sequence[SdbGapMarker] = _TypedRecordSequence(
            self, SDB_RECORD_GAP, counts.get(SDB_RECORD_GAP, 0)
        )

    @property
    def index_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".sdbi")

    def iter_timed_records(
        self,
        *,
        start_host_timestamp_ns: Optional[int] = None,
        end_host_timestamp_ns: Optional[int] = None,
    ) -> Iterator[tuple[int, FrameV2Record]]:
        receiver = FrameReceiverV2()
        if self.version == SDB_VERSION_V2:
            with self.path.open("rb") as file_object:
                file_object.seek(self._data_offset)
                remaining = self._data_end - self._data_offset
                while remaining > 0:
                    chunk = file_object.read(min(_STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        raise SdbFormatError("truncated v2 frame region")
                    remaining -= len(chunk)
                    for record in receiver.feed(chunk):
                        yield 0, record
            return

        start_offset = self._seek_offset(start_host_timestamp_ns)
        for raw in self._iter_typed_records(
            SDB_RECORD_RX_CHUNK,
            start_offset=start_offset,
        ):
            if start_host_timestamp_ns is not None and raw.host_timestamp_ns < int(
                start_host_timestamp_ns
            ):
                receiver.feed(raw.data)
                continue
            if end_host_timestamp_ns is not None and raw.host_timestamp_ns > int(
                end_host_timestamp_ns
            ):
                break
            for record in receiver.feed(raw.data):
                yield raw.host_timestamp_ns, record

    def iter_records(self) -> Iterator[FrameV2Record]:
        for _host_timestamp_ns, record in self.iter_timed_records():
            yield record

    def iter_data_reports(self) -> Iterator[DataReport]:
        for record in self.iter_records():
            if isinstance(record, DataReport):
                yield record

    def _seek_offset(self, host_timestamp_ns: Optional[int]) -> int:
        if host_timestamp_ns is None or not self.sparse_index:
            return self._data_offset
        target = int(host_timestamp_ns)
        selected = self._data_offset
        for entry in self.sparse_index:
            if entry.host_timestamp_ns > target:
                break
            selected = entry.record_offset
        return selected

    def _iter_typed_records(
        self,
        record_type: int,
        *,
        start_offset: Optional[int] = None,
    ) -> Iterator:
        if self.version != SDB_VERSION_V3:
            return
        try:
            with self.path.open("rb") as file_object:
                for envelope in iter_record_envelopes(
                    file_object,
                    start_offset=self._data_offset if start_offset is None else start_offset,
                    end_offset=self._data_end,
                ):
                    if envelope.record_type != int(record_type):
                        continue
                    file_object.seek(envelope.payload_offset)
                    payload = read_exact(
                        file_object,
                        envelope.payload_length,
                        context="v3 record payload",
                    )
                    if record_type == SDB_RECORD_RX_CHUNK:
                        yield SdbRawRecord(envelope.host_timestamp_ns, payload)
                    elif record_type == SDB_RECORD_CONTROL_TX:
                        yield SdbControlRecord(envelope.host_timestamp_ns, payload)
                    elif record_type == SDB_RECORD_GAP:
                        if len(payload) != 4:
                            raise SdbFormatError("invalid v3 gap marker")
                        yield SdbGapMarker(
                            envelope.host_timestamp_ns,
                            struct.unpack("<I", payload)[0],
                        )
        except EOFError as exc:
            raise SdbFormatError(str(exc)) from exc


class DataImporter:
    """Open historical recordings without materializing their frame regions."""

    @staticmethod
    def open_sdb(filepath: Union[str, Path]) -> SdbFile:
        path = Path(filepath)
        try:
            file_size = path.stat().st_size
            with path.open("rb") as file_object:
                core = read_exact(file_object, SDB_HEADER_CORE.size, context="SDB header")
                magic, version, timestamp = SDB_HEADER_CORE.unpack(core)
                if magic != SDB_MAGIC:
                    raise SdbFormatError("Invalid SDB file format (magic mismatch)")
                if version not in SDB_SUPPORTED_VERSIONS:
                    raise SdbFormatError(
                        f"Unsupported SDB version {version:#x}; v2 and v3 are accepted. "
                        "Convert v1 files with sdb_v1_convert.py first."
                    )
                profile_len = SDB_PROFILE_LENGTH.unpack(
                    read_exact(
                        file_object,
                        SDB_PROFILE_LENGTH.size,
                        context="profile length",
                    )
                )[0]
                header_object = DataImporter._read_header_json(file_object, profile_len)
                read_exact(
                    file_object,
                    SDB_HEADER_RESERVED_SIZE,
                    context="reserved header",
                )
                data_offset = file_object.tell()
                footer_present = DataImporter._has_footer(file_object, file_size)
        except EOFError as exc:
            raise SdbFormatError(str(exc)) from exc
        except OSError as exc:
            raise SdbFormatError(str(exc)) from exc

        data_end = file_size - len(SDB_FOOTER) if footer_present else file_size
        if data_offset > data_end:
            raise SdbFormatError("SDB data region starts beyond the file footer")

        if version == SDB_VERSION_V2:
            return SdbFile(
                path=path,
                version=version,
                timestamp=timestamp,
                profile=header_object,
                metadata={},
                quality={"footer_present": footer_present, "complete": footer_present},
                data_offset=data_offset,
                data_end=data_end,
                counts={},
                metadata_events=(),
                sparse_index=(),
            )

        metadata = header_object if isinstance(header_object, dict) else {}
        profile = metadata.get("profile")
        if profile is not None and not isinstance(profile, dict):
            raise SdbFormatError("v3 profile must be an object or null")
        session = metadata.get("session")
        session_metadata = session if isinstance(session, dict) else {}
        scan = DataImporter._load_or_build_index(
            path,
            data_offset=data_offset,
            data_end=data_end,
            footer_present=footer_present,
        )
        summary = dict(scan["summary"])
        counts = {int(key): int(value) for key, value in scan["counts"].items()}
        quality = summary
        quality["footer_present"] = footer_present
        quality["gap_markers"] = counts.get(SDB_RECORD_GAP, 0)
        quality["gap_chunks"] = int(scan["gap_chunks"])
        quality["complete"] = bool(summary.get("complete", False) and footer_present)
        sparse_index = tuple(
            SdbIndexEntry(int(item[0]), int(item[1])) for item in scan["entries"]
        )
        return SdbFile(
            path=path,
            version=version,
            timestamp=timestamp,
            profile=profile,
            metadata=session_metadata,
            quality=quality,
            data_offset=data_offset,
            data_end=data_end,
            counts=counts,
            metadata_events=tuple(scan["metadata_events"]),
            sparse_index=sparse_index,
        )

    @staticmethod
    def _read_header_json(file_object, profile_len: int) -> Optional[dict]:
        if profile_len == 0:
            return None
        try:
            decoded = json.loads(
                read_exact(file_object, profile_len, context="header JSON").decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SdbFormatError(f"invalid header JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise SdbFormatError("SDB header JSON must contain an object")
        return decoded

    @staticmethod
    def _has_footer(file_object, file_size: int) -> bool:
        if file_size < len(SDB_FOOTER):
            return False
        file_object.seek(file_size - len(SDB_FOOTER))
        return file_object.read(len(SDB_FOOTER)) == SDB_FOOTER

    @staticmethod
    def _load_or_build_index(
        path: Path,
        *,
        data_offset: int,
        data_end: int,
        footer_present: bool,
    ) -> dict:
        source_stat = path.stat()
        index_path = path.with_suffix(path.suffix + ".sdbi")
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if DataImporter._index_matches(
                payload,
                source_size=source_stat.st_size,
                source_mtime_ns=source_stat.st_mtime_ns,
                data_offset=data_offset,
                data_end=data_end,
            ):
                return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

        payload = DataImporter._scan_v3(
            path,
            data_offset=data_offset,
            data_end=data_end,
        )
        payload.update(
            {
                "schema": _INDEX_SCHEMA,
                "schema_version": _INDEX_VERSION,
                "source_size": source_stat.st_size,
                "source_mtime_ns": source_stat.st_mtime_ns,
                "data_offset": data_offset,
                "data_end": data_end,
                "footer_present": bool(footer_present),
            }
        )
        DataImporter._write_index(index_path, payload)
        return payload

    @staticmethod
    def _index_matches(
        payload: object,
        *,
        source_size: int,
        source_mtime_ns: int,
        data_offset: int,
        data_end: int,
    ) -> bool:
        return bool(
            isinstance(payload, dict)
            and payload.get("schema") == _INDEX_SCHEMA
            and int(payload.get("schema_version", 0)) == _INDEX_VERSION
            and int(payload.get("source_size", -1)) == int(source_size)
            and int(payload.get("source_mtime_ns", -1)) == int(source_mtime_ns)
            and int(payload.get("data_offset", -1)) == int(data_offset)
            and int(payload.get("data_end", -1)) == int(data_end)
            and isinstance(payload.get("counts"), dict)
            and isinstance(payload.get("entries"), list)
            and isinstance(payload.get("metadata_events"), list)
            and isinstance(payload.get("summary"), dict)
        )

    @staticmethod
    def _scan_v3(path: Path, *, data_offset: int, data_end: int) -> dict:
        counts: dict[str, int] = {}
        entries: list[list[int]] = []
        metadata_events: list[dict] = []
        summary: dict = {}
        gap_chunks = 0
        ordinal = 0
        try:
            with path.open("rb") as file_object:
                for envelope in iter_record_envelopes(
                    file_object,
                    start_offset=data_offset,
                    end_offset=data_end,
                ):
                    key = str(envelope.record_type)
                    counts[key] = counts.get(key, 0) + 1
                    if ordinal % _INDEX_STRIDE == 0:
                        entries.append(
                            [envelope.host_timestamp_ns, envelope.record_offset]
                        )
                    ordinal += 1
                    if envelope.record_type not in {
                        SDB_RECORD_GAP,
                        SDB_RECORD_METADATA,
                        SDB_RECORD_SUMMARY,
                    }:
                        continue
                    file_object.seek(envelope.payload_offset)
                    payload = read_exact(
                        file_object,
                        envelope.payload_length,
                        context="v3 indexed payload",
                    )
                    if envelope.record_type == SDB_RECORD_GAP:
                        if len(payload) != 4:
                            raise SdbFormatError("invalid v3 gap marker")
                        gap_chunks += struct.unpack("<I", payload)[0]
                        continue
                    try:
                        decoded = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SdbFormatError(f"invalid v3 JSON record: {exc}") from exc
                    if not isinstance(decoded, dict):
                        raise SdbFormatError("v3 JSON record must contain an object")
                    if envelope.record_type == SDB_RECORD_SUMMARY:
                        summary = decoded
                    else:
                        metadata_events.append(decoded)
        except EOFError as exc:
            raise SdbFormatError(str(exc)) from exc
        return {
            "counts": counts,
            "entries": entries,
            "metadata_events": metadata_events,
            "summary": summary,
            "gap_chunks": gap_chunks,
        }

    @staticmethod
    def _write_index(path: Path, payload: dict) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def read_sdb(filepath: Union[str, Path]) -> Iterator[DataReport]:
        return DataImporter.open_sdb(filepath).iter_data_reports()

    @staticmethod
    def read_csv(filepath: Union[str, Path]) -> Iterator[DataReport]:
        warnings.warn(
            "CSV import is not supported in protocol v2 yet.",
            RuntimeWarning,
            stacklevel=2,
        )
        return iter(())


__all__ = [
    "DataImporter",
    "SdbControlRecord",
    "SdbFile",
    "SdbFormatError",
    "SdbGapMarker",
    "SdbIndexEntry",
    "SdbRawRecord",
]
