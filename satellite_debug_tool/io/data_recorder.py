import struct
from datetime import datetime


SDB_MAGIC = b"SDB\x00"
SDB_VERSION = 0x0001
SDB_FOOTER = b"\xee\xee\xee\xee"


class DataRecorder:
    def __init__(self, filepath: str):
        self._filepath = filepath
        self._file = None
        self._is_recording = False

    def start(self) -> bool:
        try:
            self._file = open(self._filepath, "wb")
            self._file.write(SDB_MAGIC)
            self._file.write(struct.pack("<H", SDB_VERSION))
            self._file.write(struct.pack("<Q", int(datetime.now().timestamp())))
            self._file.write(b"\x00" * 8)
            self._is_recording = True
            return True
        except Exception:
            return False

    def write_frame(self, frame_data: bytes) -> bool:
        if not self._is_recording or not self._file:
            return False
        try:
            self._file.write(frame_data)
            return True
        except Exception:
            return False

    def stop(self) -> bool:
        if not self._is_recording:
            return False
        try:
            self._file.write(SDB_FOOTER)
            self._file.close()
            self._file = None
            self._is_recording = False
            return True
        except Exception:
            return False

    @property
    def is_recording(self) -> bool:
        return self._is_recording
