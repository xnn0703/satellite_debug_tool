"""M48 设备图状态在语言切换时始终从稳定源键重新呈现。"""

from __future__ import annotations

import pytest

from satellite_debug_tool.ui import production_snr_widget as module


_ZH = {
    "Offline": "离线",
    "Online": "在线",
    "Batch not created": "批次尚未创建",
    "Evidence recording active": "证据录制中",
    "Waiting for device": "等待设备接入",
    "Slot {slot} | {identity}": "槽位 {slot} | {identity}",
}


@pytest.fixture
def language(monkeypatch):
    locale = ["zh_CN"]

    def translate(source: str, **values: object) -> str:
        translated = _ZH.get(source, source) if locale[0] == "zh_CN" else source
        return translated.format(**values)

    monkeypatch.setattr(module, "tr", translate)
    return locale


@pytest.mark.parametrize("initial_locale", ("zh_CN", "en_US"))
def test_unbound_chart_default_status_retranslates_repeatedly(
    qapplication_session, language, initial_locale: str,
) -> None:
    language[0] = initial_locale
    panel = module.ProductionSnrPanel(1, "#00aa88")

    for locale in ("zh_CN", "en_US", "zh_CN", "en_US"):
        language[0] = locale
        panel.retranslate_ui()
        expected = "- | 离线 | 批次尚未创建" if locale == "zh_CN" else "- | Offline | Batch not created"
        assert panel._connection_label.full_text == expected
        assert panel._connection == "Offline"
        assert panel._recording == "Batch not created"
        assert panel.serial_number == ""

    panel.close()


def test_status_source_keys_retranslate_while_device_identity_remains_fixed(
    qapplication_session, language,
) -> None:
    panel = module.ProductionSnrPanel(4, "#00aa88")
    panel.set_status(
        serial_number="AFD01D-001",
        endpoint="192.168.1.12:4004",
        connection="Online",
        recording="Evidence recording active",
    )

    for locale in ("en_US", "zh_CN", "en_US"):
        language[0] = locale
        panel.retranslate_ui()
        expected = (
            "192.168.1.12:4004 | 在线 | 证据录制中" if locale == "zh_CN"
            else "192.168.1.12:4004 | Online | Evidence recording active"
        )
        assert panel._connection_label.full_text == expected
        assert panel.serial_number == "AFD01D-001"
        assert "AFD01D-001" in panel._identity_label.full_text
        assert panel.slot == 4

    panel.close()


def test_custom_status_strings_and_composed_results_remain_verbatim(
    qapplication_session, language,
) -> None:
    panel = module.ProductionSnrPanel(1, "#00aa88")
    panel.set_status(connection="raw connection evidence", recording="raw recorder evidence",
                     current_test="静置对星 · 观察中", result="raw result")

    for locale in ("en_US", "zh_CN"):
        language[0] = locale
        panel.retranslate_ui()
        assert panel._connection_label.full_text == "- | raw connection evidence | raw recorder evidence"
        assert "静置对星 · 观察中" in panel._test_label.full_text
        assert "raw result" in panel._test_label.full_text

    panel.close()
