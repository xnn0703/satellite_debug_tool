"""M43：真实输入污染、跨包解码及终端光标回归。"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, Qt
from PySide6.QtGui import QKeyEvent

from satellite_debug_tool.tests.test_tcp_shell_ui import make_workspace, _strip_stamp


def _text(workspace) -> str:
    return '\n'.join(_strip_stamp(line) for line in workspace._terminal.toPlainText().split('\n'))


def test_utf8_split_uses_single_decode(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'\xe2')
    workspace.feed_rx(b'\x80\x94')
    assert _text(workspace) == '—'


def test_backspace_moves_cursor_and_preserves_cell(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'abc\b')
    assert _text(workspace) == 'abc'
    workspace.feed_rx(b'Z')
    assert _text(workspace) == 'abZ'


def test_horizontal_cursor_survives_packet(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'abc\x1b[D')
    workspace.feed_rx(b'Z')
    assert _text(workspace) == 'abZ'


def test_ime_dash_key_produces_no_command_bytes(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    terminal = workspace._terminal
    terminal.set_input_enabled(True)
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Underscore, Qt.KeyboardModifier.ShiftModifier, '——')
    terminal.keyPressEvent(event)
    assert workspace._controller.send_raw_calls == []


def test_mixed_unicode_paste_is_atomic(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace._terminal.set_input_enabled(True)
    mime = QMimeData()
    mime.setText('para ext——ins_rot 4\r')
    workspace._terminal.insertFromMimeData(mime)
    assert workspace._controller.send_raw_calls == []


def test_ascii_shift_underscore_is_sent(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    terminal = workspace._terminal
    terminal.set_input_enabled(True)
    terminal.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Underscore,
                                    Qt.KeyboardModifier.ShiftModifier, '_'))
    assert workspace._controller.send_raw_calls == [b'_']


def test_ime_commit_uses_command_validation(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtGui import QInputMethodEvent
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace._terminal.set_input_enabled(True)
    event = QInputMethodEvent()
    event.setCommitString('——')
    qapplication_session.sendEvent(workspace._terminal, event)
    assert workspace._controller.send_raw_calls == []
    assert 'U+2014' in workspace._input_feedback.text()
    assert not workspace._input_feedback.isHidden()
    event = QInputMethodEvent()
    event.setCommitString('_')
    qapplication_session.sendEvent(workspace._terminal, event)
    assert workspace._controller.send_raw_calls == [b'_']
    assert workspace._input_feedback.isHidden()


def test_platform_paste_shortcut_and_context_menu_share_validation(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtGui import QKeySequence
    from satellite_debug_tool.i18n import tr
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    terminal = workspace._terminal
    terminal.set_input_enabled(True)
    clipboard = qapplication_session.clipboard()
    previous = clipboard.text()
    try:
        clipboard.setText('para ext——ins_rot 4\r')
        shortcut = QKeySequence(QKeySequence.StandardKey.Paste)[0]
        event = QKeyEvent(QEvent.Type.KeyPress, shortcut.key(), shortcut.keyboardModifiers(), 'v')
        qapplication_session.sendEvent(terminal, event)
        assert workspace._controller.send_raw_calls == []
        assert 'U+2014' in workspace._input_feedback.text()
        clipboard.setText('para ext_ins_rot 4\r')
        menu = terminal._build_context_menu()
        next(action for action in menu.actions() if action.text() == tr('Paste') and action.isEnabled()).trigger()
        assert workspace._controller.send_raw_calls == [b'para ext_ins_rot 4\r']
    finally:
        clipboard.setText(previous)


def test_disabled_input_blocks_paste_and_ime(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtGui import QInputMethodEvent
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace._terminal.set_input_enabled(False)
    mime = QMimeData()
    mime.setText('reboot\r')
    workspace._terminal.insertFromMimeData(mime)
    event = QInputMethodEvent()
    event.setCommitString('reboot\r')
    qapplication_session.sendEvent(workspace._terminal, event)
    assert workspace._controller.send_raw_calls == []


def test_ansi_partial_state_preserved_in_hex_view(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'abc\x1b[')
    workspace._hex_check.setChecked(True)
    workspace.feed_rx(b'DZ')
    assert '44 5a' in workspace._terminal.toPlainText()
    workspace._hex_check.setChecked(False)
    assert _text(workspace) == 'abZ'
    assert workspace._screen.column == 3


def test_log_and_display_share_utf8_decoding(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    directory = tmp_path / 'logs'
    workspace._settings.set('tcp_shell.log_dir', str(directory))
    workspace._log_check.setChecked(True)
    for chunk in [b'\xe4', b'\xb8', b'\xad', b'\x1b[3', b'1mA']:
        workspace.feed_rx(chunk)
    assert _text(workspace) == '中A'
    assert next(directory.glob('*.log')).read_text() == '中A'
    workspace.shutdown()


def test_invalid_utf8_remains_diagnosable(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'para ext\xe2_ins_rot 4\r\n')
    assert '\ufffd' in _text(workspace)
    assert 'E2' in workspace._rx_feedback.text()
    assert not workspace._rx_feedback.isHidden()
    workspace.feed_rx(b'valid\r\n')
    assert 'valid' in _text(workspace)


def test_disconnect_flushes_partial_character(qapplication_session, tmp_path, monkeypatch):
    from satellite_debug_tool.tests.test_tcp_shell_ui import _wait_until

    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'\xe2')
    workspace._on_disconnected('test')
    # 终排空按帧完成：等待最后一帧执行 decoder.finish 后再断言末字符呈现。
    assert _wait_until(qapplication_session, lambda: not workspace._final_drain_pending)
    assert _text(workspace) == '\ufffd'
    workspace._on_connected('test')
    workspace.feed_rx(b'new')
    assert _text(workspace).endswith('new')


def test_document_selection_keeps_output_cursor_authority(qapplication_session, tmp_path, monkeypatch):
    from PySide6.QtGui import QTextCursor
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace.feed_rx(b'abc\x1b[D')
    cursor = workspace._terminal.textCursor()
    cursor.setPosition(0)
    cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 3)
    workspace._terminal.setTextCursor(cursor)
    workspace.feed_rx(b'Z')
    assert _text(workspace) == 'abZ'


def test_projection_prunes_history_and_preserves_color(qapplication_session, tmp_path, monkeypatch):
    workspace = make_workspace(qapplication_session, tmp_path, monkeypatch)
    workspace._screen.max_rows = 3
    workspace.feed_rx(b'0\r\n1\r\n2\r\n3\r\n\x1b[31m4')
    assert _text(workspace) == '2\n3\n4'
    workspace.feed_rx(b'\r\n5')
    assert _text(workspace) == '3\n4\n5'
    assert workspace._output_document.blockCount() == 3
    column = workspace._screen.column
    foreground = workspace._screen.style.foreground
    workspace.set_theme('light')
    workspace.feed_rx(b'X')
    assert _text(workspace) == '3\n4\n5X'
    assert foreground == workspace._screen.style.foreground == 31
    assert workspace._screen.column == column + 1
