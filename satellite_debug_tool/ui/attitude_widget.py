"""
3D Attitude Display Widget using PyQtGraph OpenGL.
Displays an aircraft-like 3D model that rotates based on Roll, Pitch, Yaw channel values.
"""
from typing import Optional
import numpy as np

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox
from PySide6.QtCore import Qt, QTimer, Signal
import pyqtgraph.opengl as gl
from pyqtgraph.opengl import GLViewWidget, GLLinePlotItem, GLMeshItem, MeshData


class AttitudeWidget(QWidget):
    """
    3D attitude indicator widget.

    Displays a 3D aircraft model that rotates in real-time based on
    Roll, Pitch, and Yaw values from selected data channels.
    """

    channel_changed = Signal(str, str)  # (channel_name, axis)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._roll_ch = ""
        self._pitch_ch = ""
        self._yaw_ch = ""
        self._roll_value = 0.0
        self._pitch_value = 0.0
        self._yaw_value = 0.0
        self._is_dark = True
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Channel selector row
        selector = QWidget()
        selector_layout = QHBoxLayout(selector)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(8)

        for label_text, attr in [
            ("Roll:", "_roll_ch"),
            ("Pitch:", "_pitch_ch"),
            ("Yaw:", "_yaw_ch"),
        ]:
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #CCCCCC; font-size: 11px;")
            combo = QComboBox()
            combo.setEditable(True)
            combo.setPlaceholderText("—")
            combo.setStyleSheet(
                "background-color: #333; color: #CCCCCC; border: none; "
                "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
            )
            combo.currentTextChanged.connect(
                lambda text, a=attr: self._set_channel(a, text)
            )
            selector_layout.addWidget(lbl)
            selector_layout.addWidget(combo)

            if attr == "_roll_ch":
                self._roll_combo = combo
            elif attr == "_pitch_ch":
                self._pitch_combo = combo
            else:
                self._yaw_combo = combo

        selector_layout.addStretch()
        layout.addWidget(selector)

        # 3D OpenGL view
        self._gl_view = GLViewWidget()
        self._gl_view.setCameraPosition(distance=10, elevation=30, azimuth=45)
        self._gl_view.setBackgroundColor(0x1E, 0x1E, 0x1E)

        # Add grid plane
        grid = gl.GLGridItem()
        grid.setSize(20, 20)
        grid.setSpacing(1, 1)
        grid.setColor((0x3C, 0x3C, 0x3C, 0.8))
        self._gl_view.addItem(grid)

        # Add reference axes (body frame)
        self._axes = self._create_axes()
        for ax in self._axes.values():
            self._gl_view.addItem(ax)

        # Add aircraft body mesh
        self._aircraft = self._create_aircraft()
        self._gl_view.addItem(self._aircraft)

        layout.addWidget(self._gl_view, stretch=1)

        # Values display row
        values_widget = QWidget()
        values_layout = QHBoxLayout(values_widget)
        values_layout.setContentsMargins(0, 0, 0, 0)
        for label_text, attr in [
            ("Roll:", "_roll_value"),
            ("Pitch:", "_pitch_value"),
            ("Yaw:", "_yaw_value"),
        ]:
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #888888; font-size: 10px;")
            val_lbl = QLabel("0.0°")
            val_lbl.setStyleSheet("color: #CCCCCC; font-size: 10px; min-width: 50px;")
            values_layout.addWidget(lbl)
            values_layout.addWidget(val_lbl)
            if attr == "_roll_value":
                self._roll_val_lbl = val_lbl
            elif attr == "_pitch_value":
                self._pitch_val_lbl = val_lbl
            else:
                self._yaw_val_lbl = val_lbl
        values_layout.addStretch()
        layout.addWidget(values_widget)

    def _create_axes(self) -> dict:
        """Create X, Y, Z reference axes."""
        # X axis (red) - forward
        x_data = np.array([[0, 0, 0], [3, 0, 0]], dtype=np.float32)
        x_line = GLLinePlotItem(pos=x_data, color=(1, 0, 0, 1), width=2)
        # Y axis (green) - right
        y_data = np.array([[0, 0, 0], [0, 3, 0]], dtype=np.float32)
        y_line = GLLinePlotItem(pos=y_data, color=(0, 1, 0, 1), width=2)
        # Z axis (blue) - down
        z_data = np.array([[0, 0, 0], [0, 0, 3]], dtype=np.float32)
        z_line = GLLinePlotItem(pos=z_data, color=(0, 0, 1, 1), width=2)
        return {"x": x_line, "y": y_line, "z": z_line}

    def _create_aircraft(self) -> GLMeshItem:
        """
        Create a simple aircraft-like mesh.
        Nose points in +X direction, wings along Y, up is +Z.
        """
        # Vertices: [nose, tail_left, tail_right, tail_up, wing_left, wing_right]
        verts = np.array(
            [
                [3.0, 0.0, 0.0],    # 0: nose
                [-2.0, 0.0, 0.0],   # 1: tail center
                [-1.5, 0.0, 0.5],   # 2: tail top
                [-1.5, 1.2, 0.0],   # 3: left wing tip
                [-1.5, -1.2, 0.0],  # 4: right wing tip
                [-2.5, 0.3, 0.0],   # 5: left stab
                [-2.5, -0.3, 0.0],  # 6: right stab
            ],
            dtype=np.float32,
        )

        faces = [
            # Fuselage top
            [0, 3, 1],
            [0, 1, 4],
            # Tail fins
            [1, 2, 5],
            [1, 6, 2],
            # Wings
            [0, 3, 5],
            [0, 6, 4],
        ]

        meshdata = MeshData()
        meshdata.setFaces(np.array(faces, dtype=np.uint32))
        meshdata.setVertexes(verts)

        mesh = GLMeshItem(
            meshdata=meshdata,
            smooth=False,
            drawEdges=True,
            edgeColor=(0.5, 0.5, 0.5, 1),
        )
        mesh.setColor((0.6, 0.7, 0.9, 0.9))
        return mesh

    def _set_channel(self, attr: str, name: str):
        axis = ""
        if attr == "_roll_ch":
            self._roll_ch = name
            axis = "roll"
        elif attr == "_pitch_ch":
            self._pitch_ch = name
            axis = "pitch"
        else:
            self._yaw_ch = name
            axis = "yaw"
        self.channel_changed.emit(name, axis)

    def set_channel_options(self, names: list[str]) -> None:
        """Update combo box options with available channel names."""
        current_roll = self._roll_combo.currentText()
        current_pitch = self._pitch_combo.currentText()
        current_yaw = self._yaw_combo.currentText()

        for combo in [self._roll_combo, self._pitch_combo, self._yaw_combo]:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.blockSignals(False)

        if current_roll in names:
            self._roll_combo.setCurrentText(current_roll)
        if current_pitch in names:
            self._pitch_combo.setCurrentText(current_pitch)
        if current_yaw in names:
            self._yaw_combo.setCurrentText(current_yaw)

    def update_attitude(
        self,
        roll: float,
        pitch: float,
        yaw: float,
        roll_ch: str,
        pitch_ch: str,
        yaw_ch: str,
    ) -> None:
        """
        Update the 3D attitude display with new angle values.

        Args:
            roll: Roll angle in degrees
            pitch: Pitch angle in degrees
            yaw: Yaw angle in degrees
            roll_ch: Name of the roll channel
            pitch_ch: Name of the pitch channel
            yaw_ch: Name of the yaw channel
        """
        self._roll_value = roll
        self._pitch_value = pitch
        self._yaw_value = yaw
        self._roll_ch = roll_ch
        self._pitch_ch = pitch_ch
        self._yaw_ch = yaw_ch

        # Convert to radians for rotation matrix
        r = np.radians(roll)
        p = np.radians(pitch)
        y = np.radians(yaw)

        # Rotation matrix (Z-Y-X Euler angles: yaw, pitch, roll)
        # R = Rz(yaw) * Ry(pitch) * Rx(roll)
        cos_r, sin_r = np.cos(r), np.sin(r)
        cos_p, sin_p = np.cos(p), np.sin(p)
        cos_y, sin_y = np.cos(y), np.sin(y)

        # Rotation matrix elements
        r00 = cos_y * cos_p
        r01 = cos_y * sin_p * sin_r - sin_y * cos_r
        r02 = cos_y * sin_p * cos_r + sin_y * sin_r

        r10 = sin_y * cos_p
        r11 = sin_y * sin_p * sin_r + cos_y * cos_r
        r12 = sin_y * sin_p * cos_r - cos_y * sin_r

        r20 = -sin_p
        r21 = cos_p * sin_r
        r22 = cos_p * cos_r

        # Build 4x4 transformation matrix (rotation only, no translation)
        transform = np.array(
            [[r00, r01, r02, 0],
             [r10, r11, r12, 0],
             [r20, r21, r22, 0],
             [0,   0,   0,   1]], dtype=np.float32
        )
        self._aircraft.setTransform(transform)

        # Update value labels
        self._roll_val_lbl.setText(f"{roll:.1f}°")
        self._pitch_val_lbl.setText(f"{pitch:.1f}°")
        self._yaw_val_lbl.setText(f"{yaw:.1f}°")

        # Highlight active channel selectors
        self._roll_combo.setStyleSheet(
            "background-color: #0E639C; color: white; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
            if roll_ch else
            "background-color: #333; color: #CCCCCC; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
        )
        self._pitch_combo.setStyleSheet(
            "background-color: #0E639C; color: white; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
            if pitch_ch else
            "background-color: #333; color: #CCCCCC; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
        )
        self._yaw_combo.setStyleSheet(
            "background-color: #0E639C; color: white; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
            if yaw_ch else
            "background-color: #333; color: #CCCCCC; border: none; "
            "border-radius: 2px; padding: 2px 4px; font-size: 11px;"
        )

    def get_channel_selections(self) -> tuple[str, str, str]:
        """Return selected channel names for roll, pitch, yaw."""
        return (
            self._roll_combo.currentText(),
            self._pitch_combo.currentText(),
            self._yaw_combo.currentText(),
        )

    def set_dark_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        bg = (0x1E, 0x1E, 0x1E) if is_dark else (0xFF, 0xFF, 0xFF)
        self._gl_view.setBackgroundColor(*bg)

    def clear(self) -> None:
        """Reset the aircraft to default orientation."""
        self._roll_value = 0.0
        self._pitch_value = 0.0
        self._yaw_value = 0.0
        self._aircraft.setTransform(np.eye(3, dtype=np.float32))
        self._roll_val_lbl.setText("0.0°")
        self._pitch_val_lbl.setText("0.0°")
        self._yaw_val_lbl.setText("0.0°")
