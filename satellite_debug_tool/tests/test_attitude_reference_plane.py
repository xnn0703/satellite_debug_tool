"""安装参考平面回归：所有模型显示共享的水平零姿态参考面。"""

from __future__ import annotations

import pytest
from pyqtgraph.opengl.items.GLGridItem import GLGridItem

from satellite_debug_tool.ui.attitude_widget import AttitudeWidget


def test_reference_plane_is_fixed_below_model(qapplication_session) -> None:
    widget = AttitudeWidget()
    try:
        assert isinstance(widget._grid, GLGridItem)
        assert widget._grid in widget._gl_view.items
        assert widget._grid.size() == [5.0, 5.0, 1.0]
        assert widget._grid.spacing() == [0.5, 0.5, 1.0]
        # 平面低于默认模型底面 z=-0.08，避免网格横穿机体。
        assert widget._reference_plane_z < -0.08

        widget.update_attitude(20.0, -10.0, 30.0, "roll", "pitch", "yaw")
        # 姿态变化只旋转机体，不改变参考平面本身。
        assert widget._grid in widget._gl_view.items
        assert widget._reference_plane_z == -0.25
    finally:
        widget.close()
        widget.deleteLater()
