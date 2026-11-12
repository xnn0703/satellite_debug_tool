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
        assert widget._grid.size() == [7.0, 7.0, 1.0]
        assert widget._grid.spacing() == [0.5, 0.5, 1.0]
        # 平面在模型中心包围球以外：归一化模型最长边 3.6（半 1.8），
        # 最坏包围球半径 ≤3.12，平面 z=-2.6 保证任意姿态都不与机体相交。
        assert widget._reference_plane_z <= -2.5

        widget.update_attitude(20.0, -10.0, 30.0, "roll", "pitch", "yaw")
        # 姿态变化只旋转机体，不改变参考平面本身（含尺寸）。
        assert widget._grid in widget._gl_view.items
        assert widget._reference_plane_z == -2.6
        assert list(widget._grid.size()) == [7.0, 7.0, 1.0]
    finally:
        widget.close()
        widget.deleteLater()
