"""桌面图标的格式、透明度与应用启动合同。"""

import struct

from PySide6.QtGui import QImage, QImageReader
from PySide6.QtWidgets import QWidget
import pytest

from satellite_debug_tool.ui.application_icon import ICON_PATH, application_icon


def test_windows_icon_has_decodable_transparent_sizes(qapplication_session):
    reader = QImageReader(str(ICON_PATH))
    expected = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    assert reader.imageCount() == len(expected)
    for index, size in enumerate(expected):
        assert reader.jumpToImage(index)
        image = reader.read()
        assert (image.width(), image.height()) == (size, size)
        assert image.hasAlphaChannel()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(size // 2, size // 2).alpha() > 0
    assert sorted(size.width() for size in application_icon().availableSizes()) == list(expected)


def test_macos_icon_has_decodable_standard_and_retina_frames():
    data = ICON_PATH.with_suffix(".icns").read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    expected = {b"icp4": 16, b"icp5": 32, b"icp6": 64,
                b"ic07": 128, b"ic08": 256, b"ic09": 512, b"ic10": 1024,
                b"ic11": 32, b"ic12": 64, b"ic13": 256, b"ic14": 512}
    found = {}
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4]
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        assert length > 8
        image = QImage.fromData(data[offset + 8:offset + length], "PNG")
        assert image.width() == image.height() == expected[kind]
        assert image.pixelColor(0, 0).alpha() == 0
        found[kind] = image.width()
        offset += length
    assert offset == len(data)
    assert found == expected


def test_startup_sets_icon_inherited_by_windows(monkeypatch, qapplication_session, tmp_path):
    """回归：原启动入口的窗口图标为空；新入口在创建窗口前设置图标。"""
    import satellite_debug_tool.bootstrap as entry
    from satellite_debug_tool.edition import Edition
    from types import SimpleNamespace

    app = qapplication_session
    previous_icon = app.windowIcon()
    windows = []

    def create_window(**kwargs):
        window = QWidget()
        assert window.windowIcon().availableSizes() == application_icon().availableSizes()
        windows.append(window)
        return window

    monkeypatch.setattr(entry.sys, "argv", ["SatelliteDebugTool"])
    monkeypatch.setattr(entry, "QApplication", lambda args: app)
    monkeypatch.setattr(app, "exec", lambda: 0)
    monkeypatch.setattr(entry, "configure_application_logging", lambda **kwargs: None)
    monkeypatch.setattr(entry, "Settings", lambda **kwargs: SimpleNamespace(config_directory=tmp_path))
    monkeypatch.setattr(entry, "initialize_translation_manager", lambda *args: None)
    monkeypatch.setattr(entry.S, "load_bundled_fonts", lambda: None)
    monkeypatch.setattr(entry.S, "apply_global_font", lambda *args, **kwargs: None)
    try:
        with pytest.raises(SystemExit) as result:
            entry.run_application(Edition.OPS, create_window)
        assert result.value.code == 0
        assert len(windows) == 1
    finally:
        for window in windows:
            window.close()
        app.setWindowIcon(previous_icon)
