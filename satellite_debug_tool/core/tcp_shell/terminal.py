"""Shell 的文字输入合同、接收流解析与有界行终端模型。"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, field, replace
from typing import Callable
import unicodedata


class ShellInputError(ValueError):
    """文字提交包含设备命令合同之外的字符。"""

    def __init__(self, characters: tuple[int, ...]) -> None:
        self.characters = characters
        super().__init__(', '.join(f'U+{value:04X}' for value in characters))


def command_text_bytes(text: str) -> bytes:
    """原子校验文字；控制键与 YMODEM 由各自字节入口发送。"""
    characters = tuple(dict.fromkeys(ord(char) for char in text
                                    if char not in '\r\n\t' and not 0x20 <= ord(char) <= 0x7E))
    if characters:
        raise ShellInputError(characters)
    return text.encode('ascii')


Token = tuple[str, str]


class TerminalStreamDecoder:
    """每批输入一次；UTF-8、CSI 与 OSC 的未完成状态由本实例独占。"""

    MAX_ESCAPE_BYTES = 1024

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder('utf-8')(errors='surrogateescape')
        self._escape = bytearray()
        self._state = 'text'

    def reset(self) -> None:
        self._utf8.reset()
        self._escape.clear()
        self._state = 'text'

    def finish(self) -> list[Token]:
        """连接结束时呈现截断字符并释放当前流状态。"""
        return self.feed(b"", final=True)

    def feed(self, data: bytes, *, final: bool = False) -> list[Token]:
        tokens: list[Token] = []
        plain = bytearray()

        def flush(*, final: bool = False) -> None:
            text = self._utf8.decode(bytes(plain), final=final)
            plain.clear()
            if final:
                self._utf8.reset()
            visible: list[str] = []
            for char in text:
                value = ord(char)
                if 0xDC80 <= value <= 0xDCFF:
                    visible.append('\uFFFD')
                    tokens.append(('invalid_utf8', f'{value - 0xDC00:02X}'))
                else:
                    visible.append(char)
            if visible:
                tokens.append(('text', ''.join(visible)))

        for byte in data:
            if self._state == 'text':
                if byte == 0x1B:
                    flush(final=True)
                    self._escape = bytearray([byte])
                    self._state = 'escape'
                else:
                    plain.append(byte)
                continue
            self._escape.append(byte)
            if len(self._escape) > self.MAX_ESCAPE_BYTES:
                tokens.append(('invalid_escape', 'length'))
                self._escape.clear()
                self._state = 'text'
                continue
            if self._state == 'escape':
                if byte == ord('['):
                    self._state = 'csi'
                elif byte == ord(']'):
                    self._state = 'osc'
                else:
                    self._state = 'text'
                    self._escape.clear()
            elif self._state == 'csi':
                if 0x40 <= byte <= 0x7E:
                    kind = {ord('m'): 'sgr', ord('K'): 'el', ord('C'): 'right',
                            ord('D'): 'left', ord('G'): 'column'}.get(byte)
                    params = bytes(self._escape[2:-1]).decode('ascii')
                    if kind and all(c in '0123456789;' for c in params):
                        tokens.append((kind, params or ('0' if kind in ('sgr', 'el') else '1')))
                    self._state = 'text'
                    self._escape.clear()
                elif not 0x20 <= byte <= 0x3F:
                    tokens.append(('invalid_escape', 'syntax'))
                    self._state = 'text'
                    self._escape.clear()
            elif self._state == 'osc':
                if byte == 7:
                    self._state = 'text'
                    self._escape.clear()
                elif byte == 0x1B:
                    self._state = 'osc_escape'
            elif self._state == 'osc_escape':
                if byte == ord('\\'):
                    self._state = 'text'
                    self._escape.clear()
                else:
                    self._state = 'osc'
        flush(final=final)
        if final:
            if self._state != "text":
                tokens.append(("invalid_escape", "truncated"))
            self.reset()
        return tokens


@dataclass(frozen=True)
class TerminalStyle:
    foreground: int | None = None
    bold: bool = False


@dataclass(frozen=True)
class TerminalCell:
    text: str = ' '
    style: TerminalStyle = TerminalStyle()
    width: int = 1  # 2 为宽字符首格，0 为其延续格。


@dataclass
class TerminalRow:
    timestamp: str = ''
    cells: list[TerminalCell] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ''.join(cell.text for cell in self.cells)

    def text_offset(self, column: int) -> int:
        """单元格位置映射到 Qt 使用的 UTF-16 偏移。"""
        return len(''.join(cell.text for cell in self.cells[:column]).encode('utf-16-le')) // 2


@dataclass(frozen=True)
class TerminalUpdate:
    first_row: int
    removed_rows: int


class TerminalScreen:
    """本项目行交互的唯一状态源；Qt 文档只消费行快照。"""

    def __init__(self, timestamp: Callable[[], str], *, max_rows: int = 5000, max_columns: int = 16384) -> None:
        if max_rows < 1 or max_columns < 2:
            raise ValueError('terminal capacity must be positive')
        self._timestamp = timestamp
        self.max_rows = max_rows
        self.max_columns = max_columns
        self.reset()

    def reset(self) -> None:
        self.rows = [TerminalRow()]
        self.column = 0
        self.style = TerminalStyle()

    @property
    def row(self) -> TerminalRow:
        return self.rows[-1]

    def _stamp(self) -> None:
        if not self.row.timestamp:
            self.row.timestamp = self._timestamp()

    def _blank(self) -> TerminalCell:
        return TerminalCell(style=self.style)

    def _pad(self, length: int) -> None:
        self.row.cells.extend(self._blank() for _ in range(length - len(self.row.cells)))

    def _clear_wide(self, column: int) -> None:
        cells = self.row.cells
        if column < len(cells):
            if cells[column].width == 0 and column:
                cells[column - 1] = self._blank()
            elif cells[column].width == 2 and column + 1 < len(cells):
                cells[column + 1] = self._blank()
            cells[column] = self._blank()

    def _put(self, char: str) -> None:
        self._stamp()
        if unicodedata.combining(char) or unicodedata.category(char) in ('Mn', 'Me'):
            if self.column:
                index = min(self.column, len(self.row.cells)) - 1
                if index >= 0:
                    if self.row.cells[index].width == 0:
                        index -= 1
                    cell = self.row.cells[index]
                    self.row.cells[index] = replace(cell, text=cell.text + char)
            return
        width = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if self.column + width > self.max_columns:
            return
        self._pad(self.column + width)
        for index in range(self.column, self.column + width):
            self._clear_wide(index)
        self.row.cells[self.column] = TerminalCell(char, self.style, width)
        if width == 2:
            self.row.cells[self.column + 1] = TerminalCell('', self.style, 0)
        self.column += width

    def _sgr(self, params: str) -> None:
        codes = [int(part or '0') for part in params.split(';')]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0:
                self.style = TerminalStyle()
            elif code == 1:
                self.style = replace(self.style, bold=True)
            elif code == 22:
                self.style = replace(self.style, bold=False)
            elif code == 39:
                self.style = replace(self.style, foreground=None)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.style = replace(self.style, foreground=code)
            elif code in (38, 48) and index + 1 < len(codes):
                index += 2 if codes[index + 1] == 5 else 4 if codes[index + 1] == 2 else 0
            index += 1

    def feed(self, tokens: list[Token]) -> TerminalUpdate:
        first_row = len(self.rows) - 1
        for kind, value in tokens:
            if kind == 'sgr':
                self._sgr(value)
            elif kind in ('left', 'right', 'column'):
                count = max(1, int(value.split(';')[0] or '1'))
                self.column = min(self.max_columns, max(0, self.column - count if kind == 'left'
                                  else self.column + count if kind == 'right' else count - 1))
                self._stamp()
                self._pad(self.column)
            elif kind == 'el':
                mode = int(value.split(';')[0] or '0')
                self._stamp()
                if mode == 0:
                    if self.column < len(self.row.cells):
                        self._clear_wide(self.column)
                        del self.row.cells[self.column:]
                elif mode == 1:
                    stop = min(self.column + 1, self.max_columns)
                    self._pad(stop)
                    if self.column < self.max_columns:
                        self._clear_wide(self.column)
                    self.row.cells[:stop] = [self._blank() for _ in range(stop)]
                elif mode == 2:
                    self.row.cells.clear()
                    self._pad(self.column)
            elif kind == 'text':
                for char in value:
                    if char == '\r':
                        self.column = 0
                    elif char == '\n':
                        self.rows.append(TerminalRow())
                        self.column = 0
                    elif char == '\b':
                        self.column = max(0, self.column - 1)
                    elif char == '\t':
                        for _ in range(4):
                            self._put(' ')
                    elif ord(char) >= 0x20 and char != '\x7f':
                        self._put(char)
        removed = max(0, len(self.rows) - self.max_rows)
        if removed:
            del self.rows[:removed]
        return TerminalUpdate(max(0, first_row - removed), removed)
