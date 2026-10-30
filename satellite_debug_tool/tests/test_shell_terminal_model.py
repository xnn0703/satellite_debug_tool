"""M43 纯模型：流分包、单元格编辑、输入合同和现场脱敏证据。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from satellite_debug_tool.core.tcp_shell.terminal import (
    ShellInputError, TerminalScreen, TerminalStreamDecoder, command_text_bytes,
)


def _snapshot(chunks):
    decoder = TerminalStreamDecoder()
    screen = TerminalScreen(lambda: '[00:00:00.000] ')
    issues = []
    for chunk in chunks:
        tokens = decoder.feed(chunk)
        issues.extend((kind, value) for kind, value in tokens if kind.startswith('invalid_'))
        screen.feed(tokens)
    return [(row.text, row.cells) for row in screen.rows], screen.column, screen.style, issues


@pytest.mark.parametrize('payload', [
    '中文 — e\u0301 😀'.encode(),
    b'abc\bZ\x1b[2Dxy\r\x1b[Kdone\r\n',
    b'\x1b[31mred\x1b[1m!\x1b[22;39m plain',
    b'root:/$ \x1b[2K\r\r[== 10%\r\x1b[2K[=== 90%\r\n',
    b'a\x1b]0;window\x1b\\b\x1b]1;title\x07c',
    b'bad\xe2_\xff\r\nvalid',
])
def test_every_partition_preserves_terminal_state(payload):
    expected = _snapshot([payload])
    for cut in range(len(payload) + 1):
        assert _snapshot([payload[:cut], payload[cut:]]) == expected
    assert _snapshot([bytes([byte]) for byte in payload]) == expected


@pytest.mark.parametrize(('payload', 'text', 'column'), [
    (b'abc\b', 'abc', 2),
    (b'abc\bZ', 'abZ', 3),
    (b'abcdef\x1b[3DXY', 'abcXYf', 5),
    (b'abc\x1b[1GZ', 'Zbc', 1),
    (b'abc\rZ', 'Zbc', 1),
    (b'abc\r\x1b[KZ', 'Z', 1),
    (b'abc\x1b[2D\x1b[1K', '  c', 1),
    (b'abc\x1b[2K', '   ', 3),
    (b'abc\x1b[2Cz', 'abc  z', 6),
    ('中x\rZ'.encode(), 'Z x', 1),
    ('中x\x1b[2DZ'.encode(), ' Zx', 2),
    ('e\u0301x\bZ'.encode(), 'e\u0301Z', 2),
    (b'abc\b \b', 'ab ', 2),
])
def test_cell_and_cursor_semantics(payload, text, column):
    rows, actual_column, _style, _issues = _snapshot([payload])
    assert rows[-1][0] == text
    assert actual_column == column


def test_rows_and_control_sequences_are_bounded():
    screen = TerminalScreen(lambda: 'stamp', max_rows=3, max_columns=8)
    update = screen.feed([('text', '0\n1\n2\n3\n4')])
    assert len(screen.rows) == 3
    assert update.removed_rows == 2
    assert [row.text for row in screen.rows] == ['2', '3', '4']
    screen.feed([('right', '999999999999'), ('text', 'overflow')])
    assert screen.column == 8 and len(screen.row.cells) == 8
    decoder = TerminalStreamDecoder()
    tokens = decoder.feed(b'\x1b]' + b'a' * 2048 + b'\nvalid')
    assert any(kind == 'invalid_escape' for kind, _ in tokens)
    assert len(decoder._escape) <= decoder.MAX_ESCAPE_BYTES
    assert any('valid' in value for kind, value in tokens if kind == 'text')


def test_end_of_connection_flushes_incomplete_utf8_and_escape():
    decoder = TerminalStreamDecoder()
    assert decoder.feed(b'\xe2') == []
    assert ('invalid_utf8', 'E2') in decoder.finish()
    assert decoder.feed(b'fresh') == [('text', 'fresh')]
    decoder.feed(b'\x1b[')
    assert decoder.finish() == [('invalid_escape', 'truncated')]
    assert decoder.feed(b'fresh') == [('text', 'fresh')]


@pytest.mark.parametrize('text', ['——', '＿', '\u200b', '\u00a0', '１', '\x1b[A', '\x00', '\b', '\x7f', 'para ext_ins_rot 4\r\n中文'])
def test_command_text_rejects_whole_submission(text):
    with pytest.raises(ShellInputError):
        command_text_bytes(text)


def test_ascii_command_contract():
    assert command_text_bytes('para ext_ins_rot 4\r\n\t') == b'para ext_ins_rot 4\r\n\t'
    assert command_text_bytes('') == b''


def test_captured_parameter_edits_keep_byte_evidence():
    fixture = json.loads((Path(__file__).parent / 'fixtures/tcp_shell_ext_ins_rot_edits.json').read_text())
    entries = [(row['direction'], bytes.fromhex(row['hex'])) for row in fixture['entries']]
    assert ('tx', '——'.encode()) in entries
    with pytest.raises(ShellInputError):
        command_text_bytes(next(data for direction, data in entries if direction == 'tx' and data == '——'.encode()).decode())
    first_command = next(data for direction, data in entries if direction == 'rx' and data.startswith(b'para ext\xe2'))
    assert first_command == b'para ext\xe2\x80\x94\xe2_ins_rot 4'
    history_at = entries.index(('rx', first_command))
    line = bytearray(first_command)
    for direction, data in entries[history_at + 1:]:
        if direction == 'tx' and not data.startswith(b'\x1b'):
            for byte in data:
                if byte == 8:
                    line.pop()
                elif byte == 13:
                    assert bytes(line) == b'para extt_ins_rot 4'
                else:
                    line.append(byte)
    rx = [data for direction, data in entries if direction == 'rx']
    assert _snapshot(rx) == _snapshot([b''.join(rx)])
    assert _snapshot(rx)[-1]  # 原始损坏 UTF-8 保持可诊断。


def test_erase_to_start_at_last_column_keeps_capacity():
    screen = TerminalScreen(lambda: 'stamp', max_columns=8)
    screen.feed([('right', '999'), ('el', '1')])
    assert screen.column == 8
    assert len(screen.row.cells) == 8
