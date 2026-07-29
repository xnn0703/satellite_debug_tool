"""仿真控制面板 GUI — 嵌入 LiveView，控制仿真参数。

简化版：只保留卫星选择 + 频段 + 雨衰 + 遮挡 + 实时 SNR 显示。
姿态/GPS 来自真实设备，不在面板控制。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from satellite_debug_tool.core.simulation.mock_modem import RealTimeReport
from satellite_debug_tool.core.simulation.presets import BAND_PRESETS
from satellite_debug_tool.i18n import register_translatable, tr
from satellite_debug_tool.ui import styles as S


_SATELLITE_CHOICES = (
    ("apstar_6", 134.0),
    ("chinasat_10", 110.0),
    ("chinasat_9", 92.2),
    ("sinosat_3", 125.0),
)


def _satellite_label(satellite_id: str) -> str:
    return {
        "apstar_6": tr("APSTAR-6 (134°E)"),
        "chinasat_10": tr("ChinaSat-10 (110.5°E)"),
        "chinasat_9": tr("ChinaSat-9 (92.2°E)"),
        "sinosat_3": tr("Sinosat-3 (125°E)"),
    }[satellite_id]


class SimulationPanelWidget(QWidget):
    """仿真控制面板。

    Signals:
        satellite_changed(longitude_deg, freq_ghz): 卫星参数变化
        blockage_requested(duration_s): 请求注入遮挡
        rain_fade_changed(depth_db): 雨衰深度变化
    """

    satellite_changed = Signal(float, float)
    blockage_requested = Signal(float)
    rain_fade_changed = Signal(float)
    snr_baseline_changed = Signal(float)
    heading_changed = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self._connect_signals()
        register_translatable(self)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # ---- 卫星 + 频段 ----
        sat_group = QGroupBox(tr("Satellite"))
        sat_layout = QGridLayout(sat_group)
        sat_layout.setContentsMargins(6, 10, 6, 6)
        sat_layout.setSpacing(4)

        sat_layout.addWidget(QLabel(tr("Preset:")), 0, 0)
        self._sat_combo = QComboBox()
        for satellite_id, longitude in _SATELLITE_CHOICES:
            self._sat_combo.addItem(_satellite_label(satellite_id), longitude)
        self._sat_combo.setCurrentIndex(0)
        sat_layout.addWidget(self._sat_combo, 0, 1, 1, 2)

        sat_layout.addWidget(QLabel(tr("Longitude:")), 1, 0)
        self._lon_spin = QDoubleSpinBox()
        self._lon_spin.setRange(-180.0, 180.0)
        self._lon_spin.setValue(134.0)
        self._lon_spin.setSuffix(" °E")
        self._lon_spin.setDecimals(1)
        sat_layout.addWidget(self._lon_spin, 1, 1)

        sat_layout.addWidget(QLabel(tr("Band:")), 2, 0)
        self._band_combo = QComboBox()
        for band in BAND_PRESETS:
            self._band_combo.addItem(band, band)
        sat_layout.addWidget(self._band_combo, 2, 1)

        sat_layout.addWidget(QLabel(tr("SNR baseline:")), 3, 0)
        self._baseline_spin = QDoubleSpinBox()
        self._baseline_spin.setRange(0.0, 30.0)
        self._baseline_spin.setValue(16.0)
        self._baseline_spin.setSuffix(" dB")
        self._baseline_spin.setDecimals(1)
        sat_layout.addWidget(self._baseline_spin, 3, 1)

        sat_layout.addWidget(QLabel(tr("Initial heading:")), 4, 0)
        self._heading_spin = QDoubleSpinBox()
        self._heading_spin.setRange(0.0, 359.9)
        self._heading_spin.setValue(0.0)
        self._heading_spin.setSuffix(" °")
        self._heading_spin.setDecimals(1)
        self._heading_spin.setToolTip(
            tr(
                "Ground-truth initial device heading used to verify heading "
                "calibration accuracy"
            )
        )
        sat_layout.addWidget(self._heading_spin, 4, 1)

        root.addWidget(sat_group)

        # ---- 场景控制 ----
        scene_group = QGroupBox(tr("Scenario"))
        scene_layout = QVBoxLayout(scene_group)
        scene_layout.setContentsMargins(6, 10, 6, 6)
        scene_layout.setSpacing(4)

        btn_row = QHBoxLayout()
        self._block_btn = QPushButton(tr("Blockage 5 s"))
        self._block_btn.setToolTip(tr("Simulate a 5-second blockage and loss of lock"))
        btn_row.addWidget(self._block_btn)
        scene_layout.addLayout(btn_row)

        # 雨衰滑块
        rain_row = QHBoxLayout()
        rain_row.addWidget(QLabel(tr("Rain fade:")))
        self._rain_slider = QSlider(Qt.Horizontal)
        self._rain_slider.setRange(0, 200)  # 0.0 ~ 20.0 dB
        self._rain_slider.setValue(0)
        rain_row.addWidget(self._rain_slider, 1)
        self._rain_label = QLabel("0.0 dB")
        self._rain_label.setMinimumWidth(50)
        rain_row.addWidget(self._rain_label)
        scene_layout.addLayout(rain_row)

        root.addWidget(scene_group)

        # ---- 实时参数 ----
        info_group = QGroupBox(tr("Live parameters"))
        info_layout = QGridLayout(info_group)
        info_layout.setContentsMargins(6, 10, 6, 6)
        info_layout.setSpacing(2)

        self._snr_label = self._add_info_row(info_layout, 0, "SNR:")
        self._rain_info_label = self._add_info_row(
            info_layout, 1, tr("Rain fade:")
        )
        self._scan_angle_label = self._add_info_row(
            info_layout, 2, tr("Scan angle θ:")
        )
        self._pointing_err_label = self._add_info_row(
            info_layout, 3, tr("Pointing error:")
        )
        self._pitch_label = self._add_info_row(info_layout, 4, "Pitch:")
        self._roll_label = self._add_info_row(info_layout, 5, "Roll:")
        self._heading_label = self._add_info_row(info_layout, 6, "Heading:")

        root.addWidget(info_group)
        root.addStretch()

    @staticmethod
    def _add_info_row(layout: QGridLayout, row: int, label_text: str) -> QLabel:
        lbl = QLabel(label_text)
        lbl.setStyleSheet("font-weight: 600;")
        layout.addWidget(lbl, row, 0)
        val = QLabel("—")
        layout.addWidget(val, row, 1)
        return val

    def _connect_signals(self) -> None:
        self._sat_combo.currentIndexChanged.connect(self._on_sat_changed)
        self._lon_spin.valueChanged.connect(self._on_sat_changed)
        self._band_combo.currentIndexChanged.connect(
            lambda _index: self.satellite_changed.emit(
                self._lon_spin.value(),
                BAND_PRESETS.get(
                    str(self._band_combo.currentData() or ""), {}
                ).get("freq_ghz", 20.0),
            )
        )
        self._block_btn.clicked.connect(lambda: self.blockage_requested.emit(5.0))
        self._rain_slider.valueChanged.connect(self._on_rain_slider)
        self._baseline_spin.valueChanged.connect(self.snr_baseline_changed.emit)
        self._heading_spin.valueChanged.connect(self.heading_changed.emit)

    # ---- 槽函数 ----

    def _on_sat_changed(self) -> None:
        data = self._sat_combo.currentData()
        lon = float(data) if data is not None else self._lon_spin.value()
        self._lon_spin.blockSignals(True)
        self._lon_spin.setValue(lon)
        self._lon_spin.blockSignals(False)
        band = str(self._band_combo.currentData() or "")
        freq_ghz = BAND_PRESETS.get(band, {}).get("freq_ghz", 20.0)
        self.satellite_changed.emit(lon, freq_ghz)

    def _on_rain_slider(self, value: int) -> None:
        db = value / 10.0
        self._rain_label.setText(f"{db:.1f} dB")
        self.rain_fade_changed.emit(db)

    # ---- 外部更新 ----

    def update_snr(self, snr_db: float) -> None:
        """更新 SNR 显示。"""
        self._snr_label.setText(f"{snr_db:.1f} dB")
        if snr_db > 8:
            color = "#4caf50"
        elif snr_db > 3:
            color = "#ff9800"
        else:
            color = "#f44336"
        self._snr_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    def update_report(self, report: RealTimeReport) -> None:
        """更新设备报告显示。"""
        self._scan_angle_label.setText(f"{abs(report.theta):.1f}°")
        self._pitch_label.setText(f"{report.pitch:.1f}°")
        self._roll_label.setText(f"{report.roll:.1f}°")
        self._heading_label.setText(f"{report.heading:.1f}°")

    def update_metrics(self, d: dict) -> None:
        """更新 SNR 计算指标。"""
        snr = d.get("snr", 0.0)
        pointing_err = d.get("pointing_err", 0.0)
        self._pointing_err_label.setText(f"{pointing_err:.2f}°")
        # SNR 着色
        if snr > 8:
            color = "#4caf50"
        elif snr > 3:
            color = "#ff9800"
        else:
            color = "#f44336"
        self._snr_label.setStyleSheet(f"color: {color}; font-weight: 600;")
